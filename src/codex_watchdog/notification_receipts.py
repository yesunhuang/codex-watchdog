"""Indexed dual-send receipts; callers hold the existing notification lock.

The legacy schema-2 marker supports explicit receipt export. Finite schema-3
activation fences old senders and uses compact forward recovery; it cannot be
exported as old sender state.
"""
from contextlib import AbstractContextManager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .storage import FileLock, InstructionStore, StoreBusyError, snapshot_state


class NotificationReceiptError(ValueError):
    """A fixed, nonsecret persistence/rollback error code."""


def _prepared_root(store):
    from .notification_root_retention import read_notification_root
    return read_notification_root(store.directory.parent) is not None


def maintain_observer_receipts(runtime, *, scopes, sender_fence, clock=time.time, budget=32):
    """Current observer projection only; caller supplies its real owner/sender fence.

    Existing finite families are opened rw without create/import/schema repair.
    A refused dual family does not suppress independently prepared fallback.
    This is neither cold activation nor native coverage/ownership evidence.
    """
    if not callable(sender_fence):
        raise NotificationReceiptError("notification_receipts_sender_fence_required")
    from .notification_receipt_retention import PROVIDERS, _scope, OBSERVER_PREFIX
    from .notification_senders import sender_fence as common_sender_fence
    from .observer_receipt_scopes import maintain_observer_scopes
    scopes = tuple(scopes)
    if type(budget) is not int or not 1 <= budget <= 32:
        raise NotificationReceiptError("notification_receipts_observer_budget_invalid")
    for scope in scopes:
        if (not isinstance(scope, (list, tuple)) or len(scope) != 3
                or _scope(scope[0], scope[1]) != tuple(scope[:2])
                or not scope[0].startswith(OBSERVER_PREFIX)
                or scope[2] not in PROVIDERS["dual"] + PROVIDERS["fallback"]):
            raise NotificationReceiptError("notification_receipts_observer_scope_invalid")
    results = {}
    with sender_fence(), common_sender_fence(runtime):
        for family, cls in (("dual", DualDeliveryReceipts), ("fallback", FallbackDeliveryReceipts)):
            store = cls(runtime, clock=clock, require_finite=True)
            try:
                _finite_entry(store, family, require_context=False)
                selected = tuple(scope for scope in scopes if scope[2] in PROVIDERS[family])
                results[family] = maintain_observer_scopes(store.finite, selected, budget=budget)
            except (OSError, ValueError, sqlite3.Error) as exc:
                # History or an unsupported family cannot block another
                # family's current enrollment; preserve the refused bytes.
                results[family] = dict(status="refused", reason=type(exc).__name__)
            finally:
                store.__exit__(None, None, None)
    return results


def _finite_entry(store, family, *, require_context=True):
    """Prepared-only entry: never create/import/repair a legacy receipt store."""
    from .notification_receipt_retention import FRESH_ORIGIN, FiniteReceipts, context, _safe
    if require_context:
        context(store.receipt_context)  # Missing birth/identity refuses before file reads.
    from .notification_root_retention import read_notification_root
    prepared = read_notification_root(store.directory.parent)
    database = _safe(store.database)
    if (database.is_symlink() or not database.is_file()
            or database.resolve() != database):
        raise NotificationReceiptError("notification_receipts_explicit_preparation_required")
    marker = None
    if family == "dual":
        path = _safe(store.path)
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise NotificationReceiptError("notification_receipts_explicit_preparation_required")
        with path.open("rb") as stream:
            original = stream.read(16385)
        if len(original) > 16384:
            raise NotificationReceiptError("notification_receipts_explicit_preparation_required")
        try:
            marker = json.loads(original)
        except (ValueError, UnicodeError):
            raise NotificationReceiptError("notification_receipts_marker_invalid") from None
        if (not isinstance(marker, dict) or type(marker.get("schema_version")) is not int
                or marker["schema_version"] != 3 or marker.get("retention") != "finite-v1"
                or marker.get("storage") != "dual-deliveries.sqlite3"):
            raise NotificationReceiptError("notification_receipts_explicit_preparation_required")
    # rw prevents implicit file/schema creation. FiniteReceipts validates schema,
    # bounded metadata and exact context; no legacy row is read or converted.
    store.db = sqlite3.connect(database.as_uri()+"?mode=rw", uri=True, timeout=1, isolation_level=None)
    try:
        store.db.execute("PRAGMA synchronous=FULL")
        store.finite = FiniteReceipts(store.db, family, receipt_context=store.receipt_context, clock=store.clock)
        if prepared is not None and prepared["plan_sha256"] != store.finite.plan:
            raise NotificationReceiptError("notification_receipts_root_plan_mismatch")
        if marker is not None and marker.get("plan_sha256") != store.finite.plan:
            raise NotificationReceiptError("notification_receipts_marker_invalid")
        store.finite.fresh_namespace = (
            prepared is not None and prepared.get('origin') == FRESH_ORIGIN)
        if (prepared is not None and store.current_key is not None
                and store.finite.context is not None
                and store.finite.context["schema_version"] == 1):
            from .notification_receipt_retention import _digest, _initial_floor_key, _time
            from .notification_state import CurrentNotificationState
            if not _digest(store.current_key):
                raise NotificationReceiptError("notification_receipts_current_key_invalid")
            # The notifier already durably claimed this exact effect. Its
            # root-wide clock survives rewinds across new workspace keys;
            # ticket timestamps and source birth cannot substitute for it.
            checked = CurrentNotificationState(store.directory.parent).claim_time(
                store.current_key, store.finite.context["fingerprint"])
            store.finite.native_enrollment = (
                _time(prepared["floor"]), _time(checked), _initial_floor_key(prepared))
        return store
    except BaseException:
        store.__exit__(None, None, None)
        raise


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
    def __init__(self, runtime, *, atomic_writer=InstructionStore._atomic_json,
                 receipt_context=None, clock=time.time, require_finite=False, current_key=None):
        if type(require_finite) is not bool:
            raise TypeError("notification_receipts_finite_flag_invalid")
        self.require_finite = require_finite or isinstance(receipt_context, dict) and receipt_context.get("schema_version") == 2
        self.directory = Path(runtime) / "notifications"
        self.path = self.directory / "dual-deliveries.json"
        self.database = self.directory / "dual-deliveries.sqlite3"
        self.atomic_writer = atomic_writer
        self.db = None
        self.receipt_context, self.clock, self.finite = receipt_context, clock, None
        self.current_key = current_key

    def _commit(self):
        self.db.commit()

    def _begin(self):
        self.db.execute("BEGIN IMMEDIATE")

    def __enter__(self):
        if self.require_finite or _prepared_root(self):
            return _finite_entry(self, "dual")
        if self.path.is_symlink() or self.database.is_symlink():
            raise NotificationReceiptError("notification_receipts_path_invalid")
        if self.receipt_context is not None and self.path.exists() and self.path.stat().st_size > 16384:
            raise NotificationReceiptError("notification_receipts_explicit_preparation_required")
        original = self.path.read_bytes() if self.path.exists() else None
        try:
            value = json.loads(original) if original is not None else None
        except (ValueError, UnicodeError):
            raise NotificationReceiptError("notification_receipts_legacy_invalid") from None
        if original is not None:
            if isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] in (2, 3):
                if value["schema_version"] == 2:
                    _marker(value)
                elif value.get("storage") != "dual-deliveries.sqlite3" or value.get("retention") != "finite-v1":
                    raise NotificationReceiptError("notification_receipts_marker_invalid")
                if not self.database.exists():
                    raise NotificationReceiptError("notification_receipts_database_missing")
            else:
                _legacy(value)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.database), timeout=1, isolation_level=None)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version == 2:
                from .notification_receipt_retention import FiniteReceipts
                if not isinstance(value, dict) or value.get("schema_version") != 3:
                    raise NotificationReceiptError("notification_receipts_explicit_preparation_required")
                self.finite = FiniteReceipts(self.db, "dual", receipt_context=self.receipt_context, clock=self.clock)
                if value.get("plan_sha256") != self.finite.plan:
                    raise NotificationReceiptError("notification_receipts_marker_invalid")
                return self
            if isinstance(value, dict) and value.get("schema_version") == 3:
                raise NotificationReceiptError("notification_receipts_schema_invalid")
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
        if self.finite is not None:
            return self.finite.get(fingerprint)
        row = self.db.execute("SELECT slack,lark FROM receipts WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row is None:
            return {}
        if any(state is not None and state not in ("uncertain", "sent") for state in row):
            raise NotificationReceiptError("notification_receipts_row_invalid")
        return {provider: state for provider, state in zip(("slack", "lark"), row) if state is not None}

    def claim(self, fingerprint, provider):
        self._fingerprint(fingerprint)
        self._provider(provider)
        if self.finite is not None:
            return self.finite.claim(fingerprint, provider) is None
        try:
            self._begin()
            self.db.execute("INSERT OR IGNORE INTO receipts(fingerprint) VALUES(?)", (fingerprint,))
            changed = self.db.execute("UPDATE receipts SET " + provider + "='uncertain' "
                                      "WHERE fingerprint=? AND " + provider + " IS NULL", (fingerprint,)).rowcount
            self._commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise
        return changed == 1

    def confirm_sent(self, fingerprint, provider):
        self._fingerprint(fingerprint)
        self._provider(provider)
        if self.finite is not None:
            return self.finite.confirm(fingerprint, provider)
        try:
            self._begin()
            changed = self.db.execute("UPDATE receipts SET " + provider + "='sent' "
                                      "WHERE fingerprint=? AND " + provider + "='uncertain'", (fingerprint,)).rowcount
            if changed != 1:
                raise NotificationReceiptError("notification_receipts_confirmation_invalid")
            self._commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise

    def export(self):
        """Offline caller owns foreground-run.lock as well as notifications.lock."""
        if self.finite is not None:
            raise NotificationReceiptError("notification_receipts_forward_recovery_required")
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


class FallbackDeliveryReceipts(AbstractContextManager):
    """Independent one-shot SMTP/desktop claims; existing dual receipts stay intact."""

    def __init__(self, runtime, *, receipt_context=None, clock=time.time, require_finite=False, current_key=None):
        if type(require_finite) is not bool:
            raise TypeError("notification_receipts_finite_flag_invalid")
        self.require_finite = require_finite or isinstance(receipt_context, dict) and receipt_context.get("schema_version") == 2
        self.directory = Path(runtime) / "notifications"
        self.database = self.directory / "fallback-deliveries.sqlite3"
        self.db = None
        self.receipt_context, self.clock, self.finite = receipt_context, clock, None
        self.current_key = current_key

    def __enter__(self):
        if self.require_finite or _prepared_root(self):
            return _finite_entry(self, "fallback")
        if self.database.is_symlink():
            raise NotificationReceiptError("notification_fallback_path_invalid")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.database), timeout=1, isolation_level=None)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version == 2:
                from .notification_receipt_retention import FiniteReceipts
                self.finite = FiniteReceipts(self.db, "fallback", receipt_context=self.receipt_context, clock=self.clock)
                self.db.commit()
                return self
            if version == 0:
                if self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise NotificationReceiptError("notification_fallback_schema_invalid")
                self.db.execute("""CREATE TABLE receipts (
                    fingerprint TEXT NOT NULL,
                    provider TEXT NOT NULL CHECK(provider IN ('smtp','windows_msg')),
                    target_sha256 TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('uncertain','sent')),
                    PRIMARY KEY(fingerprint,provider))""")
                self.db.execute("PRAGMA user_version=1")
            elif version != 1:
                raise NotificationReceiptError("notification_fallback_schema_invalid")
            fields = self.db.execute("PRAGMA table_info(receipts)").fetchall()
            known = {row[1]: row for row in fields}
            if (not {"fingerprint", "provider", "target_sha256", "payload_sha256", "state"} <= known.keys()
                    or [row[1] for row in sorted(fields, key=lambda row: row[5]) if row[5]] != ["fingerprint", "provider"]
                    or any(known[name][2].upper() != "TEXT" or known[name][3] != 1
                           for name in ("fingerprint", "provider", "target_sha256", "payload_sha256", "state"))):
                raise NotificationReceiptError("notification_fallback_schema_invalid")
            self.db.commit()
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

    @staticmethod
    def _identity(fingerprint, provider, target_sha256, payload_sha256):
        for value in (fingerprint, target_sha256, payload_sha256):
            DualDeliveryReceipts._fingerprint(value)
        if provider not in ("smtp", "windows_msg"):
            raise NotificationReceiptError("notification_fallback_provider_invalid")

    def claim(self, fingerprint, provider, target_sha256, payload_sha256):
        """Return prior terminal eligibility, or None for one committed new claim."""
        self._identity(fingerprint, provider, target_sha256, payload_sha256)
        if self.finite is not None:
            return self.finite.claim(fingerprint, provider, target_sha256, payload_sha256)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute("SELECT target_sha256,payload_sha256,state FROM receipts "
                "WHERE fingerprint=? AND provider=?", (fingerprint, provider)).fetchone()
            if previous is not None:
                if previous[:2] != (target_sha256, payload_sha256) or previous[2] not in ("uncertain", "sent"):
                    raise NotificationReceiptError("notification_fallback_identity_collision")
                self.db.commit()
                return previous[2]
            self.db.execute("INSERT INTO receipts(fingerprint,provider,target_sha256,payload_sha256,state) "
                "VALUES(?,?,?,?,'uncertain')", (fingerprint, provider, target_sha256, payload_sha256))
            self.db.commit()  # Durable before the one possible external attempt.
            return None
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise

    def confirm_sent(self, fingerprint, provider):
        DualDeliveryReceipts._fingerprint(fingerprint)
        if provider not in ("smtp", "windows_msg"):
            raise NotificationReceiptError("notification_fallback_provider_invalid")
        if self.finite is not None:
            return self.finite.confirm(fingerprint, provider)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            changed = self.db.execute("UPDATE receipts SET state='sent' WHERE fingerprint=? "
                "AND provider=? AND state='uncertain'", (fingerprint, provider)).rowcount
            if changed != 1:
                raise NotificationReceiptError("notification_fallback_confirmation_invalid")
            self.db.commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise


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
