"""Finite notification claims; explicit cold preparation and bounded maintenance.

Callers supply the verified current scope, original birth and provider identities.
They retain the existing sender/native fences through each operation. This module
never discovers sessions, reads native history, sends, or treats uncertainty as
completion. Cold preparation requires a complete projection for this runtime;
an unknown shared/foreign runtime must not be passed as that projection.
"""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from .notification_context import OBSERVER_PREFIX
from .notification_senders import sender_fence as common_sender_fence

TTL = 72 * 60 * 60
LIMIT = 256
BUDGET = 32
FRESH_ORIGIN = 'fresh-notification-namespace-v1'
PROVIDERS = {'dual': ('slack', 'lark'), 'fallback': ('smtp', 'windows_msg')}
NAMES = {'dual': 'dual-deliveries', 'fallback': 'fallback-deliveries'}
_IDENTITIES = ("CASE WHEN typeof(fingerprint)='text' AND length(CAST(fingerprint AS BLOB))=64 THEN fingerprint END,"
               "CASE WHEN typeof(provider)='text' AND length(CAST(provider AS BLOB))<=11 THEN provider END")


class RetentionError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _time(value):
    try:
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError()
        return float(value)
    except (ValueError, OverflowError):
        raise RetentionError('notification_retention_clock_invalid') from None


def _initial_floor_key(value, *, floor_field='floor'):
    """Only an explicitly fresh namespace includes the valid epoch-zero birth."""
    if 'origin' not in value:
        return '~'
    if value['origin'] != FRESH_ORIGIN or _time(value.get(floor_field)) != 0:
        raise RetentionError('notification_retention_origin_invalid')
    return ''


def _scope(thread, scope):
    try:
        valid = isinstance(thread, str) and str(uuid.UUID(thread)) == thread
    except (ValueError, AttributeError):
        valid = False
    # A durable disjoint format discriminator for local observer receipts.
    # No native/control/command API accepts this value as a session UUID.
    observer = _digest(scope) and thread == OBSERVER_PREFIX + scope
    if (not valid and not observer) or not _digest(scope):
        raise RetentionError('notification_retention_scope_invalid')
    return thread, scope


def context(value):
    """Native v1 or disjoint observer v2; never convert one into the other."""
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] not in (1, 2)):
        raise RetentionError('notification_retention_context_required')
    if value['schema_version'] == 1:
        try:
            canonical = str(uuid.UUID(value.get('thread_id')))
        except (ValueError, TypeError, AttributeError):
            raise RetentionError('notification_retention_scope_invalid') from None
        if (canonical != value.get('thread_id') or 'subject_kind' in value
                or 'observer_scope_sha256' in value):
            raise RetentionError('notification_retention_scope_invalid')
    elif (value.get('subject_kind') != 'observer' or 'thread_id' in value
            or not _digest(value.get('observer_scope_sha256'))
            or value['observer_scope_sha256'] != value.get('scope_sha256')):
        raise RetentionError('notification_retention_scope_invalid')
    _scope(_subject(value), value.get('scope_sha256'))
    if not _digest(value.get('fingerprint')) or not isinstance(value.get('providers'), dict) or not value['providers']:
        raise RetentionError('notification_retention_context_invalid')
    for provider, identity in value['providers'].items():
        if provider not in PROVIDERS['dual'] + PROVIDERS['fallback'] or not isinstance(identity, dict) or any(
                not _digest(identity.get(k)) for k in ('target_sha256', 'payload_sha256')):
            raise RetentionError('notification_retention_identity_invalid')
    if len(_json(value).encode()) > 8192:
        raise RetentionError('notification_retention_context_too_large')
    return dict(value, born=_time(value.get('born')))


def _subject(value):
    return (value['thread_id'] if value['schema_version'] == 1
            else OBSERVER_PREFIX + value['observer_scope_sha256'])


def _safe(path):
    path = Path(path).absolute()
    if path.is_symlink() or path.resolve() != path:
        raise RetentionError('notification_retention_path_invalid')
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(str(path) + suffix)
        if side.is_symlink():
            raise RetentionError('notification_retention_path_invalid')
    return path


def _schema(db, family, plan):
    if family == 'dual':
        db.execute('CREATE TABLE receipts(fingerprint TEXT PRIMARY KEY NOT NULL,slack TEXT,lark TEXT)')
    else:
        db.execute('CREATE TABLE receipts(fingerprint TEXT NOT NULL,provider TEXT NOT NULL,target_sha256 TEXT NOT NULL,payload_sha256 TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(fingerprint,provider))')
    db.execute('CREATE TABLE retention(fingerprint TEXT NOT NULL,provider TEXT NOT NULL,thread_id TEXT NOT NULL,scope TEXT NOT NULL,born REAL NOT NULL,expires REAL NOT NULL,protected INTEGER NOT NULL,target_sha256 TEXT NOT NULL,payload_sha256 TEXT NOT NULL,PRIMARY KEY(fingerprint,provider))')
    db.execute('CREATE INDEX receipt_expiry ON retention(expires,fingerprint,provider) WHERE protected=0')
    db.execute('CREATE INDEX receipt_scope ON retention(thread_id,scope,provider,born,fingerprint) WHERE protected=0')
    db.execute('CREATE INDEX receipt_scope_expiry ON retention(thread_id,scope,provider,expires,fingerprint) WHERE protected=0')
    db.execute('CREATE TABLE boundaries(thread_id TEXT NOT NULL,scope TEXT NOT NULL,provider TEXT NOT NULL,floor REAL NOT NULL,floor_key TEXT NOT NULL,checked REAL NOT NULL,PRIMARY KEY(thread_id,scope,provider))')
    db.execute('CREATE TABLE finite_metadata(id INTEGER PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('INSERT INTO finite_metadata VALUES(1,?)', (_json(dict(schema_version=1, family=family, plan_sha256=plan)),))
    db.execute('PRAGMA user_version=2')


class FiniteReceipts:
    def __init__(self, db, family, *, receipt_context=None, clock=time.time):
        self.db, self.family, self.clock = db, family, clock
        self.context = context(receipt_context) if receipt_context is not None else None
        # Prepared entry sets this only after reading the exact committed
        # notifier claim. Direct/legacy callers cannot auto-enroll a scope.
        self.native_enrollment = None
        self.fresh_namespace = False
        if db.execute('PRAGMA user_version').fetchone()[0] != 2:
            raise RetentionError('notification_retention_schema_invalid')
        expected = {'receipts', 'retention', 'boundaries', 'finite_metadata'}
        objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchmany(17)
        if len(objects) > 16 or {n for t,n in objects if t == 'table'} != expected or any(t not in ('table','index') for t,n in objects):
            raise RetentionError('notification_retention_schema_invalid')
        for table, keys in [('receipts', ('fingerprint',) if family == 'dual' else ('fingerprint','provider')),
                            ('retention', ('fingerprint','provider')), ('boundaries', ('thread_id','scope','provider'))]:
            fields = db.execute('PRAGMA table_info('+table+')').fetchmany(17)
            if len(fields) > 16 or tuple(r[1] for r in sorted(fields,key=lambda r:r[5]) if r[5]) != keys:
                raise RetentionError('notification_retention_schema_invalid')
        for index, columns in [('receipt_expiry',('expires','fingerprint','provider')),
                               ('receipt_scope',('thread_id','scope','provider','born','fingerprint')),
                               ('receipt_scope_expiry',('thread_id','scope','provider','expires','fingerprint'))]:
            if tuple(r[2] for r in db.execute('PRAGMA index_info('+index+')')) != columns:
                raise RetentionError('notification_retention_schema_invalid')
        fields=db.execute('PRAGMA table_info(finite_metadata)').fetchmany(3)
        if (len(fields)!=2 or tuple(r[1] for r in fields)!=('id','value')
                or fields[0][2].upper()!='INTEGER' or fields[0][5]!=1
                or fields[1][2].upper()!='TEXT' or fields[1][3]!=1 or fields[1][5]!=0):
            raise RetentionError('notification_retention_schema_invalid')
        row = db.execute('SELECT CASE WHEN length(value)<=1024 THEN value END FROM finite_metadata WHERE id=1').fetchone()
        try:
            meta = json.loads(row[0])
        except (ValueError, TypeError, IndexError):
            raise RetentionError('notification_retention_schema_invalid') from None
        if not isinstance(meta,dict) or type(meta.get('schema_version')) is not int or meta['schema_version'] != 1 or meta.get('family') != family or not _digest(meta.get('plan_sha256')):
            raise RetentionError('notification_retention_schema_invalid')
        self.plan = meta['plan_sha256']

    def _identity(self, fingerprint, provider):
        ctx = self.context
        if ctx is None:
            raise RetentionError('notification_retention_context_required')
        if fingerprint != ctx['fingerprint'] or provider not in PROVIDERS[self.family] or provider not in ctx['providers']:
            raise RetentionError('notification_retention_identity_collision')
        identity = ctx['providers'][provider]
        return (_subject(ctx),ctx['scope_sha256'],ctx['born'],identity['target_sha256'],identity['payload_sha256'])

    def _row(self, fingerprint, provider):
        if not _digest(fingerprint) or provider not in PROVIDERS[self.family]:
            raise RetentionError('notification_retention_row_invalid')
        row = self.db.execute("SELECT CASE WHEN length(CAST(thread_id AS BLOB)) IN (36,73) THEN thread_id END,"
            "CASE WHEN length(CAST(scope AS BLOB))=64 THEN scope END,"
            "CASE WHEN typeof(born) IN ('integer','real') THEN born END,"
            "CASE WHEN typeof(expires) IN ('integer','real') THEN expires END,"
            "CASE WHEN typeof(protected)='integer' THEN protected END,"
            "CASE WHEN length(CAST(target_sha256 AS BLOB))=64 THEN target_sha256 END,"
            "CASE WHEN length(CAST(payload_sha256 AS BLOB))=64 THEN payload_sha256 END "
            'FROM retention WHERE fingerprint=? AND provider=?',(fingerprint,provider)).fetchone()
        if row is not None:
            if (_scope(row[0],row[1]) != row[:2] or _time(row[2])+TTL != _time(row[3]) or type(row[4]) is not int or row[4] not in (0,1) or not all(_digest(v) for v in row[5:])):
                raise RetentionError('notification_retention_row_invalid')
        return row

    def _check(self, fingerprint, provider, row):
        identity = self._identity(fingerprint,provider)
        if row is not None and (row[0],row[1],row[2],row[5],row[6]) != identity:
            raise RetentionError('notification_retention_identity_collision')
        return identity

    def _state(self, fingerprint, provider):
        if self.family == 'dual':
            row = self.db.execute('SELECT CASE WHEN '+provider+" IS NULL THEN NULL WHEN "+provider+" IN ('uncertain','sent') THEN "+provider+" ELSE 'invalid' END FROM receipts WHERE fingerprint=?",(fingerprint,)).fetchone()
        else:
            row = self.db.execute("SELECT CASE WHEN state IN ('uncertain','sent') THEN state ELSE 'invalid' END FROM receipts WHERE fingerprint=? AND provider=?",(fingerprint,provider)).fetchone()
        state = row[0] if row else None
        if state is not None and state not in ('uncertain','sent'):
            raise RetentionError('notification_retention_row_invalid')
        return state

    def get(self, fingerprint):
        if self.context is None or fingerprint != self.context['fingerprint']:
            raise RetentionError('notification_retention_context_required')
        result = {}
        for provider in PROVIDERS[self.family]:
            if provider not in self.context['providers']:
                continue
            row = self._row(fingerprint,provider)
            state = self._state(fingerprint,provider)
            if row is None and state is not None or row is not None and state is None:
                raise RetentionError('notification_retention_row_invalid')
            if row is not None:
                self._check(fingerprint,provider,row)
                result[provider] = state
        return result

    def _boundary(self, thread, scope, provider):
        row = self.db.execute("SELECT CASE WHEN typeof(floor) IN ('real','integer') THEN floor END,"
            "CASE WHEN length(CAST(floor_key AS BLOB))<=64 THEN floor_key END,"
            "CASE WHEN typeof(checked) IN ('real','integer') THEN checked END "
            'FROM boundaries WHERE thread_id=? AND scope=? AND provider=?',(thread,scope,provider)).fetchone()
        if row is None or not isinstance(row[1],str) or row[1] not in ('','~') and not _digest(row[1]) or _time(row[0]) > _time(row[2]):
            raise RetentionError('notification_retention_scope_unprepared')
        return row

    def _enroll_current(self, thread, scope, provider, floor, checked, floor_key='~'):
        if self.db.execute('SELECT 1 FROM boundaries WHERE thread_id=? AND scope=? AND provider=?',
                           (thread,scope,provider)).fetchone() is not None:
            return False  # Present malformed boundaries still fail strict _boundary.
        # A missing boundary with retained work is damage, not a new subject.
        # This covering index includes protected native/HTTP receipts. Validate
        # its exact definition only here; never scan, create or repair it.
        definition = 'CREATE INDEX retention_subject ON retention(thread_id,scope,provider)'
        if (self.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='retention_subject' AND sql=?", (definition,)).fetchone() != (1,)
                or tuple(row[2] for row in self.db.execute('PRAGMA index_info(retention_subject)')) != ('thread_id','scope','provider')):
            raise RetentionError('notification_retention_scope_index_invalid')
        if self.db.execute('SELECT 1 FROM retention INDEXED BY retention_subject WHERE thread_id=? AND scope=? AND provider=? LIMIT 1',
                           (thread,scope,provider)).fetchone() is not None:
            raise RetentionError('notification_retention_scope_unprepared')
        self.db.execute('INSERT INTO boundaries VALUES(?,?,?,?,?,?)',
                        (thread,scope,provider,floor,floor_key,checked))
        return True

    def claim(self, fingerprint, provider, target=None, payload=None):
        thread,scope,born,target_hash,payload_hash = self._identity(fingerprint,provider)
        if target is not None and (target,payload) != (target_hash,payload_hash):
            raise RetentionError('notification_retention_identity_collision')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self._row(fingerprint,provider)
            self._check(fingerprint,provider,row)
            previous = self._state(fingerprint,provider)
            if row is not None:
                if previous is None: raise RetentionError('notification_retention_row_invalid')
                self.db.commit()
                return previous
            if previous is not None: raise RetentionError('notification_retention_row_invalid')
            current = _time(self.clock())
            if self.context['schema_version'] == 1 and self.native_enrollment is not None:
                original_floor, claimed_at, initial_key = self.native_enrollment
                current = max(current, original_floor, claimed_at)
                if initial_key not in ('', '~') or (initial_key == '' and original_floor != 0):
                    raise RetentionError('notification_retention_origin_invalid')
                if (born,fingerprint) <= (original_floor,initial_key):
                    raise RetentionError('notification_retention_event_retired')
                # Enrollment and the uncertain receipt commit together before
                # one provider attempt; existing floors/pins/birth are retained.
                self._enroll_current(thread,scope,provider,original_floor,current,initial_key)
            if self.context['schema_version'] == 2:
                from .observer_receipt_scopes import observer_clock
                current = max(current, observer_clock(self))
                if self.fresh_namespace:
                    # A newly verified observer may precede the next scheduler
                    # projection. Its boundary and attempt commit together.
                    self._enroll_current(thread,scope,provider,0,current,'')
            floor,key,checked = self._boundary(thread,scope,provider)
            current = max(current,checked)
            if born > current or born+TTL <= current or (born,fingerprint) <= (floor,key):
                raise RetentionError('notification_retention_event_retired')
            self.db.execute('UPDATE boundaries SET checked=? WHERE thread_id=? AND scope=? AND provider=?',(current,thread,scope,provider))
            if self.family == 'dual':
                self.db.execute('INSERT OR IGNORE INTO receipts(fingerprint) VALUES(?)',(fingerprint,))
                self.db.execute('UPDATE receipts SET '+provider+"='uncertain' WHERE fingerprint=?",(fingerprint,))
            else:
                self.db.execute("INSERT INTO receipts VALUES(?,?,?,?,'uncertain')",(fingerprint,provider,target_hash,payload_hash))
            self.db.execute('INSERT INTO retention VALUES(?,?,?,?,?,?,0,?,?)',(fingerprint,provider,thread,scope,born,born+TTL,target_hash,payload_hash))
            # Keep this exact pre-attempt identity through the current sender
            # operation, even when its original birth trails the recent pool.
            excess = self.db.execute('SELECT '+_IDENTITIES+' FROM retention INDEXED BY receipt_scope WHERE thread_id=? AND scope=? AND provider=? AND protected=0 AND fingerprint<>? ORDER BY born DESC,fingerprint DESC LIMIT ? OFFSET ?',(thread,scope,provider,fingerprint,BUDGET,LIMIT-1)).fetchall()
            self._remove_optional(excess,current)
            self.db.commit()
            return None
        except BaseException:
            self.db.rollback(); raise

    def confirm(self, fingerprint, provider):
        row = self._row(fingerprint,provider)
        self._check(fingerprint,provider,row)
        if row is None: raise RetentionError('notification_retention_confirmation_invalid')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.family == 'dual':
                changed = self.db.execute('UPDATE receipts SET '+provider+"='sent' WHERE fingerprint=? AND "+provider+"='uncertain'",(fingerprint,)).rowcount
            else:
                changed = self.db.execute("UPDATE receipts SET state='sent' WHERE fingerprint=? AND provider=? AND state='uncertain'",(fingerprint,provider)).rowcount
            if changed != 1: raise RetentionError('notification_retention_confirmation_invalid')
            self.db.commit()
        except BaseException:
            self.db.rollback(); raise

    def _remove(self, identities, current):
        removed = 0
        for fp,provider in identities:
            row = self._row(fp,provider)
            if row is None or row[4]: continue
            floor,key,checked = self._boundary(row[0],row[1],provider)
            floor,key = max((floor,key),(row[2],fp))
            self.db.execute('UPDATE boundaries SET floor=?,floor_key=?,checked=? WHERE thread_id=? AND scope=? AND provider=?',(floor,key,max(current,checked),row[0],row[1],provider))
            self.db.execute('DELETE FROM retention WHERE fingerprint=? AND provider=?',(fp,provider))
            if self.family == 'dual':
                self.db.execute('UPDATE receipts SET '+provider+'=NULL WHERE fingerprint=?',(fp,))
                self.db.execute('DELETE FROM receipts WHERE fingerprint=? AND slack IS NULL AND lark IS NULL',(fp,))
            else: self.db.execute('DELETE FROM receipts WHERE fingerprint=? AND provider=?',(fp,provider))
            removed += 1
        return removed

    def _remove_optional(self, identities, current):
        """Refuse a malformed historical candidate, not the independent claim.

        The caller's bounded query supplies at most BUDGET identities. Keep
        each complete floor/receipt removal atomic, preserving refused bytes.
        Failure to roll back/release or commit still prevents an external send.
        """
        removed = 0
        for identity in identities:
            self.db.execute('SAVEPOINT notification_optional_gc')
            try:
                removed += self._remove((identity,),current)
            except (ValueError,TypeError,sqlite3.Error):
                self.db.execute('ROLLBACK TO notification_optional_gc')
            finally:
                self.db.execute('RELEASE notification_optional_gc')
        return removed

    def maintain(self, scopes, pins, *, complete, budget=BUDGET):
        """Caller fences supplied current subjects; unknown proof cannot clear pins.

        Native subjects require native coverage. An observer subject has no
        native work to pin and uses its verified observer/sender coverage.
        """
        if type(complete) is not bool:
            raise RetentionError('notification_retention_current_proof_invalid')
        if not complete: return dict(status='unknown',examined=0,removed=0)
        if len(pins)>16384:raise RetentionError('notification_retention_pin_budget')
        for thread,scope,provider in scopes:
            _scope(thread,scope)
            if provider not in PROVIDERS[self.family]:raise RetentionError('notification_retention_scope_invalid')
        if type(budget) is not int or not 1 <= budget <= BUDGET:
            raise RetentionError('notification_retention_budget_invalid')
        now = _time(self.clock()); removed = 0
        self.db.execute('BEGIN IMMEDIATE')
        try:
            # Only bounded supplied current identities change protection. The
            # caller passes explicit old pin removals; there is no history walk.
            for fp,provider,protected in pins:
                if not _digest(fp) or provider not in PROVIDERS[self.family] or type(protected) is not bool:
                    raise RetentionError('notification_retention_pin_invalid')
                row = self._row(fp,provider)
                if row is None: raise RetentionError('notification_retention_pin_missing')
                if (row[0],row[1],provider) not in scopes: raise RetentionError('notification_retention_pin_scope_mismatch')
                if protected and row[0].startswith(OBSERVER_PREFIX):
                    raise RetentionError('notification_retention_observer_pin_invalid')
                self.db.execute('UPDATE retention SET protected=? WHERE fingerprint=? AND provider=?',(int(protected),fp,provider))
            for thread,scope,provider in scopes:
                floor,key,checked = self._boundary(thread,scope,provider)
                now = max(now,checked)
                self.db.execute('UPDATE boundaries SET checked=? WHERE thread_id=? AND scope=? AND provider=?',(now,thread,scope,provider))
            # The scheduler supplies current scopes fairly. A missing/unknown
            # scope is not treated as proof that its native work ended.
            examined=0
            for thread,scope,provider in scopes:
                rows = self.db.execute('SELECT '+_IDENTITIES+' FROM retention INDEXED BY receipt_scope_expiry WHERE thread_id=? AND scope=? AND provider=? AND protected=0 AND expires<=? ORDER BY expires,fingerprint LIMIT ?',(thread,scope,provider,now,budget)).fetchall()
                removed+=self._remove_optional(rows,now); examined+=len(rows)
                excess = self.db.execute('SELECT '+_IDENTITIES+' FROM retention INDEXED BY receipt_scope WHERE thread_id=? AND scope=? AND provider=? AND protected=0 ORDER BY born DESC,fingerprint DESC LIMIT ? OFFSET ?',(thread,scope,provider,budget-len(rows),LIMIT)).fetchall()
                removed+=self._remove_optional(excess,now); examined+=len(excess)
            self.db.commit()
            return dict(status='maintained',examined=examined,removed=removed)
        except BaseException:
            self.db.rollback(); raise


def _legacy_rows(directory, family, *, with_metadata=False):
    """Explicit cold read only; ordinary claim/open never calls this function."""
    path = _safe(directory/(NAMES[family]+'.sqlite3'))
    marker = _safe(directory/'dual-deliveries.json')
    marker_value=json.loads(marker.read_bytes()) if family=='dual' and marker.exists() else None
    saved_marker={};metadata=None
    if not path.exists():
        if any(Path(str(path)+s).exists() for s in ('-wal','-shm','-journal')):
            raise RetentionError('notification_retention_orphaned_source')
        if family == 'dual' and marker.exists():
            from .notification_receipts import _legacy
            rows = [(fp,p,None,None,state) for fp,states in _legacy(marker_value).items() for p,state in states.items()]
        else: rows=[]
    else:
        if not path.is_file():raise RetentionError('notification_retention_source_invalid')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise RetentionError('notification_retention_explicit_source_required')
            fields=db.execute('PRAGMA table_info(receipts)').fetchall()
            names=('fingerprint','slack','lark') if family=='dual' else ('fingerprint','provider','target_sha256','payload_sha256','state')
            keys=('fingerprint',) if family=='dual' else ('fingerprint','provider')
            if tuple(r[1] for r in fields)!=names or tuple(r[1] for r in sorted(fields,key=lambda r:r[5]) if r[5])!=keys:
                raise RetentionError('notification_retention_source_invalid')
            objects=db.execute("SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
            allowed={'receipts','metadata'} if family=='dual' else {'receipts'}
            if ({n for t,n in objects if t=='table'} != allowed
                    or any(t not in ('table','index') for t,n in objects)):
                raise RetentionError('notification_retention_source_invalid')
            if family == 'dual':
                fields=db.execute('PRAGMA table_info(metadata)').fetchall()
                if (tuple(r[1] for r in fields)!=('id','source_sha256','source_present','activated','marker_value','export_sha256')
                        or tuple(r[1] for r in sorted(fields,key=lambda r:r[5]) if r[5])!=('id',)):
                    raise RetentionError('notification_retention_source_invalid')
                metadata=db.execute('SELECT * FROM metadata ORDER BY id').fetchmany(2)
                if (len(metadata)!=1 or metadata[0][0]!=1 or not _digest(metadata[0][1])
                        or any(type(v) is not int or v not in (0,1) for v in metadata[0][2:4])
                        or metadata[0][5] is not None and not _digest(metadata[0][5])):
                    raise RetentionError('notification_retention_source_invalid')
                from .notification_receipts import _marker
                saved_marker=_marker(json.loads(metadata[0][4]))
                if marker_value is None:raise RetentionError('notification_retention_source_invalid')
                rows=[(fp,p,None,None,state) for fp,s,l in db.execute('SELECT fingerprint,slack,lark FROM receipts') for p,state in [('slack',s),('lark',l)] if state is not None]
            else: rows=db.execute('SELECT fingerprint,provider,target_sha256,payload_sha256,state FROM receipts').fetchall()
    seen=set()
    for fp,provider,target,payload,state in rows:
        if (not _digest(fp) or provider not in PROVIDERS[family] or state not in ('uncertain','sent')
                or (fp,provider) in seen or family=='fallback' and not all(_digest(v) for v in (target,payload))):
            raise RetentionError('notification_retention_source_invalid')
        seen.add((fp,provider))
    if family=='dual':
        from .notification_receipts import _legacy, _marker
        if marker_value is not None and marker_value.get('schema_version')==1:
            incoming=_legacy(marker_value)
            # Supported old-sender export may append or confirm receipts. The
            # exported JSON cannot lose any prior provider claim or sent state.
            for fp,provider,_,__,state in rows:
                next_state=incoming.get(fp,{}).get(provider)
                if next_state is None or state=='sent' and next_state!='sent':
                    raise RetentionError('notification_retention_reconcile_loss')
            rows=[(fp,p,None,None,state) for fp,states in incoming.items() for p,state in states.items()]
        elif marker_value is not None:
            current=_marker(marker_value)
            if any(k in current and current[k]!=v for k,v in saved_marker.items()):
                raise RetentionError('notification_retention_marker_collision')
            saved_marker=dict(saved_marker,**current)
        if any(k in saved_marker for k in ('retention','plan_sha256')):
            raise RetentionError('notification_retention_marker_collision')
    result=dict(rows=sorted(rows),marker=saved_marker,actual_marker=marker_value,metadata=metadata)
    return result if with_metadata else result['rows']


def _atomic(path,value):
    from .storage import InstructionStore
    InstructionStore._atomic_json(path,value)


def _snapshot(db,family):
    meta=FiniteReceipts(db,family)
    records=db.execute('SELECT * FROM receipts ORDER BY fingerprint'+(',provider' if family=='fallback' else '')).fetchall()
    retention=db.execute('SELECT * FROM retention ORDER BY fingerprint,provider').fetchall()
    boundaries=db.execute('SELECT * FROM boundaries ORDER BY thread_id,scope,provider').fetchall()
    metadata = db.execute('SELECT value FROM finite_metadata WHERE id=1').fetchone()[0]
    return dict(family=family,plan_sha256=meta.plan,records=records,retention=retention,boundaries=boundaries,
                metadata=json.loads(metadata))


def _publish(directory,family,plan):
    initial_key = _initial_floor_key(plan, floor_field='born')
    path=_safe(directory/(NAMES[family]+'.sqlite3'))
    # A completed family is reused without rewriting later real claims.
    if path.exists():
        with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True)) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version==2:
                store = FiniteReceipts(db,family)
                if store.plan != plan['plan_sha256']:
                    raise RetentionError('notification_retention_preparation_collision')
                # Explicit cold preparation only. Ordinary entry never adds
                # observer metadata or repairs an unsupported prepared family.
                from .observer_receipt_scopes import prepare_observer_scopes
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                try:
                    prepare_observer_scopes(store, plan['born'], fresh=initial_key == '')
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                return
    if _sha(_legacy_rows(directory,family,with_metadata=True)) != plan['source_sha256'][family]:
        raise RetentionError('notification_retention_source_changed')
    fd,name=tempfile.mkstemp(prefix='.finite-receipts-',suffix='.sqlite3',dir=directory)
    os.close(fd);temporary=Path(name)
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.execute('PRAGMA synchronous=FULL');_schema(db,family,plan['plan_sha256'])
            from .observer_receipt_scopes import prepare_observer_scopes
            prepare_observer_scopes(FiniteReceipts(db,family),plan['born'],fresh=initial_key == '')
            for thread,scope,provider in plan['scopes']:
                if provider in PROVIDERS[family]:
                    db.execute('INSERT INTO boundaries VALUES(?,?,?,?,?,?)',(thread,scope,provider,plan['born'],initial_key,plan['born']))
            for row in plan['retained'][family]:
                fp,provider,thread,scope,born,expires,protected,target,payload,state=row
                if family=='dual':
                    db.execute('INSERT OR IGNORE INTO receipts(fingerprint) VALUES(?)',(fp,))
                    db.execute('UPDATE receipts SET '+provider+'=? WHERE fingerprint=?',(state,fp))
                else:db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',(fp,provider,target,payload,state))
                db.execute('INSERT INTO retention VALUES(?,?,?,?,?,?,?,?,?)',row[:-1])
            db.commit()
            expected=_snapshot(db,family)
        with closing(sqlite3.connect(temporary.as_uri()+'?mode=ro',uri=True)) as db:
            if _snapshot(db,family)!=expected:raise RetentionError('notification_retention_copy_invalid')
        with temporary.open('r+b') as stream:os.fsync(stream.fileno())
        # SQLite sidecars cannot survive replacing their original main file.
        # Caller has stopped senders; refuse a live/uncheckpointed source here.
        if any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
            raise RetentionError('notification_retention_source_sidecar_present')
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_notification_receipts(runtime, *, scopes, contexts, current_pins=(), sender_fence, clock=time.time,
                                  replan_prepared=False):
    """Explicit cold, fenced activation with compact process-crash recovery.

    scopes is the complete verified current (subject, scope_sha256, provider)
    projection of this selected runtime, not an inventory of unknown hosts.
    Native subjects remain canonical UUIDs. Observer subjects use the explicit
    observer:scope_hash discriminator and v2 context, never a synthetic UUID.
    contexts supplies original immutable event birth/identity for retained rows.
    current_pins are exact (fingerprint, provider) keys from the caller's verified
    current-native projection. Untimestamped old claims are covered by the import
    floor; they are never assigned a fabricated recent birth. The compact plan is
    recovery evidence, not a replay/old-sender export. Existing legacy bytes are
    not copied to a historical archive. Caller fences span the entire operation.
    """
    if not callable(sender_fence):raise RetentionError('notification_retention_fence_required')
    scope_set=set()
    for thread,scope,provider in scopes:
        _scope(thread,scope)
        if provider not in PROVIDERS['dual']+PROVIDERS['fallback']:raise RetentionError('notification_retention_scope_invalid')
        scope_set.add((thread,scope,provider))
    if not scope_set:raise RetentionError('notification_retention_scope_required')
    values={}
    for raw in contexts:
        ctx=context(raw);fp=ctx['fingerprint']
        if fp in values:raise RetentionError('notification_retention_context_collision')
        if any((_subject(ctx),ctx['scope_sha256'],p) not in scope_set for p in ctx['providers']):
            raise RetentionError('notification_retention_scope_invalid')
        values[fp]=ctx
    pins=set(tuple(v) for v in current_pins)
    if any(len(v)!=2 or not _digest(v[0]) or v[1] not in PROVIDERS['dual']+PROVIDERS['fallback'] for v in pins):
        raise RetentionError('notification_retention_pin_invalid')
    if any(fp in values and values[fp]['schema_version'] == 2 for fp, _ in pins):
        raise RetentionError('notification_retention_observer_pin_invalid')
    directory=_safe(Path(runtime).absolute()/'notifications')
    plan_path=_safe(directory/'finite-receipts-preparation.json')
    from .storage import FileLock
    with sender_fence(),common_sender_fence(runtime),FileLock(Path(runtime)/'locks/notifications.lock'):
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        prior=None;projection_changed=False
        if plan_path.exists():
            plan=json.loads(plan_path.read_bytes())
            if plan.get('plan_sha256')!=_sha({k:v for k,v in plan.items() if k!='plan_sha256'}):
                raise RetentionError('notification_retention_preparation_collision')
            initial_key = _initial_floor_key(plan, floor_field='born')
            projection_changed=plan.get('request_sha256')!=_sha(dict(scopes=sorted(scope_set),contexts=values,pins=sorted(pins)))
            if projection_changed:
                published=[]
                for family in PROVIDERS:
                    path=_safe(directory/(NAMES[family]+'.sqlite3'))
                    if path.exists():
                        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                            if db.execute('PRAGMA user_version').fetchone()[0]==2:
                                if FiniteReceipts(db,family).plan!=plan['plan_sha256']:
                                    raise RetentionError('notification_retention_preparation_collision')
                                published.append(family)
                # A fresh plan represents a namespace with no imported past;
                # forward completion must not replace zero with today's clock.
                if not published and initial_key != '':
                    if replan_prepared is not True:
                        raise RetentionError('notification_retention_prepared_replan_required')
                    prior=plan;plan=None
                # Partial activation finishes only the durable compact plan.
                # It is recovery, not a claim that its old pins are current.
        else:plan=None
        if plan is None:
            born=max(_time(clock()),prior['born'] if prior else 0);original={f:_legacy_rows(directory,f,with_metadata=True) for f in PROVIDERS}
            retained={f:[] for f in PROVIDERS};found=set();pools={}
            for family,source in original.items():
                for fp,provider,target,payload,state in source['rows']:
                    ctx=values.get(fp)
                    if ctx is None or provider not in ctx['providers']:continue
                    identity=ctx['providers'][provider]
                    if family=='fallback' and (target,payload)!=(identity['target_sha256'],identity['payload_sha256']):
                        raise RetentionError('notification_retention_identity_collision')
                    if ctx['born']>born:raise RetentionError('notification_retention_clock_invalid')
                    pin=(fp,provider) in pins;found.add((fp,provider))
                    row=[fp,provider,_subject(ctx),ctx['scope_sha256'],ctx['born'],ctx['born']+TTL,int(pin),identity['target_sha256'],identity['payload_sha256'],state]
                    if pin:retained[family].append(row)
                    elif row[5]>born:pools.setdefault((family,row[2],row[3],provider),[]).append(row)
            if not pins.issubset(found):raise RetentionError('notification_retention_pin_missing')
            for (family,_,__,___),rows in pools.items():
                retained[family].extend(sorted(rows,key=lambda r:(r[4],r[0]),reverse=True)[:LIMIT])
            retained={f:sorted(rows) for f,rows in retained.items()}
            plan=dict(schema_version=1,born=born,scopes=sorted(scope_set),retained=retained,
                      request_sha256=_sha(dict(scopes=sorted(scope_set),contexts=values,pins=sorted(pins))),
                      source_sha256={f:_sha(source) for f,source in original.items()},dual_marker=original['dual']['marker'],
                      rollback_policy='forward_recovery_only_no_old_sender')
            if prior is not None:plan['prior_prepared_sha256']=prior['plan_sha256']
            plan['plan_sha256']=_sha(plan)
            _atomic(plan_path,plan)
        for family in PROVIDERS:
            _publish(directory,family,plan)
            if family=='dual':
                marker_path=_safe(directory/'dual-deliveries.json')
                marker=json.loads(marker_path.read_bytes()) if marker_path.exists() else {}
                if marker.get('schema_version')==1:marker=dict(plan['dual_marker'])
                else:
                    if any(k in marker and marker[k]!=v for k,v in plan['dual_marker'].items() if k not in ('schema_version','storage')):
                        raise RetentionError('notification_retention_marker_collision')
                    marker=dict(plan['dual_marker'],**marker)
                marker.update(schema_version=3,storage='dual-deliveries.sqlite3',retention='finite-v1',plan_sha256=plan['plan_sha256'])
                _atomic(marker_path,marker)
        # New verified current scopes can be added without reading history or
        # lowering any old floor. A reconciles fresh pin changes before GC.
        if projection_changed:
            now=max(_time(clock()),plan['born'])
            initial_key = _initial_floor_key(plan, floor_field='born')
            floor = plan['born'] if initial_key == '' else now
            for family,providers in PROVIDERS.items():
                with closing(sqlite3.connect(directory/(NAMES[family]+'.sqlite3'))) as db:
                    db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
                    store = FiniteReceipts(db,family,clock=clock) if initial_key == '' else None
                    for thread,scope,provider in scope_set:
                        if provider in providers:
                            if store is not None:
                                store._enroll_current(thread,scope,provider,floor,now,initial_key)
                            else:
                                db.execute('INSERT OR IGNORE INTO boundaries VALUES(?,?,?,?,?,?)',(thread,scope,provider,floor,initial_key,now))
                    db.commit()
        gaps=[]
        if projection_changed:
            for fp,provider in sorted(pins):
                family=next(f for f,ps in PROVIDERS.items() if provider in ps)
                with closing(sqlite3.connect((directory/(NAMES[family]+'.sqlite3')).as_uri()+'?mode=ro',uri=True)) as db:
                    store=FiniteReceipts(db,family,receipt_context=values.get(fp),clock=clock)
                    row=store._row(fp,provider)
                    try:
                        if row is None:raise RetentionError('notification_retention_pin_missing')
                        store._check(fp,provider,row)
                    except RetentionError:
                        gaps.append(dict(fingerprint=fp,provider=provider,reason='current_pin_not_in_compact_recovery'))
        status='forward_completed' if projection_changed and prior is None else 'prepared'
        return dict(status=status+'_with_pin_gaps' if gaps else status,
                    plan_sha256=plan['plan_sha256'],retained={f:len(v) for f,v in plan['retained'].items()},
                    maintenance_required=projection_changed,pin_gaps=gaps,rollback_policy=plan['rollback_policy'])


def maintain_notification_receipts(runtime, *, scopes, pin_changes=(), complete, sender_fence, clock=time.time, budget=BUDGET):
    """Quiet bounded maintenance; complete is caller's verified native coverage.

    Unknown current coverage is a no-op. Pin changes are explicit native-work
    point identities, never inferred from historical receipt status. A supplies
    these inside its existing fences and schedules current scopes fairly.
    """
    if not callable(sender_fence):raise RetentionError('notification_retention_fence_required')
    if type(complete) is not bool:raise RetentionError('notification_retention_current_proof_invalid')
    if not complete:return dict(status='unknown',families={})
    scopes=sorted(set(tuple(s) for s in scopes));pin_changes=list(pin_changes)
    for thread,scope,provider in scopes:
        _scope(thread,scope)
        if provider not in PROVIDERS['dual']+PROVIDERS['fallback']:raise RetentionError('notification_retention_scope_invalid')
    if len(pin_changes)>16384:raise RetentionError('notification_retention_pin_budget')
    results={}
    from .storage import FileLock
    with sender_fence(),common_sender_fence(runtime),FileLock(Path(runtime)/'locks/notifications.lock'):
        for family,providers in PROVIDERS.items():
            try:
                path=_safe(Path(runtime).absolute()/'notifications'/(NAMES[family]+'.sqlite3'))
                if not path.is_file():raise RetentionError('notification_retention_database_missing')
                with closing(sqlite3.connect(path,isolation_level=None,timeout=1)) as db:
                    db.execute('PRAGMA synchronous=FULL')
                    store=FiniteReceipts(db,family,clock=clock)
                    results[family]=store.maintain([s for s in scopes if s[2] in providers],
                        [p for p in pin_changes if p[1] in providers],complete=complete,budget=budget)
            except (RetentionError,sqlite3.Error,OSError,ValueError,TypeError):
                results[family]=dict(status='unavailable',reason='notification_retention_family_unavailable',examined=0,removed=0)
    return dict(status='maintained' if all(v['status']=='maintained' for v in results.values()) else 'partial',families=results)
