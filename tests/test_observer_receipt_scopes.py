"""Real disposable SQLite tests for observer receipt scopes on finite families.

Everything runs in temporary runtimes prepared by the existing explicit cold
finite preparation. Historical rows are seeded directly into those temporary
databases; no provider, native session, setting or credential is touched.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from codex_watchdog import notification_receipt_retention as finite
from codex_watchdog import observer_receipt_scopes as observer
from codex_watchdog.notification_context import OBSERVER_PREFIX

BORN = 1_000_000.0
TTL = finite.TTL
OBSERVED = OBSERVER_PREFIX + '*'


def digest(label):
    return hashlib.sha256(label.encode()).hexdigest()


def subject(scope):
    return OBSERVER_PREFIX + scope


A, B, C, S = (digest('scope-' + name) for name in 'abcs')
P = digest('scope-protected')
NATIVE = [str(uuid.UUID(int=index + 1)) for index in range(8)]


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def context(scope, label, born, provider='slack'):
    return dict(schema_version=2, subject_kind='observer', observer_scope_sha256=scope, scope_sha256=scope,
                fingerprint=digest(label), born=born,
                providers={provider: dict(target_sha256=digest('target'), payload_sha256=digest('payload-' + label))})


def meta(db):
    return json.loads(db.execute('SELECT value FROM finite_metadata WHERE id=1').fetchone()[0])


def put(db, value):
    db.execute('UPDATE finite_metadata SET value=? WHERE id=1',
               (value if isinstance(value, str) else finite._json(value),))


def dump(db):
    return list(db.iterdump())


def seed(db, fingerprint, provider, thread, scope, born, protected=0, target=None):
    db.execute('INSERT OR IGNORE INTO receipts(fingerprint) VALUES(?)', (fingerprint,))
    db.execute('UPDATE receipts SET ' + provider + "='sent' WHERE fingerprint=?", (fingerprint,))
    db.execute('INSERT INTO retention VALUES(?,?,?,?,?,?,?,?,?)',
               (fingerprint, provider, thread, scope, born, born + TTL, protected, target or digest('target'),
                digest('payload')))


def boundary(db, thread, scope, provider, floor, checked):
    db.execute("INSERT INTO boundaries VALUES(?,?,?,?,'~',?)", (thread, scope, provider, floor, checked))


def boundaries(db, scope):
    return db.execute('SELECT provider,floor,floor_key,checked FROM boundaries WHERE thread_id=? AND scope=? '
                      'ORDER BY provider', (subject(scope), scope)).fetchall()


def guarded(db):
    """Byte renderings of every native or protected row the helper must keep."""
    retention = db.execute(
        'SELECT quote(fingerprint),quote(provider),quote(thread_id),quote(scope),quote(born),quote(expires),'
        'quote(protected),quote(target_sha256),quote(payload_sha256) FROM retention '
        'WHERE thread_id NOT GLOB ? OR protected<>0 ORDER BY fingerprint,provider', (OBSERVED,)).fetchall()
    bounds = db.execute(
        'SELECT quote(thread_id),quote(scope),quote(provider),quote(floor),quote(floor_key),quote(checked) '
        'FROM boundaries WHERE thread_id NOT GLOB ? OR scope=? ORDER BY thread_id,scope,provider',
        (OBSERVED, P)).fetchall()
    receipts = db.execute(
        'SELECT quote(fingerprint),quote(slack),quote(lark) FROM receipts WHERE fingerprint IN '
        '(SELECT fingerprint FROM retention WHERE thread_id NOT GLOB ? OR protected<>0) ORDER BY fingerprint',
        (OBSERVED,)).fetchall()
    return retention, bounds, receipts


def measure(db, action):
    ticks, statements = [0], []

    def tick():
        ticks[0] += 1
        return 0

    db.set_progress_handler(tick, 1)
    db.set_trace_callback(statements.append)
    try:
        result = action()
    finally:
        db.set_progress_handler(None, 1)
        db.set_trace_callback(None)
    return ticks[0], len(statements), result


def plan(db, sql, args):
    return [row[-1] for row in db.execute('EXPLAIN QUERY PLAN ' + sql, args)]


class ObserverReceiptScopeTests(unittest.TestCase):
    def prepared(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name).resolve() / 'runtime'
        runtime.mkdir()
        scopes = [(subject(A), A, provider) for provider in finite.PROVIDERS['dual'] + finite.PROVIDERS['fallback']]
        result = finite.prepare_notification_receipts(runtime, scopes=scopes, contexts=(),
                                                      sender_fence=contextlib.nullcontext, clock=lambda: BORN)
        self.assertEqual(result['status'], 'prepared')
        # Outer cold preparation now initializes this extension. These helper
        # tests intentionally start at the previous prepared finite schema so
        # their unchanged cold/unprepared assertions exercise that upgrade.
        for family in finite.PROVIDERS:
            with sqlite3.connect(runtime / 'notifications' / (finite.NAMES[family] + '.sqlite3')) as db:
                value = meta(db)
                del value['observer_retention']
                put(db, value)
                for name, _, __, ___ in observer._INDEXES:
                    db.execute('DROP INDEX ' + name)
        return runtime / 'notifications'

    def store(self, directory, family='dual', clock=None, receipt_context=None):
        db = sqlite3.connect(str(directory / (finite.NAMES[family] + '.sqlite3')), isolation_level=None)
        self.addCleanup(db.close)
        return finite.FiniteReceipts(db, family, receipt_context=receipt_context, clock=clock or Clock(BORN))

    def cold(self, store, born=BORN):
        store.db.execute('BEGIN IMMEDIATE')
        try:
            result = observer.prepare_observer_scopes(store, born)
            self.assertTrue(store.db.in_transaction)
        except BaseException:
            store.db.rollback()
            raise
        store.db.commit()
        return result

    def ready(self, family='dual', clock=None):
        directory = self.prepared()
        store = self.store(directory, family, clock)
        self.cold(store)
        return directory, store

    def refuses_cold(self, store, born=BORN):
        before = dump(store.db)
        store.db.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(finite.RetentionError):
                observer.prepare_observer_scopes(store, born)
            self.assertEqual(dump(store.db), before)
        finally:
            store.db.rollback()
        self.assertEqual(dump(store.db), before)

    def refuses_maintain(self, store, scopes, **kwargs):
        before = dump(store.db)
        with self.assertRaises(finite.RetentionError):
            observer.maintain_observer_scopes(store, scopes, **kwargs)
        self.assertFalse(store.db.in_transaction)
        self.assertEqual(dump(store.db), before)

    def refuses_hot(self, store, scopes=()):
        self.refuses_maintain(store, scopes)
        before = dump(store.db)
        store.db.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(finite.RetentionError):
                observer.observer_clock(store)
            self.assertEqual(dump(store.db), before)
        finally:
            store.db.rollback()

    def test_cold_preparation_is_explicit_idempotent_and_preserves_unknown_fields(self):
        directory = self.prepared()
        store = self.store(directory, clock=Clock(BORN + 50))
        put(store.db, dict(meta(store.db), future={'keep': [1, 'two']}))
        before = dump(store.db)
        with self.assertRaises(finite.RetentionError):
            observer.prepare_observer_scopes(store, BORN)  # caller must own the transaction
        self.assertEqual(dump(store.db), before)
        store.db.execute('BEGIN IMMEDIATE')
        observer.prepare_observer_scopes(store, BORN)
        self.assertTrue(store.db.in_transaction)  # never commits the caller's transaction
        store.db.rollback()
        self.assertEqual(dump(store.db), before)

        self.assertEqual(self.cold(store)['created'], 3)
        value = meta(store.db)
        self.assertEqual(value['future'], {'keep': [1, 'two']})
        self.assertEqual(value['observer_retention'], dict(schema_version=1, floor=BORN, checked=BORN))
        names = tuple(name for name, _, __, ___ in observer._INDEXES)
        stored = dict(store.db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN (?,?,?)",
                                       names))
        self.assertEqual(stored, {name: sql for name, _, __, sql in observer._INDEXES})

        put(store.db, dict(value, observer_retention=dict(value['observer_retention'], note='nested')))
        store.db.execute('BEGIN IMMEDIATE')
        self.assertEqual(observer.observer_clock(store), BORN + 50)
        store.db.commit()
        again = self.cold(store)
        self.assertEqual((again['status'], again['created'], again['checked']), ('unchanged', 0, BORN + 50))
        value = meta(store.db)
        self.assertEqual(value['observer_retention'],
                         dict(schema_version=1, floor=BORN, checked=BORN + 50, note='nested'))
        self.assertEqual(value['future'], {'keep': [1, 'two']})
        finite.FiniteReceipts(store.db, 'dual')  # original validation still accepts the family
        self.refuses_cold(store, BORN + 1)  # a different floor is never rewritten

        store.db.execute('DROP INDEX retention_subject')
        self.refuses_hot(store)  # hot paths never create or repair indexes
        self.assertEqual(self.cold(store)['created'], 1)
        self.assertEqual(observer.maintain_observer_scopes(store, ())['status'], 'maintained')

    def test_invalid_born_metadata_and_size_refuse_without_mutation(self):
        directory = self.prepared()
        store = self.store(directory)
        self.refuses_hot(store)  # missing observer metadata is permitted only in cold preparation
        for born in (True, float('nan'), float('inf'), -1, '1000000', None):
            with self.subTest(born=born):
                self.refuses_cold(store, born)
        original = meta(store.db)
        base = len(finite._json(dict(original, pad='')).encode())
        put(store.db, dict(original, pad='x' * (1000 - base)))
        self.refuses_cold(store)  # augmentation would exceed 1024 encoded bytes
        plan_hash = original['plan_sha256']
        observed = dict(schema_version=1, floor=BORN, checked=BORN)
        bad_values = [
            finite._json(dict(original, plan_sha256=digest('other-plan'))),
            finite._json(dict(original, family='fallback')),
            finite._json(dict(original, schema_version=2)),
            finite._json(dict(original, observer_retention=dict(schema_version=1, floor=BORN))),
            finite._json(dict(original, observer_retention=dict(schema_version=True, floor=BORN, checked=BORN))),
            finite._json(dict(original, observer_retention=dict(schema_version=2, floor=BORN, checked=BORN))),
            finite._json(dict(original, observer_retention=dict(schema_version=1, floor=BORN + 1, checked=BORN))),
            finite._json(dict(original, observer_retention=dict(schema_version=1, floor=-1.0, checked=BORN))),
            finite._json(dict(original, observer_retention=dict(schema_version=1, floor=True, checked=BORN))),
            finite._json(dict(original, observer_retention=[1])),
            finite._json(dict(original, observer_retention=observed, pad='y' * 1100)),
            '{"schema_version":1,"schema_version":1,"family":"dual","plan_sha256":"%s"}' % plan_hash,
            ('{"family":"dual","plan_sha256":"%s","schema_version":1,'
             '"observer_retention":{"schema_version":1,"floor":NaN,"checked":1}}') % plan_hash,
            ('{"family":"dual","plan_sha256":"%s","schema_version":1,'
             '"observer_retention":{"schema_version":1,"floor":1e999,"checked":1e999}}') % plan_hash,
            'not json',
        ]
        for value in bad_values:
            with self.subTest(value=value[:96]):
                put(store.db, value)
                self.refuses_cold(store)
                self.refuses_hot(store)

    def test_wrong_or_spoofed_index_definitions_refuse_before_mutation(self):
        directory = self.prepared()
        store = self.store(directory)
        exact = {name: sql for name, _, __, sql in observer._INDEXES}
        variants = [
            ('observer_receipt_expiry',
             'CREATE INDEX observer_receipt_expiry ON retention(expires,fingerprint,provider) WHERE protected=0'),
            ('observer_receipt_expiry',
             "CREATE INDEX observer_receipt_expiry ON retention(expires,fingerprint,provider) WHERE protected=0 "
             "/* AND thread_id GLOB 'observer:*' */"),
            ('observer_receipt_expiry',
             "CREATE INDEX observer_receipt_expiry ON retention(expires,fingerprint,provider) "
             "WHERE protected=0 AND thread_id GLOB 'observer*'"),
            ('observer_boundary_expiry',
             'CREATE INDEX observer_boundary_expiry ON boundaries(checked,thread_id,scope,provider)'),
            ('observer_boundary_expiry',
             "CREATE INDEX observer_boundary_expiry ON boundaries(checked,thread_id,scope) "
             "WHERE thread_id GLOB 'observer:*'"),
            ('retention_subject',
             'CREATE INDEX retention_subject ON retention(thread_id,scope,provider) WHERE protected=0'),
            ('retention_subject', 'CREATE UNIQUE INDEX retention_subject ON retention(thread_id,scope,provider)'),
            ('retention_subject', 'CREATE INDEX RETENTION_SUBJECT ON retention(thread_id,scope,provider)'),
            ('retention_subject', 'CREATE INDEX retention_subject ON retention(thread_id,scope,provider,born)'),
        ]
        for name, sql in variants:
            with self.subTest(cold=sql):
                store.db.execute(sql)
                self.refuses_cold(store)  # the other missing indexes are not created either
                store.db.execute('DROP INDEX ' + name)
        self.cold(store)
        for name, sql in variants:
            with self.subTest(hot=sql):
                store.db.execute('DROP INDEX ' + name)
                store.db.execute(sql)
                self.refuses_hot(store, [(subject(B), B, 'slack')])
                store.db.execute('DROP INDEX ' + name)
                store.db.execute(exact[name])
        self.assertEqual(observer.maintain_observer_scopes(store, [(subject(B), B, 'slack')])['inserted'], 1)

    def test_observer_clock_is_global_monotonic_and_uncommitted(self):
        clock = Clock(BORN + 100)
        directory, store = self.ready(clock=clock)
        put(store.db, dict(meta(store.db), future='kept'))
        with self.assertRaises(finite.RetentionError):
            observer.observer_clock(store)  # requires the caller's open claim transaction
        store.db.execute('BEGIN IMMEDIATE')
        self.assertEqual(observer.observer_clock(store), BORN + 100)
        store.db.rollback()
        self.assertEqual(meta(store.db)['observer_retention']['checked'], BORN)
        for now, expected in ((BORN + 100, BORN + 100), (BORN + 5, BORN + 100), (BORN + 200, BORN + 200),
                              (0, BORN + 200)):
            clock.value = now
            store.db.execute('BEGIN IMMEDIATE')
            self.assertEqual(observer.observer_clock(store), expected)
            store.db.commit()
        value = meta(store.db)
        self.assertEqual(value['observer_retention'], dict(schema_version=1, floor=BORN, checked=BORN + 200))
        self.assertEqual(value['future'], 'kept')
        for bad in (float('nan'), True, -1):
            with self.subTest(clock=bad):
                clock.value = bad
                before = dump(store.db)
                store.db.execute('BEGIN IMMEDIATE')
                try:
                    with self.assertRaises(finite.RetentionError):
                        observer.observer_clock(store)
                finally:
                    store.db.rollback()
                self.assertEqual(dump(store.db), before)

    def test_enrolls_current_observer_scopes_in_both_families(self):
        clock = Clock(BORN + 10)
        directory = self.prepared()
        dual, fallback = self.store(directory, 'dual', clock), self.store(directory, 'fallback', clock)
        self.cold(dual)
        self.cold(fallback)
        result = observer.maintain_observer_scopes(dual, [(subject(A), A, 'slack'), (subject(A), A, 'lark'),
                                                          (subject(B), B, 'slack'), (subject(B), B, 'slack')])
        self.assertEqual((result['status'], result['enrolled'], result['inserted'], result['scope_refused'],
                          result['checked']), ('maintained', 3, 1, 0, BORN + 10))
        self.assertEqual(boundaries(dual.db, A), [('lark', BORN, '~', BORN + 10), ('slack', BORN, '~', BORN + 10)])
        self.assertEqual(boundaries(dual.db, B), [('slack', BORN, '~', BORN + 10)])
        result = observer.maintain_observer_scopes(fallback, [(subject(C), C, 'smtp'), (subject(C), C, 'windows_msg')])
        self.assertEqual((result['status'], result['inserted']), ('maintained', 2))
        self.assertEqual(boundaries(fallback.db, C),
                         [('smtp', BORN, '~', BORN + 10), ('windows_msg', BORN, '~', BORN + 10)])
        self.assertEqual(boundaries(dual.db, C), [])

        invalid = [[(subject(C), C, 'smtp')], [(NATIVE[0], A, 'slack')], [(subject(A), B, 'slack')],
                   [(subject(A), A, 'teams')], [('x',)], [(subject(A).upper(), A, 'slack')]]
        for scopes in invalid:
            with self.subTest(scopes=scopes):
                self.refuses_maintain(dual, scopes)
        for budget in (0, 33, True, 1.0, None):
            with self.subTest(budget=budget):
                self.refuses_maintain(dual, [(subject(A), A, 'slack')], budget=budget)
        dual.db.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(finite.RetentionError):
                observer.maintain_observer_scopes(dual, [(subject(A), A, 'slack')])
        finally:
            dual.db.rollback()

        # Existing advanced floors stay; a greater stored check raises the global clock.
        dual.db.execute('UPDATE boundaries SET floor=?,floor_key=?,checked=? WHERE thread_id=? AND scope=? AND provider=?',
                        (BORN + 5, digest('key'), BORN + 500, subject(A), A, 'slack'))
        clock.value = BORN + 20
        result = observer.maintain_observer_scopes(dual, [(subject(A), A, 'slack'), (subject(A), A, 'lark')])
        self.assertEqual((result['enrolled'], result['inserted'], result['checked']), (2, 0, BORN + 500))
        self.assertEqual(boundaries(dual.db, A),
                         [('lark', BORN, '~', BORN + 20), ('slack', BORN + 5, digest('key'), BORN + 500)])
        self.assertEqual(meta(dual.db)['observer_retention']['checked'], BORN + 500)
        self.assertEqual(meta(fallback.db)['observer_retention']['checked'], BORN + 10)

    def test_malformed_scope_and_old_rows_do_not_block_healthy_work(self):
        clock = Clock(BORN + 10)
        directory, store = self.ready(clock=clock)
        db = store.db
        db.execute("UPDATE boundaries SET floor='bad' WHERE thread_id=? AND provider='slack'", (subject(A),))
        bad = boundaries(db, A)
        result = observer.maintain_observer_scopes(store, [(subject(A), A, 'slack'), (subject(B), B, 'slack')])
        self.assertEqual((result['status'], result['enrolled'], result['inserted'], result['scope_refused']),
                         ('partial', 1, 1, 1))
        self.assertEqual(boundaries(db, A), bad)  # refused, not reset
        self.assertEqual(boundaries(db, B), [('slack', BORN, '~', BORN + 10)])

        clock.value = BORN + 2 * TTL
        db.execute('BEGIN')
        seed(db, digest('malformed'), 'slack', subject(B), B, BORN + 0.5, target='short')
        for index in range(3):
            seed(db, digest('healthy-%d' % index), 'slack', subject(B), B, BORN + 1 + index)
        db.execute('COMMIT')
        result = observer.maintain_observer_scopes(store, [(subject(B), B, 'slack')])
        self.assertEqual((result['status'], result['receipt_examined'], result['receipt_removed']),
                         ('maintained', 4, 3))
        self.assertEqual((result['boundary_examined'], result['boundary_removed']), (2, 1))
        self.assertEqual(result['gc_skipped'], 2)  # malformed receipt and malformed boundary remain
        self.assertEqual(db.execute('SELECT fingerprint FROM retention WHERE thread_id=?', (subject(B),)).fetchall(),
                         [(digest('malformed'),)])
        self.assertEqual(boundaries(db, B), [('slack', BORN + 3, digest('healthy-2'), BORN + 2 * TTL)])
        self.assertEqual(boundaries(db, A), [bad[1]])

    def test_idle_boundary_retires_and_reenrolls_without_making_old_births_eligible(self):
        clock = Clock(BORN + 10)
        directory, store = self.ready(clock=clock)
        self.assertEqual(observer.maintain_observer_scopes(store, [(subject(S), S, 'slack')])['inserted'], 1)
        first = context(S, 'first', BORN + 20)
        clock.value = BORN + 30
        claimer = self.store(directory, clock=clock, receipt_context=first)
        self.assertIsNone(claimer.claim(first['fingerprint'], 'slack'))
        claimer.confirm(first['fingerprint'], 'slack')

        expired = BORN + 20 + TTL + 5
        clock.value = expired
        result = observer.maintain_observer_scopes(store, [])
        self.assertEqual((result['receipt_examined'], result['receipt_removed']), (1, 1))
        self.assertEqual(result['boundary_removed'], 2)  # idle imported dual boundaries for scope A
        self.assertIsNone(store.db.execute('SELECT 1 FROM retention WHERE fingerprint=?',
                                           (first['fingerprint'],)).fetchone())
        self.assertEqual(boundaries(store.db, S), [('slack', BORN + 20, first['fingerprint'], expired)])

        retired = BORN + 20 + 2 * TTL + 10
        clock.value = retired
        result = observer.maintain_observer_scopes(store, [])
        self.assertEqual((result['boundary_examined'], result['boundary_removed']), (1, 1))
        self.assertEqual(boundaries(store.db, S), [])
        old = context(S, 'old', BORN + 21)
        with self.assertRaises(finite.RetentionError):
            self.store(directory, clock=clock, receipt_context=old).claim(old['fingerprint'], 'slack')

        clock.value = BORN + 40  # wall clock rollback
        result = observer.maintain_observer_scopes(store, [(subject(S), S, 'slack')])
        self.assertEqual((result['inserted'], result['checked']), (1, retired))
        self.assertEqual(boundaries(store.db, S), [('slack', BORN, '~', retired)])
        for ctx in (old, first):
            with self.subTest(event=ctx['fingerprint']):
                retry = self.store(directory, clock=clock, receipt_context=ctx)
                with self.assertRaisesRegex(finite.RetentionError, 'event_retired'):
                    retry.claim(ctx['fingerprint'], 'slack')
        store.db.execute('BEGIN IMMEDIATE')
        self.assertEqual(observer.observer_clock(store), retired)  # what an observer-v2 claim will read
        store.db.rollback()
        fresh = context(S, 'fresh', retired + 1)
        clock.value = retired + 2
        self.assertIsNone(self.store(directory, clock=clock, receipt_context=fresh).claim(fresh['fingerprint'], 'slack'))

    def test_collection_is_bounded_per_call(self):
        clock = Clock(BORN + 3 * TTL)
        directory, store = self.ready(clock=clock)
        current = [(subject(B), B, 'slack')]
        observer.maintain_observer_scopes(store, current)
        store.db.execute('BEGIN')
        for index in range(40):
            seed(store.db, digest('expired-%d' % index), 'slack', subject(B), B, BORN + 1 + index)
        store.db.execute('COMMIT')
        removed = []
        for _ in range(12):
            result = observer.maintain_observer_scopes(store, current, budget=5)
            self.assertLessEqual(result['receipt_examined'], 5)
            self.assertLessEqual(result['boundary_examined'], 5)
            removed.append(result['receipt_removed'])
        self.assertEqual(removed, [5] * 8 + [0] * 4)
        self.assertEqual(store.db.execute('SELECT count(*) FROM retention').fetchone()[0], 0)

    def test_native_and_protected_rows_are_byte_unchanged(self):
        clock = Clock(BORN + 10)
        directory, store = self.ready(clock=clock)
        db = store.db
        db.execute('BEGIN')
        for index, thread in enumerate(NATIVE[:3]):
            scope = digest('native-scope-%d' % index)
            boundary(db, thread, scope, 'slack', BORN, BORN)
            for row in range(20):
                seed(db, digest('native-%d-%d' % (index, row)), 'slack', thread, scope, BORN + row, protected=row % 2)
        boundary(db, subject(P), P, 'lark', BORN, BORN)
        seed(db, digest('observer-protected'), 'lark', subject(P), P, BORN + 1, protected=1)
        db.execute('COMMIT')
        before = guarded(db)
        for now in (BORN + 2 * TTL, BORN + 5 * TTL, BORN + 9 * TTL):
            clock.value = now
            result = observer.maintain_observer_scopes(store, [(subject(B), B, 'slack')])
            self.assertEqual((result['status'], result['receipt_removed'], result['gc_skipped']), ('maintained', 0, 0))
        self.assertEqual(result['boundary_retained'], 1)  # protected observer receipt keeps its boundary
        self.assertEqual(guarded(db), before)

    def test_history_growth_keeps_operations_and_query_plans_constant(self):
        now = BORN + 10 * TTL
        fixed, busy, old = digest('fixed'), digest('busy'), digest('old')
        current = [(subject(digest('current-%d' % i)), digest('current-%d' % i), 'slack') for i in range(3)]
        queries = [(observer._RECEIPT_CANDIDATES, (now, 32)), (observer._BOUNDARY_CANDIDATES, (now - TTL, 32)),
                   (observer._SUBJECT_PROBE, (subject(fixed), fixed, 'slack'))]
        measured = {}
        for history in (0, 1000, 10000):
            directory, store = self.ready(clock=Clock(now))
            db = store.db
            db.execute('BEGIN')
            # Constant current/bounded-window scaffolding present at every size.
            for scope in (fixed, busy, old):
                boundary(db, subject(scope), scope, 'slack', BORN, now - 10)
            for index in range(2):
                stale = digest('stale-%d' % index)
                boundary(db, subject(stale), stale, 'lark', BORN, BORN + index)
            for index, thread in enumerate(NATIVE):
                boundary(db, thread, digest('native-scope-%d' % index), 'slack', BORN, BORN)
            for index in range(40):
                seed(db, digest('fixed-%d' % index), 'slack', subject(fixed), fixed, BORN + 1 + index)
            # Growing native-heavy history plus observer rows outside the bounded windows.
            for index in range(history):
                lane = index % len(NATIVE)
                seed(db, digest('native-%d' % index), 'slack', NATIVE[lane], digest('native-scope-%d' % lane),
                     BORN + 100 + index / 1000, protected=int(index % 10 == 0))
            for index in range(history // 10):
                seed(db, digest('old-%d' % index), 'slack', subject(old), old, BORN + 1000 + index / 1000)
                seed(db, digest('busy-%d' % index), 'slack', subject(busy), busy, now - 1000 + index / 1000)
            db.execute('COMMIT')
            ticks, statements, result = measure(db, lambda: observer.maintain_observer_scopes(store, current))
            self.assertEqual((result['status'], result['inserted'], result['receipt_removed'],
                              result['boundary_removed'], result['gc_skipped']), ('maintained', 3, 32, 4, 0))
            self.assertEqual(db.execute('SELECT count(*) FROM retention WHERE thread_id NOT GLOB ?',
                                        (OBSERVED,)).fetchone()[0], history)
            plans = [plan(db, sql, args) for sql, args in queries]
            for detail, name in zip(plans, ('observer_receipt_expiry', 'observer_boundary_expiry', 'retention_subject')):
                self.assertIn(name, ' '.join(detail))
                self.assertNotIn('TEMP B-TREE', ' '.join(detail))
            measured[history] = (ticks, statements, plans)
        self.assertEqual(measured[1000], measured[0])
        self.assertEqual(measured[10000], measured[0])


if __name__ == '__main__':
    unittest.main()
