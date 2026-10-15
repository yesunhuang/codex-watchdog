"""The existing Linux native owner supplies proof without a second client."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from codex_watchdog.control_context import acting_as
from codex_watchdog.control_state import ControlStore
from codex_watchdog import linux_native_work as linux
from codex_watchdog.native_inflight import validate_inventory
from codex_watchdog.native_inflight_pins import CurrentNativePins
from codex_watchdog.native_stream_work import NativeStreamWork
from test_linux_owner import owner, setup, THREAD, OTHER, QUEUE
from test_native_inflight import rows_for, provider_db, association, flags


NOW=1792008000.0
TURN="11111111-2222-4333-8444-555555555556"


def sha(value):return hashlib.sha256(value.encode()).hexdigest()
def wrapped(identifier,prompt):return "[CODEX_WATCHDOG_WAKE id="+identifier+" sha256="+sha(prompt)+"]\n"+prompt


@pytest.fixture
def work(owner,monkeypatch):
    instance,client,cycles,pid=owner
    instance.step(observe=False)
    home=instance.binding.codex_home
    workspace=instance.binding.workspace(instance.binding.load())
    rollout=home/"sessions"/("rollout-current-"+THREAD+".jsonl")
    rollout.write_text("{}\n")
    with sqlite3.connect(home/"state_5.sqlite") as db:
        db.execute("CREATE UNIQUE INDEX native_thread ON threads(id)")
        db.execute("UPDATE threads SET rollout_path=? WHERE id=?",(str(rollout),THREAD))
    with sqlite3.connect(home/"queue_1.sqlite") as db:
        db.execute("ALTER TABLE queued_items ADD COLUMN queue_order INTEGER DEFAULT 0")
        db.execute("CREATE UNIQUE INDEX native_id ON queued_items(id)")
        db.execute("CREATE INDEX native_thread_queue ON queued_items(thread_id,queue_order)")
        db.execute("CREATE UNIQUE INDEX native_revision ON queued_thread_revisions(thread_id)")
        db.execute("INSERT INTO queued_thread_revisions VALUES(?,0)",(THREAD,))
    store=ControlStore(home,THREAD,workspace.repo_root,clock=lambda:NOW,boot_id="current-fixture-boot")
    initial=store.attach("initial-vscode-owner","fixture-locality","vscode",ttl=900)
    store.detach(initial)
    token=store.claim_remote("existing-controller","fixture-locality","absent",ttl=900)
    state=SimpleNamespace(status="idle",generation=0,reason=None,alive=True,hook=None)
    client.process.poll=lambda:None if state.alive else 0
    client.recovery_snapshot=lambda:(state.generation,state.reason)
    requests=[]
    def request(method,params,timeout=30):
        requests.append((method,deepcopy(params),timeout))
        if state.hook is not None:state.hook()
        return {"thread":{"id":THREAD,"cwd":str(workspace.repo_root),"status":{"type":state.status}}}
    client.request=request
    instance.native_work.clock=lambda:NOW
    monkeypatch.setattr(linux,"control_node_name",lambda:"fixture-native-node")
    return SimpleNamespace(owner=instance,client=client,state=state,store=store,token=token,
        native=instance.native_work,home=home,repo=workspace.repo_root,runtime=instance.binding.runtime,
        now=NOW,pid=pid,requests=requests,rollout=rollout)


def collect(work,**kwargs):
    with acting_as(work.store,work.token):
        return work.native.collect(fence=nullcontext,**kwargs)


def started(work,turn=TURN):
    work.owner._event(dict(method="turn/started",params=dict(threadId=THREAD,turn=dict(id=turn,status="inProgress"))))


def queued(work,identifier="slackbot:current",prompt="same original task"):
    raw=json.dumps({"UserInput":{"content":[{"type":"text","text":wrapped(identifier,prompt)}]}})
    with sqlite3.connect(work.home/"queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES(?,?,?,?)",(QUEUE,THREAD,raw,0))
        db.execute("UPDATE queued_thread_revisions SET revision=revision+1 WHERE thread_id=?",(THREAD,))
    return raw


def test_idle_proof_reuses_exact_owned_client_request_and_preserves_native_bytes(work):
    before={p:p.read_bytes() for p in (work.home/"state_5.sqlite",work.home/"queue_1.sqlite",work.rollout)}
    value=collect(work)
    assert value["status"]=="complete" and value["native"]["status"]=="idle"
    validate_inventory(value,work.home,THREAD,work.repo,clock=lambda:NOW)
    assert work.requests==[("thread/read",{"threadId":THREAD,"includeTurns":False},5)]*2
    assert work.owner.client is work.client and work.owner.resumed and not work.client.closed
    assert all(p.read_bytes()==raw for p,raw in before.items())


def test_actual_same_thread_turn_start_without_input_is_unknown_but_current_q_is_positive(work):
    work.state.status="active";started(work)
    queued(work)
    value=collect(work)
    assert value["status"]=="unknown" and value["queue"]["complete"]
    assert value["native"]["status"]=="active"
    assert value["queue"]["items"][0]["instruction_id"]=="slackbot:current"
    assert "same original task" not in json.dumps(value)
    # The real indexed pin API can protect known current-Q references before
    # provider pruning, without inventing active-input or completion evidence.
    rows=rows_for("slackbot:current","same original task",thread=THREAD)[:3]
    db=provider_db(work,rows)
    pins=CurrentNativePins(work.runtime,work.home,THREAD,work.repo,clock=lambda:NOW)
    db.execute("BEGIN IMMEDIATE")
    pins.register(association(rows,"slackbot:current","same original task",QUEUE),value,db=db,fence=nullcontext)
    db.commit()
    assert flags(db)==[(1,)]*3
    with sqlite3.connect(work.home/"queue_1.sqlite") as q:
        q.execute("DELETE FROM queued_items");q.execute("UPDATE queued_thread_revisions SET revision=revision+1")
    work.owner._event(dict(method="turn/completed",params=dict(threadId=THREAD,turn=dict(id=TURN,status="completed"))))
    after=collect(work)
    db.execute("BEGIN IMMEDIATE")
    assert pins.reconcile(after,db=db,fence=nullcontext)["status"]=="unknown"
    db.commit()
    assert flags(db)==[(1,)]*3
    work.state.status="idle"
    idle=collect(work)
    db.execute("BEGIN IMMEDIATE")
    assert pins.reconcile(idle,db=db,fence=nullcontext)["retired"]==1
    db.commit();db.close()
    assert pins.export()["bindings"]==[]


@pytest.mark.parametrize("fault",["no_capability","no_client","not_resumed","writer","dead_client","wrong_scope","stale_token"])
def test_unsupported_current_owner_is_local_unknown_without_new_client(work,fault):
    if fault=="no_client":work.owner.client=None
    elif fault=="not_resumed":work.owner.resumed=False
    elif fault=="writer":work.pid[0]=999
    elif fault=="dead_client":work.state.alive=False
    elif fault=="wrong_scope":work.owner.thread=OTHER
    if fault=="stale_token":
        with acting_as(work.store,work.token):
            with work.store.guard(work.token) as value:
                value["epoch"]+=1
                from codex_watchdog.control_state import control_atomic_json
                control_atomic_json(work.store.path,value)
            value=work.native.collect(fence=nullcontext)
    elif fault=="no_capability":
        value=work.native.collect(fence=nullcontext)
    else:value=collect(work)
    assert value["status"]=="unknown" and not value["queue"]["complete"]
    assert not work.requests
    assert not work.client.closed


@pytest.mark.parametrize("fault",["writer","client","generation","owner"])
def test_recheck_rejects_rpc_race_without_retry_or_native_effect(work,fault):
    def change():
        if fault=="writer":work.pid[0]=999
        elif fault=="client":work.owner.client=SimpleNamespace()
        elif fault=="generation":work.state.generation+=1
        else:
            value=work.store.read();value["epoch"]+=1
            from codex_watchdog.control_state import control_atomic_json
            control_atomic_json(work.store.path,value)
    work.state.hook=change
    value=collect(work)
    assert value["status"]=="unknown" and len(work.requests)==1
    assert not work.client.closed


@pytest.mark.parametrize("event",["foreign_thread","missing_turn","bad_turn","completed","gap","replacement"])
def test_stale_or_incomplete_turn_metadata_never_becomes_current_input(work,event):
    work.state.status="active";started(work)
    if event=="foreign_thread":
        work.owner._event(dict(method="turn/started",params=dict(threadId=OTHER,turn=dict(id=OTHER,status="inProgress"))))
    elif event=="missing_turn":work.owner._event(dict(method="turn/started",params=dict(threadId=THREAD)))
    elif event=="bad_turn":started(work,"not-a-supported-turn")
    elif event=="completed":work.owner._event(dict(method="turn/completed",params=dict(threadId=THREAD)))
    elif event=="gap":work.state.generation+=1
    else:
        work.native._client=object()
    with acting_as(work.store,work.token):proof=work.native.current_native()
    assert proof["thread_read"]["thread"]["status"]["type"]=="active"
    assert proof["current_input"] is None
    if event=="foreign_thread":assert proof["current_turn"]["id"]==TURN
    else:assert proof["current_turn"] is None
    assert collect(work)["status"]=="unknown"


def test_old_observation_gap_does_not_fence_fresh_correlated_idle_rpc(work):
    work.state.reason="app_server_frame_discarded";work.state.generation=2
    value=collect(work)
    assert value["status"]=="complete" and value["native"]["status"]=="idle"
    assert work.state.reason=="app_server_frame_discarded"  # no fake recovery acknowledgement


def test_optional_input_failure_does_not_invent_input_or_hide_positive_queue(work):
    work.state.status="active";started(work);queued(work)
    def failed():raise OSError("synthetic stream failure")
    value=collect(work,input_reader=failed)
    assert value["status"]=="unknown" and value["queue"]["complete"]
    assert value["queue"]["items"][0]["queue_id"]==QUEUE


def test_current_turn_change_during_optional_stream_read_refuses_stale_join(work):
    work.state.status="active";started(work)
    def changed():
        started(work,OTHER)
        return {"untrusted":"never accepted as native evidence"}
    value=collect(work,input_reader=changed)
    assert value["status"]=="unknown" and not value["queue"]["complete"]
    assert value["reason"]=="linux_native_work_observation_changed"


def stream_reader(work):
    stream=NativeStreamWork(work.home,clock=lambda:NOW)
    read=lambda:stream.observe(THREAD,work.repo,current_owner=work.native.current_owner,fence=nullcontext)
    with acting_as(work.store,work.token):
        assert read()["native"]["status"]=="unknown"
    return stream,read


def append_stream(work,kind,turn=TURN,**extra):
    value=dict(timestamp=datetime.fromtimestamp(NOW,timezone.utc).isoformat(),ordinal=12,
        type="event_msg",payload=dict(type=kind,turn_id=turn,**extra))
    with work.rollout.open("a") as handle:handle.write(json.dumps(value)+"\n")


def stream_started(work,turn=TURN):
    append_stream(work,"task_started",turn)
    append_stream(work,"item_completed",turn,item=dict(type="UserMessage",
        content=[dict(type="text",text=wrapped("slackbot:current","same original task"))]))


def test_actual_owned_rpc_and_observed_stream_protect_same_turn_after_queue_consumed(work):
    stream,read=stream_reader(work)
    work.state.status="active";started(work);stream_started(work)
    queued(work)
    active=collect(work,input_reader=read)
    assert active["status"]=="complete" and active["native"]["turn_id"]==TURN
    assert active["native"]["evidence_kind"]=="app_server_rpc+native_rollout_stream"
    assert active["native"]["instruction_id"]=="slackbot:current"
    assert "same original task" not in json.dumps(active)
    validate_inventory(active,work.home,THREAD,work.repo,clock=lambda:NOW)
    rows=rows_for("slackbot:current","same original task",thread=THREAD)[:3]
    db=provider_db(work,rows)
    pins=CurrentNativePins(work.runtime,work.home,THREAD,work.repo,clock=lambda:NOW)
    db.execute("BEGIN IMMEDIATE")
    pins.register(association(rows,"slackbot:current","same original task",QUEUE),active,db=db,fence=nullcontext)
    db.commit()
    with sqlite3.connect(work.home/"queue_1.sqlite") as q:
        q.execute("DELETE FROM queued_items")
        q.execute("UPDATE queued_thread_revisions SET revision=revision+1")
    running=collect(work,input_reader=read)
    assert running["status"]=="complete" and running["queue"]["items"]==[]
    db.execute("BEGIN IMMEDIATE")
    assert pins.reconcile(running,db=db,fence=nullcontext)["retired"]==0
    db.commit()
    assert flags(db)==[(1,)]*3
    # The rollout terminal plus a reliable completion callback cannot override
    # a still-active actual thread/read response or retire the current binding.
    append_stream(work,"task_complete",last_agent_message="synthetic final")
    work.owner._event(dict(method="turn/completed",params=dict(threadId=THREAD,turn=dict(id=TURN,status="completed"))))
    terminal_only=collect(work,input_reader=read)
    assert terminal_only["status"]=="unknown" and terminal_only["native"]["status"]=="active"
    db.execute("BEGIN IMMEDIATE")
    assert pins.reconcile(terminal_only,db=db,fence=nullcontext)["status"]=="unknown"
    db.commit()
    assert flags(db)==[(1,)]*3
    work.state.status="idle"
    def unexpected_stream_read():raise AssertionError("idle must use actual RPC only")
    idle=collect(work,input_reader=unexpected_stream_read)
    assert idle["status"]=="complete" and idle["native"]["status"]=="idle"
    db.execute("BEGIN IMMEDIATE")
    assert pins.reconcile(idle,db=db,fence=nullcontext)["retired"]==1
    db.commit();db.close()
    assert pins.export()["bindings"]==[]
    assert work.owner.client is work.client and not work.client.closed
    assert work.requests==[("thread/read",{"threadId":THREAD,"includeTurns":False},5)]*8


@pytest.mark.parametrize("fault",["foreign_turn","foreign_owner","restart","no_start"])
def test_actual_stream_mismatch_never_fabricates_active_input_but_preserves_positive_q(work,fault):
    stream,read=stream_reader(work)
    work.state.status="active";started(work);queued(work)
    if fault!="no_start":stream_started(work,OTHER if fault=="foreign_turn" else TURN)
    else:
        append_stream(work,"item_completed",item=dict(type="UserMessage",
            content=[dict(type="text",text=wrapped("slackbot:current","same original task"))]))
    if fault=="restart":
        stream=NativeStreamWork(work.home,clock=lambda:NOW)
        read=lambda:stream.observe(THREAD,work.repo,current_owner=work.native.current_owner,fence=nullcontext)
    elif fault=="foreign_owner":
        # A genuine stream envelope for another owner remains valid in its own
        # scope, but cannot be joined to this actual existing-client RPC.
        def foreign_owner():
            value=work.native.current_owner()
            value["owner"]["epoch"]+=1
            return value
        foreign=NativeStreamWork(work.home,clock=lambda:NOW)
        with acting_as(work.store,work.token):
            foreign.observe(THREAD,work.repo,current_owner=foreign_owner,fence=nullcontext)
        stream_started(work)
        read=lambda:foreign.observe(THREAD,work.repo,current_owner=foreign_owner,fence=nullcontext)
    value=collect(work,input_reader=read)
    assert value["status"]=="unknown" and value["queue"]["complete"]
    assert value["native"]["status"]=="active" and value["native"]["instruction_id"] is None
    assert value["queue"]["items"][0]["instruction_id"]=="slackbot:current"
    assert len(work.requests)==2 and not work.client.closed
