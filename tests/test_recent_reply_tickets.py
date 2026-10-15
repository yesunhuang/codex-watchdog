from datetime import datetime,timezone
import json
import sqlite3

import pytest

from codex_watchdog.reply_tickets import ReplyTickets
from codex_watchdog.recent_state import TTL_SECONDS, RECENT_LIMIT
from codex_watchdog.session_activity import SessionActivity

THREAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def test_default_creation_clock_does_not_use_a_coarser_system_tick(tmp_path,monkeypatch):
    from codex_watchdog.models import utc_now
    monkeypatch.setattr("codex_watchdog.reply_tickets.time.time",lambda:0)
    journal=ReplyTickets(tmp_path/"slack/state.json",tmp_path/"locks/state.lock","slack",tmp_path,lambda:{})
    for number in range(8):
        with journal.transaction() as db:
            journal.record(db,str(number),dict(target=dict(thread_id=THREAD),
                event_fingerprint=format(number,"064x"),created_at=utc_now()))
    with journal.transaction() as db:
        assert set(journal.active(db))=={"4","5","6","7"}


def test_explicit_late_legacy_namespace_import_is_bounded_and_idempotent(tmp_path):
    clock=[1800000000.0]
    first=store(tmp_path,clock)
    with first.transaction():pass
    legacy=dict(schema_version=1,threads={},events={format(i,'064x'):dict(state='uncertain') for i in range(300)},messages={},notifications={})
    second=ReplyTickets(tmp_path/'slack/legacy.json',tmp_path/'locks/legacy.lock','slack',tmp_path,lambda:legacy,clock=lambda:clock[0])
    second.path.write_text(json.dumps(legacy))
    with pytest.raises(ValueError):
        with second.transaction():pass
    assert second.migrate_recent_state()['status']=='namespace_imported'
    with second.transaction() as db:
        assert db.execute("SELECT count(*) FROM records WHERE namespace=? AND kind='events'",(second.namespace,)).fetchone()[0]==RECENT_LIMIT
    before=second.path.read_bytes()
    assert second.migrate_recent_state()['status']=='unchanged'
    assert second.path.read_bytes()==before


def store(tmp_path, clock, *, provider="slack", name="state.json"):
    return ReplyTickets(tmp_path/provider/name,tmp_path/"locks"/(provider+name+".lock"),
                        provider,tmp_path,lambda:{},clock=lambda:clock[0])


def ticket(clock, number=0, thread=THREAD):
    return dict(target=dict(thread_id=thread),event_fingerprint=format(number,"064x"),
                created_at=datetime.fromtimestamp(clock[0],timezone.utc).isoformat())


@pytest.mark.parametrize("age,live", [(TTL_SECONDS-60,True),(TTL_SECONDS,False),(TTL_SECONDS+1,False)])
def test_absolute_ticket_age_controls_read_claim_and_poll(tmp_path,age,live):
    clock=[1800000000.0]
    journal=store(tmp_path,clock)
    with journal.transaction() as db:journal.record(db,"parent",ticket(clock))
    clock[0]+=age
    with journal.transaction() as db:
        assert bool(journal.active(db)) is live
        assert bool(journal.get(db,"threads","parent")) is live
        assert journal.claim(db,"parent") is live


def test_eviction_is_per_provider_and_exact_session_across_namespaces(tmp_path):
    clock=[1800000000.0]
    first=store(tmp_path,clock)
    second=store(tmp_path,clock,name="other.json")
    lark=store(tmp_path,clock,provider="lark")
    for number in range(5):
        clock[0]+=1
        selected=first if number%2 else second
        with selected.transaction() as db:selected.record(db,str(number),ticket(clock,number))
    with first.transaction() as db:
        rows=db.execute("SELECT key FROM records WHERE kind='threads' AND active=1").fetchall()
        assert {row[0] for row in rows}=={"1","2","3","4"}
        first.record(db,"other-session",ticket(clock,8,OTHER))
    with lark.transaction() as db:
        lark.record(db,"independent",ticket(clock,9))
        assert len(lark.active(db))==1


def test_closed_parent_never_reopens_after_receipt_is_physically_pruned(tmp_path):
    clock=[1800000000.0]
    journal=store(tmp_path,clock)
    original=ticket(clock)
    with journal.transaction() as db:
        journal.record(db,"parent",original)
        assert journal.claim(db,"parent")
    clock[0]+=TTL_SECONDS+1
    with journal.transaction() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='threads'").fetchone()[0]==0
        journal.record(db,"parent",original)
        assert journal.active(db)=={}
        assert db.execute("SELECT count(*) FROM records WHERE kind='threads'").fetchone()[0]==0
    clock[0]-=TTL_SECONDS+10  # boundary retains the observed high water
    with journal.transaction() as db:
        journal.record(db,"parent",original)
        assert not journal.claim(db,"parent")


def test_recent_updates_do_not_renew_claim_birth_and_live_pin_survives(tmp_path):
    clock=[1800000000.0]
    journal=store(tmp_path,clock)
    with journal.transaction() as db:
        journal.put(db,"events","claimed",dict(target=dict(thread_id=THREAD),state="uncertain"))
        journal.protect(db,"events","claimed")
        journal.put(db,"session_acl","acl",dict(thread_id=THREAD,delegates=["verified"],unknown="preserved"))
    clock[0]+=TTL_SECONDS+1
    with journal.transaction() as db:
        assert journal.get(db,"events","claimed") is not None
        journal.put(db,"events","claimed",dict(target=dict(thread_id=THREAD),state="delivered"))
        journal.protect(db,"events","claimed",False)
    with journal.transaction() as db:
        assert journal.get(db,"events","claimed") is None
        assert journal.get(db,"session_acl","acl")["unknown"]=="preserved"


def test_dormant_tracking_preserves_auth_but_rejects_reply_eligibility(tmp_path):
    clock=[1800000000.0]
    journal=store(tmp_path,clock)
    lifecycle=SessionActivity(tmp_path,clock=lambda:clock[0])
    lifecycle.observe(THREAD,genuine_at=clock[0]-TTL_SECONDS,turn_active=False,queue_active=False)
    with journal.transaction() as db:
        journal.record(db,"recent-watchdog-output",ticket(clock))
        journal.put(db,"session_acl","acl",dict(thread_id=THREAD,delegates=["verified"]))
        assert not journal.active(db) and journal.get(db,"threads","recent-watchdog-output") is None
        assert not journal.claim(db,"recent-watchdog-output")
        assert journal.get(db,"session_acl","acl")["delegates"]==["verified"]


def test_explicit_previous_schema_upgrade_preserves_current_and_retires_history(tmp_path):
    clock=[1800000000.0]
    journal=store(tmp_path,clock)
    with journal.transaction() as db:
        journal.record(db,"current",ticket(clock))
        journal.put(db,"session_routes","route",dict(thread_id=THREAD,destination="current",unknown="keep"))
        journal.put(db,"events","old",dict(target=dict(thread_id=THREAD),created_at="2020-01-01T00:00:00Z",state="uncertain"))
    with sqlite3.connect(journal.database) as db:
        db.execute("DROP TABLE recent_records")
        db.execute("DROP TABLE current_boundaries")
        db.execute("PRAGMA user_version=2")
    marker=json.loads(journal.path.read_text());marker.update(schema_version=2,extra="preserve")
    journal.path.write_text(json.dumps(marker))
    with pytest.raises(ValueError,match="migration_required"):
        with journal.transaction():pass
    result=journal.migrate_recent_state()
    assert result["schema_version"]==3 and result["retired_records"]==1
    assert journal.migrate_recent_state()["status"]=="unchanged"
    with journal.transaction() as db:
        assert journal.active(db)
        assert journal.get(db,"session_routes","route")["unknown"]=="keep"
        assert journal.get(db,"events","old") is None
    backup=json.loads(journal.database.with_name(journal.database.name+".current-v2-backup.json").read_text())
    assert all(row[2]!="old" for row in backup["rows"])
    assert json.loads(journal.path.read_text())["extra"]=="preserve"
