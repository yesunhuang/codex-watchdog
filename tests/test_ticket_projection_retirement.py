from datetime import datetime,timezone
import sqlite3
import pytest

from codex_watchdog.control_state import ControlStore
from codex_watchdog.reply_tickets import ReplyTickets

THREAD='11111111-1111-4111-8111-111111111111'


def fixture(tmp_path,monkeypatch):
    clock=[1800000000.0]
    journal=ReplyTickets(tmp_path/'slack/state.json',tmp_path/'locks/state.lock','slack',tmp_path,lambda:{},clock=lambda:clock[0])
    owner=ControlStore(tmp_path/'native',THREAD,tmp_path/'repo',clock=lambda:clock[0],boot_id='fixture')
    monkeypatch.setattr('codex_watchdog.control_context.current_control',lambda:(owner,{}))
    def entry(number):
        return dict(channel_id='C12345678',thread_ts='1800000000.'+str(100+number),
            event_fingerprint=format(number,'064x'),target=dict(thread_id=THREAD),
            created_at=datetime.fromtimestamp(clock[0],timezone.utc).isoformat().replace('+00:00','Z'))
    return clock,journal,owner,entry


def test_projection_close_happens_after_authoritative_commit(tmp_path,monkeypatch):
    clock,journal,owner,entry=fixture(tmp_path,monkeypatch)
    captured=[]
    def close(provider,keys,**kw):
        with sqlite3.connect(journal.database) as db:
            assert db.execute("SELECT active FROM records WHERE kind='threads'").fetchone()==(0,)
        captured.append((provider,keys,kw))
    monkeypatch.setattr(owner,'retire_relay_mappings',close)
    with journal.transaction() as db:journal.record(db,'parent',entry(1))
    with journal.transaction() as db:assert journal.claim(db,'parent')
    assert len(captured)==1 and captured[0][0]=='slack'
    assert list(captured[0][2]['created_at_by_key'].values())==[entry(1)['created_at']]


def test_failed_projection_cannot_reopen_ticket_or_rollback_admission(tmp_path,monkeypatch):
    clock,journal,owner,entry=fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(owner,'retire_relay_mappings',lambda *a,**k:(_ for _ in ()).throw(ValueError('optional projection corrupt')))
    with journal.transaction() as db:journal.record(db,'parent',entry(1))
    with journal.transaction() as db:assert journal.claim(db,'parent')
    with journal.transaction() as db:assert not journal.claim(db,'parent')


def test_rollback_does_not_publish_projection_close(tmp_path,monkeypatch):
    _,journal,owner,entry=fixture(tmp_path,monkeypatch)
    captured=[]
    monkeypatch.setattr(owner,'retire_relay_mappings',lambda *a,**kw:captured.append(a))
    with journal.transaction() as db:journal.record(db,'parent',entry(1))
    with pytest.raises(ValueError):
        with journal.transaction() as db:
            assert journal.claim(db,'parent')
            raise ValueError('before commit')
    assert not captured
    # A fresh transaction must not flush a close from the rolled-back attempt.
    with journal.transaction() as db:assert journal.get(db,'threads','parent') is not None
    assert not captured
