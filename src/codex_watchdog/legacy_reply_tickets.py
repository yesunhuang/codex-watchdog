"""Previous-public schema2 reply tickets as a bounded compatibility fallback.

A read-only version hint selects this journal; the existing lock revalidates
the schema before any write. It never migrates, prunes, deletes, imports schema1
JSON or creates current-only tables. Closed, expired and evicted rows stay
present and are never reopened. This is not finite retention or native proof.
"""
from contextlib import contextmanager
import json
import re
import sqlite3
import uuid

from .models import utc_now
from .recent_state import TTL_SECONDS, canonical_session, epoch
from .reply_tickets import ACTIVE_TICKET_LIMIT, ReplyTickets
from .storage import FileLock, InstructionStore, StoreBusyError

LEGACY_SCHEMA_VERSION = 2
LEGACY_MARKER_LIMIT = 16384
EVICTION_BUDGET = 32
LEGACY_COLUMNS = ('namespace', 'kind', 'key', 'value', 'fingerprint', 'thread_id', 'active', 'created_at')
LEGACY_OBJECTS = ('sources', 'records', 'session_active_tickets', 'namespace_active_tickets')
CURRENT_ONLY_TABLES = ('recent_records', 'current_boundaries')
_ACTIVE_PREDICATE = re.compile(r"(?i:\bWHERE\s+active\s*=\s*1\s+AND\s+kind\s*=)\s*'threads'\s*;?\s*\Z")


def _uri(database, mode):
    return database.absolute().as_uri() + '?mode=' + mode


def _busy(error):
    return 'locked' in str(error) or 'busy' in str(error)


def schema_hint(database):
    """Read-only version hint; never creates, locks or migrates the journal."""
    if database.is_symlink() or not database.is_file():
        return None
    try:
        db = sqlite3.connect(_uri(database, 'ro'), timeout=1, uri=True)
    except sqlite3.Error:
        return None
    try:
        return db.execute('PRAGMA user_version').fetchone()[0]
    except sqlite3.OperationalError as error:
        if _busy(error):
            raise StoreBusyError('reply_ticket_store_busy') from None
        return None  # The selected journal fails closed under its own lock.
    except sqlite3.DatabaseError:
        return None
    finally:
        db.close()


def select_reply_journal(*args, **kwargs):
    """Fresh/current schema3 journal, or the schema2 compatibility journal."""
    use_context = kwargs.get("before_prune") is None
    if use_context:
        from .current_work_retention import unsupported_retention
        kwargs["before_prune"] = unsupported_retention
    current = ReplyTickets(*args, **kwargs)
    if schema_hint(current.database) == LEGACY_SCHEMA_VERSION:
        return LegacyReplyTickets(*args, **kwargs)
    # Only the exact lexical desktop cycle can supply this proof. A
    # configured/coordinated authority retains its own typed binding. Legacy
    # compatibility has no deletion and needs no extra native transaction read.
    if use_context and kwargs.get("authority") is None:
        from .desktop_retention import current_retention
        retained = current_retention(current.runtime)
        if retained is not None:
            current.before_prune = retained.before_prune
            current.retention_fence = retained.fence
    return current


def _legacy_marker(value):
    return (isinstance(value, dict) and type(value.get('schema_version')) is int
            and value['schema_version'] == LEGACY_SCHEMA_VERSION
            and value.get('storage') == 'reply-tickets.sqlite3'
            and value.get('active_scope') == 'provider_session'
            and type(value.get('active_limit', ACTIVE_TICKET_LIMIT)) is int
            and value.get('active_limit', ACTIVE_TICKET_LIMIT) == ACTIVE_TICKET_LIMIT)


class LegacyReplyTickets(ReplyTickets):
    """Schema2 operations; the schema, marker and historical rows stay as found."""

    legacy_protocol = 2

    @contextmanager
    def transaction(self):
        lock = (self.authority.journal_lock(self.provider) if self.authority is not None
                else FileLock(self.lock_path))
        with self.retention_fence(), lock:
            if self.authority is not None and (self.database != self.authority.runtime/self.provider/'reply-tickets.sqlite3'
                    or self.path != self.authority.runtime/self.namespace):
                raise ValueError('reply_ticket_authority_binding_stale')
            self._projection_retirements = []
            self._retention_deferred = True  # Compatibility mode never deletes.
            self._retention_session = None
            if self.database.is_symlink() or not self.database.is_file():
                raise ValueError('reply_ticket_legacy_schema_changed')
            db = sqlite3.connect(_uri(self.database, 'rw'), timeout=1, uri=True)
            try:
                db.execute('PRAGMA synchronous=FULL')
                if db.execute('PRAGMA user_version').fetchone()[0] != LEGACY_SCHEMA_VERSION:
                    raise ValueError('reply_ticket_legacy_schema_changed')
                db.execute('BEGIN IMMEDIATE')
                self._validate(db)
                if not db.execute('SELECT 1 FROM sources WHERE namespace=?', (self.namespace,)).fetchone():
                    self._migrate(db)
                else:
                    self._marker()
                yield db
                db.commit()
                self._flush_projection_retirements()
            except sqlite3.OperationalError as error:
                db.rollback()
                if _busy(error):
                    raise StoreBusyError('reply_ticket_store_busy') from None
                raise
            except BaseException:
                db.rollback()
                raise
            finally:
                self._projection_retirements = []
                self._retention_deferred = False
                self._retention_session = None
                db.close()

    @staticmethod
    def _validate(db):
        if db.execute('PRAGMA user_version').fetchone()[0] != LEGACY_SCHEMA_VERSION:
            raise ValueError('reply_ticket_legacy_schema_changed')
        names = {row[0] for row in db.execute('SELECT name FROM sqlite_master WHERE name IN (?,?,?,?,?,?)',
                                              LEGACY_OBJECTS + CURRENT_ONLY_TABLES)}
        fields = db.execute('PRAGMA table_info(records)').fetchmany(9)
        columns = tuple(row[1] for row in fields)
        primary = tuple(row[1] for row in sorted(fields, key=lambda row: row[5]) if row[5])
        sources = db.execute('PRAGMA table_info(sources)').fetchmany(3)
        if (names != set(LEGACY_OBJECTS) or columns != LEGACY_COLUMNS
                or primary != ('namespace', 'kind', 'key')
                or len(sources) != 1 or sources[0][1] != 'namespace' or sources[0][5] != 1):
            raise ValueError('reply_ticket_legacy_schema_invalid')
        for name, shape in (
            ('session_active_tickets', [('thread_id', 'NOCASE'), ('created_at', 'BINARY'),
                                        ('namespace', 'BINARY'), ('key', 'BINARY')]),
            ('namespace_active_tickets', [('namespace', 'BINARY'), ('created_at', 'BINARY'),
                                          ('key', 'BINARY')]),
        ):
            index = db.execute('PRAGMA index_xinfo(' + name + ')').fetchmany(7)
            definition = db.execute("SELECT CASE WHEN length(CAST(sql AS BLOB))<=2048 THEN sql END "
                "FROM sqlite_master WHERE type='index' AND name=? AND tbl_name='records'", (name,)).fetchone()
            if ([(r[2], r[4]) for r in index if r[5]] != shape
                    or any(r[3] != 0 for r in index if r[5])
                    or definition is None or not isinstance(definition[0], str)
                    or any(token in definition[0] for token in ('--', '/*', '*/'))
                    or _ACTIVE_PREDICATE.search(definition[0]) is None):
                raise ValueError('reply_ticket_legacy_schema_invalid')
        # Validate actual compiled seeks before any record payload work. A
        # familiar name, column list or LIMIT alone does not bound a scan.
        for sql, arguments, suffix in (
            ('SELECT value FROM records WHERE namespace=? AND kind=? AND key=?',
             ('', '', ''), '(namespace=? AND kind=? AND key=?)'),
            ("SELECT key,value,created_at FROM records INDEXED BY namespace_active_tickets "
             "WHERE namespace=? AND kind='threads' AND active=1 ORDER BY created_at,key",
             ('',), 'USING INDEX namespace_active_tickets (namespace=?)'),
            ("SELECT namespace,key,created_at,rowid FROM records INDEXED BY session_active_tickets "
             "WHERE active=1 AND kind='threads' AND thread_id COLLATE NOCASE=? "
             "ORDER BY created_at DESC,namespace DESC,key DESC LIMIT 37",
             ('',), 'USING INDEX session_active_tickets (thread_id=?)'),
        ):
            try:
                plan = db.execute('EXPLAIN QUERY PLAN ' + sql, arguments).fetchmany(3)
            except sqlite3.Error:
                raise ValueError('reply_ticket_legacy_schema_invalid') from None
            # SQLite3.45 recognizes the metadata-only rank seek as covering;
            # both shapes retain all exact equality keys and no sort/scan.
            suffixes = (suffix, suffix.replace('USING INDEX ', 'USING COVERING INDEX '))
            if len(plan) != 1 or not plan[0][3].startswith(('SEARCH records ', 'SEARCH TABLE records ')) or not plan[0][3].endswith(suffixes):
                raise ValueError('reply_ticket_legacy_schema_invalid')

    def _before_prune(self, db):
        # There is no retention deletion here, so caller pins are never consulted.
        self._retention_deferred = True
        self._retention_session = None

    def _migrate(self, db):
        """Register only a new namespace without JSON; schema1 import is explicit."""
        if self.path.is_symlink() or self.path.exists():
            raise ValueError('reply_ticket_namespace_explicit_import_required')
        db.execute('INSERT INTO sources VALUES (?)', (self.namespace,))
        # Commit before publishing the marker: a crash leaves a committed source
        # whose missing marker the next transaction repairs. Never reimport.
        db.commit()
        self._marker()
        db.execute('BEGIN IMMEDIATE')
        self._validate(db)

    def _marker(self, *, explicit=False):
        if explicit:
            raise ValueError('reply_ticket_legacy_explicit_migration_unavailable')
        if self.path.is_symlink():
            raise ValueError('reply_ticket_marker_invalid')
        if not self.path.exists():
            # Repair only a committed namespace's missing marker, as schema2 did.
            InstructionStore._atomic_json(self.path, dict(schema_version=LEGACY_SCHEMA_VERSION,
                storage='reply-tickets.sqlite3', active_limit=ACTIVE_TICKET_LIMIT,
                active_scope='provider_session', legacy_policy='retired_all', migrated_at=utc_now()))
            return
        with self.path.open('rb') as stream:
            original = stream.read(LEGACY_MARKER_LIMIT + 1)
        if len(original) > LEGACY_MARKER_LIMIT:
            raise ValueError('reply_ticket_marker_explicit_repair_required')
        try:
            value = json.loads(original)
        except (ValueError, RecursionError):
            raise ValueError('reply_ticket_marker_invalid') from None
        if not _legacy_marker(value):
            # Schema1 JSON, schema3 and unknown markers need explicit repair.
            raise ValueError('reply_ticket_marker_invalid')

    def migrate_recent_state(self, *, protected_keys=()):
        raise ValueError('reply_ticket_legacy_explicit_migration_unavailable')

    def protect(self, db, kind, key, protected=True):
        """Nothing is pruned in compatibility mode; there is no pin to record."""

    def _current(self):
        current = epoch(self.clock())
        if current is None:
            raise ValueError('recent_state_clock_invalid')
        return current

    @staticmethod
    def _live(created, current):
        born = epoch(created)
        return born is not None and born <= current and current - born < TTL_SECONDS

    def _ranked_active(self, db, thread):
        """Bounded metadata seek; preserve the original rowid tie breaker.

        An unsupported oversized equal-birth group cannot prove its newest
        four. Refuse only that ticket's eligibility/cleanup, never scan the
        group or turn bounded physical retirement into extra permission.
        """
        rows = db.execute("SELECT namespace,key,created_at,rowid FROM records "
            "INDEXED BY session_active_tickets WHERE active=1 AND kind='threads' "
            "AND thread_id COLLATE NOCASE=? ORDER BY created_at DESC,namespace DESC,key DESC LIMIT 37",
            (thread,)).fetchall()
        ranked = sorted(rows[:36], key=lambda row: (row[2] or '', row[3]), reverse=True)
        if len(rows) == 37 and ranked[ACTIVE_TICKET_LIMIT-1][2] == rows[-1][2]:
            return None
        return ranked

    def _cutoff(self, db, thread, ranked):
        """Small provider/session boundary; consumption cannot promote old roots.

        A reserved current-state namespace uses the existing records PK. The
        anchor is an original ticket PK, not a mutable historical row count.
        No new schema, JSON import, archive or provider permission is created.
        """
        thread = canonical_session(thread)
        namespace = self.provider + '/reply-ticket-policy-v1'
        row = db.execute("SELECT value,thread_id FROM records WHERE namespace=? "
            "AND kind='provider_floors' AND key=?", (namespace, thread)).fetchone()
        value, floor = None, None
        if row is not None:
            value = json.loads(row[0])
            if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                    or value['schema_version'] != 1 or value.get('kind') != 'legacy_ticket_cutoff'
                    or value.get('thread_id') != thread or row[1] != thread
                    or not isinstance(value.get('ticket_namespace'), str)
                    or not isinstance(value.get('ticket_key'), str)
                    or epoch(value.get('ticket_created_at')) is None):
                raise ValueError('reply_ticket_legacy_cutoff_invalid')
            anchor = db.execute("SELECT created_at,rowid,thread_id FROM records "
                "WHERE namespace=? AND kind='threads' AND key=?",
                (value['ticket_namespace'], value['ticket_key'])).fetchone()
            if (anchor is None or anchor[0] != value['ticket_created_at']
                    or canonical_session(anchor[2]) != thread):
                raise ValueError('reply_ticket_legacy_cutoff_invalid')
            floor = anchor[:2]
        if len(ranked) >= ACTIVE_TICKET_LIMIT:
            candidate = ranked[ACTIVE_TICKET_LIMIT-1]
            cutoff = candidate[2:]
            if epoch(cutoff[0]) is None:
                raise ValueError('reply_ticket_legacy_cutoff_invalid')
            if floor is None or cutoff > floor:
                value = dict(value or {}, schema_version=1, kind='legacy_ticket_cutoff',
                    thread_id=thread, ticket_namespace=candidate[0], ticket_key=candidate[1],
                    ticket_created_at=candidate[2])
                db.execute('INSERT OR IGNORE INTO sources VALUES(?)', (namespace,))
                db.execute("INSERT INTO records(namespace,kind,key,value,thread_id) VALUES(?,'provider_floors',?,?,?) "
                    "ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value",
                    (namespace, thread, json.dumps(value, sort_keys=True), thread))
                floor = cutoff
        return floor

    def _eligible(self, db, key, thread):
        ranked = self._ranked_active(db, thread)
        if ranked is None:
            return False
        floor = self._cutoff(db, thread, ranked)
        if any((namespace, old_key) == (self.namespace, key) and (floor is None or (created, number) >= floor)
               for namespace, old_key, created, number in ranked[:ACTIVE_TICKET_LIMIT]):
            return True
        # Known logical eviction is terminal, even if bounded retirement of
        # an over-cap previous store did not physically reach this point.
        self.retire_projection(db, self.namespace, key)
        db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                   (self.namespace, key))
        return False

    def get(self, db, kind, key):
        row = db.execute('SELECT value,thread_id,created_at,active FROM records WHERE namespace=? AND kind=? AND key=?',
                         (self.namespace, kind, key)).fetchone()
        if row is None:
            return None
        if (kind == 'threads' and self.authority is not None
                and canonical_session(row[1]) != canonical_session(self.authority.thread_id)):
            return None
        value = json.loads(row[0])
        if isinstance(value, dict) and value.get('relay_authority_quarantine') is True:
            raise ValueError('relay_authority_historical_identity_quarantined')
        if kind == 'threads' and value is not None:
            # Absolute TTL from the row's original birth; refreshes never extend it.
            current = self._current()
            if not isinstance(value, dict) or not self._live(row[2], current):
                born = epoch(row[2])
                if born is not None and current-born >= TTL_SECONDS:
                    # Observed expiration is terminal even if a later clock
                    # moves backward. Keep the original row, close in place.
                    self.retire_projection(db, self.namespace, key)
                    db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                               (self.namespace, key))
                return None
            if row[3] and not self._eligible(db, key, row[1]):
                return None
            from .session_activity import SessionActivity
            try:
                runtime = self.authority.runtime if self.authority is not None else self.runtime
                if not SessionActivity(runtime).is_active(str(uuid.UUID(value['target']['thread_id']))):
                    return None
            except (ValueError, OSError, KeyError):
                pass  # Lifecycle uncertainty does not replace native/auth safety.
        return value

    def put(self, db, kind, key, value):
        target = value.get('target')
        target_thread = target.get('thread_id') if isinstance(target, dict) else None
        thread_id = value.get('thread_id') or target_thread
        if (target_thread is not None and value.get('thread_id') is not None
                and canonical_session(target_thread) != canonical_session(value['thread_id'])):
            raise ValueError('reply_ticket_record_thread_mismatch')
        if thread_id is not None:
            thread_id = canonical_session(thread_id)
        if self.authority is not None:
            thread_id = thread_id or self.authority.thread_id
            if thread_id != self.authority.thread_id:
                raise ValueError('relay_authority_foreign_session')
        # created_at is written once on insertion; updates never move birth.
        db.execute('''INSERT INTO records(namespace,kind,key,value,fingerprint,thread_id,created_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value,
            fingerprint=excluded.fingerprint,thread_id=excluded.thread_id''',
            (self.namespace, kind, key, json.dumps(value, sort_keys=True), value.get('event_fingerprint'),
             thread_id, value.get('created_at')))
        if self.authority is not None and kind == 'events':
            pending = ((value.get('state') in ('dispatching', 'uncertain')
                or value.get('delivery_status') in ('enqueued', 'consumed_or_started', 'started'))
                    and value.get('native_completed') is not True)
            db.execute('UPDATE records SET active=? WHERE namespace=? AND kind=? AND key=?',
                       (int(pending), self.namespace, kind, key))

    def record(self, db, key, entry, *, active=True):
        row = db.execute("SELECT value FROM records WHERE namespace=? AND kind='threads' AND key=?",
                         (self.namespace, key)).fetchone()
        if row is not None:
            prior = json.loads(row[0])
            if (not isinstance(prior, dict) or prior.get('target') != entry['target']
                    or prior.get('event_fingerprint') != entry['event_fingerprint']):
                raise ValueError('reply_ticket_mapping_collision')
            return  # Re-caching or re-sending must never reopen a closed ticket.
        if active:
            thread_id = entry.get('target', {}).get('thread_id')
            try:
                canonical = str(uuid.UUID(thread_id))
                if canonical != thread_id.lower():
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('reply_ticket_thread_id_invalid') from None
            if not self._live(entry.get('created_at'), self._current()):
                return  # Expired, future or unknown births cannot open a parent.
        self.put(db, 'threads', key, entry)
        if not active:
            return
        db.execute("UPDATE records SET active=1 WHERE namespace=? AND kind='threads' AND key=?",
                   (self.namespace, key))
        # Bounded eviction per new parent; evicted rows stay present and closed.
        ranked = self._ranked_active(db, canonical)
        if ranked is not None:
            self._cutoff(db, canonical, ranked)
        excess = ranked[ACTIVE_TICKET_LIMIT:ACTIVE_TICKET_LIMIT+EVICTION_BUDGET] if ranked is not None else ()
        for namespace, old_key, _, _ in excess:
            self.retire_projection(db, namespace, old_key)
            db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                       (namespace, old_key))

    def claim(self, db, key):
        if self.get(db, 'threads', key) is None:
            return False
        if self.authority is not None:
            claimed = db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' "
                                 "AND key=? AND active=1 AND thread_id COLLATE NOCASE=?",
                                 (self.namespace, key, self.authority.thread_id)).rowcount == 1
        else:
            claimed = db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' "
                                 "AND key=? AND active=1", (self.namespace, key)).rowcount == 1
        if claimed:
            self.retire_projection(db, self.namespace, key)
        return claimed

    def active(self, db):
        if self.authority is not None:
            rows = db.execute(
                "SELECT key,value,created_at FROM records INDEXED BY session_active_tickets "
                "WHERE namespace=? AND kind='threads' AND active=1 AND thread_id COLLATE NOCASE=? "
                "ORDER BY created_at,key", (self.namespace, self.authority.thread_id)).fetchall()
        else:
            rows = db.execute(
                "SELECT key,value,created_at FROM records INDEXED BY namespace_active_tickets "
                "WHERE namespace=? AND kind='threads' AND active=1 "
                "ORDER BY created_at,key", (self.namespace,)).fetchall()
        current = self._current()
        from .session_activity import SessionActivity
        lifecycle = SessionActivity(self.authority.runtime if self.authority is not None else self.runtime)
        result = {}
        for key, raw, created in rows:
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    continue
                if not self._live(created, current):
                    # Logically invalid: close in place, never delete or reopen.
                    self.retire_projection(db, self.namespace, key)
                    db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                               (self.namespace, key))
                    continue
                if not self._eligible(db, key, value.get('target', {}).get('thread_id')):
                    continue
                try:
                    enabled = lifecycle.is_active(str(uuid.UUID(value['target']['thread_id'])))
                except (ValueError, OSError):
                    enabled = True  # Optional tracking uncertainty cannot erase current auth.
                if not enabled:
                    continue
            except (ValueError, KeyError, TypeError, OSError):
                continue
            result[key] = value
        return result
