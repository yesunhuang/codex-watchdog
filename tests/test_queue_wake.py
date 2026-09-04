from __future__ import annotations

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
