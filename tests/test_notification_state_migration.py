import json

import pytest

from codex_watchdog.notification_state import CurrentNotificationState
from codex_watchdog.notification_state_migration import migrate_notification_state

KEY = "a" * 64
OLD = "b" * 64


def legacy(tmp_path):
    path = tmp_path / "notifications" / "last-events.json"
    path.parent.mkdir()
    current = dict(fingerprint="c" * 64, recorded_at="2026-10-01T10:00:00Z")
    path.write_text(json.dumps(dict(schema_version=1,last_events={KEY:current,OLD:dict(current,fingerprint="d"*64)})))
    return path, current


def test_explicit_upgrade_retains_current_suppression_without_archiving_history(tmp_path):
    path, entry = legacy(tmp_path)
    result = migrate_notification_state(tmp_path, current_keys=[KEY])
    assert result["current_count"] == 1 and not path.exists()
    backup = json.loads((path.parent / "current-suppression-upgrade.json").read_text())
    assert list(backup["protected_current"]) == [KEY] and OLD not in backup["protected_current"]
    store = CurrentNotificationState(tmp_path)
    assert not store.claim(KEY, entry["fingerprint"])
    assert migrate_notification_state(tmp_path,current_keys=[KEY])["status"] == "unchanged"


def test_crash_during_point_publication_preserves_source_and_recovers_once(tmp_path, monkeypatch):
    path, entry = legacy(tmp_path)
    original = path.read_bytes()
    real = CurrentNotificationState.import_current
    monkeypatch.setattr(CurrentNotificationState, "import_current", lambda *args: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        migrate_notification_state(tmp_path,current_keys=[KEY])
    assert path.read_bytes() == original
    monkeypatch.setattr(CurrentNotificationState,"import_current",real)
    assert migrate_notification_state(tmp_path,current_keys=[KEY])["status"] == "migrated"
    assert not CurrentNotificationState(tmp_path).claim(KEY,entry["fingerprint"])


def test_changed_scope_or_source_is_not_silently_overwritten(tmp_path, monkeypatch):
    path, _ = legacy(tmp_path)
    monkeypatch.setattr(CurrentNotificationState,"import_current",lambda *args: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        migrate_notification_state(tmp_path,current_keys=[KEY])
    path.write_text(path.read_text()+" ")
    with pytest.raises(ValueError,match="legacy_changed"):
        migrate_notification_state(tmp_path,current_keys=[KEY])
    with pytest.raises(ValueError,match="scope_changed"):
        migrate_notification_state(tmp_path,current_keys=[OLD])
