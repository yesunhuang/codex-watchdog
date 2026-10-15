"""Finite one-attempt identity, never inferred delivery or native completion."""
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog import wake_attempt_retention as retention
from codex_watchdog.native_inflight import collect_native_work


THREAD = "11111111-2222-4333-8444-000000000001"
OTHER = "11111111-2222-4333-8444-000000000002"
TURN = "11111111-2222-4333-8444-000000000003"
NOW = 1792008000.0
DIGEST = "a" * 64
OLD_SHA = "cd0eb83038723538b507791fd5b54df9fdfbd002b4bdc7d18626d707d00b093d"


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    repos = {thread: tmp_path / ("repo-"+thread) for thread in (THREAD, OTHER)}
    (home/"sessions").mkdir()
    for repo in repos.values():
        repo.mkdir()
    with sqlite3.connect(home/"state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,cwd,source,thread_source,archived,rollout_path)")
        for thread,repo in repos.items():
            rollout=home/"sessions"/("rollout-current-"+thread+".jsonl")
            rollout.write_text("Synthetic native path; payload is never read\n")
            db.execute("INSERT INTO threads VALUES(?,?,'vscode','user',0,?)",(thread,str(repo),str(rollout)))
    with sqlite3.connect(home/"queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT NOT NULL,payload_json TEXT NOT NULL,queue_order INTEGER NOT NULL)")
        db.execute("CREATE INDEX queue_by_thread ON queued_items(thread_id,queue_order)")
        db.execute("CREATE TABLE queued_thread_revisions(thread_id TEXT PRIMARY KEY,revision INTEGER NOT NULL)")
        db.executemany("INSERT INTO queued_thread_revisions VALUES(?,0)",[(thread,) for thread in repos])
    result = SimpleNamespace(home=home,repos=repos,now=NOW,held=0,active={},unknown=[],bad_fence=None)
    monkeypatch.setattr(cs,"_wake_time",lambda:result.now)
    @contextmanager
    def fence():
        result.held += 1
        try:
            yield
        finally:
            result.held -= 1
    result.fence=fence
    def inventory(thread):
        def native():
            assert result.held
            active=result.active.get(thread)
            proof=dict(codex_home=str(home),thread_id=thread,repo_path=str(repos[thread]),observed_at=result.now,
                owner=dict(node="fixture",boot_id="fixture-boot",epoch=2,instance="fixture-owner"),
                thread_read={"thread":{"id":thread,"cwd":str(repos[thread]),"status":{"type":"active" if active else "idle"}}},
                current_turn=None,current_input=None)
            if active:
                identifier,prompt=active
                proof["current_turn"]={"id":TURN,"status":"inProgress"}
                proof["current_input"]={"type":"event_msg","payload":{"thread_id":thread,"turn_id":TURN,
                    "type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":wrapped(identifier,prompt)}]}}}
            return proof
        return collect_native_work(home,thread,repos[thread],current_native=native,fence=fence,clock=lambda:result.now)
    result.inventory=inventory
    @contextmanager
    def home_fence():
        with fence():
            def check():
                assert result.held
                value=dict(schema_version=1,kind="wake_home_sender_fence",codex_home=str(home),observed_at=result.now,
                    discovery_complete=True,all_old_senders_fenced=True,unknown_senders=list(result.unknown),threads=list(repos),
                    senders=[dict(thread_id=thread,node="fixture",boot_id="fixture-boot",instance="fixture-owner",fenced=True) for thread in repos])
                if result.bad_fence:
                    result.bad_fence(value)
                return value
            yield check
    result.home_fence=home_fence
    result.scopes=lambda:{thread:dict(repo_path=repo,inventory_reader=lambda t=thread:inventory(t)) for thread,repo in repos.items()}
    return result


def wrapped(identifier,prompt):
    return "[CODEX_WATCHDOG_WAKE id="+identifier+" sha256="+sha(prompt)+"]\n"+prompt


def enqueue(rig,identifier,prompt,thread=THREAD):
    queue=str(uuid.uuid4())
    with sqlite3.connect(rig.home/"queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES(?,?,?,?)",(queue,thread,json.dumps({"UserInput":{"content":[{"type":"text","text":wrapped(identifier,prompt)}]}}),1))
        db.execute("UPDATE queued_thread_revisions SET revision=revision+1 WHERE thread_id=?",(thread,))
    return queue


def clear(rig):
    with sqlite3.connect(rig.home/"queue_1.sqlite") as db:
        db.execute("DELETE FROM queued_items")
        db.execute("UPDATE queued_thread_revisions SET revision=revision+1")
    rig.active.clear()


def prepare(rig,**kwargs):
    return retention.prepare_wake_retention(rig.home,rig.scopes(),all_sender_fence=rig.home_fence,clock=lambda:rig.now,**kwargs)


def context(rig,thread=THREAD,*,born=None):
    policy=cs.control_wake_retention_policy(rig.home)
    return dict(schema_version=1,thread_id=thread,repo_path=str(rig.repos[thread]),generation=policy["generation"],
        born_at=rig.now if born is None else born,validated_at=rig.now,source_kind="provider_parent",source_key="original-provider-event")


def claim(rig,identifier,digest=DIGEST,thread=THREAD,ctx=True):
    value=context(rig,thread) if ctx is True and cs.control_wake_retention_policy(rig.home) else ctx if isinstance(ctx,dict) else None
    with cs.control_wake_attempt_lock(rig.home,identifier,thread):
        return cs.control_wake_attempt_claim(rig.home,identifier,thread,digest,context=value)


def database(rig):
    return rig.home/"watchdog-wake-retention"/"attempts.sqlite3"


def maintain(rig,**kwargs):
    return retention.WakeAttemptMaintenance(rig.home,clock=lambda:rig.now).step(THREAD,rig.repos[THREAD],
        inventory_reader=lambda:rig.inventory(THREAD),fence=rig.fence,**kwargs)


def test_preparation_is_fenced_and_old_actual_source_cannot_send_after_activation(rig):
    assert claim(rig,"git:old")["duplicate"] is False
    original=cs.control_wake_attempt_path(rig.home,"git:old").read_bytes()
    result=prepare(rig)
    assert result["current_claims"]==0 and result["legacy_history_imported"]==0
    assert (rig.home/"watchdog-wake-retention"/"legacy-retired"/(sha("git:old")+".json")).read_bytes()==original
    old=(Path(__file__).parent/"fixtures"/"wake_attempt_protocol_v1.txt").read_text()
    assert sha(old)==OLD_SHA
    namespace={}
    exec(compile(cs.CONTROL_SOURCE,"<new-protocol-fixture>","exec"),namespace)
    exec(compile(old,"<frozen-prior-protocol>","exec"),namespace)
    with pytest.raises(namespace["ControlError"],match="boundary_invalid"):
        namespace["control_wake_attempt_read"](rig.home,"git:fresh")
    with pytest.raises(namespace["ControlError"],match="boundary_invalid"):
        with namespace["control_wake_attempt_lock"](rig.home,"git:fresh"):
            pytest.fail("old courier was eligible")
    with pytest.raises(cs.ControlError,match="eligibility_required"):
        claim(rig,"git:old",ctx=context(rig,born=rig.now-1))
    rig.now+=1
    assert not claim(rig,"git:independent")["duplicate"]
    assert claim(rig,"git:independent")["duplicate"]


@pytest.mark.parametrize("mutation",[
    lambda v:v.update(all_old_senders_fenced=False),lambda v:v.update(discovery_complete=False),
    lambda v:v.update(unknown_senders=["login5"]),lambda v:v["senders"][0].update(fenced=False),
    lambda v:v.update(observed_at=NOW-6),lambda v:v.update(codex_home="/foreign"),
    lambda v:v.update(threads=[THREAD]),lambda v:v["senders"][0].update(thread_id=str(uuid.uuid4())),
])
def test_unobservable_or_foreign_old_sender_refuses_without_shared_mutation(rig,mutation):
    claim(rig,"git:reserved")
    before={p.name:p.read_bytes() for p in (rig.home/"watchdog-wake-attempts").iterdir()}
    rig.bad_fence=mutation
    with pytest.raises(cs.ControlError,match="all_senders_fence_required"):
        prepare(rig)
    assert not (rig.home/"watchdog-wake-retention").exists()
    assert {p.name:p.read_bytes() for p in (rig.home/"watchdog-wake-attempts").iterdir()}==before
    assert claim(rig,"git:reserved")["duplicate"]
    assert not claim(rig,"git:independent")["duplicate"]


@pytest.mark.parametrize("phase",["prepared","current_durable"])
def test_pre_fence_crash_preserves_legacy_progress_and_existing_claim(rig,phase):
    claim(rig,"git:old")
    original=cs.control_wake_attempt_path(rig.home,"git:old").read_bytes()
    def crash(point):
        if point==phase:
            raise RuntimeError("fixture crash")
    with pytest.raises(RuntimeError):
        prepare(rig,checkpoint=crash)
    assert cs.control_wake_retention_policy(rig.home) is None
    assert cs.control_wake_attempt_path(rig.home,"git:old").read_bytes()==original
    assert claim(rig,"git:old")["duplicate"]
    assert not claim(rig,"git:new-after-prepared")["duplicate"]
    assert prepare(rig)["status"]=="ready"


@pytest.mark.parametrize("phase",["old_root_moved","old_sender_fenced","ready"])
def test_crossed_fence_crash_requires_forward_recovery_never_old_replay(rig,phase):
    claim(rig,"git:old")
    def crash(point):
        if point==phase:
            raise RuntimeError("fixture crash")
    with pytest.raises(RuntimeError):
        prepare(rig,checkpoint=crash)
    if phase!="ready":
        with pytest.raises(cs.ControlError):
            claim(rig,"git:new")
    assert prepare(rig)["status"]=="ready"
    rig.now+=1
    assert not claim(rig,"git:new")["duplicate"]
    assert claim(rig,"git:new")["duplicate"]


def test_recreated_original_directory_after_crossed_fence_is_not_assumed_safe(rig):
    def crash(point):
        if point=="old_root_moved":
            raise RuntimeError()
    with pytest.raises(RuntimeError):
        prepare(rig,checkpoint=crash)
    (rig.home/"watchdog-wake-attempts").mkdir(mode=0o700)
    with pytest.raises(cs.ControlError):
        claim(rig,"git:unproven")
    with pytest.raises(cs.ControlError,match="selector_changed"):
        prepare(rig)


def test_cold_current_queue_and_live_turn_pins_preserve_legacy_unknown_fields(rig):
    prompt="This synthetic current task"
    claim(rig,"git:queued",sha(prompt))
    path=cs.control_wake_attempt_path(rig.home,"git:queued")
    value=json.loads(path.read_text())
    value["future_field"]={"keep":True}
    path.write_text(json.dumps(value))
    enqueue(rig,"git:queued",prompt)
    rig.active[OTHER]=("git:active","Another current task")
    result=prepare(rig)
    assert result["current_claims"]==2
    row=cs.control_wake_attempt_read(rig.home,"git:queued",THREAD,sha(prompt))
    assert row["born_at"] is None and row["expires_at"] is None and row["future_field"]=={"keep":True}
    assert row["state"]=="claimed" and row["current_pin"]
    rig.now+=retention.TTL*10
    assert maintain(rig)["removed"]==0
    assert cs.control_wake_attempt_read(rig.home,"git:queued",THREAD)["current_pin"]
    clear(rig)
    assert maintain(rig)["removed"]==1
    assert cs.control_wake_attempt_read(rig.home,"git:queued",THREAD) is None
    with pytest.raises(cs.ControlError,match="eligibility_required"):
        claim(rig,"git:queued",ctx=context(rig,born=NOW-10))


def test_global_id_collision_and_immutable_birth_across_restart(rig):
    result=prepare(rig)
    rig.now+=1
    first=claim(rig,"git:one")
    rig.now+=100
    assert claim(rig,"git:one")["born_at"]==first["born_at"]
    with pytest.raises(cs.ControlError,match="collision"):
        claim(rig,"git:one",thread=OTHER)
    with pytest.raises(cs.ControlError,match="collision"):
        claim(rig,"git:one",digest="b"*64)
    assert prepare(rig)["generation"]==result["generation"]
    assert cs.control_wake_attempt_read(rig.home,"git:one",THREAD)["expires_at"]==first["born_at"]+retention.TTL


@pytest.mark.parametrize("field,value",[
    ("born_at",NOW),("born_at",NOW+20),("validated_at",NOW-10),("generation","11111111-2222-4333-8444-000000000099"),
    ("repo_path","/wrong"),("thread_id",OTHER),("source_key",""),("source_kind","historical_uncertain"),
    ("schema_version",True),
])
def test_no_original_birth_or_current_exact_context_never_becomes_eligible(rig,field,value):
    prepare(rig)
    rig.now+=10
    ctx=context(rig)
    ctx[field]=value
    with pytest.raises(cs.ControlError,match="eligibility_required"):
        claim(rig,"git:context",ctx=ctx)
    assert cs.control_wake_attempt_read(rig.home,"git:context",THREAD) is None
    assert not claim(rig,"git:newer")["duplicate"]


def test_activated_identityless_unknown_session_calls_and_missing_context_fail_closed(rig):
    prepare(rig)
    rig.now+=1
    with pytest.raises(cs.ControlError,match="exact_context_required"):
        cs.control_wake_attempt_read(rig.home,"git:one")
    with pytest.raises(cs.ControlError,match="not_activated"):
        cs.control_wake_attempt_read(rig.home,"git:one",str(uuid.uuid4()))
    with pytest.raises(cs.ControlError,match="eligibility_required"):
        claim(rig,"git:one",ctx=False)


def test_expiry_forgotten_callback_is_still_terminal_and_new_birth_progresses(rig):
    prepare(rig)
    rig.now+=1
    ctx=context(rig)
    claim(rig,"git:expires",ctx=ctx)
    rig.now+=retention.TTL+1
    assert maintain(rig)["removed"]==1
    with pytest.raises(cs.ControlError,match="eligibility_required"):
        claim(rig,"git:expires",ctx=dict(ctx,validated_at=rig.now))
    assert not claim(rig,"git:new")["duplicate"]


def test_current_known_birth_survives_ttl_then_absence_allows_retirement(rig):
    prepare(rig)
    rig.now+=1
    prompt="Native Q keeps this exact attempt current"
    claim(rig,"git:current",sha(prompt))
    enqueue(rig,"git:current",prompt)
    rig.now+=retention.TTL+1
    assert maintain(rig)["removed"]==0
    assert cs.control_wake_attempt_read(rig.home,"git:current",THREAD)["state"]=="claimed"
    clear(rig)
    assert maintain(rig)["removed"]==1


def test_missing_queue_does_not_establish_absence_or_retire_pins(rig):
    prepare(rig)
    rig.now+=1
    claim(rig,"git:keep")
    rig.now+=retention.TTL+1
    before=database(rig).read_bytes()
    (rig.home/"queue_1.sqlite").rename(rig.home/"queue-unavailable")
    with pytest.raises(ValueError,match="proof_required"):
        maintain(rig)
    assert database(rig).read_bytes()==before


def test_held_courier_is_not_deleted_even_when_native_queue_is_empty(rig):
    prepare(rig)
    rig.now+=1
    claim(rig,"git:inflight")
    rig.now+=retention.TTL+1
    with cs.control_wake_attempt_lock(rig.home,"git:inflight",THREAD):
        result=maintain(rig)
        assert result["busy"]==1 and result["removed"]==0
    assert maintain(rig)["removed"]==1


def test_recent_cap_global_floor_prevents_cross_target_replay_after_compaction(rig):
    prepare(rig)
    contexts={}
    for index in range(retention.LIMIT+12):
        rig.now+=1
        key="git:cap:%04d"%index
        contexts[key]=context(rig)
        claim(rig,key,ctx=contexts[key])
    for _ in range(40):
        result=maintain(rig)
        assert result["examined"]<=retention.BUDGET
    with sqlite3.connect(database(rig)) as db:
        assert db.execute("SELECT count(*) FROM claims WHERE protected=0").fetchone()[0]<=retention.LIMIT
        assert db.execute("SELECT count(*) FROM claims WHERE protected=1").fetchone()[0]==0
    assert cs.control_wake_attempt_read(rig.home,"git:cap:0000",THREAD) is None
    other=context(rig,OTHER,born=contexts["git:cap:0000"]["born_at"])
    with pytest.raises(cs.ControlError,match="eligibility_required"):
        claim(rig,"git:cap:0000",thread=OTHER,ctx=other)
    rig.now+=1
    assert not claim(rig,"git:new-on-other",thread=OTHER)["duplicate"]


def test_cleanup_is_bounded_preserves_unknown_and_progresses_past_unknown_entries(rig):
    root=rig.home/"watchdog-wake-attempts"
    root.mkdir(mode=0o700)
    for i in range(40):
        (root/("unknown-%d"%i)).write_text("unknown")
    for i in range(10):
        claim(rig,"git:old:%d"%i)
    prepare(rig)
    keeper=retention.WakeAttemptMaintenance(rig.home,clock=lambda:rig.now)
    total=0
    for _ in range(20):
        value=keeper.cleanup_legacy(budget=8)
        assert value["examined"]<=8
        total+=value["removed"]
        if value["status"]=="pass_complete":
            break
    assert total==20
    assert len(list((rig.home/"watchdog-wake-retention"/"legacy-retired").iterdir()))==40


def test_scope_close_requires_empty_actual_queue_idle_native_and_bounded_retirement(rig):
    prepare(rig)
    rig.now+=1
    claim(rig,"git:close",sha("task"))
    enqueue(rig,"git:close","task")
    with pytest.raises(cs.ControlError,match="still_current"):
        maintain(rig,close=True)
    clear(rig)
    assert maintain(rig,close=True)["removed"]==1
    with pytest.raises(cs.ControlError,match="not_activated"):
        cs.control_wake_attempt_read(rig.home,"git:close",THREAD)


def test_schema_missing_index_refuses_before_claim_lookup(rig):
    prepare(rig)
    with sqlite3.connect(database(rig)) as db:
        db.execute("DROP INDEX wake_pool")
    with pytest.raises(cs.ControlError,match="schema_invalid"):
        cs.control_wake_attempt_read(rig.home,"git:one",THREAD)


def test_same_process_embedded_namespaces_do_not_release_outer_lock(rig):
    prepare(rig)
    namespace={}
    exec(compile(cs.CONTROL_SOURCE,"<fixture-new-embedded>","exec"),namespace)
    with cs.control_wake_attempt_lock(rig.home,"git:same",THREAD):
        with pytest.raises(namespace["ControlBusy"]):
            with namespace["control_wake_attempt_lock"](rig.home,"git:same",THREAD):
                pytest.fail("second namespace entered")
        with pytest.raises(cs.ControlBusy):
            with cs.control_wake_attempt_lock(rig.home,"git:same",THREAD):
                pytest.fail("reentrant lock entered")


def test_embedded_source_is_python36_compatible():
    ast.parse(cs.CONTROL_SOURCE,feature_version=(3,6))


def test_process_death_after_durable_claim_keeps_same_id_ineligible(rig):
    prepare(rig)
    rig.now+=1
    ctx=context(rig)
    code="""
import json,os,sys
from pathlib import Path
from codex_watchdog import control_state as c
c._wake_time=lambda:float(sys.argv[4])
with c.control_wake_attempt_lock(Path(sys.argv[1]),'git:crash',sys.argv[2]):
 c.control_wake_attempt_claim(Path(sys.argv[1]),'git:crash',sys.argv[2],'a'*64,context=json.loads(sys.argv[3]))
 os._exit(19)
"""
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/"src"))
    result=subprocess.run([sys.executable,"-c",code,str(rig.home),THREAD,json.dumps(ctx),str(rig.now)],env=env,capture_output=True,timeout=10)
    assert result.returncode==19,result.stderr
    assert claim(rig,"git:crash")["duplicate"]
    assert not claim(rig,"git:after-crash")["duplicate"]


def test_forward_recovery_never_lowers_prepared_birth_floor_on_clock_rewind(rig):
    def crash(point):
        if point=="old_root_moved":
            raise RuntimeError()
    with pytest.raises(RuntimeError):
        prepare(rig,checkpoint=crash)
    rig.now-=100
    prepare(rig)
    with sqlite3.connect(database(rig)) as db:
        assert db.execute("SELECT floor FROM metadata").fetchone()[0]==NOW
        assert db.execute("SELECT min(floor) FROM sessions").fetchone()[0]==NOW
    with pytest.raises(cs.ControlError,match="eligibility_required"):
        claim(rig,"git:pre-boundary",ctx=context(rig,born=NOW-50))
    rig.now=NOW+1
    assert not claim(rig,"git:new-clock-safe")["duplicate"]


def test_inventory_repo_must_match_persisted_route_before_retirement(rig):
    prepare(rig)
    rig.now+=1
    claim(rig,"git:reserved")
    with sqlite3.connect(database(rig)) as db:
        db.execute("UPDATE sessions SET repo_path=? WHERE thread_id=?",(str(rig.repos[OTHER]),THREAD))
    rig.now+=retention.TTL+1
    before=database(rig).read_bytes()
    with pytest.raises(cs.ControlError,match="scope_invalid"):
        maintain(rig)
    assert database(rig).read_bytes()==before


@pytest.mark.parametrize("column,value",[("thread_id","x"*200000),("prompt_sha256","x"*200000),
    ("born","x"*200000),("expires","x"*200000),("value","x"*200000)],
    ids=lambda value: value if len(value)<64 else "oversized-"+str(len(value)))
def test_oversized_bad_exact_claim_is_bounded_and_independent_new_claim_progresses(rig,column,value):
    prepare(rig)
    rig.now+=1
    claim(rig,"git:bad")
    with sqlite3.connect(database(rig)) as db:
        db.execute("UPDATE claims SET "+column+"=? WHERE instruction_id='git:bad'",(value,))
    with pytest.raises(cs.ControlError,match="record_invalid"):
        cs.control_wake_attempt_read(rig.home,"git:bad",THREAD)
    assert not claim(rig,"git:good")["duplicate"]


def test_closed_scope_explicit_reenrollment_uses_new_floor_and_current_proof(rig):
    prepare(rig)
    rig.now+=1
    old=context(rig)
    claim(rig,"git:old",ctx=old)
    assert maintain(rig,close=True)["removed"]==1
    rig.now+=1
    result=retention.enroll_wake_session(rig.home,THREAD,rig.repos[THREAD],
        inventory_reader=lambda:rig.inventory(THREAD),fence=rig.fence,clock=lambda:rig.now)
    assert not result["duplicate"]
    with pytest.raises(cs.ControlError,match="eligibility_required"):
        claim(rig,"git:old",ctx=dict(old,validated_at=rig.now))
    rig.now+=1
    assert not claim(rig,"git:new")["duplicate"]


def test_death_between_sentinel_link_and_unlink_recovers_only_exact_known_inode(rig,monkeypatch):
    real_link=os.link
    def crash(source,target,*args,**kwargs):
        real_link(source,target,*args,**kwargs)
        if Path(target)==rig.home/"watchdog-wake-attempts":
            raise RuntimeError("process-death boundary")
    with monkeypatch.context() as patch:
        patch.setattr(os,"link",crash)
        with pytest.raises(RuntimeError,match="process-death"):
            prepare(rig)
    original=rig.home/"watchdog-wake-attempts"
    temporary=rig.home/"watchdog-wake-retention"/"sentinel.tmp"
    assert original.stat().st_nlink==2 and original.stat().st_ino==temporary.stat().st_ino
    with pytest.raises(cs.ControlError):
        claim(rig,"git:unsafe-before-recovery")
    assert prepare(rig)["status"]=="ready"
    assert original.stat().st_nlink==1 and not temporary.exists()
    rig.now+=1
    assert not claim(rig,"git:safe-after-recovery")["duplicate"]


def test_partial_known_sentinel_temp_recovers_but_unknown_bytes_are_preserved(rig):
    def crash(point):
        if point=="old_root_moved":
            raise RuntimeError()
    with pytest.raises(RuntimeError):
        prepare(rig,checkpoint=crash)
    temporary=rig.home/"watchdog-wake-retention"/"sentinel.tmp"
    temporary.write_bytes(b"unknown user data")
    temporary.chmod(0o600)
    with pytest.raises(cs.ControlError,match="recovery_invalid"):
        prepare(rig)
    assert temporary.read_bytes()==b"unknown user data"
    # Explicit fixture construction of the known partial-write crash state.
    temporary.write_bytes(cs._WAKE_SENTINEL[:12])
    assert prepare(rig)["status"]=="ready"


def test_unsupported_bounded_schema_probe_refuses_before_structural_old_root_fence(rig,monkeypatch):
    claim(rig,"git:old")
    with monkeypatch.context() as patch:
        patch.setattr(cs,"_wake_schema",lambda db:(_ for _ in ()).throw(sqlite3.OperationalError("fixture unsupported SQL")))
        with pytest.raises(sqlite3.OperationalError,match="unsupported SQL"):
            prepare(rig)
    assert cs.control_wake_retention_policy(rig.home) is None
    assert claim(rig,"git:old")["duplicate"]
    assert not claim(rig,"git:new-legacy")["duplicate"]
    assert prepare(rig)["status"]=="ready"


def test_prepared_current_projection_can_forward_replan_after_scope_closes(rig):
    enqueue(rig,"git:completed-before-ready","Former native work",thread=OTHER)
    def crash(point):
        if point=="old_root_moved":
            raise RuntimeError()
    with pytest.raises(RuntimeError):
        prepare(rig,checkpoint=crash)
    old_policy=json.loads((rig.home/"watchdog-wake-retention"/"policy.json").read_text())
    clear(rig)
    # Complete fresh current projection: OTHER genuinely closed, current work
    # remains only in THREAD. Existing all-home fence still covers old senders.
    scopes=rig.scopes()
    scopes.pop(OTHER)
    @contextmanager
    def fence():
        with rig.home_fence() as check:
            def current():
                proof=check()
                proof["threads"]=[THREAD]
                return proof
            yield current
    rig.now+=1
    result=retention.prepare_wake_retention(rig.home,scopes,all_sender_fence=fence,clock=lambda:rig.now)
    assert result["prepared_replanned"] and result["generation"]==old_policy["generation"]
    with sqlite3.connect(database(rig)) as db:
        assert db.execute("SELECT thread_id FROM sessions").fetchall()==[(THREAD,)]
        assert db.execute("SELECT count(*) FROM claims").fetchone()==(0,)
        assert db.execute("SELECT floor FROM metadata").fetchone()[0]>=old_policy["activated_at"]
    rig.now+=1
    assert not claim(rig,"git:fresh-after-replan")["duplicate"]


@pytest.mark.parametrize("history",[0,1000,10000,100000])
def test_history_growth_fixed_current_and_zero_recent_have_bounded_hot_operations(rig,monkeypatch,history):
    root=rig.home/"watchdog-wake-attempts"
    root.mkdir(mode=0o700)
    for index in range(history):
        key="git:historical:%d"%index
        value=dict(schema_version=1,kind="wake_attempt",state="claimed",instruction_id=key,
            thread_id=THREAD,prompt_sha256=DIGEST,origin="dispatch")
        path=root/(sha(key)+".json")
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    enqueue(rig,"git:current","Fixed current input")
    assert prepare(rig)["current_claims"]==1
    rig.now+=1
    original_connect=sqlite3.connect
    counters={"sql":0,"vm":0}
    class Measured(sqlite3.Connection):
        def execute(self,*args,**kwargs):
            counters["sql"]+=1
            return super().execute(*args,**kwargs)
        def set_progress_handler(self,callback,n):
            ticks=[0]
            def progress():
                counters["vm"]+=1
                ticks[0]+=1
                return callback() if ticks[0]%n==0 else 0
            return super().set_progress_handler(progress,1)
    def connect(path,*args,**kwargs):
        if "watchdog-wake-retention/attempts.sqlite3" in str(path):
            kwargs["factory"]=Measured
        return original_connect(path,*args,**kwargs)
    def no_history(*args,**kwargs):
        pytest.fail("hot path traversed historical files")
    original_glob,original_scandir=Path.glob,os.scandir
    def queue_files(path,pattern,*args,**kwargs):
        if path==rig.home and pattern=="queue_*.sqlite":
            return original_glob(path,pattern,*args,**kwargs)
        no_history()
    def root_directory(path):
        if Path(path)==rig.home:
            return original_scandir(path)
        no_history()
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3,"connect",connect)
        patch.setattr(Path,"glob",queue_files)
        patch.setattr(Path,"rglob",no_history)
        patch.setattr(os,"scandir",root_directory)
        assert not claim(rig,"git:fixed-new")["duplicate"]
        assert claim(rig,"git:fixed-new")["duplicate"]
        assert maintain(rig)["removed"]==0
    active=dict(counters)
    clear(rig)
    rig.now+=retention.TTL+1
    assert maintain(rig)["removed"]==2
    counters.update(sql=0,vm=0)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3,"connect",connect)
        patch.setattr(Path,"glob",queue_files)
        patch.setattr(Path,"rglob",no_history)
        patch.setattr(os,"scandir",root_directory)
        result=maintain(rig)
        assert result["examined"]==0 and result["removed"]==0
    quiet=dict(counters)
    assert active["sql"]<=256 and active["vm"]<=30000
    assert quiet["sql"]<=64 and quiet["vm"]<=10000
    cleanup=retention.WakeAttemptMaintenance(rig.home,clock=lambda:rig.now).cleanup_legacy()
    assert cleanup["examined"]<=retention.BUDGET
    print("WAKE_HISTORY_OPERATIONS "+json.dumps(dict(history=history,actual_legacy_files=history,
        fixed_current_queue=1,active=active,zero_recent=quiet,cleanup_examined=cleanup["examined"],
        cleanup_removed=cleanup["removed"]),sort_keys=True))
