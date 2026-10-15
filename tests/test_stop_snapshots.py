from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

import codex_watchdog.stop_snapshots as stops_module
from codex_watchdog.models import sha256_text
from codex_watchdog.storage import FileLock, InstructionStore, StoreBusyError
from codex_watchdog.stop_snapshots import StopSnapshots, STOP_MAX_BYTES, STOP_RETENTION
from test_mvp_service import (FakeAdapter, FakeMutator, FakeNotifier, FakeQueue,
                              FakeRegistry, build_service, observation, workspace,
                              write_rollout, write_stop_output, receipt, THREAD_A, THREAD_B, OID_B)


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def event(repo, *, session=THREAD_A, turn="turn-1", age=60, **overrides):
    completed = NOW - timedelta(seconds=age)
    value = {"schema_version": 1, "event_type": "Stop", "outcome": "grace_expired_parked",
        "audit_id": "audit-" + turn, "invocation_id": "12345678-1234-4234-8234-123456789abc",
        "session_id": session, "turn_id": turn, "workspace": str(repo.resolve()),
        "hook_started_at": (completed - timedelta(seconds=30)).isoformat(),
        "hook_completed_at": completed.isoformat(), "recorded_at": completed.isoformat(),
        "last_assistant_message_available": True, "last_output_sha256": sha256_text("output"),
        "last_output_chars": 6}
    return {**value, **overrides}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(stops_module, "utc_now", lambda: NOW.isoformat())
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = tmp_path / "runtime"
    snapshots = StopSnapshots(runtime)
    return SimpleNamespace(repo=repo, runtime=runtime, snapshots=snapshots,
                           tracked=workspace(repo))


def service_for(rig, **kwargs):
    kwargs.setdefault("clock", lambda: NOW)
    result = build_service(rig.runtime, [rig.tracked],
        FakeAdapter({str(rig.repo): [observation(rig.repo)]}), FakeMutator(),
        FakeNotifier(rig.runtime), FakeQueue(), **kwargs)
    return result


def test_repeated_stops_keep_only_current_terminal_and_phase(rig):
    store = InstructionStore(rig.runtime)
    for index in range(25):
        value = event(rig.repo, turn="turn-" + str(index), age=60 - index)
        waiting = {**value, "outcome": "waiting"}
        waiting.pop("hook_completed_at")
        store.record_audit(waiting)
        store.record_audit(value)
    assert len(list(store.audit.iterdir())) == 2
    assert len(list(rig.snapshots.directory.glob("*.json"))) == 2
    current = rig.snapshots.read(THREAD_A, rig.repo)
    assert current["event"]["turn_id"] == "turn-24"
    assert current["revision"] == 25
    assert "output" not in json.dumps(current).replace("last_output", "")


@pytest.mark.parametrize("change", ["waiting", "return_intent", "duplicate", "changed_output", "older", "late_older_turn", "equal_times"])
def test_waiting_duplicates_and_late_callbacks_cannot_replace_newer_completion(rig, change):
    current = event(rig.repo, age=30)
    assert rig.snapshots.publish(current)
    path = rig.snapshots.path(THREAD_A, rig.repo)
    before = path.read_bytes()
    if change == "waiting":
        candidate = {**current, "outcome": "waiting"}
        candidate.pop("hook_completed_at")
    elif change == "return_intent":
        candidate = {**current, "outcome": "return_intent"}
    elif change == "duplicate":
        candidate = {**current, "invocation_id": "another-invocation", "audit_id": "another-audit"}
    elif change == "changed_output":
        candidate = {**current, "last_output_sha256": sha256_text("changed")}
    elif change == "older":
        candidate = event(rig.repo, turn="older", age=120)
    elif change == "late_older_turn":
        candidate = event(rig.repo, turn="older", age=1,
                          hook_started_at=(NOW - timedelta(seconds=180)).isoformat())
    else:
        candidate = {**current, "turn_id": "cannot-order"}
    assert not rig.snapshots.publish(candidate)
    assert path.read_bytes() == before
    assert rig.snapshots.publish(event(rig.repo, turn="next", age=1))


def test_exact_scope_lookup_and_unknown_current_fields_survive(rig, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    rig.snapshots.publish(event(rig.repo))
    assert rig.snapshots.read(THREAD_B, rig.repo) is None
    assert rig.snapshots.read(THREAD_A, other) is None
    path = rig.snapshots.path(THREAD_A, rig.repo)
    current = json.loads(path.read_bytes())
    current["future_setting"] = {"preserve": True}
    InstructionStore._atomic_json(path, current)
    assert rig.snapshots.publish(event(rig.repo, turn="new", age=1, future_event="retain"))
    after = rig.snapshots.read(THREAD_A, rig.repo)
    assert after["future_setting"] == {"preserve": True}
    assert after["event"]["future_event"] == "retain"


@pytest.mark.parametrize("bad", ["json", "oversize", "boolean_version", "wrong_session", "wrong_repo", "wrong_hash", "wrong_floor", "no_event", "invalid_revision"])
def test_corrupt_current_scope_is_inert_and_does_not_block_healthy_scope(rig, bad):
    rig.snapshots.publish(event(rig.repo))
    path = rig.snapshots.path(THREAD_A, rig.repo)
    current = json.loads(path.read_bytes())
    if bad == "json":
        path.write_bytes(b"{")
    elif bad == "oversize":
        path.write_bytes(b" " * (STOP_MAX_BYTES + 10))
    else:
        if bad == "boolean_version": current["schema_version"] = True
        if bad == "wrong_session": current["session_id"] = THREAD_B
        if bad == "wrong_repo": current["workspace"] = str(rig.repo.parent)
        if bad == "wrong_hash": current["identity"] = "0" * 64
        if bad == "wrong_floor": current["floor_completed_at"] = NOW.isoformat()
        if bad == "no_event": current.pop("event")
        if bad == "invalid_revision": current["revision"] = True
        InstructionStore._atomic_json(path, current)
    before = path.read_bytes()
    assert rig.snapshots.read(THREAD_A, rig.repo) is None
    assert path.read_bytes() == before
    assert rig.snapshots.publish(event(rig.repo, session=THREAD_B, turn="healthy"))
    assert rig.snapshots.read(THREAD_B, rig.repo)["event"]["turn_id"] == "healthy"
    # A fresh independent completion can repair its own compact observation;
    # no provider reservation, queue or owner is reset.
    assert rig.snapshots.publish(event(rig.repo, turn="fresh", age=1))


def test_scope_lock_does_not_block_other_session(rig):
    from codex_watchdog.stop_snapshots import stop_scope
    key = stop_scope(THREAD_A, rig.repo)[0]
    with FileLock(rig.runtime / "locks" / ("stop-" + key + ".lock")):
        with pytest.raises(StoreBusyError):
            rig.snapshots.publish(event(rig.repo))
        assert rig.snapshots.publish(event(rig.repo, session=THREAD_B))
    assert rig.snapshots.publish(event(rig.repo))


@pytest.mark.parametrize("after_commit", [False, True])
def test_crash_publication_is_atomic_and_same_turn_is_not_republished(rig, after_commit):
    old = event(rig.repo)
    assert rig.snapshots.publish(old)
    before = rig.snapshots.path(THREAD_A, rig.repo).read_bytes()
    newer = event(rig.repo, turn="newer", age=1)
    def failed_write(path, value):
        if path.name.endswith(".floor.json"):
            if after_commit:
                InstructionStore._atomic_json(path, value)
                return
            raise OSError("fixture stop before publication boundary")
        if after_commit:
            InstructionStore._atomic_json(path, value)
        raise OSError("fixture stop at publication boundary")
    broken = StopSnapshots(rig.runtime, writer=failed_write)
    with pytest.raises(OSError): broken.publish(newer)
    if after_commit:
        assert rig.snapshots.read(THREAD_A, rig.repo)["event"]["turn_id"] == "newer"
        assert not rig.snapshots.publish(newer)
    else:
        assert rig.snapshots.path(THREAD_A, rig.repo).read_bytes() == before
        assert rig.snapshots.publish(newer)


def test_symlink_current_path_is_never_followed(rig, tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("OS has no symbolic links")
    target = tmp_path / "protected"
    target.write_bytes(b"protected")
    path = rig.snapshots.path(THREAD_A, rig.repo)
    path.parent.mkdir(parents=True)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("fixture cannot create symlink")
    assert rig.snapshots.read(THREAD_A, rig.repo) is None
    assert not rig.snapshots.publish(event(rig.repo))
    assert target.read_bytes() == b"protected"


def test_explicit_legacy_import_is_idempotent_and_keeps_inflight_unknown_and_native_data(rig):
    audit = rig.runtime / "audit"
    audit.mkdir(parents=True)
    old = event(rig.repo, turn="old", age=4 * 24 * 3600)
    recent = event(rig.repo, turn="recent", age=60)
    protected = {
        "waiting.json": {**old, "outcome": "waiting"},
        "intent.json": {**old, "outcome": "return_intent"},
        "unknown.json": {"schema_version": 1, "event_type": "user_data"},
        "spooled.json": event(rig.repo, turn="spooled", age=4 * 24 * 3600,
                              invocation_id="44444444-4444-4444-8444-444444444444"),
    }
    for name, value in {"a-old.json": old, "z-recent.json": recent, **protected}.items():
        InstructionStore._atomic_json(audit / name, value)
    (audit / "corrupt.json").write_bytes(b"{broken")
    spool = rig.runtime / "transient" / "stop-output" / (protected["spooled.json"]["invocation_id"] + ".json")
    spool.parent.mkdir(parents=True)
    spool.write_bytes(b"live output")
    native = rig.runtime / "native-history"
    native.write_bytes(b"native must stay untouched")
    before = {name: (audit / name).read_bytes() for name in (*protected, "corrupt.json")}
    assert rig.snapshots.read(THREAD_A, rig.repo) is None
    result = rig.snapshots.import_legacy(now=NOW)
    assert result["state"] == "completed" and result["visited"] == 7
    assert result["retired"] == 1
    assert rig.snapshots.read(THREAD_A, rig.repo)["event"]["turn_id"] == "recent"
    for name, raw in before.items(): assert (audit / name).read_bytes() == raw
    assert spool.read_bytes() == b"live output" and native.read_bytes() == b"native must stay untouched"
    assert rig.snapshots.import_legacy(now=NOW)["already_completed"] is True
    assert rig.snapshots.cleanup() == 1
    assert rig.snapshots.cleanup() == 0


def test_import_crash_can_resume_without_duplicate_current_publication(rig):
    audit = rig.runtime / "audit"
    audit.mkdir(parents=True)
    InstructionStore._atomic_json(audit / "legacy.json", event(rig.repo))
    def marker_failure(path, value):
        if path.name == "legacy-import.json": raise OSError("fixture import interrupted")
        InstructionStore._atomic_json(path, value)
    with pytest.raises(OSError): StopSnapshots(rig.runtime, writer=marker_failure).import_legacy(now=NOW)
    first = rig.snapshots.read(THREAD_A, rig.repo)
    assert rig.snapshots.import_legacy(now=NOW)["state"] == "completed"
    assert rig.snapshots.read(THREAD_A, rig.repo) == first


def test_cleanup_has_fixed_visit_budget_and_preserves_unrecognized_or_live_records(rig, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)
    monkeypatch.setattr(stops_module, "datetime", Clock)
    rig.snapshots.retired.mkdir(parents=True)
    old = event(rig.repo, age=4 * 24 * 3600)
    for index in range(100):
        InstructionStore._atomic_json(rig.snapshots.retired / (sha256_text(str(index)) + ".json"), old)
    unknown = rig.snapshots.retired / ("f" * 64 + ".json")
    unknown.write_bytes(b'{"kind":"native_user_data"}')
    live = rig.snapshots.retired / ("e" * 64 + ".json")
    InstructionStore._atomic_json(live, event(rig.repo))
    visited = []
    real_scandir = stops_module.os.scandir
    class Counted:
        def __init__(self, path): self.iterator = real_scandir(path)
        def __enter__(self): return self
        def __iter__(self): return self
        def close(self): self.iterator.close()
        def __exit__(self, *args): self.close()
        def __next__(self):
            result = next(self.iterator)
            visited.append(result.name)
            return result
    monkeypatch.setattr(stops_module.os, "scandir", Counted)
    removed = rig.snapshots.cleanup()
    assert len(visited) == 16
    expected = {sha256_text(str(index)) + ".json" for index in range(100)} & set(visited)
    assert removed == len(expected)
    assert all(not (rig.snapshots.retired/name).exists() for name in expected)
    assert unknown.read_bytes() == b'{"kind":"native_user_data"}'
    assert json.loads(live.read_bytes()) == event(rig.repo)
    monkeypatch.setattr(stops_module.os, "scandir", real_scandir)
    assert len(list(rig.snapshots.retired.glob("*.json"))) == 102 - removed
    rig.snapshots.close()


@pytest.mark.parametrize("problem", ["recent", "spool", "identity", "expired"])
def test_lifecycle_expiry_is_exact_and_never_deletes_current_output(rig, problem):
    value = event(rig.repo, age=4 * 24 * 3600 if problem != "recent" else 60)
    rig.snapshots.publish(value)
    current = rig.snapshots.read(THREAD_A, rig.repo)
    identity = current["identity"] if problem != "identity" else "0" * 64
    if problem == "spool":
        spool = rig.runtime / "transient" / "stop-output" / (value["invocation_id"] + ".json")
        spool.parent.mkdir(parents=True)
        spool.write_bytes(b"protected")
    removed = rig.snapshots.expire_scope(THREAD_A, rig.repo, expected_identity=identity, now=NOW)
    assert removed is (problem == "expired")
    if problem == "spool": assert spool.read_bytes() == b"protected"


def test_imported_completion_is_not_fresh_after_start_or_late_discovery(rig):
    audit = rig.runtime / "audit"
    audit.mkdir(parents=True)
    InstructionStore._atomic_json(audit / "a-old.json", event(rig.repo))
    service = service_for(rig)
    rig.snapshots.import_legacy(now=NOW)
    state, matches = service._load_state_and_stops(rig.tracked, replay_latest_stop=False)
    assert matches == ()
    assert state["stop_snapshot_identity"] == rig.snapshots.read(THREAD_A, rig.repo)["identity"]


def test_legacy_cursor_import_selects_only_current_completion_and_preserves_state(rig):
    audit = rig.runtime / "audit"
    audit.mkdir(parents=True)
    InstructionStore._atomic_json(audit / "a-done.json", event(rig.repo, turn="old", age=90))
    InstructionStore._atomic_json(audit / "z-latest.json", event(rig.repo, turn="latest", age=60))
    rig.snapshots.import_legacy(now=NOW)
    service = service_for(rig)
    state = {"schema_version": 2, "workspace_id": rig.tracked.workspace_id,
        "repo_root": str(rig.repo), "session_id": THREAD_A, "audit_cursor": "a-done.json",
        "last_remote_oid": None, "pending_remote_oid": None, "pending_remote_detected_at": None,
        "future_setting": "preserve"}
    InstructionStore._atomic_json(service.state_path(rig.tracked.workspace_id), state)
    new_state, matches = service._load_state_and_stops(rig.tracked, replay_latest_stop=False)
    assert [value["turn_id"] for value in matches] == ["latest"]
    assert new_state["future_setting"] == "preserve"
    service._write_state(service.state_path(rig.tracked.workspace_id), new_state)
    assert service._load_state_and_stops(rig.tracked, replay_latest_stop=False)[1] == ()


def test_corrupt_snapshot_keeps_independent_git_observation_running(rig):
    path = rig.snapshots.path(THREAD_A, rig.repo)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{")
    service = service_for(rig)
    assert service.run_once().workspaces[0].status == "completed"
    assert service.notifier.events == []


def test_rollout_lookup_revalidates_native_archive_and_repo_before_cached_tail(rig):
    service = service_for(rig)
    home = service.codex_home
    write_rollout(home, rig.tracked, "finished")
    assert service._latest_rollout_completion(rig.tracked)["turn_id"] == "finished"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET archived=1 WHERE id=?", (THREAD_A,))
    assert service._latest_rollout_completion(rig.tracked) is None
    assert THREAD_A not in service._rollout_snapshots


@pytest.mark.parametrize("history", [0, 1000, 10000, 100000])
@pytest.mark.parametrize("current", [False, True])
def test_stop_hot_paths_do_not_visit_historical_files(rig, monkeypatch, history, current):
    audit = rig.runtime / "audit"
    audit.mkdir(parents=True)
    # Actual historical directory entries, fixed one active exact scope.
    for index in range(history):
        (audit / (str(index).zfill(8) + ".json")).write_bytes(b'{"event_type":"old"}')
    if current: rig.snapshots.publish(event(rig.repo))
    reads, writes, enumerations = [], [], []
    original_read = stops_module._read
    original_write = InstructionStore._atomic_json
    original_glob, original_scandir = Path.glob, os.scandir
    def counted_read(path):
        result = original_read(path)
        reads.append((str(path), path.stat().st_size if path.is_file() else 0))
        return result
    def counted_write(path, value):
        original_write(path, value)
        writes.append(path.stat().st_size)
    def deny_glob(path, pattern):
        enumerations.append((str(path), pattern))
        raise AssertionError("ordinary Stop path traversed history")
    def deny_scandir(path):
        enumerations.append((str(path), "scandir"))
        raise AssertionError("ordinary Stop path traversed history")
    monkeypatch.setattr(stops_module, "_read", counted_read)
    monkeypatch.setattr(Path, "glob", deny_glob)
    monkeypatch.setattr(os, "scandir", deny_scandir)
    service = service_for(rig)
    before, matches = service._load_state_and_stops(rig.tracked, replay_latest_stop=False)
    assert matches == ()
    assert service._has_matching_stop_turn(rig.tracked, "turn-1") is current
    # Producer, normal startup, cycle lookup and exact-turn match all stay fixed.
    monkeypatch.setattr(InstructionStore, "_atomic_json", staticmethod(counted_write))
    InstructionStore(rig.runtime).record_audit(event(rig.repo, turn="next", age=1))
    assert len(reads) == 5 and len(writes) == 3 and enumerations == []
    assert sum(size for _, size in reads) < 6 * STOP_MAX_BYTES
    print(json.dumps({"history": history, "current": current, "snapshot_reads": len(reads),
        "parsed_bytes": sum(size for _, size in reads), "snapshot_writes": len(writes),
        "written_bytes": sum(writes), "historical_entries_visited": 0, "sql": 0,
        "provider_calls": 0}, sort_keys=True))


@pytest.mark.parametrize("loss", ["missing", "corrupt"])
def test_lost_snapshot_and_old_callback_do_not_become_fresh_for_new_discovery(rig, loss):
    original = event(rig.repo)
    rig.snapshots.publish(original)
    path = rig.snapshots.path(THREAD_A, rig.repo)
    if loss == "missing": path.unlink()
    else: path.write_bytes(b"{bad snapshot")
    service = service_for(rig)
    before = path.read_bytes() if path.exists() else None
    assert not rig.snapshots.publish(original)
    assert service._load_state_and_stops(rig.tracked, replay_latest_stop=False)[1] == ()
    assert (path.read_bytes() if path.exists() else None) == before
    assert rig.snapshots.publish(event(rig.repo, turn="actually-new", age=1))


def test_bad_old_floor_does_not_gate_new_native_completion_or_other_scopes(rig):
    original = event(rig.repo)
    rig.snapshots.publish(original)
    path = rig.snapshots.path(THREAD_A, rig.repo)
    path.unlink()
    floor = path.with_name(path.stem + ".floor.json")
    floor.write_bytes(b"{corrupt old observation")
    assert not rig.snapshots.publish(original)
    assert rig.snapshots.publish(event(rig.repo, session=THREAD_B))
    # A new hook's terminal writes immediately after actual completion.
    assert rig.snapshots.publish(event(rig.repo, turn="fresh-native", age=1))
    assert rig.snapshots.read(THREAD_A, rig.repo)["event"]["turn_id"] == "fresh-native"


def test_claim_then_snapshot_crash_cannot_trigger_rollout_fallback(rig):
    service = service_for(rig)
    value = event(rig.repo)
    def failed_snapshot(path, record):
        if path.name.endswith(".floor.json"):
            InstructionStore._atomic_json(path, record)
        else:
            raise OSError("fixture lost current payload after durable reservation")
    with pytest.raises(OSError): StopSnapshots(rig.runtime, writer=failed_snapshot).publish(value)
    assert rig.snapshots.read(THREAD_A, rig.repo) is None
    assert rig.snapshots.has_claimed_turn(THREAD_A, rig.repo, value["turn_id"])
    assert not rig.snapshots.publish(value)
    completion = {"turn_id": value["turn_id"], "completed_at": value["hook_completed_at"], "last_agent_message": "output"}
    service._latest_rollout_completion = lambda tracked: completion
    assert service._rollout_stop_fallback(rig.tracked) is None
    assert not (rig.runtime / "transient").exists()
    assert service.notifier.events == []
    assert rig.snapshots.publish(event(rig.repo, turn="later-independent", age=1))


@pytest.mark.parametrize("bad_cursor", [
    {"rollout_stop_turn_id": "finished"},
    {"rollout_stop_turn_id": "finished", "rollout_stop_completed_at": "bad"},
    {"rollout_stop_turn_id": "finished", "rollout_stop_completed_at": "2100-01-01T00:00:00Z"},
    {"rollout_stop_turn_id": [], "rollout_stop_completed_at": NOW.isoformat()},
])
def test_optional_bad_or_future_rollout_cursor_is_not_an_independent_completion_gate(rig, bad_cursor):
    service = service_for(rig)
    completion = {"turn_id": "finished", "completed_at": (NOW - timedelta(seconds=100)).isoformat(),
                  "last_agent_message": "fresh final"}
    service._latest_rollout_completion = lambda tracked: completion
    state = dict(bad_cursor)
    assert service._rollout_stop_fallback(rig.tracked, state=state)["turn_id"] == "finished"
    assert state["rollout_stop_turn_id"] == "finished"
    assert state["rollout_stop_completed_at"] == completion["completed_at"]


def test_rollout_completion_cursor_survives_restart_without_second_spool(rig):
    service = service_for(rig)
    completion = {"turn_id": "finished", "completed_at": (NOW - timedelta(seconds=100)).isoformat(),
                  "last_agent_message": "fresh final"}
    service._latest_rollout_completion = lambda tracked: completion
    state = {}
    assert service._rollout_stop_fallback(rig.tracked, state=state) is not None
    before = next((rig.runtime / "transient" / "stop-output").iterdir()).read_bytes()
    restarted = service_for(rig)
    restarted._latest_rollout_completion = lambda tracked: completion
    assert restarted._rollout_stop_fallback(rig.tracked, state=state) is None
    assert next((rig.runtime / "transient" / "stop-output").iterdir()).read_bytes() == before


def test_rollout_snapshot_read_is_capped_when_file_appends_after_stat(rig, monkeypatch):
    service = service_for(rig)
    path = write_rollout(service.codex_home, rig.tracked, "finished")
    original_open = Path.open
    reads = []
    original_size = path.stat().st_size
    class Appending:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()
        def seek(self, position): return self.handle.seek(position)
        def read(self, size):
            reads.append(size)
            with original_open(path, "ab") as output:
                output.write(b"x" * 1_100_000)
            return self.handle.read(size)
    def racing_open(candidate, *args, **kwargs):
        handle = original_open(candidate, *args, **kwargs)
        return Appending(handle) if candidate == path and args == ("rb",) else handle
    monkeypatch.setattr(Path, "open", racing_open)
    assert service._latest_rollout_completion(rig.tracked)["turn_id"] == "finished"
    assert reads == [original_size]


def test_rollout_wrong_thread_event_is_not_a_completion(rig):
    service = service_for(rig)
    path = write_rollout(service.codex_home, rig.tracked, "finished")
    lines = [json.loads(raw) for raw in path.read_text().splitlines()]
    lines[-1]["payload"]["thread_id"] = THREAD_B
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert service._latest_rollout_completion(rig.tracked) is None


@pytest.mark.parametrize("malformed", ["missing_recorded_at", "bad_recorded_at", "unavailable_nonempty"])
def test_malformed_legacy_completion_is_local_and_does_not_abort_import(rig, malformed):
    bad = event(rig.repo, turn="bad")
    if malformed == "missing_recorded_at": bad.pop("recorded_at")
    elif malformed == "bad_recorded_at": bad["recorded_at"] = "broken"
    else: bad["last_assistant_message_available"] = False
    audit = rig.runtime / "audit"
    audit.mkdir(parents=True)
    InstructionStore._atomic_json(audit / "bad.json", bad)
    InstructionStore._atomic_json(audit / "good.json", event(rig.repo, turn="good", age=1))
    before = (audit / "bad.json").read_bytes()
    assert rig.snapshots.import_legacy(now=NOW)["state"] == "completed"
    assert rig.snapshots.read(THREAD_A, rig.repo)["event"]["turn_id"] == "good"
    assert (audit / "bad.json").read_bytes() == before


@pytest.mark.parametrize("field,bad", [
    ("stop_snapshot_identity", "malformed"),
    ("stop_snapshot_revision", True),
    ("stop_snapshot_completed_at", "bad-time"),
    ("stop_snapshot_completed_at", "2100-01-01T00:00:00Z"),
    ("audit_cursor", "../unsafe.json"),
    ("audit_cursor", ["malformed"]),
])
def test_bad_optional_stop_cursor_preserves_evidence_and_independent_git_then_new_stop(rig, monkeypatch, field, bad):
    monkeypatch.setattr("codex_watchdog.mvp_service.utc_now", lambda: NOW.isoformat())
    rig.snapshots.publish(event(rig.repo, turn="already-observed", age=120))
    service = service_for(rig)
    state, _ = service._load_state_and_stops(rig.tracked, replay_latest_stop=False)
    state[field] = bad
    state["future_setting"] = {"preserve": True}
    path = service.state_path(rig.tracked.workspace_id)
    InstructionStore._atomic_json(path, state)
    old_bytes = path.read_bytes()
    service.git_adapter = FakeAdapter({str(rig.repo): [observation(rig.repo, upstream=OID_B, topology="remote_ahead")]})
    service.queue_dispatcher = FakeQueue(remote_receipt=receipt(THREAD_A, "git:fixture-new"))
    service._latest_rollout_completion = lambda tracked: None
    result = service.run_once().workspaces[0]
    assert result.status == "completed" and result.stop_count == 0
    assert service.queue_dispatcher.remote_calls == [(THREAD_A, OID_B, rig.tracked.workspace_id)]
    assert service.notifier.events == []
    assert json.loads(path.read_bytes())[field] == bad
    assert json.loads(path.read_bytes())["future_setting"] == state["future_setting"]
    assert old_bytes != path.read_bytes()  # legitimate new Git observation
    assert rig.snapshots.publish(event(rig.repo, turn="new-native", age=1))
    assert service.run_once().workspaces[0].stop_count == 1
    assert json.loads(path.read_bytes())[field] == bad
    assert service.run_once().workspaces[0].stop_count == 0
    restarted = service_for(rig)
    restarted._latest_rollout_completion = lambda tracked: None
    assert restarted.run_once().workspaces[0].stop_count == 0


def test_lost_claim_payload_then_newer_stop_prevents_old_rollout_requalification(rig):
    original = event(rig.repo, turn="A", age=180)
    def fail_payload(path, value):
        if path.name.endswith(".floor.json"):
            InstructionStore._atomic_json(path, value)
        else: raise OSError("fixture lost claimed A payload")
    with pytest.raises(OSError): StopSnapshots(rig.runtime, writer=fail_payload).publish(original)
    newer = event(rig.repo, turn="B", age=60)
    assert rig.snapshots.publish(newer)
    assert not rig.snapshots.has_claimed_turn(THREAD_A, rig.repo, "A")
    service = service_for(rig)
    service._latest_rollout_completion = lambda tracked: {
        "turn_id": "A", "completed_at": original["hook_completed_at"], "last_agent_message": "old output"}
    assert service._rollout_stop_fallback(rig.tracked, state={}) is None
    assert service.notifier.events == []
    assert not (rig.runtime / "transient").exists()
    service._latest_rollout_completion = lambda tracked: {
        "turn_id": "C", "completed_at": (NOW - timedelta(seconds=50)).isoformat(), "last_agent_message": "new output"}
    assert service._rollout_stop_fallback(rig.tracked, state={})["turn_id"] == "C"


def test_cleanup_rotates_past_retained_first_page_without_history_index(rig, monkeypatch):
    rig.snapshots.retired.mkdir(parents=True)
    for index in range(16):
        (rig.snapshots.retired / (str(index).zfill(64) + ".json")).write_bytes(b'{"kind":"user_data"}')
    for index in range(32):
        InstructionStore._atomic_json(rig.snapshots.retired / ("f" + str(index).zfill(63) + ".json"),
                                     event(rig.repo, age=4 * 24 * 3600))
    real = os.scandir
    with real(rig.snapshots.retired) as entries:
        ordered = sorted(entries, key=lambda entry: entry.name)
    starts, visits = [], []
    class Ordered:
        def __init__(self, path):
            starts.append(path)
            self.entries = iter(ordered)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __next__(self):
            entry = next(self.entries)
            visits.append(entry.name)
            return entry
        def close(self): pass
    monkeypatch.setattr(stops_module.os, "scandir", Ordered)
    assert rig.snapshots.cleanup() == 0
    assert len(visits) == 16
    assert rig.snapshots.cleanup() == 16
    assert len(visits) == 32 and len(starts) == 1
    assert rig.snapshots.cleanup() == 16
    rig.snapshots.close()


@pytest.mark.parametrize("corrupt_current", [False, True])
def test_valid_shaped_future_boundary_cannot_gate_fresh_terminal(rig, corrupt_current):
    original = event(rig.repo)
    rig.snapshots.publish(original)
    path = rig.snapshots.path(THREAD_A, rig.repo)
    floor_path = path.with_name(path.stem + ".floor.json")
    floor = json.loads(floor_path.read_bytes())
    floor.update(floor_started_at="2100-01-01T00:00:00Z", floor_completed_at="2100-01-01T00:00:01Z")
    if corrupt_current:
        current = json.loads(path.read_bytes())
        current["event"].update(hook_started_at=floor["floor_started_at"], hook_completed_at=floor["floor_completed_at"])
        current.update(floor_started_at=floor["floor_started_at"], floor_completed_at=floor["floor_completed_at"],
                       identity=stops_module._identity(current["event"]))
        floor["identity"] = current["identity"]
        InstructionStore._atomic_json(path, current)
    InstructionStore._atomic_json(floor_path, floor)
    assert not rig.snapshots.publish(original)
    assert rig.snapshots.publish(event(rig.repo, turn="actually-fresh", age=1))
    assert rig.snapshots.read(THREAD_A, rig.repo)["event"]["turn_id"] == "actually-fresh"


@pytest.mark.parametrize("change,removed", [("access",16),("identity",0),("size",0),("mtime",0)])
def test_retired_cleanup_ignores_read_access_time_but_keeps_mutation_guard(rig,monkeypatch,change,removed):
    rig.snapshots.retired.mkdir(parents=True)
    for index in range(20):
        InstructionStore._atomic_json(rig.snapshots.retired/(sha256_text(str(index))+".json"),event(rig.repo,age=4*24*3600))
    read,stat=stops_module._read,Path.stat
    touched=set()
    class AfterRead:
        def __init__(self,original):self.original=original
        def __getattr__(self,name):
            adjusted={"st_atime_ns":2_000_000_000,"st_atime":2}
            if change=="identity":adjusted["st_ino"]=1
            elif change=="size":adjusted["st_size"]=1
            elif change=="mtime":adjusted["st_mtime_ns"]=1
            return getattr(self.original,name)+adjusted.get(name,0)
        def __eq__(self,other):return False  # Complete stat includes changed access time.
    def after_read(path):
        value=read(path)
        if path.parent==rig.snapshots.retired:touched.add(path)
        return value
    def observed_stat(path,*args,**kwargs):
        original=stat(path,*args,**kwargs)
        return AfterRead(original) if path in touched else original
    monkeypatch.setattr(stops_module,"_read",after_read)
    monkeypatch.setattr(Path,"stat",observed_stat)
    assert rig.snapshots.cleanup()==removed
    rig.snapshots.close()
    assert len(list(rig.snapshots.retired.glob("*.json")))==20-removed
