"""Older relocation cannot drop newer one-shot SMTP/desktop claims."""
import hashlib
import sqlite3

import pytest

from codex_watchdog import relay_notification_migration as migration
from codex_watchdog.notification_receipts import FallbackDeliveryReceipts


def pins(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file() and not path.is_symlink()}


@pytest.mark.parametrize("provider", ["smtp", "windows_msg"])
@pytest.mark.parametrize("state", ["uncertain", "sent"])
def test_claimed_fallback_relocation_refused_without_source_or_destination_writes(tmp_path, provider, state):
    source, target = tmp_path / "source", tmp_path / "target"
    with FallbackDeliveryReceipts(source) as receipts:
        assert receipts.claim("1" * 64, provider, "2" * 64, "3" * 64) is None
        if state == "sent":
            receipts.confirm_sent("1" * 64, provider)
    before = pins(source)
    with pytest.raises(migration.RelayNotificationMigrationError, match="fallback_relocation_unsupported"):
        migration.apply_notification_migration(target, [source])
    assert pins(source) == before and not target.exists()


def test_missing_and_valid_empty_ledger_leave_old_import_plan_unchanged(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    old = migration.plan_notification_migration([source])
    with FallbackDeliveryReceipts(source):
        pass
    before = pins(source)
    assert migration.plan_notification_migration([source]) == old
    assert pins(source) == before
    assert migration.apply_notification_migration(tmp_path / "target", [source],
        expected_plan_sha256=old["plan_sha256"])["status"] == "migrated"
    assert pins(source) == before


@pytest.mark.parametrize("bad", ["bytes", "version", "columns", "index", "directory"])
def test_malformed_fallback_ledger_is_preserved_and_refuses_relocation(tmp_path, bad):
    source, target = tmp_path / "source", tmp_path / "target"
    database = source / "notifications/fallback-deliveries.sqlite3"
    database.parent.mkdir(parents=True)
    if bad == "bytes":
        database.write_bytes(b"retained malformed ledger")
    elif bad == "directory":
        database.mkdir()
    else:
        with sqlite3.connect(database) as db:
            db.execute("PRAGMA user_version=" + ("9" if bad == "version" else "1"))
            if bad == "columns":
                db.execute("CREATE TABLE receipts(fingerprint TEXT PRIMARY KEY)")
            else:
                key = "PRIMARY KEY(provider,fingerprint)" if bad == "index" else "PRIMARY KEY(fingerprint,provider)"
                db.execute("CREATE TABLE receipts(fingerprint TEXT NOT NULL,provider TEXT NOT NULL,"
                    "target_sha256 TEXT NOT NULL,payload_sha256 TEXT NOT NULL,state TEXT NOT NULL," + key + ")")
    before = pins(source)
    with pytest.raises(migration.RelayNotificationMigrationError, match="fallback_source_invalid"):
        migration.apply_notification_migration(target, [source])
    assert pins(source) == before and not target.exists()


def test_aliased_fallback_ledger_is_not_followed(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    original = tmp_path / "original"
    with FallbackDeliveryReceipts(original):
        pass
    directory = source / "notifications"
    directory.mkdir(parents=True)
    link = directory / "fallback-deliveries.sqlite3"
    try:
        link.symlink_to(original / "notifications/fallback-deliveries.sqlite3")
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Native Windows symlink privilege unavailable")
        raise
    before = pins(original)
    with pytest.raises(migration.RelayNotificationMigrationError, match="fallback_source_invalid"):
        migration.apply_notification_migration(target, [source])
    assert link.is_symlink() and pins(original) == before and not target.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_orphaned_fallback_database_evidence_is_not_treated_as_missing(tmp_path, suffix):
    source, target = tmp_path / "source", tmp_path / "target"
    directory = source / "notifications"
    directory.mkdir(parents=True)
    (directory / ("fallback-deliveries.sqlite3" + suffix)).write_bytes(b"retained orphaned ledger evidence")
    before = pins(source)
    with pytest.raises(migration.RelayNotificationMigrationError, match="fallback_source_invalid"):
        migration.apply_notification_migration(target, [source])
    assert pins(source) == before and not target.exists()


@pytest.mark.parametrize("history", [0, 10000])
def test_fallback_presence_guard_uses_one_indexed_seek_without_history_reads(tmp_path, history, monkeypatch):
    source = tmp_path / "source"
    with FallbackDeliveryReceipts(source) as receipts:
        receipts.db.executemany("INSERT INTO receipts VALUES(?,?,?,?,?)", (
            (hashlib.sha256(str(number).encode()).hexdigest(), "smtp", "2" * 64, "3" * 64, "uncertain")
            for number in range(history)))
        plan = receipts.db.execute("EXPLAIN QUERY PLAN SELECT 1 FROM receipts "
            "INDEXED BY sqlite_autoindex_receipts_1 WHERE fingerprint>=? LIMIT 1", ("",)).fetchall()
    connect = sqlite3.connect
    statements = []
    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(migration.sqlite3, "connect", traced)
    if history:
        with pytest.raises(migration.RelayNotificationMigrationError, match="fallback_relocation_unsupported"):
            migration.plan_notification_migration([source])
    else:
        migration.plan_notification_migration([source])
    assert "SEARCH" in str(plan) and "INDEX" in str(plan) and "SCAN" not in str(plan)
    reads = [statement for statement in statements if statement.startswith("SELECT")]
    assert reads == ["SELECT 1 FROM receipts INDEXED BY sqlite_autoindex_receipts_1 WHERE fingerprint>='' LIMIT 1"]
    assert len(statements) == 6
