"""Observer receipt scopes on already prepared finite receipt families.

Explicit cold preparation records the original import floor and three exact
indexes. Hot callers read that state, advance one global monotonic observer
clock, enroll verified current observer:scope_hash subjects and optionally
collect a bounded number of expired observer rows. Native subjects, receipts,
pins and claims are never changed here. Callers keep the existing sender and
native fences; this module opens no connection, file, lock, thread or process,
never sends and never reads native or legacy history.
"""
import json
import math
import sqlite3

from .notification_context import OBSERVER_PREFIX
from .notification_receipt_retention import (BUDGET, FRESH_ORIGIN, PROVIDERS, TTL,
    RetentionError, _IDENTITIES, _digest, _initial_floor_key, _json, _scope, _time)

METADATA_LIMIT = 1024
# SQLite keeps these statements verbatim in sqlite_master; only exact text and
# columns are accepted, so a weaker or comment-spoofed predicate is refused.
_INDEXES = (
    ('observer_receipt_expiry', 'retention', ('expires', 'fingerprint', 'provider'),
     "CREATE INDEX observer_receipt_expiry ON retention(expires,fingerprint,provider)"
     " WHERE protected=0 AND thread_id GLOB 'observer:*'"),
    ('observer_boundary_expiry', 'boundaries', ('checked', 'thread_id', 'scope', 'provider'),
     "CREATE INDEX observer_boundary_expiry ON boundaries(checked,thread_id,scope,provider)"
     " WHERE thread_id GLOB 'observer:*'"),
    ('retention_subject', 'retention', ('thread_id', 'scope', 'provider'),
     'CREATE INDEX retention_subject ON retention(thread_id,scope,provider)'),
)
_METADATA = ("SELECT CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB))<=1024 THEN value END "
             'FROM finite_metadata WHERE id=1')
_SUBJECT_BYTES = len((OBSERVER_PREFIX + '0' * 64).encode())
# Candidate reads stay inside the exact partial indexes and stop at budget.
_RECEIPT_CANDIDATES = ('SELECT ' + _IDENTITIES + ' FROM retention INDEXED BY observer_receipt_expiry '
                       "WHERE protected=0 AND thread_id GLOB 'observer:*' AND expires<=? "
                       'ORDER BY expires,fingerprint,provider LIMIT ?')
_BOUNDARY_CANDIDATES = (
    "SELECT CASE WHEN typeof(thread_id)='text' AND length(CAST(thread_id AS BLOB))=%d THEN thread_id END,"
    "CASE WHEN typeof(scope)='text' AND length(CAST(scope AS BLOB))=64 THEN scope END,"
    "CASE WHEN typeof(provider)='text' AND length(CAST(provider AS BLOB))<=11 THEN provider END,"
    "CASE WHEN typeof(checked) IN ('integer','real') THEN checked END "
    "FROM boundaries INDEXED BY observer_boundary_expiry WHERE thread_id GLOB 'observer:*' AND checked<=? "
    'ORDER BY checked,thread_id,scope,provider LIMIT ?') % _SUBJECT_BYTES
_SUBJECT_PROBE = ('SELECT 1 FROM retention INDEXED BY retention_subject '
                  'WHERE thread_id=? AND scope=? AND provider=? LIMIT 1')
_GC_COUNTS = ('receipt_examined', 'receipt_removed', 'boundary_examined', 'boundary_removed',
              'boundary_retained', 'gc_skipped')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RetentionError('observer_retention_metadata_invalid')
        value[key] = item
    return value


def _finite(text):
    value = float(text)
    if not math.isfinite(value):
        raise RetentionError('observer_retention_metadata_invalid')
    return value


def _constant(text):
    raise RetentionError('observer_retention_metadata_invalid')


def _decode(text):
    try:
        return json.loads(text, object_pairs_hook=_unique, parse_float=_finite, parse_constant=_constant)
    except (ValueError, TypeError, RecursionError):
        raise RetentionError('observer_retention_metadata_invalid') from None


def _load(store, *, cold=False):
    """Read the fixed metadata row; its size is bounded before any decode."""
    row = store.db.execute(_METADATA).fetchone()
    meta = _decode(row[0] if row else None)
    if (not isinstance(meta, dict) or type(meta.get('schema_version')) is not int
            or meta['schema_version'] != 1 or meta.get('family') != store.family
            or not _digest(meta.get('plan_sha256')) or meta['plan_sha256'] != store.plan):
        raise RetentionError('observer_retention_metadata_invalid')
    if 'observer_retention' not in meta:
        if cold:
            return meta, None
        raise RetentionError('observer_retention_unprepared')
    observed = meta['observer_retention']
    if (not isinstance(observed, dict) or type(observed.get('schema_version')) is not int
            or observed['schema_version'] != 1 or 'floor' not in observed or 'checked' not in observed
            or _time(observed['floor']) > _time(observed['checked'])):
        raise RetentionError('observer_retention_metadata_invalid')
    _initial_floor_key(observed)
    return meta, observed


def _encode(meta):
    try:
        text = _json(meta)
        size = len(text.encode())
    except (ValueError, TypeError):
        raise RetentionError('observer_retention_metadata_invalid') from None
    if size > METADATA_LIMIT:
        raise RetentionError('observer_retention_metadata_too_large')
    return text


def _write(db, text):
    if db.execute('UPDATE finite_metadata SET value=? WHERE id=1', (text,)).rowcount != 1:
        raise RetentionError('observer_retention_metadata_invalid')


def _indexes(db):
    """Return missing exact index statements; refuse every other definition."""
    missing = []
    for name, table, columns, sql in _INDEXES:
        rows = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master WHERE lower(name)=?',
                          (name,)).fetchmany(2)
        if not rows:
            missing.append(sql)
            continue
        info = db.execute('PRAGMA index_info(' + name + ')').fetchmany(len(columns) + 1)
        if rows != [('index', name, table, sql)] or tuple(r[2] for r in info) != columns:
            raise RetentionError('observer_retention_index_invalid')
    return missing


def _require(store):
    """Hot paths need explicit cold state; nothing is repaired or imported."""
    meta, observed = _load(store)
    if _indexes(store.db):
        raise RetentionError('observer_retention_index_missing')
    return meta, observed


def _isolated(db, action, *args):
    """Refuse one malformed subject or row without resetting healthy work."""
    db.execute('SAVEPOINT observer_subject')
    try:
        outcome = action(*args)
    except RetentionError:
        db.execute('ROLLBACK TO observer_subject')
        outcome = None
    db.execute('RELEASE observer_subject')
    return outcome


def prepare_observer_scopes(store, born, *, fresh=False):
    """Initialize inside the caller's fenced cold or exclusive-fresh transaction.

    born is the verified original cold-plan import floor. Missing exact indexes
    and missing observer metadata are created only here; any other definition
    or partial metadata is refused, never repaired. ``fresh`` is reserved for
    the proven new namespace's zero floor and its explicit forward recovery.
    Nothing is committed.
    """
    born = _time(born)
    if type(fresh) is not bool or (fresh and born != 0):
        raise RetentionError('observer_retention_origin_invalid')
    db = store.db
    if not db.in_transaction:
        raise RetentionError('observer_retention_transaction_required')
    meta, observed = _load(store, cold=True)
    missing = _indexes(db)
    text = None
    if observed is None:
        value = dict(schema_version=1, floor=born, checked=born)
        if fresh:
            value['origin'] = FRESH_ORIGIN
        text = _encode(dict(meta, observer_retention=value))
    elif (_time(observed['floor']) != born
          or _initial_floor_key(observed) != ('' if fresh else '~')):
        raise RetentionError('observer_retention_floor_collision')
    # Every input and existing definition is validated before this first write.
    for sql in missing:
        db.execute(sql)
    if text is not None:
        _write(db, text)
    return dict(status='prepared' if missing or text else 'unchanged', floor=born,
                checked=born if observed is None else _time(observed['checked']), created=len(missing))


def observer_clock(store):
    """Advance the global observer high-water inside an already open claim.

    The value never decreases, so a rolled-back wall clock or a re-enrolled
    scope cannot make an expired observer effect eligible again.
    """
    db = store.db
    if not db.in_transaction:
        raise RetentionError('observer_retention_transaction_required')
    meta, observed = _require(store)
    prior = _time(observed['checked'])
    checked = max(_time(store.clock()), prior)
    if checked != prior:
        _write(db, _encode(dict(meta, observer_retention=dict(observed, checked=checked))))
    return checked


def _enroll(store, subject, floor, checked, floor_key='~'):
    thread, scope, provider = subject
    if floor_key == '':
        # Missing fresh boundaries with retained work are damaged scopes,
        # including protected rows; maintenance must not undo claim refusal.
        inserted = int(store._enroll_current(thread, scope, provider, floor, checked, floor_key))
    else:
        inserted = store.db.execute('INSERT OR IGNORE INTO boundaries VALUES(?,?,?,?,?,?)',
                                    (thread, scope, provider, floor, floor_key, checked)).rowcount
    # An existing boundary keeps its higher floor and floor_key.
    prior = _time(store._boundary(thread, scope, provider)[2])
    store.db.execute('UPDATE boundaries SET checked=? WHERE thread_id=? AND scope=? AND provider=?',
                     (max(prior, checked), thread, scope, provider))
    return inserted, prior


def _collect_receipt(store, fingerprint, provider, now):
    row = store._row(fingerprint, provider)
    if row is None or row[0] != OBSERVER_PREFIX + row[1] or row[4] != 0 or _time(row[3]) > now:
        raise RetentionError('observer_retention_candidate_invalid')
    store._remove([(fingerprint, provider)], now)
    return True


def _collect_boundary(store, row, cutoff):
    thread, scope, provider, checked = row
    _scope(thread, scope)
    if (thread != OBSERVER_PREFIX + scope or provider not in PROVIDERS[store.family]
            or _time(checked) > cutoff or _time(store._boundary(thread, scope, provider)[2]) > cutoff):
        raise RetentionError('observer_retention_candidate_invalid')
    # Any remaining receipt, including a protected one, keeps its boundary.
    if store.db.execute(_SUBJECT_PROBE, (thread, scope, provider)).fetchone() is not None:
        return 'retained'
    store.db.execute('DELETE FROM boundaries WHERE thread_id=? AND scope=? AND provider=?',
                     (thread, scope, provider))
    return 'removed'


def _collect(store, budget, result):
    db = store.db
    db.execute('BEGIN IMMEDIATE')
    now = observer_clock(store)
    rows = db.execute(_RECEIPT_CANDIDATES, (now, budget)).fetchall()
    result['receipt_examined'] = len(rows)
    for fingerprint, provider in rows:
        removed = _isolated(db, _collect_receipt, store, fingerprint, provider, now)
        result['receipt_removed' if removed else 'gc_skipped'] += 1
    cutoff = now - TTL
    rows = db.execute(_BOUNDARY_CANDIDATES, (cutoff, budget)).fetchall()
    result['boundary_examined'] = len(rows)
    for row in rows:
        outcome = _isolated(db, _collect_boundary, store, row, cutoff)
        result['boundary_' + outcome if outcome else 'gc_skipped'] += 1
    db.commit()
    return now


def maintain_observer_scopes(store, scopes, *, budget=BUDGET):
    """Enroll current observer scopes, then optionally collect bounded old rows.

    scopes is the caller's verified O(active) (subject, scope_sha256, provider)
    projection. Enrollment and the clock commit before collection, so an old
    malformed row cannot block later independent admission. Collection only
    retires expired unprotected observer receipts and idle observer boundaries.
    """
    current, seen = [], set()
    for item in scopes:
        try:
            thread, scope, provider = item
        except (TypeError, ValueError):
            raise RetentionError('observer_retention_scope_invalid') from None
        _scope(thread, scope)
        if thread != OBSERVER_PREFIX + scope or provider not in PROVIDERS[store.family]:
            raise RetentionError('observer_retention_scope_invalid')
        if (thread, scope, provider) not in seen:
            seen.add((thread, scope, provider))
            current.append((thread, scope, provider))
    if type(budget) is not int or not 1 <= budget <= BUDGET:
        raise RetentionError('notification_retention_budget_invalid')
    db = store.db
    if db.in_transaction:
        raise RetentionError('observer_retention_transaction_active')
    _, observed = _require(store)
    floor = _time(observed['floor'])
    initial_key = _initial_floor_key(observed)
    result = dict(status='maintained', enrolled=0, inserted=0, scope_refused=0, gc='collected',
                  checked=None, **dict.fromkeys(_GC_COUNTS, 0))
    db.execute('BEGIN IMMEDIATE')
    try:
        checked = observer_clock(store)
        highest = checked
        for subject in current:
            outcome = _isolated(db, _enroll, store, subject, floor, checked, initial_key)
            if outcome is None:
                result['scope_refused'] += 1
                continue
            result['enrolled'] += 1
            result['inserted'] += outcome[0]
            highest = max(highest, outcome[1])
        if highest > checked:
            meta, observed = _load(store)
            _write(db, _encode(dict(meta, observer_retention=dict(observed, checked=highest))))
            checked = highest
        db.commit()
    except BaseException:
        db.rollback()
        raise
    result['checked'] = checked
    # Optional collection runs separately; its failure leaves committed work.
    try:
        result['checked'] = _collect(store, budget, result)
    except (RetentionError, sqlite3.Error):
        db.rollback()
        result.update(dict.fromkeys(_GC_COUNTS, 0), gc='failed')
    except BaseException:
        db.rollback()
        raise
    if result['scope_refused'] or result['gc'] != 'collected':
        result['status'] = 'partial'
    return result
