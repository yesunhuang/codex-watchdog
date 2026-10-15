"""C334's same-key index counterexamples must refuse before payload work."""
import sqlite3

import pytest

from codex_watchdog.legacy_reply_tickets import LegacyReplyTickets
from test_legacy_reply_tickets import (build_schema2, thread_row, stamp, SESSION,
                                      NAMESPACE, MARKER, NOW, HOUR)


@pytest.mark.parametrize('history',[0,10000])
@pytest.mark.parametrize('name,columns',[
    ('session_active_tickets','thread_id COLLATE NOCASE,created_at,namespace,key'),
    ('namespace_active_tickets','namespace,created_at,key')])
@pytest.mark.parametrize('predicate',[''," WHERE kind='threads'"," WHERE active=1 OR kind='threads'",
    " WHERE active=1 OR kind='threads' -- WHERE active=1 AND kind='threads'",
    " WHERE active=1 OR kind='threads' /* WHERE active=1 AND kind='threads' */"])
def test_same_keys_require_exact_supported_active_predicate(tmp_path, history, name, columns, predicate):
    database = tmp_path/'slack/reply-tickets.sqlite3'
    rows = [thread_row('old-%06d'%n, SESSION, stamp(-100*HOUR), active=0) for n in range(history)]
    rows.append(thread_row('current', SESSION, stamp(-HOUR)))
    build_schema2(database, rows)
    (tmp_path/NAMESPACE).write_bytes(MARKER)
    with sqlite3.connect(database) as db:
        db.execute('DROP INDEX '+name)
        db.execute('CREATE INDEX '+name+' ON records('+columns+')'+predicate)
    before = database.read_bytes()
    journal = LegacyReplyTickets(tmp_path/NAMESPACE,tmp_path/'locks/provider.lock','slack',
        tmp_path,lambda:pytest.fail('no JSON import'),clock=lambda:NOW.timestamp())
    with pytest.raises(ValueError,match='legacy_schema_invalid'):
        with journal.transaction():
            pytest.fail('unsupported predicate reached journal body')
    assert database.read_bytes() == before and (tmp_path/NAMESPACE).read_bytes() == MARKER
