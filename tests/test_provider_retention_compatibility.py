"""Actual provider factory paths: unknown GC and schema2 remain usable."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from codex_watchdog.legacy_reply_tickets import LegacyReplyTickets
from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.models import sha256_text
from codex_watchdog.onebot_relay import OneBotThreadStore
from codex_watchdog.provider_activity import message_is_current
from codex_watchdog.recent_state import TTL_SECONDS, track
from codex_watchdog.relay import RelayTarget
from codex_watchdog.slack_mapping import SlackThreadStore
from test_legacy_reply_tickets import build_schema2, MARKER

THREAD = '00000000-0000-4000-8000-000000000001'
T0 = 1_800_000_000.5


def stamp(current):
    return datetime.fromtimestamp(current, timezone.utc).isoformat().replace('+00:00','Z')


def store_for(runtime, provider, clock):
    if provider == 'slack':
        return SlackThreadStore(runtime, clock=clock)
    cls = LarkThreadStore if provider == 'lark' else OneBotThreadStore
    return cls(runtime, 'a'*64, clock=clock)


@pytest.mark.parametrize('provider', ['slack','lark','onebot'])
def test_unsupported_schema3_proof_defers_all_deletion_but_not_new_claim(tmp_path,provider):
    current = [T0]
    store = store_for(tmp_path,provider,lambda:current[0])
    journal = store.journal
    with journal.transaction() as db:
        assert journal._retention_deferred
        journal.put(db,'events','original',dict(thread_id=THREAD,state='uncertain'))
    current[0] += TTL_SECONDS+1
    journal = store.journal
    with journal.transaction() as db:
        assert journal._retention_deferred
        assert journal.get(db,'events','original')['state']=='uncertain'
        journal.put(db,'events','independent',dict(thread_id=THREAD,state='dispatching'))
    with sqlite3.connect(journal.database) as db:
        assert db.execute('SELECT key FROM records WHERE kind="events" ORDER BY key').fetchall()==[('independent',),('original',)]
        assert db.execute('SELECT count(*) FROM recent_records').fetchone()==(2,)


@pytest.mark.parametrize('provider', ['slack','lark','onebot'])
def test_saved_schema2_provider_prepare_finish_and_activity_check_use_no_schema3_tables(tmp_path,provider):
    store = store_for(tmp_path,provider,lambda:T0)
    before = store.journal
    build_schema2(before.database, sources=(before.namespace,))
    before.path.parent.mkdir(parents=True,exist_ok=True)
    before.path.write_bytes(MARKER)
    target = RelayTarget('fixture-workspace',THREAD,'process_local')
    fingerprint = sha256_text('new exact notification')
    if provider == 'slack':
        address = store.thread_key('C12345678','1800000000.500000')
        store.record_thread('C12345678','1800000000.500000',target,fingerprint,created_at=stamp(T0))
    else:
        chat,message=('oc_'+32*'a','om_'+32*'b') if provider=='lark' else ('private:1234','5678')
        address=store._address(chat,message)
        assert store.prepare_notification(fingerprint,sha256_text('payload')) is None
        store.finish_notification(fingerprint,chat,message,target)
    assert isinstance(store.journal,LegacyReplyTickets)
    assert message_is_current(store,address,T0,resolution=1 if provider=='onebot' else 0)
    assert not message_is_current(store,address,T0-2,resolution=1 if provider=='onebot' else 0)
    journal=store.journal
    with journal.transaction() as db:
        entry=journal.get(db,'threads',address)
        assert entry['target']['thread_id']==THREAD
        journal.put(db,'events','prior-uncertainty',dict(thread_id=THREAD,state='uncertain'))
        track(journal,db,'slack_bot_receipts','new-ack',dict(created_at=stamp(T0)),namespace='bot/receipts')
        assert db.execute('PRAGMA user_version').fetchone()==(2,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name IN ('recent_records','current_boundaries')").fetchall()==[]
    assert before.path.read_bytes()==MARKER
    with journal.transaction() as db:
        assert journal.claim(db,address)
        assert not journal.claim(db,address)
        assert journal.get(db,'events','prior-uncertainty')['state']=='uncertain'



def test_schema2_point_expiration_remains_closed_after_clock_rollback(tmp_path):
    current=[T0]
    store=SlackThreadStore(tmp_path,clock=lambda:current[0])
    journal=store.journal
    build_schema2(journal.database,sources=(journal.namespace,))
    journal.path.parent.mkdir(parents=True,exist_ok=True)
    journal.path.write_bytes(MARKER)
    target=RelayTarget('fixture-workspace',THREAD,'process_local')
    store.record_thread('C12345678','1800000000.500000',target,sha256_text('original'),created_at=stamp(T0))
    key=store.thread_key('C12345678','1800000000.500000')
    current[0]+=TTL_SECONDS
    journal=store.journal
    with journal.transaction() as db:
        assert journal.get(db,'threads',key) is None
        assert db.execute("SELECT active,created_at FROM records WHERE kind='threads'").fetchone()==(0,stamp(T0))
    current[0]=T0+1
    journal=store.journal
    with journal.transaction() as db:
        assert journal.active(db)=={}
        assert not journal.claim(db,key)
