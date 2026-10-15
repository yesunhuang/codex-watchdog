"""Activated finite roots never silently replace a missing family with legacy."""
from pathlib import Path
import sqlite3

from codex_watchdog.notifications import EnvironmentNotifier,NotificationConfig
from test_notification_root_integration import prepare,event,NOW


def test_missing_prepared_native_fallback_drops_only_that_effect_without_recreation(tmp_path):
    prepare(tmp_path)
    path=tmp_path/'notifications/fallback-deliveries.sqlite3';path.unlink()
    ready=(tmp_path/'notifications/finite-notification-root.json').read_bytes()
    calls=[]
    config=NotificationConfig(smtp_host='mail.example.test',smtp_port=587,
        smtp_sender='from@example.test',smtp_recipients=('to@example.test',))
    sender=EnvironmentNotifier(tmp_path,config,clock=lambda:NOW+1)
    sender._send_smtp=lambda current:calls.append(current.event_fingerprint())
    result=sender.notify(event())
    assert result.status=='delivery_failed' and result.attempted_channels==()
    assert calls==[] and not path.exists()
    assert (tmp_path/'notifications/finite-notification-root.json').read_bytes()==ready
    def post(token,method,payload,timeout):
        calls.append(payload);return dict(channel=payload['channel'],ts='1792008000.000001')
    healthy=EnvironmentNotifier(tmp_path,NotificationConfig(slack_bot_token='xoxb-fixture',
        slack_channel_id='C12345678'),slack_api_post=post,clock=lambda:NOW+2)
    assert healthy.notify(event(born=NOW+2,state='healthy-independent')).status=='sent'
    assert len(calls)==1 and not path.exists()


def test_unactivated_fallback_retains_fresh_legacy_compatibility(tmp_path):
    from codex_watchdog.notification_receipts import FallbackDeliveryReceipts
    with FallbackDeliveryReceipts(tmp_path) as receipts:
        assert receipts.finite is None
    with sqlite3.connect(tmp_path/'notifications/fallback-deliveries.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()==(1,)
