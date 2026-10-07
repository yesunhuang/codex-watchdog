"""Offline import of notification evidence into an exact session's runtime.

The caller fences every old sender before planning/applying. This helper never
contacts a provider, chooses an execution node, or replays a notification.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from .notification_receipts import DualDeliveryReceipts
from .storage import InstructionStore


class RelayNotificationMigrationError(ValueError):
    pass


def _payload(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return hashlib.sha256(_payload(value)).hexdigest()


def _fingerprint(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _safe(path):
    if path.is_symlink() or path.resolve() != path:
        raise RelayNotificationMigrationError("relay_notification_source_path_invalid")


def _runtime(path):
    path = Path(path).expanduser().absolute()
    _safe(path)
    return path


def _json(path, *, unwrap_refusal=True):
    _safe(path)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        raise RelayNotificationMigrationError("relay_notification_source_invalid") from None
    if not isinstance(value, dict):
        raise RelayNotificationMigrationError("relay_notification_source_invalid")
    # An interrupted cluster migration reads the retained original, not a
    # refusal marker inserted to stop old senders from reopening their state.
    if unwrap_refusal and value.get("schema_version") == 3 and value.get("storage") == "cluster_provider_journals_v1":
        backup = path.with_name(path.name + ".relay-authority-v1-backup")
        _safe(backup)
        if not backup.exists():
            raise RelayNotificationMigrationError("relay_notification_backup_missing")
        try:
            value = json.loads(backup.read_bytes())
        except (ValueError, UnicodeError):
            raise RelayNotificationMigrationError("relay_notification_source_invalid") from None
        if not isinstance(value, dict):
            raise RelayNotificationMigrationError("relay_notification_source_invalid")
    return value


def _dual_rows(directory, marker):
    database = directory / "dual-deliveries.sqlite3"
    _safe(database)
    if marker is None and not database.exists():
        return {}, None
    if marker is not None and type(marker.get("schema_version")) is not int:
        raise RelayNotificationMigrationError("relay_notification_dual_invalid")
    if marker is not None and marker.get("schema_version") == 1:
        if not isinstance(marker.get("events"), dict):
            raise RelayNotificationMigrationError("relay_notification_dual_invalid")
        rows = marker["events"]
        database_digest = None
        if database.exists():
            # Both stores present after rollback: retain all durable claims.
            persisted, database_digest = _dual_rows(directory, {"schema_version": 2, "storage": "dual-deliveries.sqlite3"})
            rows = _merge_dual((rows, persisted))
        return rows, database_digest
    if marker is not None and (type(marker.get("schema_version")) is not int
            or marker["schema_version"] != 2 or marker.get("storage") != "dual-deliveries.sqlite3"):
        raise RelayNotificationMigrationError("relay_notification_dual_invalid")
    if not database.exists():
        raise RelayNotificationMigrationError("relay_notification_dual_database_missing")
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise RelayNotificationMigrationError("relay_notification_dual_schema_invalid")
            rows = {fingerprint: {provider: state for provider, state in (("slack", slack), ("lark", lark))
                                  if state is not None}
                    for fingerprint, slack, lark in db.execute("SELECT fingerprint,slack,lark FROM receipts")}
            metadata = db.execute("SELECT * FROM metadata ORDER BY id").fetchall()
            if (len(metadata) != 1 or len(metadata[0]) < 6 or metadata[0][0] != 1
                    or not _fingerprint(metadata[0][1]) or metadata[0][2] not in (0, 1)
                    or metadata[0][3] not in (0, 1)):
                raise RelayNotificationMigrationError("relay_notification_dual_database_invalid")
            try:
                saved_marker = json.loads(metadata[0][4])
            except (ValueError, TypeError):
                raise RelayNotificationMigrationError("relay_notification_dual_database_invalid") from None
            if (not isinstance(saved_marker, dict) or type(saved_marker.get("schema_version")) is not int
                    or saved_marker["schema_version"] != 2 or saved_marker.get("storage") != "dual-deliveries.sqlite3"):
                raise RelayNotificationMigrationError("relay_notification_dual_database_invalid")
            digest = hashlib.sha256()
            # Offline only: include unknown tables/fields in the frozen-source
            # discriminator as well as retaining them in the SQLite snapshot.
            for statement in db.iterdump():
                digest.update(statement.encode("utf-8"))
                digest.update(b"\n")
    except sqlite3.Error:
        raise RelayNotificationMigrationError("relay_notification_dual_database_invalid") from None
    return rows, digest.hexdigest()


def _merge_dual(copies):
    result = {}
    for rows in copies:
        for fingerprint, values in rows.items():
            if (not _fingerprint(fingerprint) or not isinstance(values, dict)
                    or not set(values) <= {"slack", "lark"}
                    or any(value not in ("sent", "uncertain") for value in values.values())):
                raise RelayNotificationMigrationError("relay_notification_dual_invalid")
            current = result.setdefault(fingerprint, {})
            for provider, value in values.items():
                # Either retained state prevents another send. Positive provider
                # confirmation is stronger evidence than an uncertain attempt.
                if current.get(provider) != "sent":
                    current[provider] = value
    return result


def plan_notification_migration(source_runtimes):
    """Read-only plan; payload stays private, summary contains hashes/counts."""
    sources, history, copies, marker_fields = [], {}, [], {}
    for runtime in sorted({_runtime(path) for path in source_runtimes}, key=str):
        directory = runtime / "notifications"
        _safe(directory)
        state = _json(directory / "last-events.json")
        marker = _json(directory / "dual-deliveries.json")
        if state is not None:
            if (type(state.get("schema_version")) is not int or state["schema_version"] != 1
                    or not isinstance(state.get("last_events"), dict)):
                raise RelayNotificationMigrationError("relay_notification_last_events_invalid")
            for key, entry in state["last_events"].items():
                if (not _fingerprint(key) or not isinstance(entry, dict)
                        or not _fingerprint(entry.get("fingerprint"))
                        or not isinstance(entry.get("recorded_at"), str) or not entry["recorded_at"]):
                    raise RelayNotificationMigrationError("relay_notification_last_events_invalid")
                item = dict(key=key, entry=entry)
                history[_digest(item)] = item  # Preserve conflicting/unknown evidence, not just one latest row.
        rows, database_digest = _dual_rows(directory, marker)
        copies.append(rows)
        if marker is not None and marker.get("schema_version") == 2:
            for key, value in marker.items():
                if key in ("schema_version", "storage"):
                    continue
                if key in marker_fields and marker_fields[key] != value:
                    raise RelayNotificationMigrationError("relay_notification_marker_field_conflict")
                marker_fields[key] = value
        sources.append(dict(runtime=str(runtime), last_events_sha256=_digest(state),
                            dual_marker_sha256=_digest(marker), dual_database_sha256=database_digest))
    # Every imported event, including conflicting histories, stays in the
    # indexed union. Do not place node-wide historical session keys back into
    # the JSON state that the notifier reads on every new notification.
    merged = _merge_dual(copies)
    result = dict(schema_version=1, sources=sources, history=[history[key] for key in sorted(history)],
                  last_events=dict(schema_version=1, last_events={}), dual_events=merged,
                  dual_marker_fields=marker_fields)
    result["plan_sha256"] = _digest(result)
    result["summary"] = dict(source_count=len(sources), history_count=len(history),
                             dual_count=len(merged), plan_sha256=result["plan_sha256"])
    return result


def apply_notification_migration(target_runtime, source_runtimes, *, expected_plan_sha256=None):
    """Publish complete imported evidence atomically; old senders must be stopped."""
    source_runtimes = tuple(source_runtimes)
    target = _runtime(target_runtime)
    directory = target / "notifications"
    _safe(directory)
    plan = plan_notification_migration(source_runtimes)
    if expected_plan_sha256 is not None and expected_plan_sha256 != plan["plan_sha256"]:
        raise RelayNotificationMigrationError("relay_notification_source_changed")
    receipt = directory / "relay-migration.json"
    if receipt.exists():
        validate_notification_migration(target, plan["plan_sha256"])
        return dict(status="already_migrated", **plan["summary"])
    if directory.exists() and any(directory.iterdir()):
        raise RelayNotificationMigrationError("relay_notification_target_not_empty")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".relay-notifications-", dir=target))
    try:
        notification_dir = stage / "notifications"
        notification_dir.mkdir(mode=0o700)
        evidence = notification_dir / "source-evidence"
        evidence.mkdir(mode=0o700)
        for source in plan["sources"]:
            runtime = Path(source["runtime"])
            destination = evidence / hashlib.sha256(str(runtime).encode()).hexdigest()
            destination.mkdir(mode=0o700)
            for name in ("last-events.json", "dual-deliveries.json"):
                path = runtime / "notifications" / name
                if path.exists():
                    _safe(path)
                    shutil.copyfile(path, destination / name)
                    (destination / name).chmod(0o600)
                    backup = path.with_name(path.name + ".relay-authority-v1-backup")
                    if backup.exists():
                        _safe(backup)
                        shutil.copyfile(backup, destination / backup.name)
                        (destination / backup.name).chmod(0o600)
            database = runtime / "notifications" / "dual-deliveries.sqlite3"
            if database.exists():
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    with closing(sqlite3.connect(str(destination / database.name))) as snapshot:
                        db.backup(snapshot)
                (destination / database.name).chmod(0o600)
        InstructionStore._atomic_json(notification_dir / "last-events.json", plan["last_events"])
        InstructionStore._atomic_json(notification_dir / "dual-deliveries.json",
                                      dict(schema_version=1, events=plan["dual_events"]))
        with DualDeliveryReceipts(stage) as receipts:
            if plan["dual_marker_fields"]:
                value = dict(schema_version=2, storage="dual-deliveries.sqlite3", **plan["dual_marker_fields"])
                receipts.db.execute("UPDATE metadata SET marker_value=? WHERE id=1", (json.dumps(value, sort_keys=True),))
                InstructionStore._atomic_json(receipts.path, value)
        (notification_dir / "dual-deliveries.sqlite3").chmod(0o600)
        history_database = notification_dir / "relay-migration-history.sqlite3"
        with closing(sqlite3.connect(str(history_database))) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE history(fingerprint TEXT NOT NULL, evidence_sha256 TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE INDEX notification_history_lookup ON history(fingerprint)")
            for item in plan["history"]:
                db.execute("INSERT INTO history VALUES(?,?,?)", (item["entry"]["fingerprint"], _digest(item), _payload(item).decode()))
            db.execute("PRAGMA user_version=1")
            db.commit()
        history_database.chmod(0o600)
        InstructionStore._atomic_json(notification_dir / "relay-migration.json",
            dict(schema_version=1, state="ready", storage="relay_notification_history_v1", **plan["summary"]))
        if plan_notification_migration(source_runtimes)["plan_sha256"] != plan["plan_sha256"]:
            raise RelayNotificationMigrationError("relay_notification_source_changed")
        _sync_tree(notification_dir)
        os.replace(notification_dir, directory)
        _sync_directory(target)
        return dict(status="migrated", **plan["summary"])
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _sync_directory(directory):
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sync_tree(directory):
    for root, _, files in os.walk(directory, topdown=False):
        for name in files:
            # Windows FlushFileBuffers requires a writable handle. These are
            # our private staged copies, never the retained source files.
            with open(Path(root) / name, "r+b") as stream:
                os.fsync(stream.fileno())
        _sync_directory(root)


def _history_lookup(directory, fingerprint):
    receipt, database = directory / "relay-migration.json", directory / "relay-migration-history.sqlite3"
    _safe(receipt)
    _safe(database)
    value = _json(receipt, unwrap_refusal=False)
    if (value is None or type(value.get("schema_version")) is not int or value["schema_version"] != 1 or value.get("state") != "ready"
            or value.get("storage") != "relay_notification_history_v1" or not database.exists()):
        raise RelayNotificationMigrationError("relay_notification_migration_incomplete")
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise RelayNotificationMigrationError("relay_notification_history_schema_invalid")
            return db.execute("SELECT 1 FROM history INDEXED BY notification_history_lookup "
                              "WHERE fingerprint=? LIMIT 1", (fingerprint,)).fetchone() is not None
    except sqlite3.Error:
        raise RelayNotificationMigrationError("relay_notification_history_unavailable") from None


def validate_notification_migration(runtime, expected_plan_sha256=None):
    """Validate installed readiness with indexed reads, never create or send."""
    directory = _runtime(runtime) / "notifications"
    _safe(directory)
    for name in ("relay-migration.json", "last-events.json", "dual-deliveries.json",
                 "dual-deliveries.sqlite3", "relay-migration-history.sqlite3"):
        path = directory / name
        _safe(path)
        if not path.is_file():
            raise RelayNotificationMigrationError("relay_notification_migration_incomplete")
    value = _json(directory / "relay-migration.json", unwrap_refusal=False)
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("state") != "ready" or value.get("storage") != "relay_notification_history_v1"
            or not _fingerprint(value.get("plan_sha256"))
            or any(type(value.get(key)) is not int or value[key] < 0
                   for key in ("source_count", "history_count", "dual_count"))
            or expected_plan_sha256 is not None and value["plan_sha256"] != expected_plan_sha256):
        raise RelayNotificationMigrationError("relay_notification_existing_migration_conflict")
    marker = _json(directory / "dual-deliveries.json", unwrap_refusal=False)
    if (type(marker.get("schema_version")) is not int or marker["schema_version"] != 2
            or marker.get("storage") != "dual-deliveries.sqlite3"):
        raise RelayNotificationMigrationError("relay_notification_dual_invalid")
    try:
        database = directory / "dual-deliveries.sqlite3"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise RelayNotificationMigrationError("relay_notification_dual_schema_invalid")
            meta = db.execute("SELECT source_sha256,source_present,activated,marker_value FROM metadata WHERE id=1").fetchone()
            if (meta is None or not _fingerprint(meta[0]) or meta[1] not in (0, 1) or meta[2] != 1
                    or json.loads(meta[3]) != marker):
                raise RelayNotificationMigrationError("relay_notification_dual_database_invalid")
            db.execute("SELECT fingerprint,slack,lark FROM receipts INDEXED BY sqlite_autoindex_receipts_1 "
                       "WHERE fingerprint=? LIMIT 1", ("0" * 64,)).fetchone()
    except (sqlite3.Error, json.JSONDecodeError, UnicodeError, TypeError):
        raise RelayNotificationMigrationError("relay_notification_dual_database_invalid") from None
    _history_lookup(directory, "0" * 64)
    return {key: value[key] for key in ("plan_sha256", "source_count", "history_count", "dual_count")}


def previous_notification(runtime, fingerprint):
    """Indexed pre-send suppression, including conflicting older event histories."""
    if not _fingerprint(fingerprint):
        raise RelayNotificationMigrationError("relay_notification_fingerprint_invalid")
    directory = _runtime(runtime) / "notifications"
    _safe(directory)
    receipt, database = directory / "relay-migration.json", directory / "relay-migration-history.sqlite3"
    _safe(receipt)
    _safe(database)
    if not receipt.exists() and not database.exists():
        return False
    return _history_lookup(directory, fingerprint)
