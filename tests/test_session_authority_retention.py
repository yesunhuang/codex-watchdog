"""Cold exact-session retention uses real authority/native journal boundaries."""
from contextlib import nullcontext, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog import session_authority_retention as retention
from codex_watchdog.models import sha256_text
from codex_watchdog.native_inflight import collect_native_work
from codex_watchdog.relay_authority import SessionRelayAuthority
from codex_watchdog.relay_authority_migration import (read_authority_snapshot, snapshot_digest,
                                                     write_authority_snapshot)
from test_linux_node import THREAD, nodes
from test_relay_authority import _node, LOGIN6


NOW = 1800000000.0
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
QUEUE = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
NAMESPACE = "slack/poll-relay-state.json"
PRINCIPAL = dict(team_id="T12345678", user_id="U12345678", bot_id="B12345678", app_id="A12345678")


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def row(kind, key, body, *, thread=THREAD, active=0):
    return dict(namespace=NAMESPACE, kind=kind, key=key, value=json.dumps(body, sort_keys=True),
                fingerprint=body.get("event_fingerprint"), thread_id=thread,
                active=active, created_at=body.get("created_at"))


def ticket(key, created, thread=THREAD, active=1):
    body = dict(channel_id="C12345678", thread_ts="1799999000.%06d" % (int(key[-2:],16)+1),
                target=dict(workspace_id="workspace-"+thread, thread_id=thread,
                            execution_locality="remote_ssh", remote_authority="ssh-remote+fixture",
                            remote_repo_path="/fixture/project", remote_storage_key="a"*32),
                event_fingerprint=key, ticket_schema=1, created_at=stamp(created))
    return row("threads",key,body,thread=thread,active=active)


def snapshot(version=2, extra=()):
    current = dict(schema_version=1, provider="slack", scope="C12345678", thread_id=THREAD,
                   principals=[PRINCIPAL], created_at=stamp(NOW-1000), future_choice="keep")
    registry = dict(schema_version=1, provider="slack", user_id=PRINCIPAL["user_id"],
                    principals=[PRINCIPAL], created_at=stamp(NOW-1000))
    rows = [row("slack_bot_grants","grant",current),
            row("slack_bot_users","known",registry,thread=None),
            row("session_routes","route",dict(schema_version=1,provider="slack",scope="C12345678",
                thread_id=THREAD,destination=None,last_command_ts="1799999000.000001",
                created_at=stamp(NOW-1000))),
            row("slack_bot_grants","foreign",dict(current,thread_id=OTHER),thread=OTHER)]
    rows += [ticket(sha256_text(str(n)),NOW-100+n) for n in range(6)]
    rows += [ticket("f"*64,NOW-100,OTHER)]
    rows += list(extra)
    recent = []
    if version == 3:
        for entry in rows:
            if entry["kind"] in retention.RECENT_KINDS:
                born = datetime.fromisoformat(entry["created_at"].replace("Z","+00:00")).timestamp()
                recent.append(dict(namespace=entry["namespace"],kind=entry["kind"],key=entry["key"],
                    born=born,expires=born+72*3600,protected=0,session=entry["thread_id"] or ""))
    return dict(schema_version=1,source_schema=version,namespaces=[NAMESPACE],records=rows,
                recent_records=recent,current_boundaries=([] if version==2 else [
                    dict(namespace=NAMESPACE,session="",floor=0,checked_at=NOW-20),
                    dict(namespace=NAMESPACE,session=THREAD,floor=NOW-200,checked_at=NOW-10),
                    dict(namespace=NAMESPACE,session=OTHER,floor=NOW-50,checked_at=NOW-10)]))


@pytest.fixture
def native(nodes):
    home,repo,host,roots,pid = nodes
    authority,_ = _node(nodes)
    # Actual indexed native thread locator and queue schemas. No RPC/consumer
    # or native process is executed by these source fixtures.
    rollout = home/"sessions"/("rollout-fixture-"+THREAD+".jsonl")
    rollout.write_text("{}\n")
    with sqlite3.connect(home/"state_5.sqlite") as db:
        db.execute("CREATE UNIQUE INDEX native_thread_id ON threads(id)")
        db.execute("UPDATE threads SET rollout_path=? WHERE id=?",(str(rollout),THREAD))
    with sqlite3.connect(home/"queue_1.sqlite") as db:
        db.execute("ALTER TABLE queued_items ADD COLUMN queue_order INTEGER DEFAULT 0")
        db.execute("CREATE UNIQUE INDEX native_queue_id ON queued_items(id)")
        db.execute("CREATE INDEX native_thread_queue ON queued_items(thread_id,queue_order)")
        db.execute("CREATE UNIQUE INDEX native_queue_revision ON queued_thread_revisions(thread_id)")
        db.execute("INSERT INTO queued_thread_revisions VALUES(?,0)",(THREAD,))
    owner = dict(node=LOGIN6,boot_id=authority.store.boot_id,epoch=authority.token["epoch"],
                 instance=authority.token["instance"])
    def current_native():
        return dict(codex_home=str(home),thread_id=THREAD,repo_path=str(repo),owner=owner,
            observed_at=NOW,thread_read={"thread":{"id":THREAD,"cwd":str(repo),"status":{"type":"idle"}}},
            current_turn=None,current_input=None)
    def inventory():
        return collect_native_work(home,THREAD,repo,current_native=current_native,
                                   fence=nullcontext,clock=lambda:NOW)
    source = authority.runtime/"slack/reply-tickets.sqlite3"
    lock = authority.runtime/"locks/slack-authority.lock"
    lock.parent.mkdir(parents=True,exist_ok=True)
    lock.write_bytes(b"\0")
    # Unknown login5 and a different exact manifest must remain byte-identical.
    unknown = home/"watchdog-nodes/login5.example/private-owner-state.json"
    unknown.parent.mkdir(parents=True)
    unknown.write_text('{"unreachable_owner":"preserve"}')
    other_manifest = authority.root/"sessions"/(OTHER+".json")
    cs.control_atomic_json(other_manifest,dict(schema_version=1,storage="unknown-old-owner",thread_id=OTHER))
    return dict(authority=authority,home=home,repo=repo,source=source,inventory=inventory,
                unknown=unknown,other_manifest=other_manifest)


def migrate(native, **kwargs):
    authority = native["authority"]
    with authority.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        options=dict(sources={"slack":native["source"]},inflight_reader=native["inventory"],
                     sender_fence=nullcontext,clock=lambda:NOW)
        options.update(kwargs)
        return retention.migrate_session_authority(authority,**options)


@pytest.mark.parametrize("version",[2,3])
def test_compact_source_upgrade_selects_only_exact_session_preserving_current_authority(native,version):
    write_authority_snapshot(native["source"],snapshot(version))
    authority=native["authority"]
    protected=[native["source"],authority.root/"cluster.json",native["unknown"],
               native["other_manifest"],native["home"]/"state_5.sqlite",native["home"]/"queue_1.sqlite"]
    before={path:path.read_bytes() for path in protected}
    notification=authority.notification_runtime
    result=migrate(native)
    assert result["status"]=="migrated"
    assert all(path.read_bytes()==raw for path,raw in before.items())
    authority._ready()
    assert authority.notification_runtime==notification
    assert authority.runtime!=authority.root/"runtime"
    value=read_authority_snapshot(authority.runtime/"slack/reply-tickets.sqlite3")
    assert value["source_schema"]==3
    assert not any(entry["thread_id"]==OTHER for entry in value["records"])
    assert sum(entry["kind"]=="threads" and entry["active"] for entry in value["records"])==4
    grant=next(json.loads(entry["value"]) for entry in value["records"] if entry["kind"]=="slack_bot_grants")
    assert grant["principals"]==[PRINCIPAL] and grant["future_choice"]=="keep"
    assert any(entry["kind"]=="slack_bot_users" for entry in value["records"])
    assert any(entry["session"]==THREAD and entry["floor"]>=NOW for entry in value["current_boundaries"])
    assert not any(entry["session"]==OTHER for entry in value["current_boundaries"])
    if version==3:
        original={entry["key"]:entry for entry in snapshot(3)["recent_records"]}
        assert all((entry["born"],entry["expires"])==(original[entry["key"]]["born"],original[entry["key"]]["expires"])
                   for entry in value["recent_records"])
    fresh=SessionRelayAuthority(authority.store,authority.token,authority.observation,authority.workspace_id)
    with fresh.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        fresh.activate()
    assert fresh.runtime==authority.runtime
    with fresh.journal("slack").transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()==(3,)


def test_repeat_after_new_progress_never_reimports_history_or_requires_pristine_copy(native,monkeypatch):
    from codex_watchdog import relay_authority_migration as snapshots
    write_authority_snapshot(native["source"],snapshot())
    migrate(native)
    authority=native["authority"]
    with sqlite3.connect(authority.runtime/"slack/reply-tickets.sqlite3") as db:
        db.execute("UPDATE current_boundaries SET checked_at=checked_at+1")
    before=authority.manifest.read_bytes()
    monkeypatch.setattr(snapshots,"read_authority_snapshot",lambda *args:pytest.fail("repeat read history"))
    assert migrate(native)["status"]=="already_migrated"
    assert authority.manifest.read_bytes()==before


@pytest.mark.parametrize("phase",["before_manifest","after_manifest"])
def test_crash_around_exact_manifest_is_idempotent_and_does_not_rewrite_sources(native,monkeypatch,phase):
    write_authority_snapshot(native["source"],snapshot())
    authority=native["authority"]
    source=native["source"].read_bytes()
    original=retention.control_atomic_json
    def crash(path,value):
        if path==authority.manifest:
            if phase=="after_manifest":original(path,value)
            raise RuntimeError("fixture publication crash")
        original(path,value)
    with monkeypatch.context() as patch:
        patch.setattr(retention,"control_atomic_json",crash)
        with pytest.raises(RuntimeError,match="publication crash"):
            migrate(native)
    result=migrate(native)
    assert result["status"]==("migrated" if phase=="before_manifest" else "already_migrated")
    assert native["source"].read_bytes()==source
    assert json.loads(authority.manifest.read_text())["storage"]==retention.STORAGE


def test_new_storage_discriminator_fences_old_consumers_without_global_cluster_change(native):
    write_authority_snapshot(native["source"],snapshot())
    cluster=native["authority"].root/"cluster.json"
    before=cluster.read_bytes()
    migrate(native)
    manifest=cs.control_read_json(native["authority"].manifest)
    # Exact old production acceptance predicate, not an invented permissive reader.
    assert manifest["storage"]!="cluster_provider_journals_v1"
    assert cs.control_read_json(cluster)["storage"]=="cluster_provider_journals_v1"
    assert cluster.read_bytes()==before


def test_migration_requires_admitted_exact_native_owner_and_sender_fence(native):
    write_authority_snapshot(native["source"],snapshot())
    authority=native["authority"]
    with pytest.raises(cs.ControlBusy,match="observation_required"):
        retention.migrate_session_authority(authority,sources={"slack":native["source"]},
            inflight_reader=native["inventory"],sender_fence=nullcontext,clock=lambda:NOW)
    assert cs.control_read_json(authority.manifest)["storage"]=="cluster_provider_journals_v1"


def test_unknown_current_scope_or_kind_is_not_silently_promoted_or_discarded(native):
    extra=row("future_authorization","unknown",dict(thread_id=THREAD,permission=True))
    write_authority_snapshot(native["source"],snapshot(extra=[extra]))
    with pytest.raises(cs.ControlError,match="kind_unsupported"):
        migrate(native)
    assert cs.control_read_json(native["authority"].manifest)["storage"]=="cluster_provider_journals_v1"


def test_readiness_does_not_scan_or_reconstruct_provider_history(native,monkeypatch):
    from codex_watchdog import relay_authority_migration as snapshots
    write_authority_snapshot(native["source"],snapshot(3))
    migrate(native)
    monkeypatch.setattr(snapshots,"read_authority_snapshot",lambda *args:pytest.fail("hot history scan"))
    for _ in range(4):native["authority"]._ready()


def test_expired_uncertain_history_is_not_current_pin_but_actual_native_queue_is(native):
    parent=ticket("e"*64,NOW-4*24*3600,active=0)
    instruction,prompt="slackbot:actual-current-fixture","fixture current work"
    event=row("events","d"*64,dict(thread_key=parent["key"],instruction_id=instruction,
        text_sha256=sha256_text(prompt),message_key="c"*64,state="uncertain",created_at=parent["created_at"]),active=1)
    physical=row("messages","c"*64,dict(event_key=event["key"]),thread=None)
    stale=row("events","b"*64,dict(thread_key=parent["key"],instruction_id="slackbot:old-unknown",
        text_sha256="a"*64,state="uncertain",created_at=parent["created_at"]),active=1)
    original=snapshot(extra=[parent,event,physical,stale])
    write_authority_snapshot(native["source"],original)
    payload=json.dumps({"UserInput":{"content":[{"type":"text","text":
        "[CODEX_WATCHDOG_WAKE id=%s sha256=%s]\n%s" % (instruction,sha256_text(prompt),prompt)}]}})
    with sqlite3.connect(native["home"]/"queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items(id,thread_id,payload_json) VALUES(?,?,?)",(QUEUE,THREAD,payload))
    result=migrate(native)
    assert result["status"]=="migrated"
    migrated=read_authority_snapshot(native["authority"].runtime/"slack/reply-tickets.sqlite3")
    ids={(entry["namespace"],entry["kind"],entry["key"]) for entry in migrated["records"]}
    assert (NAMESPACE,"events",stale["key"]) not in ids
    assert {(NAMESPACE,entry["kind"],entry["key"]) for entry in (parent,event,physical)}.issubset(ids)
    assert all(entry["protected"] for entry in migrated["recent_records"] if entry["key"] in (parent["key"],event["key"],physical["key"]))
    from codex_watchdog.native_inflight_pins import CurrentNativePins
    pins=CurrentNativePins(native["authority"].runtime,native["home"],THREAD,native["repo"],clock=lambda:NOW)
    exported=pins.export()
    assert len(exported["bindings"])==1
    assert {tuple(ref) for ref in exported["bindings"][0]["refs"]}=={
        (NAMESPACE,entry["kind"],entry["key"]) for entry in (parent,event,physical)}
    manifest=cs.control_read_json(native["authority"].manifest)
    assert exported["sha256"]==manifest["native_pins_sha256"]
    with sqlite3.connect(native["home"]/"queue_1.sqlite") as db:
        assert db.execute("SELECT id,payload_json FROM queued_items").fetchall()==[(QUEUE,payload)]


@pytest.mark.parametrize("sources",[{}, {"onebot":"not-the-configured-provider"}])
def test_omitting_or_replacing_existing_provider_is_refused_before_intent(native,sources):
    write_authority_snapshot(native["source"],snapshot())
    before=native["authority"].manifest.read_bytes()
    with pytest.raises(cs.ControlError,match="complete_providers_required"):
        migrate(native,sources=sources)
    assert native["authority"].manifest.read_bytes()==before
    assert not (native["authority"].root/"session-authorities").exists()


def test_configured_missing_provider_is_not_silently_ignored(native):
    write_authority_snapshot(native["source"],snapshot())
    cluster=native["authority"].root/"cluster.json"
    value=cs.control_read_json(cluster)
    cs.control_atomic_json(cluster,dict(value,providers=["slack","lark"]))
    with pytest.raises(cs.ControlError,match="complete_providers_required"):
        migrate(native)
    assert not (native["authority"].root/"session-authorities").exists()


def test_wrong_provider_namespace_is_not_imported_as_current_authority(native):
    value=snapshot()
    value["namespaces"].append("lark/relay-state.json")
    value["namespaces"].sort()
    write_authority_snapshot(native["source"],value)
    with pytest.raises(cs.ControlError,match="provider_namespace_mismatch"):
        migrate(native)
    assert cs.control_read_json(native["authority"].manifest)["storage"]=="cluster_provider_journals_v1"


def test_equal_ticket_times_preserve_actual_row_rank_and_next_n4_membership(native):
    value=snapshot(3)
    tickets=[entry for entry in value["records"] if entry["kind"]=="threads" and entry["thread_id"]==THREAD]
    for entry in tickets:
        body=json.loads(entry["value"])
        body["created_at"]=stamp(NOW-100)
        entry["created_at"],entry["value"]=body["created_at"],json.dumps(body,sort_keys=True)
    for metadata in value["recent_records"]:
        if metadata["key"] in {entry["key"] for entry in tickets}:
            metadata["born"],metadata["expires"]=NOW-100,NOW-100+72*3600
    order=[tuple(entry[name] for name in ("namespace","kind","key")) for entry in reversed(value["records"])]
    value["record_order"]=[list(item) for item in order]
    write_authority_snapshot(native["source"],value)
    migrate(native)
    copied=read_authority_snapshot(native["authority"].runtime/"slack/reply-tickets.sqlite3")
    assert copied["record_order"]==[list(item) for item in order if item in {
        tuple(row[name] for name in ("namespace","kind","key")) for row in copied["records"]}]
    rank=[identity[2] for identity in order if identity[1]=="threads" and identity[2] in {row["key"] for row in tickets}]
    assert {entry["key"] for entry in copied["records"] if entry["kind"]=="threads" and entry["active"]}==set(rank[-4:])


def prepared(native,monkeypatch):
    original=retention.control_atomic_json
    def crash(path,value):
        if path==native["authority"].manifest:raise RuntimeError("fixture before selector")
        original(path,value)
    with monkeypatch.context() as patch:
        patch.setattr(retention,"control_atomic_json",crash)
        with pytest.raises(RuntimeError,match="before selector"):
            migrate(native)
    root=native["authority"].root/"session-authorities"/THREAD
    return root,cs.control_read_json(root/"migration-intent.json")


def abandon(native,intent):
    authority=native["authority"]
    with authority.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        return retention.abandon_prepared_session_authority(authority,intent["plan_sha256"],
            inflight_reader=native["inventory"],sender_fence=nullcontext,clock=lambda:NOW)


def test_changed_source_can_abandon_only_unselected_copy_then_freshly_reprepare(native,monkeypatch):
    write_authority_snapshot(native["source"],snapshot())
    root,intent=prepared(native,monkeypatch)
    with sqlite3.connect(native["source"]) as db:
        original=json.loads(db.execute("SELECT value FROM records WHERE kind='slack_bot_grants' AND key='grant'").fetchone()[0])
        db.execute("UPDATE records SET value=? WHERE kind='slack_bot_grants' AND key='grant'",
                   (json.dumps(dict(original,future_choice="new-current-state")),))
    source=native["source"].read_bytes()
    with pytest.raises(cs.ControlError,match="recovery_state_changed"):
        migrate(native)
    assert abandon(native,intent)["status"]=="abandoned"
    assert not (root/"migration-intent.json").exists()
    assert (root/"abandoned/runtime/slack/reply-tickets.sqlite3").is_file()
    assert native["source"].read_bytes()==source
    assert migrate(native)["status"]=="migrated"
    assert native["source"].read_bytes()==source


def test_repeated_abandon_retains_one_compact_managed_copy(native,monkeypatch):
    write_authority_snapshot(native["source"],snapshot())
    for iteration in range(2):
        root,intent=prepared(native,monkeypatch)
        assert abandon(native,intent)["status"]=="abandoned"
        assert not (root/"migration-intent.json").exists()
        assert not (root/"retiring-abandoned.json").exists()
        assert [path.name for path in root.iterdir() if path.is_dir()]==["abandoned"]
        with sqlite3.connect(native["source"]) as db:
            value=json.loads(db.execute("SELECT value FROM records WHERE key='grant'").fetchone()[0])
            db.execute("UPDATE records SET value=? WHERE key='grant'",(json.dumps(dict(value,iteration=iteration)),))


def test_unknown_prepared_file_prevents_abandon_without_deleting_evidence(native,monkeypatch):
    write_authority_snapshot(native["source"],snapshot())
    root,intent=prepared(native,monkeypatch)
    path=root/intent["plan_sha256"]/"unrelated-user-file"
    path.write_bytes(b"retain unknown evidence")
    with pytest.raises(cs.ControlError,match="unknown_prepared_artifact"):
        abandon(native,intent)
    assert path.read_bytes()==b"retain unknown evidence"
    assert (root/"migration-intent.json").exists()


def test_abandon_restart_after_move_retains_recovery_and_finishes_only_metadata(native,monkeypatch):
    write_authority_snapshot(native["source"],snapshot())
    root,intent=prepared(native,monkeypatch)
    original=retention.os.rename
    def crash(source,destination):
        original(source,destination)
        raise RuntimeError("fixture after recovery move")
    with monkeypatch.context() as patch:
        patch.setattr(retention.os,"rename",crash)
        with pytest.raises(RuntimeError,match="after recovery move"):
            abandon(native,intent)
    assert abandon(native,intent)["status"]=="abandoned"
    assert (root/"abandoned/runtime/slack/reply-tickets.sqlite3").is_file()
    assert not (root/"migration-intent.json").exists()


def test_activated_candidate_can_never_be_abandoned(native):
    write_authority_snapshot(native["source"],snapshot())
    result=migrate(native)
    manifest=native["authority"].manifest.read_bytes()
    with pytest.raises(cs.ControlError,match="already_selected_or_changed"):
        abandon(native,dict(plan_sha256=result["plan_sha256"]))
    assert native["authority"].manifest.read_bytes()==manifest


@pytest.mark.parametrize("location", ["stage", "destination"])
def test_unknown_prepared_file_prevents_activation_without_altering_evidence(native,monkeypatch,location):
    write_authority_snapshot(native["source"],snapshot())
    root,intent=prepared(native,monkeypatch)
    candidate=root/intent["plan_sha256"]
    if location=="stage":
        staged=root/("stage-"+intent["plan_sha256"])
        candidate.rename(staged)
        candidate=staged
    unknown=candidate/"unrelated-user-file"
    unknown.write_bytes(b"preserve unrelated evidence")
    manifest=native["authority"].manifest.read_bytes()
    with pytest.raises(cs.ControlError,match="unknown_prepared_artifact"):
        migrate(native)
    assert unknown.read_bytes()==b"preserve unrelated evidence"
    assert native["authority"].manifest.read_bytes()==manifest


def test_current_work_change_at_publication_refuses_without_source_or_selector_write(native):
    write_authority_snapshot(native["source"],snapshot())
    source=native["source"].read_bytes()
    manifest=native["authority"].manifest.read_bytes()
    calls=[]
    def reader():
        if calls:
            with sqlite3.connect(native["home"]/"queue_1.sqlite") as db:
                db.execute("UPDATE queued_thread_revisions SET revision=revision+1 WHERE thread_id=?",(THREAD,))
        calls.append(True)
        return native["inventory"]()
    with pytest.raises(cs.ControlBusy,match="native_work_changed"):
        migrate(native,inflight_reader=reader)
    assert len(calls)==2
    assert native["source"].read_bytes()==source
    assert native["authority"].manifest.read_bytes()==manifest


def test_sender_fence_spans_current_proof_and_selector_publication(native,monkeypatch):
    write_authority_snapshot(native["source"],snapshot())
    held=[]
    @contextmanager
    def sender():
        held.append(True)
        try:yield
        finally:held.pop()
    def reader():
        assert held==[True]
        return native["inventory"]()
    original=retention.control_atomic_json
    def publish(path,value):
        if path==native["authority"].manifest:assert held==[True]
        return original(path,value)
    monkeypatch.setattr(retention,"control_atomic_json",publish)
    assert migrate(native,sender_fence=sender,inflight_reader=reader)["status"]=="migrated"
    assert held==[]


def test_crash_with_known_incomplete_stage_resumes_without_importing_unknowns(native,monkeypatch):
    write_authority_snapshot(native["source"],snapshot())
    original=retention._prepare_pins
    with monkeypatch.context() as patch:
        def crash(*args,**kwargs):raise RuntimeError("fixture incomplete stage")
        patch.setattr(retention,"_prepare_pins",crash)
        with pytest.raises(RuntimeError,match="incomplete stage"):migrate(native)
    assert retention._prepare_pins is original
    assert migrate(native)["status"]=="migrated"


def test_crash_while_rotating_known_recovery_resumes_without_deleting_unknowns(native,monkeypatch):
    write_authority_snapshot(native["source"],snapshot())
    root,intent=prepared(native,monkeypatch)
    abandon(native,intent)
    with sqlite3.connect(native["source"]) as db:
        body=json.loads(db.execute("SELECT value FROM records WHERE key='grant'").fetchone()[0])
        db.execute("UPDATE records SET value=? WHERE key='grant'",(json.dumps(dict(body,future_choice="changed")),))
    root,intent=prepared(native,monkeypatch)
    original=Path.unlink
    crashed=[]
    def unlink(path,*args,**kwargs):
        original(path,*args,**kwargs)
        if "abandoned" in path.parts and path.name=="reply-tickets.sqlite3" and not crashed:
            crashed.append(True)
            raise RuntimeError("fixture recovery retirement")
    with monkeypatch.context() as patch:
        patch.setattr(Path,"unlink",unlink)
        with pytest.raises(RuntimeError,match="recovery retirement"):abandon(native,intent)
    assert (root/"retiring-abandoned.json").exists()
    assert abandon(native,intent)["status"]=="abandoned"
    assert not (root/"retiring-abandoned.json").exists()
    assert (root/"abandoned/runtime/slack/reply-tickets.sqlite3").is_file()


@pytest.mark.parametrize("operation",["migrate","abandon"])
def test_exact_session_publication_uses_existing_cluster_cold_lock_nonblocking(native,monkeypatch,operation):
    write_authority_snapshot(native["source"],snapshot())
    if operation=="abandon":root,intent=prepared(native,monkeypatch)
    manifest=native["authority"].manifest.read_bytes()
    source=native["source"].read_bytes()
    ready,release=threading.Event(),threading.Event()
    def holder():
        with retention.observation_lock(native["authority"].root/"migration.lock"):
            ready.set()
            assert release.wait(5)
    worker=threading.Thread(target=holder)
    worker.start()
    assert ready.wait(5)
    try:
        with pytest.raises(cs.ControlBusy,match="observation_busy"):
            if operation=="migrate":migrate(native)
            else:abandon(native,intent)
        assert native["authority"].manifest.read_bytes()==manifest
        assert native["source"].read_bytes()==source
        if operation=="abandon":assert (root/"migration-intent.json").exists()
    finally:
        release.set();worker.join(5)
    assert not worker.is_alive()
    if operation=="migrate":assert migrate(native)["status"]=="migrated"
    else:assert abandon(native,intent)["status"]=="abandoned"
