"""Native stream truth stays distinct from RPC and historical receipt state."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import uuid

import pytest

from codex_watchdog import native_inflight as native
from codex_watchdog import native_stream_work as stream
from test_native_inflight import (rig as native_rig,THREAD,OTHER,TURN,NOW,sha,wrapped,enqueue,remove_queue)


NEXT="11111111-2222-4333-8444-000000000099"


def event(kind,stamp,turn=TURN,**extra):
    return dict(timestamp=datetime.fromtimestamp(stamp,timezone.utc).isoformat().replace("+00:00","Z"),
        ordinal=12,type="event_msg",payload=dict(type=kind,turn_id=turn,**extra))


@pytest.fixture
def rig(native_rig):
    r=native_rig
    r.path=r.home/"sessions"/("rollout-current-"+THREAD+".jsonl")
    r.path.write_bytes(b"")
    r.stream=stream.NativeStreamWork(r.home,clock=lambda:r.now)
    def owner():
        assert r.held
        return dict(codex_home=str(r.home),thread_id=THREAD,repo_path=str(r.repo),
            owner=dict(r.owner),observed_at=r.now)
    r.current_owner=owner
    r.observe=lambda:r.stream.observe(THREAD,r.repo,current_owner=owner,fence=r.fence)
    r.collect=lambda:native.collect_native_work(r.home,THREAD,r.repo,current_native=r.observe,fence=r.fence,clock=lambda:r.now)
    return r


def append(rig,*events):
    with rig.path.open("ab") as handle:
        for value in events:
            handle.write((json.dumps(value)+"\n").encode())


def start(rig,identifier="slackbot:stream",prompt="alpha",turn=TURN):
    rig.now+=1
    append(rig,event("task_started",rig.now,turn),
        event("item_completed",rig.now,turn,item=dict(type="UserMessage",content=[dict(type="text",text=wrapped(identifier,prompt))])))


def terminal(rig,turn=TURN,kind="task_complete"):
    rig.now+=1
    append(rig,event(kind,rig.now,turn,last_agent_message="Synthetic final output"))


def test_in_read_same_length_rewrite_cannot_publish_stale_lifecycle(rig,monkeypatch):
    assert rig.observe()["native"]["status"]=="unknown"
    start(rig)
    anchor=stream._anchor
    calls=[0]
    def rewrite(handle,offset):
        result=anchor(handle,offset)
        calls[0]+=1
        if calls[0]==2:
            before=rig.path.read_bytes()
            changed=before.replace(b"alpha",b"bravo")
            assert changed!=before and len(changed)==len(before)
            rig.path.write_bytes(changed)
        return result
    monkeypatch.setattr(stream,"_anchor",rewrite)
    assert rig.observe()["native"]["status"]=="unknown"


def test_threads_view_is_not_supported_current_scope_even_with_indexed_underlying_id(rig):
    with sqlite3.connect(rig.home/"state_5.sqlite") as db:
        db.execute("ALTER TABLE threads RENAME TO other_threads")
        db.execute("CREATE VIEW threads AS SELECT * FROM other_threads")
    assert not stream.stream_exact_thread(rig.home,THREAD,rig.repo)


def test_unscoped_redundant_user_message_cannot_erase_exact_later_user_item(rig):
    rig.observe()
    rig.now+=1
    append(rig,event("task_started",rig.now),
        dict(timestamp=datetime.fromtimestamp(rig.now,timezone.utc).isoformat(),type="event_msg",
             payload=dict(type="user_message",message="alpha")),
        event("item_completed",rig.now,item=dict(type="UserMessage",content=[dict(type="text",text=wrapped("slackbot:stream","alpha"))])))
    value=rig.observe()
    assert value["native"]["status"]=="active"
    assert value["native"]["prompt_sha256"]==sha("alpha")


def test_observed_start_exact_input_and_terminal_are_not_rpc(rig):
    first=rig.observe()
    assert first["native"]["status"]=="unknown" and "thread_read" not in first
    start(rig)
    active=rig.observe()
    assert active["native"]["status"]=="active" and active["native"]["turn_id"]==TURN
    assert active["proof_kind"]=="native_rollout_stream"
    assert active["native"]["instruction_id"]=="slackbot:stream"
    stream.validate_stream_work(active,native._scope(rig.home,THREAD,rig.repo),rig.now)
    terminal(rig)
    ended=rig.observe()
    assert ended["native"]["status"]=="idle"
    assert ended["native"]["lifecycle"]["completed_at"]==rig.now
    assert ended["native"]["lifecycle"]["turn_id"]==TURN
    assert ended["native"]["lifecycle"]["terminal"]=="task_complete"


def test_unknown_stream_still_registers_positive_queue_without_claiming_active(rig):
    enqueue(rig,"slackbot:queued","Original queued task")
    value=rig.collect()
    assert value["status"]=="unknown" and value["native"]["status"]=="unknown"
    assert value["queue"]["complete"] and value["queue"]["items"][0]["instruction_id"]=="slackbot:queued"
    native.validate_queue_inventory(value,rig.home,THREAD,rig.repo,clock=lambda:rig.now)
    with pytest.raises(native.NativeInflightError,match="complete_proof_required"):
        native.validate_inventory(value,rig.home,THREAD,rig.repo,clock=lambda:rig.now)


def test_missing_stream_content_is_unknown_with_exact_positive_q_but_archived_scope_refuses(rig):
    enqueue(rig,"slackbot:queued","Original queued task")
    rig.path.unlink()
    value=rig.collect()
    assert value["native"]["status"]=="unknown" and value["queue"]["complete"]
    native.validate_queue_inventory(value,rig.home,THREAD,rig.repo,clock=lambda:rig.now)
    with sqlite3.connect(rig.home/"state_5.sqlite") as db:
        db.execute("UPDATE threads SET archived=1")
    assert not rig.collect()["queue"]["complete"]


@pytest.mark.parametrize("fault",["restart","owner","truncate","replace","same_size_rewrite","gap","record_budget"])
def test_historical_terminal_or_cursor_break_cannot_bootstrap_idle(rig,fault):
    rig.observe()
    start(rig)
    assert rig.observe()["native"]["status"]=="active"
    terminal(rig)
    if fault=="restart":
        rig.stream=stream.NativeStreamWork(rig.home,clock=lambda:rig.now)
    elif fault=="owner":
        rig.owner["epoch"]+=1
    elif fault=="truncate":
        rig.path.write_bytes(b"")
    elif fault=="replace":
        replacement=rig.path.with_suffix(".replacement")
        replacement.write_bytes(rig.path.read_bytes())
        replacement.replace(rig.path)
    elif fault=="same_size_rewrite":
        rig.path.write_bytes(rig.path.read_bytes().replace(b"alpha",b"bravo"))
    elif fault=="gap":
        with rig.path.open("ab") as handle:handle.write(b"x"*(stream.BYTE_BUDGET+1))
    else:
        append(rig,*[dict(type="event_msg",payload=dict(type="tool_output")) for _ in range(stream.RECORD_BUDGET+1)])
    assert rig.observe()["native"]["status"]=="unknown"


def test_partial_terminal_is_unknown_until_its_newline_arrives(rig):
    rig.observe();start(rig)
    assert rig.observe()["native"]["status"]=="active"
    rig.now+=1
    data=json.dumps(event("task_complete",rig.now)).encode()
    with rig.path.open("ab") as handle:handle.write(data[:-6])
    assert rig.observe()["native"]["status"]=="unknown"
    with rig.path.open("ab") as handle:handle.write(data[-6:]+b"\n")
    assert rig.observe()["native"]["status"]=="idle"


@pytest.mark.parametrize("fault",["wrong_turn","wrong_thread","no_input","old_time","future_time","input_changed","malformed"])
def test_unsupported_or_mismatched_lifecycle_cannot_retire_current_work(rig,fault):
    rig.observe()
    if fault=="no_input":
        rig.now+=1;append(rig,event("task_started",rig.now))
    else:
        start(rig)
    if fault=="wrong_turn":terminal(rig,NEXT)
    elif fault=="wrong_thread":
        rig.now+=1;append(rig,event("task_complete",rig.now,thread_id=OTHER))
    elif fault=="old_time":append(rig,event("task_complete",NOW-1))
    elif fault=="future_time":append(rig,event("task_complete",rig.now+60))
    elif fault=="input_changed":
        append(rig,event("item_completed",rig.now,item=dict(type="UserMessage",content=[dict(type="text",text=wrapped("slackbot:stream","bravo"))])))
    elif fault=="malformed":
        with rig.path.open("ab") as handle:handle.write(b"{broken}\n")
    else:terminal(rig)
    assert rig.observe()["native"]["status"]=="unknown"


def test_active_rpc_can_use_only_matching_truthful_observed_stream_input(rig):
    rig.observe();start(rig)
    proof=rig.observe()
    rpc=dict(codex_home=str(rig.home),thread_id=THREAD,repo_path=str(rig.repo),observed_at=rig.now,
        owner=dict(rig.owner),proof_kind="app_server_rpc",thread_read={"thread":{"id":THREAD,"cwd":str(rig.repo),"status":{"type":"active"}}},
        current_turn={"id":TURN,"status":"inProgress"},active_input_proof=proof)
    owner,native_proof=native._native(rpc,native._scope(rig.home,THREAD,rig.repo),rig.now)
    assert native_proof["instruction_id"]=="slackbot:stream"
    assert native_proof["evidence_kind"]=="app_server_rpc+native_rollout_stream"
    rpc["current_turn"]["id"]=NEXT
    assert native._native(rpc,native._scope(rig.home,THREAD,rig.repo),rig.now)[1].get("instruction_id") is None


def test_current_turn_with_missing_start_stays_unknown_until_new_complete_lifecycle(rig):
    rig.observe()
    rig.now+=1
    append(rig,event("item_completed",rig.now,item=dict(type="UserMessage",content=[dict(type="text",text=wrapped("slackbot:old","old"))])))
    terminal(rig)
    assert rig.observe()["native"]["status"]=="unknown"
    start(rig,turn=NEXT)
    assert rig.observe()["native"]["status"]=="active"
    terminal(rig,NEXT)
    assert rig.observe()["native"]["status"]=="idle"


def test_real_clock_advancing_during_owner_callback_is_not_future_fabrication(rig):
    def owner():
        assert rig.held
        rig.now+=0.001
        return dict(codex_home=str(rig.home),thread_id=THREAD,repo_path=str(rig.repo),
            owner=dict(rig.owner),observed_at=rig.now)
    value=rig.stream.observe(THREAD,rig.repo,current_owner=owner,fence=rig.fence)
    assert value["owner"]==rig.owner and value["native"]["status"]=="unknown"
    start(rig)
    value=rig.stream.observe(THREAD,rig.repo,current_owner=owner,fence=rig.fence)
    assert value["native"]["status"]=="active"
    assert value["native"]["lifecycle"]["started_at"]==pytest.approx(rig.now-0.002)


@pytest.mark.parametrize("suffix,known",[("",True),("\n",True),("\n\n",False)])
def test_native_known_single_terminal_lf_only(rig,suffix,known):
    rig.observe();rig.now+=1
    text=wrapped("slackbot:stream","alpha")+suffix
    append(rig,event("task_started",rig.now),event("item_completed",rig.now,item=dict(type="UserMessage",content=[dict(type="text",text=text)])))
    assert (rig.observe()["native"]["status"]=="active")==known


def test_same_turn_input_metadata_and_native_times_are_not_redated_by_quiet_poll(rig):
    rig.observe();start(rig)
    active=rig.observe()
    original=active["native"]["lifecycle"]
    rig.now+=3600
    again=rig.observe()
    assert again["native"]["lifecycle"]==original
    terminal(rig)
    complete=rig.observe()
    assert complete["native"]["lifecycle"]["started_at"]==original["started_at"]
    assert "genuine_at" not in complete and "thread_read" not in complete


def test_unended_prior_turn_gap_is_unknown_but_later_independent_lifecycle_can_recover(rig):
    rig.observe();start(rig)
    assert rig.observe()["native"]["status"]=="active"
    start(rig,turn=NEXT)
    assert rig.observe()["native"]["status"]=="unknown"
    terminal(rig,NEXT)
    assert rig.observe()["native"]["status"]=="unknown"
    fresh="11111111-2222-4333-8444-000000000098"
    start(rig,turn=fresh)
    assert rig.observe()["native"]["status"]=="active"


@pytest.mark.parametrize("fault",["archived","foreign_repo","agent","source","missing_index"])
def test_positive_q_requires_supported_exact_indexed_native_scope(rig,fault):
    enqueue(rig,"slackbot:queued","Native queued task")
    with sqlite3.connect(rig.home/"state_5.sqlite") as db:
        if fault=="missing_index":
            db.execute("CREATE TABLE unindexed AS SELECT * FROM threads")
            db.execute("DROP TABLE threads")
            db.execute("ALTER TABLE unindexed RENAME TO threads")
        elif fault=="archived":db.execute("UPDATE threads SET archived=1")
        elif fault=="foreign_repo":db.execute("UPDATE threads SET cwd=?",(str(rig.home),))
        elif fault=="agent":db.execute("UPDATE threads SET thread_source='subagent'")
        else:db.execute("UPDATE threads SET source='cli'")
    value=rig.collect()
    assert not value["queue"]["complete"]
    with pytest.raises(native.NativeInflightError):
        native.validate_queue_inventory(value,rig.home,THREAD,rig.repo,clock=lambda:rig.now)


def test_stale_owner_or_owner_change_during_stream_read_is_unknown_not_idle(rig):
    rig.observe();start(rig)
    owner=rig.current_owner
    def stale():
        value=owner();value["observed_at"]-=6;return value
    value=rig.stream.observe(THREAD,rig.repo,current_owner=stale,fence=rig.fence)
    assert value["native"]["status"]=="unknown"
    calls=[0]
    def changed():
        calls[0]+=1
        if calls[0]==2:rig.owner["epoch"]+=1
        return owner()
    value=rig.stream.observe(THREAD,rig.repo,current_owner=changed,fence=rig.fence)
    assert value["native"]["status"]=="unknown"


@pytest.mark.parametrize("suffix",["-wal","-shm"])
def test_broken_native_sidecar_is_refused_before_opening_sqlite(rig,monkeypatch,suffix):
    side=Path(str(rig.home/"state_5.sqlite")+suffix)
    try:
        side.symlink_to(rig.home/"missing-native-sidecar")
    except (OSError,NotImplementedError):
        pytest.skip("This platform cannot create the native symlink refusal fixture")
    def unsafe_open(*args,**kwargs):pytest.fail("unsafe native sidecar reached SQLite")
    monkeypatch.setattr(stream.sqlite3,"connect",unsafe_open)
    assert not stream.stream_exact_thread(rig.home,THREAD,rig.repo)


@pytest.mark.parametrize("history",[0,1000,10000,100000])
def test_fixed_current_and_quiet_stream_work_is_bounded_under_native_history_growth(rig,monkeypatch,history):
    # Real native file records and indexed historical native thread rows, not
    # a mocked tail/page or an elapsed-time performance assertion.
    old=json.dumps(dict(timestamp="2026-01-01T00:00:00Z",type="event_msg",payload=dict(type="tool_output",text="x"*64)))+"\n"
    with rig.path.open("w") as handle:
        for _ in range(history):handle.write(old)
    with sqlite3.connect(rig.home/"state_5.sqlite") as db:
        db.executemany("INSERT INTO threads VALUES(?,?,'vscode','user',1,?)",
            ((str(uuid.UUID(int=index+100000)),str(rig.repo),str(rig.home/"sessions"/"old")) for index in range(history)))
    assert rig.observe()["native"]["status"]=="unknown"
    counts=dict(read_bytes=0,read_calls=0,parsed_records=0,parsed_bytes=0,sql=0,vm=0)
    original_open,original_connect,original_load=Path.open,sqlite3.connect,stream._load
    class Reader:
        def __init__(self,handle):self.handle=handle
        def __enter__(self):return self
        def __exit__(self,*args):return self.handle.__exit__(*args)
        def __getattr__(self,key):return getattr(self.handle,key)
        def read(self,size=-1):
            assert 0<=size<=stream.BYTE_BUDGET
            result=self.handle.read(size)
            counts["read_calls"]+=1;counts["read_bytes"]+=len(result)
            return result
    def opened(path,*args,**kwargs):
        handle=original_open(path,*args,**kwargs)
        return Reader(handle) if path==rig.path and args and args[0]=="rb" else handle
    class DB(sqlite3.Connection):
        def execute(self,*args,**kwargs):counts["sql"]+=1;return super().execute(*args,**kwargs)
        def set_progress_handler(self,callback,n):
            ticks=[0]
            def step():
                counts["vm"]+=1;ticks[0]+=1
                return callback() if ticks[0]%n==0 else 0
            return super().set_progress_handler(step,1)
    def connect(path,*args,**kwargs):
        kwargs["factory"]=DB
        return original_connect(path,*args,**kwargs)
    def load(raw):
        counts["parsed_records"]+=1;counts["parsed_bytes"]+=len(raw)
        return original_load(raw)
    def traversal(*args,**kwargs):pytest.fail("native history traversal")
    def measure():
        for key in counts:counts[key]=0
        with monkeypatch.context() as patch:
            patch.setattr(Path,"open",opened);patch.setattr(sqlite3,"connect",connect)
            patch.setattr(stream,"_load",load)
            patch.setattr(Path,"glob",traversal);patch.setattr(Path,"rglob",traversal)
            value=rig.observe()
        return value,dict(counts)
    start(rig)
    value,active=measure()
    assert value["native"]["status"]=="active" and active["parsed_records"]==2
    terminal(rig)
    value,complete=measure()
    assert value["native"]["status"]=="idle" and complete["parsed_records"]==1
    value,quiet=measure()
    assert value["native"]["status"]=="idle" and quiet["parsed_records"]==0
    rig.stream=stream.NativeStreamWork(rig.home,clock=lambda:rig.now)
    value,restart=measure()
    assert value["native"]["status"]=="unknown" and restart["parsed_records"]==0
    for result in (active,complete,quiet,restart):
        assert result["read_bytes"]<=stream.BYTE_BUDGET+3*stream.ANCHOR_BYTES
        assert result["sql"]<=30 and result["vm"]<=2000
    print("NATIVE_STREAM_HISTORY "+json.dumps(dict(history_records=history,archived_thread_rows=history,
        active=active,terminal=complete,quiet=quiet,restart_unknown=restart),sort_keys=True))
