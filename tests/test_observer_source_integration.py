"""Actual observer producers; clocks are evidence, never native identities."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import os
from pathlib import Path

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.notification_context import observer_scope
from codex_watchdog.notification_receipt_retention import prepare_notification_receipts, TTL
from codex_watchdog.storage import InstructionStore
from test_observer_notification_context import notifier, NOW
from test_mvp_service import FakeRegistry, FakeAdapter, FakeNotifier, FakeQueue

URI = "file:///fixture-workspace"
REASON = "vscode_thread_owner_unverified"


def snapshot(at=NOW+1, reason=None, *, unavailable=False, status=None, session=None):
    windows = () if unavailable else (SimpleNamespace(
        workspace_uri=URI, locality="process_local", reason=reason,
        tracking_status="unresolved" if reason else "tracked",
        session_id=session, repo_root=None, workspace_id="native-label-only"),)
    return SimpleNamespace(discovered_at=at, status=status or ("partial" if reason or unavailable else "ok"),
        windows=windows, issues=("vscode_live_status_unavailable",) if unavailable else (),
        effective_workspaces=())


def service(root, send=None, writer=InstructionStore._atomic_json):
    result=MvpWatchdogService.__new__(MvpWatchdogService)
    result.runtime=root; result.atomic_writer=writer
    events=[]
    def capture(event):
        events.append(event)
        if event.source_born is not None:
            # The producer's point is durable BEFORE entering the notifier.
            path=root/"service/observer-sources"/(sha256_text(event.workspace_id)+".json")
            saved=json.loads(path.read_text())
            assert saved["points"]["discovery_attention"]["born"]==event.source_born
        return send(event) if send else {"event_type":event.event_type}
    result._safe_notify=capture
    return result,events


@pytest.mark.parametrize("global_failure",[False,True])
def test_first_unknown_then_real_transition_restart_and_recurrence(tmp_path,global_failure):
    monitor,events=service(tmp_path)
    failure=lambda at:snapshot(at,REASON,unavailable=global_failure)
    monitor._discovery_attention_notifications(failure(NOW+1))
    initial=events[-1]
    assert initial.source_observer and initial.source_born is None
    assert initial.relay_target is None and initial.source_thread_id is None
    monitor._discovery_attention_notifications(failure(NOW+2))
    assert events[-1].event_fingerprint()==initial.event_fingerprint()
    monitor._discovery_attention_notifications(snapshot(NOW+3))
    monitor._discovery_attention_notifications(failure(NOW+4))
    current=events[-1]
    assert current.source_born==NOW+4
    assert current.event_fingerprint()!=initial.event_fingerprint()
    restarted,restart_events=service(tmp_path)
    restarted._discovery_attention_notifications(failure(NOW+TTL+5))
    assert restart_events[-1].source_born==NOW+4
    assert restart_events[-1].event_fingerprint()==current.event_fingerprint()
    restarted._discovery_attention_notifications(snapshot(NOW+TTL+6))
    restarted._discovery_attention_notifications(failure(NOW+TTL+7))
    assert restart_events[-1].source_born==NOW+TTL+7
    assert restart_events[-1].event_fingerprint()!=current.event_fingerprint()


def test_actual_dual_delivery_prepared_current_scope_no_retry_or_native_parent(tmp_path,monkeypatch):
    sender,calls,clock=notifier(tmp_path,prepare_finite=False)
    scopes=[(subject,scope,p) for name in ("vscode-discovery",MvpWatchdogService._observer_window_scope(URI))
        for subject,scope in [observer_scope(name)] for p in ("slack","lark","smtp","windows_msg")]
    prepare_notification_receipts(tmp_path,scopes=scopes,contexts=(),sender_fence=nullcontext,clock=lambda:NOW)
    send=lambda event:sender.notify(event).to_dict()
    monitor,events=service(tmp_path,send)
    monitor._discovery_attention_notifications(snapshot(NOW+1))
    clock[0]=NOW+2
    result=monitor._discovery_attention_notifications(snapshot(NOW+2,REASON))
    assert result[0]["provider_outcomes"]=={"slack":"sent","lark":"sent"}
    assert len(calls)==2
    clock[0]=NOW+TTL+3
    restarted,_=service(tmp_path,send)
    result=restarted._discovery_attention_notifications(snapshot(clock[0],REASON))
    assert result[0]["duplicate"] and len(calls)==2
    restarted._discovery_attention_notifications(snapshot(clock[0]+1))
    clock[0]+=2
    result=restarted._discovery_attention_notifications(snapshot(clock[0],REASON))
    assert result[0]["provider_outcomes"]=={"slack":"sent","lark":"sent"}
    assert len(calls)==4
    assert not (tmp_path/"slack/reply-tickets.sqlite3").exists()
    assert not (tmp_path/"lark/reply-tickets.sqlite3").exists()


@pytest.mark.parametrize("damage",["missing-time","backwards","write","unresolved"])
def test_unknown_current_evidence_cannot_mint_a_birth(tmp_path,damage):
    monitor,events=service(tmp_path)
    monitor._discovery_attention_notifications(snapshot(NOW+2))
    observed=snapshot(NOW+3,REASON)
    if damage=="missing-time":del observed.discovered_at
    elif damage=="backwards":observed.discovered_at=NOW+1
    elif damage=="write":
        def fail(*args):raise OSError("synthetic publication failure")
        monitor._observer_source_store()._writer=fail
    else:
        observed.windows=(*observed.windows, snapshot(NOW+3,"unrelated_unresolved").windows[0])
    monitor._discovery_attention_notifications(observed)
    assert events[-1].source_born is None and events[-1].source_observer


def test_unavailable_discovery_is_not_a_healthy_window_reset_and_duplicate_views_are_once(tmp_path):
    monitor,events=service(tmp_path)
    monitor._discovery_attention_notifications(snapshot(NOW+1,REASON))
    failed=snapshot(NOW+2,REASON,unavailable=True)
    failed.windows=snapshot(NOW+2,REASON).windows
    monitor._discovery_attention_notifications(failed)
    monitor._discovery_attention_notifications(snapshot(NOW+3,REASON))
    assert events[-1].source_born is None
    healthy=snapshot(NOW+4)
    monitor._discovery_attention_notifications(healthy)
    duplicate=snapshot(NOW+5,REASON)
    duplicate.windows=duplicate.windows*2
    result=monitor._discovery_attention_notifications(duplicate)
    assert len(result)==1 and events[-1].source_born==NOW+5
    assert "paused" in events[-1].message


def test_current_scope_cleanup_receives_unbound_views_and_resume_has_one_stable_scope(tmp_path):
    registry=FakeRegistry([])
    registry.last_snapshot=snapshot(NOW+1,REASON)
    notifier=FakeNotifier(tmp_path)
    maintained=[]
    notifier.maintenance=lambda *,current_scopes:maintained.append(set(current_scopes))
    clock=[NOW+1]
    monitor=MvpWatchdogService(tmp_path,registry=registry,git_adapter=FakeAdapter({}),notifier=notifier,
        queue_dispatcher=FakeQueue(),codex_home=tmp_path/"home",
        clock=lambda:datetime.fromtimestamp(clock[0],timezone.utc))
    monitor.run_once()
    assert maintained==[{"vscode-discovery",monitor._observer_window_scope(URI),"resume-prompt"}]
    assert monitor._resume_observer_point["born"] is None
    (tmp_path/"resume_prompt.md").write_text("fixture")
    clock[0]+=1
    monitor.run_once()
    assert monitor._resume_observer_point["born"]==NOW+2
    monitor._notify_resume_ambiguity(SimpleNamespace(workspace_id="first"))
    monitor._notify_resume_ambiguity(SimpleNamespace(workspace_id="second"))
    first,second=notifier.events[-2:]
    assert first.workspace_id==second.workspace_id=="resume-prompt"
    assert first.event_fingerprint()==second.event_fingerprint()
    assert first.source_born==second.source_born==NOW+2
    assert first.source_observer and first.source_thread_id is None


def test_healthy_error_snapshot_cannot_turn_an_old_global_failure_into_new_event(tmp_path):
    monitor,events=service(tmp_path)
    monitor._discovery_attention_notifications(snapshot(NOW+1,unavailable=True))
    monitor._discovery_attention_notifications(snapshot(NOW+2,status="error"))
    monitor._discovery_attention_notifications(snapshot(NOW+3,unavailable=True))
    assert events[-1].source_born is None


@pytest.mark.parametrize("history",[0,1000,10000])
def test_observer_point_work_is_history_independent_and_cleanup_budgeted(tmp_path,monkeypatch,history):
    from codex_watchdog.observer_notification_sources import ObserverNotificationSources
    store=ObserverNotificationSources(tmp_path)
    store.observe("current","discovery_attention",sha256_text("healthy"),100)
    directory=tmp_path/"service/observer-sources"
    original=json.loads((directory/(sha256_text("current")+".json")).read_text())
    for i in range(history):
        key=sha256_text("history"+str(i))
        value=dict(original,scope_sha256=key)
        (directory/(key+".json")).write_text(json.dumps(value))
    read_sizes=[];opened=[];state_fds=set()
    raw_open,raw_read,raw_close=os.open,os.read,os.close
    def opening(path,*args,**kwargs):
        descriptor=raw_open(path,*args,**kwargs)
        candidate=Path(path)
        if candidate.parent==directory and candidate.suffix==".json":
            opened.append(str(candidate));state_fds.add(descriptor)
        return descriptor
    def reading(descriptor,size):
        if descriptor in state_fds:
            assert 0<=size<=8193, "source read must be capped before JSON decode"
        raw=raw_read(descriptor,size)
        if descriptor in state_fds:read_sizes.append(len(raw))
        return raw
    def closing(descriptor):
        state_fds.discard(descriptor);return raw_close(descriptor)
    monkeypatch.setattr(os,"open",opening)
    monkeypatch.setattr(os,"read",reading)
    monkeypatch.setattr(os,"close",closing)
    raw_scandir=os.scandir
    def forbidden(*args,**kwargs):pytest.fail("ordinary observer point enumerated historical scopes")
    monkeypatch.setattr(os,"scandir",forbidden)
    point=store.observe("current","discovery_attention",sha256_text("failure"),101)
    assert point["born"]==101 and len(opened)==1
    ordinary=list(read_sizes);read_sizes.clear();opened.clear()
    monkeypatch.setattr(os,"scandir",raw_scandir)
    result=store.cleanup(TTL+120,budget=16)
    assert result["visited"]<=16 and len(opened)<=16
    print(json.dumps(dict(observer_sources=dict(history=history,
        point_json_reads=1,point_json_bytes=sum(ordinary),
        cleanup=result,cleanup_json_reads=len(opened),cleanup_json_bytes=sum(read_sizes)))))
