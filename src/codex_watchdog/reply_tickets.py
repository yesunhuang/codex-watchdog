"""Four active one-shot tickets per provider/session, with indexed audit history.

Legacy JSON journals remain as atomic backups. Their mappings are retired on
migration: they cannot prove that an old controlled reply wasn't admitted.
No history scan occurs on polling, lookup or claim after that one-time import.
"""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone

from .models import utc_now
from .storage import FileLock, InstructionStore, StoreBusyError

ACTIVE_TICKET_LIMIT = 4


def ticket_time(entry):
    """Only new-protocol immutable envelopes may open an imported ticket."""
    if entry.get("ticket_schema") != 1:
        return None
    value = entry.get("created_at")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("reply_ticket_timestamp_invalid") from None


class ReplyTickets:
    def __init__(self, path, lock_path, provider, runtime, legacy_reader):
        self.path, self.lock_path = Path(path), Path(lock_path)
        self.database = Path(runtime) / provider / "reply-tickets.sqlite3"
        self.namespace = str(self.path.relative_to(Path(runtime))).replace("\\", "/")
        self.legacy_reader = legacy_reader

    def _backup_v1(self, db):
        backup = self.database.with_name(self.database.name + ".v1-backup")
        if backup.is_symlink():
            raise ValueError("reply_ticket_backup_invalid")
        if backup.exists():
            return  # Never replace the pre-migration recovery snapshot.
        fd, name = tempfile.mkstemp(prefix=".reply-ticket-backup-", dir=self.database.parent)
        temporary = Path(name)
        os.close(fd)
        try:
            with closing(sqlite3.connect(str(temporary))) as destination:
                db.backup(destination)  # Must run before BEGIN IMMEDIATE.
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            try:
                os.link(temporary, backup)
            except FileExistsError:
                pass  # A concurrent namespace published the same migration backup.
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _active_indexes(db):
        db.execute("DROP INDEX IF EXISTS active_tickets")
        db.execute("""CREATE INDEX IF NOT EXISTS session_active_tickets
            ON records(thread_id COLLATE NOCASE,created_at,namespace,key)
            WHERE active=1 AND kind='threads'""")
        db.execute("""CREATE INDEX IF NOT EXISTS namespace_active_tickets
            ON records(namespace,created_at,key) WHERE active=1 AND kind='threads'""")

    @contextmanager
    def transaction(self):
        with FileLock(self.lock_path):
            self.database.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.database), timeout=1)
            try:
                db.execute("PRAGMA synchronous=FULL")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise ValueError("reply_ticket_schema_invalid")
                if version == 1:
                    self._backup_v1(db)
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise ValueError("reply_ticket_schema_invalid")
                if version == 0:
                    db.execute("CREATE TABLE sources (namespace TEXT PRIMARY KEY)")
                    db.execute("""CREATE TABLE records (
                        namespace TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
                        value TEXT NOT NULL, fingerprint TEXT, thread_id TEXT,
                        active INTEGER NOT NULL DEFAULT 0, created_at TEXT,
                        PRIMARY KEY(namespace,kind,key))""")
                    db.execute("CREATE INDEX notification_lookup ON records(namespace,kind,fingerprint)")
                    db.execute("CREATE INDEX thread_lookup ON records(namespace,kind,thread_id)")
                if version in (0, 1):
                    self._active_indexes(db)
                    db.execute("DELETE FROM records WHERE kind='route_poll_cursors'")
                    db.execute("PRAGMA user_version=2")
                if not db.execute("SELECT 1 FROM sources WHERE namespace=?", (self.namespace,)).fetchone():
                    self._migrate(db)
                else:
                    self._marker()
                yield db
                db.commit()
            except sqlite3.OperationalError as error:
                db.rollback()
                if "locked" in str(error) or "busy" in str(error):
                    raise StoreBusyError("reply_ticket_store_busy") from None
                raise
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def _migrate(self, db):
        if self.path.exists():
            original = self.path.read_bytes()
            marker = json.loads(original)
            if marker.get("schema_version") == 2:
                raise ValueError("reply_ticket_database_missing")
            old = self.legacy_reader()
            backup = self.path.with_name(self.path.name + ".v1-backup")
            if backup.exists():
                if backup.read_bytes() != original:
                    raise ValueError("reply_ticket_migration_backup_mismatch")
            else:
                from .messaging_profile import write_new
                write_new(backup, original)
            for kind in ("threads", "events", "messages", "notifications"):
                for key, entry in old.get(kind, {}).items():
                    self.put(db, kind, key, entry)  # Every legacy ticket is closed.
        db.execute("INSERT INTO sources VALUES (?)", (self.namespace,))
        # Commit before publishing the marker: crash recovery uses the completed
        # namespace import even if the old JSON is still present. Never reimport.
        db.commit()
        self._marker()
        db.execute("BEGIN IMMEDIATE")

    def _marker(self):
        if self.path.is_symlink():
            raise ValueError("reply_ticket_marker_invalid")
        if self.path.exists():
            original = self.path.read_bytes()
            value = json.loads(original)
            if value.get("schema_version") == 2 and value.get("storage") == "reply-tickets.sqlite3":
                if value.get("active_scope") == "provider_session":
                    return
                if "active_scope" in value:
                    raise ValueError("reply_ticket_marker_invalid")
                backup = self.path.with_name(self.path.name + ".scope-v1-backup")
                if backup.exists():
                    if backup.read_bytes() != original:
                        raise ValueError("reply_ticket_migration_backup_mismatch")
                else:
                    from .messaging_profile import write_new
                    write_new(backup, original)
                value["active_scope"] = "provider_session"
                InstructionStore._atomic_json(self.path, value)
                return
            backup = self.path.with_name(self.path.name + ".v1-backup")
            if value.get("schema_version") != 1 or not backup.exists() or backup.read_bytes() != original:
                raise ValueError("reply_ticket_marker_invalid")
        # Repair only a completed namespace import's missing/legacy marker.
        InstructionStore._atomic_json(self.path, dict(schema_version=2,
            storage="reply-tickets.sqlite3", active_limit=ACTIVE_TICKET_LIMIT,
            active_scope="provider_session", legacy_policy="retired_all", migrated_at=utc_now()))

    def get(self, db, kind, key):
        row = db.execute("SELECT value FROM records WHERE namespace=? AND kind=? AND key=?",
                         (self.namespace, kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, db, kind, key, value):
        db.execute("""INSERT INTO records(namespace,kind,key,value,fingerprint,thread_id,created_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value,
            fingerprint=excluded.fingerprint,thread_id=excluded.thread_id""",
            (self.namespace, kind, key, json.dumps(value, sort_keys=True), value.get("event_fingerprint"),
             value.get("target", {}).get("thread_id"), value.get("created_at")))

    def record(self, db, key, entry, *, active=True):
        prior = self.get(db, "threads", key)
        if prior is not None:
            if prior["target"] != entry["target"] or prior["event_fingerprint"] != entry["event_fingerprint"]:
                raise ValueError("reply_ticket_mapping_collision")
            return  # Re-caching or re-sending must never reopen a closed ticket.
        if active:
            thread_id = entry.get("target", {}).get("thread_id")
            try:
                canonical = str(uuid.UUID(thread_id))
                if canonical != thread_id.lower():
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError("reply_ticket_thread_id_invalid") from None
        self.put(db, "threads", key, entry)
        if not active:
            return
        db.execute("UPDATE records SET active=1 WHERE namespace=? AND kind='threads' AND key=?",
                   (self.namespace, key))
        # Equal timestamps are possible on coarse clocks. Persisted insertion
        # order breaks ties across namespaces; mapping refreshes retain rowid.
        excess = db.execute("""SELECT namespace,key FROM records INDEXED BY session_active_tickets
            WHERE active=1 AND kind='threads'
            AND thread_id COLLATE NOCASE=?
            ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET ?""",
            (canonical, ACTIVE_TICKET_LIMIT)).fetchall()
        for namespace, old_key in excess:
            db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                       (namespace, old_key))

    def claim(self, db, key):
        return db.execute("""UPDATE records SET active=0 WHERE namespace=? AND kind='threads'
            AND key=? AND active=1""", (self.namespace, key)).rowcount == 1

    def active(self, db):
        return {key: json.loads(value) for key, value in db.execute(
            "SELECT key,value FROM records INDEXED BY namespace_active_tickets "
            "WHERE namespace=? AND kind='threads' AND active=1 "
            "ORDER BY created_at,key", (self.namespace,))}

    def mappings(self, db, fingerprint=None, thread_ids=None, active=False):
        if active:
            entries = self.active(db).values()
            return [v for v in entries if (fingerprint is None or v["event_fingerprint"] == fingerprint)
                    and (thread_ids is None or v["target"]["thread_id"] in thread_ids)]
        return [json.loads(row[0]) for row in db.execute(
            "SELECT value FROM records WHERE namespace=? AND kind='threads' AND fingerprint=?",
            (self.namespace, fingerprint))]
