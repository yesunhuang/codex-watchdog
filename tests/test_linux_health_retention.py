"""Reused package-fixture notification state needs explicit cold preparation.

All state is disposable. The exclusion below is a real fixture foreground lock,
not a live process census or native-owner proof. No client or process is opened.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import socket
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from codex_watchdog import linux_health
from codex_watchdog.linux_health import LinuxOwnerHealth
from codex_watchdog.notification_cutover import notifier_cutover_projection
from codex_watchdog.notification_root_retention import READY, prepare_notification_root
from codex_watchdog.notification_state import CurrentNotificationState
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
from codex_watchdog.storage import FileLock, StoreBusyError
from test_notification_receipt_retention import NOW, SID
from test_notification_sender_integration import busy


def claims(runtime):
    with closing(sqlite3.connect(
            (runtime / 'notifications/recent-notification-claims.sqlite3').as_uri()
            + '?mode=ro', uri=True)) as db:
        return db.execute('SELECT scope,fingerprint,born,expires FROM claims '
                          'ORDER BY scope,fingerprint').fetchall()


def test_unconfigured_health_history_requires_cold_entry_before_later_loss_recovery(
        tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('health retention fixture attempted a process or network call')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    runtime, home, repo = (tmp_path / name for name in ('runtime', 'codex', 'repo'))
    for path in (runtime, home, repo):
        path.mkdir()
    workspace = SimpleNamespace(workspace_id='package-fixture', session_id=SID,
                                repo_root=repo)
    clock, sends = [NOW], []
    monkeypatch.setattr(linux_health, 'utc_now', lambda: datetime.fromtimestamp(
        clock[0], timezone.utc).isoformat().replace('+00:00', 'Z'))
    unconfigured = EnvironmentNotifier(runtime, NotificationConfig(), clock=lambda: clock[0])
    health = LinuxOwnerHealth(runtime, home, workspace, unconfigured)
    assert health.report('linux_conflicting_writer')['status'] == 'audit_only'
    assert health.report()['status'] == 'audit_only'
    original = claims(runtime)
    assert len(original) == 2 and all(row[2] == NOW for row in original)
    assert not (runtime / 'notifications' / READY).exists()

    def post(token, method, payload, timeout):
        busy(runtime)
        with pytest.raises(StoreBusyError):
            with FileLock(runtime / 'locks/notifications.lock'):
                pytest.fail('provider escaped actual notification lock')
        sends.append(payload['text'])
        return dict(channel=payload['channel'], ts='1792008000.000001')
    notifier = EnvironmentNotifier(runtime, NotificationConfig(
        slack_bot_token='xoxb-fixture', slack_channel_id='C12345678'),
        slack_api_post=post, clock=lambda: clock[0])
    health = LinuxOwnerHealth(runtime, home, workspace, notifier)
    clock[0] += 1
    refused = health.report('app_server_exited', recovering=True)
    assert refused['status'] == 'delivery_failed' and refused['attempted_channels'] == []
    assert health.report()['status'] == 'delivery_failed'
    before_cold = claims(runtime)
    assert len(before_cold) == 4 and set(original) <= set(before_cold) and sends == []
    assert not (runtime / 'notifications/dual-deliveries.sqlite3').exists()

    # This fixture owns every sender and has no provider attempt/current HTTP
    # operation. Its explicit exclusion never infers idle from queue absence.
    projection = notifier_cutover_projection(notifier, workspaces=[workspace],
        observer_workspace_ids=[], event_types=('linux_owner_attention', 'linux_owner_recovered'),
        current_contexts=[], current_http_pins=[])
    phases = []
    @contextmanager
    def excluded_synthetic_senders():
        with FileLock(runtime / 'locks/foreground-run.lock'):
            phases.append('enter')
            yield
            phases.append('exit')
    clock[0] += 1
    ready = prepare_notification_root(runtime, **projection['projection'],
        sender_fence=excluded_synthetic_senders, clock=lambda: clock[0])
    assert ready['status'] == 'ready' and ready['floor'] == clock[0]
    assert phases == ['enter', 'exit'] and claims(runtime) == before_cold and sends == []

    clock[0] += 1
    loss = health.report('app_server_exited', recovering=True)
    assert loss['status'] == 'sent' and len(sends) == 1
    assert 'retrying observation automatically' in sends[0]
    state = json.loads(health.path.read_bytes())
    attention_born = state['attention_born']
    assert health.report('app_server_exited', recovering=True) == loss
    reopened = LinuxOwnerHealth(runtime, home, workspace, notifier)
    assert reopened.report('app_server_exited', recovering=True) == loss and len(sends) == 1
    clock[0] += 1
    recovery = reopened.report()
    assert recovery['status'] == 'sent' and len(sends) == 2 and 'again.' in sends[1]
    assert reopened.report() is None and len(sends) == 2
    saved = json.loads(reopened.path.read_bytes())
    assert saved['attention_born'] == attention_born
    with closing(sqlite3.connect(runtime / 'notifications/dual-deliveries.sqlite3')) as db:
        for result, born in ((loss, NOW + 3), (recovery, NOW + 4)):
            assert db.execute('SELECT born FROM retention WHERE fingerprint=? AND provider=?',
                (result['event_fingerprint'], 'slack')).fetchone() == (born,)
            assert CurrentNotificationState(runtime).claim_time(
                next(row[0] for row in claims(runtime) if row[1] == result['event_fingerprint']),
                result['event_fingerprint']) == born
    assert set(before_cold) <= set(claims(runtime))
