"""C331 counterexamples and independent logical-eviction persistence."""
from datetime import timedelta
import json
import sqlite3

import pytest

from codex_watchdog.legacy_reply_tickets import LegacyReplyTickets
from codex_watchdog.session_activity import SessionActivity
from test_legacy_reply_tickets import (build_schema2, thread_row, stamp, ticket,
    NAMESPACE, SECOND, SESSION, OTHER, MARKER, NOW, HOUR)


def journal_at(runtime, history, parents, monkeypatch, sources=(NAMESPACE,)):
    monkeypatch.setattr(SessionActivity, 'is_active', lambda *a: True)
    database = runtime / 'slack/reply-tickets.sqlite3'
    rows = (thread_row('historic-%08d' % n, SESSION, stamp(-100*HOUR), active=0)
            for n in range(history))
    build_schema2(database, list(rows) + parents, sources)
    (runtime / NAMESPACE).write_bytes(MARKER)
    return LegacyReplyTickets(runtime/NAMESPACE, runtime/'locks/provider.lock', 'slack', runtime,
        lambda: pytest.fail('no JSON import'), clock=lambda: NOW.timestamp())


@pytest.mark.parametrize('history', [0, 10000])
def test_last_four_survivor_is_ineligible_after_bounded_cleanup_and_later_consumption(tmp_path, monkeypatch, history):
    parents = [thread_row('c-%02d' % n, SESSION, stamp(-60*HOUR + timedelta(minutes=n))) for n in range(40)]
    journal = journal_at(tmp_path, history, parents, monkeypatch)
    with journal.transaction() as db:
        journal.record(db, 'k-new', ticket(SESSION, stamp(-HOUR)))
        assert db.execute("SELECT count(*) FROM records WHERE kind='threads' AND active=1").fetchone() == (9,)
        assert not journal.claim(db, 'c-00')
        for key in ('k-new', 'c-39', 'c-38', 'c-37'):
            assert journal.claim(db, key)
    restarted = LegacyReplyTickets(journal.path, journal.lock_path, 'slack', tmp_path,
        lambda: {}, clock=lambda: NOW.timestamp())
    with restarted.transaction() as db:
        assert not restarted.claim(db, 'c-01')
        assert restarted.active(db) == {}
        assert db.execute("SELECT count(*) FROM records WHERE kind='threads'").fetchone() == (history+41,)


def test_boundary_is_per_provider_session_across_namespaces(tmp_path, monkeypatch):
    parents = [thread_row('c-%02d' % n, SESSION, stamp(-60*HOUR + timedelta(minutes=n))) for n in range(40)]
    parents.append(thread_row('other', OTHER, stamp(-HOUR)))
    journal = journal_at(tmp_path, 0, parents, monkeypatch, (NAMESPACE, SECOND))
    second = LegacyReplyTickets(tmp_path/SECOND, journal.lock_path, 'slack', tmp_path,
        lambda: {}, clock=lambda: NOW.timestamp())
    with second.transaction() as db:
        second.record(db, 'new-scope', ticket(SESSION, stamp()))
    with journal.transaction() as db:
        assert not journal.claim(db, 'c-00')
        assert journal.claim(db, 'other')
    with second.transaction() as db:
        assert second.claim(db, 'new-scope')
    with journal.transaction() as db:
        for key in ('c-39', 'c-38', 'c-37'):
            assert journal.claim(db, key)
        assert not journal.claim(db, 'c-01')


def remove_pk(database):
    with sqlite3.connect(database) as db:
        indexes = db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall()
        for name, _ in indexes:
            db.execute('DROP INDEX ' + name)
        db.execute('ALTER TABLE records RENAME TO previous_records')
        db.execute('CREATE TABLE records(namespace TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,'
            'value TEXT NOT NULL,fingerprint TEXT,thread_id TEXT,active INTEGER NOT NULL DEFAULT 0,created_at TEXT)')
        db.execute('INSERT INTO records SELECT * FROM previous_records')
        db.execute('DROP TABLE previous_records')
        for _, sql in indexes:
            db.execute(sql)


@pytest.mark.parametrize('history', [0, 10000])
@pytest.mark.parametrize('fault', ['missing_records_pk', 'wrong_active_index', 'wrong_collation'])
def test_unsupported_schema_refuses_before_provider_body_or_payload_work(tmp_path, monkeypatch, history, fault):
    journal = journal_at(tmp_path, history, [thread_row('current', SESSION, stamp(-HOUR))], monkeypatch)
    if fault == 'missing_records_pk':
        remove_pk(journal.database)
    else:
        with sqlite3.connect(journal.database) as db:
            if fault == 'wrong_active_index':
                db.execute('DROP INDEX namespace_active_tickets')
                db.execute('CREATE INDEX namespace_active_tickets ON records(namespace)')
            else:
                db.execute('DROP INDEX session_active_tickets')
                db.execute("CREATE INDEX session_active_tickets ON records(thread_id,created_at,namespace,key) "
                           "WHERE active=1 AND kind='threads'")
    original = journal.database.read_bytes()
    with pytest.raises(ValueError, match='legacy_schema_invalid'):
        with journal.transaction():
            pytest.fail('unsupported PK/index reached provider payload body')
    assert journal.database.read_bytes() == original


@pytest.mark.parametrize('history', [0, 1000, 10000, 100000])
def test_valid_point_and_logical_rank_work_remains_independent_of_closed_history(tmp_path, monkeypatch, history, capsys):
    journal = journal_at(tmp_path, history, [thread_row('current', SESSION, stamp(-HOUR))], monkeypatch)
    with journal.transaction() as db:
        statements, operations = [], [0]
        db.set_trace_callback(statements.append)
        db.set_progress_handler(lambda: operations.__setitem__(0, operations[0]+1) or 0, 1)
        assert journal.get(db, 'threads', 'current') is not None
        db.set_trace_callback(None)
        db.set_progress_handler(None, 0)
        assert len(statements) <= 5 and operations[0] < 250
        with capsys.disabled():
            print(json.dumps(dict(schema2_point=dict(history=history, sql=len(statements), vm=operations[0]))))


def test_oversized_equal_birth_group_refuses_ticket_only_and_preserves_independent_write(tmp_path, monkeypatch):
    parents = [thread_row('tie-%02d' % n, SESSION, stamp(-HOUR)) for n in range(80)]
    journal = journal_at(tmp_path, 0, parents, monkeypatch)
    with journal.transaction() as db:
        assert not journal.claim(db, 'tie-00')
        journal.put(db, 'events', 'independent', dict(thread_id=SESSION, state='uncertain'))
    with journal.transaction() as db:
        assert journal.get(db, 'events', 'independent')['state'] == 'uncertain'
