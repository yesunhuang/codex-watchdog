import json
from types import SimpleNamespace

import pytest

from codex_watchdog.notifications import NotificationConfig, EnvironmentNotifier, NotificationEvent
from codex_watchdog.slack_mapping import SlackRelayTarget, SlackThreadStore
from codex_watchdog.slack_poll import SlackPollingThreadStore, SlackReplyPoller
from codex_watchdog.slack_relay import SlackReplyRelay
from codex_watchdog.storage import FileLock, StoreBusyError


THREAD = "11111111-2222-4333-8444-555555555555"
PARENT = "1789111611.674949"
REPLY = "1789112244.814879"
CHANNEL = "C12345678"
USER = "U12345678"


def config(**changes):
    return NotificationConfig(**dict(dict(slack_bot_token="xoxb-test", slack_channel_id=CHANNEL,
        slack_allowed_user_ids=(USER,), slack_reply_mode="poll"), **changes))


def event(**changes):
    return dict(dict(type="message", ts=REPLY, thread_ts=PARENT, user=USER,
        client_msg_id="92b14040-a1f0-4e1f-9508-f6e316883213", text="run five minutes, then say yes"), **changes)


def fixture(tmp_path, *, status="enqueued"):
    deliveries = []
    def dispatch(*args):
        deliveries.append(args)
        return SimpleNamespace(status=status)
    relay = SlackReplyRelay.from_notification_config(tmp_path, config(),
        queue_dispatcher=SimpleNamespace(dispatch=dispatch), remote_ssh_adapter=None)
    target = SlackRelayTarget("workspace", THREAD, "process_local")
    relay.thread_store.record_thread(CHANNEL, PARENT, target, "a" * 64)
    return relay, deliveries, target


def test_bot_only_install_is_not_a_working_reply_install_and_poll_requires_allowlist():
    outgoing = config(slack_allowed_user_ids=(), slack_reply_mode="socket")
    assert outgoing.slack_post_configured and not outgoing.slack_relay_configured
    assert config().slack_relay_configured
    assert not config(slack_allowed_user_ids=()).slack_relay_configured
    assert not config(slack_allowed_user_ids=()).slack_post_configured
    assert not config(slack_allowed_user_ids=("not-a-user",)).slack_relay_configured
    parsed = NotificationConfig.from_environment(dict(CODEX_WATCHDOG_SLACK_REPLY_MODE="poll",
        CODEX_WATCHDOG_SLACK_BOT_TOKEN="xoxb-test", CODEX_WATCHDOG_SLACK_CHANNEL_ID=CHANNEL,
        CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS=USER))
    assert parsed.slack_relay_configured and parsed.slack_app_token is None
    with pytest.raises(ValueError, match="reply mode"):
        config(slack_reply_mode="typo")


def test_poll_mapping_is_saved_at_send_and_is_not_exported_to_old_socket_listeners(tmp_path):
    relay, deliveries, target = fixture(tmp_path)
    notifier = EnvironmentNotifier(tmp_path, config=config(), slack_api_post=lambda *args:
        dict(ok=True, channel=CHANNEL, ts=PARENT))
    notice = NotificationEvent("workspace", "stopped", "fingerprint", "Stopped", "yes", relay_target=target)
    notifier.notify(notice)
    assert isinstance(notifier.slack_thread_store, SlackPollingThreadStore)
    assert relay.thread_store.lookup_thread(CHANNEL, PARENT).target == target
    assert notifier.slack_thread_store.notification_mappings(notice.event_fingerprint()) == []
    assert relay.thread_store.mappings_for_threads((THREAD,)) == []
    assert SlackThreadStore(tmp_path).lookup_thread(CHANNEL, PARENT) is None


def test_old_socket_mappings_and_receipts_are_preserved_and_never_imported(tmp_path):
    relay, _, target = fixture(tmp_path)
    old = SlackThreadStore(tmp_path)
    old.record_thread(CHANNEL, "1789111600.000001", target, "b" * 64)
    before = old.path.read_bytes()
    relay.thread_store.cache_mappings(old.mappings_for_threads((THREAD,)), target)
    assert old.path.read_bytes() == before
    assert relay.thread_store.lookup_thread(CHANNEL, "1789111600.000001") is None


def test_reply_arriving_while_listener_offline_is_delivered_once_across_restart(tmp_path):
    relay, deliveries, _ = fixture(tmp_path)
    calls = []
    def api(method, params):
        calls.append((method, params))
        return dict(messages=[dict(type="message", ts=PARENT), event()]) if method == "conversations.replies" else dict(ok=True)
    poller = SlackReplyPoller(relay, api=api)
    assert poller.poll_once()[0]["status"] == "queued"
    assert len(deliveries) == 1 and deliveries[0][0] == THREAD
    assert deliveries[0][2] == event()["text"]
    assert SlackReplyPoller(relay, api=api).poll_once() == []
    assert len(deliveries) == 1
    assert calls[-1][1]["oldest"] == REPLY
    assert "run five minutes" not in poller.path.read_text()


@pytest.mark.parametrize("changes", [dict(user="U99999999"), dict(bot_id="B12345678"), dict(subtype="message_changed")])
def test_unauthorized_and_bot_replies_do_not_dispatch(tmp_path, changes):
    relay, deliveries, _ = fixture(tmp_path)
    poller = SlackReplyPoller(relay, api=lambda *args: dict(messages=[event(**changes)]))
    poller.poll_once()
    assert deliveries == []


def test_wrong_parent_fails_before_any_reply_is_delivered(tmp_path):
    relay, deliveries, _ = fixture(tmp_path)
    poller = SlackReplyPoller(relay, api=lambda *args:
        dict(messages=[event(), event(ts="1789112245.000001", thread_ts="1789111000.000001")]))
    with pytest.raises(ValueError, match="identity"):
        poller.poll_once()
    assert deliveries == [] and not poller.path.exists()


def test_uncertain_delivery_is_not_replayed_when_cursor_write_failed(tmp_path):
    relay, deliveries, _ = fixture(tmp_path, status="uncertain")
    poller = SlackReplyPoller(relay, api=lambda *args: dict(messages=[event()]))
    assert poller.poll_once()[0]["status"] == "uncertain"
    poller.path.unlink()  # Simulate cursor loss after the durable delivery receipt.
    assert poller.poll_once()[0]["status"] == "duplicate"
    assert len(deliveries) == 1


def test_deferral_does_not_skip_reply_and_pagination_uses_durable_oldest(tmp_path, monkeypatch):
    from codex_watchdog.slack_relay import SlackReplyResult as ReplyResult
    relay, deliveries, _ = fixture(tmp_path)
    handler = relay.handle_message
    monkeypatch.setattr(relay, "handle_message", lambda *args: ReplyResult("deferred"))
    poller = SlackReplyPoller(relay, api=lambda *args: dict(messages=[event()], has_more=True))
    poller.poll_once()
    assert json.loads(poller.path.read_text())["threads"] == {}
    monkeypatch.setattr(relay, "handle_message", handler)
    poller.poll_once()
    assert list(json.loads(poller.path.read_text())["threads"].values()) == [REPLY]
    assert len(deliveries) == 1


def test_shared_app_polling_delivers_own_reply_when_other_machine_receives_socket_event(tmp_path):
    mac, deliveries, _ = fixture(tmp_path / "mac")
    windows_deliveries = []
    windows = SlackReplyRelay.from_notification_config(tmp_path / "windows",
        config(slack_reply_mode="socket", slack_app_token="xapp-fixture"),
        queue_dispatcher=SimpleNamespace(dispatch=lambda *args: windows_deliveries.append(args)),
        remote_ssh_adapter=None)
    # Slack can give the Mac reply to Windows, where the exact local mapping is absent.
    assert windows.handle_message(dict(event(), channel=CHANNEL), event_id="EvSharedApp").status == "ignored_unknown_thread"
    assert windows_deliveries == []
    # Polling reads the Mac's own parent without relying on which socket got the event.
    poller = SlackReplyPoller(mac, api=lambda method, params:
        dict(messages=[event()]) if method == "conversations.replies" else dict(ok=True))
    assert poller.poll_once()[0]["status"] == "queued"
    assert poller.poll_once() == []
    assert len(deliveries) == 1 and deliveries[0][0] == THREAD
    assert not SlackThreadStore(tmp_path / "windows").has_notification_mapping("a" * 64)


def test_single_listener_lock_and_close(tmp_path):
    relay, _, _ = fixture(tmp_path)
    with FileLock(tmp_path / "locks/slack-poll-listener.lock"):
        with pytest.raises(StoreBusyError):
            relay.start()
    assert relay._poller is None


def test_concurrent_observation_and_reply_keep_separate_host_results(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from codex_watchdog.linux_auto import HostRemoteAdapter
    from codex_watchdog.remote_ssh import RemoteSshTarget
    adapter = HostRemoteAdapter(tmp_path)
    def run(request):
        time.sleep(0.005)
        adapter.namespace["emit"](dict(repo=request["repo_path"]))
    adapter.namespace["run"] = run
    targets = [RemoteSshTarget("ssh-remote+localhost", "/repo" + str(i), "a" * 32) for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(adapter.probe, targets))
    assert [result["repo"] for result in results] == [target.repo_path for target in targets]
