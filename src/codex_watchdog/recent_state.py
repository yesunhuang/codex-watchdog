"""Finite recent journal state and compact current anti-replay boundaries.

History-sized work is restricted to explicit cold upgrade. The native caller
supplies proven current in-flight pins; historical uncertainty alone is not a
live owner, an immortal receipt, or a reason to replay an action.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import time
import uuid

from .messaging_profile import write_new

SCHEMA_VERSION = 3
TTL_SECONDS = 72 * 60 * 60
RECENT_LIMIT = 256
PRUNE_BUDGET = 32
CURRENT_KINDS = frozenset({"session_routes", "session_acl", "slack_bot_grants",
    "slack_bot_users", "relay_cursors", "relay_reconcile_cursor",
    "route_poll_cursors", "activation_state", "provider_floors"})
RECENT_KINDS = frozenset({"threads", "events", "messages", "notifications",
    "bind_challenges", "bind_operations", "bind_operation_messages", "bind_completions",
    "binding_seen", "binding_rejected", "route_commands", "route_messages",
    "acl_commands", "acl_messages", "slack_bot_controls", "slack_bot_requests",
    "slack_bot_receipts", "relay_native_receipts"})


def epoch(value):
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            value = parsed.astimezone(timezone.utc).timestamp()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return None
        return float(value)
    except (ValueError, TypeError, OverflowError):
        return None


def initialize(db):
    from .current_work_retention import create_reference_indexes
    create_reference_indexes(db)
    db.execute("CREATE TABLE IF NOT EXISTS recent_records (namespace TEXT NOT NULL,kind TEXT NOT NULL,"
               "key TEXT NOT NULL,born REAL NOT NULL,expires REAL NOT NULL,protected INTEGER NOT NULL DEFAULT 0,"
               "session TEXT NOT NULL,PRIMARY KEY(namespace,kind,key))")
    db.execute("CREATE INDEX IF NOT EXISTS recent_expiry ON recent_records(expires,namespace,kind,key) WHERE protected=0")
    db.execute("CREATE INDEX IF NOT EXISTS recent_pool ON recent_records(namespace,session,kind,born,key) WHERE protected=0")
    db.execute("CREATE INDEX IF NOT EXISTS recent_session_expiry ON recent_records(session,expires,namespace,kind,key) WHERE protected=0")
    db.execute("CREATE TABLE IF NOT EXISTS current_boundaries (namespace TEXT NOT NULL,session TEXT NOT NULL,"
               "floor REAL NOT NULL DEFAULT 0,checked_at REAL NOT NULL,PRIMARY KEY(namespace,session))")
    db.execute("CREATE INDEX IF NOT EXISTS namespace_pending_bindings ON records(namespace,key) "
               "WHERE active=1 AND kind='bind_challenges'")


def now(journal, db):
    current = epoch(journal.clock())
    if current is None:
        raise ValueError("recent_state_clock_invalid")
    if getattr(journal, "legacy_protocol", None) == 2:
        return current  # Schema2 compatibility does not invent schema3 boundaries.
    row = db.execute("SELECT checked_at FROM current_boundaries WHERE namespace=? AND session=''",
                     (journal.namespace,)).fetchone()
    if row is not None:
        stamp = epoch(row[0])
        if stamp is None:
            raise ValueError("recent_state_boundary_invalid")
        current = max(current, stamp)
    db.execute("INSERT INTO current_boundaries(namespace,session,floor,checked_at) VALUES(?,'',0,?) "
               "ON CONFLICT(namespace,session) DO UPDATE SET checked_at=max(checked_at,excluded.checked_at)",
               (journal.namespace,current))
    return current


def retention_deferred(journal):
    """Unknown caller-supplied current proof defers only retention deletion."""
    return bool(getattr(journal, "_retention_deferred", False))


def retention_allowed(journal, session):
    scope = getattr(journal, "_retention_session", None)
    return not retention_deferred(journal) and (scope is None or canonical_session(session) == scope)


def session_of(journal, value):
    target = value.get("target")
    session = value.get("thread_id") or (target.get("thread_id") if isinstance(target,dict) else None)
    authority = getattr(journal,"authority",None)
    return canonical_session(session or (authority.thread_id if authority is not None else ""))


def canonical_session(value):
    try:return str(uuid.UUID(value))
    except (ValueError,TypeError,AttributeError):return value


def _birth(row, value):
    first = epoch(row[7])
    return first if first is not None else epoch(value.get("created_at"))


def boundary(db, namespace, session):
    session=canonical_session(session)
    row = db.execute("SELECT floor FROM current_boundaries WHERE namespace=? AND session=?",
                     (namespace,session)).fetchone()
    return row[0] if row is not None else -1


def close_ticket(db, namespace, session, created, current):
    session=canonical_session(session)
    stamp = epoch(created)
    if stamp is None or not session:
        return
    db.execute("INSERT INTO current_boundaries(namespace,session,floor,checked_at) VALUES(?,?,?,?) "
               "ON CONFLICT(namespace,session) DO UPDATE SET floor=max(floor,excluded.floor),"
               "checked_at=max(checked_at,excluded.checked_at)", (namespace,session,stamp,current))


def track(journal, db, kind, key, value, *, namespace=None, protected=False):
    """Current auth/config never expires; recent updates never refresh birth."""
    if getattr(journal, "legacy_protocol", None) == 2 or kind not in RECENT_KINDS:
        return
    namespace = journal.namespace if namespace is None else namespace
    current = now(journal,db)
    session = session_of(journal,value)
    born = epoch(value.get("created_at")) if kind == "threads" else current
    born = current if born is None else born
    expires = born + TTL_SECONDS
    prior = db.execute("SELECT born,expires FROM recent_records WHERE namespace=? AND kind=? AND key=?",
                       (namespace,kind,key)).fetchone()
    if prior is not None:
        born, expires = prior
    db.execute("INSERT INTO recent_records VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,kind,key) "
               "DO UPDATE SET session=excluded.session,protected=max(protected,excluded.protected)",
               (namespace,kind,key,born,expires,int(protected),session))
    if kind == "bind_challenges":
        # A newly admitted challenge generation is a new short-lived action;
        # its polling never extends the immutable challenge expiry.
        expiry = epoch(value.get("expires_at"))
        active = value.get("status") == "pending" and expiry is not None and expiry > current
        db.execute("UPDATE records SET active=? WHERE namespace=? AND kind=? AND key=?",
                   (int(active),namespace,kind,key))
    cap(journal,db,namespace,session,kind,current)


def _remove(journal, db, rows, current):
    for namespace,kind,key in rows:
        if kind == "threads":
            journal.retire_projection(db,namespace,key)
            row = db.execute("SELECT thread_id,created_at FROM records WHERE namespace=? AND kind=? AND key=?",
                             (namespace,kind,key)).fetchone()
            if row:
                close_ticket(db,namespace,row[0],row[1],current)
        db.execute("DELETE FROM records WHERE namespace=? AND kind=? AND key=?",(namespace,kind,key))
        db.execute("DELETE FROM recent_records WHERE namespace=? AND kind=? AND key=?",(namespace,kind,key))


def cap(journal, db, namespace, session, kind, current):
    if not retention_allowed(journal, session):
        return  # Unknown current proof: keep the bounded pool until proven.
    rows = db.execute("SELECT r.namespace,r.kind,r.key FROM recent_records r INDEXED BY recent_pool "
        "WHERE r.namespace=? AND r.session=? AND r.kind=? AND r.protected=0 "
        "AND NOT (r.kind='threads' AND EXISTS(SELECT 1 FROM records v WHERE "
        "v.namespace=r.namespace AND v.kind=r.kind AND v.key=r.key AND v.active=1)) "
        "ORDER BY r.born DESC,r.key DESC LIMIT ? OFFSET ?",
        (namespace,session,kind,PRUNE_BUDGET,RECENT_LIMIT)).fetchall()
    _remove(journal,db,rows,current)


def prune(journal, db):
    current = now(journal,db)
    if retention_deferred(journal):
        return  # Unknown current proof: expired points stay until proven.
    scope = getattr(journal, "_retention_session", None)
    if scope is None:
        rows = db.execute("SELECT namespace,kind,key FROM recent_records INDEXED BY recent_expiry "
                          "WHERE protected=0 AND expires<=? LIMIT ?",(current,PRUNE_BUDGET)).fetchall()
    else:
        rows = db.execute("SELECT namespace,kind,key FROM recent_records INDEXED BY recent_session_expiry "
                          "WHERE protected=0 AND session=? AND expires<=? LIMIT ?",
                          (scope,current,PRUNE_BUDGET)).fetchall()
    _remove(journal,db,rows,current)


def protect(journal, db, kind, key, protected=True):
    db.execute("UPDATE recent_records SET protected=? WHERE namespace=? AND kind=? AND key=?",
               (int(protected),journal.namespace,kind,key))


def live_ticket(journal, db, value):
    created = epoch(value.get("created_at")) if isinstance(value,dict) else None
    current = now(journal,db)
    return created is not None and created <= current and current-created < TTL_SECONDS


def migrate(journal, *, protected_keys=()):
    """Explicit, lock-protected schema2 upgrade, never a polling/startup path."""
    import sqlite3
    from contextlib import closing
    from .storage import FileLock
    lock = journal.authority.journal_lock(journal.provider) if journal.authority is not None else FileLock(journal.lock_path)
    journal.database.parent.mkdir(parents=True,exist_ok=True)
    pins = set(tuple(item) for item in protected_keys)
    if any(len(item)!=3 or any(not isinstance(part,str) for part in item) for item in pins):
        raise ValueError("recent_state_current_pin_invalid")
    with lock, closing(sqlite3.connect(journal.database,timeout=1)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            known=db.execute('SELECT 1 FROM sources WHERE namespace=?',(journal.namespace,)).fetchone()
            if known is None and journal.path.exists():
                return _import_legacy_namespace(journal,db)
            journal._marker(explicit=True)
            return dict(status="unchanged",schema_version=SCHEMA_VERSION)
        if version == 0:
            if not journal.path.exists():
                return dict(status="fresh",schema_version=SCHEMA_VERSION)
            original=journal.path.read_bytes()  # Explicit legacy upgrade only.
            legacy=journal.legacy_reader()
            if not isinstance(legacy,dict) or legacy.get("schema_version")!=1:
                raise ValueError("recent_state_legacy_schema_invalid")
            compact=journal.path.with_name(journal.path.name+".legacy-current-v1.json")
            header={key:value for key,value in legacy.items() if key not in ("threads","events","messages","notifications")}
            recovery=dict(schema_version=1,source_sha256=hashlib.sha256(original).hexdigest(),legacy_header=header)
            if compact.exists():
                if json.loads(compact.read_bytes())!=recovery:
                    raise ValueError("recent_state_legacy_backup_changed")
            else:write_new(compact,(json.dumps(recovery,sort_keys=True)+'\n').encode())
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE sources(namespace TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE records(namespace TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,"
                       "value TEXT NOT NULL,fingerprint TEXT,thread_id TEXT,active INTEGER NOT NULL DEFAULT 0,"
                       "created_at TEXT,PRIMARY KEY(namespace,kind,key))")
            db.execute("CREATE INDEX notification_lookup ON records(namespace,kind,fingerprint)")
            db.execute("CREATE INDEX thread_lookup ON records(namespace,kind,thread_id)")
            db.execute("INSERT INTO sources VALUES(?)",(journal.namespace,))
            for kind in ("threads","events","messages","notifications"):
                for key,value in legacy.get(kind,{}).items():
                    target=value.get("target")
                    thread=target.get("thread_id") if isinstance(target,dict) else value.get("thread_id")
                    db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,0,?)",(journal.namespace,kind,key,
                        json.dumps(value,sort_keys=True),value.get("event_fingerprint"),thread,value.get("created_at")))
            db.execute("PRAGMA user_version=2")
            db.commit()
            version=2
        if version not in (1,2):
            raise ValueError("recent_state_explicit_source_schema_unsupported")
        current = epoch(journal.clock())
        if current is None:
            raise ValueError("recent_state_clock_invalid")
        columns = tuple(row[1] for row in db.execute("PRAGMA table_info(records)"))
        if columns != ("namespace","kind","key","value","fingerprint","thread_id","active","created_at"):
            raise ValueError("recent_state_source_schema_invalid")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT namespace,kind,key,value,fingerprint,thread_id,active,created_at "
                          "FROM records ORDER BY rowid").fetchall()
        from .legacy_ticket_cutoff import apply_legacy_ticket_cutoffs
        fields = ('namespace','kind','key','value','fingerprint','thread_id','active','created_at')
        prepared = apply_legacy_ticket_cutoffs([dict(zip(fields, row)) for row in rows],
                    [row[:3] for row in rows], source_schema=version)
        rows = [tuple(row[name] for name in fields) for row in prepared]
        selected, removed, active, pools = {}, [], {}, {}
        for row in rows:
            identity = row[:3]
            if row[1] not in RECENT_KINDS or identity in pins:
                selected[identity] = row
                continue
            try:
                value=json.loads(row[3])
                if not isinstance(value,dict):raise ValueError()
                born=_birth(row,value)
            except (ValueError,TypeError):
                value={}
                born=None
                row=(*row[:6],0,row[7])
            if born is None:
                # Keep only a bounded quarantine horizon for an opaque record.
                # It cannot become an active ticket or an invented completion.
                born=current
                row=(*row[:6],0,row[7])
            if born>current or current-born>=TTL_SECONDS:
                removed.append(row)
                continue
            target=value.get("target")
            session=canonical_session(value.get("thread_id") or (target.get("thread_id") if isinstance(target,dict) else None) or row[5] or "")
            pools.setdefault((row[0],session,row[1]),[]).append((born,row))
        for pool, values in pools.items():
            values.sort(key=lambda item:(item[0],item[1][2]),reverse=True)
            for born,row in values[:RECENT_LIMIT]:
                selected[row[:3]]=row
            removed.extend(row for _,row in values[RECENT_LIMIT:])
        for identity,row in list(selected.items()):
            if row[1]=="threads" and row[6]:
                active.setdefault(row[5].lower() if isinstance(row[5],str) else row[5],[]).append(row)
        for session, tickets in active.items():
            tickets.sort(key=lambda row:(epoch(row[7]) or -1,row[2]),reverse=True)
            for row in tickets[4:]:
                selected[row[:3]]=(*row[:6],0,row[7])
        backup = journal.database.with_name(journal.database.name + ".current-v2-backup.json")
        protected = dict(schema_version=2,rows=[list(row) for row in selected.values()],
                         sources=[row[0] for row in db.execute("SELECT namespace FROM sources")],
                         source_schema=version,target_schema=3,created_at=current,
                         rollback_policy="no_active_downgrade_after_new_claims",pins=[list(item) for item in sorted(pins)])
        payload=(json.dumps(protected,sort_keys=True,indent=2)+'\n').encode()
        if backup.is_symlink():raise ValueError("recent_state_backup_redirected")
        if backup.exists():
            if backup.read_bytes()!=payload:
                old=json.loads(backup.read_bytes())
                # Crash before commit: protect the exact same selected rows, not
                # a second changing archive or a silently replaced snapshot.
                if old.get("rows")!=protected["rows"] or old.get("sources")!=protected["sources"]:
                    raise ValueError("recent_state_backup_changed")
        else:
            write_new(backup,payload)
        initialize(db)
        journal._active_indexes(db)
        db.execute("DELETE FROM recent_records")
        for row in removed:
            if row[1]=="threads":close_ticket(db,row[0],row[5],row[7],current)
        db.execute("DELETE FROM records")
        for row in selected.values():
            db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)",row)
            if row[1] in RECENT_KINDS:
                try:value=json.loads(row[3])
                except ValueError:value={}
                if not isinstance(value,dict):value={}
                born=_birth(row,value)
                born=current if born is None else born
                target=value.get("target")
                session=canonical_session(value.get("thread_id") or (target.get("thread_id") if isinstance(target,dict) else None) or row[5] or "")
                db.execute("INSERT INTO recent_records VALUES(?,?,?,?,?,?,?)",(*row[:3],born,born+TTL_SECONDS,int(row[:3] in pins),session))
                if row[1]=="threads" and not row[6]:close_ticket(db,row[0],session,row[7],current)
                if row[1]=="bind_challenges":
                    pending=value.get("status")=="pending" and (epoch(value.get("expires_at")) or 0)>current
                    db.execute("UPDATE records SET active=? WHERE namespace=? AND kind=? AND key=?",(int(pending),*row[:3]))
        db.execute("PRAGMA user_version=3")
        db.commit()
        journal._marker(explicit=True)
        # One-time physical compaction is explicit and happens only after the
        # protected backup and committed replacement are usable.
        db.execute("VACUUM")
        return dict(status="migrated",schema_version=3,retained_records=len(selected),
                    retired_records=len(rows)-len(selected),protected_count=len(pins))


def _import_legacy_namespace(journal,db):
    """Explicit late namespace import; ordinary transactions never call this."""
    original=journal.path.read_bytes()
    legacy=journal.legacy_reader()
    if not isinstance(legacy,dict) or legacy.get('schema_version')!=1:
        raise ValueError('recent_state_legacy_namespace_invalid')
    stamp=epoch(journal.clock())
    if stamp is None:raise ValueError('recent_state_clock_invalid')
    selected=[]
    for kind in ('threads','events','messages','notifications'):
        values=legacy.get(kind,{})
        if not isinstance(values,dict):raise ValueError('recent_state_legacy_namespace_invalid')
        retained=[]
        for key,value in values.items():
            if not isinstance(key,str) or not isinstance(value,dict):continue
            born=epoch(value.get('created_at'))
            if born is not None and (born>stamp or stamp-born>=TTL_SECONDS):continue
            retained.append((born if born is not None else stamp,key,value))
        retained.sort(key=lambda entry:(entry[0],entry[1]),reverse=True)
        selected.extend((kind,key,value) for _,key,value in retained[:RECENT_LIMIT])
    header={key:value for key,value in legacy.items() if key not in ('threads','events','messages','notifications')}
    recovery=dict(schema_version=1,source_sha256=hashlib.sha256(original).hexdigest(),legacy_header=header,
        protected_recent=selected,rollback_policy='forward_recovery_only')
    backup=journal.path.with_name(journal.path.name+'.legacy-current-v1.json')
    raw=(json.dumps(recovery,sort_keys=True)+'\n').encode()
    if backup.is_symlink():raise ValueError('recent_state_backup_redirected')
    if backup.exists():
        if backup.read_bytes()!=raw:raise ValueError('recent_state_legacy_backup_changed')
    else:write_new(backup,raw)
    db.execute('BEGIN IMMEDIATE')
    for kind,key,value in selected:
        journal.put(db,kind,key,value)
        # An old legacy parent never proves unused one-shot authorization.
        db.execute('UPDATE records SET active=0 WHERE namespace=? AND kind=? AND key=?',(journal.namespace,kind,key))
        if kind=='threads':close_ticket(db,journal.namespace,session_of(journal,value),value.get('created_at'),now(journal,db))
    db.execute('INSERT INTO sources VALUES(?)',(journal.namespace,))
    db.commit()
    journal._marker(explicit=True)
    return dict(status='namespace_imported',schema_version=3,retained_records=len(selected),legacy_tickets_retired=True)
