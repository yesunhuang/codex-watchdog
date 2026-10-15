"""Explicit cold v2->v3 must not resurrect unretired logical N4 evictions."""
from datetime import timedelta
import json
import sqlite3

import pytest

from codex_watchdog.legacy_reply_tickets import LegacyReplyTickets
from codex_watchdog.reply_tickets import ReplyTickets
from codex_watchdog.relay_authority_migration import read_authority_snapshot
from codex_watchdog.session_authority_retention import compact_session_snapshot
from codex_watchdog.session_activity import SessionActivity
from codex_watchdog.legacy_ticket_cutoff import apply_legacy_ticket_cutoffs
from test_legacy_reply_tickets import build_schema2, thread_row, stamp, ticket, NAMESPACE, SESSION, MARKER, NOW, HOUR


def legacy(runtime, monkeypatch):
    monkeypatch.setattr(SessionActivity, 'is_active', lambda *a: True)
    parents = [thread_row('c-%02d' % n, SESSION, stamp(-60*HOUR+timedelta(minutes=n)), fingerprint='a'*64)
               for n in range(40)]
    build_schema2(runtime/'slack/reply-tickets.sqlite3', parents)
    (runtime/NAMESPACE).write_bytes(MARKER)
    journal = LegacyReplyTickets(runtime/NAMESPACE, runtime/'locks/provider.lock', 'slack', runtime,
        lambda: {}, clock=lambda: NOW.timestamp())
    with journal.transaction() as db:
        journal.record(db, 'new', ticket(SESSION, stamp(-HOUR), fingerprint='b'*64))
        for key in ('new', 'c-39', 'c-38', 'c-37'):
            assert journal.claim(db, key)
        assert db.execute("SELECT count(*) FROM records WHERE kind='threads' AND active=1").fetchone() == (5,)
    return journal


def test_inplace_cold_upgrade_applies_logical_eviction_without_deleting_recent_receipts(tmp_path, monkeypatch):
    old = legacy(tmp_path, monkeypatch)
    journal = ReplyTickets(old.path, old.lock_path, 'slack', tmp_path, lambda: {}, clock=lambda: NOW.timestamp())
    assert journal.migrate_recent_state()['status'] == 'migrated'
    with journal.transaction() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='threads'").fetchone() == (41,)
        assert db.execute("SELECT count(*) FROM records WHERE kind='threads' AND active=1").fetchone() == (0,)
        assert not journal.claim(db, 'c-00')
        body = json.loads(db.execute("SELECT value FROM records WHERE kind='provider_floors'").fetchone()[0])
        assert body['kind'] == 'legacy_ticket_cutoff_applied'


def test_exact_session_cold_projection_keeps_applied_boundary_after_anchor_expires(tmp_path, monkeypatch):
    old = legacy(tmp_path, monkeypatch)
    original = read_authority_snapshot(old.database)
    prepared = compact_session_snapshot(original, SESSION, now=NOW.timestamp())
    assert all(not row['active'] for row in prepared['records'] if row['kind'] == 'threads')
    assert sum(row['kind'] == 'threads' for row in prepared['records']) == 41
    future = compact_session_snapshot(prepared, SESSION, now=NOW.timestamp()+73*3600)
    assert not any(row['kind'] == 'threads' for row in future['records'])
    assert len(future['records']) == 1
    assert json.loads(future['records'][0]['value'])['kind'] == 'legacy_ticket_cutoff_applied'
    again = compact_session_snapshot(future, SESSION, now=NOW.timestamp()+74*3600)
    assert again['records'] == future['records']


def test_missing_original_anchor_refuses_cold_transfer_without_mutating_source(tmp_path, monkeypatch):
    old = legacy(tmp_path, monkeypatch)
    original = read_authority_snapshot(old.database)
    before = json.dumps(original, sort_keys=True)
    rows = [row for row in original['records'] if row['key'] != 'c-37']
    order = [identity for identity in original['record_order'] if identity[2] != 'c-37']
    with pytest.raises(ValueError, match='anchor_invalid'):
        apply_legacy_ticket_cutoffs(rows, order, source_schema=2)
    assert json.dumps(original, sort_keys=True) == before


def test_missing_order_refuses_affected_legacy_cutoff_before_synthesizing_rank(tmp_path, monkeypatch):
    from codex_watchdog.relay_authority_migration import snapshot_digest
    old = legacy(tmp_path, monkeypatch)
    original = read_authority_snapshot(old.database)
    original.pop('record_order')
    before = json.dumps(original, sort_keys=True)
    for operation, reason in ((lambda: snapshot_digest(original), 'migration_snapshot_invalid'),
                      (lambda: compact_session_snapshot(original, SESSION, now=NOW.timestamp()), 'cutoff_order_required'),
                      (lambda: apply_legacy_ticket_cutoffs(original['records'], None, source_schema=2), 'cutoff_order_required')):
        with pytest.raises(ValueError, match=reason):
            operation()
    assert json.dumps(original, sort_keys=True) == before


def test_applied_cutoff_needs_no_expired_original_order(tmp_path, monkeypatch):
    old = legacy(tmp_path, monkeypatch)
    value = compact_session_snapshot(read_authority_snapshot(old.database), SESSION, now=NOW.timestamp())
    value.pop('record_order')
    copied = compact_session_snapshot(value, SESSION, now=NOW.timestamp()+74*3600)
    assert len(copied['records']) == 1
    assert json.loads(copied['records'][0]['value'])['kind'] == 'legacy_ticket_cutoff_applied'
