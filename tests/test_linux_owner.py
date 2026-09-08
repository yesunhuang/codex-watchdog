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
    namespace["Path"] = type("HomePath", (), {"home": staticmethod(lambda: binding.codex_home.parent)})
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
    service = SimpleNamespace(run_once=lambda: cycles.append("read-only-service"))
    owner = LinuxThreadOwner(binding, service=service, client_factory=lambda *a: client)
    return owner, client, cycles, pid


def test_attached_writer_waits_without_sending_or_resuming(owner):
    instance, client, cycles, pid = owner
    pid[0] = 777
    assert instance.step(observe=True)["owner_state"] == "waiting_for_detach"
    assert client.requests == [] and cycles == []


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
