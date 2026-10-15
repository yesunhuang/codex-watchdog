"""Actual cold root entry retires only recognized disposable importer copies."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from codex_watchdog import notification_root_retention as root
from codex_watchdog import relay_notification_migration as migration
from codex_watchdog.notification_receipts import DualDeliveryReceipts
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
from codex_watchdog.storage import StoreBusyError
from test_notification_root_integration import event
from test_notification_root_retention import NOW, SID, scopes, SENTINELS
from test_notification_sender_integration import busy


def imported(tmp_path):
    source, selected = tmp_path / "original", tmp_path / "selected"
    directory = source / "notifications"
    directory.mkdir(parents=True)
    value = dict(schema_version=1, last_events={"a"*64:dict(fingerprint="b"*64, recorded_at="2026-10-01T00:00:00Z")})
    (directory/"last-events.json").write_text(json.dumps(value))
    for name in ("last-events.json", "dual-deliveries.json"):
        if name.startswith("dual"):
            (directory/name).write_text(json.dumps(dict(schema_version=1, events={"c"*64:dict(slack="uncertain")})))
        (directory/(name+".relay-authority-v1-backup")).write_bytes((directory/name).read_bytes())
    with DualDeliveryReceipts(source):
        pass
    migration.apply_notification_migration(selected, [source])
    copies = selected/"notifications/source-evidence"/hashlib.sha256(str(source).encode()).hexdigest()
    original = {p.relative_to(source):p.read_bytes() for p in source.rglob("*") if p.is_file()}
    for name, raw in SENTINELS.items():
        path = selected/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return source, selected, copies, original


def prepare(selected, fence=None):
    if fence is None:
        @contextmanager
        def fence():
            yield
    return root.prepare_notification_root(selected, scopes=scopes(), current_keys=[],
        current_pins=(), sender_fence=fence, clock=lambda:NOW)


def verify_protected(source, selected, original):
    assert {p.relative_to(source):p.read_bytes() for p in source.rglob("*") if p.is_file()} == original
    assert all((selected/name).read_bytes() == raw for name,raw in SENTINELS.items())


def test_actual_cutover_retires_copies_only_after_ready_and_under_all_fences(tmp_path, monkeypatch):
    source, selected, copies, original = imported(tmp_path)
    old = {path:path.read_bytes() for path in copies.iterdir()}
    assert len(old) == 5
    entered = [False]
    @contextmanager
    def fence():
        entered[0] = True
        try: yield
        finally: entered[0] = False
    unlink = Path.unlink
    def checked_unlink(path, *args, **kwargs):
        if path in old:
            assert entered[0] and root.validate_notification_root(selected) is not None
            busy(selected)
            from codex_watchdog.storage import FileLock
            with pytest.raises(StoreBusyError):
                with FileLock(selected/"locks/notifications.lock"):
                    pass
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", checked_unlink)
    result = prepare(selected, fence)
    assert result["copied_history"] == dict(removed=5, removed_bytes=sum(map(len,old.values())), preserved=0)
    assert not copies.parent.exists()
    again = prepare(selected)
    assert again["floor"] == result["floor"] and again["plan_sha256"] == result["plan_sha256"]
    assert again["copied_history"] == dict(removed=0, removed_bytes=0, preserved=0)
    verify_protected(source, selected, original)


@pytest.mark.parametrize("damage", ["unknown-file", "unknown-root-field", "malformed", "unknown-sql-table", "busy-sidecar"])
def test_optional_copy_refusal_preserves_bytes_without_blocking_root_or_later_output(tmp_path,damage):
    source, selected, copies, original = imported(tmp_path)
    path = copies/"dual-deliveries.json"
    if damage == "unknown-file":
        path = copies/"operator-notes.txt";path.write_bytes(b"not disposable history")
    elif damage == "unknown-root-field":
        value = json.loads(path.read_bytes());value["operator_setting"] = "preserve"
        path.write_text(json.dumps(value))
    elif damage == "malformed":
        path.write_bytes(b"{broken")
    else:
        path = copies/"dual-deliveries.sqlite3"
        if damage == "unknown-sql-table":
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE operator_data(value TEXT)")
        else:
            Path(str(path)+"-journal").write_bytes(b"busy sentinel")
    raw = path.read_bytes()
    result = prepare(selected)
    assert result["status"] == "ready" and result["copied_history"]["preserved"] >= 1
    assert path.read_bytes() == raw
    assert result["copied_history"]["removed"] >= 3
    calls=[]
    def post(token,method,payload,timeout):
        calls.append(payload);return dict(channel=payload["channel"],ts="1792008000.000001")
    # Root-integration fixture uses another scope; this explicit current native
    # scope matches the notifier's actual event rather than inventing a pin.
    from codex_watchdog.models import sha256_text
    root.prepare_notification_root(selected, scopes=[(SID,sha256_text("fixture"),"slack")],
        current_keys=[],current_pins=(),sender_fence=nullcontext,clock=lambda:NOW)
    notifier=EnvironmentNotifier(selected,NotificationConfig(slack_bot_token="xoxb-fixture",slack_channel_id="C12345678"),
        slack_api_post=post,clock=lambda:NOW+1)
    assert notifier.notify(event()).status == "sent" and len(calls) == 1
    assert notifier.notify(event()).status == "suppressed" and len(calls) == 1
    assert path.read_bytes() == raw
    verify_protected(source, selected, original)


def test_failed_unlink_is_retained_and_forward_cleanup_can_resume(tmp_path,monkeypatch):
    source, selected, copies, original = imported(tmp_path)
    retained=copies/"last-events.json"
    raw=retained.read_bytes()
    unlink=Path.unlink
    def interrupted(path,*args,**kwargs):
        if path == retained:raise PermissionError("owned disposable fixture only")
        return unlink(path,*args,**kwargs)
    monkeypatch.setattr(Path,"unlink",interrupted)
    first=prepare(selected)
    assert first["copied_history"]["removed"] == 4 and first["copied_history"]["preserved"] == 1
    assert retained.read_bytes() == raw
    monkeypatch.setattr(Path,"unlink",unlink)
    second=prepare(selected)
    assert second["floor"] == first["floor"] and second["plan_sha256"] == first["plan_sha256"]
    assert second["copied_history"] == dict(removed=1,removed_bytes=len(raw),preserved=0)
    assert not copies.parent.exists()
    verify_protected(source, selected, original)


def test_copy_changed_during_recognition_and_unknown_namespace_are_preserved(tmp_path,monkeypatch):
    from codex_watchdog import notification_legacy_cleanup as cleanup
    source, selected, copies, original = imported(tmp_path)
    retained=copies/"last-events.json"
    unknown=copies.parent/"operator-backup";unknown.mkdir()
    private=unknown/"last-events.json";private.write_bytes(retained.read_bytes())
    private_raw=private.read_bytes()
    recognize=cleanup._json_copy
    def changing(path,name):
        result=recognize(path,name)
        if path == retained:
            path.write_bytes(path.read_bytes()+b" ")
        return result
    monkeypatch.setattr(cleanup,"_json_copy",changing)
    result=prepare(selected)
    assert result["copied_history"]["removed"] == 4 and result["copied_history"]["preserved"] == 2
    assert retained.read_bytes().endswith(b" ") and private.read_bytes() == private_raw
    verify_protected(source, selected, original)


def test_integer_sqlite_marker_is_optional_refusal_with_ready_and_copy_unchanged(tmp_path):
    # C344's original actual-root counterexample: names match, but marker_value
    # is BLOB affinity with integer 7. Keep this unsupported copy, not a repair.
    selected=tmp_path/"selected";selected.mkdir()
    initial=prepare(selected)
    ready=selected/"notifications"/root.READY;ready_bytes=ready.read_bytes()
    copies=selected/"notifications/source-evidence"/hashlib.sha256(b"synthetic-source-runtime").hexdigest()
    copies.mkdir(parents=True)
    copied=copies/"dual-deliveries.sqlite3"
    with sqlite3.connect(copied) as db:
        db.execute("CREATE TABLE receipts(fingerprint TEXT,slack TEXT,lark TEXT)")
        db.execute("CREATE TABLE metadata(id INTEGER PRIMARY KEY,source_sha256 TEXT,source_present INTEGER,activated INTEGER,marker_value BLOB,export_sha256 TEXT)")
        db.execute("INSERT INTO metadata VALUES(1,?,0,1,7,NULL)",("a"*64,))
        db.execute("PRAGMA user_version=1")
        assert db.execute("SELECT typeof(marker_value),marker_value FROM metadata").fetchone() == ("integer",7)
    original=copied.read_bytes()
    result=prepare(selected)
    assert result["status"] == "ready" and result["floor"] == initial["floor"]
    assert result["plan_sha256"] == initial["plan_sha256"]
    assert result["copied_history"] == dict(removed=0,removed_bytes=0,preserved=1)
    assert copied.read_bytes() == original and ready.read_bytes() == ready_bytes


@pytest.mark.parametrize("link", ["hardlink", "symlink"])
def test_links_never_authorize_deleting_target_or_entry(tmp_path,link):
    source, selected, copies, original = imported(tmp_path)
    path=copies/"last-events.json"
    raw=path.read_bytes();outside=tmp_path/"user-data.json";outside.write_bytes(raw)
    path.unlink()
    try:
        if link == "hardlink":os.link(outside,path)
        else:path.symlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not permit this link")
    result=prepare(selected)
    assert result["copied_history"]["preserved"] >= 1 and path.read_bytes() == outside.read_bytes() == raw
    assert path.exists()
    verify_protected(source, selected, original)
