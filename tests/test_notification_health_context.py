"""Current health-source metadata only; providers and native guards are fakes."""
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from codex_watchdog import linux_health, linux_continuation, notification_receipt_retention as finite
from codex_watchdog.models import sha256_text
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.notification_context import source_time
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
from codex_watchdog.storage import InstructionStore

SID = "11111111-2222-4333-8444-555555555555"
START = 1792008000.0


def stamp(instant):
    return datetime.fromtimestamp(instant, timezone.utc).isoformat().replace("+00:00", "Z")


def remote_monitor(tmp_path, clock, monkeypatch, *, writer=InstructionStore._atomic_json):
    monitor = MvpWatchdogService.__new__(MvpWatchdogService)
    monitor.runtime, monitor.states, monitor.atomic_writer = tmp_path, tmp_path/"states", writer
    target = SimpleNamespace(workspace_id="fixture", label="Fixture", authority="ssh-remote+fixture-host",
        repo_path="/home/fixture/project", storage_key="a"*32)
    calls, events = [], []
    monkeypatch.setattr("codex_watchdog.mvp_service.utc_now", lambda: stamp(clock[0]))

    def post(token, method, payload, timeout):
        # The identity and original clock must already survive process loss.
        saved = json.loads(monitor.state_path("fixture").read_text())
        assert isinstance(saved["outage_id"], str)
        calls.append(payload)
        return dict(channel=payload["channel"], ts=str(int(clock[0]))+".000001")

    notifier = EnvironmentNotifier(tmp_path,
        NotificationConfig(slack_bot_token="xoxb-fixture", slack_channel_id="C12345678"),
        slack_api_post=post, clock=lambda: clock[0]+0.5)
    scopes = [(SID,sha256_text("fixture"),"slack")]
    finite.prepare_notification_receipts(tmp_path, scopes=scopes, contexts=(),
        sender_fence=nullcontext, clock=lambda: START)

    def notify(event):
        events.append(event)
        return notifier.notify(event).to_dict()

    monitor._safe_notify = notify
    return monitor, target, calls, events


def test_remote_loss_and_recovery_original_birth_survive_pre_finish_crashes(tmp_path, monkeypatch):
    clock = [START+1]
    monitor, target, calls, events = remote_monitor(tmp_path, clock, monkeypatch)
    state = dict(connection_status="connected", consecutive_failures=2, session_id=SID)
    monitor._record_remote_failure(target,state,"first-outage","remote_unavailable",[])
    assert len(calls) == 1 and events[-1].source_born == START+1
    assert events[-1].source_thread_id == SID
    # Crash after provider attempt and before the ordinary cycle's final save:
    # the pre-send source state and separate durable attempt claim are enough.
    state = json.loads(monitor.state_path("fixture").read_text())
    clock[0] = START+2
    monitor._record_remote_failure(target,state,"other-cycle","remote_unavailable",[])
    assert len(calls) == 1 and state["outage_id"] == "first-outage"
    assert events[-1].source_born == START+1
    clock[0] = START+3
    monitor._record_remote_success(target,state,[],SID)
    assert len(calls) == 2 and events[-1].source_born == START+3
    state = json.loads(monitor.state_path("fixture").read_text())
    clock[0] = START+4
    monitor._record_remote_success(target,state,[],SID)
    assert len(calls) == 2 and events[-1].source_born == START+3


def test_unknown_legacy_loss_birth_is_not_minted_but_fresh_recovery_progresses(tmp_path, monkeypatch):
    clock = [START+1]
    monitor, target, calls, events = remote_monitor(tmp_path, clock, monkeypatch)
    state = dict(connection_status="disconnected", consecutive_failures=10, session_id=SID,
        outage_id="legacy-outage", connection_alert_pending=True)
    monitor._record_remote_failure(target,state,"new-cycle","remote_unavailable",[])
    assert not calls and events[-1].source_born is None and "outage_born" not in state
    clock[0] = START+2
    monitor._record_remote_success(target,state,[],SID)
    assert len(calls) == 1 and events[-1].source_born == START+2


def test_failed_remote_source_publication_prevents_current_alert_attempt(tmp_path, monkeypatch):
    def broken(*args): raise OSError("fixture failure")
    clock = [START+1]
    monitor, target, calls, events = remote_monitor(tmp_path,clock,monkeypatch,writer=broken)
    state = dict(connection_status="connected", consecutive_failures=2, session_id=SID)
    monitor._record_remote_failure(target,state,"outage","remote_unavailable",[])
    assert not calls and not events


def test_linux_health_keeps_outage_birth_and_records_new_recovery_birth(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_health, "effect_guard", lambda *args: nullcontext())
    monkeypatch.setattr(linux_health, "current_control", lambda: None)
    clock = [START+1]
    monkeypatch.setattr(linux_health, "utc_now", lambda: stamp(clock[0]))
    workspace = SimpleNamespace(session_id=SID, workspace_id="fixture", repo_root=tmp_path/"project")
    events = []
    notifier = SimpleNamespace(notify=lambda event: events.append(event) or SimpleNamespace(
        to_dict=lambda: dict(status="delivery_failed")))
    health = linux_health.LinuxOwnerHealth(tmp_path, tmp_path/"home", workspace, notifier)
    health.report("fixture_owner_lost")
    assert events[-1].source_born == START+1 and events[-1].source_thread_id == SID
    clock[0] = START+2
    replacement = linux_health.LinuxOwnerHealth(tmp_path,tmp_path/"home",workspace,notifier)
    replacement.report("fixture_owner_lost")
    assert len(events) == 1
    clock[0] = START+3
    replacement.report()
    assert len(events) == 2 and events[-1].source_born == START+3
    saved = json.loads(replacement.path.read_text())
    assert source_time(saved["attention_born"]) == START+1
    assert source_time(saved["recovery_born"]) == START+3


def test_linux_legacy_pending_health_does_not_reconstruct_original_birth(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_health, "effect_guard", lambda *args: nullcontext())
    monkeypatch.setattr(linux_health, "current_control", lambda: None)
    workspace = SimpleNamespace(session_id=SID, workspace_id="fixture", repo_root=tmp_path/"project")
    events=[]
    notifier=SimpleNamespace(notify=lambda event: events.append(event) or SimpleNamespace(
        to_dict=lambda: dict(status="delivery_failed")))
    health=linux_health.LinuxOwnerHealth(tmp_path,tmp_path/"home",workspace,notifier)
    InstructionStore._atomic_json(health.path,dict(schema_version=1,identity=health.identity,
        health="lost",pending=True,outage_id="old-outage",unrelated_saved_choice="retained"))
    health.report("fixture_owner_lost")
    assert events[0].source_born is None
    saved=json.loads(health.path.read_text())
    assert "attention_born" not in saved and saved["unrelated_saved_choice"] == "retained"


def test_continuation_notice_uses_immutable_intent_not_current_retry_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_continuation,"effect_guard",lambda *args: nullcontext())
    monkeypatch.setattr(linux_continuation,"current_control",lambda: None)
    binding=SimpleNamespace(runtime=tmp_path,codex_home=tmp_path/"home",
        load=lambda: dict(state="armed",expires_at=START+1000))
    monkeypatch.setattr(linux_continuation.time,"time",lambda:START+100)
    workspace=SimpleNamespace(session_id=SID,workspace_id="fixture",repo_root=tmp_path/"project")
    events=[]
    continuation=linux_continuation.LinuxContinuation(binding,None,SimpleNamespace(
        notify=lambda event: events.append(event) or SimpleNamespace(to_dict=lambda:dict(status="uncertain"))))
    value=dict(instruction_id="fixture-intent",created_at=stamp(START+1),status="started")
    continuation._notify(value,workspace,"started")
    assert events[0].source_born == START+1 and events[0].source_thread_id == SID
    continuation._notify(value,workspace,"started")
    assert len(events) == 1


def test_continuation_unknown_intent_date_remains_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_continuation,"effect_guard",lambda *args:nullcontext())
    monkeypatch.setattr(linux_continuation,"current_control",lambda:None)
    binding=SimpleNamespace(runtime=tmp_path,codex_home=tmp_path/"home",
        load=lambda:dict(state="armed",expires_at=START+1000))
    monkeypatch.setattr(linux_continuation.time,"time",lambda:START+100)
    events=[]
    continuation=linux_continuation.LinuxContinuation(binding,None,SimpleNamespace(notify=lambda event:
        events.append(event) or SimpleNamespace(to_dict=lambda:dict(status="uncertain"))))
    workspace=SimpleNamespace(session_id=SID,workspace_id="fixture",repo_root=tmp_path)
    continuation._notify(dict(instruction_id="old-intent",status="uncertain"),workspace,"uncertain")
    assert events[0].source_born is None
