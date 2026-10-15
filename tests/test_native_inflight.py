"""Current native work, rather than historical uncertainty, protects GC pins."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from codex_watchdog import native_inflight as native
from codex_watchdog.native_inflight import NativeInflightError, collect_native_work, resolve_snapshot_pins, validate_inventory
from codex_watchdog.native_inflight_pins import CurrentNativePins, prepare_migration_pins
from codex_watchdog.recent_state import initialize, prune


THREAD = "11111111-2222-4333-8444-000000000001"
OTHER = "11111111-2222-4333-8444-000000000002"
TURN = "11111111-2222-4333-8444-000000000003"
QUEUE = "11111111-2222-4333-8444-000000000004"
NOW = 1792008000.0
NS = "slack/poll-relay-state.json"
ACK_NS = "slack/bot-command-receipts-v1"


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def wrapped(identifier, prompt):
    return "[CODEX_WATCHDOG_WAKE id=" + identifier + " sha256=" + sha(prompt) + "]\n" + prompt


@pytest.fixture
def rig(tmp_path):
    home, repo, runtime = [tmp_path / name for name in ("codex", "repo", "runtime")]
    for path in (home, repo, runtime, home / "sessions"):
        path.mkdir(exist_ok=True)
    rollout = home / "sessions" / ("rollout-current-" + THREAD + ".jsonl")
    rollout.write_text("fixture native bytes never scanned\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,source TEXT,thread_source TEXT,archived INTEGER,rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (THREAD, str(repo), "vscode", "user", 0, str(rollout)))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT NOT NULL,payload_json TEXT NOT NULL,queue_order INTEGER NOT NULL)")
        db.execute("CREATE INDEX queue_by_thread ON queued_items(thread_id,queue_order)")
        db.execute("CREATE TABLE queued_thread_revisions(thread_id TEXT PRIMARY KEY,revision INTEGER NOT NULL)")
        db.execute("INSERT INTO queued_thread_revisions VALUES(?,7)", (THREAD,))
    result = SimpleNamespace(home=home, repo=repo, runtime=runtime, now=NOW, calls=0,
                             native_status="idle", active_text=None, turn=TURN, hook=None,
                             owner=dict(node="fixture",boot_id="fixture-boot",epoch=7,instance="fixture-instance"), held=0)
    @contextmanager
    def fence():
        result.held += 1
        try:
            yield
        finally:
            result.held -= 1
    result.fence = fence
    def proof():
        assert result.held > 0
        result.calls += 1
        value = dict(codex_home=str(home), thread_id=THREAD, repo_path=str(repo), owner=dict(result.owner), observed_at=result.now,
            thread_read={"thread": {"id": THREAD,"cwd":str(repo),"status":{"type":result.native_status}}},
            current_turn=None,current_input=None)
        if result.native_status == "active":
            value["current_turn"]={"id":result.turn,"status":"inProgress"}
            if result.active_text is not None:
                value["current_input"]={"type":"event_msg","payload":{"thread_id":THREAD,"turn_id":result.turn,"type":"item_completed",
                    "item":{"type":"UserMessage","content":[{"type":"text","text":result.active_text}]}}}
        if result.hook:
            result.hook(value)
        return value
    result.proof = proof
    def inventory(**kwargs):
        return collect_native_work(home,THREAD,repo,current_native=proof,fence=fence,clock=lambda:result.now,**kwargs)
    result.inventory=inventory
    return result


def enqueue(rig, identifier, prompt, *, queue_id=QUEUE, thread=THREAD, order=1):
    payload=json.dumps({"UserInput":{"content":[{"type":"text","text":wrapped(identifier,prompt)}]}})
    with sqlite3.connect(rig.home/"queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES(?,?,?,?)",(queue_id,thread,payload,order))
        db.execute("UPDATE queued_thread_revisions SET revision=revision+1 WHERE thread_id=?",(thread,))
    return payload


def remove_queue(rig):
    with sqlite3.connect(rig.home/"queue_1.sqlite") as db:
        db.execute("DELETE FROM queued_items WHERE thread_id=?",(THREAD,))
        db.execute("UPDATE queued_thread_revisions SET revision=revision+1 WHERE thread_id=?",(THREAD,))


def rows_for(identifier, prompt, *, suffix="", thread=THREAD):
    parent,event,message,request,ack=[sha(name+suffix) for name in ("parent","event","message","request","ack")]
    def row(kind,key,body,namespace=NS):
        return dict(namespace=namespace,kind=kind,key=key,value=json.dumps(body),thread_id=thread,active=0,created_at="2026-10-01T00:00:00Z",fingerprint=None)
    target={"thread_id":thread}
    return [
        row("threads",parent,dict(target=target,created_at="2026-10-01T00:00:00Z")),
        row("events",event,dict(instruction_id=identifier,text_sha256=sha(prompt),thread_key=parent,message_key=message,state="uncertain",payload_sha256=sha("payload"))),
        row("messages",message,dict(event_key=event,payload_sha256=sha("payload"))),
        row("slack_bot_requests",request,dict(target=target,event_key_digest=event,message_key_digest=message)),
        row("slack_bot_receipts",ack,dict(target=target,source_namespace=NS,event_key_digest=event,message_key_digest=message,
            proof=dict(event_key_digest=event,message_key_digest=message,request_key=request)),ACK_NS),
        row("relay_native_receipts",event,dict(thread_id=thread,receipt=dict(thread_id=thread,instruction_id=identifier,prompt_sha256=sha(prompt),queue_message_id=QUEUE)))
    ]


def provider_db(rig, rows, *, provider="slack"):
    path=rig.runtime/provider/"reply-tickets.sqlite3"
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE records(namespace TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,fingerprint TEXT,thread_id TEXT,active INTEGER NOT NULL DEFAULT 0,created_at TEXT,PRIMARY KEY(namespace,kind,key))")
    db.execute("CREATE INDEX notification_lookup ON records(namespace,kind,fingerprint)")
    db.execute("CREATE INDEX thread_lookup ON records(namespace,kind,thread_id)")
    initialize(db)
    for row in rows:
        db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)",tuple(row[name] for name in ("namespace","kind","key","value","fingerprint","thread_id","active","created_at")))
        db.execute("INSERT INTO recent_records VALUES(?,?,?,?,?,?,?)",(row["namespace"],row["kind"],row["key"],NOW-400000,NOW-100000,0,row["thread_id"]))
    db.execute("PRAGMA user_version=3")
    db.commit()
    return db


def pin_store(rig):
    return CurrentNativePins(rig.runtime,rig.home,THREAD,rig.repo,clock=lambda:rig.now)


def association(rows,identifier,prompt,queue_id=QUEUE):
    return dict(instruction_id=identifier,prompt_sha256=sha(prompt),queue_id=queue_id,
                refs=[[row[k] for k in ("namespace","kind","key")] for row in rows])


def flags(db):
    return db.execute("SELECT protected FROM recent_records ORDER BY namespace,kind,key").fetchall()


def test_queue_inventory_is_exact_fenced_read_only_and_redacted(rig):
    identifier,prompt="slackbot:"+sha("event")[:40],"Only this synthetic current task"
    enqueue(rig,identifier,prompt)
    before={p:p.read_bytes() for p in (rig.home/"state_5.sqlite",rig.home/"queue_1.sqlite",next((rig.home/"sessions").iterdir()))}
    value=rig.inventory()
    assert value["status"]=="complete" and rig.calls==2 and rig.held==0
    assert value["queue"]["items"][0]["instruction_id"]==identifier
    assert value["queue"]["items"][0]["prompt_sha256"]==sha(prompt)
    assert prompt not in json.dumps(value) and "content" not in json.dumps(value)
    validate_inventory(value,rig.home,THREAD,rig.repo,clock=lambda:rig.now)
    assert all(p.read_bytes()==raw for p,raw in before.items())


@pytest.mark.parametrize("fault",["missing_queue","ambiguous_queue","queue_symlink","no_queue_index","no_revision_index","missing_revision","bad_revision","bad_payload","too_large","wrong_digest","wrong_thread","wrong_repo","wrong_owner","stale","future","idle_boolean","active_without_input","completed_turn","foreign_turn","changed_owner","changed_queue","archived"])
def test_unknown_native_evidence_cannot_be_no_work(rig,fault):
    identifier,prompt="slackbot:"+sha("event")[:40],"fixture prompt"
    enqueue(rig,identifier,prompt)
    path=rig.home/"queue_1.sqlite"
    if fault=="missing_queue":path.unlink()
    elif fault=="ambiguous_queue":(rig.home/"queue_2.sqlite").touch()
    elif fault=="queue_symlink":
        saved=rig.home/"saved.sqlite";path.rename(saved)
        try:
            path.symlink_to(saved)
        except OSError as error:
            import os
            if os.name != "nt" or getattr(error,"winerror",None) != 1314:
                raise
            pytest.skip("Windows account cannot create the synthetic symlink (WinError 1314)")
    elif fault in ("no_queue_index","no_revision_index","missing_revision","bad_revision","bad_payload","too_large","wrong_digest"):
        with sqlite3.connect(path) as db:
            if fault=="no_queue_index":db.execute("DROP INDEX queue_by_thread")
            elif fault=="no_revision_index":
                db.execute("ALTER TABLE queued_thread_revisions RENAME TO old")
                db.execute("CREATE TABLE queued_thread_revisions(thread_id TEXT,revision INTEGER)")
                db.execute("INSERT INTO queued_thread_revisions SELECT * FROM old")
            elif fault=="missing_revision":db.execute("DELETE FROM queued_thread_revisions")
            elif fault=="bad_revision":db.execute("UPDATE queued_thread_revisions SET revision=-1")
            else:
                raw="{}" if fault=="bad_payload" else "x"*(native.PAYLOAD_BYTES+1) if fault=="too_large" else json.dumps({"UserInput":{"content":[{"type":"text","text":wrapped(identifier,prompt)+"edit"}]}})
                db.execute("UPDATE queued_items SET payload_json=?",(raw,))
    elif fault=="archived":
        with sqlite3.connect(rig.home/"state_5.sqlite") as db:db.execute("UPDATE threads SET archived=1")
    else:
        def corrupt(value):
            if fault=="wrong_thread":value["thread_read"]["thread"]["id"]=OTHER
            elif fault=="wrong_repo":value["thread_read"]["thread"]["cwd"]+="/other"
            elif fault=="wrong_owner":value["owner"]["epoch"]=True
            elif fault in ("stale","future"):value["observed_at"]+=-6 if fault=="stale" else 1
            elif fault=="idle_boolean":value["thread_read"]={"idle":True}
            elif fault=="changed_owner" and rig.calls==2:value["owner"]["epoch"]+=1
            elif fault=="changed_queue" and rig.calls==2:
                with sqlite3.connect(path) as db:db.execute("UPDATE queued_thread_revisions SET revision=revision+1")
            elif fault in ("active_without_input","completed_turn","foreign_turn"):
                value["thread_read"]["thread"]["status"]={"type":"active"}
                value["current_turn"]={"id":TURN,"status":"completed" if fault=="completed_turn" else "inProgress"}
                if fault=="foreign_turn":value["current_input"]={"type":"event_msg","payload":{"thread_id":THREAD,"turn_id":OTHER,"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":wrapped(identifier,prompt)}]}}}
        rig.hook=corrupt
    value=rig.inventory()
    assert value["status"]=="unknown"
    with pytest.raises(NativeInflightError):validate_inventory(value,rig.home,THREAD,rig.repo,clock=lambda:rig.now)


@pytest.mark.parametrize("active",[False,True])
def test_current_work_maps_exact_reservations_not_historical_uncertainty(rig,active):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    current=rows_for(identifier,prompt)
    old=rows_for("slackbot:"+sha("old")[:40],"old uncertain",suffix="old")
    foreign=rows_for(identifier,"wrong digest",suffix="foreign",thread=OTHER)
    if active:
        rig.native_status="active";rig.active_text=wrapped(identifier,prompt)
    else:enqueue(rig,identifier,prompt)
    value=rig.inventory()
    pins=resolve_snapshot_pins({"records":current+old+foreign},value)
    assert pins=={(row["namespace"],row["kind"],row["key"]) for row in current}
    assert all(row["active"]==0 for row in current+old)


def test_active_human_native_input_does_not_pin_old_watchdog_uncertainty(rig):
    rig.native_status="active";rig.active_text="ordinary user task"
    value=rig.inventory()
    assert value["status"]=="complete"
    assert resolve_snapshot_pins(rows_for("slackbot:"+sha("event")[:40],"old task"),value)==set()


def test_current_reservations_survive_actual_ttl_gc_and_retire_without_completion_fiction(rig):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt)
    enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows)
    store=pin_store(rig)
    db.execute("BEGIN IMMEDIATE")
    store.register(association(rows,identifier,prompt),rig.inventory(),db=db,fence=rig.fence)
    db.commit()
    assert flags(db)==[(1,)]*len(rows)
    before=db.execute("SELECT value FROM records ORDER BY namespace,kind,key").fetchall()
    journal=SimpleNamespace(namespace=NS,clock=lambda:rig.now)
    prune(journal,db);db.commit()
    assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0]==len(rows)
    assert store.export()["bindings"]
    remove_queue(rig)
    rig.native_status="active";rig.active_text=wrapped(identifier,prompt)
    db.execute("BEGIN IMMEDIATE")
    assert store.reconcile(rig.inventory(),db=db,fence=rig.fence)["retired"]==0
    db.commit()
    rig.native_status="idle";rig.active_text=None
    db.execute("BEGIN IMMEDIATE")
    assert pin_store(rig).reconcile(rig.inventory(),db=db,fence=rig.fence)["retired"]==1
    db.commit()
    assert flags(db)==[(0,)]*len(rows)
    assert db.execute("SELECT value FROM records ORDER BY namespace,kind,key").fetchall()==before
    assert store.export()["bindings"]==[]
    db.close()


def test_missing_queue_or_unknown_active_turn_keeps_current_pins_after_restart(rig):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows);store=pin_store(rig)
    db.execute("BEGIN IMMEDIATE");store.register(association(rows,identifier,prompt),rig.inventory(),db=db,fence=rig.fence);db.commit()
    before=store.export()
    (rig.home/"queue_1.sqlite").unlink()
    db.execute("BEGIN IMMEDIATE")
    assert pin_store(rig).reconcile(rig.inventory(),db=db,fence=rig.fence)==dict(status="unknown",examined=0,retired=0)
    db.commit()
    assert pin_store(rig).export()==before and flags(db)==[(1,)]*len(rows)
    db.close()


@pytest.mark.parametrize("phase",["register","retire"])
def test_crash_before_commit_rolls_back_bindings_and_flags_together(rig,phase):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows);store=pin_store(rig)
    if phase=="retire":
        db.execute("BEGIN IMMEDIATE");store.register(association(rows,identifier,prompt),rig.inventory(),db=db,fence=rig.fence);db.commit()
        remove_queue(rig)
    db.execute("BEGIN IMMEDIATE")
    if phase=="register":store.register(association(rows,identifier,prompt),rig.inventory(),db=db,fence=rig.fence)
    else:store.reconcile(rig.inventory(),db=db,fence=rig.fence)
    db.rollback();db.close()
    with sqlite3.connect(rig.runtime/"slack"/"reply-tickets.sqlite3") as check:
        assert flags(check)==[(int(phase=="retire"),)]*len(rows)
    if phase=="retire":assert len(pin_store(rig).export()["bindings"])==1
    else:
        # An uncommitted newly attached file is not falsely accepted as recovery.
        with pytest.raises(NativeInflightError):pin_store(rig).export()


def test_cold_stage_hydration_recovery_is_idempotent_and_metadata_only(rig):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows);db.close()
    snapshot={"records":rows}
    proof=rig.inventory()
    recovery=prepare_migration_pins(rig.runtime,"slack",snapshot,proof,fence=rig.fence,clock=lambda:rig.now)
    assert prompt not in json.dumps(recovery) and "uncertain" not in json.dumps(recovery)
    assert prepare_migration_pins(rig.runtime,"slack",snapshot,proof,fence=rig.fence,clock=lambda:rig.now)==recovery
    with sqlite3.connect(rig.runtime/"slack"/"reply-tickets.sqlite3") as db:
        db.execute("PRAGMA synchronous=FULL");db.execute("BEGIN IMMEDIATE")
        assert pin_store(rig).reinstall(recovery,proof,db=db,fence=rig.fence)["registered"]==1
        db.commit()
    assert pin_store(rig).export()==recovery


@pytest.mark.parametrize("fault",["expired","wrong_session","wrong_repo","tampered","historical_only","wrong_reference","unfenced","wal"])
def test_unsafe_pin_registration_is_local_refusal_with_original_rows_retained(rig,fault):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows);store=pin_store(rig)
    value=rig.inventory();binding=association(rows,identifier,prompt)
    if fault=="expired":rig.now+=6
    elif fault=="wrong_session":value["thread_id"]=OTHER
    elif fault=="wrong_repo":value["repo_path"]+="/elsewhere"
    elif fault=="tampered":value["queue"]["items"][0]["instruction_id"]="changed"
    elif fault=="historical_only":binding["instruction_id"]="slackbot:"+sha("historical")[:40]
    elif fault=="wrong_reference":binding["refs"][0][2]=sha("unrelated")
    elif fault=="wal":db.execute("PRAGMA journal_mode=WAL")
    before=db.execute("SELECT value FROM records ORDER BY namespace,kind,key").fetchall()
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(NativeInflightError):store.register(binding,value,db=db,fence=None if fault=="unfenced" else rig.fence)
    db.rollback()
    assert flags(db)==[(0,)]*len(rows)
    assert db.execute("SELECT value FROM records ORDER BY namespace,kind,key").fetchall()==before
    db.close()


def test_unknown_active_input_still_protects_verified_current_queue_without_retirement(rig):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    rig.native_status="active"  # Existing native owner has not supplied its input.
    value=rig.inventory()
    assert value["status"]=="unknown" and value["queue"]["complete"] is True
    db=provider_db(rig,rows);store=pin_store(rig)
    db.execute("BEGIN IMMEDIATE")
    store.register(association(rows,identifier,prompt),value,db=db,fence=rig.fence)
    db.commit()
    assert flags(db)==[(1,)]*len(rows)
    db.execute("BEGIN IMMEDIATE")
    assert store.reconcile(value,db=db,fence=rig.fence)["retired"]==0
    db.commit();db.close()


def test_queue_budget_is_refusal_not_fifo_drop_or_new_work_cap(rig):
    for index in range(3):
        enqueue(rig,"slack:"+sha(str(index))[:40],"prompt "+str(index),queue_id=str(uuid.UUID(int=index+100)),order=index)
    before=(rig.home/"queue_1.sqlite").read_bytes()
    value=rig.inventory(queue_limit=2)
    assert value["status"]=="unknown" and value["reason"]=="native_inflight_queue_budget"
    assert (rig.home/"queue_1.sqlite").read_bytes()==before
    complete=rig.inventory(queue_limit=3)
    assert [item["queue_id"] for item in complete["queue"]["items"]]==[str(uuid.UUID(int=i+100)) for i in range(3)]


def test_reconcile_is_bounded_and_progresses_past_current_bindings_after_restart(rig):
    inputs=[("slack:"+sha(str(i))[:40],"prompt "+str(i),str(uuid.UUID(int=i+100))) for i in range(7)]
    rows=[]
    for index,(identifier,prompt,queue_id) in enumerate(inputs):
        rows.extend(rows_for(identifier,prompt,suffix=str(index)))
        enqueue(rig,identifier,prompt,queue_id=queue_id,order=index)
    db=provider_db(rig,rows);store=pin_store(rig);proof=rig.inventory()
    db.execute("BEGIN IMMEDIATE")
    for index,(identifier,prompt,queue_id) in enumerate(inputs):
        own=rows[index*6:(index+1)*6]
        store.register(association(own,identifier,prompt,queue_id),proof,db=db,fence=rig.fence)
    db.commit()
    with sqlite3.connect(rig.home/"queue_1.sqlite") as queue:
        queue.execute("DELETE FROM queued_items WHERE id!=?",(inputs[0][2],))
        queue.execute("UPDATE queued_thread_revisions SET revision=revision+1")
    store=pin_store(rig)  # Current bindings, not process cursor, survive restart.
    retired=0
    for _ in range(9):
        db.execute("BEGIN IMMEDIATE")
        value=store.reconcile(rig.inventory(),db=db,fence=rig.fence,budget=1)
        db.commit()
        assert value["examined"]<=1
        retired+=value["retired"]
    assert retired==6 and len(store.export()["bindings"])==1
    assert sum(flag for (flag,) in flags(db))==6
    db.close()


def test_shared_parent_pin_remains_until_last_current_binding_is_absent(rig):
    first,second=("slack:"+sha(str(i))[:40] for i in range(2))
    rows1=rows_for(first,"one")
    rows2=rows_for(second,"two",suffix="second")
    shared=rows1[0]
    rows2[0]=shared
    event=json.loads(rows2[1]["value"]);event["thread_key"]=shared["key"]
    rows2[1]["value"]=json.dumps(event)
    enqueue(rig,first,"one");enqueue(rig,second,"two",queue_id=OTHER,order=2)
    db=provider_db(rig,rows1+rows2[1:]);store=pin_store(rig)
    proof=rig.inventory();db.execute("BEGIN IMMEDIATE")
    store.register(association(rows1,first,"one"),proof,db=db,fence=rig.fence)
    store.register(association(rows2,second,"two",OTHER),proof,db=db,fence=rig.fence)
    db.commit()
    with sqlite3.connect(rig.home/"queue_1.sqlite") as queue:
        queue.execute("DELETE FROM queued_items WHERE id=?",(QUEUE,))
        queue.execute("UPDATE queued_thread_revisions SET revision=revision+1")
    db.execute("BEGIN IMMEDIATE");store.reconcile(rig.inventory(),db=db,fence=rig.fence);db.commit()
    assert db.execute("SELECT protected FROM recent_records WHERE namespace=? AND kind='threads' AND key=?",(NS,shared["key"])).fetchone()==(1,)
    db.close()


@pytest.mark.parametrize("history,q",[(0,4),(1000,4),(10000,4),(100000,4),(10000,0),(10000,1),(10000,16),(10000,256)])
def test_current_inventory_indexed_work_is_independent_of_unrelated_history(rig,monkeypatch,history,q):
    # Real indexed native tables carry H unrelated rows, not a mocked page.
    with sqlite3.connect(rig.home/"state_5.sqlite") as db:
        db.executemany("INSERT INTO threads VALUES(?,?,?,?,?,?)",((str(uuid.UUID(int=i+1000)),str(rig.repo),"vscode","user",1,"unrelated historical path") for i in range(history)))
    with sqlite3.connect(rig.home/"queue_1.sqlite") as db:
        db.executemany("INSERT INTO queued_items VALUES(?,?,?,?)",((str(uuid.UUID(int=i+1000)),OTHER,'{}',i) for i in range(history)))
    for i in range(q):
        enqueue(rig,"slack:"+sha(str(i))[:40],"p"+str(i),queue_id=str(uuid.UUID(int=history+i+200000)),order=i)
    counts={"sql":0,"vm":0,"writes":0,"rollout_reads":0}
    original=sqlite3.connect
    class CountConnection(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            counts["sql"]+=1
            if sql.split()[0].upper() not in ("SELECT","EXPLAIN","PRAGMA","BEGIN","COMMIT"):
                counts["writes"]+=1
            return super().execute(sql,*args,**kwargs)
        def set_progress_handler(self,callback,n):
            progress=[0]
            def counted():
                counts["vm"]+=1;progress[0]+=1
                return callback() if progress[0]%n==0 else 0
            return super().set_progress_handler(counted,1)
    def connect(*args,**kwargs):
        kwargs["factory"]=CountConnection
        return original(*args,**kwargs)
    monkeypatch.setattr(native.sqlite3,"connect",connect)
    original_open=Path.open
    def bounded_open(path,*args,**kwargs):
        if path.suffix==".jsonl":
            counts["rollout_reads"]+=1
            raise AssertionError("native rollout content read is forbidden")
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,"open",bounded_open)
    def forbidden(*args,**kwargs):raise AssertionError("native history traversal is forbidden")
    monkeypatch.setattr(Path,"rglob",forbidden)
    value=rig.inventory(queue_limit=max(q,1))
    assert value["status"]=="complete" and len(value["queue"]["items"])==q
    assert counts["writes"]==0 and counts["sql"]==19
    assert counts["vm"]<=2000+q*40
    print("NATIVE_INFLIGHT_COUNTS "+json.dumps(dict(history=history,q=q,**counts),sort_keys=True))


@pytest.mark.parametrize("history",[0,1000,10000,100000])
@pytest.mark.parametrize("current",[False,True])
def test_current_pin_lookup_and_retirement_do_not_scan_provider_history(rig,monkeypatch,history,current):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt) if current else []
    if current:enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows)
    db.executemany("INSERT INTO records(namespace,kind,key,value,active,thread_id,created_at) VALUES(?,'events',?,'{}',0,?,'2026-10-01T00:00:00Z')",((NS,"old-"+str(i),OTHER) for i in range(history)))
    db.executemany("INSERT INTO recent_records VALUES(?,'events',?,?,?,0,?)",((NS,"old-"+str(i),NOW-400000,NOW-100000,OTHER) for i in range(history)))
    db.commit()
    statements=[];db.set_trace_callback(statements.append)
    store=pin_store(rig);db.execute("BEGIN IMMEDIATE")
    if current:store.register(association(rows,identifier,prompt),rig.inventory(),db=db,fence=rig.fence)
    db.commit()
    remove_queue(rig);db.execute("BEGIN IMMEDIATE")
    assert store.reconcile(rig.inventory(),db=db,fence=rig.fence,budget=1)["retired"]==int(current)
    db.commit()
    provider_reads=[sql for sql in statements if "FROM main.records" in sql]
    assert len(provider_reads)==len(rows) and all("namespace=" in sql and "kind=" in sql and "key=" in sql for sql in provider_reads)
    assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0]==history+len(rows)
    print("NATIVE_PIN_COUNTS "+json.dumps(dict(history=history,current=current,point_reads=len(provider_reads),retired_bindings=int(current),provider_rows_deleted=0)))
    db.close()


@pytest.mark.parametrize("field",["source_namespace","request_key","message_key_digest","event_key_digest"])
def test_ack_pin_requires_exact_source_event_physical_and_logical_join(rig,field):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    body=json.loads(rows[4]["value"])
    if field=="source_namespace":body[field]="slack/other.json"
    elif field=="request_key":body["proof"][field]=sha("other request")
    else:body["proof"][field]=sha("other "+field)
    rows[4]["value"]=json.dumps(body)
    if field in ("source_namespace","event_key_digest"):
        pins=resolve_snapshot_pins(rows,rig.inventory())
        assert (ACK_NS,"slack_bot_receipts",rows[4]["key"]) not in pins
    else:
        with pytest.raises(NativeInflightError,match="reference_invalid"):resolve_snapshot_pins(rows,rig.inventory())


@pytest.mark.parametrize("committed",[False,True])
def test_abrupt_process_exit_preserves_atomic_current_pin_registration(rig,committed,tmp_path):
    # A real disposable process exits without closing its transaction.  This is
    # source-fixture crash/restart proof, not a native actor or courier process.
    import os
    import subprocess
    import sys
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows);db.close()
    inputs=tmp_path/"crash-input.json"
    inputs.write_text(json.dumps(dict(runtime=str(rig.runtime),home=str(rig.home),repo=str(rig.repo),
        proof=rig.inventory(),association=association(rows,identifier,prompt),committed=committed)))
    script='''
from contextlib import nullcontext
import json,os,sqlite3,sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from codex_watchdog.native_inflight_pins import CurrentNativePins
value=json.loads(Path(sys.argv[1]).read_text())
db=sqlite3.connect(Path(value['runtime'])/'slack'/'reply-tickets.sqlite3')
db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
store=CurrentNativePins(Path(value['runtime']),Path(value['home']),value['proof']['thread_id'],Path(value['repo']),clock=lambda:value['proof']['observed_at'])
store.register(value['association'],value['proof'],db=db,fence=nullcontext)
if value['committed']:db.commit()
os._exit(23)
'''
    process=subprocess.run([sys.executable,"-I","-c",script,str(inputs),str(Path(__file__).resolve().parents[1]/"src")],
        env=dict(os.environ),capture_output=True,text=True,timeout=10)
    assert process.returncode==23,process.stderr
    with sqlite3.connect(rig.runtime/"slack"/"reply-tickets.sqlite3") as recovered:
        assert flags(recovered)==[(int(committed),)]*len(rows)
        if not committed:
            # The same *existing current* Q may reinstall only metadata; it is
            # never dispatched again to recover the interrupted transaction.
            recovered.execute("PRAGMA synchronous=FULL");recovered.execute("BEGIN IMMEDIATE")
            pin_store(rig).register(association(rows,identifier,prompt),rig.inventory(),db=recovered,fence=rig.fence)
            recovered.commit()
    assert len(pin_store(rig).export()["bindings"])==1


@pytest.mark.parametrize("suffix,accepted",[("",True),("\n",True),("\n\n",False),("changed",False)])
def test_native_current_message_only_allows_exact_known_single_terminal_lf(rig,suffix,accepted):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rig.native_status="active";rig.active_text=wrapped(identifier,prompt)+suffix
    value=rig.inventory()
    assert (value["status"]=="complete") is accepted
    if accepted:
        assert value["native"]["prompt_sha256"]==sha(prompt)
        assert resolve_snapshot_pins(rows_for(identifier,prompt),value)


def test_caught_optional_pin_failure_cannot_commit_partial_flags_or_sidecar(rig):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows);store=pin_store(rig)
    failing=sorted((row["namespace"],row["kind"],row["key"]) for row in rows)[1]
    db.execute("CREATE TRIGGER fixture_pin_failure BEFORE UPDATE OF protected ON recent_records WHEN NEW.namespace="
               +repr(failing[0])+" AND NEW.kind="+repr(failing[1])+" AND NEW.key="+repr(failing[2])+" BEGIN SELECT RAISE(ABORT,'fixture fail'); END")
    db.commit();db.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError,match="fixture fail"):
        store.register(association(rows,identifier,prompt),rig.inventory(),db=db,fence=rig.fence)
    # The caller may commit independent metadata after handling the optional fault.
    db.execute("INSERT INTO records(namespace,kind,key,value) VALUES('slack/local','future_metadata','independent','{}')")
    db.commit()
    assert flags(db)==[(0,)]*len(rows)
    db.close()


def test_mixed_work_references_cannot_be_owned_by_one_current_binding(rig):
    first,second=("slack:"+sha(str(i))[:40] for i in range(2))
    rows1,rows2=rows_for(first,"one"),rows_for(second,"two",suffix="second")
    enqueue(rig,first,"one");enqueue(rig,second,"two",queue_id=OTHER,order=2)
    db=provider_db(rig,rows1+rows2);store=pin_store(rig)
    before=db.execute("SELECT * FROM records ORDER BY namespace,kind,key").fetchall()
    native_before=(rig.home/"queue_1.sqlite").read_bytes()
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(NativeInflightError,match="pin_reference_invalid"):
        store.register(association(rows1+rows2,first,"one"),rig.inventory(),db=db,fence=rig.fence)
    db.commit()  # Even a caught optional failure must not publish partial pins.
    assert flags(db)==[(0,)]*(len(rows1)+len(rows2))
    assert db.execute("SELECT * FROM records ORDER BY namespace,kind,key").fetchall()==before
    assert (rig.home/"queue_1.sqlite").read_bytes()==native_before
    db.close()


def test_pin_registration_rechecks_freshness_after_caller_fence_delay(rig):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows);store=pin_store(rig);proof=rig.inventory()
    @contextmanager
    def delayed():
        with rig.fence():
            rig.now+=6
            yield
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(NativeInflightError,match="complete_proof_required"):
        store.register(association(rows,identifier,prompt),proof,db=db,fence=delayed)
    db.commit()
    assert flags(db)==[(0,)]*len(rows)
    db.close()


@pytest.mark.parametrize("fault",["bindings_pk","pin_index"])
def test_sidecar_schema_loss_refuses_retirement_before_any_history_scan(rig,fault):
    identifier,prompt="slackbot:"+sha("event")[:40],"current prompt"
    rows=rows_for(identifier,prompt);enqueue(rig,identifier,prompt)
    db=provider_db(rig,rows);store=pin_store(rig)
    db.execute("BEGIN IMMEDIATE");store.register(association(rows,identifier,prompt),rig.inventory(),db=db,fence=rig.fence);db.commit();db.close()
    with sqlite3.connect(store.path) as damaged:
        if fault=="bindings_pk":
            damaged.execute("CREATE TABLE replacement(provider TEXT,identity TEXT,value TEXT)")
            damaged.execute("INSERT INTO replacement SELECT * FROM bindings")
            damaged.execute("DROP TABLE bindings")
            damaged.execute("ALTER TABLE replacement RENAME TO bindings")
        else:damaged.execute("DROP INDEX pin_reference")
    remove_queue(rig)
    with sqlite3.connect(rig.runtime/"slack"/"reply-tickets.sqlite3") as db:
        db.execute("PRAGMA synchronous=FULL");db.execute("BEGIN IMMEDIATE")
        with pytest.raises(NativeInflightError,match="sidecar_schema_invalid"):
            store.reconcile(rig.inventory(),db=db,fence=rig.fence)
        db.commit()
        assert flags(db)==[(1,)]*len(rows)
