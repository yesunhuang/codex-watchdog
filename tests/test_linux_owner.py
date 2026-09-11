from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from codex_watchdog import linux_binding, linux_owner
from codex_watchdog.linux_binding import LinuxBinding, LinuxBindingError, reservation_path
from codex_watchdog.linux_owner import LinuxThreadOwner
from codex_watchdog.models import sha256_text
from codex_watchdog.queue_wake import QueueWakeDispatcher
from codex_watchdog.storage import FileLock, InstructionStore, StoreBusyError
from codex_watchdog.workspace_registry import TrackedWorkspace


THREAD = "11111111-2222-4333-8444-555555555555"
OTHER = "21111111-2222-4333-8444-555555555555"
QUEUE = "99999999-aaaa-4bbb-8ccc-dddddddddddd"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_binding, "locality_identity", lambda: "this-host-and-user")
    home = tmp_path / "codex home"
    repo = tmp_path / "private repo"
    repo.mkdir()
    rollout = home / "sessions" / "thread.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id, cwd, source, thread_source, archived, rollout_path)")
        db.execute("INSERT INTO threads VALUES (?, ?, 'vscode', 'user', 0, ?)",
                   (THREAD, str(repo), str(rollout)))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items (id, thread_id, payload_json)")
        db.execute("CREATE TABLE queued_thread_revisions (thread_id, revision)")
    workspace = TrackedWorkspace.create("disposable", repo, THREAD)
    binding = LinuxBinding(tmp_path / "runtime", home)
    binding.bind(workspace, 600)
    return binding, workspace


def change(binding, **changes):
    path = reservation_path(binding.codex_home, THREAD)
    value = json.loads(path.read_text())
    value.update(changes)
    InstructionStore._atomic_json(path, value)


def test_binding_idempotent_preserves_unknown_state_and_does_not_renew(setup):
    binding, workspace = setup
    change(binding, future_setting={"keep": True})
    before = reservation_path(binding.codex_home, THREAD).read_bytes()
    binding.bind(workspace, 1200)
    assert reservation_path(binding.codex_home, THREAD).read_bytes() == before
    binding.set_state("release_requested")
    assert binding.load()["future_setting"] == {"keep": True}


def test_bound_run_starts_and_closes_reply_listener(setup, monkeypatch):
    binding, _ = setup
    events = []
    service = SimpleNamespace(slack_reply_relay=SimpleNamespace(
        start=lambda: events.append("start"), close=lambda: events.append("close")),
        notifier=SimpleNamespace())
    instance = LinuxThreadOwner(binding, service=service)
    monkeypatch.setattr(instance, "step", lambda **kw: dict(owner_state="released"))
    assert instance.run() == 0
    assert events == ["start", "close"]


def test_binding_excludes_other_runtime_and_cannot_retarget(setup):
    binding, workspace = setup
    other = LinuxBinding(binding.runtime.parent / "other", binding.codex_home)
    with pytest.raises(LinuxBindingError, match="reserved"):
        other.bind(workspace, 600)
    with sqlite3.connect(binding.codex_home / "state_5.sqlite") as db:
        db.execute("INSERT INTO threads SELECT ?, cwd, source, thread_source, archived, rollout_path FROM threads", (OTHER,))
    with pytest.raises(LinuxBindingError, match="collision"):
        binding.bind(TrackedWorkspace.create("disposable", workspace.repo_root, OTHER), 600)
    assert binding.load()["thread_id"] == THREAD


@pytest.mark.parametrize("field,value", [("cwd", "/other"), ("archived", 1),
                                       ("source", "cli"), ("thread_source", "guardian_review")])
def test_exact_binding_rejects_changed_thread_metadata(setup, field, value):
    binding, workspace = setup
    with sqlite3.connect(binding.codex_home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET " + field + "=?", (value,))
    with pytest.raises(LinuxBindingError, match="exact_thread"):
        linux_binding.exact_thread(binding.codex_home, workspace)


def test_binding_refuses_locality_change_and_future_schema(setup, monkeypatch):
    binding, _ = setup
    monkeypatch.setattr(linux_binding, "locality_identity", lambda: "different-host")
    with pytest.raises(LinuxBindingError, match="mismatch"):
        binding.load()
    change(binding, schema_version=2)
    with pytest.raises(LinuxBindingError, match="unreadable"):
        binding.load()


def test_partial_binding_publication_is_recoverable(setup):
    binding, workspace = setup
    before = reservation_path(binding.codex_home, THREAD).read_bytes()
    binding.pointer.unlink()
    binding.bind(workspace, 600)
    assert binding.load()["thread_id"] == THREAD
    assert reservation_path(binding.codex_home, THREAD).read_bytes() == before


def test_owner_lock_prevents_second_owner_or_rebind(setup):
    binding, workspace = setup
    with FileLock(reservation_path(binding.codex_home, THREAD).with_suffix(".owner.lock")):
        with pytest.raises(StoreBusyError):
            binding.bind(workspace, 600)


@pytest.mark.skipif(sys.platform != "linux", reason="requires native Linux kernel FLOCK evidence")
def test_kernel_writer_identity_without_unrelated_process_fd_access(setup):
    import fcntl

    binding, _ = setup
    path = binding.codex_home / "thread-writer-locks" / (THREAD + ".lock")
    path.parent.mkdir()
    with path.open("wb") as handle:
        assert linux_owner.writer_pid(binding.codex_home, THREAD) is None
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert linux_owner.writer_pid(binding.codex_home, THREAD) == os.getpid()
    assert linux_owner.writer_pid(binding.codex_home, THREAD) is None


def test_reserved_delivery_fences_foreign_sender_and_preserves_uncertain_journal(setup):
    binding, _ = setup
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "uncertain test failure")

    dispatcher = QueueWakeDispatcher(binding.runtime, codex_home=binding.codex_home, runner=runner)
    foreign = QueueWakeDispatcher(binding.runtime.parent / "other", codex_home=binding.codex_home, runner=runner)
    assert foreign.dispatch(THREAD, "foreign", "hello", "test").status == "rejected"
    assert calls == []
    assert dispatcher.dispatch(THREAD, "fixed", "hello", "test").status == "uncertain"
    binding.set_state("release_requested")
    receipt = dispatcher.dispatch(THREAD, "fixed", "hello", "test")
    assert receipt.status == "uncertain" and receipt.deduplicated
    assert len(calls) == 1
    assert dispatcher.dispatch(THREAD, "new", "hello", "test").status == "rejected"
    assert not (dispatcher.records / (sha256_text("new") + ".json")).exists()


def test_expired_binding_fences_new_delivery(setup):
    binding, _ = setup
    change(binding, expires_at=0)
    dispatcher = QueueWakeDispatcher(binding.runtime, codex_home=binding.codex_home,
                                     runner=lambda *a, **kw: pytest.fail("must not send"))
    assert dispatcher.dispatch(THREAD, "new", "hello", "test").status == "rejected"


def test_mismatched_released_reservation_still_fails_closed(setup):
    binding, _ = setup
    change(binding, state="released", thread_id=OTHER)
    dispatcher = QueueWakeDispatcher(binding.runtime, codex_home=binding.codex_home,
                                     runner=lambda *a, **kw: pytest.fail("must not send"))
    assert dispatcher.dispatch(THREAD, "new", "hello", "test").status == "rejected"


def test_remote_helper_cannot_send_into_a_local_binding(setup):
    from codex_watchdog.remote_ssh import _REMOTE_SCRIPT

    binding, _ = setup
    namespace = {"__name__": "test_remote_binding"}
    exec(_REMOTE_SCRIPT, namespace)
    # The compact helper uses the current-user .codex directory.
    namespace["remote_codex_home"] = lambda: binding.codex_home.parent / ".codex"
    fence = binding.codex_home.parent / ".codex" / "watchdog-linux" / (THREAD + ".json")
    InstructionStore._atomic_json(fence, binding.load())
    namespace["wake_record_path"] = lambda _id: binding.runtime / "not_sent.json"
    namespace["queue_database"] = lambda: pytest.fail("remote helper must stop before queue access")
    assert namespace["dispatch_wake"]({"instruction_id": "remote", "prompt": "hello"}, THREAD) == {
        "status": "rejected", "reason": "linux_thread_reserved",
    }


def test_cli_status_contains_only_digests_and_release_is_durable(setup, capsys):
    from codex_watchdog.cli import main

    binding, _ = setup
    common = ["--runtime", str(binding.runtime), "--codex-home", str(binding.codex_home)]
    assert main(common + ["linux-status"]) == 0
    result = capsys.readouterr().out
    assert THREAD not in result and str(binding.codex_home) not in result
    assert main(common + ["linux-release"]) == 0
    assert binding.load()["state"] == "release_requested"


def test_bound_doctor_uses_live_binding_without_desktop_probe_or_writes(setup, monkeypatch):
    from codex_watchdog.doctor import WatchdogDoctor
    from codex_watchdog.platform_adapters import detect_platform_adapter

    binding, _ = setup
    adapter = detect_platform_adapter(system_name="Linux", home=binding.codex_home.parent,
                                      environment={}, which=lambda _: None)
    doctor = WatchdogDoctor(binding.runtime, codex_home=binding.codex_home, adapter=adapter,
                           linux_bound=True, status_probe=lambda _: pytest.fail("no desktop probe"))
    monkeypatch.setattr(doctor, "_codex_executable", lambda: ("unused", True, "test"))
    monkeypatch.setattr(linux_owner, "kernel_lock_owner", lambda _: 8887)
    monkeypatch.setattr(linux_owner, "writer_pid", lambda *args: 777)
    monkeypatch.setattr(linux_owner, "vscode_writer", lambda pid: pid == 777)
    before = {str(p): p.read_bytes() for p in binding.codex_home.parent.rglob("*") if p.is_file()}
    report = doctor.run()
    after = {str(p): p.read_bytes() for p in binding.codex_home.parent.rglob("*") if p.is_file()}
    assert after == before
    checks = {c.name: c for c in report.checks}
    assert report.status == "PARTIAL"
    assert checks["linux_binding"].status == "PASS"
    assert checks["linux_controller"].status == "PASS"
    assert checks["linux_writer"].reason == "linux_vscode_writer_verified"
    assert "vscode_cli" not in checks and "current_thread_resolution" not in checks
    encoded = json.dumps(report.to_dict())
    assert str(binding.runtime) not in encoded and THREAD not in encoded and "8887" not in encoded


def test_bound_doctor_rejects_missing_binding_without_creating_it(tmp_path, monkeypatch):
    from codex_watchdog.doctor import WatchdogDoctor
    from codex_watchdog.platform_adapters import detect_platform_adapter

    adapter = detect_platform_adapter(system_name="Linux", home=tmp_path,
                                      environment={}, which=lambda _: None)
    runtime = tmp_path / "must-not-be-created"
    report = WatchdogDoctor(runtime, adapter=adapter, linux_bound=True).run()
    checks = {c.name: c for c in report.checks}
    assert checks["linux_binding"].status == "FAIL"
    assert not runtime.exists()


def test_bound_doctor_rechecks_cwd_and_does_not_accept_saved_owner_status(setup, monkeypatch):
    from codex_watchdog.doctor import WatchdogDoctor
    from codex_watchdog.platform_adapters import detect_platform_adapter

    binding, _ = setup
    InstructionStore._atomic_json(binding.runtime / "linux/status.json", {
        "schema_version": 1, "owner_state": "owned",
    })
    with sqlite3.connect(binding.codex_home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET cwd='/changed'")
    adapter = detect_platform_adapter(system_name="Linux", home=binding.codex_home.parent,
                                      environment={}, which=lambda _: None)
    checks = {c.name: c for c in WatchdogDoctor(
        binding.runtime, codex_home=binding.codex_home, adapter=adapter, linux_bound=True
    ).run().checks}
    assert checks["linux_binding"].status == "FAIL"
    assert checks["linux_binding"].reason == "linux_exact_thread_unavailable"


class FakeClient:
    def __init__(self, workspace, pid_state):
        self.workspace = workspace
        self.pid_state = pid_state
        self.process = SimpleNamespace(pid=1234)
        self.requests = []
        self.closed = False
        self.mismatch = False

    def initialize(self):
        pass

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/resume":
            self.pid_state[0] = self.process.pid
        return {"thread": {"id": OTHER if self.mismatch else THREAD,
                           "cwd": str(self.workspace.repo_root), "status": {"type": "idle"}}}

    def pump(self, timeout=0.2):
        pass

    def close(self):
        self.closed = True
        self.pid_state[0] = None


@pytest.fixture
def owner(setup, monkeypatch):
    binding, workspace = setup
    pid = [None]
    monkeypatch.setattr(linux_owner, "writer_pid", lambda *a: pid[0])
    monkeypatch.setattr(linux_owner, "vscode_writer", lambda p: p == 777)
    client = FakeClient(workspace, pid)
    cycles = []
    from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
    def observe():
        cycles.append("read-only-service")
        return SimpleNamespace(status="completed", reason=None, workspaces=(
            SimpleNamespace(workspace_id=workspace.workspace_id, status="completed"),))
    service = SimpleNamespace(run_once=observe,
        notifier=EnvironmentNotifier(binding.runtime, config=NotificationConfig()))
    owner = LinuxThreadOwner(binding, service=service, client_factory=lambda *a: client)
    return owner, client, cycles, pid


def test_attached_writer_observes_completions_without_resuming(owner):
    instance, client, cycles, pid = owner
    pid[0] = 777
    assert instance.step(observe=True)["owner_state"] == "observing"
    assert client.requests == [] and cycles == ["read-only-service"]


def test_conflicting_writer_fails_closed(owner):
    instance, client, cycles, pid = owner
    pid[0] = 999
    with pytest.raises(LinuxBindingError, match="conflicting"):
        instance.step(observe=True)
    assert client.requests == [] and cycles == []


def test_resume_same_id_only_and_idle_handback(owner):
    instance, client, cycles, _ = owner
    assert instance.step(observe=True)["owner_state"] == "owned"
    assert client.requests == [
        ("thread/read", {"threadId": THREAD, "includeTurns": False}),
        ("thread/resume", {"threadId": THREAD, "excludeTurns": True}),
    ]
    assert len(cycles) == 1
    instance.binding.set_state("release_requested")
    assert instance.step(observe=True)["owner_state"] == "released"
    assert client.closed and len(cycles) == 1
    output = instance.status_path.read_text()
    assert THREAD not in output and str(instance.binding.runtime) not in output


def test_idle_writer_parks_without_desktop_request_and_keeps_monitoring(owner):
    instance, client, cycles, pid = owner
    instance.renew_lease = True
    assert instance.step(observe=True)["owner_state"] == "owned"
    instance._idle_since -= 6
    assert instance.step(observe=True)["owner_state"] == "parked"
    assert pid[0] is None and client.closed
    assert instance.binding.load()["state"] == "armed"
    requests = list(client.requests)
    assert instance.step(observe=True)["owner_state"] == "parked"
    assert client.requests == requests and len(cycles) == 3
    pid[0] = 777
    assert instance.step(observe=True)["owner_state"] == "observing"
    assert client.requests == requests  # VS Code can acquire the vacant writer.


@pytest.mark.parametrize("gate", ["active", "queued", "live_active", "queue_race", "writer_race", "continuation"])
def test_idle_parking_rechecks_native_activity_and_admission(owner, gate):
    instance, client, _, pid = owner
    instance.step(observe=False)
    instance._idle_since -= 6
    request = client.request

    def enqueue():
        with sqlite3.connect(instance.binding.codex_home / "queue_1.sqlite") as db:
            db.execute("INSERT INTO queued_items VALUES (?, ?, '{}')", (QUEUE, THREAD))

    def race(method, params):
        value = request(method, params)
        if method == "thread/read":
            if gate == "live_active":
                value["thread"]["status"]["type"] = "active"
            elif gate == "queue_race":
                enqueue()
            elif gate == "writer_race":
                pid[0] = 999
        return value

    client.request = race
    if gate == "active":
        instance._event({"method": "turn/started", "params": {"threadId": THREAD}})
    elif gate == "queued":
        enqueue()
    elif gate == "continuation":
        instance.continuation_status = {"status": "consumed_or_started"}
    if gate == "writer_race":
        with pytest.raises(LinuxBindingError, match="linux_writer_changed"):
            instance.step(observe=False)
    else:
        assert instance.step(observe=False)["owner_state"] == "owned"
    assert not client.closed


@pytest.mark.parametrize("trigger", ["queue", "rollout"])
def test_parked_thread_resumes_only_for_new_work_in_same_thread(owner, trigger):
    instance, client, _, pid = owner
    instance.step(observe=False)
    instance._idle_since -= 6
    assert instance.step(observe=False)["owner_state"] == "parked"
    workspace = instance.binding.workspace(instance.binding.load())
    if trigger == "queue":
        with sqlite3.connect(instance.binding.codex_home / "queue_1.sqlite") as db:
            db.execute("INSERT INTO queued_items VALUES (?, ?, '{}')", (QUEUE, THREAD))
    else:
        path = linux_binding.exact_thread(instance.binding.codex_home, workspace)
        path.write_text('{"new_interrupted_turn": true}\n')
    replacement = FakeClient(workspace, pid)
    instance.client_factory = lambda *a: replacement
    assert instance.step(observe=False)["owner_state"] == "owned"
    assert [params["threadId"] for _, params in replacement.requests] == [THREAD, THREAD]
    assert client.closed and not replacement.closed


def test_release_during_turn_waits_for_idle_and_empty_queue(owner):
    instance, client, cycles, _ = owner
    instance.step(observe=False)
    instance._event({"method": "turn/started", "params": {"threadId": THREAD}})
    instance.binding.set_state("release_requested")
    assert instance.step(observe=True)["owner_state"] == "releasing"
    instance._event({"method": "turn/completed", "params": {"threadId": THREAD}})
    assert instance.step(observe=True)["owner_state"] == "releasing"
    instance._event({"method": "thread/status/changed", "params": {"threadId": OTHER, "status": {"type": "idle"}}})
    assert instance.step(observe=True)["owner_state"] == "releasing"
    instance._event({"method": "thread/status/changed", "params": {"threadId": THREAD, "status": {"type": "idle"}}})
    with sqlite3.connect(instance.binding.codex_home / "queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES (?, ?, '{}')", (QUEUE, THREAD))
    assert instance.step(observe=True)["owner_state"] == "releasing"
    assert not client.closed and cycles == []


def test_expiry_does_not_resume_or_send(owner):
    instance, client, cycles, _ = owner
    change(instance.binding, expires_at=0)
    assert instance.step(observe=True)["owner_state"] == "released"
    assert client.requests == [] and cycles == []


def test_restart_with_release_request_does_not_resume(owner):
    instance, client, cycles, _ = owner
    instance.binding.set_state("release_requested")
    assert instance.step(observe=True)["owner_state"] == "released"
    assert client.requests == [] and cycles == []


@pytest.mark.parametrize("attached", [False, True])
def test_opt_in_renewal_keeps_same_live_binding_without_another_resume(owner, monkeypatch, attached):
    instance, client, _, pid = owner
    tick = [10000.0]
    monkeypatch.setattr(linux_binding.time, "time", lambda: tick[0])
    change(instance.binding, expires_at=10600.0, future_setting={"keep": True})
    instance.renew_lease = True
    if attached:
        pid[0] = 777
    expected = "observing" if attached else "owned"
    assert instance.step(observe=False)["owner_state"] == expected
    value = instance.binding.load()
    assert value["expires_at"] == 96400.0 and value["thread_id"] == THREAD
    assert value["future_setting"] == {"keep": True}
    # Past the original lease and then near the next expiry: keep the existing writer.
    requests = list(client.requests)
    tick[0] = 11000.0
    assert instance.step(observe=False)["owner_state"] == expected
    assert instance.binding.load()["expires_at"] == 96400.0
    tick[0] = 94000.0
    assert instance.step(observe=False)["owner_state"] == expected
    assert instance.binding.load()["expires_at"] == 180400.0
    assert client.requests == requests


@pytest.mark.parametrize("stopped_by", ["expired", "release_requested", "released", "signal"])
def test_renewal_never_revives_expired_or_cancelled_binding(owner, stopped_by):
    instance, client, cycles, _ = owner
    instance.renew_lease = True
    if stopped_by == "expired":
        change(instance.binding, expires_at=0)
    elif stopped_by == "signal":
        instance._signal_release(None, None)
    else:
        instance.binding.set_state(stopped_by)
    before = instance.binding.load()["expires_at"]
    assert instance.step(observe=True)["owner_state"] == "released"
    assert instance.binding.load()["expires_at"] == before
    assert client.requests == [] and cycles == []


def test_renewal_preserves_concurrent_operator_release(owner, monkeypatch):
    instance, client, _, _ = owner
    instance.renew_lease = True
    before = instance.binding.load()["expires_at"]
    renew = instance.binding.renew_lease
    def release_before_renewal(thread):
        instance.binding.set_state("release_requested")
        renew(thread)
    monkeypatch.setattr(instance.binding, "renew_lease", release_before_renewal)
    instance.step(observe=False)
    assert instance.binding.load()["state"] == "release_requested"
    assert instance.binding.load()["expires_at"] == before
    assert instance.step(observe=False)["owner_state"] == "released"
    assert client.closed


def test_renewal_admission_contention_retries_without_replacing_writer(owner):
    instance, client, _, _ = owner
    instance.renew_lease = True
    before = instance.binding.load()["expires_at"]
    with FileLock(reservation_path(instance.binding.codex_home, THREAD).with_suffix(".send.lock")):
        assert instance.step(observe=False)["owner_state"] == "owned"
    assert instance.binding.load()["expires_at"] == before
    assert instance.step(observe=False)["owner_state"] == "owned"
    assert instance.binding.load()["expires_at"] > before
    assert [method for method, _ in client.requests].count("thread/resume") == 1


def test_renewal_rejects_unverified_writer_and_is_off_by_default(owner):
    instance, client, _, pid = owner
    before = instance.binding.load()["expires_at"]
    instance.step(observe=False)
    assert instance.binding.load()["expires_at"] == before
    instance.renew_lease = True
    pid[0] = 999
    with pytest.raises(LinuxBindingError, match="writer_changed"):
        instance.step(observe=False)
    assert instance.binding.load()["expires_at"] == before


def test_linux_run_exposes_explicit_lease_renewal_only():
    from codex_watchdog.cli import build_parser
    assert build_parser().parse_args(["linux-run"]).renew_lease is False
    assert build_parser().parse_args(["linux-run", "--renew-lease"]).renew_lease is True


def test_mismatched_server_thread_cannot_start_service(owner):
    instance, client, cycles, _ = owner
    client.mismatch = True
    with pytest.raises(LinuxBindingError, match="mismatch"):
        instance.step(observe=True)
    assert [m for m, _ in client.requests] == ["thread/read"]
    assert cycles == []


def test_lost_writer_does_not_respawn_or_send(owner):
    instance, client, cycles, pid = owner
    instance.step(observe=False)
    pid[0] = 777
    with pytest.raises(LinuxBindingError, match="writer_changed"):
        instance.step(observe=True)
    assert len(client.requests) == 2 and cycles == []


def test_foreground_owner_reports_app_server_exit_before_stopping(owner):
    from codex_watchdog.app_server import AppServerError
    from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
    instance, client, cycles, pid = owner
    messages = []
    instance.health.notifier = EnvironmentNotifier(instance.binding.runtime,
        config=NotificationConfig(slack_webhook_url="https://hooks.slack.invalid/fixture"),
        http_post=lambda *args: messages.append(args) or 200)

    def exited(**kwargs):
        raise AppServerError("app_server_exited")

    client.pump = exited
    results = []
    assert instance.run(emit=results.append) == 1
    assert results[-1]["reason"] == "app_server_exited"
    assert results[-1]["notification"]["status"] == "sent"
    assert len(messages) == 1 and client.closed
    assert len(client.requests) == 2


def test_uncoordinated_owner_does_not_replay_an_uncertain_health_notification(owner):
    instance, client, cycles, pid = owner
    attempts = []

    class UncertainNotifier:
        def notify(self, event):
            attempts.append(event)
            raise RuntimeError("fixture interrupted after a possibly sent request")

    instance.health.notifier = UncertainNotifier()
    assert instance.report_failure("app_server_exited")["status"] == "blocked"
    assert instance.report_failure("app_server_exited")["reason"] == "linux_health_notification_outcome_uncertain"
    assert len(attempts) == 1


@pytest.mark.parametrize("busy", [False, True])
def test_failed_observation_cannot_report_recovery(owner, busy):
    instance, client, cycles, pid = owner
    instance.service.run_once = lambda: SimpleNamespace(status="error", workspaces=(),
        reason="service_cycle_lock_held" if busy else None)
    if busy:
        assert instance.step(observe=True)["owner_state"] == "owned"
    else:
        with pytest.raises(LinuxBindingError, match="linux_observation_failed"):
            instance.step(observe=True)
    assert not instance.health.path.exists()


def test_release_racing_observation_does_not_raise_a_loss_alert(owner):
    instance, client, cycles, pid = owner

    def releasing():
        instance.binding.set_state("release_requested")
        return SimpleNamespace(status="completed", workspaces=(), reason=None)

    instance.service.run_once = releasing
    assert instance.step(observe=True)["owner_state"] == "owned"
    assert instance.step(observe=True)["owner_state"] == "released"
    assert not instance.health.path.exists()


@pytest.mark.parametrize("already_owned", [False, True])
@pytest.mark.parametrize("already_lost", [False, True])
def test_queue_admission_defers_owner_without_losing_writer_or_health(owner, already_owned, already_lost, monkeypatch):
    from contextvars import Context
    from codex_watchdog import queue_wake
    instance, client, cycles, pid = owner
    monkeypatch.setattr(queue_wake, "codex_process_environment", lambda home: {})
    if already_owned:
        instance.step(observe=False)
    if already_lost:
        instance.report_failure("app_server_exited")
    health_before = instance.health.path.read_bytes() if already_lost else None
    requests_before = list(client.requests)
    reservation = reservation_path(instance.binding.codex_home, THREAD)
    binding_before = reservation.read_bytes()

    def courier(command, **kwargs):
        # Exercise the real queue dispatcher's control-lock admission while
        # the foreground owner tries its next writer check.
        result = Context().run(instance.step, observe=True)
        assert result["owner_state"] == "standby"
        assert result["reason"] == "control_operation_in_progress"
        assert client.requests == requests_before and not client.closed
        assert cycles == []
        after = instance.health.path.read_bytes() if instance.health.path.exists() else None
        assert after == health_before
        return subprocess.CompletedProcess(command, 0, f"Queued message {QUEUE} for thread {THREAD}.", "")

    dispatcher = QueueWakeDispatcher(instance.binding.runtime, codex_home=instance.binding.codex_home,
                                    codex_executable="fixture-codex", runner=courier)
    receipt = dispatcher.dispatch(THREAD, "owner-contention-test", "Reply only OK", "fixture")
    assert receipt.status == "enqueued", receipt.stderr
    assert receipt.queue_message_id == QUEUE
    assert reservation.read_bytes() == binding_before
    assert instance.step(observe=False)["owner_state"] == "owned"
    assert len(client.requests) == 2 and not client.closed


def test_control_contention_after_writer_admission_is_not_retried(owner):
    from codex_watchdog.control_state import ControlBusy
    instance, client, cycles, pid = owner
    calls = []

    def uncertain_step(**kwargs):
        calls.append("entered")
        raise ControlBusy("control_operation_in_progress")

    instance._owned_step = uncertain_step
    with pytest.raises(ControlBusy):
        instance.step(observe=True)
    assert calls == ["entered"] and client.requests == [] and cycles == []


@pytest.mark.parametrize("already_lost", [False, True])
def test_workspace_control_contention_preserves_health_until_observed(owner, already_lost):
    from codex_watchdog.control_state import control_file_lock
    from codex_watchdog.mvp_service import MvpWatchdogService
    instance, client, cycles, pid = owner
    workspace = instance.binding.workspace(instance.binding.load())
    service = MvpWatchdogService(
        instance.binding.runtime, codex_home=instance.binding.codex_home,
        registry=SimpleNamespace(list_workspaces=lambda: (workspace,)),
        notifier=instance.service.notifier,
    )
    if already_lost:
        instance.report_failure("linux_observation_failed")
    before = instance.health.path.read_bytes() if already_lost else None

    def contended_observation():
        # The owner has finished its writer check. A concurrent queue/release
        # command can hold this lock when the service begins its own guard.
        lock = instance.binding.codex_home / "watchdog-control" / THREAD / "owner.lock"
        with control_file_lock(lock):
            cycle = service.run_once()
        assert cycle.status == "completed" and len(cycle.workspaces) == 1
        assert cycle.workspaces[0].status == "standby"
        assert cycle.workspaces[0].reason == "control_operation_in_progress"
        return cycle

    instance.service.run_once = contended_observation
    assert instance.step(observe=True)["owner_state"] == "owned"
    after = instance.health.path.read_bytes() if instance.health.path.exists() else None
    assert after == before  # Neither a false outage nor a false recovery.


@pytest.mark.parametrize("reason", ["control_stale_epoch", "control_lease_expired", "control_activation_changed"])
def test_workspace_authority_failure_is_not_transient_contention(owner, reason):
    instance, client, cycles, pid = owner
    workspace = instance.binding.workspace(instance.binding.load())
    instance.service.run_once = lambda: SimpleNamespace(
        status="completed", reason=None, workspaces=(SimpleNamespace(
            workspace_id=workspace.workspace_id, status="standby", reason=reason),),
    )
    with pytest.raises(LinuxBindingError, match="linux_observation_failed"):
        instance.step(observe=True)
    assert not instance.health.path.exists()


@pytest.mark.parametrize("coordinated", [False, True])
@pytest.mark.parametrize("lock_kind", ["control", "sender"])
def test_operator_release_waits_for_initial_lock_admission(setup, coordinated, lock_kind, monkeypatch):
    from codex_watchdog import control_state
    from codex_watchdog.control_state import ControlStore, control_file_lock
    binding, workspace = setup
    store = ControlStore(binding.codex_home, THREAD, workspace.repo_root, boot_id="fixture-boot")
    monkeypatch.setattr(control_state, "ControlStore", lambda *args: store)
    if coordinated:
        store.attach("desktop", "host", "vscode")
    lock = (control_file_lock(store.lock_path) if lock_kind == "control" else
            FileLock(reservation_path(binding.codex_home, THREAD).with_suffix(".send.lock")))
    lock.__enter__()
    tick = [0.0]
    before = reservation_path(binding.codex_home, THREAD).read_bytes()

    def unlock(seconds):
        assert reservation_path(binding.codex_home, THREAD).read_bytes() == before
        if coordinated:
            assert store.read().get("auto_paused") is not True
        tick[0] += seconds
        lock.__exit__(None, None, None)

    result = binding.request_release(monotonic=lambda: tick[0], sleep=unlock)
    assert result["state"] == "release_requested" and tick[0] == 0.05
    if coordinated:
        assert store.read()["auto_paused"] is True


def test_operator_release_contention_is_bounded_and_preserves_state(setup):
    from codex_watchdog.control_state import ControlStore, control_file_lock
    binding, workspace = setup
    store = ControlStore(binding.codex_home, THREAD, workspace.repo_root, boot_id="fixture-boot")
    tick = [0.0]
    before = reservation_path(binding.codex_home, THREAD).read_bytes()
    with control_file_lock(store.lock_path):
        with pytest.raises(LinuxBindingError, match="linux_release_busy"):
            binding.request_release(monotonic=lambda: tick[0], sleep=lambda seconds: tick.__setitem__(0, tick[0]+seconds))
    assert tick[0] == 1.0
    assert reservation_path(binding.codex_home, THREAD).read_bytes() == before


def test_operator_release_does_not_adopt_first_activation_during_wait(setup):
    from codex_watchdog.control_state import ControlStore, control_file_lock
    binding, workspace = setup
    store = ControlStore(binding.codex_home, THREAD, workspace.repo_root, boot_id="fixture-boot")
    held = control_file_lock(store.lock_path)
    held.__enter__()
    tick = [0.0]
    before = reservation_path(binding.codex_home, THREAD).read_bytes()

    def activate(seconds):
        tick[0] += seconds
        held.__exit__(None, None, None)
        store.attach("new-desktop", "host", "vscode")

    with pytest.raises(LinuxBindingError, match="control_activation_changed"):
        binding.request_release(monotonic=lambda: tick[0], sleep=activate)
    assert store.read().get("auto_paused") is not True
    assert reservation_path(binding.codex_home, THREAD).read_bytes() == before


def test_operator_release_never_retries_after_admission(setup, monkeypatch):
    from codex_watchdog.control_state import ControlBusy
    binding, workspace = setup
    writes = []

    def interrupted(*args):
        writes.append(args)
        raise ControlBusy("fixture_write_interrupted")

    monkeypatch.setattr(InstructionStore, "_atomic_json", interrupted)
    with pytest.raises(ControlBusy, match="fixture_write_interrupted"):
        binding.request_release(sleep=lambda seconds: pytest.fail("must not replay a write"))
    assert len(writes) == 1
