"""Exact current points survive a fenced authority selector change."""
import json
import os
from pathlib import Path

import pytest

from codex_watchdog.session_activity import SessionActivity, hello_claim_key
from codex_watchdog import session_local_state as local
from codex_watchdog.control_state import ControlError
from codex_watchdog.storage import FileLock, StoreBusyError
from codex_watchdog.relay_authority_migration import write_authority_snapshot
from test_session_authority_retention import (THREAD, OTHER, NOW, native, nodes,
                                             migrate, snapshot)


def seed(native):
    runtime = native["authority"].runtime
    activity = SessionActivity(runtime, clock=lambda: NOW)
    activity.observe(THREAD, genuine_at=NOW-1, turn_active=False, queue_active=False)
    assert activity.claim_hello(THREAD, "slack", "scope", "a"*64)
    lifecycle = runtime/"lifecycle"/(THREAD+".json")
    value = json.loads(lifecycle.read_bytes())
    value["future_compatible"] = {"choice": [1, "preserve"]}
    lifecycle.write_text(json.dumps(value, indent=3)+"\n")
    cursor = runtime/"activity-cursors"/(THREAD+".json")
    cursor.parent.mkdir()
    cursor.write_text(json.dumps(dict(schema_version=1, thread_id=THREAD,
        future_compatible={"preserve": True}, cursor=dict(path="/fixture/rollout.jsonl",
        offset=123456, boundary_sha256="b"*64, observed_since=NOW-300,
        checked_at=NOW, activity_known=True, genuine_at=NOW-1, lifecycle=None,
        human_turn=None, future_cursor="keep"))))
    (runtime/"locks"/("activity-cursor-"+THREAD+".lock")).write_bytes(b"\0")
    foreign = cursor.with_name(OTHER+".json")
    foreign.write_text('{"unknown_other_session":true}')
    write_authority_snapshot(native["source"], snapshot())
    return runtime, {"lifecycle": lifecycle.read_bytes(), "activity-cursors": cursor.read_bytes()}


def test_migration_preserves_current_points_and_hello_claim_before_attempt(native):
    old, expected = seed(native)
    notification = native["authority"].notification_runtime
    migrate(native)
    selected = native["authority"].runtime
    for directory, raw in expected.items():
        assert (selected/directory/(THREAD+".json")).read_bytes() == raw
        assert (old/directory/(THREAD+".json")).read_bytes() == raw
        assert not (selected/directory/(OTHER+".json")).exists()
    activity = SessionActivity(selected, clock=lambda: NOW+1)
    assert not activity.claim_hello(THREAD, "slack", "scope", "c"*64)
    assert activity.snapshot(THREAD)["hello_claims"][hello_claim_key("slack", "scope")]["generation"] == 1
    assert native["authority"].notification_runtime == notification


@pytest.mark.parametrize("phase", ["before_manifest", "after_manifest"])
def test_crash_repeat_keeps_claim_and_cursor_then_allows_selected_progress(native, monkeypatch, phase):
    from codex_watchdog import session_authority_retention as retention
    old, expected = seed(native)
    authority = native["authority"]
    original = retention.control_atomic_json
    def crash(path, value):
        if path == authority.manifest:
            if phase == "after_manifest":
                original(path, value)
            raise RuntimeError("fixture selector crash")
        original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(retention, "control_atomic_json", crash)
        with pytest.raises(RuntimeError, match="selector crash"):
            migrate(native)
    migrate(native)
    for directory, raw in expected.items():
        assert (authority.runtime/directory/(THREAD+".json")).read_bytes() == raw
    lifecycle = SessionActivity(authority.runtime, clock=lambda: NOW+10)
    lifecycle.observe(THREAD, genuine_at=NOW+9, turn_active=True, queue_active=False)
    progressed = lifecycle.snapshot(THREAD)
    assert migrate(native)["status"] == "already_migrated"
    assert lifecycle.snapshot(THREAD) == progressed
    assert not lifecycle.claim_hello(THREAD, "slack", "scope", "d"*64)


def test_absent_current_points_and_locks_stay_absent_in_old_runtime(native):
    write_authority_snapshot(native["source"], snapshot())
    old = native["authority"].runtime
    before = {str(path.relative_to(old)): path.read_bytes() for path in old.rglob("*") if path.is_file()}
    migrate(native)
    after = {str(path.relative_to(old)): path.read_bytes() for path in old.rglob("*") if path.is_file()}
    # Actual migration now takes the common sender fence in the old root too.
    # Its single lock byte is the only allowed addition; no old payload or
    # per-session activity/cursor lock is created, changed or removed.
    expected = {**before, str(Path("locks")/"notification-senders.lock"): b"\0"}
    assert after == expected
    for directory in ("lifecycle", "activity-cursors"):
        assert not (old/directory).exists()
    assert not (native["authority"].runtime/"lifecycle").exists()
    assert not (native["authority"].runtime/"activity-cursors").exists()


@pytest.mark.parametrize("kind", ["lifecycle", "activity-cursors"])
@pytest.mark.parametrize("change", ["wrong_session", "boolean_schema", "corrupt", "oversized", "symlink", "hardlink"])
def test_invalid_exact_point_refuses_without_changing_old_state_or_selector(native, kind, change):
    old, _ = seed(native)
    path = old/kind/(THREAD+".json")
    if change in ("wrong_session", "boolean_schema"):
        value = json.loads(path.read_bytes())
        value["schema_version" if change == "boolean_schema" else
              ("session_id" if kind == "lifecycle" else "thread_id")] = True if change == "boolean_schema" else OTHER
        path.write_text(json.dumps(value))
    elif change == "corrupt":
        path.write_bytes(b"{broken")
    elif change == "oversized":
        path.write_bytes(b" "*(local.LIMITS[kind]+1))
    else:
        original = path.with_suffix(".original")
        path.rename(original)
        if change == "symlink":
            try:
                path.symlink_to(original)
            except (OSError, NotImplementedError) as error:
                if os.name != "nt" or isinstance(error, OSError) and getattr(error, "winerror", None) != 1314:
                    raise
                pytest.skip("Windows account cannot create the synthetic symlink")
        else:
            os.link(original, path)
    before, manifest = path.read_bytes(), native["authority"].manifest.read_bytes()
    with pytest.raises(ControlError, match="session_local_state"):
        migrate(native)
    assert path.read_bytes() == before
    assert native["authority"].manifest.read_bytes() == manifest
    assert not (native["authority"].root/"session-authorities"/THREAD/"migration-intent.json").exists()


@pytest.mark.parametrize("kind,prefix", [("lifecycle", "lifecycle-"), ("activity-cursors", "activity-cursor-")])
@pytest.mark.parametrize("state", ["missing", "empty", "busy"])
def test_producer_lock_is_never_created_reset_or_bypassed(native, kind, prefix, state):
    old, expected = seed(native)
    lock = old/"locks"/(prefix+THREAD+".lock")
    manifest = native["authority"].manifest.read_bytes()
    if state == "missing":
        lock.unlink()
        with pytest.raises(ControlError, match="source_lock_missing"):
            migrate(native)
        assert not lock.exists()
    elif state == "empty":
        lock.write_bytes(b"")
        with pytest.raises(ControlError, match="source_lock_invalid"):
            migrate(native)
        assert lock.read_bytes() == b""
    else:
        with FileLock(lock):
            with pytest.raises(StoreBusyError):
                migrate(native)
        assert lock.read_bytes() == b"\0"
    assert (old/kind/(THREAD+".json")).read_bytes() == expected[kind]
    assert native["authority"].manifest.read_bytes() == manifest


@pytest.mark.parametrize("kind", ["lifecycle", "activity-cursors"])
def test_source_point_change_after_staging_refuses_selector_then_explicit_reprepare(native, monkeypatch, kind):
    from codex_watchdog import session_authority_retention as retention
    from test_session_authority_retention import abandon
    old, _ = seed(native)
    authority = native["authority"]
    before = authority.manifest.read_bytes()
    original = local.SessionLocalState.revalidate
    def changed(instance):
        path = old/kind/(THREAD+".json")
        value = json.loads(path.read_bytes())
        value["later_current_producer"] = True
        path.write_text(json.dumps(value))
        original(instance)
    with monkeypatch.context() as patch:
        patch.setattr(local.SessionLocalState, "revalidate", changed)
        with pytest.raises(ControlError, match="snapshot_changed"):
            migrate(native)
    assert authority.manifest.read_bytes() == before
    intent = json.loads((authority.root/"session-authorities"/THREAD/"migration-intent.json").read_bytes())
    assert abandon(native, intent)["status"] == "abandoned"
    assert migrate(native)["status"] == "migrated"
    assert json.loads((authority.runtime/kind/(THREAD+".json")).read_bytes())["later_current_producer"] is True


def test_source_absence_is_revalidated_before_selection(native, monkeypatch):
    old, expected = seed(native)
    point = old/"activity-cursors"/(THREAD+".json")
    point.unlink()
    original = local.SessionLocalState.revalidate
    def appeared(instance):
        point.write_bytes(expected["activity-cursors"])
        original(instance)
    manifest = native["authority"].manifest.read_bytes()
    monkeypatch.setattr(local.SessionLocalState, "revalidate", appeared)
    with pytest.raises(ControlError, match="snapshot_changed"):
        migrate(native)
    assert native["authority"].manifest.read_bytes() == manifest


def test_partial_local_staging_resumes_exact_bytes(native, monkeypatch):
    from codex_watchdog import messaging_profile
    old, expected = seed(native)
    original = messaging_profile.write_new
    def crash(path, raw):
        if "activity-cursors" in path.parts:
            raise RuntimeError("fixture between current points")
        original(path, raw)
    with monkeypatch.context() as patch:
        patch.setattr(messaging_profile, "write_new", crash)
        with pytest.raises(RuntimeError, match="between current points"):
            migrate(native)
    migrate(native)
    for kind, raw in expected.items():
        assert (native["authority"].runtime/kind/(THREAD+".json")).read_bytes() == raw


def test_exact_helper_never_enumerates_other_sessions_or_native_history(native, monkeypatch):
    old, expected = seed(native)
    monkeypatch.setattr(local.os, "scandir", lambda *args: pytest.fail("history enumeration"))
    with local.capture_session_local_state(old, THREAD) as current:
        assert current.records == expected
        current.revalidate()


@pytest.mark.parametrize("field,value", [("generation", True), ("hello_claims", []), ("status", "UNKNOWN")])
def test_lifecycle_schema_validation_is_shared_with_current_producer(native, field, value):
    old, _ = seed(native)
    path = old/"lifecycle"/(THREAD+".json")
    body = json.loads(path.read_bytes())
    body[field] = value
    path.write_text(json.dumps(body))
    with pytest.raises(ControlError, match="record_invalid"):
        migrate(native)


def test_cursor_floor_is_not_normalized_or_clock_refreshed(native):
    old, _ = seed(native)
    path = old/"activity-cursors"/(THREAD+".json")
    body = json.loads(path.read_bytes())
    body["cursor"]["checked_at"] = NOW+600
    path.write_text(json.dumps(body))
    raw = path.read_bytes()
    migrate(native)
    assert (native["authority"].runtime/"activity-cursors"/(THREAD+".json")).read_bytes() == raw


def test_disappearing_source_lock_is_never_recreated(native, monkeypatch):
    old, expected = seed(native)
    lock = old/"locks"/("lifecycle-"+THREAD+".lock")
    original = Path.open
    def disappear(path, *args, **kwargs):
        if path == lock and args == ("r+b",):
            path.unlink()
        return original(path, *args, **kwargs)
    manifest = native["authority"].manifest.read_bytes()
    monkeypatch.setattr(Path, "open", disappear)
    with pytest.raises(FileNotFoundError):
        migrate(native)
    assert not lock.exists()
    assert (old/"lifecycle"/(THREAD+".json")).read_bytes() == expected["lifecycle"]
    assert native["authority"].manifest.read_bytes() == manifest


def test_dormant_state_is_preserved_without_reactivation(native):
    old, _ = seed(native)
    lifecycle = SessionActivity(old, clock=lambda: NOW+72*3600+1)
    assert lifecycle.observe(THREAD, turn_active=False, queue_active=False)["status"] == "DORMANT"
    raw = (old/"lifecycle"/(THREAD+".json")).read_bytes()
    migrate(native)
    selected = native["authority"].runtime
    assert (selected/"lifecycle"/(THREAD+".json")).read_bytes() == raw
    assert not SessionActivity(selected).is_active(THREAD)
    assert not SessionActivity(selected).claim_hello(THREAD, "slack", "scope", "e"*64)


def test_replaced_source_lock_refuses_selector_even_when_point_bytes_match(native, monkeypatch):
    old, _ = seed(native)
    lock = old/"locks"/("lifecycle-"+THREAD+".lock")
    manifest = native["authority"].manifest.read_bytes()
    original = local.SessionLocalState.revalidate
    def replaced(instance):
        try:
            lock.rename(lock.with_suffix(".prior"))
        except PermissionError as error:
            if os.name != "nt" or getattr(error, "winerror", None) != 32:
                raise
            pytest.skip("Windows kernel prevents replacement of this actually held lock")
        lock.write_bytes(b"\0")
        original(instance)
    monkeypatch.setattr(local.SessionLocalState, "revalidate", replaced)
    with pytest.raises(ControlError, match="source_lock_changed"):
        migrate(native)
    assert native["authority"].manifest.read_bytes() == manifest
