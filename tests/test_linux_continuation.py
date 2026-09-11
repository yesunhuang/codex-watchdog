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
    assert s.step()["owner_state"] == "waiting_for_detach"
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
    from codex_watchdog import cli, linux_auto
    s = scenario
    received = []
    class Controller:
        def __init__(self, *args, **kwargs):
            received.append(kwargs)
        def run(self, interval, emit):
            return 0
    monkeypatch.setattr(linux_owner, "LinuxThreadOwner", Controller)
    monkeypatch.setattr(linux_auto, "LinuxAutoWatchdog", Controller)
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
