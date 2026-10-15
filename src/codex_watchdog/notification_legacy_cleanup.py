"""Cold-only retirement of recognized, copied notification history.

Called only by the root cutover while its real old-sender exclusion and both
notification locks remain held. These are copies made by the relay importer,
not the source runtimes, native journals, configuration or arbitrary backups.
Unknown entries stay in place. Ordinary observation never enumerates this tree.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import stat

from .notification_receipt_retention import RetentionError, _digest, _safe

JSON_NAMES = ("last-events.json", "dual-deliveries.json")
BACKUP = ".relay-authority-v1-backup"
DATABASE = "dual-deliveries.sqlite3"


def _identity(value):
    # Reads may legitimately advance atime. It is not the copied bytes' identity.
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _regular(path):
    _safe(path)
    value = path.stat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ValueError("notification_copy_not_private_regular_file")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("notification_copy_sidecar_present")
    return value


def _json_copy(path, name):
    # This is an explicit one-time cold parse. Never called by a hot reader.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("notification_copy_duplicate_key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("notification_copy_nonfinite_number")
    with path.open("rb") as stream:
        value = json.load(stream, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        return False
    if name == "last-events.json":
        return (set(value) == {"schema_version", "last_events"}
                and value["schema_version"] == 1 and isinstance(value["last_events"], dict)
                and all(_digest(key) and isinstance(entry, dict)
                        and _digest(entry.get("fingerprint"))
                        and isinstance(entry.get("recorded_at"), str) and entry["recorded_at"]
                        for key, entry in value["last_events"].items()))
    if value["schema_version"] == 2:
        # Unknown marker fields might carry operator data. Do not erase them.
        return set(value) == {"schema_version", "storage"} and value["storage"] == DATABASE
    from .notification_receipts import _legacy
    _legacy(value)
    return True


def _database_copy(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchmany(4)
        if (db.execute("PRAGMA user_version").fetchone()[0] != 1
                or set(objects) != {("table", "receipts"), ("table", "metadata")}):
            return False
        for table, names in (("receipts", ("fingerprint", "slack", "lark")),
                             ("metadata", ("id", "source_sha256", "source_present", "activated", "marker_value", "export_sha256"))):
            fields = db.execute("PRAGMA table_info(" + table + ")").fetchmany(8)
            if tuple(row[1] for row in fields) != names:
                return False
        rows = db.execute("SELECT id,source_sha256,source_present,activated,marker_value FROM metadata LIMIT 2").fetchall()
        if (len(rows) != 1 or rows[0][0] != 1 or not _digest(rows[0][1])
                or rows[0][2] not in (0, 1) or rows[0][3] not in (0, 1)
                or not isinstance(rows[0][4], str)):
            return False
        marker = json.loads(rows[0][4])
        return marker == dict(schema_version=2, storage=DATABASE)


def _retire_source_evidence(runtime, *, expected_root):
    """Internal continuation of the already-fenced root cutover.

    The finite root is the anti-replay boundary; copied history is no longer
    consulted. Failure to remove one optional copy must not roll back readiness
    or block a later independent notification. Counts omit names and content.
    Interrupted deletion is idempotent and does not require another archive.
    """
    from .notification_root_retention import read_notification_root
    if read_notification_root(runtime) != expected_root:
        raise RetentionError("notification_copy_root_identity_changed")
    root = Path(runtime).absolute() / "notifications" / "source-evidence"
    result = dict(removed=0, removed_bytes=0, preserved=0)
    try:
        _safe(root)
        if not root.exists():
            return result
        if not root.is_dir():
            raise ValueError("notification_copy_directory_invalid")
        for directory in root.iterdir():
            try:
                _safe(directory)
                if not _digest(directory.name) or not directory.is_dir():
                    raise ValueError("notification_copy_namespace_unknown")
                for path in directory.iterdir():
                    try:
                        name = path.name.removesuffix(BACKUP)
                        if path.name not in JSON_NAMES + tuple(n + BACKUP for n in JSON_NAMES) + (DATABASE,):
                            raise ValueError("notification_copy_name_unknown")
                        before = _regular(path)
                        recognized = _database_copy(path) if name == DATABASE else _json_copy(path, name)
                        if not recognized or _identity(_regular(path)) != _identity(before):
                            raise ValueError("notification_copy_unrecognized_or_changed")
                        path.unlink()
                        result["removed"] += 1
                        result["removed_bytes"] += before.st_size
                    except (OSError, ValueError, RecursionError, sqlite3.Error):
                        result["preserved"] += 1
                try:
                    directory.rmdir()  # Empty only; never recursively remove unknown content.
                except OSError:
                    pass
            except (OSError, ValueError):
                result["preserved"] += 1
        try:
            root.rmdir()
        except OSError:
            pass
    except (OSError, ValueError):
        result["preserved"] += 1
    return result
