from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from codex_watchdog import queue_wake
from codex_watchdog.queue_wake import QueueWakeDispatcher, REMOTE_UPDATE_PROMPT
from codex_watchdog.storage import InstructionCollisionError


THREAD_ID = "11111111-2222-4333-8444-555555555555"
QUEUE_ID = "99999999-aaaa-4bbb-8ccc-dddddddddddd"


@pytest.fixture(autouse=True)
def isolated_default_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            self.returncode,
            stdout=self.stdout
            or "Queued message " + QUEUE_ID + " for thread " + THREAD_ID + ".\r\n",
            stderr="" if self.returncode == 0 else "simulated failure",
        )


def create_queue_database(path: Path, revision: int = 0) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE queued_items (
                id TEXT PRIMARY KEY NOT NULL,
                thread_id TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE queued_thread_revisions (
                thread_id TEXT PRIMARY KEY NOT NULL,
                revision INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO queued_thread_revisions (thread_id, revision) VALUES (?, ?)",
            (THREAD_ID, revision),
        )


def test_extension_upgrade_refreshes_auto_executable_before_new_wake(tmp_path, monkeypatch):
    current = ["old-extension/codex.exe"]
    monkeypatch.setattr(queue_wake, "_resolve_codex_executable", lambda: current[0])
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(tmp_path, runner=runner)
    current[0] = "new-extension/codex.exe"
    assert dispatcher.dispatch(THREAD_ID, "upgrade-1", "Resume", "manual").status == "enqueued"
    assert runner.calls[0][0][0] == current[0]
    current[0] = "next-extension/codex.exe"
    assert dispatcher.dispatch(THREAD_ID, "upgrade-2", "Resume", "manual").status == "enqueued"
    assert runner.calls[1][0][0] == current[0]


def test_extension_refresh_preserves_explicit_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(queue_wake, "_resolve_codex_executable",
                        lambda: pytest.fail("explicit executable must not be rediscovered"))
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(tmp_path, codex_executable="selected-codex", runner=runner)
    dispatcher.dispatch(THREAD_ID, "explicit-1", "Resume", "manual")
    assert runner.calls[0][0][0] == "selected-codex"


def test_extension_refresh_does_not_retry_uncertain_wake(tmp_path, monkeypatch):
    monkeypatch.setattr(queue_wake, "_resolve_codex_executable", lambda: "old-codex")
    calls = []
    def missing(argv, **kwargs):
        calls.append(argv)
        raise FileNotFoundError("executable removed during upgrade")
    dispatcher = QueueWakeDispatcher(tmp_path, runner=missing)
    assert dispatcher.dispatch(THREAD_ID, "missing-1", "Resume", "manual").status == "uncertain"
    monkeypatch.setattr(queue_wake, "_resolve_codex_executable",
                        lambda: pytest.fail("uncertain receipt must not launch again"))
    receipt = dispatcher.dispatch(THREAD_ID, "missing-1", "Resume", "manual")
    assert receipt.status == "uncertain" and receipt.deduplicated and len(calls) == 1


def test_codex_executable_on_path_wins_over_extension_fallback(tmp_path: Path,) -> None:
    path_executable = tmp_path / "path" / "codex.exe"

    resolved = queue_wake._resolve_codex_executable(
        home=tmp_path,
        which=lambda command: str(path_executable) if command == "codex" else None,
        platform_name="nt",
    )

    assert resolved == str(path_executable.resolve())


def test_latest_installed_vscode_codex_is_used_when_path_is_missing(
    tmp_path: Path,
) -> None:
    older = (
        tmp_path
        / ".vscode"
        / "extensions"
        / "openai.chatgpt-26.99.1-win32-x64"
        / "bin"
        / "windows-x86_64"
        / "codex.exe"
    )
    newer = (
        tmp_path
        / ".vscode"
        / "extensions"
        / "openai.chatgpt-26.825.51511-win32-x64"
        / "bin"
        / "windows-x86_64"
        / "codex.exe"
    )
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")

    resolved = queue_wake._resolve_codex_executable(
        home=tmp_path, which=lambda _command: None, platform_name="nt"
    )

    assert resolved == str(newer.resolve())


@pytest.mark.parametrize(
    ("platform_name", "target"),
    (("linux", "linux-x86_64"), ("darwin", "darwin-arm64")),
)
def test_posix_vscode_codex_extension_fallback_is_executable(
    tmp_path: Path, platform_name: str, target: str
) -> None:
    executable = (
        tmp_path
        / ".vscode"
        / "extensions"
        / f"openai.chatgpt-26.900.1-{target}"
        / "bin"
        / target
        / "codex"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    executable.chmod(0o755)

    resolved = queue_wake._resolve_codex_executable(
        home=tmp_path,
        which=lambda _command: None,
        platform_name=platform_name,
    )

    assert resolved == str(executable.resolve())


def test_dispatcher_uses_automatically_resolved_codex_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex.exe"
    runner = FakeRunner()
    monkeypatch.setattr(
        queue_wake, "_resolve_codex_executable", lambda: str(executable)
    )

    dispatcher = QueueWakeDispatcher(tmp_path, runner=runner)
    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")

    assert runner.calls[0][0][0] == str(executable)


def test_first_party_queue_command_is_narrow_and_duplicate_suppressed(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(
        tmp_path, codex_executable="codex.exe", runner=runner
    )

    first = dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")
    second = dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")

    assert first.status == "enqueued"
    assert first.queue_message_id == QUEUE_ID
    assert second.status == "enqueued"
    assert second.deduplicated is True
    assert len(runner.calls) == 1
    argv = runner.calls[0][0]
    assert argv[:4] == ["codex.exe", "queue", "--thread", THREAD_ID]
    assert "id=wake-1" in argv[-1]
    assert runner.calls[0][1]["env"]["CODEX_HOME"] == str(dispatcher.codex_home)


def test_duplicate_dispatch_reconciles_existing_enqueue_without_resending(
    tmp_path: Path,
) -> None:
    database = tmp_path / "queue_1.sqlite"
    create_queue_database(database)
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(
        tmp_path / "runtime",
        runner=runner,
        codex_home=tmp_path,
        queue_database=database,
    )

    first = dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE queued_thread_revisions SET revision = 2 WHERE thread_id = ?",
            (THREAD_ID,),
        )
    second = dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")

    assert first.status == "enqueued"
    assert second.status == "consumed_or_started"
    assert second.deduplicated is True
    assert len(runner.calls) == 1


def test_failed_queue_is_uncertain_and_never_retried_automatically(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(returncode=1)
    dispatcher = QueueWakeDispatcher(tmp_path, runner=runner)

    first = dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")
    second = dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")

    assert first.status == "uncertain"
    assert second.status == "uncertain"
    assert len(runner.calls) == 1


def test_remote_git_source_uses_fixed_mechanical_prompt_and_safe_record_name(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(tmp_path, runner=runner)

    receipt = dispatcher.dispatch_remote_update(THREAD_ID, "a" * 40)

    assert receipt.status == "enqueued"
    assert REMOTE_UPDATE_PROMPT in runner.calls[0][0][-1]
    assert REMOTE_UPDATE_PROMPT.startswith("You were resumed by WatchDog.")
    assert "temporal/resume prompt first" in REMOTE_UPDATE_PROMPT
    assert ".codex-watchdog/resume/archive/" in REMOTE_UPDATE_PROMPT
    assert "do not delete it" in REMOTE_UPDATE_PROMPT
    assert "synchronize Git safely" in REMOTE_UPDATE_PROMPT
    assert "do not discard work" in REMOTE_UPDATE_PROMPT.lower()
    assert "hard-reset" in REMOTE_UPDATE_PROMPT
    assert "substantive decision is genuinely unclear" in REMOTE_UPDATE_PROMPT
    assert "new unprocessed `## comment`" in REMOTE_UPDATE_PROMPT
    record_names = [
        path.name for path in (tmp_path / "wake" / "records").glob("*.json")
    ]
    assert len(record_names) == 1
    assert ":" not in record_names[0]


def test_resume_prompt_is_atomically_claimed_and_retained_inflight(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(tmp_path, runner=runner)
    resume = tmp_path / "resume_prompt.md"
    resume.write_text("Continue the operational recovery verbatim.", encoding="utf-8")

    receipt = dispatcher.claim_and_dispatch_resume_prompt(THREAD_ID)

    assert receipt is not None
    assert receipt.status == "enqueued"
    assert not resume.exists()
    inflight = list((tmp_path / "resume" / "inflight").glob("*.md"))
    assert len(inflight) == 1
    assert (
        inflight[0].read_text(encoding="utf-8")
        == "Continue the operational recovery verbatim."
    )
    assert "RESUME_PROMPT_DISPOSITION: DISCARD" in runner.calls[0][0][-1]


def test_wake_id_reuse_for_another_thread_is_a_collision(tmp_path: Path) -> None:
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(tmp_path, runner=runner)
    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")

    with pytest.raises(InstructionCollisionError):
        dispatcher.dispatch(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "wake-1", "Resume once", "manual",
        )


def test_wake_record_stores_output_digests_not_raw_output(tmp_path: Path) -> None:
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(tmp_path, runner=runner)

    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")

    record_path = next((tmp_path / "wake" / "records").glob("*.json"))
    record_text = record_path.read_text(encoding="utf-8")
    record = json.loads(record_text)
    assert "Queued message" not in record_text
    assert record["stdout_chars"] > 0
    assert record["stdout_sha256"]


@pytest.mark.parametrize(
    "stdout",
    [
        "not an acknowledgement\n",
        "Queued message not-a-uuid for thread " + THREAD_ID + ".\n",
        "Queued message "
        + QUEUE_ID
        + " for thread aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.\n",
        "Queued message " + QUEUE_ID + " for thread " + THREAD_ID + ".\nextra\n",
    ],
)
def test_zero_exit_requires_one_exact_matching_acknowledgement(
    tmp_path: Path, stdout: str
) -> None:
    receipt = QueueWakeDispatcher(tmp_path, runner=FakeRunner(stdout=stdout)).dispatch(
        THREAD_ID, "wake-1", "Resume once", "manual"
    )

    assert receipt.status == "uncertain"
    assert receipt.queue_message_id is None


def test_nonzero_exit_never_accepts_valid_looking_acknowledgement(
    tmp_path: Path,
) -> None:
    receipt = QueueWakeDispatcher(tmp_path, runner=FakeRunner(returncode=1)).dispatch(
        THREAD_ID, "wake-1", "Resume once", "manual"
    )

    assert receipt.status == "uncertain"


def test_stale_revision_and_absent_row_do_not_promote(tmp_path: Path) -> None:
    database = tmp_path / "queue_1.sqlite"
    create_queue_database(database, revision=7)
    dispatcher = QueueWakeDispatcher(
        tmp_path / "runtime",
        runner=FakeRunner(),
        codex_home=tmp_path,
        queue_database=database,
    )
    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")

    receipt = dispatcher.observe_delivery("wake-1")

    assert receipt.status == "enqueued"
    assert receipt.deduplicated is False


def test_queue_observation_promotes_seen_then_removed_item(tmp_path: Path,) -> None:
    database = tmp_path / "queue_1.sqlite"
    create_queue_database(database)
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(
        tmp_path / "runtime",
        runner=runner,
        codex_home=tmp_path,
        queue_database=database,
    )
    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO queued_items (id, thread_id, payload_json) VALUES (?, ?, ?)",
            (QUEUE_ID, THREAD_ID, runner.calls[0][0][-1]),
        )
        connection.execute(
            "UPDATE queued_thread_revisions SET revision = 1 WHERE thread_id = ?",
            (THREAD_ID,),
        )
    assert dispatcher.observe_delivery("wake-1").status == "enqueued"

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM queued_items WHERE id = ?", (QUEUE_ID,))
        connection.execute(
            "UPDATE queued_thread_revisions SET revision = 2 WHERE thread_id = ?",
            (THREAD_ID,),
        )

    receipt = dispatcher.observe_delivery("wake-1")
    assert receipt.status == "consumed_or_started"
    assert receipt.queue_message_id == QUEUE_ID


def test_immediate_insert_delete_revision_cycle_promotes(tmp_path: Path) -> None:
    database = tmp_path / "queue_1.sqlite"
    create_queue_database(database)
    dispatcher = QueueWakeDispatcher(
        tmp_path / "runtime",
        runner=FakeRunner(),
        codex_home=tmp_path,
        queue_database=database,
    )
    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE queued_thread_revisions SET revision = 2 WHERE thread_id = ?",
            (THREAD_ID,),
        )

    assert dispatcher.observe_delivery("wake-1").status == "consumed_or_started"


def test_queue_read_error_after_row_seen_does_not_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "queue_1.sqlite"
    create_queue_database(database)
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(
        tmp_path / "runtime",
        runner=runner,
        codex_home=tmp_path,
        queue_database=database,
    )
    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO queued_items (id, thread_id, payload_json) VALUES (?, ?, ?)",
            (QUEUE_ID, THREAD_ID, runner.calls[0][0][-1]),
        )
    assert dispatcher.observe_delivery("wake-1").status == "enqueued"

    def fail_snapshot(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated locked database")

    monkeypatch.setattr(dispatcher, "_queue_snapshot", fail_snapshot)

    assert dispatcher.observe_delivery("wake-1").status == "enqueued"


def test_pinned_queue_database_wins_over_newer_compatible_database(
    tmp_path: Path,
) -> None:
    pinned = tmp_path / "queue_1.sqlite"
    newer = tmp_path / "queue_2.sqlite"
    create_queue_database(pinned)
    create_queue_database(newer)
    dispatcher = QueueWakeDispatcher(
        tmp_path / "runtime", runner=FakeRunner(), codex_home=tmp_path
    )

    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")

    record = json.loads(
        next((tmp_path / "runtime" / "wake" / "records").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert Path(record["queue_database"]) == pinned.resolve()


def test_queue_database_outside_selected_codex_home_is_rejected(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    database = tmp_path / "other" / "queue_1.sqlite"
    database.parent.mkdir()
    create_queue_database(database)

    with pytest.raises(ValueError, match="inside the selected Codex home"):
        QueueWakeDispatcher(
            tmp_path / "runtime", codex_home=codex_home, queue_database=database,
        )


def test_exact_new_rollout_user_message_promotes_to_started(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    rollout = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "30"
        / f"rollout-test-{THREAD_ID}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"baseline"}\n', encoding="utf-8")
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(
        tmp_path / "runtime", runner=runner, codex_home=codex_home
    )
    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")
    turn_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    events = [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_id,},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": THREAD_ID,
                "turn_id": turn_id,
                "item": {
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": runner.calls[0][0][-1]}],
                },
            },
        },
    ]
    with rollout.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")

    receipt = dispatcher.observe_delivery("wake-1")

    assert receipt.status == "started"
    record = json.loads(
        next((tmp_path / "runtime" / "wake" / "records").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert record["started_turn_id"] == turn_id


def test_assistant_marker_echo_is_not_started_evidence(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    rollout = codex_home / "sessions" / f"rollout-test-{THREAD_ID}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("", encoding="utf-8")
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(
        tmp_path / "runtime", runner=runner, codex_home=codex_home
    )
    dispatcher.dispatch(THREAD_ID, "wake-1", "Resume once", "manual")
    turn_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id,},
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "thread_id": THREAD_ID,
                        "turn_id": turn_id,
                        "item": {
                            "type": "AgentMessage",
                            "content": [
                                {"type": "text", "text": runner.calls[0][0][-1]}
                            ],
                        },
                    },
                }
            )
            + "\n"
        )

    assert dispatcher.observe_delivery("wake-1").status == "enqueued"


INTERRUPTED_TURN = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_TURN = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
OTHER_THREAD = "22222222-3333-4444-8555-666666666666"


def wake_event(kind, turn=INTERRUPTED_TURN, **fields):
    return {"timestamp": "2026-09-17T02:00:00Z", "type": "event_msg",
            "payload": {"type": kind, "turn_id": turn, **fields}}


def wake_user(text, turn=INTERRUPTED_TURN, thread=THREAD_ID):
    return wake_event("item_completed", turn, thread_id=thread,
                      item={"type": "UserMessage", "content": [{"type": "text", "text": text}]})


def append_wake_events(path, *events):
    with path.open("ab") as handle:
        for event in events:
            handle.write((json.dumps(event) + "\n").encode())


@pytest.fixture
def queued_rollout(tmp_path):
    """A persisted, observed then consumed queue item, never a fabricated ACK."""
    def create(*, source="manual", instruction="wake-1", prompt="Resume once", history=()):
        home = tmp_path / "codex-home"
        rollout = home / "sessions" / f"rollout-test-{THREAD_ID}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.touch()
        append_wake_events(rollout, *history)
        database = home / "queue_1.sqlite"
        create_queue_database(database)
        runner = FakeRunner()
        dispatcher = QueueWakeDispatcher(tmp_path / "runtime", runner=runner,
            codex_executable="fixture-codex", codex_home=home, queue_database=database)
        dispatcher.dispatch(THREAD_ID, instruction, prompt, source)
        wrapped = runner.calls[0][0][-1]
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO queued_items VALUES (?,?,?)", (QUEUE_ID, THREAD_ID, wrapped))
        assert dispatcher.observe_delivery(instruction).status == "enqueued"
        with sqlite3.connect(database) as db:
            db.execute("DELETE FROM queued_items WHERE id=?", (QUEUE_ID,))
        assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"
        record_path = next(dispatcher.records.glob("*.json"))
        return dispatcher, runner, rollout, record_path, wrapped, instruction
    return create


@pytest.mark.parametrize("added_lf", [False, True])
def test_completed_existing_interrupted_turn_promotes_old_receipt(queued_rollout, added_lf):
    instruction = f"linux-continue:{THREAD_ID}:{INTERRUPTED_TURN}"
    started = wake_event("task_started")
    started["timestamp"] = "2026-09-17T01:58:08.516Z"
    dispatcher, runner, rollout, path, wrapped, _ = queued_rollout(
        source="linux_interruption", instruction=instruction, history=[started])
    before = json.loads(path.read_text())
    before["created_at"] = "2026-09-17T01:58:38.981078Z"
    before["consumed_or_started_at"] = "2026-09-17T01:59:41.606969Z"
    before["future_compatible_field"] = {"keep": True}
    path.write_text(json.dumps(before))
    complete = wake_event("task_complete")
    complete["timestamp"] = "2026-09-17T02:13:34.843Z"
    append_wake_events(rollout, wake_user(wrapped + ("\n" if added_lf else "")), complete)

    # A fresh dispatcher models an upgrade/restart reading the old schema-2 receipt.
    restarted = QueueWakeDispatcher(dispatcher.runtime, codex_executable="fixture-codex",
        codex_home=dispatcher.codex_home, runner=runner)
    receipt = restarted.dispatch(THREAD_ID, instruction, "Resume once", "linux_interruption")
    assert receipt.status == "started" and receipt.deduplicated
    after = json.loads(path.read_text())
    assert after["started_turn_id"] == INTERRUPTED_TURN
    assert after["future_compatible_field"] == {"keep": True}
    for key in ("created_at", "schema_version", "prompt_sha256", "queue_message_id",
                "rollout_baseline_offset", "consumed_or_started_at"):
        assert after[key] == before[key]
    assert restarted.dispatch(THREAD_ID, instruction, "Resume once", "linux_interruption").deduplicated
    assert len(runner.calls) == 1
    with sqlite3.connect(dispatcher.queue_database) as db:
        assert db.execute("SELECT count(*) FROM queued_items").fetchone()[0] == 0


def test_one_added_terminal_lf_promotes_new_turn_without_rewriting_prompt(queued_rollout):
    dispatcher, runner, rollout, path, wrapped, instruction = queued_rollout()
    before = json.loads(path.read_text())
    append_wake_events(rollout, wake_event("task_started"), wake_user(wrapped + "\n"))
    assert dispatcher.observe_delivery(instruction).status == "started"
    assert json.loads(path.read_text())["prompt_sha256"] == before["prompt_sha256"]
    assert len(runner.calls) == 1


@pytest.mark.parametrize("suffix", ["\n\n", "\r\n", "\r", " ", "\t", " \n", "\n ", "\n\r"])
def test_rollout_does_not_normalize_other_prompt_changes(queued_rollout, suffix):
    dispatcher, runner, rollout, _, wrapped, instruction = queued_rollout()
    append_wake_events(rollout, wake_event("task_started"), wake_user(wrapped + suffix))
    assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"
    assert len(runner.calls) == 1


@pytest.mark.parametrize("prompt", ["Resume once\n", "Resume once ", "  Resume once", "Resume once\r"])
@pytest.mark.parametrize("extra_lf", ["", "\n"])
def test_rollout_preserves_original_prompt_whitespace(queued_rollout, prompt, extra_lf):
    dispatcher, _, rollout, _, wrapped, instruction = queued_rollout(prompt=prompt)
    append_wake_events(rollout, wake_event("task_started"), wake_user(wrapped + extra_lf))
    assert dispatcher.observe_delivery(instruction).status == "started"


@pytest.mark.parametrize("gate", [
    "source", "instruction_thread", "instruction_turn", "instruction_shape", "noncanonical_id",
    "other_thread", "other_turn", "wrong_hash", "wrong_marker", "leading_edit", "missing_user",
    "completion_only", "completion_before_user", "missing_completion", "wrong_completion_turn",
    "wrong_completion_thread", "assistant_echo", "mixed_content", "invalid_turn",
])
def test_existing_turn_requires_exact_unambiguous_postbaseline_proof(queued_rollout, gate):
    instruction = f"linux-continue:{THREAD_ID}:{INTERRUPTED_TURN}"
    source = "linux_interruption"
    if gate == "source":
        source = "manual"
    elif gate == "instruction_thread":
        instruction = f"linux-continue:{OTHER_THREAD}:{INTERRUPTED_TURN}"
    elif gate == "instruction_turn":
        instruction = f"linux-continue:{THREAD_ID}:{OTHER_TURN}"
    elif gate == "instruction_shape":
        instruction += ":suffix"
    elif gate == "noncanonical_id":
        instruction = f"linux-continue:{THREAD_ID}:{INTERRUPTED_TURN.upper()}"
    dispatcher, runner, rollout, _, wrapped, _ = queued_rollout(
        source=source, instruction=instruction, history=[wake_event("task_started")])
    user, complete = wake_user(wrapped), wake_event("task_complete")
    if gate == "other_thread":
        user["payload"]["thread_id"] = OTHER_THREAD
    elif gate == "other_turn":
        user["payload"]["turn_id"] = OTHER_TURN
        complete["payload"]["turn_id"] = OTHER_TURN
    elif gate == "wrong_hash":
        user = wake_user(wrapped + "changed")
    elif gate == "wrong_marker":
        user = wake_user(wrapped.replace("linux-continue:", "forged-continue:", 1))
    elif gate == "leading_edit":
        user = wake_user(" " + wrapped)
    elif gate in ("missing_user", "completion_only"):
        user = {"type": "response_item", "payload": {"type": "message", "role": "user"}}
    elif gate == "wrong_completion_turn":
        complete["payload"]["turn_id"] = OTHER_TURN
    elif gate == "wrong_completion_thread":
        complete["payload"]["thread_id"] = OTHER_THREAD
    elif gate == "assistant_echo":
        user["payload"]["item"]["type"] = "AgentMessage"
    elif gate == "mixed_content":
        user["payload"]["item"]["content"].append({"type": "text", "text": "extra instruction"})
    elif gate == "invalid_turn":
        user["payload"]["turn_id"] = complete["payload"]["turn_id"] = "not-a-turn"
    events = [user, complete]
    if gate == "completion_before_user":
        events.reverse()
    elif gate == "missing_completion":
        events.pop()
    append_wake_events(rollout, *events)
    assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"
    assert len(runner.calls) == 1


@pytest.mark.parametrize("resumed", [False, True])
def test_prebaseline_exact_user_echo_cannot_prove_delivery(queued_rollout, resumed):
    instruction = f"linux-continue:{THREAD_ID}:{INTERRUPTED_TURN}" if resumed else "wake-1"
    digest = hashlib.sha256(b"Resume once").hexdigest()
    wrapped = f"[CODEX_WATCHDOG_WAKE id={instruction} sha256={digest}]\nResume once"
    dispatcher, _, rollout, _, _, _ = queued_rollout(
        source="linux_interruption" if resumed else "manual", instruction=instruction,
        history=[wake_event("task_started"), wake_user(wrapped)])
    append_wake_events(rollout, wake_event("task_complete"))
    assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"


@pytest.mark.parametrize("resumed", [False, True])
def test_competing_exact_wake_turns_are_ambiguous(queued_rollout, resumed):
    instruction = f"linux-continue:{THREAD_ID}:{INTERRUPTED_TURN}" if resumed else "wake-1"
    dispatcher, _, rollout, _, wrapped, _ = queued_rollout(
        source="linux_interruption" if resumed else "manual", instruction=instruction)
    append_wake_events(rollout, wake_event("task_started"), wake_user(wrapped),
        wake_event("task_complete"), wake_event("task_started", OTHER_TURN),
        wake_user(wrapped, OTHER_TURN))
    assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"


@pytest.mark.parametrize("bad", [b'{broken}\n', b'null\n', b'[]\n', b'\xff\n',
                                 b'{"type":"event_msg","payload":null}\n', b'{"partial":'])
def test_malformed_or_incomplete_suffix_fails_closed(queued_rollout, bad):
    dispatcher, _, rollout, _, wrapped, instruction = queued_rollout()
    append_wake_events(rollout, wake_event("task_started"), wake_user(wrapped))
    with rollout.open("ab") as handle:
        handle.write(bad)
    assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"


@pytest.mark.parametrize("offset", [-1, True, 0.5, "0", 1, 10**12])
def test_invalid_rollout_baseline_fails_closed(queued_rollout, offset):
    dispatcher, _, rollout, path, wrapped, instruction = queued_rollout()
    append_wake_events(rollout, wake_event("task_started"), wake_user(wrapped))
    record = json.loads(path.read_text())
    record["rollout_baseline_offset"] = offset
    path.write_text(json.dumps(record))
    assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"


def test_completion_with_exact_thread_and_repeated_same_turn_evidence_is_valid(queued_rollout):
    instruction = f"linux-continue:{THREAD_ID}:{INTERRUPTED_TURN}"
    dispatcher, _, rollout, _, wrapped, _ = queued_rollout(
        source="linux_interruption", instruction=instruction)
    append_wake_events(rollout, wake_user(wrapped), wake_user(wrapped),
        wake_event("task_complete", thread_id=THREAD_ID))
    assert dispatcher.observe_delivery(instruction).status == "started"


def test_unfinished_native_line_can_be_reconciled_on_a_later_read(queued_rollout):
    dispatcher, runner, rollout, _, wrapped, instruction = queued_rollout()
    append_wake_events(rollout, wake_event("task_started"))
    with rollout.open("ab") as handle:
        handle.write(json.dumps(wake_user(wrapped)).encode())
    assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"
    with rollout.open("ab") as handle:
        handle.write(b"\n")
    assert dispatcher.observe_delivery(instruction).status == "started"
    assert len(runner.calls) == 1


def test_new_turn_postbaseline_start_can_follow_its_user_message(queued_rollout):
    dispatcher, _, rollout, _, wrapped, instruction = queued_rollout()
    append_wake_events(rollout, wake_user(wrapped), wake_event("task_started"))
    assert dispatcher.observe_delivery(instruction).status == "started"


def test_new_turn_conflicting_native_start_thread_fails_closed(queued_rollout):
    dispatcher, _, rollout, _, wrapped, instruction = queued_rollout()
    append_wake_events(rollout, wake_event("task_started", thread_id=OTHER_THREAD), wake_user(wrapped))
    assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"


def test_rollout_scan_streams_only_complete_postbaseline_events(queued_rollout, monkeypatch):
    history = [{"type": "irrelevant", "padding": "history" * 1000}] * 20
    dispatcher, _, rollout, path, wrapped, instruction = queued_rollout(history=history)
    append_wake_events(rollout, *[{"type": "irrelevant", "padding": "later" * 100}] * 2000,
                       wake_event("task_started"), wake_user(wrapped))
    baseline = json.loads(path.read_text())["rollout_baseline_offset"]
    original = Path.open
    reads, seeks, lines = [], [], []

    class StreamOnly:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def fileno(self):
            return self.handle.fileno()
        def seek(self, offset):
            seeks.append(offset)
            return self.handle.seek(offset)
        def read(self, size=-1):
            assert size == 1, "only the baseline boundary may use read()"
            reads.append(size)
            return self.handle.read(size)
        def readline(self, limit):
            line = self.handle.readline(limit)
            lines.append(len(line))
            return line

    def tracked_open(path, mode="r", *args, **kwargs):
        handle = original(path, mode, *args, **kwargs)
        return StreamOnly(handle) if path == rollout and mode == "rb" else handle
    monkeypatch.setattr(Path, "open", tracked_open)
    assert dispatcher.observe_delivery(instruction).status == "started"
    assert reads == [1] and seeks == [baseline - 1, baseline]
    assert len(lines) == 2002 and max(lines) < 2000


@pytest.mark.parametrize("change", ["truncate", "replace"])
def test_rollout_changed_during_scan_cannot_promote(queued_rollout, monkeypatch, change):
    dispatcher, _, rollout, _, wrapped, instruction = queued_rollout()
    append_wake_events(rollout, wake_event("task_started"), wake_user(wrapped))
    original = Path.open
    changed = []

    class ChangingStream:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def fileno(self):
            return self.handle.fileno()
        def seek(self, offset):
            return self.handle.seek(offset)
        def readline(self, limit):
            line = self.handle.readline(limit)
            if not changed:
                changed.append(True)
                if change == "replace":
                    rollout.rename(rollout.with_suffix(".old"))
                with original(rollout, "wb") as out:
                    out.write(b"{}\n")
            return line

    def tracked_open(path, mode="r", *args, **kwargs):
        handle = original(path, mode, *args, **kwargs)
        return ChangingStream(handle) if path == rollout and mode == "rb" else handle
    monkeypatch.setattr(Path, "open", tracked_open)
    assert dispatcher.observe_delivery(instruction).status == "consumed_or_started"
    assert changed == [True]
