"""Linux editor reattachment is one immutable signal, never a polling clock."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codex_watchdog import linux_attachment as attachment
from codex_watchdog import linux_owner, observed_writer
from codex_watchdog.native_inflight import NativeInflightError
from codex_watchdog.session_activity import SessionActivity, TTL_SECONDS
from codex_watchdog.session_lifecycle import SessionLifecycle
from codex_watchdog.workspace_registry import TrackedWorkspace


SID = "11111111-2222-4333-8444-555555555555"
BOOT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # Synthetic OS proof only: never inspect or control a native process.
    monkeypatch.setattr(attachment.sys, "platform", "linux")
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = TrackedWorkspace.create("fixture", repo, SID)
    now = [1800000000.0]
    birth = "linux:" + BOOT + ":500"
    monkeypatch.setattr(attachment, "_boot_time", lambda: now[0] - 10)
    monkeypatch.setattr(attachment.os, "sysconf", lambda name: 100, raising=False)
    monkeypatch.setattr(linux_owner, "writer_pid", lambda *args: 321)
    monkeypatch.setattr(linux_owner, "vscode_writer", lambda pid: pid == 321)
    monkeypatch.setattr(observed_writer, "_linux_birth", lambda pid: birth)
    calls = []
    def capture(home, thread, repo, pid):
        calls.append((home, thread, repo, pid))
        return {"writer": {"platform": "linux", "pid": pid, "process_birth": birth}}
    monkeypatch.setattr(observed_writer, "capture_writer", capture)
    return SimpleNamespace(home=tmp_path / "home", workspace=workspace, now=now,
                           birth=birth, capture=capture, calls=calls)


def test_new_verified_writer_reactivates_original_lifecycle_once(rig, monkeypatch, tmp_path):
    # btime is fixed even as observation and daemon instances change.
    boot_time = rig.now[0] - 10
    monkeypatch.setattr(attachment, "_boot_time", lambda: boot_time)
    stamp = attachment.native_reattachment_at(rig.home, rig.workspace)
    assert stamp == rig.now[0] - 5
    runtime = tmp_path / "runtime"
    notifier = SimpleNamespace(config=SimpleNamespace(interactive_providers=()))
    lifecycle = SessionLifecycle(runtime, notifier, clock=lambda: rig.now[0])
    signals = dict(exact=True, genuine_at=rig.now[0] - 2 * TTL_SECONDS,
                   turn_active=False, queue_active=False)
    before = lifecycle.observe(rig.workspace, signals, verified_owner=True, legacy=True)
    assert before["status"] == "DORMANT"
    reopened = replace(rig.workspace, attachment_at=stamp)
    active = lifecycle.observe(reopened, signals, verified_owner=True, legacy=True)
    assert active["status"] == "ACTIVE" and active["activated"] is True
    generation = active["generation"]
    rig.now[0] += 10
    # A new service instance sees the same birth, not a new enrollment.
    reconstructed = SessionLifecycle(runtime, notifier, clock=lambda: rig.now[0])
    again = reconstructed.observe(replace(rig.workspace, attachment_at=
        attachment.native_reattachment_at(rig.home, rig.workspace)), signals, verified_owner=True)
    assert again["generation"] == generation and again["activated"] is False
    assert again["last_activity_at"] == stamp
    rig.now[0] = stamp + TTL_SECONDS + 1
    idle = reconstructed.observe(reopened, signals, verified_owner=True)
    assert idle["status"] == "DORMANT" and idle["generation"] == generation
    assert SessionActivity(runtime).snapshot(SID)["last_activity_at"] == stamp
    assert all(call[1:] == (SID, rig.workspace.repo_root, 321) for call in rig.calls)


@pytest.mark.parametrize("fault", ["no_writer", "not_editor", "proof", "birth_changed",
    "writer_changed", "platform", "pid", "malformed_birth", "ticks", "hz", "boot"])
def test_unknown_foreign_changed_or_invalid_writer_never_activates(rig, monkeypatch, fault):
    if fault == "no_writer":
        monkeypatch.setattr(linux_owner, "writer_pid", lambda *args: None)
    elif fault == "not_editor":
        monkeypatch.setattr(linux_owner, "vscode_writer", lambda pid: False)
    elif fault == "proof":
        def fail(*args): raise NativeInflightError("held_writer_not_proven")
        monkeypatch.setattr(observed_writer, "capture_writer", fail)
    elif fault == "birth_changed":
        monkeypatch.setattr(observed_writer, "_linux_birth", lambda pid: rig.birth + "1")
    elif fault == "writer_changed":
        pids = iter([321, 322])
        monkeypatch.setattr(linux_owner, "writer_pid", lambda *args: next(pids))
    elif fault == "hz":
        monkeypatch.setattr(attachment.os, "sysconf", lambda name: 0)
    elif fault == "boot":
        monkeypatch.setattr(attachment, "_boot_time", lambda: float("inf"))
    else:
        def malformed(*args):
            value = rig.capture(*args)
            writer = value["writer"]
            if fault == "platform": writer["platform"] = "darwin"
            if fault == "pid": writer["pid"] = 322
            if fault == "malformed_birth": writer["process_birth"] = "linux:unknown:500"
            if fault == "ticks": writer["process_birth"] = "linux:" + BOOT + ":0"
            return value
        monkeypatch.setattr(observed_writer, "capture_writer", malformed)
    assert attachment.native_reattachment_at(rig.home, rig.workspace) is None


def test_other_platform_does_no_native_query(rig, monkeypatch):
    monkeypatch.setattr(attachment.sys, "platform", "win32")
    assert attachment.native_reattachment_at(rig.home, rig.workspace) is None
    assert not rig.calls
