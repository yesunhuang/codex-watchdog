"""A relay's queue receipt must not authorize handoff of pending native work."""
import json
import os
from pathlib import Path
import sqlite3

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.relay import RelayTarget
from codex_watchdog.relay_authority import SessionRelayAuthority
from codex_watchdog.relay_native_completion import completion_progress
from codex_watchdog.models import sha256_text
from codex_watchdog.slack_poll import SlackPollingThreadStore
from test_linux_node import THREAD, nodes
from test_node_relay_epoch import _observer


QUEUE_ID = "33333333-3333-4333-8333-333333333333"
OTHER_THREAD = "22222222-2222-4222-8222-222222222222"
PARENT = "1789111600.000001"


def queue(home, thread=THREAD, *, name="queue_1.sqlite"):
    with sqlite3.connect(home / name) as db:
        db.execute("CREATE TABLE queued_items (id TEXT PRIMARY KEY, thread_id TEXT, payload_json TEXT)")
        db.execute("CREATE INDEX fixture_thread_queue ON queued_items(thread_id)")
        if thread is not None:
            db.execute("INSERT INTO queued_items VALUES (?,?,?)", (QUEUE_ID, thread, "{}"))


def authority(observation):
    result = SessionRelayAuthority(observation.store, observation.token, observation, "workspace")
    if not result.manifest.exists():
        cs.control_atomic_json(result.manifest, dict(schema_version=1,
            thread_id=observation.store.thread_id, repo_path=observation.store.repo_path,
            state="ready", storage="cluster_provider_journals_v1"))
    return result


def stamp(home):
    path = home / "sessions/thread.jsonl"
    info = path.stat()
    return [str(path), info.st_ino, info.st_size, info.st_mtime_ns]


def park_with_enqueued_receipt(native):
    home, repo, host, roots, pid = native
    observation = _observer(home, repo)
    with observation.guard(rollout_stamp=stamp(home)) as admitted:
        assert admitted
    current = authority(observation)
    current.activate()
    store = SlackPollingThreadStore(Path(observation.store.read()["runtime_path"]))
    store.relay_authority = current
    # This envelope is remote POSIX state; native fixture files stay in repo.
    remote_repo = "/fixture/project" if os.name == "nt" else repo.as_posix()
    target = RelayTarget("workspace", THREAD, "remote_ssh", "ssh-remote+login-a.example", remote_repo, "a" * 32)
    store.record_thread("C12345678", PARENT, target, "a" * 64)
    assert store.claim_reply(event_key="relay-native-fixture", channel_id="C12345678",
        thread_ts=PARENT, instruction_id="slack:fixture", text="fixture", admission=lambda db: True) == (True, None)
    store.finish_reply("relay-native-fixture", state_value="delivered", delivery_status="enqueued")
    pid[0] = None
    with observation.guard(rollout_stamp=stamp(home)) as admitted:
        assert admitted
    # A relay dispatch does not populate the Git observer's pending field.
    assert observation.store.read()["remote_state"] is None
    assert cs.control_read_json(observation.path)["wake_pending"] is False
    return observation, current


def replacement(native):
    home, repo, host, roots, pid = native
    host[0], pid[0] = "login-b.example", 123
    observation = _observer(home, repo, instance="dog-b", boot="boot-b")
    current = authority(observation)
    observation.relay_handoff_safe = current.handoff_safe
    return observation, current


def test_native_queued_item_blocks_handoff_after_confirmed_relay_enqueue(nodes):
    home = nodes[0]
    old, original = park_with_enqueued_receipt(nodes)
    queue(home)
    saved = old.path.read_bytes()
    native_queue = (home / "queue_1.sqlite").read_bytes()
    candidate, selected = replacement(nodes)
    assert selected.handoff_safe() is False
    with candidate.guard(rollout_stamp=stamp(home)) as admitted:
        assert admitted is False
    assert candidate.reason == "control_node_relay_pending"
    assert candidate.path.read_bytes() == saved
    assert (home / "queue_1.sqlite").read_bytes() == native_queue
    with pytest.raises(cs.ControlBusy, match="relay_not_selected"):
        candidate.capture_relay_epoch()


@pytest.mark.parametrize("status", ["enqueued", "consumed_or_started", "started"])
def test_native_queue_guard_is_independent_of_relay_delivery_status(nodes, status):
    home = nodes[0]
    old, current = park_with_enqueued_receipt(nodes)
    with current.guard():
        store = SlackPollingThreadStore(Path(old.store.read()["runtime_path"]))
        store.relay_authority = current
        store.finish_reply("relay-native-fixture", state_value="delivered", delivery_status=status)
    queue(home)
    candidate, selected = replacement(nodes)
    assert selected.handoff_safe() is False


@pytest.mark.parametrize("problem", ["missing", "multiple", "malformed"])
def test_native_queue_unavailable_or_ambiguous_cannot_prove_handoff_safe(nodes, problem):
    home = nodes[0]
    park_with_enqueued_receipt(nodes)
    if problem == "multiple":
        queue(home, None)
        queue(home, None, name="queue_2.sqlite")
    elif problem == "malformed":
        (home / "queue_1.sqlite").write_text("not a native queue database")
    candidate, selected = replacement(nodes)
    saved = candidate.path.read_bytes()
    assert selected.handoff_safe() is False
    with candidate.guard(rollout_stamp=stamp(home)) as admitted:
        assert admitted is False
    assert candidate.path.read_bytes() == saved


def test_queue_guard_does_not_count_other_sessions_pending_rows(nodes):
    home, repo = nodes[:2]
    observation = _observer(home, repo)
    with observation.guard(rollout_stamp=stamp(home)) as admitted:
        assert admitted
    current = authority(observation)
    current.activate()
    queue(home, OTHER_THREAD)
    # There is no admitted or uncertain relay for this exact session.
    assert current.handoff_safe() is True


TURN = "44444444-4444-4444-8444-444444444444"
OTHER_TURN = "55555555-5555-4555-8555-555555555555"
PROMPT = "Complete the authorized native fixture."


def receipt(tmp_path):
    return dict(thread_id=THREAD, instruction_id="slackbot:" + "a" * 40,
        prompt_sha256=sha256_text(PROMPT), rollout_path=str(tmp_path / "rollout.jsonl"),
        rollout_baseline_offset=0, queue_message_id=QUEUE_ID)


def native(kind, *, turn=TURN, thread=THREAD, **fields):
    payload = dict(type=kind, turn_id=turn, **fields)
    if thread is not None:
        payload["thread_id"] = thread
    return dict(type="event_msg", payload=payload)


def user_message(record, *, turn=TURN, thread=THREAD, prompt=PROMPT, digest=None):
    marker = "[CODEX_WATCHDOG_WAKE id={} sha256={}]\n".format(
        record["instruction_id"], digest or record["prompt_sha256"])
    return native("item_completed", turn=turn, thread=thread, item=dict(type="UserMessage",
        content=[dict(type="text", text=marker + prompt)]))


def transcript(record, *events):
    Path(record["rollout_path"]).write_bytes(b"".join(
        (json.dumps(event, separators=(",", ":")) + "\n").encode() for event in events))


@pytest.mark.parametrize("suffix", ["", "\n"])
def test_exact_message_and_same_turn_completion_prove_only_original_wake(tmp_path, suffix):
    record = receipt(tmp_path)
    transcript(record, native("task_started"), user_message(record, prompt=PROMPT + suffix),
        native("task_complete", thread=None))
    before = Path(record["rollout_path"]).read_bytes()
    progress, complete = completion_progress(record)
    assert complete and progress["turn_id"] == TURN and progress["terminal"] is True
    assert progress["offset"] == len(before) and progress["bytes_read"] <= 65536
    assert PROMPT not in json.dumps(progress)
    assert Path(record["rollout_path"]).read_bytes() == before


@pytest.mark.parametrize("events", ["started", "different_turn", "terminal_before_message"])
def test_start_or_unrelated_completion_never_proves_native_completion(tmp_path, events):
    record = receipt(tmp_path)
    if events == "started":
        items = [native("task_started"), user_message(record)]
    elif events == "different_turn":
        items = [user_message(record), native("task_complete", turn=OTHER_TURN)]
    else:
        items = [native("task_complete"), user_message(record)]
    transcript(record, *items)
    progress, complete = completion_progress(record)
    assert not complete and progress["terminal"] is False


@pytest.mark.parametrize("problem", ["full_prompt", "header_digest", "thread", "missing_thread", "turn"])
def test_native_identity_and_full_prompt_are_required(tmp_path, problem):
    record = receipt(tmp_path)
    arguments = {}
    if problem == "full_prompt": arguments["prompt"] = "Different prompt with the same claimed marker."
    elif problem == "header_digest": arguments["digest"] = "f" * 64
    elif problem == "thread": arguments["thread"] = OTHER_THREAD
    elif problem == "missing_thread": arguments["thread"] = None
    else: arguments["turn"] = "not-a-canonical-turn"
    transcript(record, user_message(record, **arguments), native("task_complete"))
    progress, complete = completion_progress(record)
    assert not complete and progress["invalid"]


@pytest.mark.parametrize("same_turn", [False, True])
def test_ambiguous_duplicate_native_messages_remain_fenced_even_after_first_terminal(tmp_path, same_turn):
    record = receipt(tmp_path)
    transcript(record, user_message(record), native("task_complete"),
        user_message(record, turn=TURN if same_turn else OTHER_TURN), native("task_complete", turn=OTHER_TURN))
    progress, complete = completion_progress(record)
    assert not complete and progress["invalid"]


def test_conflicting_thread_terminal_is_not_completion(tmp_path):
    record = receipt(tmp_path)
    transcript(record, user_message(record), native("task_complete", thread=OTHER_THREAD))
    progress, complete = completion_progress(record)
    assert not complete and progress["invalid"]


def test_pre_dispatch_message_or_terminal_cannot_be_reused(tmp_path):
    record = receipt(tmp_path)
    transcript(record, user_message(record), native("task_complete"))
    record["rollout_baseline_offset"] = Path(record["rollout_path"]).stat().st_size
    progress, complete = completion_progress(record)
    assert not complete and progress["turn_id"] is None and progress["terminal"] is False


def test_baseline_inside_native_event_is_not_an_admission_boundary(tmp_path):
    record = receipt(tmp_path)
    transcript(record, user_message(record), native("task_complete"))
    record["rollout_baseline_offset"] = 1
    progress, complete = completion_progress(record)
    assert not complete and progress["invalid"]


def test_partial_final_event_retains_unread_offset_until_complete_line(tmp_path):
    record = receipt(tmp_path)
    transcript(record, user_message(record))
    path = Path(record["rollout_path"])
    boundary = path.stat().st_size
    terminal = (json.dumps(native("task_complete")) + "\n").encode()
    with path.open("ab") as stream:
        stream.write(terminal[:20])
    progress, complete = completion_progress(record)
    assert not complete and not progress["invalid"] and progress["offset"] == boundary
    with path.open("ab") as stream:
        stream.write(terminal[20:])
    progress, complete = completion_progress(record, progress)
    assert complete and progress["offset"] == path.stat().st_size


@pytest.mark.parametrize("problem", ["truncate", "replace", "boundary_rewrite", "malformed"])
def test_changed_or_invalid_native_evidence_cannot_clear_pending(tmp_path, problem):
    record = receipt(tmp_path)
    transcript(record, user_message(record))
    path = Path(record["rollout_path"])
    progress, complete = completion_progress(record)
    assert not complete
    if problem == "truncate":
        path.write_bytes(b"{}\n")
    elif problem == "replace":
        replacement = path.with_name("replacement.jsonl")
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    elif problem == "boundary_rewrite":
        content = path.read_bytes()
        path.write_bytes(content[:-2] + b" \n")
    else:
        with path.open("ab") as stream:
            stream.write(b"{bad event}\n")
    progress, complete = completion_progress(record, progress)
    assert not complete and progress["invalid"]


def test_missing_rollout_can_retry_without_guessing_another_source(tmp_path):
    record = receipt(tmp_path)
    progress, complete = completion_progress(record)
    assert not complete and not progress["invalid"]
    transcript(record, user_message(record), native("task_complete"))
    progress, complete = completion_progress(record, progress)
    assert complete


@pytest.mark.parametrize("field", ["thread_id", "instruction_id", "prompt_sha256", "rollout_path",
                                  "rollout_baseline_offset", "queue_message_id"])
def test_changed_immutable_receipt_never_adopts_another_completion(tmp_path, field):
    record = receipt(tmp_path)
    transcript(record, user_message(record), native("task_complete"))
    progress, complete = completion_progress(record)
    assert complete
    changed = dict(record)
    changed[field] = 1 if field == "rollout_baseline_offset" else str(record[field]) + "changed"
    progress, complete = completion_progress(changed, progress)
    assert not complete and progress["invalid"]


def test_mutable_courier_observation_does_not_change_immutable_completion_identity(tmp_path):
    record = receipt(tmp_path)
    transcript(record, user_message(record), native("task_complete"))
    progress, complete = completion_progress(record)
    assert complete
    observed = dict(record, state="started", started_turn_id=TURN, queue_row_seen=True, queue_revision=34)
    progress, complete = completion_progress(observed, progress)
    assert complete and progress["bytes_read"] <= 16


def test_long_transcript_progress_has_fixed_byte_budget_and_survives_restart(tmp_path, monkeypatch):
    record = receipt(tmp_path)
    path = Path(record["rollout_path"])
    noise = (json.dumps(dict(type="response_item", payload=dict(text="x" * 600))) + "\n").encode()
    transcript(record, user_message(record), native("task_complete"))
    path.write_bytes(noise * 10000 + path.read_bytes())
    assert path.stat().st_size > 4194304
    original_open = Path.open
    reads = []
    class Stream:
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *args): self.value.close()
        def __getattr__(self, name): return getattr(self.value, name)
        def read(self, amount=-1):
            assert 0 <= amount <= 16384
            result = self.value.read(amount)
            reads.append(len(result))
            return result
    def opened(self, *args, **kwargs):
        value = original_open(self, *args, **kwargs)
        return Stream(value) if self == path and args == ("rb",) else value
    monkeypatch.setattr(Path, "open", opened)
    progress, complete, calls = None, False, 0
    while not complete and calls < 1000:
        before = sum(reads)
        previous = 0 if progress is None else progress["offset"]
        progress, complete = completion_progress(record, progress, budget=16384)
        assert sum(reads) - before == progress["bytes_read"] <= 16384
        assert progress["offset"] > previous and not progress["invalid"]
        progress = json.loads(json.dumps(progress))  # Durable restart, no in-memory parser cache.
        calls += 1
    assert complete and calls < 1000 and progress["offset"] == path.stat().st_size


def test_terminal_before_budget_end_cannot_hide_later_ambiguous_admission(tmp_path):
    record = receipt(tmp_path)
    transcript(record, user_message(record), native("task_complete"),
        dict(type="response_item", payload=dict(text="x" * 2000)), user_message(record, turn=OTHER_TURN))
    progress, complete = completion_progress(record, budget=1024)
    assert not complete and progress["terminal"] is True
    progress, complete = completion_progress(record, progress, budget=4096)
    assert not complete and progress["invalid"]


@pytest.mark.parametrize("malformed", [
    b'{"type":"event_msg","type":"response_item","payload":{}}\n',
    b'{"type":"response_item","payload":{"cost":NaN}}\n',
    b'{"type":"response_item","payload":{"cost":Infinity}}\n',
])
def test_noncanonical_json_after_terminal_cannot_clear_pending(tmp_path, malformed):
    record = receipt(tmp_path)
    transcript(record, user_message(record), native("task_complete"))
    with Path(record["rollout_path"]).open("ab") as stream:
        stream.write(malformed)
    progress, complete = completion_progress(record)
    assert not complete and progress["invalid"]


def test_excessively_nested_json_remains_fenced_instead_of_crashing_reconciliation(tmp_path):
    record = receipt(tmp_path)
    transcript(record, user_message(record), native("task_complete"))
    with Path(record["rollout_path"]).open("ab") as stream:
        stream.write(b"[" * 2000 + b"0" + b"]" * 2000 + b"\n")
    progress, complete = completion_progress(record)
    assert not complete and progress["invalid"]


def test_over_budget_single_event_requires_explicit_larger_bounded_read(tmp_path):
    record = receipt(tmp_path)
    transcript(record, dict(type="response_item", payload=dict(text="x" * 2000)),
        user_message(record), native("task_complete"))
    progress, complete = completion_progress(record, budget=1024)
    assert not complete and not progress["invalid"] and progress["offset"] == 0
    assert progress["bytes_read"] == 1024
    progress, complete = completion_progress(record, progress, budget=4096)
    assert complete and progress["bytes_read"] <= 4096
