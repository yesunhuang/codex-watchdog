"""Notification-only cold composition through actual notifier/authority adapters."""
from contextlib import contextmanager, nullcontext
from dataclasses import replace
import json
import sqlite3

import pytest

from codex_watchdog import notification_root_retention as root
from codex_watchdog import relay_notification_migration as migration
from codex_watchdog.models import sha256_text
from codex_watchdog.notification_state import CurrentNotificationState
from codex_watchdog.notification_context import observer_scope
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from test_notification_receipt_retention import SID, NOW
from test_notification_sender_integration import busy


def event(*, born=NOW+1, state="new"):
    return NotificationEvent("fixture", "git_attention", state, "Subject", "Message",
                             source_thread_id=SID, source_born=born)


def prepare(path, **kwargs):
    return root.prepare_notification_root(path,
        scopes=[(SID, sha256_text("fixture"), p) for p in ("slack", "lark", "smtp", "windows_msg")],
        current_keys=[event().dedupe_key()], current_pins=kwargs.pop("current_pins",()),
        sender_fence=nullcontext, clock=lambda:NOW, **kwargs)


@pytest.mark.parametrize("portable", [False, True])
def test_actual_notifier_admits_later_source_without_reading_retired_history(tmp_path, monkeypatch, portable):
    selected = tmp_path/"selected"
    calls = []
    legacy_plan = None
    if portable:
        source = tmp_path/"source"
        source.mkdir()
        legacy_plan = migration.apply_notification_migration(selected, [source])["plan_sha256"]
    prepare(selected)
    def post(token, method, payload, timeout):
        busy(selected)
        calls.append(payload)
        return dict(channel=payload["channel"], ts="1792008000.000001")
    notifier = EnvironmentNotifier(selected if not portable else tmp_path/"controller",
        NotificationConfig(slack_bot_token="xoxb-fixture", slack_channel_id="C12345678"),
        slack_api_post=post, clock=lambda:NOW+1)
    if portable:
        notifier.relay_runtime = selected
        notifier.lock_path = selected/"locks/notifications.lock"
    monkeypatch.setattr(migration,"_history_lookup",lambda *a,**k:pytest.fail("hot legacy history"))
    monkeypatch.setattr(root,"prepare_notification_receipts",lambda *a,**k:pytest.fail("hot cold preparation"))
    monkeypatch.setattr(CurrentNotificationState,"import_current",lambda *a,**k:pytest.fail("hot suppression import"))
    for born in (None, NOW-1, NOW):
        result = notifier.notify(event(born=born, state=str(born)))
        assert result.status == "suppressed" and result.attempted_channels == ()
    assert calls == []
    assert notifier.notify(event()).status == "sent"
    assert len(calls) == 1
    assert notifier.notify(event()).status == "suppressed" and len(calls) == 1
    if portable:
        assert migration.validate_notification_migration(selected,legacy_plan)["history_retired"] is True
        with pytest.raises(migration.RelayNotificationMigrationError,match="plan_mismatch"):
            migration.validate_notification_migration(selected,"a"*64)
        assert migration.previous_notification(selected,event().event_fingerprint()) is True
        assert migration.previous_notification(selected,event().event_fingerprint(),born=NOW+1) is False
        (selected/"notifications/fallback-deliveries.sqlite3").unlink()
        # This broken optional family must not invalidate exact current control.
        assert migration.validate_notification_migration(selected,legacy_plan)["history_retired"] is True


def test_root_cutover_uses_caller_old_sender_exclusion_through_deletion(tmp_path,monkeypatch):
    selected=tmp_path/"root";selected.mkdir()
    migration.apply_notification_migration(selected,[])
    phases=[];active=[False]
    @contextmanager
    def old_sender_exclusion():
        active[0]=True
        phases.append("enter")
        try:yield
        finally:
            active[0]=False
            phases.append("exit")
    real_prepare=root.prepare_notification_receipts
    def checked_prepare(*args,**kwargs):
        assert active[0]
        busy(selected);phases.append("prepare")
        return real_prepare(*args,**kwargs)
    monkeypatch.setattr(root,"prepare_notification_receipts",checked_prepare)
    unlink=root.Path.unlink
    def checked_unlink(path,*args,**kwargs):
        if path.name in root.RETIRED:
            assert active[0]
            busy(selected)
            assert root.validate_notification_root(selected) is not None
            phases.append("retire:"+path.name)
        return unlink(path,*args,**kwargs)
    monkeypatch.setattr(root.Path,"unlink",checked_unlink)
    result=root.prepare_notification_root(selected,
        scopes=[(SID,sha256_text("fixture"),"slack")],current_keys=[],
        current_pins=(),sender_fence=old_sender_exclusion,clock=lambda:NOW)
    assert result["status"]=="ready"
    assert phases==["enter","prepare",*["retire:"+name for name in root.RETIRED],"exit"]


def test_corrupt_root_loses_only_this_notifiers_effect_other_root_can_progress(tmp_path):
    for name in ("bad","healthy"):prepare(tmp_path/name)
    (tmp_path/"bad"/"notifications"/root.READY).write_bytes(b"{broken")
    calls=[]
    def post(token,method,payload,timeout):
        calls.append(payload);return dict(channel=payload["channel"],ts="1792008000.000001")
    config=NotificationConfig(slack_bot_token="xoxb-fixture",slack_channel_id="C12345678")
    bad=EnvironmentNotifier(tmp_path/"bad",config,slack_api_post=post,clock=lambda:NOW+1)
    healthy=EnvironmentNotifier(tmp_path/"healthy",config,slack_api_post=post,clock=lambda:NOW+1)
    assert bad.notify(event()).status=="state_error" and calls==[]
    assert healthy.notify(event()).status=="sent" and len(calls)==1


def test_explicit_notifier_cold_entry_selects_authority_before_both_real_sender_fences(tmp_path):
    original=tmp_path/"cached";selected=tmp_path/"selected";common=tmp_path/"authority"
    phases=[]
    class Authority:
        runtime=original
        notification_runtime=original
        @contextmanager
        def guard(self):
            phases.append("authority")
            self.runtime=common
            self.notification_runtime=selected
            yield
    @contextmanager
    def old_sender_exclusion():
        # Fixture protocol evidence, not actual live old-binary proof.
        busy(common)
        phases.append("old_exclusion")
        yield
    notifier=EnvironmentNotifier(tmp_path/"constructor",NotificationConfig(),clock=lambda:NOW)
    notifier.relay_authority=Authority()
    notifier.relay_runtime=original
    subject,scope=observer_scope("current-observer")
    result=notifier.prepare_retention(scopes=[(subject,scope,"slack")],current_keys=[],
        contexts=[],current_pins=(),old_sender_fence=old_sender_exclusion)
    assert result["status"]=="ready" and phases==["authority","old_exclusion"]
    assert not original.exists()
    assert root.validate_notification_root(selected) is not None
