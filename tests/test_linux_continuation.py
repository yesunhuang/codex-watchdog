import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from codex_watchdog import linux_binding, linux_owner
from codex_watchdog.linux_binding import LinuxBinding, LinuxBindingError
from codex_watchdog.linux_owner import LinuxThreadOwner
from codex_watchdog.models import sha256_text
from codex_watchdog.queue_wake import QueueWakeDispatcher
from codex_watchdog.storage import InstructionStore
from codex_watchdog.workspace_registry import TrackedWorkspace


THREAD = "11111111-2222-4333-8444-555555555555"
INTERRUPTED = "21111111-2222-4333-8444-555555555555"
CONTINUED = "31111111-2222-4333-8444-555555555555"
QUEUE = "41111111-2222-4333-8444-555555555555"


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    home, repo = tmp_path / "codex", tmp_path / "project"
    repo.mkdir()
    rollout = home / "sessions" / ("rollout-test-" + THREAD + ".jsonl")
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id,cwd,source,thread_source,archived,rollout_path)")
        db.execute("INSERT INTO threads VALUES (?,?,'vscode','user',0,?)", (THREAD, str(repo), str(rollout)))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id,thread_id,payload_json)")
        db.execute("CREATE TABLE queued_thread_revisions(thread_id,revision)")
    monkeypatch.setattr(linux_binding, "locality_identity", lambda: "fixture-host")
    binding = LinuxBinding(tmp_path / "runtime", home)
    workspace = TrackedWorkspace.create("fixture-project", repo, THREAD)
    binding.bind(workspace, 600)
    data = SimpleNamespace(binding=binding, workspace=workspace, writer=None,
                           latest=INTERRUPTED, status="interrupted", native_status="idle",
                           sent=[], notices=[], calls=[], request_hook=None, uncertain=False)
    monkeypatch.setattr(linux_owner, "writer_pid", lambda *a: data.writer)
    monkeypatch.setattr(linux_owner, "vscode_writer", lambda pid: pid == 777)

    def queue(args, **kwargs):
        assert args[:3] == ["fixture-codex", "queue", "--thread"]
        assert args[3] == THREAD
        data.sent.append(args[5])
        if data.uncertain:
            raise subprocess.TimeoutExpired(args, 1)
        with sqlite3.connect(home / "queue_1.sqlite") as db:
            db.execute("INSERT INTO queued_items VALUES (?,?,?)", (QUEUE, THREAD, args[5]))
        return subprocess.CompletedProcess(args, 0, f"Queued message {QUEUE} for thread {THREAD}.", "")

    dispatcher = QueueWakeDispatcher(binding.runtime, codex_executable="fixture-codex", runner=queue, codex_home=home)

    class Client:
        process = SimpleNamespace(pid=1234)

        def initialize(self):
            pass

        def request(self, method, params):
            data.calls.append((method, params))
            assert params["threadId"] == THREAD
            if data.request_hook:
                data.request_hook(method, params)
            if method == "thread/resume":
                data.writer = self.process.pid
            if method == "thread/turns/list":
                assert params == {"threadId": THREAD, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"}
                return {"data": [{"id": data.latest, "status": data.status}]} if data.latest else {"data": []}
            assert method in ("thread/read", "thread/resume")
            return {"thread": {"id": THREAD, "cwd": str(repo), "status": {"type": data.native_status}}}

        def pump(self, timeout):
            pass

        def close(self):
            data.writer = None

    def notify(event):
        data.notices.append(event)
        return SimpleNamespace(to_dict=lambda: {"status": "sent"})

    notifier = SimpleNamespace(notify=notify)
    service = SimpleNamespace(queue_dispatcher=dispatcher, notifier=notifier)

    def make_owner(enabled=True):
        return LinuxThreadOwner(binding, executable="fixture-codex", service=service,
                                client_factory=lambda *a: Client(), continue_interrupted=enabled)

    data.owner = make_owner()
    data.make_owner = make_owner
    data.dispatcher = dispatcher
    data.notifier = notifier
    data.rollout = rollout

    def step():
        data.owner._next_continuation_check = 0
        return data.owner.step(observe=False)

    data.step = step

    def started():
        assert data.sent
        with sqlite3.connect(home / "queue_1.sqlite") as db:
            db.execute("DELETE FROM queued_items")
        events = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": CONTINUED}},
            {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": THREAD,
                "turn_id": CONTINUED, "item": {"type": "UserMessage", "content": [
                    {"type": "text", "text": data.sent[-1]}]}}},
        ]
        with rollout.open("a", encoding="utf-8") as file:
            for event in events:
                file.write(json.dumps(event) + "\n")
        data.latest, data.status = CONTINUED, "completed"

    data.started = started
    return data


def test_continue_once_and_notify_only_after_native_started_evidence(scenario):
    s = scenario
    assert s.step()["continuation"]["status"] == "enqueued"
    assert len(s.sent) == 1 and not s.notices
    s.step()
    assert len(s.sent) == 1 and not s.notices
    s.started()
    result = s.step()
    assert result["continuation"]["status"] == "started"
    assert len(s.notices) == 1 and s.notices[0].event_type == "linux_continuation_started"
    assert THREAD in s.notices[0].message and "project" in s.notices[0].message
    s.step()
    s.owner.client.close()
    s.owner = s.make_owner()
    s.step()
    assert len(s.sent) == len(s.notices) == 1
    assert THREAD not in json.dumps(result) and INTERRUPTED not in json.dumps(result)


@pytest.mark.parametrize("status", ["completed", "failed", "inProgress"])
def test_other_turn_states_do_not_continue(scenario, status):
    s = scenario
    s.status = status
    s.step()
    assert not s.sent and not s.notices


def test_empty_history_and_default_off_do_not_continue(scenario):
    s = scenario
    s.latest = None
    s.step()
    assert not s.sent
    s.owner.client.close()
    s.owner = s.make_owner(enabled=False)
    s.latest = INTERRUPTED
    assert "continuation" not in s.step()
    assert not s.sent


def test_attached_writer_never_resumes_or_queues(scenario):
    s = scenario
    s.writer = 777
    assert s.step()["owner_state"] == "observing"
    assert not s.calls and not s.sent


@pytest.mark.parametrize("gate", ["active", "approval", "pending", "release", "expired"])
def test_no_continuation_while_gated(scenario, gate):
    s = scenario
    if gate == "active":
        s.native_status = "active"
    elif gate == "approval":
        s.owner.approval_required = True
    elif gate == "pending":
        with sqlite3.connect(s.binding.codex_home / "queue_1.sqlite") as db:
            db.execute("INSERT INTO queued_items VALUES (?,?,?)", (QUEUE, THREAD, "user's queued work"))
    elif gate == "release":
        s.binding.request_release()
    else:
        path = linux_binding.reservation_path(s.binding.codex_home, THREAD)
        value = json.loads(path.read_text())
        value["expires_at"] = 0
        InstructionStore._atomic_json(path, value)
    s.step()
    assert not s.sent and not s.notices


@pytest.mark.parametrize("gate", ["turn_changed", "signal", "approval", "writer"])
def test_recheck_after_last_native_read_prevents_racing_send(scenario, gate):
    s = scenario
    count = []
    def race(method, params):
        if method == "thread/turns/list":
            count.append(method)
            if len(count) == 2:
                if gate == "turn_changed":
                    s.latest, s.status = CONTINUED, "completed"
                elif gate == "signal":
                    s.owner.release_requested = True
                elif gate == "approval":
                    s.owner.approval_required = True
                else:
                    s.writer = 999
    s.request_hook = race
    s.step()
    assert not s.sent


def test_uncertain_dispatch_never_retries_or_claims_success(scenario):
    s = scenario
    s.uncertain = True
    assert s.step()["continuation"]["status"] == "uncertain"
    s.step()
    s.owner.client.close()
    s.owner = s.make_owner()
    s.step()
    assert len(s.sent) == len(s.notices) == 1
    assert s.notices[0].event_type == "linux_continuation_uncertain"


def test_auto_continuation_interruption_does_not_make_a_retry_loop(scenario):
    s = scenario
    s.step()
    s.started()
    s.step()
    s.status = "interrupted"
    assert s.step()["continuation"]["attention"] == "continuation_interrupted"
    s.step()
    assert len(s.sent) == 1
    assert [x.event_type for x in s.notices] == ["linux_continuation_started", "linux_continuation_interrupted"]


def _append_native_events(scenario, events):
    with scenario.rollout.open("a", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event) + "\n")


def _consume_completed_continuation(scenario, turn, extra_newline=False):
    with sqlite3.connect(scenario.binding.codex_home / "queue_1.sqlite") as db:
        db.execute("DELETE FROM queued_items WHERE thread_id=?", (THREAD,))
    _append_native_events(scenario, [
        {"type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": THREAD, "turn_id": turn,
            "item": {"type": "UserMessage", "content": [{
                "type": "text", "text": scenario.sent[-1] + ("\n" if extra_newline else ""),
            }]},
        }},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}},
    ])
    scenario.latest, scenario.status = turn, "completed"


@pytest.mark.parametrize("resumed_original,extra_newline", [
    (False, True), (True, False), (True, True),
])
def test_completed_saved_receipt_reconciles_and_parks_without_replay(
    scenario, resumed_original, extra_newline,
):
    s = scenario
    turn = INTERRUPTED if resumed_original else CONTINUED
    if resumed_original:
        # Codex resumes the interrupted turn; its start is outside the new
        # receipt's append-only baseline in the native continuation transcript.
        _append_native_events(s, [{"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": turn,
        }}])
    assert s.step()["continuation"]["status"] == "enqueued"
    instruction = s.owner.continuation._instruction(THREAD, INTERRUPTED)
    receipt_path = s.dispatcher.records / (sha256_text(instruction) + ".json")
    receipt = json.loads(receipt_path.read_text())
    receipt.update(state="consumed_or_started", created_at="2000-01-01T00:00:00Z")
    InstructionStore._atomic_json(receipt_path, receipt)
    continuation = json.loads(s.owner.continuation.path.read_text())
    continuation.update(status="consumed_or_started", created_at="2000-01-01T00:00:00Z")
    InstructionStore._atomic_json(s.owner.continuation.path, continuation)
    if not resumed_original:
        _append_native_events(s, [{"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": turn,
        }}])
    _consume_completed_continuation(s, turn, extra_newline)
    s.owner._idle_since -= 6

    result = s.step()
    assert result["continuation"]["status"] == "started"
    assert result["owner_state"] == "parked" and s.owner.client is None
    assert s.writer is None and s.binding.load()["state"] == "armed"
    saved = json.loads(s.owner.continuation.path.read_text())
    assert saved["continuation_turn_id"] == turn
    assert saved["interrupted_turn_id"] == INTERRUPTED
    assert saved["created_at"] == "2000-01-01T00:00:00Z"
    assert json.loads(receipt_path.read_text())["started_turn_id"] == turn
    assert len(s.sent) == len(s.notices) == 1
    assert s.notices[0].event_type == "linux_continuation_started"
    calls = list(s.calls)
    assert s.step()["owner_state"] == "parked"
    assert s.calls == calls and len(s.sent) == len(s.notices) == 1


def test_resumed_original_turn_interruption_does_not_repeat_continuation(scenario):
    s = scenario
    _append_native_events(s, [{"type": "event_msg", "payload": {
        "type": "task_started", "turn_id": INTERRUPTED,
    }}])
    s.step()
    _consume_completed_continuation(s, INTERRUPTED, extra_newline=True)
    s.status = "interrupted"
    assert s.step()["continuation"]["attention"] == "continuation_interrupted"
    s.step()
    assert len(s.sent) == 1
    assert [notice.event_type for notice in s.notices] == [
        "linux_continuation_started", "linux_continuation_interrupted",
    ]


@pytest.mark.parametrize("state", ["consumed_or_started", "uncertain", "dispatching"])
def test_idle_empty_queue_and_old_receipt_never_authorize_uncertain_handback(scenario, state):
    s = scenario
    s.step()
    instruction = s.owner.continuation._instruction(THREAD, INTERRUPTED)
    path = s.dispatcher.records / (sha256_text(instruction) + ".json")
    receipt = json.loads(path.read_text())
    receipt.update(state=state, created_at="2000-01-01T00:00:00Z")
    InstructionStore._atomic_json(path, receipt)
    with sqlite3.connect(s.binding.codex_home / "queue_1.sqlite") as db:
        db.execute("DELETE FROM queued_items")
    # Completion alone is not delivery proof: the exact wake is absent.
    _append_native_events(s, [{"type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": INTERRUPTED,
    }}])
    s.status = "completed"
    s.owner._idle_since -= 6
    result = s.step()
    assert result["continuation"]["status"] == state
    assert result["owner_state"] == "owned" and s.owner.client is not None
    s.step()
    assert len(s.sent) == 1
    assert not any(notice.event_type == "linux_continuation_started" for notice in s.notices)


@pytest.mark.parametrize("gate", ["active", "live_active", "new_queue", "writer_race"])
def test_reconciled_receipt_keeps_fresh_handback_guards(scenario, gate):
    s = scenario
    s.step()
    s.started()
    s.owner._idle_since -= 6
    if gate == "active":
        s.owner._event({"method": "turn/started", "params": {"threadId": THREAD}})
    elif gate == "live_active":
        s.native_status = "active"
    elif gate == "new_queue":
        with sqlite3.connect(s.binding.codex_home / "queue_1.sqlite") as db:
            db.execute("INSERT INTO queued_items VALUES (?,?,?)", (QUEUE, THREAD, "new work"))
    else:
        def change_writer(method, params):
            if method == "thread/read":
                s.writer = 999
        s.request_hook = change_writer
    if gate == "writer_race":
        with pytest.raises(LinuxBindingError, match="linux_writer_changed"):
            s.step()
    else:
        result = s.step()
        assert result["continuation"]["status"] == "started"
        assert result["owner_state"] == "owned"
    assert s.owner.client is not None
    assert len(s.sent) == 1


@pytest.mark.parametrize("changes,reason", [
    ({"state": "failed"}, "receipt_invalid"),
    ({"state": "started", "thread_id": CONTINUED, "started_turn_id": CONTINUED}, "receipt_invalid"),
    ({"state": "started", "started_turn_id": "not-a-turn"}, "start_unverified"),
])
def test_invalid_completion_receipt_cannot_release_writer(scenario, changes, reason):
    s = scenario
    s.step()
    instruction = s.owner.continuation._instruction(THREAD, INTERRUPTED)
    path = s.dispatcher.records / (sha256_text(instruction) + ".json")
    receipt = json.loads(path.read_text())
    receipt.update(changes)
    InstructionStore._atomic_json(path, receipt)
    with sqlite3.connect(s.binding.codex_home / "queue_1.sqlite") as db:
        db.execute("DELETE FROM queued_items")
    s.status = "completed"
    s.owner._idle_since -= 6
    with pytest.raises(LinuxBindingError, match=reason):
        s.step()
    assert s.owner.client is not None and s.writer == s.owner.client.process.pid
    assert len(s.sent) == 1 and not s.notices
    assert json.loads(path.read_text())["state"] == changes["state"]


def _park_and_restart_saved_continuation(s, monkeypatch, *, proof="exact"):
    from codex_watchdog.control_context import acting_as
    from codex_watchdog.control_state import ControlStore

    store = ControlStore(s.binding.codex_home, THREAD, s.workspace.repo_root,
                         clock=lambda: 100, boot_id="fixture-boot")
    local = store.attach("desktop", "fixture-desktop", "vscode")
    store.detach(local)
    token = store.claim_remote("original-monitor", "fixture-host", "absent", host_observer=True)
    # Exercise the node's actual durable park writer path with a coordinated
    # native-writer fixture, without emulating any second native Codex session.
    s.owner.node_local = True
    _append_native_events(s, [{"type": "event_msg", "payload": {
        "type": "task_started", "turn_id": INTERRUPTED,
    }}])
    with acting_as(store, token):
        assert s.step()["continuation"]["status"] == "enqueued"
    instruction = s.owner.continuation._instruction(THREAD, INTERRUPTED)
    receipt_path = s.dispatcher.records / (sha256_text(instruction) + ".json")
    receipt = json.loads(receipt_path.read_text())
    receipt.update(state="consumed_or_started", future_receipt_setting={"preserve": True})
    InstructionStore._atomic_json(receipt_path, receipt)
    continuation = json.loads(s.owner.continuation.path.read_text())
    continuation.update(status="consumed_or_started", future_continuation_setting={"preserve": True})
    InstructionStore._atomic_json(s.owner.continuation.path, continuation)
    if proof == "exact":
        _consume_completed_continuation(s, INTERRUPTED, extra_newline=True)
    elif proof == "wrong_turn":
        _consume_completed_continuation(s, CONTINUED, extra_newline=True)
    else:
        with sqlite3.connect(s.binding.codex_home / "queue_1.sqlite") as db:
            db.execute("DELETE FROM queued_items")
        _append_native_events(s, [{"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": INTERRUPTED,
        }}])
        s.latest, s.status = INTERRUPTED, "completed"
    # Supported idle handback skips dispatch/continuation work, closes only
    # its verified writer, and retains the node's armed durable park gate.
    s.owner.yield_requested = True
    with acting_as(store, token):
        assert s.step()["owner_state"] == "waiting_for_attach"
    assert s.owner.client is None and s.writer is None
    assert store.read()["node_parked"] is True and s.binding.load()["state"] == "armed"
    assert json.loads(receipt_path.read_text())["state"] == "consumed_or_started"
    store.release_remote(token, "absent")
    s.token = store.claim_remote("replacement-monitor", "fixture-host", "absent", host_observer=True)
    s.store, s.receipt_path, s.initial_receipt = store, receipt_path, receipt
    s.owner = s.make_owner()
    s.owner.node_local = True
    s.native_calls_before_restart = list(s.calls)
    monkeypatch.setattr(s.owner, "client_factory", lambda *a: pytest.fail("parked restart must not open a native client"))
    monkeypatch.setattr(s.dispatcher, "dispatch", lambda *a, **kw: pytest.fail("parked reconciliation must not dispatch"))


def _step_parked(s):
    from codex_watchdog.control_context import acting_as
    with acting_as(s.store, s.token):
        return s.step()


def test_parked_restart_automatically_reconciles_completed_saved_receipt(scenario, monkeypatch):
    s = scenario
    _park_and_restart_saved_continuation(s, monkeypatch)
    result = _step_parked(s)
    assert result["owner_state"] == "parked"
    receipt = json.loads(s.receipt_path.read_text())
    assert receipt["state"] == "started"
    assert result["continuation"]["status"] == "started"
    assert receipt["started_turn_id"] == INTERRUPTED
    for key in ("instruction_id", "thread_id", "prompt_sha256", "queue_message_id",
                "rollout_baseline_offset", "created_at", "future_receipt_setting"):
        assert receipt[key] == s.initial_receipt[key]
    saved = json.loads(s.owner.continuation.path.read_text())
    assert saved["continuation_turn_id"] == INTERRUPTED
    assert saved["future_continuation_setting"] == {"preserve": True}
    assert saved["notifications"] == {}
    assert s.store.read()["node_parked"] is True and s.binding.load()["state"] == "armed"
    assert s.owner.client is None and s.writer is None
    assert s.calls == s.native_calls_before_restart and len(s.sent) == 1 and not s.notices
    assert _step_parked(s)["owner_state"] == "parked"
    s.store.release_remote(s.token, "absent")
    s.token = s.store.claim_remote("third-monitor", "fixture-host", "absent", host_observer=True)
    s.owner = s.make_owner()
    s.owner.node_local = True
    monkeypatch.setattr(s.owner, "client_factory", lambda *a: pytest.fail("receipt recovery must keep a vacant writer"))
    assert _step_parked(s)["continuation"]["status"] == "started"
    assert s.store.read()["node_parked"] is True
    assert s.calls == s.native_calls_before_restart and len(s.sent) == 1 and not s.notices


@pytest.mark.parametrize("damage,reason", [
    ("missing", "receipt_missing"), ("malformed", "Expecting"), ("wrong_target", "receipt_invalid"),
])
def test_parked_reconciliation_fails_closed_on_invalid_saved_receipt(scenario, monkeypatch, damage, reason):
    s = scenario
    _park_and_restart_saved_continuation(s, monkeypatch)
    if damage == "missing":
        s.receipt_path.unlink()
    elif damage == "malformed":
        s.receipt_path.write_text("not-json")
    else:
        receipt = json.loads(s.receipt_path.read_text())
        receipt["thread_id"] = CONTINUED
        InstructionStore._atomic_json(s.receipt_path, receipt)
    with pytest.raises(ValueError, match=reason):
        _step_parked(s)
    assert s.owner.client is None and s.writer is None and s.store.read()["node_parked"] is True
    assert s.calls == s.native_calls_before_restart and len(s.sent) == 1 and not s.notices


@pytest.mark.parametrize("proof,state", [
    ("missing_message", "consumed_or_started"), ("wrong_turn", "consumed_or_started"),
    ("exact", "uncertain"), ("exact", "dispatching"),
])
def test_parked_reconciliation_preserves_unproven_and_uncertain_delivery(scenario, monkeypatch, proof, state):
    s = scenario
    _park_and_restart_saved_continuation(s, monkeypatch, proof=proof)
    receipt = json.loads(s.receipt_path.read_text())
    receipt["state"] = state
    InstructionStore._atomic_json(s.receipt_path, receipt)
    value = json.loads(s.owner.continuation.path.read_text())
    value["status"] = state
    InstructionStore._atomic_json(s.owner.continuation.path, value)
    result = _step_parked(s)
    assert result["owner_state"] == "parked" and result["continuation"]["status"] == state
    assert json.loads(s.receipt_path.read_text())["state"] == state
    saved = json.loads(s.owner.continuation.path.read_text())
    assert saved.get("continuation_turn_id") is None and saved["notifications"] == {}
    assert s.owner.client is None and s.writer is None and s.store.read()["node_parked"] is True
    assert s.calls == s.native_calls_before_restart and len(s.sent) == 1 and not s.notices


def test_parked_prepared_without_queue_record_does_not_admit_work(scenario, monkeypatch):
    s = scenario
    _park_and_restart_saved_continuation(s, monkeypatch)
    s.receipt_path.unlink()
    value = json.loads(s.owner.continuation.path.read_text())
    value["status"] = "prepared"
    InstructionStore._atomic_json(s.owner.continuation.path, value)
    result = _step_parked(s)
    assert result["owner_state"] == "parked" and result["continuation"]["status"] == "prepared"
    assert not s.receipt_path.exists() and s.owner.client is None and s.writer is None
    assert s.calls == s.native_calls_before_restart and len(s.sent) == 1 and not s.notices


def test_parked_reconciliation_is_rate_limited_between_owner_steps(scenario, monkeypatch):
    from codex_watchdog.control_context import acting_as
    s = scenario
    _park_and_restart_saved_continuation(s, monkeypatch, proof="missing_message")
    clock = [1000.0]
    monkeypatch.setattr(linux_owner, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=linux_owner.time.time))
    assert _step_parked(s)["continuation"]["status"] == "consumed_or_started"
    next_check = s.owner._next_continuation_check
    assert next_check > clock[0]
    observe, calls = s.dispatcher.observe_delivery, []

    def checked_observe(*args, **kwargs):
        assert clock[0] >= next_check, "tight owner step must not rescan a historical receipt"
        calls.append(args)
        return observe(*args, **kwargs)

    monkeypatch.setattr(s.dispatcher, "observe_delivery", checked_observe)
    with acting_as(s.store, s.token):
        assert s.owner.step(observe=False)["owner_state"] == "parked"
    assert not calls
    clock[0] = next_check
    with acting_as(s.store, s.token):
        assert s.owner.step(observe=False)["owner_state"] == "parked"
    assert len(calls) == 1
    assert s.calls == s.native_calls_before_restart and len(s.sent) == 1 and not s.notices


@pytest.mark.parametrize("gate,expected", [
    ("release", "released"), ("expired", "released"), ("yield", "waiting_for_attach"),
    ("attached_writer", "observing"),
])
def test_parked_receipt_reconciliation_retains_lifecycle_barriers(scenario, monkeypatch, gate, expected):
    s = scenario
    _park_and_restart_saved_continuation(s, monkeypatch)
    before = s.receipt_path.read_bytes()
    if gate == "release":
        s.owner.release_requested = True
    elif gate == "expired":
        path = linux_binding.reservation_path(s.binding.codex_home, THREAD)
        value = json.loads(path.read_text())
        value["expires_at"] = 0
        InstructionStore._atomic_json(path, value)
    elif gate == "yield":
        s.owner.yield_requested = True
    else:
        s.writer = 777
    assert _step_parked(s)["owner_state"] == expected
    assert s.receipt_path.read_bytes() == before
    assert s.owner.client is None and s.calls == s.native_calls_before_restart
    assert len(s.sent) == 1 and not s.notices


def test_automatic_pause_releases_parked_owner_without_reconciling_receipt(scenario, monkeypatch):
    from contextlib import ExitStack
    from codex_watchdog import linux_auto
    from codex_watchdog.control_state import control_atomic_json
    s = scenario
    _park_and_restart_saved_continuation(s, monkeypatch)
    before = s.receipt_path.read_bytes()
    with s.store.guard(s.token) as value:
        value["auto_paused"] = True
        control_atomic_json(s.store.path, value)
    monkeypatch.setattr(linux_auto, "writer_pid", lambda *a: s.writer)
    monkeypatch.setattr(linux_auto, "vscode_writer", lambda pid: pid == 777)
    monkeypatch.setattr(linux_auto, "locality_identity", lambda: "fixture-host")
    agent = linux_auto.LinuxAutoWatchdog(s.binding.runtime, s.binding.codex_home, continue_interrupted=True)
    agent.controllers[THREAD] = dict(store=s.store, token=s.token, owner=s.owner, locks=ExitStack())
    assert agent.step(observe=False)[0]["state"] == "released"
    assert s.receipt_path.read_bytes() == before and s.store.read()["auto_paused"] is True
    assert s.owner.client is None and s.writer is None and s.calls == s.native_calls_before_restart
    assert len(s.sent) == 1 and not s.notices


@pytest.mark.parametrize("gate,reason", [
    ("stale_epoch", "control_stale_epoch"), ("expired_owner", "control_lease_expired"),
    ("missing_capability", "control_owner_capability_required"), ("foreign_writer", "linux_conflicting_writer"),
])
def test_parked_reconciliation_cannot_adopt_missing_or_stale_ownership(scenario, monkeypatch, gate, reason):
    from codex_watchdog.control_state import control_atomic_json
    s = scenario
    _park_and_restart_saved_continuation(s, monkeypatch)
    before = s.receipt_path.read_bytes()
    if gate == "stale_epoch":
        s.store.release_remote(s.token, "absent")
        assert s.store.claim_remote("new-authoritative-owner", "fixture-host", "absent", host_observer=True)
    elif gate == "expired_owner":
        with s.store.guard(s.token) as value:
            value["owner"]["expires"] = 99
            control_atomic_json(s.store.path, value)
    elif gate == "foreign_writer":
        s.writer = 999
    with pytest.raises(ValueError, match=reason):
        s.step() if gate == "missing_capability" else _step_parked(s)
    assert s.receipt_path.read_bytes() == before and s.owner.client is None
    assert s.calls == s.native_calls_before_restart and len(s.sent) == 1 and not s.notices


def test_crash_after_notification_send_does_not_send_again(scenario):
    s = scenario
    s.step()
    s.started()
    def crash(event):
        s.notices.append(event)
        raise RuntimeError("crash after a possibly delivered notification")
    s.notifier.notify = crash
    with pytest.raises(RuntimeError):
        s.step()
    s.owner.client.close()
    s.owner = s.make_owner()
    s.step()
    assert len(s.sent) == len(s.notices) == 1


def test_prepared_before_queue_journal_can_recover_without_duplicate(scenario, monkeypatch):
    s = scenario
    original = s.dispatcher.dispatch
    monkeypatch.setattr(s.dispatcher, "dispatch", lambda *a: (_ for _ in ()).throw(RuntimeError("before queue")))
    with pytest.raises(RuntimeError):
        s.step()
    assert not s.sent
    monkeypatch.setattr(s.dispatcher, "dispatch", original)
    s.step()
    s.step()
    assert len(s.sent) == 1


def test_invalid_latest_turn_and_corrupt_saved_state_fail_closed(scenario):
    s = scenario
    s.latest = "not-a-turn-id"
    with pytest.raises(LinuxBindingError, match="turn_unverified"):
        s.step()
    assert not s.sent
    s.latest = INTERRUPTED
    InstructionStore._atomic_json(s.owner.continuation.path, {"schema_version": 1, "thread_sha256": "other"})
    with pytest.raises(LinuxBindingError, match="state_invalid"):
        s.step()
    assert not s.sent


def test_coordinated_owner_uses_canonical_notification_receipt(scenario):
    from codex_watchdog.control_context import acting_as
    from codex_watchdog.control_state import ControlStore
    s = scenario
    store = ControlStore(s.binding.codex_home, THREAD, s.workspace.repo_root, boot_id="fixture-boot")
    local = store.attach("desktop", "local", "vscode")
    store.detach(local)
    token = store.claim_remote("linux", "fixture-host", "absent")
    with acting_as(store, token):
        s.step()
        s.started()
        s.step()
        s.step()
    assert len(s.notices) == 1 and store.read()["external_effect"] is None
    event = s.notices[0]
    receipt = json.loads(store.effect_path("notification", event.event_fingerprint()).read_text())
    assert receipt["result"]["status"] == "sent"


def test_cli_opt_in_defaults_and_explicit_enablement():
    from codex_watchdog.cli import build_parser
    for command in ("linux-run", "linux-auto-run"):
        assert not build_parser().parse_args([command]).continue_interrupted
        assert build_parser().parse_args([command, "--continue-interrupted"]).continue_interrupted


@pytest.mark.parametrize("mode", ["standalone", "bound_coordinated", "automatic"])
def test_cli_passes_opt_in_to_selected_linux_controller(scenario, monkeypatch, mode):
    from codex_watchdog import cli, linux_auto, messaging_setup
    s = scenario
    received = []
    class Controller:
        def __init__(self, *args, **kwargs):
            received.append(kwargs)
        def run(self, interval, emit):
            return 0
    monkeypatch.setattr(linux_owner, "LinuxThreadOwner", Controller)
    monkeypatch.setattr(linux_auto, "LinuxAutoWatchdog", Controller)
    # This test verifies controller arguments, never the operator's provider
    # profile or secure store. Messaging bootstrap has its own acceptance tests.
    monkeypatch.setattr(messaging_setup, "prepare_launch", lambda *a, **kw: {})
    if mode == "bound_coordinated":
        InstructionStore._atomic_json(s.binding.codex_home / "watchdog-control" / THREAD / "owner.json", {})
    command = "linux-auto-run" if mode == "automatic" else "linux-run"
    extra = ["--thread", THREAD, "--renew-lease"] if mode == "automatic" else []
    assert cli.main(["--runtime", str(s.binding.runtime), "--codex-home", str(s.binding.codex_home),
                     command, "--continue-interrupted", *extra]) == 0
    assert len(received) == 1 and received[0]["continue_interrupted"] is True
    if mode == "automatic":
        assert received[0]["threads"] == [THREAD] and received[0]["renew_lease"] is True


def test_automatic_thread_filter_accepts_repeated_exact_ids_and_rejects_invalid():
    from codex_watchdog.cli import build_parser
    args = build_parser().parse_args(["linux-auto-run", "--thread", THREAD, "--thread", CONTINUED])
    assert args.thread == [THREAD, CONTINUED]
    with pytest.raises(SystemExit):
        build_parser().parse_args(["linux-auto-run", "--thread", "a project name"])
