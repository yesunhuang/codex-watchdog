"""Real first control/authority callers, without pre-provisioning notifications.

The existing node fixture models native ownership; no native/provider operation
is performed. All endpoints and state are disposable, with actual store locks.
"""
from dataclasses import replace
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from codex_watchdog.control_context import acting_as
from codex_watchdog.control_state import ControlBusy, ControlError, ControlStore, control_file_lock
from codex_watchdog.notification_attempts import local_attempt
from codex_watchdog.notification_state import CurrentNotificationState
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.relay_authority import bind_service_authority
from codex_watchdog.remote_control import notify_local_control
from codex_watchdog.storage import FileLock, StoreBusyError
from test_linux_node import THREAD, nodes
from test_notification_fresh_entry import assert_fresh_root, no_history, provider_state
from test_notification_native_enrollment import NOW, database, notifier, retained
from test_notification_sender_integration import busy
from test_relay_authority import _admit, _node


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("first-caller fixture attempted provider network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def event(index=1, *, workspace="control-fixture"):
    return NotificationEvent(workspace, "linux_owner_attention", "outage-" + str(index),
        "Owner unavailable", "Fixture alert", source_thread_id=THREAD,
        source_born=NOW - 1)


def local_control(base):
    repo = base / "repo"
    repo.mkdir(parents=True)
    store = ControlStore(base / "codex", THREAD, repo, boot_id="fixture", clock=lambda: NOW)
    token = store.attach("fixture-sender", "fixture-host", "vscode")
    return store, token, base / "runtime"


def local_notify(store, token, current, sender):
    # This is the actual current-control context used by Linux health callers.
    with acting_as(store, token):
        return notify_local_control(store, token, current, sender)


@pytest.mark.parametrize("provider", ["slack", "smtp"])
@pytest.mark.parametrize("lost_ack", [False, True])
def test_first_local_control_provisions_before_attempt_lease_and_stays_terminal(
        tmp_path, monkeypatch, provider, lost_ack):
    store, token, runtime = local_control(tmp_path)
    current, calls, attempted = event(), [], []
    clock = [NOW]

    def before_attempt():
        assert_fresh_root(runtime)
        attempt = local_attempt(runtime, store)
        intent = attempt.read()
        assert intent["phase"] == "active" and intent["fingerprint"] == current.event_fingerprint()
        effect = json.loads(store.effect_path("notification", current.event_fingerprint()).read_bytes())
        assert effect["state"] == "uncertain" and effect["operation_id"] == intent["operation_id"]
        assert store.read()["external_effect"]["id"] == intent["operation_id"]
        assert CurrentNotificationState(runtime).claim_time(
            current.dedupe_key(), current.event_fingerprint()) == NOW
        assert provider_state(runtime, current.event_fingerprint(), provider) == "uncertain"
        assert retained(runtime, current.event_fingerprint(), provider)[4] == NOW - 1
        # Both the outer attempt and current owner remain live kernel fences.
        for lease in (attempt.lease, lambda: attempt.effect_lease(current.event_fingerprint()),
                      lambda: control_file_lock(store.lock_path)):
            with pytest.raises(ControlBusy):
                with lease():
                    pytest.fail("provider attempt escaped a current owner/attempt fence")
        attempted.append(provider)
        if lost_ack:
            raise TimeoutError("disposable lost provider ACK")

    sender = notifier(runtime, provider, clock, calls, before_attempt)
    assert not (runtime / "notifications").exists()
    no_history(monkeypatch)
    result = local_notify(store, token, current, sender)
    assert result["status"] == ("delivery_failed" if lost_ack else "sent")
    assert result["terminal"] is True and attempted == [provider]
    assert calls == ([] if lost_ack else [provider])
    assert store.read()["external_effect"] is None
    attempt = local_attempt(runtime, store)
    saved = attempt.effect_path(current.event_fingerprint()).read_bytes()
    effect_path = store.effect_path("notification", current.event_fingerprint())
    effect = effect_path.read_bytes()
    assert json.loads(saved)["phase"] == "finalized"
    # Reconstruct actual store/notifier objects against the saved state.
    reopened = ControlStore(store.codex_home, THREAD, Path(store.repo_path),
                            boot_id="fixture", clock=lambda: NOW)
    repeated = local_notify(reopened, token, current, notifier(runtime, provider, clock, calls))
    assert repeated["duplicate"] and repeated["status"] == "suppressed"
    assert attempt.effect_path(current.event_fingerprint()).read_bytes() == saved
    assert effect_path.read_bytes() == effect
    assert attempted == [provider] and calls == ([] if lost_ack else [provider])
    healthy_provider = "smtp" if provider == "slack" else "slack"
    assert local_notify(reopened, token, event(2),
        notifier(runtime, healthy_provider, clock, calls))["status"] == "sent"
    assert calls == ([] if lost_ack else [provider]) + [healthy_provider]
    assert attempt.effect_path(current.event_fingerprint()).read_bytes() == saved
    assert effect_path.read_bytes() == effect and reopened.read()["external_effect"] is None


@pytest.mark.parametrize("damage", ["empty", "partial", "linked"])
def test_control_existing_or_partial_namespace_refuses_without_repair_and_other_root_progresses(
        tmp_path, damage):
    store, token, runtime = local_control(tmp_path)
    runtime.mkdir()
    directory = runtime / "notifications"
    sentinel = b"operator fixture data must remain unchanged"
    if damage == "linked":
        target = tmp_path / "redirect-target"
        target.mkdir()
        (target / "sentinel").write_bytes(sentinel)
        try:
            directory.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip("fixture platform cannot create directory symlink: " + str(exc))
    else:
        directory.mkdir()
        if damage == "partial":
            (directory / "finite-notification-root-intent.json").write_bytes(sentinel)
    calls = []
    try:
        result = local_notify(store, token, event(), notifier(runtime, "slack", [NOW], calls))
    except (ValueError, OSError):
        pass  # Existing strict path/pending-state errors may precede a result.
    else:
        assert result["status"] not in ("sent", "sent_fallback")
    assert calls == [] and not database(runtime, "slack").exists()
    if damage == "linked":
        assert set(target.iterdir()) == {target / "sentinel"}
        assert (target / "sentinel").read_bytes() == sentinel
    elif damage == "partial":
        assert (directory / "finite-notification-root-intent.json").read_bytes() == sentinel
    healthy = tmp_path / "independent-runtime"
    assert local_notify(store, token, event(2, workspace="independent"),
        notifier(healthy, "slack", [NOW], calls))["status"] == "sent"
    assert calls == ["slack"]


@pytest.mark.parametrize("lost_ack", [False, True])
def test_real_activated_authority_binds_first_absent_notification_leaf(nodes, lost_ack):
    authority, _ = _node(nodes)
    selected = authority.notification_runtime
    local_runtime = Path(authority.store.read()["runtime_path"])
    # activate must prepare only the supported fixed parents, not this leaf or
    # a fabricated notification directory. The first real notifier owns entry.
    assert not selected.exists() and not (selected / "notifications").exists()
    calls = []
    current = event(workspace=authority.workspace_id)

    def post(token, method, payload, timeout):
        assert_fresh_root(selected)
        assert getattr(authority._local, "depth", 0) > 0
        for runtime in (authority.runtime, selected):
            busy(runtime)
        for lock in (local_runtime / "locks/notifications.lock", selected / "locks/notifications.lock"):
            with pytest.raises(StoreBusyError):
                with FileLock(lock):
                    pytest.fail("authority or selected notification lock absent")
        with pytest.raises(ControlBusy):
            with control_file_lock(authority.store.lock_path):
                pytest.fail("native current owner fence absent")
        assert provider_state(selected, current.event_fingerprint(), "slack") == "uncertain"
        calls.append(method)
        if lost_ack:
            raise TimeoutError("disposable shared-authority lost ACK")
        return dict(channel=payload["channel"], ts="1792008000.000001")

    config = NotificationConfig(slack_bot_token="xoxb-fixture", slack_channel_id="C12345678")

    def bound():
        instance = EnvironmentNotifier(local_runtime, config, slack_api_post=post, clock=lambda: NOW)
        bind_service_authority(SimpleNamespace(notifier=instance), authority)
        assert instance.relay_runtime == selected and instance.runtime == local_runtime
        return instance

    with acting_as(authority.store, authority.token):
        result = bound().notify(current)
    assert result.status == ("delivery_failed" if lost_ack else "sent")
    assert calls == ["chat.postMessage"]
    ready = (selected / "notifications/finite-notification-root.json").read_bytes()
    with acting_as(authority.store, authority.token):
        assert bound().notify(current).status == "suppressed"
    assert calls == ["chat.postMessage"]
    assert (selected / "notifications/finite-notification-root.json").read_bytes() == ready
    assert not (local_runtime / "notifications").exists()


@pytest.mark.parametrize("damage", ["file", "linked"])
def test_real_authority_activation_refuses_unsafe_fixed_parent_without_touching_target(nodes, damage):
    authority, _ = _node(nodes, activate=False)
    home = nodes[0]
    authority.runtime.mkdir(parents=True, exist_ok=True)
    parent = authority.runtime / "session-notifications"
    sentinel = b"unrelated fixture state"
    if damage == "file":
        parent.write_bytes(sentinel)
    else:
        target = home / "unrelated-directory"
        target.mkdir()
        (target / "sentinel").write_bytes(sentinel)
        try:
            parent.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip("fixture platform cannot create directory symlink: " + str(exc))
    with pytest.raises((ControlError, OSError)):
        _admit(authority, home)
    assert not authority.notification_runtime.exists()
    if damage == "file":
        assert parent.read_bytes() == sentinel
    else:
        assert set(target.iterdir()) == {target / "sentinel"}
        assert (target / "sentinel").read_bytes() == sentinel


def measured_existing_control(base, history, monkeypatch):
    store, token, runtime = local_control(base)
    directory = runtime / "notifications"
    directory.mkdir(parents=True)
    legacy = directory / "dual-deliveries.json"
    raw = json.dumps(dict(schema_version=1, events={
        ("%064x" % index): dict(slack="uncertain") for index in range(history)})).encode()
    legacy.write_bytes(raw)
    calls, sql, opens, vm = [], [], [], [0]
    sender = notifier(runtime, "slack", [NOW], calls)
    connect, path_open = sqlite3.connect, Path.open

    def progress():
        vm[0] += 1
        return 0

    def measured_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(sql.append)
        db.set_progress_handler(progress, 1)
        return db

    def point_open(path, *args, **kwargs):
        assert path != legacy, "control entry read/imported legacy notification history"
        opens.append(path.name)
        return path_open(path, *args, **kwargs)

    def refuse_listing(*args, **kwargs):
        pytest.fail("first-control caller enumerated history")

    with monkeypatch.context() as patch:
        no_history(patch)
        patch.setattr(sqlite3, "connect", measured_connect)
        patch.setattr(Path, "open", point_open)
        for name in ("glob", "rglob", "iterdir"):
            patch.setattr(Path, name, refuse_listing)
        result = local_notify(store, token, event(), sender)
    assert result["status"] not in ("sent", "sent_fallback") and not calls
    assert legacy.read_bytes() == raw
    assert not (directory / "finite-notification-root.json").exists()
    return dict(sql=len(sql), vm=vm[0], point_opens=len(opens), legacy_reads=0)


@pytest.mark.parametrize("history", [0, 1000, 10000, 100000])
def test_control_existing_namespace_refusal_is_known_path_work(tmp_path, monkeypatch, capsys, history):
    small = measured_existing_control(tmp_path / "baseline", 0, monkeypatch)
    large = measured_existing_control(tmp_path / "selected", history, monkeypatch)
    assert large == small
    with capsys.disabled():
        print("C351_CONTROL_ENTRY_COUNTS", json.dumps(dict(
            history=history, baseline=small, selected=large), sort_keys=True))
