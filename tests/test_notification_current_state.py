import json
from pathlib import Path

import pytest

from codex_watchdog.notification_state import CurrentNotificationState, RECENT_LIMIT, TTL_SECONDS

KEY = "a" * 64
OTHER = "b" * 64


def test_claim_before_send_and_restart_prevents_retry(tmp_path):
    clock = [100.0]
    store = CurrentNotificationState(tmp_path, clock=lambda: clock[0])
    assert store.claim(KEY, "c" * 64)
    # A crash has no finish receipt; eligibility still ends before HTTP.
    assert not CurrentNotificationState(tmp_path, clock=lambda: clock[0]).claim(KEY, "c" * 64)
    store.finish(KEY, "c" * 64, sent=False)
    assert not store.claim(KEY, "c" * 64)
    clock[0] += TTL_SECONDS + 1
    assert not store.claim(KEY, "c" * 64)  # current stable observation remains current
    assert store.claim(KEY, "d" * 64)


def test_recent_horizon_cap_and_rollback(tmp_path):
    clock = [1000000.0]
    store = CurrentNotificationState(tmp_path, clock=lambda: clock[0])
    for number in range(RECENT_LIMIT + 3):
        clock[0] += 1
        assert store.claim(KEY, format(number, "064x"))
        store.finish(KEY, format(number, "064x"), sent=True)
    value = store.read(KEY)
    assert len(value["recent"]) < RECENT_LIMIT
    assert not store.claim(KEY, format(RECENT_LIMIT, "064x"))
    clock[0] -= 1000  # a wall-clock rollback cannot extend old eligibility
    assert not store.claim(KEY, format(RECENT_LIMIT, "064x"))
    clock[0] = value["checked_at"] + TTL_SECONDS
    assert store.claim(KEY, format(RECENT_LIMIT, "064x"))


def test_point_reads_ignore_historical_map_and_directories(tmp_path, monkeypatch):
    legacy = tmp_path / "notifications" / "last-events.json"
    legacy.parent.mkdir()
    legacy.write_text("bad obsolete cache", encoding="utf-8")
    store = CurrentNotificationState(tmp_path, clock=lambda: 100)
    monkeypatch.setattr(Path, "glob", lambda *args, **kwargs: pytest.fail("history scan"))
    assert store.claim(KEY, "c" * 64)
    assert store.claim(OTHER, "c" * 64)
    assert legacy.read_text() == "bad obsolete cache"
    store.path(KEY).write_text("broken")
    assert store.claim(KEY, "d" * 64)
    assert not store.claim(KEY, "d" * 64)
    assert store.path(KEY).read_text() == "broken"
    assert store.claim(OTHER, "d" * 64)  # corrupt key affects only its own action


def test_atomic_claim_failure_never_grants_external_eligibility(tmp_path, monkeypatch):
    store = CurrentNotificationState(tmp_path, clock=lambda: 100)
    def fail(*args, **kwargs):
        raise OSError("durable ledger unavailable")
    monkeypatch.setattr("codex_watchdog.notification_state.sqlite3.connect", fail)
    with pytest.raises(OSError):
        store.claim(KEY, "c" * 64)
    assert not store.path(KEY).exists()


def test_optional_cache_failure_does_not_make_the_durable_claim_retryable(tmp_path):
    def fail(path, value):
        raise OSError("optional cache unavailable")
    store = CurrentNotificationState(tmp_path, clock=lambda: 100, writer=fail)
    assert store.claim(KEY, "c" * 64)
    assert not store.claim(KEY, "c" * 64)
    assert store.claim(KEY, "d" * 64)


def test_upgrade_import_is_current_only_and_idempotent(tmp_path):
    store = CurrentNotificationState(tmp_path, clock=lambda: 1800000000)
    entry = dict(fingerprint="c" * 64, recorded_at="2026-10-01T10:00:00Z")
    assert store.import_current(KEY, entry)
    before = store.path(KEY).read_bytes()
    assert not store.import_current(KEY, entry)
    assert store.path(KEY).read_bytes() == before
    assert not store.claim(KEY, entry["fingerprint"])


def test_cross_scope_clock_rollback_preserves_actual_claim_time(tmp_path):
    clock = [1000000.0]
    store = CurrentNotificationState(tmp_path,clock=lambda:clock[0])
    assert store.claim(KEY,'c'*64)
    clock[0] -= 100
    assert store.claim(OTHER,'d'*64)
    assert store.claim_time(OTHER,'d'*64) == store.read(OTHER)['checked_at'] == 1000000


def test_quiet_cleanup_is_bounded_and_keeps_current_configuration(tmp_path,monkeypatch):
    clock = [1000000.0]
    store = CurrentNotificationState(tmp_path,clock=lambda:clock[0])
    assert store.claim(KEY,'c'*64,scope='retired-workspace')
    assert store.claim(OTHER,'d'*64,scope='current-workspace')
    clock[0] += TTL_SECONDS
    monkeypatch.setattr(Path,'iterdir',lambda *a:pytest.fail('eager history-sized directory listing'))
    result=store.cleanup(current_scopes={'current-workspace'},budget=1)
    assert result['visited'] <= 1
    for _ in range(3): store.cleanup(current_scopes={'current-workspace'},budget=1)
    assert not store.path(KEY).exists() and store.path(OTHER).exists()
    assert not store.claim(OTHER,'d'*64)  # current stable observation stays suppressed
