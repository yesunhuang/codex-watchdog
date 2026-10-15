"""Actual prepared-only observer enrollment and service/notifier boundaries."""
from contextlib import contextmanager, nullcontext
from datetime import datetime,timezone
import hashlib,json,sqlite3
from types import SimpleNamespace

import pytest

from codex_watchdog import notification_receipt_retention as finite
from codex_watchdog.notification_receipts import maintain_observer_receipts
from codex_watchdog.notification_context import observer_scope
from codex_watchdog.notification_senders import sender_fence
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.notifications import NotificationConfig,EnvironmentNotifier
from codex_watchdog.storage import StoreBusyError
from test_notification_receipt_retention import prepare,ctx,opened,claim,metadata,NOW,SID
from test_observer_notification_context import notifier
from test_observer_source_integration import snapshot,REASON,URI
from test_notification_sender_integration import busy,held_elsewhere
from test_mvp_service import FakeRegistry,FakeAdapter,FakeQueue


def scopes(name="new-observer"):
    subject,scope=observer_scope(name)
    return [(subject,scope,p) for p in ("slack","lark","smtp","windows_msg")]


def maintain(root,*,now=NOW+1,names=("new-observer",),budget=32):
    selected=[s for name in names for s in scopes(name)]
    return maintain_observer_receipts(root,scopes=selected,sender_fence=nullcontext,
                                      clock=lambda:now,budget=budget)


def test_actual_service_automatically_enrolls_new_observer_without_cold_reentry(tmp_path,monkeypatch):
    # Only the synthetic native subject was in the original verified cold plan.
    prepare(tmp_path)
    sender,calls,clock=notifier(tmp_path,prepare_finite=False)
    import codex_watchdog.notification_receipt_retention as module
    monkeypatch.setattr(module,"_legacy_rows",lambda *a,**k:pytest.fail("hot observer legacy import"))
    monkeypatch.setattr(module,"prepare_notification_receipts",lambda *a,**k:pytest.fail("hot cold activation"))
    registry=FakeRegistry([]);registry.last_snapshot=snapshot(NOW+1)
    service=MvpWatchdogService(tmp_path,registry=registry,notifier=sender,
        queue_dispatcher=FakeQueue(),git_adapter=FakeAdapter({}),codex_home=tmp_path/"home",
        clock=lambda:datetime.fromtimestamp(clock[0],timezone.utc))
    service.run_once()
    clock[0]=NOW+3;registry.last_snapshot=snapshot(clock[0],REASON)
    result=service.run_once()
    assert result.discovery["notifications"][0]["provider_outcomes"]=={"slack":"sent","lark":"sent"}
    assert len(calls)==2
    service.run_once();assert len(calls)==2
    subject,scope=observer_scope(service._observer_window_scope(URI))
    with sqlite3.connect(tmp_path/"notifications/dual-deliveries.sqlite3") as db:
        rows=db.execute("SELECT thread_id,scope,provider,floor FROM boundaries WHERE thread_id=?",(subject,)).fetchall()
    assert {(row[1],row[2],row[3]) for row in rows}=={(scope,"slack",NOW),(scope,"lark",NOW)}
    assert not (tmp_path/"slack/reply-tickets.sqlite3").exists()
    assert not (tmp_path/"lark/reply-tickets.sqlite3").exists()


def test_unprepared_service_maintenance_has_no_sql_or_legacy_conversion(tmp_path,monkeypatch):
    root=tmp_path/"notifications";root.mkdir()
    legacy=root/"dual-deliveries.json"
    original=json.dumps(dict(schema_version=1,events={str(i):{} for i in range(10000)})).encode()
    legacy.write_bytes(original)
    monkeypatch.setattr(sqlite3,"connect",lambda *a,**k:pytest.fail("unprepared observer opened SQL"))
    result=maintain(tmp_path)
    assert all(value["status"]=="refused" for value in result.values())
    assert legacy.read_bytes()==original
    assert not list(root.glob("*.sqlite3"))


def test_bad_dual_family_does_not_block_independently_prepared_fallback(tmp_path):
    prepare(tmp_path)
    path=tmp_path/"notifications/dual-deliveries.json";path.write_bytes(b"retained invalid marker")
    result=maintain(tmp_path)
    assert result["dual"]["status"]=="refused"
    subject,scope,_=scopes()[0]
    with sqlite3.connect(tmp_path/"notifications/fallback-deliveries.sqlite3") as db:
        assert db.execute("SELECT provider FROM boundaries WHERE thread_id=? AND scope=? ORDER BY provider",(subject,scope)).fetchall()==[("smtp",),("windows_msg",)]
    assert path.read_bytes()==b"retained invalid marker"


def test_observer_maintenance_real_common_fence_refuses_before_mutation(tmp_path):
    prepare(tmp_path)
    before={p.name:p.read_bytes() for p in (tmp_path/"notifications").glob("*.sqlite3")}
    with held_elsewhere(tmp_path):
        with pytest.raises(StoreBusyError):maintain(tmp_path)
    assert before=={p.name:p.read_bytes() for p in (tmp_path/"notifications").glob("*.sqlite3")}
    maintain(tmp_path)


def test_actual_authority_selection_and_physical_fences_span_observer_maintenance(tmp_path,monkeypatch):
    import codex_watchdog.notification_receipts as receipts
    original=tmp_path/"original";selected=tmp_path/"selected";receipt_root=tmp_path/"receipts"
    for root in (original,selected,receipt_root):root.mkdir()
    prepare(receipt_root)
    sender=EnvironmentNotifier(original,NotificationConfig(),clock=lambda:NOW+1)
    sender.relay_runtime=receipt_root
    class Authority:
        runtime=original
        @contextmanager
        def guard(self):
            self.runtime=selected
            yield
    sender.relay_authority=Authority()
    entry=receipts._finite_entry;phases=[]
    def entered(store,family,**kwargs):
        busy(selected);busy(receipt_root);phases.append(family)
        return entry(store,family,**kwargs)
    monkeypatch.setattr(receipts,"_finite_entry",entered)
    sender.maintain_observers({"fixture"})
    assert phases==["dual","fallback"]


def test_scope_retirement_and_clock_rollback_cannot_replay_expired_effect(tmp_path):
    prepare(tmp_path);maintain(tmp_path)
    from test_observer_notification_context import event
    from codex_watchdog.notification_context import receipt_context
    base=event(born=NOW+2)
    subject,scope=observer_scope(base.workspace_id)
    maintain(tmp_path,names=(base.workspace_id,))
    context=receipt_context(base,{"slack":dict(target_sha256="a"*64,payload_sha256="b"*64)})
    with opened(tmp_path,"slack",context,now=NOW+2) as store:
        assert store.claim(context["fingerprint"],"slack")
    maintain(tmp_path,now=NOW+finite.TTL+4,names=())
    with sqlite3.connect(tmp_path/"notifications/dual-deliveries.sqlite3") as db:
        assert db.execute("SELECT 1 FROM receipts WHERE fingerprint=?",(context["fingerprint"],)).fetchone() is None
        # Receipt retirement advances this existing boundary's checked/floor.
        # It ages out on a later bounded pass; collection is not user activity.
        assert db.execute("SELECT checked FROM boundaries WHERE thread_id=? AND scope=?",(subject,scope)).fetchone()==(NOW+finite.TTL+4,)
    maintain(tmp_path,now=NOW+2*finite.TTL+6,names=())
    with sqlite3.connect(tmp_path/"notifications/dual-deliveries.sqlite3") as db:
        assert db.execute("SELECT 1 FROM boundaries WHERE thread_id=? AND scope=?",(subject,scope)).fetchone() is None
    maintain(tmp_path,now=NOW+3,names=(base.workspace_id,))
    with opened(tmp_path,"slack",context,now=NOW+3) as store:
        with pytest.raises(finite.RetentionError,match="retired"):
            store.claim(context["fingerprint"],"slack")


def test_native_receipt_and_native_boundary_are_not_observer_maintenance_targets(tmp_path):
    prepare(tmp_path);context=ctx()
    with opened(tmp_path,"slack",context) as store:claim(store,"slack",context)
    before=metadata(tmp_path,"slack")
    with sqlite3.connect(tmp_path/"notifications/dual-deliveries.sqlite3") as db:
        boundary=db.execute("SELECT * FROM boundaries WHERE thread_id=?",(SID,)).fetchall()
    maintain(tmp_path,now=NOW+finite.TTL+5)
    assert metadata(tmp_path,"slack")==before
    with sqlite3.connect(tmp_path/"notifications/dual-deliveries.sqlite3") as db:
        assert db.execute("SELECT * FROM boundaries WHERE thread_id=?",(SID,)).fetchall()==boundary


def test_old_prepared_metadata_needs_explicit_cold_upgrade_while_native_continues(tmp_path):
    prepare(tmp_path)
    path=tmp_path/"notifications/dual-deliveries.sqlite3"
    with sqlite3.connect(path) as db:
        value=json.loads(db.execute("SELECT value FROM finite_metadata WHERE id=1").fetchone()[0])
        value.pop("observer_retention");value["future_setting"]={"preserve":True}
        db.execute("UPDATE finite_metadata SET value=? WHERE id=1",(json.dumps(value),))
        for index in ("observer_receipt_expiry","observer_boundary_expiry","retention_subject"):
            db.execute("DROP INDEX "+index)
    context=ctx("native-with-old-metadata")
    with opened(tmp_path,"slack",context) as store:assert claim(store,"slack",context)
    native_before=metadata(tmp_path,"slack")
    original=path.read_bytes()
    result=maintain(tmp_path)
    assert result["dual"]["status"]=="refused"
    assert path.read_bytes()==original and metadata(tmp_path,"slack")==native_before
    prepare(tmp_path)  # Real explicit preparation, original plan/floor, no reset.
    result=maintain(tmp_path)
    assert result["dual"]["status"]!="refused"
    assert metadata(tmp_path,"slack")==native_before
    with sqlite3.connect(path) as db:
        current=json.loads(db.execute("SELECT value FROM finite_metadata WHERE id=1").fetchone()[0])
        assert current["future_setting"]=={"preserve":True}
        assert current["observer_retention"]["floor"]==NOW


def _measure_history_work(tmp_path,history,kind):
    from codex_watchdog.observer_receipt_scopes import maintain_observer_scopes
    from codex_watchdog.notification_receipts import DualDeliveryReceipts,_finite_entry
    prepare(tmp_path)
    path=tmp_path/"notifications/dual-deliveries.sqlite3"
    with sqlite3.connect(path) as db:
        boundaries=[];receipts=[];retention=[]
        for i in range(history):
            key=hashlib.sha256((kind+str(i)).encode()).hexdigest()
            subject=SID if kind=="native" else "observer:"+key
            boundaries.append((subject,key,"slack",0,"",0))
            receipts.append((key,"uncertain",None))
            retention.append((key,"slack",subject,key,0,finite.TTL,0,"a"*64,"b"*64))
        db.executemany("INSERT INTO boundaries VALUES(?,?,?,?,?,?)",boundaries)
        db.executemany("INSERT INTO receipts VALUES(?,?,?)",receipts)
        db.executemany("INSERT INTO retention VALUES(?,?,?,?,?,?,?,?,?)",retention)
    statements=[];operations=[0]
    store=DualDeliveryReceipts(tmp_path,clock=lambda:NOW+finite.TTL+10,require_finite=True)
    try:
        _finite_entry(store,"dual",require_context=False)
        def progress():operations[0]+=1;return 0
        store.db.set_trace_callback(statements.append);store.db.set_progress_handler(progress,1)
        result=maintain_observer_scopes(store.finite,[scopes("current")[0]],budget=32)
        store.db.set_trace_callback(None);store.db.set_progress_handler(None,0)
        assert result["receipt_examined"]<=32 and result["boundary_examined"]<=32
        plans=[]
        for sql,args in (
            ("SELECT fingerprint,provider FROM retention INDEXED BY observer_receipt_expiry "
             "WHERE protected=0 AND thread_id GLOB 'observer:*' AND expires<=? "
             "ORDER BY expires,fingerprint,provider LIMIT 32",(NOW,)),
            ("SELECT thread_id,scope,provider FROM boundaries INDEXED BY observer_boundary_expiry "
             "WHERE thread_id GLOB 'observer:*' AND checked<=? "
             "ORDER BY checked,thread_id,scope,provider LIMIT 32",(NOW,)),
            ("SELECT 1 FROM retention INDEXED BY retention_subject "
             "WHERE thread_id=? AND scope=? AND provider=? LIMIT 1",tuple(scopes("current")[0]))):
            plan=store.db.execute("EXPLAIN QUERY PLAN "+sql,args).fetchall()
            assert all("SEARCH" in row[3] and "INDEX" in row[3] for row in plan)
            plans.append(plan)
        if kind=="native":
            assert result["receipt_removed"]==result["boundary_removed"]==0
            assert store.db.execute("SELECT count(*) FROM retention WHERE thread_id=?",(SID,)).fetchone()[0]==history
        return dict(history=history,kind=kind,sql=len(statements),vm=operations[0],result=result,plans=plans)
    finally:store.__exit__(None,None,None)


@pytest.mark.parametrize("kind",["native","observer"])
def test_actual_prepared_maintenance_has_bounded_history_work(tmp_path,kind,capsys):
    measured={}
    for history in (0,1000,10000,100000):
        root=tmp_path/str(history);root.mkdir()
        result=_measure_history_work(root,history,kind);measured[history]=result
        with capsys.disabled():print(json.dumps(dict(observer_scope_work=result)))
    histories=(0,1000,10000,100000) if kind=="native" else (1000,10000,100000)
    assert len({measured[n]["sql"] for n in histories})==1
    assert max(measured[n]["vm"] for n in histories)-min(measured[n]["vm"] for n in histories)<=256
