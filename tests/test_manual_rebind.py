import hashlib
import json
from pathlib import Path

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.queue_wake import QueueWakeDispatcher, REMOTE_UPDATE_PROMPT
from codex_watchdog.storage import InstructionStore, snapshot_state
from codex_watchdog.workspace_registry import WorkspaceRegistry
from test_mvp_service import (
    THREAD_A, THREAD_B, OID_A, OID_B, FakeAdapter, FakeMutator, FakeNotifier,
    FakeQueue, build_service, observation, write_audit,
)


def setup_rebind(tmp_path, monkeypatch):
    runtime, repo = tmp_path / "runtime", tmp_path / "repo"
    repo.mkdir()
    registry = WorkspaceRegistry(runtime)
    monkeypatch.setattr("codex_watchdog.workspace_registry.utc_now", lambda: "2026-09-01T00:00:00Z")
    old = registry.add("watchdog", repo, THREAD_A).workspace
    # This new thread's Stop sorts behind an unrelated cursor already consumed
    # by the old registration. A cursor copied without re-baselining loses it.
    write_audit(runtime, "a-new-first.json", old, audit_id="new-first", session_id=THREAD_B,
                completed_at="2026-09-01T00:00:20Z")
    write_audit(runtime, "a-new-history.json", old, audit_id="new-history", session_id=THREAD_B,
                completed_at="2026-09-01T00:00:05Z")
    write_audit(runtime, "z-old-cursor.json", old, audit_id="old")
    notifier = FakeNotifier(runtime)
    service = build_service(runtime, [old], FakeAdapter({str(repo): [observation(repo)]}),
                            FakeMutator(), notifier, FakeQueue())
    service.registry = registry
    assert service.run_once().workspaces[0].stop_count == 0
    path = service.state_path("watchdog")
    state = json.loads(path.read_text())
    state["future_compatible_setting"] = {"retain": True}
    InstructionStore._atomic_json(path, state)
    original = path.read_bytes()
    registry.remove("watchdog")
    monkeypatch.setattr("codex_watchdog.workspace_registry.utc_now", lambda: "2026-09-01T00:00:10Z")
    new = registry.add("watchdog", repo, THREAD_B).workspace
    return service, registry, old, new, original, notifier


def test_manual_rebind_keeps_backup_and_notifies_first_new_stop_once(tmp_path, monkeypatch):
    service, registry, old, new, original, notifier = setup_rebind(tmp_path, monkeypatch)
    result = service.run_once().workspaces[0]
    assert result.status == "completed" and result.stop_count == 1
    assert result.stop_audit_id == "new-first"
    path = service.state_path("watchdog")
    backup = path.with_name(path.name + ".backup-" + hashlib.sha256(original).hexdigest())
    assert backup.read_bytes() == original
    state = json.loads(path.read_text())
    assert state["session_id"] == THREAD_B and state["schema_version"] == 2
    assert state["future_compatible_setting"] == {"retain": True}
    # A new controller reads the committed migration without replaying it.
    restarted = build_service(service.runtime, [new], service.git_adapter, FakeMutator(), notifier, FakeQueue())
    restarted.registry = registry
    assert restarted.run_once().workspaces[0].stop_count == 0
    write_audit(service.runtime, "zz-next.json", new, audit_id="next", completed_at="2026-09-01T00:01:00Z")
    assert restarted.run_once().workspaces[0].stop_count == 1
    assert restarted.run_once().workspaces[0].stop_count == 0
    assert len([e for e in notifier.events if e.event_type == "codex_parked"]) == 2
    assert backup.read_bytes() == original


def test_rebind_preserves_pending_git_and_never_resends_old_delivery(tmp_path, monkeypatch):
    service, registry, old, new, original, notifier = setup_rebind(tmp_path, monkeypatch)
    path = service.state_path("watchdog")
    state = json.loads(path.read_text())
    state.update(pending_remote_oid=OID_B, pending_remote_detected_at="2026-09-01T00:00:01Z")
    InstructionStore._atomic_json(path, state)
    wake_id = "git:" + sha256_text("watchdog")[:16] + ":" + OID_B
    record = service.runtime / "wake/records" / (sha256_text(wake_id) + ".json")
    InstructionStore._atomic_json(record, {"schema_version": 2, "instruction_id": wake_id,
        "thread_id": THREAD_A, "source": "remote_git", "prompt_sha256": sha256_text(REMOTE_UPDATE_PROMPT),
        "state": "uncertain", "future_setting": "retain"})
    receipt_bytes = record.read_bytes()
    def forbidden_runner(*args, **kwargs):
        raise AssertionError("An old uncertain delivery must never be resent")
    service.queue_dispatcher = QueueWakeDispatcher(service.runtime, codex_home=service.codex_home,
                                                   codex_executable="fixture-codex", runner=forbidden_runner)
    service.git_adapter = FakeAdapter({str(new.repo_root): [observation(new.repo_root, upstream=OID_B, topology="remote_ahead")]})
    result = service.run_once().workspaces[0]
    assert result.status == "completed" and result.stop_count == 1
    assert result.wake["status"] == "exception"
    updated = json.loads(path.read_text())
    assert updated["pending_remote_oid"] == OID_B
    assert updated["pending_remote_detected_at"] == state["pending_remote_detected_at"]
    assert record.read_bytes() == receipt_bytes
    assert updated["session_id"] == THREAD_B


@pytest.mark.parametrize("problem", ["no_manual_registration", "different_repo", "invalid_time", "future_schema", "invalid_old_thread"])
def test_ambiguous_or_unsupported_rebind_keeps_old_state(tmp_path, monkeypatch, problem):
    service, registry, old, new, original, notifier = setup_rebind(tmp_path, monkeypatch)
    path = service.state_path("watchdog")
    if problem == "no_manual_registration":
        registry.remove("watchdog")
    elif problem == "different_repo":
        state = json.loads(path.read_text()); state["repo_root"] = str(tmp_path / "other")
        InstructionStore._atomic_json(path, state)
    elif problem == "invalid_time":
        data = json.loads(registry.path.read_text()); data["workspaces"][0]["registered_at"] = "invalid"
        InstructionStore._atomic_json(registry.path, data)
    else:
        state = json.loads(path.read_text())
        state["schema_version" if problem == "future_schema" else "session_id"] = 99 if problem == "future_schema" else "invalid"
        InstructionStore._atomic_json(path, state)
    original = path.read_bytes()
    with pytest.raises((ValueError, KeyError)):
        service._load_state_and_stops(new, replay_latest_stop=False)
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.backup-*"))
    assert not notifier.events


def test_failed_cycle_publication_leaves_old_state_and_reusable_backup(tmp_path, monkeypatch):
    service, registry, old, new, original, notifier = setup_rebind(tmp_path, monkeypatch)
    # A transient Git pass must not advance the new thread's audit cursor.
    busy = observation(new.repo_root, blockers=("state_changed_during_observation",))
    service.git_adapter = FakeAdapter({str(new.repo_root): [busy]})
    result = service.run_once().workspaces[0]
    assert result.stop_count == 0
    assert service.state_path("watchdog").read_bytes() == original
    assert len(list(service.states.glob("*.backup-*"))) == 1
    service.git_adapter = FakeAdapter({str(new.repo_root): [observation(new.repo_root)]})
    assert service.run_once().workspaces[0].stop_count == 1
    assert service.run_once().workspaces[0].stop_count == 0
    assert len(list(service.states.glob("*.backup-*"))) == 1


def test_snapshot_is_independent_idempotent_and_refuses_collision(tmp_path):
    path = tmp_path / "state.json"
    original = b'{ "preserve": "exact bytes" }\n'
    path.write_bytes(original)
    backup = snapshot_state(path, original)
    assert snapshot_state(path, original) == backup
    path.write_bytes(b'{}')
    assert backup.read_bytes() == original
    with pytest.raises(ValueError, match="state changed"):
        snapshot_state(path, original)
    path.write_bytes(original)
    backup.write_bytes(b'wrong backup')
    with pytest.raises(ValueError, match="backup collision"):
        snapshot_state(path, original)
    assert path.read_bytes() == original


def test_crash_after_notification_does_not_send_first_stop_twice(tmp_path, monkeypatch):
    from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig

    service, registry, old, new, original, _ = setup_rebind(tmp_path, monkeypatch)
    sends = []
    def post(url, payload, timeout):
        sends.append(payload)
        return 200
    config = NotificationConfig(slack_webhook_url="https://hooks.slack.invalid/fixture")
    service.notifier = EnvironmentNotifier(service.runtime, config, http_post=post)
    state_path = service.state_path("watchdog")
    def failed_write(path, value):
        if path == state_path:
            raise OSError("fixture: controller stopped before state publication")
        InstructionStore._atomic_json(path, value)
    service.atomic_writer = failed_write
    assert service.run_once().workspaces[0].status == "error"
    assert len(sends) == 1 and state_path.read_bytes() == original
    restarted = build_service(service.runtime, [new], service.git_adapter, FakeMutator(),
                              EnvironmentNotifier(service.runtime, config, http_post=post), FakeQueue())
    restarted.registry = registry
    assert restarted.run_once().workspaces[0].stop_count == 1
    assert restarted.run_once().workspaces[0].stop_count == 0
    assert len(sends) == 1


def test_failed_backup_prevents_migration_or_notification(tmp_path, monkeypatch):
    service, registry, old, new, original, notifier = setup_rebind(tmp_path, monkeypatch)
    def fail_snapshot(*args):
        raise OSError("fixture: backup could not be published")
    monkeypatch.setattr("codex_watchdog.mvp_service.snapshot_state", fail_snapshot)
    assert service.run_once().workspaces[0].status == "error"
    assert service.state_path("watchdog").read_bytes() == original
    assert not notifier.events


def test_legacy_state_rebind_preserves_previous_schema_backup(tmp_path, monkeypatch):
    service, registry, old, new, original, notifier = setup_rebind(tmp_path, monkeypatch)
    path = service.state_path("watchdog")
    state = json.loads(path.read_text())
    state["schema_version"] = 1
    state.pop("last_remote_oid")
    InstructionStore._atomic_json(path, state)
    original = path.read_bytes()
    assert service.run_once().workspaces[0].stop_count == 1
    assert json.loads(path.read_text())["schema_version"] == 2
    assert next(path.parent.glob("*.backup-*")).read_bytes() == original
