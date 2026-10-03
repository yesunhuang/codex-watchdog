"""Indexed dual-send receipts; callers hold the existing notification lock.

The schema-2 JSON marker fences old binaries. Offline rollback exports every
current receipt, not the pre-migration snapshot, before an old sender can run.
"""
from contextlib import AbstractContextManager
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .storage import FileLock, InstructionStore, StoreBusyError, snapshot_state


class NotificationReceiptError(ValueError):
    """A fixed, nonsecret persistence/rollback error code."""


def _legacy(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "events"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["events"], dict)):
        raise NotificationReceiptError("notification_receipts_legacy_invalid")
    for fingerprint, entry in value["events"].items():
        if (not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                or not isinstance(entry, dict) or not set(entry) <= {"slack", "lark"}
                or any(state not in ("uncertain", "sent") for state in entry.values())):
            raise NotificationReceiptError("notification_receipts_legacy_invalid")
    return value["events"]


def _marker(value):
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 2 or value.get("storage") != "dual-deliveries.sqlite3"):
        raise NotificationReceiptError("notification_receipts_marker_invalid")
    return value  # Retain unknown nonconflicting fields without rewriting them.


def _payload(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class DualDeliveryReceipts(AbstractContextManager):
    def __init__(self, runtime, *, atomic_writer=InstructionStore._atomic_json):
        self.directory = Path(runtime) / "notifications"
        self.path = self.directory / "dual-deliveries.json"
        self.database = self.directory / "dual-deliveries.sqlite3"
        self.atomic_writer = atomic_writer
        self.db = None

    def _commit(self):
        self.db.commit()

    def _begin(self):
        self.db.execute("BEGIN IMMEDIATE")

    def __enter__(self):
        if self.path.is_symlink() or self.database.is_symlink():
            raise NotificationReceiptError("notification_receipts_path_invalid")
        original = self.path.read_bytes() if self.path.exists() else None
        try:
            value = json.loads(original) if original is not None else None
        except (ValueError, UnicodeError):
            raise NotificationReceiptError("notification_receipts_legacy_invalid") from None
        if original is not None:
            if isinstance(value, dict) and value.get("schema_version") == 2:
                _marker(value)
                if not self.database.exists():
                    raise NotificationReceiptError("notification_receipts_database_missing")
            else:
                _legacy(value)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.database), timeout=1, isolation_level=None)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise NotificationReceiptError("notification_receipts_schema_invalid")
            if version == 0:
                if value is not None and value.get("schema_version") == 2:
                    raise NotificationReceiptError("notification_receipts_database_invalid")
                if self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise NotificationReceiptError("notification_receipts_database_invalid")
                self._begin()
                self.db.execute("""CREATE TABLE receipts (
                    fingerprint TEXT NOT NULL PRIMARY KEY
                        CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
                    slack TEXT CHECK(slack IS NULL OR slack IN ('uncertain','sent')),
                    lark TEXT CHECK(lark IS NULL OR lark IN ('uncertain','sent')))""")
                self.db.execute("""CREATE TABLE metadata (
                    id INTEGER PRIMARY KEY CHECK(id=1), source_sha256 TEXT NOT NULL,
                    source_present INTEGER NOT NULL CHECK(source_present IN (0,1)),
                    activated INTEGER NOT NULL CHECK(activated IN (0,1)),
                    marker_value TEXT NOT NULL, export_sha256 TEXT)""")
                self.db.execute("PRAGMA user_version=1")
                # Schema creation and initial import are one transaction.
            meta = self.db.execute("SELECT source_sha256,source_present,activated,marker_value "
                                   "FROM metadata WHERE id=1").fetchone()
            if meta is not None:
                if (not isinstance(meta[0], str) or re.fullmatch(r"[0-9a-f]{64}", meta[0]) is None
                        or type(meta[1]) is not int or meta[1] not in (0, 1)
                        or type(meta[2]) is not int or meta[2] not in (0, 1)):
                    raise NotificationReceiptError("notification_receipts_database_invalid")
                _marker(json.loads(meta[3]))
            if value is not None and value.get("schema_version") == 2:
                if meta is None:
                    raise NotificationReceiptError("notification_receipts_database_invalid")
                if not meta[2]:
                    self._activate()
            elif value is not None:
                self._import(value, original, meta)
            elif meta is None:
                self._import({"schema_version": 1, "events": {}}, None, None)
            elif not meta[2] and not meta[1]:
                # Fresh creation committed before its marker, without any send.
                self.atomic_writer(self.path, _marker(json.loads(meta[3])))
                self._activate()
            else:
                raise NotificationReceiptError("notification_receipts_marker_missing")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.db is not None:
            try:
                if self.db.in_transaction:
                    self.db.rollback()
            finally:
                self.db.close()
                self.db = None

    def _activate(self):
        self._begin()
        self.db.execute("UPDATE metadata SET activated=1 WHERE id=1")
        self._commit()

    def _import(self, value, original, meta):
        events = _legacy(value)
        if original is not None:
            snapshot_state(self.path, original)
        if not self.db.in_transaction:
            self._begin()
        if meta is not None:
            # Rollback may append legitimate old-version receipts. It may not
            # discard or downgrade evidence retained by the new store.
            for fingerprint, slack, lark in self.db.execute("SELECT fingerprint,slack,lark FROM receipts"):
                incoming = events.get(fingerprint)
                if incoming is None or any(previous is not None and (
                        incoming.get(provider) is None or previous == "sent" and incoming.get(provider) != "sent")
                        for provider, previous in (("slack", slack), ("lark", lark))):
                    raise NotificationReceiptError("notification_receipts_reconcile_loss")
        for fingerprint, entry in events.items():
            self.db.execute("""INSERT INTO receipts VALUES (?,?,?) ON CONFLICT(fingerprint)
                DO UPDATE SET slack=excluded.slack,lark=excluded.lark""",
                (fingerprint, entry.get("slack"), entry.get("lark")))
        marker = (_marker(json.loads(meta[3])) if meta is not None else
                  {"schema_version": 2, "storage": "dual-deliveries.sqlite3"})
        digest = hashlib.sha256(original if original is not None else _payload(value)).hexdigest()
        self.db.execute("""INSERT INTO metadata(id,source_sha256,source_present,activated,marker_value)
            VALUES(1,?,?,0,?) ON CONFLICT(id) DO UPDATE SET source_sha256=excluded.source_sha256,
            source_present=excluded.source_present,activated=0""",
            (digest, int(original is not None), json.dumps(marker, sort_keys=True)))
        self._commit()  # Import durable before marker; no provider send yet.
        self.atomic_writer(self.path, marker)
        self._activate()

    @staticmethod
    def _fingerprint(fingerprint):
        if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise NotificationReceiptError("notification_receipts_fingerprint_invalid")

    @staticmethod
    def _provider(provider):
        if provider not in ("slack", "lark"):
            raise NotificationReceiptError("notification_receipts_provider_invalid")

    def get(self, fingerprint):
        self._fingerprint(fingerprint)
        row = self.db.execute("SELECT slack,lark FROM receipts WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row is None:
            return {}
        if any(state is not None and state not in ("uncertain", "sent") for state in row):
            raise NotificationReceiptError("notification_receipts_row_invalid")
        return {provider: state for provider, state in zip(("slack", "lark"), row) if state is not None}

    def claim(self, fingerprint, provider):
        self._fingerprint(fingerprint)
        self._provider(provider)
        self._begin()
        self.db.execute("INSERT OR IGNORE INTO receipts(fingerprint) VALUES(?)", (fingerprint,))
        changed = self.db.execute("UPDATE receipts SET " + provider + "='uncertain' "
                                  "WHERE fingerprint=? AND " + provider + " IS NULL", (fingerprint,)).rowcount
        self._commit()
        return changed == 1

    def confirm_sent(self, fingerprint, provider):
        self._fingerprint(fingerprint)
        self._provider(provider)
        self._begin()
        changed = self.db.execute("UPDATE receipts SET " + provider + "='sent' "
                                  "WHERE fingerprint=? AND " + provider + "='uncertain'", (fingerprint,)).rowcount
        if changed != 1:
            raise NotificationReceiptError("notification_receipts_confirmation_invalid")
        self._commit()

    def export(self):
        """Offline caller owns foreground-run.lock as well as notifications.lock."""
        marker_bytes = self.path.read_bytes()
        marker = _marker(json.loads(marker_bytes))
        snapshot_state(self.path, marker_bytes)
        self._begin()
        events = {}
        for fingerprint, slack, lark in self.db.execute("SELECT fingerprint,slack,lark FROM receipts"):
            events[fingerprint] = {provider: state for provider, state in (("slack", slack), ("lark", lark))
                                   if state is not None}
        value = {"schema_version": 1, "events": events}
        _legacy(value)
        digest = hashlib.sha256(_payload(value)).hexdigest()
        self.db.execute("UPDATE metadata SET marker_value=?,export_sha256=? WHERE id=1",
                        (json.dumps(marker, sort_keys=True), digest))
        self._commit()
        self.atomic_writer(self.path, value)
        return {"status": "exported", "schema_version": 1, "receipt_count": len(events),
                "receipt_sha256": digest}


def export_legacy(runtime, *, atomic_writer=InstructionStore._atomic_json):
    """Export a stopped runtime without loading configuration or credentials."""
    runtime = Path(runtime)
    if not runtime.is_dir():
        raise NotificationReceiptError("notification_receipts_runtime_missing")
    try:
        with FileLock(runtime / "locks" / "foreground-run.lock"):
            try:
                with FileLock(runtime / "locks" / "notifications.lock"):
                    path = runtime / "notifications" / "dual-deliveries.json"
                    database = runtime / "notifications" / "dual-deliveries.sqlite3"
                    if path.is_symlink() or database.is_symlink():
                        raise NotificationReceiptError("notification_receipts_path_invalid")
                    if not path.exists() and not database.exists():
                        return {"status": "no_receipts", "schema_version": 1, "receipt_count": 0}
                    if not database.exists() and path.exists() and not path.is_symlink():
                        try:
                            value = json.loads(path.read_bytes())
                        except (ValueError, UnicodeError):
                            raise NotificationReceiptError("notification_receipts_legacy_invalid") from None
                        events = _legacy(value)
                        return {"status": "already_legacy", "schema_version": 1,
                                "receipt_count": len(events), "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    with DualDeliveryReceipts(runtime, atomic_writer=atomic_writer) as receipts:
                        return receipts.export()
            except StoreBusyError:
                raise NotificationReceiptError("notification_receipts_lock_held") from None
    except StoreBusyError:
        raise NotificationReceiptError("foreground_run_lock_held") from None
