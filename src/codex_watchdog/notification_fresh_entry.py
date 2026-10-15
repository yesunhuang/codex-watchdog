"""Bounded first entry for an exclusively created notification namespace.

The notifier retains its authority, actual selected sender and notification locks.
An existing directory is never evidence of freshness, even when empty. This
helper neither imports history nor repairs interrupted state; explicit offline
recovery owns that state. READY is published last, before the first effect claim.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time

from .messaging_profile import write_new
from .notification_receipt_retention import (FRESH_ORIGIN, FiniteReceipts, NAMES,
    PROVIDERS, TTL, RetentionError, _json, _safe, _schema, _sha, _subject, _time, context)
from .notification_root_retention import INTENT, READY, RETIRED, read_notification_root
from .observer_receipt_scopes import prepare_observer_scopes


def _write_new(path, value):
    write_new(path, (_json(value) + '\n').encode('utf-8'))


def _publish_family(directory, family, plan):
    """Only new fixed paths inside our exclusively owned empty directory."""
    temporary = directory / ('.fresh-' + family + '.sqlite3')
    final = directory / (NAMES[family] + '.sqlite3')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(temporary, timeout=1)) as db:
        db.execute('PRAGMA synchronous=FULL')
        _schema(db, family, plan['plan_sha256'])
        prepare_observer_scopes(FiniteReceipts(db, family), 0, fresh=True)
        for subject, scope, provider in plan['scopes']:
            if provider in PROVIDERS[family]:
                db.execute('INSERT INTO boundaries VALUES(?,?,?,?,?,?)',
                           (subject, scope, provider, 0, '', 0))
        db.commit()
    with temporary.open('r+b') as stream:
        os.fsync(stream.fileno())
    # Atomic no-clobber publication, as with existing new-profile writes.
    os.link(temporary, final)
    temporary.unlink()


def ensure_fresh_notification_entry(runtime, receipt_context, *, clock=time.time):
    """Called before CurrentNotificationState.claim, with real fences held.

    A valid original context authorizes no native action; it only supplies the
    first local notification's immutable identity. Both receipt families start
    empty. The zero floor is inclusive only under the explicit fresh tag, so
    neither epoch-zero nor an older genuine pre-initialization event is reborn
    or retired using the initialization clock. Normal TTL/claim guards remain.
    """
    runtime = _safe(Path(runtime).absolute())
    directory = _safe(runtime / 'notifications')
    if os.path.lexists(directory):
        if not directory.is_dir():
            raise RetentionError('notification_fresh_directory_invalid')
        # A published root's provider health remains independent. A pending root
        # cannot fall through to a partially written family or a legacy sender.
        pending = _safe(directory / INTENT)
        if pending.exists() and read_notification_root(runtime) is None:
            raise RetentionError('notification_fresh_offline_recovery_required')
        return dict(status='existing')
    value = context(receipt_context)
    now = _time(clock())
    if value['born'] > now or value['born'] + TTL <= now:
        raise RetentionError('notification_retention_event_retired')
    subject, scope = _subject(value), value['scope_sha256']
    scopes = sorted((subject, scope, provider)
                    for providers in PROVIDERS.values() for provider in providers)
    empty = dict(rows=[], marker={}, actual_marker=None, metadata=None)
    plan = dict(schema_version=1, origin=FRESH_ORIGIN, born=0.0, scopes=scopes,
                retained={family: [] for family in PROVIDERS},
                request_sha256=_sha(dict(scopes=scopes, contexts={}, pins=[])),
                source_sha256={family: _sha(empty) for family in PROVIDERS},
                dual_marker={}, rollback_policy='forward_recovery_only_no_old_sender')
    plan['plan_sha256'] = _sha(plan)
    # This exclusive creation, not a listing/stat of an existing namespace,
    # establishes freshness. Failure leaves visible state for offline recovery.
    directory.mkdir(mode=0o700)
    _write_new(directory / 'finite-receipts-preparation.json', plan)
    _write_new(directory / INTENT, dict(schema_version=1,
        storage='notification_root_intent_v1', relay=None, legacy_metadata=None,
        retired={name: None for name in RETIRED}))
    for family in PROVIDERS:
        _publish_family(directory, family, plan)
    _write_new(directory / 'dual-deliveries.json', dict(schema_version=3,
        storage='dual-deliveries.sqlite3', retention='finite-v1', plan_sha256=plan['plan_sha256']))
    ready = dict(schema_version=1, storage='finite_notification_root_v1', state='ready',
        origin=FRESH_ORIGIN, floor=0.0, plan_sha256=plan['plan_sha256'], relay=None,
        rollback_policy='forward_recovery_only_no_old_sender')
    ready['ready_sha256'] = _sha(ready)
    _write_new(directory / READY, ready)
    return dict(status='initialized', plan_sha256=plan['plan_sha256'], floor=0.0)
