"""Integration tests for exact session-scoped Slack routing."""
from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.notifications import NotificationConfig, NotificationEvent, EnvironmentNotifier
from codex_watchdog.relay import RelayTarget
from codex_watchdog.session_routes import SessionRoutes
from codex_watchdog.slack_mapping import SlackThreadStore
from codex_watchdog.slack_relay import SlackReplyRelay

# ── Shared constants ─────────────────────────────────────────────────────────

DEFAULT_CHANNEL = "C00000001"
BOUND_CHANNEL = "C99999999"
BOUND_CHANNEL2 = "C88888888"
USER = "U12345678"
BOT_TOKEN = "xoxb-test-token"
APP_TOKEN = "xapp-test-token"


def _ts(n: int) -> str:
    return "1789111600.{:06d}".format(n)


def _target(n: int) -> RelayTarget:
    return RelayTarget(
        workspace_id="ws-" + str(n),
        thread_id="11111111-2222-4333-8444-{:012d}".format(n),
        execution_locality="process_local",
    )


def _record(store: SlackThreadStore, n: int, channel: str = DEFAULT_CHANNEL) -> str:
    fp = sha256_text("notice:" + str(n))
    store.record_thread(channel, _ts(n), _target(n), fp)
    return _target(n).thread_id


def _make_event(channel: str, thread_ts: str, ts: str, text: str, user: str = USER) -> dict:
    return dict(type="message", channel=channel, thread_ts=thread_ts, ts=ts, text=text, user=user)


def _make_relay(tmp_path: Path, *, route_api=None, slack_api_post=None) -> SlackReplyRelay:
    from unittest.mock import MagicMock
    store = SlackThreadStore(tmp_path)
    qd = MagicMock()
    qd.codex_home = None
    qd.dispatch.return_value = SimpleNamespace(status="enqueued")
    ssh = MagicMock()
    ssh.supports_control = False
    relay = SlackReplyRelay(
        tmp_path,
        bot_token=BOT_TOKEN,
        app_token=APP_TOKEN,
        channel_id=DEFAULT_CHANNEL,
        allowed_user_ids=(USER,),
        queue_dispatcher=qd,
        remote_ssh_adapter=ssh,
        thread_store=store,
        route_api=route_api,
    )
    return relay


def _info_api(channel_id: str) -> Any:
    def api(method: str, params: dict) -> dict:
        assert method == "conversations.info"
        return {"ok": True, "channel": {
            "id": channel_id, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    return api


# ── Relay: bind command ───────────────────────────────────────────────────────

def test_relay_bind_returns_route_bound(tmp_path):
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)

    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|general>")
    result = relay.handle_message(event)

    assert result.status == "route_bound"
    assert result.delivery_status == BOUND_CHANNEL
    assert result.duplicate is False


def test_relay_unbind_returns_route_unbound(tmp_path):
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)

    bind_ts = _ts(1) + "1"
    relay.handle_message(_make_event(DEFAULT_CHANNEL, _ts(1), bind_ts, f"bind <#{BOUND_CHANNEL}|general>"))

    unbind_ts = _ts(1) + "2"
    result = relay.handle_message(_make_event(DEFAULT_CHANNEL, _ts(1), unbind_ts, "unbind"))

    assert result.status == "route_unbound"
    assert result.duplicate is False


# ── Relay: bind does not wake Codex ──────────────────────────────────────────

def test_relay_bind_does_not_dispatch_to_codex(tmp_path):
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)

    relay.handle_message(
        _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|general>"))
    relay.queue_dispatcher.dispatch.assert_not_called()


# ── Relay: malformed command rejected without wake ────────────────────────────

def test_relay_malformed_bind_rejected_visibly(tmp_path):
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    result = relay.handle_message(
        _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", "bind"))
    assert result.status == "rejected_route_command"
    relay.queue_dispatcher.dispatch.assert_not_called()


def test_relay_malformed_bind_does_not_close_ticket(tmp_path):
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    relay.handle_message(_make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", "bind"))
    journal = relay.thread_store.journal
    with journal.transaction() as db:
        row = db.execute(
            "SELECT active FROM records WHERE kind='threads' AND key=?",
            (SlackThreadStore.thread_key(DEFAULT_CHANNEL, _ts(1)),),
        ).fetchone()
    assert row is not None and row[0] == 1


# ── Relay: acknowledgment at most once ───────────────────────────────────────

def test_relay_bind_ack_sent_exactly_once(tmp_path):
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)

    ts_val = "1789500000.000001"
    msgs: list = []
    def _client(**kw):
        msgs.append(kw)
        return {"ok": True, "channel": kw["channel"], "ts": ts_val}
    client = SimpleNamespace(chat_postMessage=_client)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|general>")

    r1 = relay.handle_message(event)
    relay.acknowledge(event, r1, client)
    # Two messages now: (1) hello top-level in destination, (2) source thread ack
    assert len(msgs) == 2
    hello_msg = msgs[0]
    source_ack = msgs[1]
    assert hello_msg["channel"] == BOUND_CHANNEL
    assert "thread_ts" not in hello_msg or hello_msg["thread_ts"] is None  # no thread_ts on hello
    assert BOUND_CHANNEL in source_ack["text"]

    relay.acknowledge(event, r1, client)
    assert len(msgs) == 2  # Reusing a result must not bypass the durable ack fence.

    r2 = relay.handle_message(event)
    relay.acknowledge(event, r2, client)
    assert r2.status == "duplicate"
    assert len(msgs) == 2  # no second ack


# ── Relay: duplicate bind ─────────────────────────────────────────────────────

def test_relay_duplicate_bind_no_reapplication(tmp_path):
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)

    e = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|general>")
    relay.handle_message(e)

    routes = SessionRoutes(relay.thread_store, provider="slack", scope=DEFAULT_CHANNEL)
    tid = _target(1).thread_id
    relay.handle_message(e)
    assert routes.destination(tid) == BOUND_CHANNEL


# ── Relay: stale command (command_ts amendment required) ─────────────────────

def test_relay_stale_bind_after_unbind_ignored(tmp_path):
    """Newer unbind consumed before old bind: old bind must not override it.

    Requires storage worker command_ts amendment in session_routes.py.
    """
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)

    unbind_ts = _ts(1) + "9"  # higher timestamp
    bind_ts = _ts(1) + "1"    # lower timestamp

    # Process newer unbind first
    unbind_event = _make_event(DEFAULT_CHANNEL, _ts(1), unbind_ts, "unbind")
    r_unbind = relay.handle_message(unbind_event)
    assert r_unbind.status == "route_unbound"

    # Now process older bind: should be stale
    bind_event = _make_event(DEFAULT_CHANNEL, _ts(1), bind_ts, f"bind <#{BOUND_CHANNEL}|general>")
    r_bind = relay.handle_message(bind_event)
    assert r_bind.status == "route_stale"
    assert r_bind.duplicate is True

    routes = SessionRoutes(relay.thread_store, provider="slack", scope=DEFAULT_CHANNEL)
    assert routes.destination(_target(1).thread_id) is None


# ── Relay: wrong-channel / unknown mapping ────────────────────────────────────

def test_relay_unknown_non_default_channel_ignored_channel(tmp_path):
    relay = _make_relay(tmp_path)
    event = _make_event(BOUND_CHANNEL, _ts(1), _ts(1) + "1", "hello")
    result = relay.handle_message(event)
    assert result.status == "ignored_channel"


def test_relay_default_channel_unknown_thread_ignored_unknown(tmp_path):
    relay = _make_relay(tmp_path)
    event = _make_event(DEFAULT_CHANNEL, _ts(99), _ts(99) + "1", "hello")
    result = relay.handle_message(event)
    assert result.status == "ignored_unknown_thread"


def test_relay_non_default_channel_with_mapping_accepted(tmp_path):
    """A message in a non-default channel is accepted if that channel is mapped."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    # Record the thread as if a notification was sent to BOUND_CHANNEL
    store = relay.thread_store
    fp = sha256_text("notice:99")
    store.record_thread(BOUND_CHANNEL, _ts(99), _target(99), fp)

    # Bind from that channel
    event = _make_event(BOUND_CHANNEL, _ts(99), _ts(99) + "1", f"bind <#{BOUND_CHANNEL}|general>")
    result = relay.handle_message(event)
    assert result.status not in ("ignored_channel", "ignored_unknown_thread")


# ── Relay: unauthorized / bot events leave state unchanged ────────────────────

def test_relay_unauthorized_user_ignored(tmp_path):
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    event = dict(type="message", channel=DEFAULT_CHANNEL, thread_ts=_ts(1),
                 ts=_ts(1) + "1", text=f"bind <#{BOUND_CHANNEL}|g>", user="UOTHER000")
    result = relay.handle_message(event)
    assert result.status == "ignored_unauthorized"

    routes = SessionRoutes(relay.thread_store, provider="slack", scope=DEFAULT_CHANNEL)
    assert routes.destination(_target(1).thread_id) is None


def test_relay_bot_message_ignored(tmp_path):
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    event = dict(type="message", channel=DEFAULT_CHANNEL, thread_ts=_ts(1),
                 ts=_ts(1) + "1", text=f"bind <#{BOUND_CHANNEL}|g>",
                 user=USER, bot_id="B12345678")
    result = relay.handle_message(event)
    assert result.status == "ignored_bot_or_subtype"


# ── Relay: ordinary text not impacted by routing ─────────────────────────────

def test_relay_ordinary_text_dispatches_normally(tmp_path):
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", "hello codex")
    result = relay.handle_message(event)
    assert result.status in ("queued", "uncertain")
    relay.queue_dispatcher.dispatch.assert_called_once()


# ── Relay: session independence ───────────────────────────────────────────────

def test_relay_second_session_independent(tmp_path):
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    _record(relay.thread_store, 2)

    relay.handle_message(
        _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>"))

    routes = SessionRoutes(relay.thread_store, provider="slack", scope=DEFAULT_CHANNEL)
    assert routes.destination(_target(2).thread_id) is None


def test_relay_unbind_only_removes_one(tmp_path):
    def _api2(method, params):
        return {"ok": True, "channel": {
            "id": BOUND_CHANNEL2, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}

    calls: list = []
    def route_api(method, params):
        if calls:
            return _api2(method, params)
        calls.append(1)
        return _info_api(BOUND_CHANNEL)(method, params)

    relay = _make_relay(tmp_path, route_api=route_api)
    _record(relay.thread_store, 1)
    _record(relay.thread_store, 2)

    relay.handle_message(_make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>"))
    relay.handle_message(_make_event(DEFAULT_CHANNEL, _ts(2), _ts(2) + "1", f"bind <#{BOUND_CHANNEL2}|g>"))

    relay.handle_message(_make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "2", "unbind"))

    routes = SessionRoutes(relay.thread_store, provider="slack", scope=DEFAULT_CHANNEL)
    assert routes.destination(_target(1).thread_id) is None
    assert routes.destination(_target(2).thread_id) == BOUND_CHANNEL2


# ── Notifier: send to bound channel ──────────────────────────────────────────

def _make_notifier(tmp_path: Path, *, posts: list) -> EnvironmentNotifier:
    def _post(token, method, payload, timeout):
        posts.append(dict(token=token, method=method, payload=payload))
        return {"ok": True, "channel": payload["channel"], "ts": "1789200000.000001"}

    config = NotificationConfig(
        slack_bot_token=BOT_TOKEN,
        slack_channel_id=DEFAULT_CHANNEL,
        slack_allowed_user_ids=(USER,),
        slack_reply_mode="poll",
    )
    return EnvironmentNotifier(tmp_path, config, slack_api_post=_post)


def test_notifier_sends_to_default_channel_when_unbound(tmp_path):
    posts: list = []
    notifier = _make_notifier(tmp_path, posts=posts)
    rt = _target(1)
    event = NotificationEvent("ws-1", "codex_started", sha256_text("fp1"), "subject", "msg",
                               relay_target=rt)
    notifier.notify(event)
    assert len(posts) == 1
    assert posts[0]["payload"]["channel"] == DEFAULT_CHANNEL


def test_notifier_sends_to_bound_channel(tmp_path):
    posts: list = []
    notifier = _make_notifier(tmp_path, posts=posts)

    # Manually bind a route
    from codex_watchdog.slack_poll import SlackPollingThreadStore
    store = SlackPollingThreadStore(tmp_path)
    fp = sha256_text("fp1")
    store.record_thread(DEFAULT_CHANNEL, _ts(1), _target(1), fp)
    routes = SessionRoutes(store, provider="slack", scope=DEFAULT_CHANNEL)
    routes.apply("event:E001", DEFAULT_CHANNEL, _ts(1), USER, f"bind <#{BOUND_CHANNEL}|g>",
                 BOUND_CHANNEL, command_ts=_ts(100))

    # Notify: should use bound channel
    rt = _target(1)
    event = NotificationEvent("ws-1", "codex_started", sha256_text("fp1"), "subject", "msg",
                               relay_target=rt)
    notifier.notify(event)
    assert any(p["payload"]["channel"] == BOUND_CHANNEL for p in posts)


def test_notifier_bound_failure_never_falls_back_to_webhook(tmp_path):
    fail_calls: list = []
    def _post(token, method, payload, timeout):
        fail_calls.append(payload["channel"])
        raise RuntimeError("channel_not_found")

    webhook_calls: list = []
    def _http(url, data, timeout):
        webhook_calls.append(url)
        return 200

    from codex_watchdog.slack_poll import SlackPollingThreadStore
    store = SlackPollingThreadStore(tmp_path)
    fp = sha256_text("fp2")
    store.record_thread(DEFAULT_CHANNEL, _ts(2), _target(2), fp)
    routes = SessionRoutes(store, provider="slack", scope=DEFAULT_CHANNEL)
    routes.apply("event:E002", DEFAULT_CHANNEL, _ts(2), USER, f"bind <#{BOUND_CHANNEL}|g>",
                 BOUND_CHANNEL, command_ts=_ts(100))

    config = NotificationConfig(
        slack_bot_token=BOT_TOKEN,
        slack_channel_id=DEFAULT_CHANNEL,
        slack_allowed_user_ids=(USER,),
        slack_reply_mode="poll",
        slack_webhook_url="https://hooks.slack.com/fake",
    )
    notifier = EnvironmentNotifier(tmp_path, config, slack_api_post=_post, http_post=_http,
                                   slack_thread_store=store)
    rt = _target(2)
    event = NotificationEvent("ws-2", "codex_started", sha256_text("fp2"), "subject", "msg",
                               relay_target=rt)
    result = notifier.notify(event)
    assert result.status == "delivery_failed"
    assert not webhook_calls  # webhook must NOT be called for a bound session


def test_notifier_unbound_failure_falls_back_to_webhook(tmp_path):
    def _post(token, method, payload, timeout):
        raise RuntimeError("channel_error")

    webhook_calls: list = []
    def _http(url, data, timeout):
        webhook_calls.append(url)
        return 200

    config = NotificationConfig(
        slack_bot_token=BOT_TOKEN,
        slack_channel_id=DEFAULT_CHANNEL,
        slack_allowed_user_ids=(USER,),
        slack_reply_mode="poll",
        slack_webhook_url="https://hooks.slack.com/fake",
    )
    notifier = EnvironmentNotifier(tmp_path, config, slack_api_post=_post, http_post=_http)
    rt = _target(3)
    event = NotificationEvent("ws-3", "codex_started", sha256_text("fp3"), "subject", "msg",
                               relay_target=rt)
    notifier.notify(event)
    assert len(webhook_calls) == 1  # fallback IS allowed for unbound sessions


# ── Notifier: end-to-end bind->future notification ───────────────────────────

def test_end_to_end_bind_then_notify_to_bound_channel(tmp_path):
    """Notifier sends to default; user binds; next notification goes to bound channel."""
    posts: list = []
    def _post(token, method, payload, timeout):
        ts = "17892{:08d}.000001".format(len(posts))
        posts.append(dict(method=method, channel=payload["channel"], ts=ts))
        return {"ok": True, "channel": payload["channel"], "ts": ts}

    config = NotificationConfig(
        slack_bot_token=BOT_TOKEN,
        slack_channel_id=DEFAULT_CHANNEL,
        slack_allowed_user_ids=(USER,),
        slack_reply_mode="poll",
    )
    from codex_watchdog.slack_poll import SlackPollingThreadStore
    store = SlackPollingThreadStore(tmp_path)
    notifier = EnvironmentNotifier(tmp_path, config, slack_api_post=_post, slack_thread_store=store)

    # First notification → default channel
    rt = _target(10)
    fp1 = sha256_text("fp-e2e-1")
    event1 = NotificationEvent("ws-10", "codex_started", fp1, "subject", "msg", relay_target=rt)
    notifier.notify(event1)
    assert posts[-1]["channel"] == DEFAULT_CHANNEL
    first_ts = posts[-1]["ts"]

    # Simulate user bind via relay
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    relay.thread_store = store
    bind_event = _make_event(DEFAULT_CHANNEL, first_ts, first_ts + "1",
                             f"bind <#{BOUND_CHANNEL}|g>")
    r = relay.handle_message(bind_event)
    assert r.status == "route_bound"

    # Second notification (different fingerprint) → bound channel
    fp2 = sha256_text("fp-e2e-2")
    event2 = NotificationEvent("ws-10", "codex_resumed", fp2, "subject", "msg2", relay_target=rt)
    notifier.notify(event2)
    assert posts[-1]["channel"] == BOUND_CHANNEL

    bound_ts = posts[-1]["ts"]
    reply = _make_event(BOUND_CHANNEL, bound_ts, bound_ts + "1", "continue")
    assert relay.handle_message(reply).status == "queued"
    assert relay.handle_message(reply).status == "duplicate"
    relay.queue_dispatcher.dispatch.assert_called_once()
    assert relay.queue_dispatcher.dispatch.call_args.args[0] == rt.thread_id


# ── Notifier: other sessions unaffected ──────────────────────────────────────

def test_notifier_other_sessions_use_default_channel(tmp_path):
    posts: list = []
    def _post(token, method, payload, timeout):
        ts = "1789300000.{:06d}".format(len(posts))
        posts.append(dict(channel=payload["channel"], ts=ts))
        return {"ok": True, "channel": payload["channel"], "ts": ts}

    config = NotificationConfig(
        slack_bot_token=BOT_TOKEN,
        slack_channel_id=DEFAULT_CHANNEL,
        slack_allowed_user_ids=(USER,),
        slack_reply_mode="poll",
    )
    from codex_watchdog.slack_poll import SlackPollingThreadStore
    store = SlackPollingThreadStore(tmp_path)
    notifier = EnvironmentNotifier(tmp_path, config, slack_api_post=_post, slack_thread_store=store)

    # Bind session 20
    fp20 = sha256_text("fp20")
    store.record_thread(DEFAULT_CHANNEL, _ts(21), replace(_target(21), workspace_id="ws-20"),
                        sha256_text("old-session-21"))
    store.record_thread(DEFAULT_CHANNEL, _ts(20), _target(20), fp20)
    routes = SessionRoutes(store, provider="slack", scope=DEFAULT_CHANNEL)
    routes.apply("event:E020", DEFAULT_CHANNEL, _ts(20), USER,
                 f"bind <#{BOUND_CHANNEL}|g>", BOUND_CHANNEL, command_ts=_ts(100))

    # Notify session 21 (different thread) → still default channel
    rt21 = replace(_target(21), workspace_id="ws-20")
    ev21 = NotificationEvent("ws-20", "codex_started", sha256_text("fp21"), "s", "m",
                              relay_target=rt21)
    notifier.notify(ev21)
    assert posts[-1]["channel"] == DEFAULT_CHANNEL

    # A newly created session in that same workspace also stays in the inbox.
    rt22 = replace(_target(22), workspace_id="ws-20")
    notifier.notify(NotificationEvent("ws-20", "codex_started", sha256_text("fp22"),
                                     "s", "m", relay_target=rt22))
    assert posts[-1]["channel"] == DEFAULT_CHANNEL


def test_route_api_failure_is_redacted_and_preserves_ticket(tmp_path):
    def fail(*args):
        raise RuntimeError("private-provider-detail")
    relay = _make_relay(tmp_path, route_api=fail)
    _record(relay.thread_store, 1)
    result = relay.handle_message(_make_event(DEFAULT_CHANNEL, _ts(1), _ts(100),
                                              f"bind <#{BOUND_CHANNEL}>"))
    assert result.status == "rejected_route_command"
    assert "private-provider-detail" not in json.dumps(result.to_dict())
    assert relay.handle_message(_make_event(DEFAULT_CHANNEL, _ts(1), _ts(101),
                                           "continue")).status == "queued"


def test_uncertain_route_ack_cannot_be_resent(tmp_path):
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(100), f"bind <#{BOUND_CHANNEL}>")
    result = relay.handle_message(event)
    attempts = []
    def fail(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("uncertain")
    client = SimpleNamespace(chat_postMessage=fail)
    # First acknowledge: hello send raises (caught; uncertain), then source ack raises (propagates)
    with pytest.raises(TimeoutError):
        relay.acknowledge(event, result, client)
    # Two attempts: (1) hello to BOUND_CHANNEL, (2) source ack to DEFAULT_CHANNEL
    assert len(attempts) == 2
    # Subsequent calls must not retry either send
    relay.acknowledge(event, result, client)
    relay.acknowledge(event, relay.handle_message(event), client)
    assert len(attempts) == 2


def test_poll_fifth_tick_keeps_active_polling_without_history(tmp_path):
    from codex_watchdog.slack_poll import SlackReplyPoller, SlackPollingThreadStore
    relay = _make_relay(tmp_path)
    relay.thread_store = SlackPollingThreadStore(tmp_path)
    _record(relay.thread_store, 1)
    calls = []
    def api(method, params):
        calls.append(method)
        return {"ok": True, "messages": []}
    poller = SlackReplyPoller(relay, api=api)
    for _ in range(5):
        poller.poll_once()
    assert calls == ["conversations.replies"] * 5


def test_malformed_historical_cursor_fails_before_provider_access(tmp_path):
    from codex_watchdog.slack_poll import SlackReplyPoller, SlackPollingThreadStore
    relay = _make_relay(tmp_path)
    relay.thread_store = SlackPollingThreadStore(tmp_path)
    _record(relay.thread_store, 1)
    journal = relay.thread_store.journal
    key = relay.thread_store.thread_key(DEFAULT_CHANNEL, _ts(1))
    with journal.transaction() as db:
        journal.claim(db, key)
        journal.put(db, "route_poll_cursors", key, {"schema_version": 1, "cursor": "NaN"})
    calls = []
    poller = SlackReplyPoller(relay, api=lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="cursor"):
        poller._poll_closed_once()
    assert calls == []


# ── Poll: bounded SQL query – no full-table scan ──────────────────────────────

def test_poll_closed_once_queries_one_row(tmp_path):
    """_poll_closed_once must issue bounded SQL, not load all mappings."""
    from codex_watchdog.slack_poll import SlackPollingThreadStore, SlackReplyPoller
    from unittest.mock import MagicMock

    store = SlackPollingThreadStore(tmp_path)
    for n in range(1, 6):
        fp = sha256_text("fp:" + str(n))
        store.record_thread(DEFAULT_CHANNEL, _ts(n), _target(n), fp)
    # Close all tickets
    journal = store.journal
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE kind='threads'")

    api_calls: list = []
    def mock_api(method, params):
        api_calls.append(dict(method=method, params=dict(params)))
        return {"ok": True, "messages": []}

    qd = MagicMock()
    qd.codex_home = None
    ssh = MagicMock()
    ssh.supports_control = False
    relay = SlackReplyRelay(tmp_path, bot_token=BOT_TOKEN, app_token=APP_TOKEN,
                            channel_id=DEFAULT_CHANNEL, allowed_user_ids=(USER,),
                            queue_dispatcher=qd, remote_ssh_adapter=ssh, thread_store=store,
                            reply_mode="poll")
    relay.thread_store = store
    poller = SlackReplyPoller(relay, api=mock_api)

    poller._poll_closed_once()
    replies_calls = [c for c in api_calls if c["method"] == "conversations.replies"]
    assert len(replies_calls) == 1  # exactly one page per closed-parent tick


# ── Poll: active count and closed-parent rotation ────────────────────────────

def test_poll_five_ticks_one_closed_poll(tmp_path):
    """Every 5th poll_once call invokes _poll_closed_once; first 4 are active-only."""
    from codex_watchdog.slack_poll import SlackPollingThreadStore, SlackReplyPoller
    from unittest.mock import MagicMock, patch

    store = SlackPollingThreadStore(tmp_path)
    qd = MagicMock()
    qd.codex_home = None
    ssh = MagicMock()
    ssh.supports_control = False
    relay = SlackReplyRelay(tmp_path, bot_token=BOT_TOKEN, app_token=APP_TOKEN,
                            channel_id=DEFAULT_CHANNEL, allowed_user_ids=(USER,),
                            queue_dispatcher=qd, remote_ssh_adapter=ssh, thread_store=store,
                            reply_mode="poll")
    relay.thread_store = store

    active_ticks = []
    closed_ticks = []

    def mock_api(method, params):
        return {"ok": True, "messages": []}

    poller = SlackReplyPoller(relay, api=mock_api)

    original_poll_closed = poller._poll_closed_once
    def track_closed():
        closed_ticks.append(poller._tick_count)
    poller._poll_closed_once = track_closed

    for _ in range(5):
        poller.poll_once()

    assert 5 in closed_ticks
    assert len(closed_ticks) == 1


# ── Poll: ordinary reply on closed parent never dispatches ───────────────────

def test_poll_closed_parent_ordinary_text_not_dispatched(tmp_path):
    """Ordinary text in a closed parent must not wake Codex."""
    from codex_watchdog.slack_poll import SlackPollingThreadStore, SlackReplyPoller
    from unittest.mock import MagicMock

    store = SlackPollingThreadStore(tmp_path)
    fp = sha256_text("fp:close1")
    store.record_thread(DEFAULT_CHANNEL, _ts(1), _target(1), fp)
    journal = store.journal
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE kind='threads'")

    dispatch_calls: list = []
    qd = MagicMock()
    qd.codex_home = None
    qd.dispatch.side_effect = lambda *a, **kw: dispatch_calls.append(a) or SimpleNamespace(status="enqueued")
    ssh = MagicMock()
    ssh.supports_control = False

    relay = SlackReplyRelay(tmp_path, bot_token=BOT_TOKEN, app_token=APP_TOKEN,
                            channel_id=DEFAULT_CHANNEL, allowed_user_ids=(USER,),
                            queue_dispatcher=qd, remote_ssh_adapter=ssh, thread_store=store,
                            reply_mode="poll")
    relay.thread_store = store

    reply_ts = _ts(1) + "5"
    def mock_api(method, params):
        if method == "conversations.replies":
            return {"ok": True, "messages": [
                dict(ts=reply_ts, thread_ts=_ts(1), user=USER, text="hello codex"),
            ]}
        return {"ok": True, "ts": "1789999999.000001", "channel": DEFAULT_CHANNEL}

    poller = SlackReplyPoller(relay, api=mock_api)
    poller._poll_closed_once()
    assert not dispatch_calls


# ── Poll: historical bind is processed ───────────────────────────────────────

def test_poll_closed_parent_bind_command_processed(tmp_path):
    """A bind command in a closed parent's history is processed by the relay."""
    from codex_watchdog.slack_poll import SlackPollingThreadStore, SlackReplyPoller
    from unittest.mock import MagicMock

    store = SlackPollingThreadStore(tmp_path)
    fp = sha256_text("fp:close2")
    store.record_thread(DEFAULT_CHANNEL, _ts(5), _target(5), fp)
    journal = store.journal
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE kind='threads'")

    qd = MagicMock()
    qd.codex_home = None
    qd.dispatch.return_value = SimpleNamespace(status="enqueued")
    ssh = MagicMock()
    ssh.supports_control = False

    relay = SlackReplyRelay(tmp_path, bot_token=BOT_TOKEN, app_token=APP_TOKEN,
                            channel_id=DEFAULT_CHANNEL, allowed_user_ids=(USER,),
                            queue_dispatcher=qd, remote_ssh_adapter=ssh, thread_store=store,
                            reply_mode="poll", route_api=_info_api(BOUND_CHANNEL))
    relay.thread_store = store

    bind_ts = _ts(5) + "3"
    def mock_api(method, params):
        if method == "conversations.replies":
            return {"ok": True, "messages": [
                dict(type="message", ts=bind_ts, thread_ts=_ts(5), user=USER,
                     text=f"bind <#{BOUND_CHANNEL}|g>"),
            ]}
        return {"ok": True, "ts": "1789999999.000001", "channel": DEFAULT_CHANNEL}

    poller = SlackReplyPoller(relay, api=mock_api)
    poller._poll_closed_once()

    routes = SessionRoutes(store, provider="slack", scope=DEFAULT_CHANNEL)
    assert routes.destination(_target(5).thread_id) == BOUND_CHANNEL


# ── Poll: repeated command after restart is idempotent ────────────────────────

def test_poll_repeated_bind_after_restart_idempotent(tmp_path):
    """Re-processing a bind command after restart returns duplicate, not an error."""
    from codex_watchdog.slack_poll import SlackPollingThreadStore, SlackReplyPoller
    from unittest.mock import MagicMock

    store = SlackPollingThreadStore(tmp_path)
    fp = sha256_text("fp:restart1")
    store.record_thread(DEFAULT_CHANNEL, _ts(7), _target(7), fp)
    journal = store.journal
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE kind='threads'")

    qd = MagicMock()
    qd.codex_home = None
    qd.dispatch.return_value = SimpleNamespace(status="enqueued")
    ssh = MagicMock()
    ssh.supports_control = False

    bind_ts = _ts(7) + "1"

    def make_relay(store):
        return SlackReplyRelay(
            tmp_path, bot_token=BOT_TOKEN, app_token=APP_TOKEN,
            channel_id=DEFAULT_CHANNEL, allowed_user_ids=(USER,),
            queue_dispatcher=qd, remote_ssh_adapter=ssh, thread_store=store,
            reply_mode="poll", route_api=_info_api(BOUND_CHANNEL))

    def mock_api(method, params):
        if method == "conversations.replies":
            return {"ok": True, "messages": [
                dict(type="message", ts=bind_ts, thread_ts=_ts(7), user=USER,
                     text=f"bind <#{BOUND_CHANNEL}|g>"),
            ]}
        return {"ok": True, "ts": "1789999999.000001", "channel": DEFAULT_CHANNEL}

    relay1 = make_relay(store)
    relay1.thread_store = store
    poller1 = SlackReplyPoller(relay1, api=mock_api)
    poller1._poll_closed_once()

    # Simulate restart by creating new store/relay pointing to same tmp_path
    store2 = SlackPollingThreadStore(tmp_path)
    relay2 = make_relay(store2)
    relay2.thread_store = store2

    # Erase cursor so it replays from the beginning
    journal2 = store2.journal
    with journal2.transaction() as db:
        db.execute("DELETE FROM records WHERE kind='route_poll_cursors'")

    poller2 = SlackReplyPoller(relay2, api=mock_api)
    poller2._poll_closed_once()

    routes = SessionRoutes(store2, provider="slack", scope=DEFAULT_CHANNEL)
    assert routes.destination(_target(7).thread_id) == BOUND_CHANNEL


# ── Poll: route cursor persists independently ─────────────────────────────────

def test_poll_route_cursor_stored_in_sqlite_not_json(tmp_path):
    """route_poll_cursors kind must be in SQLite, not in the JSON cursors file."""
    from codex_watchdog.slack_poll import SlackPollingThreadStore, SlackReplyPoller
    from unittest.mock import MagicMock

    store = SlackPollingThreadStore(tmp_path)
    fp = sha256_text("fp:cursor1")
    store.record_thread(DEFAULT_CHANNEL, _ts(3), _target(3), fp)
    journal = store.journal
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE kind='threads'")

    qd = MagicMock()
    qd.codex_home = None
    qd.dispatch.return_value = SimpleNamespace(status="enqueued")
    ssh = MagicMock()
    ssh.supports_control = False

    relay = SlackReplyRelay(tmp_path, bot_token=BOT_TOKEN, app_token=APP_TOKEN,
                            channel_id=DEFAULT_CHANNEL, allowed_user_ids=(USER,),
                            queue_dispatcher=qd, remote_ssh_adapter=ssh, thread_store=store,
                            reply_mode="poll", route_api=_info_api(BOUND_CHANNEL))
    relay.thread_store = store

    bind_ts = _ts(3) + "2"
    def mock_api(method, params):
        if method == "conversations.replies":
            return {"ok": True, "messages": [
                dict(ts=bind_ts, thread_ts=_ts(3), user=USER,
                     text=f"bind <#{BOUND_CHANNEL}|g>"),
            ]}
        return {"ok": True, "ts": "1789999999.000001", "channel": DEFAULT_CHANNEL}

    poller = SlackReplyPoller(relay, api=mock_api)
    poller._poll_closed_once()

    # The JSON cursors file must not contain per-parent reply cursors
    state = json.loads(poller.path.read_text(encoding="utf-8"))
    assert "route_poll_cursors" not in state
    assert "threads" in state  # active-ticket cursors are still there

    # The SQLite record must exist
    with journal.transaction() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM records WHERE kind='route_poll_cursors'"
        ).fetchone()[0]
    assert count >= 1


# ── Wire-level default _get_route_api tests ───────────────────────────────────
# These tests create the relay without an injected route_api so the default
# slack_conversations_get path is exercised. urllib.request.urlopen is patched
# at the module boundary in slack_route_commands.

def _wire_urlopen_ok(channel_id: str):
    """Return a fake urlopen that serves a successful conversations.info response."""
    def fake_urlopen(req, timeout):
        raw = json.dumps({"ok": True, "channel": {
            "id": channel_id, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}).encode()
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        cm.read.return_value = raw
        return cm
    return fake_urlopen


def test_wire_default_api_uses_get_not_post(tmp_path):
    """With no injected route_api, bind must issue a GET request (data=None)."""
    relay = _make_relay(tmp_path)  # no route_api
    _record(relay.thread_store, 1)

    captured = []
    original_fake = _wire_urlopen_ok(BOUND_CHANNEL)

    def capturing_urlopen(req, timeout):
        captured.append(req)
        return original_fake(req, timeout)

    with patch("codex_watchdog.slack_route_commands.urlopen", capturing_urlopen):
        result = relay.handle_message(
            _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
        )

    assert result.status == "route_bound"
    assert len(captured) == 1
    req = captured[0]
    assert req.data is None  # GET, not POST
    parsed = urlparse(req.full_url)
    assert parsed.netloc == "slack.com"
    assert "conversations.info" in parsed.path
    qs = parse_qs(parsed.query)
    assert qs.get("channel") == [BOUND_CHANNEL]
    assert req.get_header("Authorization") == f"Bearer {BOT_TOKEN}"
    assert BOT_TOKEN not in parsed.query


def test_wire_default_api_list_booleans_lowercase(tmp_path):
    """conversations.list params (exclude_archived=True) must be 'true' not 'True'."""
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    list_qs: list = []
    calls = [0]

    def fake_urlopen(req, timeout):
        calls[0] += 1
        parsed = urlparse(req.full_url)
        if "conversations.list" in parsed.path:
            list_qs.append(parse_qs(parsed.query))
            raw = json.dumps({"ok": True, "channels": [
                {"id": BOUND_CHANNEL, "name": "general"},
            ], "response_metadata": {}}).encode()
        else:
            raw = json.dumps({"ok": True, "channel": {
                "id": BOUND_CHANNEL, "is_member": True,
                "is_archived": False, "is_read_only": False, "is_frozen": False,
            }}).encode()
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        cm.read.return_value = raw
        return cm

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        result = relay.handle_message(
            _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", "bind #general")
        )

    assert result.status == "route_bound"
    assert list_qs, "conversations.list was not called"
    # Boolean must be 'true', never 'True'
    assert list_qs[0].get("exclude_archived") == ["true"]


def test_wire_default_api_success_commits_route(tmp_path):
    """Successful bind via default GET api must persist the destination."""
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    with patch("codex_watchdog.slack_route_commands.urlopen", _wire_urlopen_ok(BOUND_CHANNEL)):
        result = relay.handle_message(
            _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
        )

    assert result.status == "route_bound"
    assert result.delivery_status == BOUND_CHANNEL
    routes = SessionRoutes(relay.thread_store, provider="slack", scope=DEFAULT_CHANNEL)
    assert routes.destination(_target(1).thread_id) == BOUND_CHANNEL


def test_wire_missing_scope_delivery_status_stable_and_actionable(tmp_path):
    """missing_scope must produce delivery_status='route_destination_missing_scope'
    with actionable ack text, and must not commit a route or close the ticket."""
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    def fake_urlopen(req, timeout):
        raw = json.dumps({"ok": False, "error": "missing_scope"}).encode()
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        cm.read.return_value = raw
        return cm

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        result = relay.handle_message(
            _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}>")
        )

    assert result.status == "rejected_route_command"
    assert result.delivery_status == "route_destination_missing_scope"
    # Ack text must contain actionable scope names and reinstall guidance
    ack = relay._response_text(result)
    assert ack is not None
    assert "channels:read" in ack
    assert "groups:read" in ack
    assert "reinstall" in ack.lower()
    # Route must not have been committed
    routes = SessionRoutes(relay.thread_store, provider="slack", scope=DEFAULT_CHANNEL)
    assert routes.destination(_target(1).thread_id) is None
    # Ticket must remain open (ordinary text still queues)
    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        follow_up = relay.handle_message(
            _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "9", "hello codex")
        )
    assert follow_up.status in ("queued", "uncertain")


def test_wire_malformed_api_error_is_redacted(tmp_path):
    """Internal provider detail from a malformed response must not appear in result."""
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    secret = "super-secret-internal-detail"

    def fake_urlopen(req, timeout):
        raw = json.dumps({"ok": False, "error": "internal_error", "detail": secret}).encode()
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        cm.read.return_value = raw
        return cm

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        result = relay.handle_message(
            _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}>")
        )

    assert result.status == "rejected_route_command"
    assert secret not in json.dumps(result.to_dict())


def test_wire_http429_is_preserved_through_relay(tmp_path):
    """HTTPError 429 from the wire must propagate out of handle_message for poller backoff."""
    relay = _make_relay(tmp_path)
    _record(relay.thread_store, 1)

    err = HTTPError("https://slack.com/api/conversations.info", 429, "Too Many Requests", {}, BytesIO(b""))

    def fake_urlopen(req, timeout):
        raise err

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        with pytest.raises(HTTPError) as exc_info:
            relay.handle_message(
                _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}>")
            )
    assert exc_info.value.code == 429


# ── Hello: destination greeting after successful bind ────────────────────────

def _hello_client(msgs: list, *, channel_ts="1789600000.000001"):
    """Return a client whose chat_postMessage appends to msgs and returns a valid response."""
    def _post(**kw):
        msgs.append(kw)
        return {"ok": True, "channel": kw["channel"], "ts": channel_ts}
    return SimpleNamespace(chat_postMessage=_post)


def test_hello_sent_to_destination_no_thread_ts(tmp_path):
    """Hello must be posted to destination channel with no thread_ts."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    msgs: list = []
    client = _hello_client(msgs)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)
    hello = msgs[0]
    assert hello["channel"] == BOUND_CHANNEL
    assert "thread_ts" not in hello
    assert "Hello! Binding successful." in hello["text"]
    assert _target(1).thread_id in hello["text"]
    assert _target(1).workspace_id in hello["text"]


def test_hello_records_new_thread_mapping_for_destination_replies(tmp_path):
    """After a successful hello, replies to the new destination thread route to the same session."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    hello_ts = "1789600000.000099"
    msgs: list = []
    client = _hello_client(msgs, channel_ts=hello_ts)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)
    assert len(msgs) == 2  # hello + source ack

    # A reply to the hello thread in BOUND_CHANNEL must be accepted and dispatched
    reply_event = _make_event(BOUND_CHANNEL, hello_ts, hello_ts + "1", "continue session")
    relay2 = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    relay2.thread_store = relay.thread_store
    r = relay2.handle_message(reply_event)
    assert r.status in ("queued", "uncertain")
    assert relay2.queue_dispatcher.dispatch.call_args.args[0] == _target(1).thread_id


def test_hello_exactly_one_send_on_duplicate_acknowledge(tmp_path):
    """No second hello is sent when acknowledge is called again with the same result."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    hello_ts = "1789600000.000002"
    msgs: list = []
    client = _hello_client(msgs, channel_ts=hello_ts)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)
    relay.acknowledge(event, result, client)
    relay.acknowledge(event, relay.handle_message(event), client)
    hello_calls = [m for m in msgs if m["channel"] == BOUND_CHANNEL and "thread_ts" not in m]
    assert len(hello_calls) == 1


def test_source_ticket_closed_new_hello_ticket_active(tmp_path):
    """After bind+hello, source ticket is closed and hello ticket is active."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    hello_ts = "1789600000.000003"
    msgs: list = []
    client = _hello_client(msgs, channel_ts=hello_ts)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)

    journal = relay.thread_store.journal
    with journal.transaction() as db:
        # Source ticket (DEFAULT_CHANNEL, _ts(1)) must be closed
        src_key = relay.thread_store.thread_key(DEFAULT_CHANNEL, _ts(1))
        row = db.execute(
            "SELECT active FROM records WHERE kind='threads' AND key=?", (src_key,)
        ).fetchone()
        assert row is not None and row[0] == 0
        # Hello ticket (BOUND_CHANNEL, hello_ts) must be active
        hello_key = relay.thread_store.thread_key(BOUND_CHANNEL, hello_ts)
        row2 = db.execute(
            "SELECT active FROM records WHERE kind='threads' AND key=?", (hello_key,)
        ).fetchone()
        assert row2 is not None and row2[0] == 1


def test_hello_not_sent_for_unbind(tmp_path):
    """route_unbound never triggers a destination hello."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    hello_ts = "1789600000.000004"
    msgs: list = []
    client = _hello_client(msgs, channel_ts=hello_ts)

    bind_event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    r1 = relay.handle_message(bind_event)
    relay.acknowledge(bind_event, r1, client)
    msgs.clear()

    unbind_event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "2", "unbind")
    r2 = relay.handle_message(unbind_event)
    relay.acknowledge(unbind_event, r2, client)
    assert r2.status == "route_unbound"
    # No hello should be sent (unbind has no destination)
    hello_calls = [m for m in msgs if "thread_ts" not in m]
    assert len(hello_calls) == 0


def test_hello_not_sent_for_stale_command(tmp_path):
    """A stale command must never produce a destination hello."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    hello_ts = "1789600000.000005"
    msgs: list = []
    client = _hello_client(msgs, channel_ts=hello_ts)

    # Bind with higher ts
    bind_event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "9", f"bind <#{BOUND_CHANNEL}|g>")
    r1 = relay.handle_message(bind_event)
    relay.acknowledge(bind_event, r1, client)
    msgs.clear()

    # Stale command (lower ts, never seen before)
    stale_event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    r_stale = relay.handle_message(stale_event)
    assert r_stale.status == "route_stale"
    relay.acknowledge(stale_event, r_stale, client)
    assert len(msgs) == 0  # no ack, no hello for stale


def test_hello_wrong_channel_response_no_mapping(tmp_path):
    """Wrong channel in chat.postMessage response must leave no new thread mapping."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    msgs: list = []

    def bad_client(**kw):
        msgs.append(kw)
        return {"ok": True, "channel": "CWRONG0001", "ts": "1789600000.000006"}

    client = SimpleNamespace(chat_postMessage=bad_client)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)

    # No mapping recorded for BOUND_CHANNEL + returned ts
    mapping = relay.thread_store.lookup_thread(BOUND_CHANNEL, "1789600000.000006")
    assert mapping is None
    # Source ack should still mention hello failure
    source_ack = msgs[1]
    assert "could not be confirmed" in source_ack["text"] or "Binding saved" in source_ack["text"]


def test_hello_malformed_ok_false_no_mapping(tmp_path):
    """ok=False in chat.postMessage response leaves no thread mapping."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)

    def fail_ok_client(**kw):
        return {"ok": False, "error": "channel_not_found"}

    client = SimpleNamespace(chat_postMessage=fail_ok_client)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)

    journal = relay.thread_store.journal
    with journal.transaction() as db:
        hello_rows = db.execute(
            "SELECT COUNT(*) FROM records WHERE kind='threads' AND key!=?",
            (relay.thread_store.thread_key(DEFAULT_CHANNEL, _ts(1)),)
        ).fetchone()[0]
    assert hello_rows == 0


def test_hello_timeout_uncertain_persists_no_resend(tmp_path):
    """Timeout on hello send leaves hello_status=uncertain; second acknowledge never resends."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)

    hello_attempts = []
    source_ts = "1789600000.000007"

    def flaky_client(**kw):
        if "thread_ts" not in kw:
            hello_attempts.append(kw)
            raise TimeoutError("hello timeout")
        return {"ok": True, "channel": kw["channel"], "ts": source_ts}

    client = SimpleNamespace(chat_postMessage=flaky_client)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)
    assert len(hello_attempts) == 1

    # Second acknowledge must not retry hello
    relay.acknowledge(event, result, client)
    assert len(hello_attempts) == 1

    # hello_status must be uncertain in the journal
    from codex_watchdog.session_routes import SessionRoutes
    routes = SessionRoutes(relay.thread_store, scope=DEFAULT_CHANNEL)
    journal = relay.thread_store.journal
    with journal.transaction() as db:
        key = routes._command_key(result.instruction_id)
        entry = journal.get(db, "route_commands", key)
    assert entry.get("hello_claimed") is True
    assert entry.get("hello_status") == "uncertain"


def test_source_ack_still_sent_after_hello_failure(tmp_path):
    """Source thread ack is posted even when hello fails."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)

    source_acks = []
    source_ts = "1789600000.000008"

    def partial_client(**kw):
        if "thread_ts" not in kw:
            raise RuntimeError("hello failed")
        source_acks.append(kw)
        return {"ok": True, "channel": kw["channel"], "ts": source_ts}

    client = SimpleNamespace(chat_postMessage=partial_client)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)
    assert len(source_acks) == 1
    assert source_acks[0]["channel"] == DEFAULT_CHANNEL
    assert source_acks[0]["thread_ts"] == _ts(1)
    # Source ack text should reflect that hello could not be confirmed
    assert "could not be confirmed" in source_acks[0]["text"] or "Binding saved" in source_acks[0]["text"]


def test_hello_socket_sdk_response_data_accepted(tmp_path):
    """Slack SDK SocketMode response with .data dict must be accepted as valid."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    hello_ts = "1789600000.000009"
    msgs: list = []

    def sdk_client(**kw):
        resp = MagicMock()
        resp.data = {"ok": True, "channel": kw["channel"], "ts": hello_ts}
        msgs.append(kw)
        return resp

    client = SimpleNamespace(chat_postMessage=sdk_client)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)

    # Hello must have succeeded: mapping recorded
    mapping = relay.thread_store.lookup_thread(BOUND_CHANNEL, hello_ts)
    assert mapping is not None
    assert mapping.target.thread_id == _target(1).thread_id


def test_hello_poll_dict_accepted(tmp_path):
    """Plain dict response (as returned by the poller path) is accepted."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    hello_ts = "1789600000.000010"
    msgs: list = []

    def poll_client(**kw):
        msgs.append(kw)
        return {"ok": True, "channel": kw["channel"], "ts": hello_ts}

    client = SimpleNamespace(chat_postMessage=poll_client)
    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    relay.acknowledge(event, result, client)

    mapping = relay.thread_store.lookup_thread(BOUND_CHANNEL, hello_ts)
    assert mapping is not None
    assert mapping.target.thread_id == _target(1).thread_id


def test_hello_same_workspace_different_session_isolated(tmp_path):
    """Two sessions each get their own distinct hello mapping; thread_ids do not cross."""
    relay1 = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay1.thread_store, 1)
    _record(relay1.thread_store, 2)

    hello_ts1 = "1789600000.100001"
    hello_ts2 = "1789600000.200002"

    def multi_client(**kw):
        ch = kw["channel"]
        ts = hello_ts1 if ch == BOUND_CHANNEL else (hello_ts2 if ch == BOUND_CHANNEL2 else "1789600000.000000")
        return {"ok": True, "channel": ch, "ts": ts}

    client = SimpleNamespace(chat_postMessage=multi_client)

    ev1 = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    r1 = relay1.handle_message(ev1)
    relay1.acknowledge(ev1, r1, client)

    relay2 = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL2))
    relay2.thread_store = relay1.thread_store
    ev2 = _make_event(DEFAULT_CHANNEL, _ts(2), _ts(2) + "1", f"bind <#{BOUND_CHANNEL2}|g>")
    r2 = relay2.handle_message(ev2)
    relay2.acknowledge(ev2, r2, client)

    mapping1 = relay1.thread_store.lookup_thread(BOUND_CHANNEL, hello_ts1)
    mapping2 = relay1.thread_store.lookup_thread(BOUND_CHANNEL2, hello_ts2)
    assert mapping1 is not None and mapping1.target.thread_id == _target(1).thread_id
    assert mapping2 is not None and mapping2.target.thread_id == _target(2).thread_id
    assert mapping1.target.thread_id != mapping2.target.thread_id


def test_hello_existing_old_receipt_without_hello_claimed_compatible(tmp_path):
    """Old command records lacking hello_claimed still trigger a hello on acknowledge."""
    relay = _make_relay(tmp_path, route_api=_info_api(BOUND_CHANNEL))
    _record(relay.thread_store, 1)
    hello_ts = "1789600000.000011"

    event = _make_event(DEFAULT_CHANNEL, _ts(1), _ts(1) + "1", f"bind <#{BOUND_CHANNEL}|g>")
    result = relay.handle_message(event)
    assert result.status == "route_bound"

    # Strip hello_claimed to simulate a pre-feature stored record
    from codex_watchdog.session_routes import SessionRoutes
    routes = SessionRoutes(relay.thread_store, scope=DEFAULT_CHANNEL)
    ek = "message:" + event["channel"] + ":" + event["ts"]
    journal = relay.thread_store.journal
    with journal.transaction() as db:
        ckey = routes._command_key(ek)
        entry = journal.get(db, "route_commands", ckey)
        if entry is not None:
            entry.pop("hello_claimed", None)
            entry.pop("hello_status", None)
            journal.put(db, "route_commands", ckey, entry)

    msgs: list = []
    def compat_client(**kw):
        msgs.append(kw)
        return {"ok": True, "channel": kw["channel"], "ts": hello_ts}
    client = SimpleNamespace(chat_postMessage=compat_client)
    relay.acknowledge(event, result, client)
    # Absent hello_claimed is treated as False → hello must be sent
    hello_calls = [m for m in msgs if m["channel"] == BOUND_CHANNEL and "thread_ts" not in m]
    assert len(hello_calls) == 1
