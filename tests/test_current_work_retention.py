"""Real pre-prune journals, indexed original references and native/Q fixtures."""
import json
from contextlib import nullcontext
import sqlite3

import pytest

from codex_watchdog.current_work_retention import CurrentWorkRetention, current_associations
from codex_watchdog.reply_tickets import ReplyTickets
from test_native_inflight import (rig, THREAD, OTHER, NS, QUEUE, rows_for, provider_db,
                                 enqueue, remove_queue, wrapped, sha, flags)


def binding(rig):
    return CurrentWorkRetention(rig.runtime, rig.home, THREAD, rig.repo,
        reader=lambda: rig.inventory(), fence=rig.fence, clock=lambda: rig.now)


def journal(rig, retention):
    return ReplyTickets(rig.runtime/NS, rig.runtime/"locks/slack.lock", "slack", rig.runtime,
        lambda: {}, clock=lambda: rig.now, before_prune=retention.before_prune,
        retention_fence=retention.fence)


def prepare(rig):
    identifier, prompt = "slackbot:"+sha("production-hook")[:40], "current original work"
    current = rows_for(identifier, prompt)
    old = rows_for("slackbot:"+sha("old")[:40], "past uncertain work", suffix="old")
    db = provider_db(rig, current+old)
    db.execute("CREATE TABLE sources(namespace TEXT PRIMARY KEY)")
    db.execute("INSERT INTO sources VALUES(?)",(NS,));db.commit();db.close()
    enqueue(rig, identifier, prompt)
    return identifier, prompt, current, old


def test_current_references_are_pinned_before_prune_then_retired_by_true_idle(rig):
    identifier, prompt, current, old = prepare(rig)
    retained = binding(rig)
    with journal(rig,retained).transaction() as db:
        assert rig.held
        assert flags(db)==[(1,)]*len(current)
        assert db.execute("SELECT count(*) FROM records").fetchone()==(len(current),)
    remove_queue(rig)
    rig.native_status="active";rig.active_text=wrapped(identifier,prompt)
    with journal(rig,retained).transaction() as db:
        assert flags(db)==[(1,)]*len(current)
    rig.native_status="idle";rig.active_text=None
    with journal(rig,retained).transaction() as db:
        assert db.execute("SELECT count(*) FROM records").fetchone()==(0,)
    assert retained.pins.export()["bindings"]==[]


def test_positive_queue_only_pins_commit_but_unknown_current_turn_defers_gc(rig):
    _, _, current, old = prepare(rig)
    rig.native_status="active";rig.active_text=None
    retained=binding(rig)
    j=journal(rig,retained)
    with j.transaction() as db:
        assert j._retention_deferred
        assert db.execute("SELECT count(*) FROM records").fetchone()==(len(current+old),)
        assert sum(row[0] for row in flags(db))==len(current)
        j.put(db,"events","fresh",dict(thread_id=THREAD,created_at="2026-10-01T00:00:00Z"))
    assert not j._retention_deferred and j._retention_session is None
    assert retained.pins.export()["bindings"]


def test_unknown_queue_refuses_only_gc_and_independent_record_commits(rig):
    _, _, current, old = prepare(rig)
    (rig.home/"queue_1.sqlite").unlink()
    retained=binding(rig);j=journal(rig,retained)
    with j.transaction() as db:
        assert j._retention_deferred
        j.put(db,"events","fresh",dict(thread_id=THREAD))
    with sqlite3.connect(j.database) as db:
        assert db.execute("SELECT count(*) FROM records").fetchone()==(len(current+old)+1,)


def test_one_session_evidence_cannot_retire_another_sessions_rows(rig):
    prepare(rig)
    retained=binding(rig);j=journal(rig,retained)
    with sqlite3.connect(j.database) as db:
        db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)",(NS,"events","foreign","{}",None,OTHER,0,None))
        db.execute("INSERT INTO recent_records VALUES(?,?,?,?,?,?,?)",(NS,"events","foreign",0,1,0,OTHER))
    with j.transaction() as db:
        assert j.get(db,"events","foreign")=={}
    with sqlite3.connect(j.database) as db:
        assert db.execute("SELECT 1 FROM records WHERE key='foreign'").fetchone()==(1,)


def test_actual_transaction_commit_stays_inside_the_supplied_fence(rig,monkeypatch):
    prepare(rig)
    retained=binding(rig);seen=[]
    connect=sqlite3.connect
    def traced(*args,**kwargs):
        db=connect(*args,**kwargs)
        db.set_trace_callback(lambda sql: seen.append(rig.held) if sql=="COMMIT" else None)
        return db
    monkeypatch.setattr("codex_watchdog.reply_tickets.sqlite3.connect",traced)
    with journal(rig,retained).transaction():pass
    assert seen and all(held>0 for held in seen)
    assert rig.held==0


@pytest.mark.parametrize("index,columns,condition", [
    ("native_event_work", "namespace", "kind='events'"),
    ("native_event_work", "thread_id", "kind='events'"),
    ("native_event_work", "thread_id,json_extract(value,'$.instruction_id')", "kind='events'"),
    ("native_request_event", "namespace", "kind='slack_bot_requests'"),
    ("native_ack_event", "json_extract(value,'$.source_namespace')", "kind='slack_bot_receipts'"),
])
def test_replaced_wrong_leading_index_defers_gc_before_any_history_scan(rig,index,columns,condition):
    prepare(rig)
    retained=binding(rig);j=journal(rig,retained)
    with sqlite3.connect(j.database) as db:
        db.execute("DROP INDEX " + index)
        db.execute("CREATE INDEX " + index + " ON records(" + columns + ") WHERE " + condition + " AND json_valid(value)")
    with j.transaction() as db:
        assert j._retention_deferred
        j.put(db,"events","independent",dict(thread_id=THREAD))
    assert retained.last_result is None
    with sqlite3.connect(j.database) as db:
        assert db.execute("SELECT count(*) FROM records").fetchone()==(13,)


@pytest.mark.parametrize("history",[0,1000,10000,100000])
def test_original_key_lookup_uses_fixed_indexed_work_under_history_growth(rig,history,capsys):
    prepare(rig)
    with sqlite3.connect(rig.runtime/"slack/reply-tickets.sqlite3") as db:
        db.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)",(
            (NS,"events","old-%s"%i,json.dumps(dict(instruction_id="slackbot:old-%s"%i,text_sha256=sha("past"))),
             None,THREAD,0,None) for i in range(history)))
        operations=[0];statements=[]
        db.set_progress_handler(lambda:operations.__setitem__(0,operations[0]+1) or 0,1)
        db.set_trace_callback(statements.append)
        result=current_associations(db,rig.inventory())
        assert len(result)==1 and len(result[0]["refs"])==6
        assert len(statements)<=12 and operations[0]<500
        measured=dict(history=history,sql=len(statements),vm=operations[0])
        plan=db.execute("EXPLAIN QUERY PLAN SELECT namespace,key FROM records INDEXED BY native_event_work "
            "WHERE kind='events' AND json_valid(value) AND thread_id=? AND json_extract(value,'$.instruction_id')=? "
            "AND json_extract(value,'$.text_sha256')=? LIMIT 2",(THREAD,"slackbot:fixture",sha("fixture"))).fetchall()
        assert "SEARCH" in str(plan) and "INDEX native_event_work" in str(plan) and "SCAN" not in str(plan)
        with capsys.disabled():print(json.dumps(dict(native_reference_lookup=measured,query_plan=plan)))


@pytest.mark.parametrize("history", [0, 10000])
def test_incomplete_search_refuses_before_visiting_any_historical_session_row(rig,history,capsys):
    from codex_watchdog.native_inflight import NativeInflightError
    prepare(rig)
    with sqlite3.connect(rig.runtime/"slack/reply-tickets.sqlite3") as db:
        db.execute("DROP INDEX native_event_work")
        db.execute("CREATE INDEX native_event_work ON records(thread_id) WHERE kind='events' AND json_valid(value)")
        db.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)", (
            (NS,"events","old-%s"%i,json.dumps(dict(instruction_id="slackbot:old-%s"%i,text_sha256=sha("past"))),
             None,THREAD,0,None) for i in range(history)))
        operations=[0];statements=[]
        db.set_progress_handler(lambda:operations.__setitem__(0,operations[0]+1) or 0,1)
        db.set_trace_callback(statements.append)
        with pytest.raises(NativeInflightError,match="native_retention_index_unavailable"):
            current_associations(db,rig.inventory())
        assert statements==[] and operations[0]<500
        with capsys.disabled():print(json.dumps(dict(refused_incomplete_search=dict(history=history,sql=len(statements),vm=operations[0]))))
