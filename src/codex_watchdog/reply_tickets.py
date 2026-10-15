"""Four recent one-shot tickets per provider/session, with a 72-hour TTL.

Legacy JSON journals remain as atomic backups. Their mappings are retired on
migration: they cannot prove that an old controlled reply wasn't admitted.
Current grants/routes and small monotonic boundaries survive recent retirement.
History-sized migration is explicit; ordinary operations never import history.
"""
from contextlib import closing, contextmanager, nullcontext
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
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
    def __init__(self, path, lock_path, provider, runtime, legacy_reader, *, authority=None, clock=None,
                 before_prune=None, retention_fence=None):
        self.path, self.lock_path = Path(path), Path(lock_path)
        self.database = Path(runtime) / provider / "reply-tickets.sqlite3"
        self.namespace = str(self.path.relative_to(Path(runtime))).replace("\\", "/")
        self.legacy_reader = legacy_reader
        self.authority = authority
        self.provider = provider
        self.runtime = Path(runtime)
        # Python 3.9 on Windows can expose coarser time.time() ticks than
        # datetime.now(); use the same UTC clock as ticket creation so a fresh
        # local envelope cannot look several milliseconds into the future.
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc).timestamp()
        if before_prune is not None and not callable(before_prune):
            raise TypeError("reply_ticket_before_prune_invalid")
        # Caller-owned current pins (journal, db) -> exact bool. The caller holds
        # its own fence; this journal never creates or acquires native ownership.
        self.before_prune = before_prune
        if retention_fence is not None and not callable(retention_fence):
            raise TypeError("reply_ticket_retention_fence_invalid")
        self.retention_fence = retention_fence or nullcontext
        self._retention_deferred = False
        self._retention_session = None
        if authority is not None:
            self.path = authority.runtime / self.namespace
            self.database = authority.runtime / provider / "reply-tickets.sqlite3"
            # Initial migration is explicit and complete before activation.
            # A new namespace may start empty; polling never imports old state.
            self.legacy_reader = lambda: {}

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

    def _before_prune(self, db):
        """Run caller-supplied current pins; unknown proof defers only deletion."""
        self._retention_deferred = False
        self._retention_session = None
        callback = getattr(self, "before_prune", None)
        if callback is None:
            return
        if not db.in_transaction:
            raise ValueError("reply_ticket_before_prune_transaction_inactive")
        db.execute("SAVEPOINT reply_ticket_before_prune")
        try:
            proven = callback(self, db)
        except Exception:
            proven = None  # Unknown current proof; independent work continues.
        if not db.in_transaction:
            raise ValueError("reply_ticket_before_prune_transaction_ended")
        try:
            if proven is not True:
                # Exact True only. Discard partial pin writes so the independent
                # body cannot commit them; retention deletion waits for proof.
                self._retention_deferred = True
                db.execute("ROLLBACK TO SAVEPOINT reply_ticket_before_prune")
            db.execute("RELEASE SAVEPOINT reply_ticket_before_prune")
        except sqlite3.OperationalError as error:
            if "locked" in str(error) or "busy" in str(error):
                raise
            raise ValueError("reply_ticket_before_prune_savepoint_invalid") from None

    @contextmanager
    def transaction(self):
        lock = (self.authority.journal_lock(self.provider) if self.authority is not None
                else FileLock(self.lock_path))
        with self.retention_fence(), lock:
            if self.authority is not None and (self.database != self.authority.runtime/self.provider/'reply-tickets.sqlite3'
                    or self.path != self.authority.runtime/self.namespace):
                raise ValueError('reply_ticket_authority_binding_stale')
            self._projection_retirements=[]
            self._retention_deferred=False
            self._retention_session=None
            self.database.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.database), timeout=1)
            try:
                db.execute("PRAGMA synchronous=FULL")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0 and self.path.exists():
                    raise ValueError("reply_ticket_retention_migration_required")
                if version in (1, 2):
                    raise ValueError("reply_ticket_retention_migration_required")
                if version not in (0, 3):
                    raise ValueError("reply_ticket_schema_invalid")
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 3):
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
                if version == 0:
                    self._active_indexes(db)
                    from .recent_state import initialize
                    initialize(db)
                    db.execute("PRAGMA user_version=3")
                if self.authority is not None:
                    db.execute("CREATE INDEX IF NOT EXISTS session_relay_pending "
                               "ON records(thread_id,namespace,key) WHERE active=1 AND kind='events'")
                if not db.execute("SELECT 1 FROM sources WHERE namespace=?", (self.namespace,)).fetchone():
                    self._migrate(db)
                else:
                    self._marker()
                # Caller-supplied current pins precede any retention deletion.
                self._before_prune(db)
                from .recent_state import prune
                prune(self, db)
                yield db
                db.commit()
                self._flush_projection_retirements()
            except sqlite3.OperationalError as error:
                db.rollback()
                if "locked" in str(error) or "busy" in str(error):
                    raise StoreBusyError("reply_ticket_store_busy") from None
                raise
            except BaseException:
                db.rollback()
                raise
            finally:
                self._projection_retirements=[]
                self._retention_deferred=False
                self._retention_session=None
                db.close()

    def defer_retention(self):
        """Commit valid Q-only pin additions without uncertain retirement.

        A successful preflight may lack proof for deletion. False or failed
        callbacks roll back their additions; successful callers can defer GC.
        """
        self._retention_deferred = True

    def _migrate(self, db):
        if self.path.exists():
            raise ValueError("reply_ticket_namespace_explicit_import_required")
        db.execute("INSERT INTO sources VALUES (?)", (self.namespace,))
        # Commit before publishing the marker: crash recovery uses the completed
        # namespace import even if the old JSON is still present. Never reimport.
        db.commit()
        self._marker()
        db.execute("BEGIN IMMEDIATE")

    def _marker(self, *, explicit=False):
        if self.path.is_symlink():
            raise ValueError("reply_ticket_marker_invalid")
        if self.path.exists():
            with self.path.open("rb") as stream:
                original = stream.read() if explicit else stream.read(16385)
            if not explicit and len(original)>16384:
                raise ValueError("reply_ticket_marker_explicit_repair_required")
            value = json.loads(original)
            if value.get("schema_version")==1:
                recovery=self.path.with_name(self.path.name+".legacy-current-v1.json")
                from .models import sha256_text
                import hashlib
                if not recovery.exists():
                    raise ValueError("reply_ticket_marker_explicit_repair_required")
                saved=json.loads(recovery.read_bytes())
                if saved.get("source_sha256")!=hashlib.sha256(original).hexdigest():
                    raise ValueError("reply_ticket_legacy_source_changed")
                marker=dict(saved["legacy_header"])
                marker.update(schema_version=3,storage="reply-tickets.sqlite3",active_limit=4,
                    active_scope="provider_session",ticket_ttl_seconds=72*60*60,recent_limit=256,
                    retention_policy="bounded_recent",active_downgrade="unsupported",legacy_policy="retired_all")
                InstructionStore._atomic_json(self.path,marker)
                return
            if value.get("schema_version") == 3 and value.get("storage") == "reply-tickets.sqlite3":
                if (value.get("active_limit") != 4 or value.get("active_scope") != "provider_session"
                        or value.get("ticket_ttl_seconds") != 72*60*60 or value.get("recent_limit") != 256):
                    raise ValueError("reply_ticket_marker_invalid")
                return
            if value.get("schema_version") == 2 and value.get("storage") == "reply-tickets.sqlite3":
                if value.get("active_scope") not in (None, "provider_session"):
                    raise ValueError("reply_ticket_marker_invalid")
                backup = self.path.with_name(self.path.name + ".current-v2-backup")
                if backup.exists():
                    if backup.read_bytes() != original:
                        raise ValueError("reply_ticket_migration_backup_mismatch")
                else:
                    from .messaging_profile import write_new
                    write_new(backup, original)
                value["active_scope"] = "provider_session"
                value.update(schema_version=3, ticket_ttl_seconds=72*60*60, recent_limit=256,
                             retention_policy="bounded_recent", active_downgrade="unsupported")
                InstructionStore._atomic_json(self.path, value)
                return
            backup = self.path.with_name(self.path.name + ".v1-backup")
            if value.get("schema_version") != 1 or not backup.exists() or backup.read_bytes() != original:
                raise ValueError("reply_ticket_marker_invalid")
        # Repair only a completed namespace import's missing/legacy marker.
        InstructionStore._atomic_json(self.path, dict(schema_version=3,
            storage="reply-tickets.sqlite3", active_limit=ACTIVE_TICKET_LIMIT,
            active_scope="provider_session", legacy_policy="retired_all", migrated_at=utc_now(),
            ticket_ttl_seconds=72*60*60, recent_limit=256, retention_policy="bounded_recent",
            active_downgrade="unsupported"))

    def migrate_recent_state(self, *, protected_keys=()):
        from .recent_state import migrate
        return migrate(self, protected_keys=protected_keys)

    def protect(self, db, kind, key, protected=True):
        from .recent_state import protect
        protect(self, db, kind, key, protected)

    def retire_projection(self, db, namespace, key):
        """Capture current metadata before deletion; publish only after commit."""
        row=db.execute("SELECT value FROM records WHERE namespace=? AND kind='threads' AND key=?",(namespace,key)).fetchone()
        if row is None:return
        try:
            entry=json.loads(row[0])
            if not isinstance(entry,dict):return
            from .control_context import current_control
            selected=current_control()
            store=self.authority.store if self.authority is not None else selected[0] if selected is not None else None
            if store is None or entry['target']['thread_id'] != store.thread_id:return
            entry=dict(entry,provider=self.provider,ticket_schema=1)
            if self.provider != 'slack':entry.setdefault('scope',namespace.split('/')[1])
            from .control_state import control_mapping_identity
            provider,address,_=control_mapping_identity(store,entry)
            pending=getattr(self,'_projection_retirements',None)
            if pending is None:pending=self._projection_retirements=[]
            pending.append((store,provider,address,entry['created_at']))
        except Exception:
            pass  # Authoritative journal close does not depend on projection.

    def _flush_projection_retirements(self):
        pending=getattr(self,'_projection_retirements',())
        self._projection_retirements=[]
        for store,provider,address,created in pending:
            try:
                store.retire_relay_mappings(provider,[address],now=self.clock(),created_at_by_key={address:created})
            except Exception:
                pass  # Never roll back/retry an already committed admission.

    def get(self, db, kind, key):
        from .recent_state import now, _remove, retention_allowed
        recent = db.execute("SELECT expires,protected,session FROM recent_records WHERE namespace=? AND kind=? AND key=?",
                            (self.namespace,kind,key)).fetchone()
        if recent is not None and not recent[1] and recent[0] <= now(self,db):
            if retention_allowed(self, recent[2]):
                _remove(self,db,[(self.namespace,kind,key)],now(self,db))
                return None
            # Unknown current proof keeps the expired deduplication point;
            # live_ticket below still refuses an expired parent.
        row = db.execute("SELECT value,thread_id FROM records WHERE namespace=? AND kind=? AND key=?",
                         (self.namespace, kind, key)).fetchone()
        if (row and kind == "threads" and self.authority is not None
                and row[1] != self.authority.thread_id):
            return None
        value = json.loads(row[0]) if row else None
        if isinstance(value, dict) and value.get("relay_authority_quarantine") is True:
            raise ValueError("relay_authority_historical_identity_quarantined")
        if kind == "threads" and value is not None:
            from .recent_state import live_ticket
            if not live_ticket(self, db, value):
                return None
            from .session_activity import SessionActivity
            try:
                runtime = self.authority.runtime if self.authority is not None else self.runtime
                if not SessionActivity(runtime).is_active(str(uuid.UUID(value["target"]["thread_id"]))):
                    return None
            except (ValueError,OSError,KeyError):
                pass  # Lifecycle uncertainty does not replace native/auth safety.
        return value

    def put(self, db, kind, key, value):
        from .recent_state import canonical_session
        target=value.get('target')
        target_thread=target.get('thread_id') if isinstance(target,dict) else None
        thread_id=value.get('thread_id') or target_thread
        if target_thread is not None and value.get('thread_id') is not None and canonical_session(target_thread)!=canonical_session(value['thread_id']):
            raise ValueError('reply_ticket_record_thread_mismatch')
        if thread_id is not None:
            thread_id = canonical_session(thread_id)
        if self.authority is not None:
            thread_id = thread_id or self.authority.thread_id
            if thread_id != self.authority.thread_id:
                raise ValueError("relay_authority_foreign_session")
        db.execute("""INSERT INTO records(namespace,kind,key,value,fingerprint,thread_id,created_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value,
            fingerprint=excluded.fingerprint,thread_id=excluded.thread_id""",
            (self.namespace, kind, key, json.dumps(value, sort_keys=True), value.get("event_fingerprint"),
             thread_id, value.get("created_at")))
        if self.authority is not None and kind == "events":
            pending = ((value.get("state") in ("dispatching", "uncertain")
                or value.get("delivery_status") in ("enqueued", "consumed_or_started", "started"))
                    and value.get("native_completed") is not True)
            db.execute("UPDATE records SET active=? WHERE namespace=? AND kind=? AND key=?",
                       (int(pending), self.namespace, kind, key))
        from .recent_state import track
        track(self, db, kind, key, value)

    def record(self, db, key, entry, *, active=True):
        row = db.execute("SELECT value FROM records WHERE namespace=? AND kind='threads' AND key=?",
                         (self.namespace,key)).fetchone()
        prior = json.loads(row[0]) if row else None
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
            from .recent_state import boundary, epoch, live_ticket
            if not live_ticket(self,db,entry) or epoch(entry.get("created_at")) <= boundary(db,self.namespace,canonical):
                return  # Ancient/closed callbacks cannot resurrect a missing row.
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
            ORDER BY created_at DESC,rowid DESC LIMIT 32 OFFSET ?""",
            (canonical, ACTIVE_TICKET_LIMIT)).fetchall()
        for namespace, old_key in excess:
            self.retire_projection(db,namespace,old_key)
            from .recent_state import close_ticket, now
            row = db.execute("SELECT created_at FROM records WHERE namespace=? AND kind='threads' AND key=?",
                             (namespace,old_key)).fetchone()
            if row:close_ticket(db,namespace,canonical,row[0],now(self,db))
            db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                       (namespace, old_key))

    def claim(self, db, key):
        entry = self.get(db, "threads", key)
        if entry is None:
            return False
        if self.authority is not None:
            claimed = db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' "
                              "AND key=? AND active=1 AND thread_id=?",
                              (self.namespace, key, self.authority.thread_id)).rowcount == 1
        else:
            claimed = db.execute("""UPDATE records SET active=0 WHERE namespace=? AND kind='threads'
                AND key=? AND active=1""", (self.namespace, key)).rowcount == 1
        if claimed:
            self.retire_projection(db,self.namespace,key)
            from .recent_state import close_ticket, now
            close_ticket(db,self.namespace,entry["target"]["thread_id"],entry.get("created_at"),now(self,db))
        return claimed

    def active(self, db):
        if self.authority is not None:
            rows = db.execute(
                "SELECT key,value FROM records INDEXED BY session_active_tickets "
                "WHERE namespace=? AND kind='threads' AND active=1 AND thread_id COLLATE NOCASE=? "
                "ORDER BY created_at,key", (self.namespace, self.authority.thread_id)).fetchall()
        else:
            rows = db.execute(
                "SELECT key,value FROM records INDEXED BY namespace_active_tickets "
                "WHERE namespace=? AND kind='threads' AND active=1 "
                "ORDER BY created_at,key", (self.namespace,)).fetchall()
        result = {}
        from .recent_state import live_ticket, close_ticket, now
        from .session_activity import SessionActivity
        runtime = self.authority.runtime if self.authority is not None else self.runtime
        lifecycle = SessionActivity(runtime)
        for key,raw in rows:
            try:
                value=json.loads(raw)
                if not live_ticket(self,db,value):
                    self.retire_projection(db,self.namespace,key)
                    db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",(self.namespace,key))
                    close_ticket(db,self.namespace,value["target"]["thread_id"],value.get("created_at"),now(self,db))
                    continue
                try:
                    enabled=lifecycle.is_active(str(uuid.UUID(value["target"]["thread_id"])))
                except (ValueError,OSError):
                    enabled=True  # optional tracking uncertainty cannot erase current auth
                if not enabled:continue
            except (ValueError,KeyError,TypeError,OSError):
                continue
            result[key]=value
        return result

    def mappings(self, db, fingerprint=None, thread_ids=None, active=False):
        if active:
            entries = self.active(db).values()
            return [v for v in entries if (fingerprint is None or v["event_fingerprint"] == fingerprint)
                    and (thread_ids is None or v["target"]["thread_id"] in thread_ids)]
        values = [json.loads(row[0]) for row in db.execute(
            "SELECT value FROM records WHERE namespace=? AND kind='threads' AND fingerprint=?",
            (self.namespace, fingerprint))]
        return [entry for entry in values if self.authority is None
                or entry.get("target", {}).get("thread_id") == self.authority.thread_id]
