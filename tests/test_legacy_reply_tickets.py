"""Schema2 compatibility journal: no migration, pruning, deletion or reopening."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from codex_watchdog.legacy_reply_tickets import LegacyReplyTickets, select_reply_journal
from codex_watchdog.reply_tickets import ReplyTickets
from codex_watchdog.session_activity import SessionActivity
from codex_watchdog.storage import FileLock, InstructionStore, StoreBusyError

# Half-second precision keeps float timestamp arithmetic exact at 72 hours.
NOW = datetime(2026, 10, 14, 12, 0, 0, 500000, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
TTL = timedelta(hours=72)
SESSION = '6f1c3e1a-2b4d-4c8e-9a10-1b2c3d4e5f60'
OTHER = '0a9b8c7d-6e5f-4a3b-8c2d-1e0f9a8b7c6d'
NAMESPACE = 'slack/relay-state.json'
SECOND = 'slack/second-state.json'
MARKER = json.dumps({'storage': 'reply-tickets.sqlite3', 'schema_version': 2, 'active_limit': 4,
                     'active_scope': 'provider_session', 'legacy_policy': 'retired_all',
                     'migrated_at': '2026-09-01T00:00:00Z', 'operator_note': {'kept': True}},
                    indent=1).encode()
CURRENT_ONLY = {'recent_records', 'current_boundaries', 'recent_expiry', 'recent_pool',
                'recent_session_expiry', 'namespace_pending_bindings'}


def stamp(delta=timedelta()):
    return (NOW + delta).isoformat().replace('+00:00', 'Z')


def ticket(session, created, fingerprint='fp', **extra):
    return dict(ticket_schema=1, created_at=created, event_fingerprint=fingerprint,
                target=dict(thread_id=session), **extra)


def raw(value):
    # Unsorted, indented synthetic text: any rewrite of retained rows is visible.
    return json.dumps(value, indent=2)


def row(kind, key, value, *, namespace=NAMESPACE, fingerprint=None, thread=None, active=0, created=None):
    return (namespace, kind, key, raw(value), fingerprint, thread, active, created)


def thread_row(key, session, created, *, active=1, fingerprint='fp', **extra):
    return row('threads', key, ticket(session, created, fingerprint, **extra), fingerprint=fingerprint,
               thread=session, active=active, created=created)


def build_schema2(database, rows=(), sources=(NAMESPACE,)):
    """Explicit previous-public schema2 layout, not a downgraded schema3 store."""
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(database))) as db:
        db.execute('CREATE TABLE sources (namespace TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE records (namespace TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, '
                   'value TEXT NOT NULL, fingerprint TEXT, thread_id TEXT, '
                   'active INTEGER NOT NULL DEFAULT 0, created_at TEXT, PRIMARY KEY(namespace,kind,key))')
        db.execute('CREATE INDEX notification_lookup ON records(namespace,kind,fingerprint)')
        db.execute('CREATE INDEX thread_lookup ON records(namespace,kind,thread_id)')
        db.execute('CREATE INDEX session_active_tickets ON records(thread_id COLLATE NOCASE,created_at,namespace,key) '
                   "WHERE active=1 AND kind='threads'")
        db.execute('CREATE INDEX namespace_active_tickets ON records(namespace,created_at,key) '
                   "WHERE active=1 AND kind='threads'")
        for namespace in sources:
            db.execute('INSERT INTO sources VALUES (?)', (namespace,))
        for entry in rows:
            db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?)', entry)
        db.execute('PRAGMA user_version=2')
        db.commit()


def snapshot(database):
    with closing(sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True)) as db:
        return dict(version=db.execute('PRAGMA user_version').fetchone()[0],
                    master=db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall(),
                    sources=db.execute('SELECT namespace FROM sources ORDER BY namespace').fetchall(),
                    records=db.execute('SELECT * FROM records ORDER BY namespace,kind,key').fetchall())


def records(database):
    return {entry[:3]: entry for entry in snapshot(database)['records']}


class RecordingLock:
    def __init__(self, path, log):
        self.lock, self.log = FileLock(path), log

    def __enter__(self):
        self.lock.__enter__()
        self.log.append('locked')
        return self

    def __exit__(self, *exc):
        self.log.append('released')
        return self.lock.__exit__(*exc)


class Authority:
    def __init__(self, runtime, thread_id):
        self.runtime, self.thread_id, self.store = Path(runtime), thread_id, None
        self.lock_path, self.log = Path(runtime) / 'slack' / 'authority.lock', []

    def journal_lock(self, provider):
        return RecordingLock(self.lock_path, self.log)


class LegacyReplyTicketsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        (self.runtime / 'slack').mkdir()
        self.path = self.runtime / NAMESPACE
        self.lock = self.runtime / 'slack' / 'reply-tickets.lock'
        self.database = self.runtime / 'slack' / 'reply-tickets.sqlite3'
        self.now = NOW.timestamp()
        patcher = mock.patch.object(SessionActivity, 'is_active', return_value=True)
        self.activity = patcher.start()
        self.addCleanup(patcher.stop)

    def reader(self):
        raise AssertionError('compatibility mode must never import legacy JSON')

    def arguments(self, path=None):
        return (path or self.path, self.lock, 'slack', self.runtime, self.reader)

    def journal(self, path=None, **kwargs):
        return LegacyReplyTickets(*self.arguments(path), clock=lambda: self.now, **kwargs)

    def fixture(self, *rows, marker=True, sources=(NAMESPACE,)):
        build_schema2(self.database, rows, sources)
        if marker:
            self.path.write_bytes(MARKER)
        return snapshot(self.database)

    def assert_schema2_only(self, before):
        after = snapshot(self.database)
        self.assertEqual(after['version'], 2)
        self.assertEqual(after['master'], before['master'])
        self.assertFalse({entry[1] for entry in after['master']} & CURRENT_ONLY)

    def test_selection_is_a_read_only_hint_that_never_creates_a_database(self):
        selected = select_reply_journal(*self.arguments(), clock=lambda: self.now)
        self.assertIs(type(selected), ReplyTickets)
        self.assertFalse(self.database.exists())
        before = self.fixture(thread_row('t-live', SESSION, stamp(-HOUR)))
        content = self.database.read_bytes()
        selected = select_reply_journal(*self.arguments(), clock=lambda: self.now)
        self.assertIs(type(selected), LegacyReplyTickets)
        self.assertEqual(self.database.read_bytes(), content)
        self.assertEqual(snapshot(self.database), before)
        self.assertEqual(self.path.read_bytes(), MARKER)

    def test_independent_receipt_commits_despite_old_uncertainty_without_callback(self):
        before = self.fixture(thread_row('t-expired', SESSION, stamp(-TTL - HOUR), active=0),
                              row('events', 'e-uncertain', {'state': 'uncertain', 'unknown_receipt': 7}, active=1),
                              row('slack_bot_grants', 'grant-1', {'principal': 'U1', 'scope': 'exact'}))
        calls, fences = [], []

        @contextmanager
        def fence():
            fences.append('entered')
            yield

        journal = select_reply_journal(*self.arguments(), clock=lambda: self.now,
                                       before_prune=lambda *args: calls.append(args) or False,
                                       retention_fence=fence)
        self.assertIsInstance(journal, LegacyReplyTickets)
        with journal.transaction() as db:
            journal.put(db, 'events', 'e-new', {'state': 'delivered', 'native_completed': True})
        self.assertEqual(calls, [])
        self.assertEqual(fences, ['entered'])
        after = records(self.database)
        for entry in before['records']:
            self.assertEqual(after[entry[:3]], entry)
        self.assertEqual(json.loads(after[(NAMESPACE, 'events', 'e-new')][3]),
                         {'state': 'delivered', 'native_completed': True})
        self.assertEqual(self.path.read_bytes(), MARKER)
        self.assert_schema2_only(before)

    def test_reads_and_put_retain_bytes_birth_unknown_fields_and_marker(self):
        closed = thread_row('t-closed', SESSION, stamp(-100 * HOUR), active=0, fingerprint='fp-old',
                            legacy_unknown={'keep': [1, 2]})
        grant = row('slack_bot_grants', 'grant-1', {'scope': 'exact', 'principal': 'U1', 'extra': None})
        cursor = row('relay_cursors', 'C1', {'cursor': '1700000000.000100'})
        before = self.fixture(closed, grant, cursor)
        journal = self.journal()
        with journal.transaction() as db:
            self.assertEqual(journal.get(db, 'slack_bot_grants', 'grant-1'), json.loads(grant[3]))
            self.assertEqual(journal.get(db, 'relay_cursors', 'C1'), json.loads(cursor[3]))
            self.assertIsNone(journal.get(db, 'threads', 't-closed'))
            self.assertEqual(journal.mappings(db, fingerprint='fp-old'), [json.loads(closed[3])])
            refreshed = dict(json.loads(closed[3]), created_at=stamp(), refreshed=True)
            journal.put(db, 'threads', 't-closed', refreshed)
            self.assertIsNone(journal.get(db, 'threads', 't-closed'))
            self.assertFalse(journal.claim(db, 't-closed'))
        after = records(self.database)
        self.assertEqual(after[(NAMESPACE, 'slack_bot_grants', 'grant-1')], grant)
        self.assertEqual(after[(NAMESPACE, 'relay_cursors', 'C1')], cursor)
        rewritten = after[(NAMESPACE, 'threads', 't-closed')]
        self.assertEqual(rewritten[7], closed[7])  # Original birth is immutable.
        self.assertEqual(rewritten[6], 0)
        self.assertEqual(json.loads(rewritten[3]), refreshed)
        self.assertEqual(self.path.read_bytes(), MARKER)
        self.assert_schema2_only(before)

    def test_new_namespace_registration_publishes_marker_after_commit(self):
        before = self.fixture()
        second = self.runtime / SECOND
        journal = self.journal(second)
        with mock.patch.object(InstructionStore, '_atomic_json', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                with journal.transaction():
                    self.fail('body must not run before the marker is published')
        self.assertIn((SECOND,), snapshot(self.database)['sources'])
        self.assertFalse(second.exists())
        with journal.transaction() as db:
            journal.put(db, 'events', 'e-second', {'state': 'delivered'})
        marker = second.read_bytes()
        value = json.loads(marker)
        self.assertLessEqual(len(marker), 16384)
        self.assertEqual((value['schema_version'], value['storage'], value['active_limit'], value['active_scope']),
                         (2, 'reply-tickets.sqlite3', 4, 'provider_session'))
        with journal.transaction():
            pass
        self.assertEqual(second.read_bytes(), marker)
        self.assertEqual(self.path.read_bytes(), MARKER)
        self.assertIn((SECOND, 'events', 'e-second'), records(self.database))
        self.assert_schema2_only(before)

    def test_unregistered_namespace_with_existing_json_is_refused(self):
        before = self.fixture()
        second = self.runtime / SECOND
        legacy = json.dumps({'schema_version': 1, 'threads': {}}).encode()
        second.write_bytes(legacy)
        with self.assertRaises(ValueError) as caught:
            with self.journal(second).transaction():
                self.fail('unknown legacy JSON must not be ignored')
        self.assertEqual(str(caught.exception), 'reply_ticket_namespace_explicit_import_required')
        self.assertEqual(second.read_bytes(), legacy)
        self.assertEqual(snapshot(self.database), before)

    def test_unknown_or_oversized_markers_are_refused_without_change(self):
        before = self.fixture(marker=False)
        base = json.loads(MARKER)
        padded = dict(base, pad='')
        padded['pad'] = 'x' * (16384 - len(json.dumps(padded).encode()))
        exact = json.dumps(padded).encode()
        self.assertEqual(len(exact), 16384)
        refused = [json.dumps({'schema_version': 1, 'threads': {}}).encode(),
                   json.dumps(dict(base, schema_version=3, ticket_ttl_seconds=259200, recent_limit=256)).encode(),
                   json.dumps({k: v for k, v in base.items() if k != 'active_scope'}).encode(),
                   json.dumps(dict(base, active_limit=5)).encode(),
                   json.dumps(dict(padded, pad=padded['pad'] + 'x')).encode(),
                   b'[' * 16384, b'not json']
        for payload in refused:
            with self.subTest(payload=payload[:40]):
                self.path.write_bytes(payload)
                with self.assertRaises(ValueError):
                    with self.journal().transaction():
                        self.fail('unknown marker must be refused')
                self.assertEqual(self.path.read_bytes(), payload)
                self.assertEqual(snapshot(self.database), before)
        self.path.write_bytes(exact)
        with self.journal().transaction():
            pass
        self.assertEqual(self.path.read_bytes(), exact)
        self.path.unlink()
        with self.journal().transaction():
            pass  # Committed source: absent marker is repaired.
        self.assertEqual(json.loads(self.path.read_bytes())['schema_version'], 2)
        self.assert_schema2_only(before)

    def test_symlinked_marker_is_refused(self):
        before = self.fixture(marker=False)
        target = self.runtime / 'elsewhere.json'
        target.write_bytes(MARKER)
        try:
            os.symlink(target, self.path)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        with self.assertRaises(ValueError):
            with self.journal().transaction():
                self.fail('redirected marker must be refused')
        self.assertEqual(target.read_bytes(), MARKER)
        self.assertEqual(snapshot(self.database), before)

    def test_schema_change_after_selection_refuses_without_write(self):
        self.fixture(thread_row('t-live', SESSION, stamp(-HOUR)))
        journal = select_reply_journal(*self.arguments(), clock=lambda: self.now)
        self.assertIsInstance(journal, LegacyReplyTickets)
        with closing(sqlite3.connect(str(self.database))) as db:
            db.execute('PRAGMA user_version=3')
            db.commit()
        content = self.database.read_bytes()
        with self.assertRaises(ValueError) as caught:
            with journal.transaction():
                self.fail('stale schema2 selection must not run')
        self.assertEqual(str(caught.exception), 'reply_ticket_legacy_schema_changed')
        self.assertEqual(self.database.read_bytes(), content)
        self.assertEqual(self.path.read_bytes(), MARKER)
        reselected = select_reply_journal(*self.arguments(), clock=lambda: self.now)
        self.assertIs(type(reselected), ReplyTickets)

    def test_schema2_with_current_only_tables_is_refused(self):
        self.fixture()
        with closing(sqlite3.connect(str(self.database))) as db:
            db.execute('CREATE TABLE recent_records (namespace TEXT)')
            db.commit()
        before = snapshot(self.database)
        with self.assertRaises(ValueError) as caught:
            with self.journal().transaction():
                self.fail('ambiguous schema must not run')
        self.assertEqual(str(caught.exception), 'reply_ticket_legacy_schema_invalid')
        self.assertEqual(snapshot(self.database), before)

    def test_missing_database_is_not_created_by_compatibility_journal(self):
        with self.assertRaises(ValueError):
            with self.journal().transaction():
                self.fail('missing schema2 database must not be created')
        self.assertFalse(self.database.exists())

    def test_absolute_ttl_uses_original_birth_and_precise_clock(self):
        before = self.fixture(thread_row('t-old-exact', SESSION, stamp(-TTL)),
                              thread_row('t-old-future', SESSION, stamp(timedelta(seconds=1))))
        journal = self.journal()
        almost = stamp(-TTL + timedelta(minutes=1))
        with journal.transaction() as db:
            journal.record(db, 'k-71h59m', ticket(SESSION, almost))
            journal.record(db, 'k-72h', ticket(SESSION, stamp(-TTL)))
            journal.record(db, 'k-future', ticket(SESSION, stamp(timedelta(microseconds=1))))
            self.assertEqual(journal.get(db, 'threads', 'k-71h59m')['created_at'], almost)
            for key in ('k-72h', 'k-future', 't-old-exact', 't-old-future'):
                self.assertIsNone(journal.get(db, 'threads', key))
                self.assertFalse(journal.claim(db, key))
            self.assertEqual(set(journal.active(db)), {'k-71h59m'})
        after = records(self.database)
        self.assertNotIn((NAMESPACE, 'threads', 'k-72h'), after)
        self.assertNotIn((NAMESPACE, 'threads', 'k-future'), after)
        for entry in before['records']:
            self.assertEqual(after[entry[:3]], entry[:6] + (0,) + entry[7:])
        self.now += 60
        with journal.transaction() as db:
            self.assertIsNone(journal.get(db, 'threads', 'k-71h59m'))
            self.assertEqual(journal.active(db), {})
        self.assertEqual(records(self.database)[(NAMESPACE, 'threads', 'k-71h59m')][6], 0)
        self.now = float('nan')
        with journal.transaction() as db:
            with self.assertRaises(ValueError):
                journal.get(db, 'threads', 'k-71h59m')
        self.assertEqual(len(records(self.database)), 3)
        self.assert_schema2_only(before)

    def test_four_parents_per_exact_session_and_cross_uuid_isolation(self):
        others = [thread_row('o-%d' % index, OTHER, stamp(-index * HOUR)) for index in range(1, 5)]
        upper = thread_row('t-upper', SESSION.upper(), stamp(-10 * HOUR))
        self.fixture(upper, *others)
        journal = self.journal()
        with journal.transaction() as db:
            for index in range(5, 1, -1):
                journal.record(db, 'k-%d' % index, ticket(SESSION, stamp(-index * HOUR)))
            self.assertEqual(set(journal.active(db)) - {'o-1', 'o-2', 'o-3', 'o-4'}, {'k-2', 'k-3', 'k-4', 'k-5'})
            journal.record(db, 'k-1', ticket(SESSION, stamp(-HOUR)))
            journal.record(db, 'k-5', ticket(SESSION, stamp(-5 * HOUR)))  # Evicted: never reopened.
            self.assertFalse(journal.claim(db, 'k-5'))
        after = records(self.database)
        active = {key[2] for key, entry in after.items() if entry[6] == 1}
        self.assertEqual(active, {'k-1', 'k-2', 'k-3', 'k-4', 'o-1', 'o-2', 'o-3', 'o-4'})
        self.assertEqual(after[upper[:3]], upper[:6] + (0,) + upper[7:])
        self.assertEqual(after[(NAMESPACE, 'threads', 'k-5')][6], 0)
        for entry in others:
            self.assertEqual(after[entry[:3]], entry)

    def test_eviction_touches_at_most_32_rows_per_new_parent(self):
        crowded = [thread_row('c-%02d' % index, SESSION, stamp(-60 * HOUR + index * timedelta(minutes=1)))
                   for index in range(40)]
        self.fixture(*crowded)
        journal = self.journal()
        with journal.transaction() as db:
            journal.record(db, 'k-new', ticket(SESSION, stamp(-HOUR)))
        after = records(self.database)
        active = sorted(key[2] for key, entry in after.items() if entry[6] == 1)
        self.assertEqual(sum(entry[1] == 'threads' for entry in after.values()), 41)
        # One current boundary prevents old survivors becoming eligible when
        # newer one-shot roots close; no historical ticket was deleted.
        self.assertEqual(sum(entry[1] == 'provider_floors' for entry in after.values()), 1)
        self.assertEqual(active, ['c-00', 'c-01', 'c-02', 'c-03', 'c-04', 'c-37', 'c-38', 'c-39', 'k-new'])

    def test_authority_session_scope_quarantine_and_pending_flag(self):
        mine = thread_row('t-mine', SESSION, stamp(-HOUR))
        foreign = thread_row('t-foreign', OTHER, stamp(-HOUR))
        old_event = row('events', 'e-old', {'state': 'uncertain', 'unknown': 1}, thread=SESSION, active=1)
        quarantine = row('events', 'e-quarantine', {'relay_authority_quarantine': True})
        self.fixture(mine, foreign, old_event, quarantine)
        authority = Authority(self.runtime, SESSION)
        journal = self.journal(authority=authority)
        with journal.transaction() as db:
            self.assertEqual(journal.get(db, 'threads', 't-mine')['target']['thread_id'], SESSION)
            self.assertIsNone(journal.get(db, 'threads', 't-foreign'))
            self.assertFalse(journal.claim(db, 't-foreign'))
            self.assertEqual(set(journal.active(db)), {'t-mine'})
            with self.assertRaises(ValueError) as caught:
                journal.get(db, 'events', 'e-quarantine')
            self.assertEqual(str(caught.exception), 'relay_authority_historical_identity_quarantined')
            with self.assertRaises(ValueError) as caught:
                journal.put(db, 'events', 'e-foreign', {'target': {'thread_id': OTHER}})
            self.assertEqual(str(caught.exception), 'relay_authority_foreign_session')
            journal.put(db, 'events', 'e-new', {'state': 'uncertain'})
            journal.put(db, 'events', 'e-done', {'state': 'dispatching', 'native_completed': True})
            self.assertTrue(journal.claim(db, 't-mine'))
            self.assertFalse(journal.claim(db, 't-mine'))
        after = records(self.database)
        self.assertEqual(after[(NAMESPACE, 'events', 'e-new')][5:7], (SESSION, 1))
        self.assertEqual(after[(NAMESPACE, 'events', 'e-done')][6], 0)
        self.assertNotIn((NAMESPACE, 'events', 'e-foreign'), after)
        self.assertEqual(after[old_event[:3]], old_event)
        self.assertEqual(after[quarantine[:3]], quarantine)
        self.assertEqual(after[foreign[:3]], foreign)
        self.assertEqual(after[mine[:3]], mine[:6] + (0,) + mine[7:])
        self.assertEqual(authority.log, ['locked', 'released'])

    def test_stale_authority_binding_is_refused_under_the_actual_lock(self):
        before = self.fixture(thread_row('t-mine', SESSION, stamp(-HOUR)))
        authority = Authority(self.runtime, SESSION)
        journal = self.journal(authority=authority)
        moved = self.runtime / 'moved'
        (moved / 'slack').mkdir(parents=True)
        authority.runtime = moved
        with self.assertRaises(ValueError) as caught:
            with journal.transaction():
                self.fail('stale authority must not open the journal')
        self.assertEqual(str(caught.exception), 'reply_ticket_authority_binding_stale')
        self.assertEqual(authority.log, ['locked', 'released'])
        self.assertFalse((moved / 'slack' / 'reply-tickets.sqlite3').exists())
        self.assertEqual(snapshot(self.database), before)
        self.assertEqual(self.path.read_bytes(), MARKER)

    def test_rollback_busy_and_base_exception_keep_the_journal_unchanged(self):
        before = self.fixture(thread_row('t-live', SESSION, stamp(-HOUR)))
        journal = self.journal()

        class Interrupted(BaseException):
            pass

        for failure in (RuntimeError('body failed'), Interrupted()):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(type(failure)):
                    with journal.transaction() as db:
                        journal.put(db, 'events', 'e-rolled-back', {'state': 'uncertain'})
                        self.assertTrue(journal.claim(db, 't-live'))
                        raise failure
                self.assertEqual(snapshot(self.database), before)
        holder = sqlite3.connect(str(self.database), timeout=0)
        try:
            holder.execute('BEGIN IMMEDIATE')
            with self.assertRaises(StoreBusyError):
                with journal.transaction():
                    self.fail('busy journal must not run the body')
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(snapshot(self.database), before)
        self.assert_schema2_only(before)

    def test_dormant_session_hides_without_closing(self):
        before = self.fixture(thread_row('t-live', SESSION, stamp(-HOUR)))
        journal = self.journal()
        self.activity.return_value = False
        with journal.transaction() as db:
            self.assertIsNone(journal.get(db, 'threads', 't-live'))
            self.assertEqual(journal.active(db), {})
            self.assertFalse(journal.claim(db, 't-live'))
        self.assertEqual(snapshot(self.database), before)
        self.activity.return_value = True
        with journal.transaction() as db:
            self.assertEqual(set(journal.active(db)), {'t-live'})
            self.assertTrue(journal.claim(db, 't-live'))
            journal.record(db, 't-live', ticket(SESSION, stamp(-HOUR)))
            self.assertFalse(journal.claim(db, 't-live'))
        self.assertEqual(records(self.database)[(NAMESPACE, 'threads', 't-live')][6], 0)

    def test_collision_and_closed_parent_are_never_reopened(self):
        before = self.fixture(thread_row('t-closed', SESSION, stamp(-HOUR), active=0))
        journal = self.journal()
        with journal.transaction() as db:
            with self.assertRaises(ValueError) as caught:
                journal.record(db, 't-closed', ticket(OTHER, stamp(-HOUR)))
            self.assertEqual(str(caught.exception), 'reply_ticket_mapping_collision')
            journal.record(db, 't-closed', ticket(SESSION, stamp()))
            self.assertFalse(journal.claim(db, 't-closed'))
            self.assertEqual(journal.active(db), {})
            with self.assertRaises(ValueError) as caught:
                journal.record(db, 'k-bad', ticket('not-a-uuid', stamp()))
            self.assertEqual(str(caught.exception), 'reply_ticket_thread_id_invalid')
        self.assertEqual(snapshot(self.database), before)

    def test_explicit_migration_entry_points_are_unavailable(self):
        before = self.fixture(thread_row('t-live', SESSION, stamp(-HOUR)))
        journal = self.journal()
        with self.assertRaises(ValueError):
            journal.migrate_recent_state()
        with journal.transaction() as db:
            journal.protect(db, 'threads', 't-live')
        self.assertEqual(snapshot(self.database), before)
        self.assertEqual(self.path.read_bytes(), MARKER)


if __name__ == '__main__':
    unittest.main()
