"""A verified new native subject must work after another subject's cold cutover.

This exercises the notifier's already trusted original-source input with a fake
provider; it is not live owner/native acceptance or permission to create a thread.
"""
from contextlib import nullcontext
from dataclasses import replace

from codex_watchdog.models import sha256_text
from codex_watchdog.notification_root_retention import prepare_notification_root
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
from test_notification_root_integration import NOW, SID, event


def test_new_verified_native_subject_after_cutover_can_notify_without_another_cold_scan(tmp_path):
    prepare_notification_root(tmp_path,scopes=[(SID,sha256_text("fixture"),"slack")],
        current_keys=[],current_pins=(),sender_fence=nullcontext,clock=lambda:NOW)
    calls=[]
    def post(token,method,payload,timeout):
        calls.append(payload);return dict(channel=payload["channel"],ts="1792008000.000001")
    notifier=EnvironmentNotifier(tmp_path,NotificationConfig(slack_bot_token="xoxb-fixture",slack_channel_id="C12345678"),
        slack_api_post=post,clock=lambda:NOW+1)
    fresh=replace(event(),workspace_id="new-current-workspace",source_thread_id="22222222-3333-4444-8555-666666666666")
    assert notifier.notify(fresh).status == "sent"
    assert len(calls) == 1
    assert notifier.notify(fresh).status == "suppressed" and len(calls) == 1
