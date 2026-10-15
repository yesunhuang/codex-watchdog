"""Observed editor work is distinct from owned-client/control authority."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from codex_watchdog import native_inflight as native
from codex_watchdog.native_inflight_pins import CurrentNativePins
from codex_watchdog.native_observed_work import ObservedNativeStreamWork
from codex_watchdog.native_stream_work import NativeStreamWork, validate_stream_work
from codex_watchdog.session_authority_retention import _inventory
from test_native_inflight import (rig as native_rig, THREAD, OTHER, TURN, QUEUE,
    enqueue, remove_queue, rows_for, provider_db, association, flags, sha, wrapped)


BOOT="11111111-2222-4333-8444-000000000008"


@pytest.fixture
def rig(native_rig):
    r=native_rig
    r.path=r.home/"sessions"/("rollout-current-"+THREAD+".jsonl")
    r.path.write_bytes(b"")
    r.writer=dict(kind="observed_native_writer",platform="linux",node="fixture-node",
        pid=123,process_birth="linux:"+BOOT+":12345",
        lock_path=str(r.home/"thread-writer-locks"/(THREAD+".lock")),lock_device=1,lock_inode=2)
    r.producer=ObservedNativeStreamWork(r.home,clock=lambda:r.now)
    def current_writer():
        assert r.held
        r.calls+=1
        return dict(codex_home=str(r.home),thread_id=THREAD,repo_path=str(r.repo),
                    observed_at=r.now,writer=deepcopy(r.writer))
    r.current_writer=current_writer
    r.observe=lambda:r.producer.observe(THREAD,r.repo,current_writer=current_writer,fence=r.fence)
    r.collect=lambda:native.collect_native_work(r.home,THREAD,r.repo,current_native=r.observe,
                                               fence=r.fence,clock=lambda:r.now)
    return r


def append(rig,kind,turn=TURN,**extra):
    value=dict(type="event_msg",ordinal=12,
        timestamp=datetime.fromtimestamp(rig.now,timezone.utc).isoformat().replace("+00:00","Z"),
        payload=dict(type=kind,thread_id=THREAD,turn_id=turn,**extra))
    with rig.path.open("a") as stream:stream.write(json.dumps(value)+"\n")


def start(rig,identifier="slackbot:observed",prompt="synthetic exact current task",turn=TURN):
    rig.now+=1
    append(rig,"task_started",turn)
    append(rig,"item_completed",turn,item=dict(type="UserMessage",
        content=[dict(type="text",text=wrapped(identifier,prompt))]))


def terminal(rig,turn=TURN):
    rig.now+=1
    append(rig,"task_complete",turn,last_agent_message="Synthetic final output")


def test_observed_lifecycle_preserves_real_writer_tag_without_control_epoch_or_rpc(rig):
    first=rig.observe()
    assert first["kind"]=="native_observed_writer_stream_work"
    assert first["proof_kind"]=="observed_native_writer_stream"
    assert first["owner"]==rig.writer and "epoch" not in first["owner"]
    assert first["native"]["status"]=="unknown"
    assert "thread_read" not in first
    start(rig)
    active=rig.collect()
    assert active["status"]=="complete" and active["owner"]==rig.writer
    assert active["native"]["status"]=="active" and active["native"]["turn_id"]==TURN
    assert active["native"]["instruction_id"]=="slackbot:observed"
    assert active["native"]["evidence_kind"]=="observed_native_writer_stream"
    assert "synthetic exact current task" not in json.dumps(active)
    native.validate_inventory(active,rig.home,THREAD,rig.repo,clock=lambda:rig.now)
    terminal(rig)
    ended=rig.collect()
    assert ended["status"]=="complete" and ended["native"]["status"]=="idle"
    assert ended["owner"]==rig.writer and ended["native"]["lifecycle"]["turn_id"]==TURN
    assert rig.held==0


@pytest.mark.parametrize("change",["restart","birth","pid","lock_path","lock_inode","lock_device"])
def test_changed_writer_generation_or_stream_restart_cannot_bootstrap_old_terminal(rig,change):
    rig.observe();start(rig)
    assert rig.collect()["status"]=="complete"
    terminal(rig)
    if change=="restart":rig.producer=ObservedNativeStreamWork(rig.home,clock=lambda:rig.now)
    elif change=="birth":rig.writer["process_birth"]="linux:"+BOOT+":12346"
    elif change=="pid":rig.writer["pid"]+=1
    elif change=="lock_path":rig.writer["lock_path"]=str(rig.home/"another.lock")
    else:rig.writer[change]+=1
    value=rig.collect()
    assert value["status"]=="unknown"
    assert value.get("native") is None or value["native"]["status"]=="unknown"
    with pytest.raises(native.NativeInflightError):
        native.validate_inventory(value,rig.home,THREAD,rig.repo,clock=lambda:rig.now)


def test_first_observation_cannot_adopt_historical_start_input_terminal(rig):
    start(rig);terminal(rig)
    assert rig.collect()["status"]=="unknown"
    assert rig.observe()["native"]["status"]=="unknown"


@pytest.mark.parametrize("gap",["truncate","replace","oversized_append"])
def test_stream_continuity_break_cannot_establish_idle(rig,gap):
    rig.observe();start(rig)
    assert rig.collect()["status"]=="complete"
    terminal(rig)
    if gap=="truncate":rig.path.write_bytes(b"")
    elif gap=="replace":
        replacement=rig.path.with_suffix(".replacement")
        replacement.write_bytes(rig.path.read_bytes());replacement.replace(rig.path)
    else:
        from codex_watchdog.native_stream_work import BYTE_BUDGET
        with rig.path.open("ab") as stream:stream.write(b"x"*(BYTE_BUDGET+1))
    assert rig.collect()["status"]=="unknown"


def test_positive_queue_pins_while_unknown_then_same_turn_protects_until_observed_terminal(rig):
    identifier,prompt="slackbot:observed","synthetic exact current task"
    rows=rows_for(identifier,prompt)
    enqueue(rig,identifier,prompt)
    pins=CurrentNativePins(rig.runtime,rig.home,THREAD,rig.repo,clock=lambda:rig.now)
    db=provider_db(rig,rows)
    try:
        before=db.execute("SELECT namespace,kind,key,value FROM records ORDER BY namespace,kind,key").fetchall()
        first=rig.collect()
        assert first["status"]=="unknown" and first["queue"]["complete"] is True
        native.validate_queue_inventory(first,rig.home,THREAD,rig.repo,clock=lambda:rig.now)
        db.execute("BEGIN IMMEDIATE")
        pins.register(association(rows,identifier,prompt),first,db=db,fence=rig.fence)
        db.commit()
        assert flags(db)==[(1,)]*len(rows)
        remove_queue(rig)
        unknown=rig.collect()
        assert unknown["status"]=="unknown" and unknown["native"]["status"]=="unknown"
        db.execute("BEGIN IMMEDIATE")
        assert pins.reconcile(unknown,db=db,fence=rig.fence)["retired"]==0
        db.commit()
        assert flags(db)==[(1,)]*len(rows)
        start(rig,identifier,prompt)
        active=rig.collect()
        assert active["status"]=="complete" and active["queue"]["items"]==[]
        db.execute("BEGIN IMMEDIATE")
        assert pins.reconcile(active,db=db,fence=rig.fence)["retired"]==0
        db.commit()
        terminal(rig)
        ended=rig.collect()
        assert ended["status"]=="complete"
        db.execute("BEGIN IMMEDIATE")
        assert pins.reconcile(ended,db=db,fence=rig.fence)["retired"]==1
        db.commit()
        assert pins.export()["bindings"]==[] and flags(db)==[(0,)]*len(rows)
        assert db.execute("SELECT namespace,kind,key,value FROM records ORDER BY namespace,kind,key").fetchall()==before
    finally:db.close()


def test_missing_native_queue_is_never_idle_or_permission_to_retire(rig):
    rig.observe();start(rig);assert rig.collect()["status"]=="complete"
    terminal(rig)
    (rig.home/"queue_1.sqlite").unlink()
    value=rig.collect()
    assert value["status"]=="unknown" and value["queue"]["complete"] is False
    with pytest.raises(native.NativeInflightError):
        native.validate_inventory(value,rig.home,THREAD,rig.repo,clock=lambda:rig.now)


def test_standard_control_stream_and_rpc_reject_observed_writer_identity(rig):
    standard=NativeStreamWork(rig.home,clock=lambda:rig.now)
    def observed_as_owner():
        value=rig.current_writer();value["owner"]=value.pop("writer")
        return value
    value=standard.observe(THREAD,rig.repo,current_owner=observed_as_owner,fence=rig.fence)
    assert value["native"]["status"]=="unknown"
    with pytest.raises(native.NativeInflightError):native._owner(rig.writer)
    forged=dict(codex_home=str(rig.home),thread_id=THREAD,repo_path=str(rig.repo),
        owner=rig.writer,observed_at=rig.now,proof_kind="app_server_rpc",
        thread_read={"thread":{"id":THREAD,"cwd":str(rig.repo),"status":{"type":"idle"}}},
        current_turn=None,current_input=None)
    with pytest.raises(native.NativeInflightError):
        native._native(forged,native._scope(rig.home,THREAD,rig.repo),rig.now)
    with pytest.raises(native.NativeInflightError):
        validate_stream_work(rig.observe(),native._scope(rig.home,THREAD,rig.repo),rig.now)


@pytest.mark.parametrize("fault",["control_owner","extra_epoch","wrong_scope","stale","future"])
def test_observed_writer_callback_cannot_promote_control_token_or_invalid_scope(rig,fault):
    original=rig.current_writer
    def invalid():
        value=original()
        if fault=="control_owner":value["writer"]=deepcopy(rig.owner)
        elif fault=="extra_epoch":value["writer"]["epoch"]=7
        elif fault=="wrong_scope":value["thread_id"]=OTHER
        else:value["observed_at"]+=-6 if fault=="stale" else 1
        return value
    value=rig.producer.observe(THREAD,rig.repo,current_writer=invalid,fence=rig.fence)
    assert value["native"]["status"]=="unknown"
    assert value.get("owner") is None


def test_observed_complete_inventory_is_not_linux_controlstore_cold_owner_authority(rig):
    rig.observe();start(rig);terminal(rig)
    inventory=rig.collect()
    assert inventory["status"]=="complete"
    authority=SimpleNamespace(store=SimpleNamespace(codex_home=rig.home,boot_id=BOOT),thread_id=THREAD,
        repo_path=str(rig.repo),observation=SimpleNamespace(node="fixture-node"),token=dict(epoch=7,instance="actual-control-instance"))
    from codex_watchdog.control_state import ControlBusy
    with pytest.raises(ControlBusy,match="native_owner_mismatch"):
        _inventory(authority,lambda:inventory,lambda:rig.now)
