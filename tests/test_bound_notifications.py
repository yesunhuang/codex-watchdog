"""Provider-bound exact-session destination sending in EnvironmentNotifier.

Each scenario seeds session_routes records directly through the journal
(mirroring how the SessionRoutes worker's own bind flow persists them),
independent of the still-Slack-only SessionRoutes.apply() control surface.
"""
from dataclasses import dataclass, field

from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.onebot_relay import OneBotThreadStore
from codex_watchdog.onebot_transport import OneBotConfig
from codex_watchdog.relay import RelayTarget
from codex_watchdog.session_routes import SessionRoutes


LARK_CHAT = "oc_default000000001"
LARK_BOUND = "oc_bound0000000001"
LARK_USER = "ou_loopback0000001"

ONEBOT_GROUP = "67890"
ONEBOT_BOUND_GROUP = "99999"
ONEBOT_USER = "54321"

THREAD_A = "11111111-2222-4333-8444-555555555555"
THREAD_B = "22222222-2222-4333-8444-555555555555"


def lark_config():
    return LarkConfig("cli_" + "a" * 20, "loopback-secret", LARK_CHAT, (LARK_USER,))


def onebot_config():
    return OneBotConfig("ws://127.0.0.1:1/", "loopback-token", "12345", "group", ONEBOT_GROUP, (ONEBOT_USER,))


def seed_route(store, provider, scope, thread_id, destination, *, last_command_ts="1700000000000"):
    routes = SessionRoutes(store, provider=provider, scope=scope)
    key = routes._route_key(thread_id)
    entry = dict(schema_version=1, provider=provider, scope=scope, thread_id=thread_id,
                 destination=destination, last_command_ts=last_command_ts,
                 created_at="2026-09-26T00:00:00Z")
    journal = store.journal
    with journal.transaction() as db:
        journal.put(db, "session_routes", key, entry)


@dataclass
class FakeApi:
    responder: object
    calls: list = field(default_factory=list)

    def send(self, text, operation_id, *, reply_to=None, destination=None):
        self.calls.append(dict(text=text, operation_id=operation_id, reply_to=reply_to, destination=destination))
        return self.responder(destination)


def make_event(fingerprint, target=None):
    return NotificationEvent(fingerprint, "waiting", fingerprint, "Subject", "Body", target)


# ── Lark ──────────────────────────────────────────────────────────────────

def test_lark_unbound_thread_uses_configured_default_destination(tmp_path):
    config = NotificationConfig(lark=lark_config())
    store = LarkThreadStore(tmp_path, config.lark.scope)
    api = FakeApi(lambda destination: {"chat_id": destination or LARK_CHAT, "message_id": "om_reply00000001"})
    notifier = EnvironmentNotifier(tmp_path, config, lark_api=api, lark_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    result = notifier.notify(event)

    assert result.status == "sent"
    assert api.calls == [dict(text=api.calls[0]["text"], operation_id=event.event_fingerprint(),
                              reply_to=None, destination=None)]


def test_lark_bound_thread_sends_to_bound_destination_and_records_mapping(tmp_path):
    config = NotificationConfig(lark=lark_config())
    store = LarkThreadStore(tmp_path, config.lark.scope)
    seed_route(store, "lark", config.lark.scope, THREAD_A, LARK_BOUND)
    api = FakeApi(lambda destination: {"chat_id": destination, "message_id": "om_reply00000002"})
    notifier = EnvironmentNotifier(tmp_path, config, lark_api=api, lark_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    result = notifier.notify(event)

    assert result.status == "sent"
    assert api.calls[0]["destination"] == LARK_BOUND
    assert store.has_notification_mapping(event.event_fingerprint())


def test_lark_second_session_without_route_stays_at_default(tmp_path):
    config = NotificationConfig(lark=lark_config())
    store = LarkThreadStore(tmp_path, config.lark.scope)
    seed_route(store, "lark", config.lark.scope, THREAD_A, LARK_BOUND)
    api = FakeApi(lambda destination: {"chat_id": destination or LARK_CHAT, "message_id": "om_reply00000003"})
    notifier = EnvironmentNotifier(tmp_path, config, lark_api=api, lark_thread_store=store)
    event = make_event("workspace-b", RelayTarget("workspace-b", THREAD_B, "process_local"))

    result = notifier.notify(event)

    assert result.status == "sent"
    assert api.calls[0]["destination"] is None  # unaffected by session A's binding


def test_lark_malformed_route_fails_closed_without_any_api_call(tmp_path):
    config = NotificationConfig(lark=lark_config())
    store = LarkThreadStore(tmp_path, config.lark.scope)
    seed_route(store, "lark", config.lark.scope, THREAD_A, "not-an-oc-id")  # malformed destination
    api = FakeApi(lambda destination: {"chat_id": destination, "message_id": "om_reply00000004"})
    notifier = EnvironmentNotifier(tmp_path, config, lark_api=api, lark_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    result = notifier.notify(event)

    assert result.status == "delivery_failed"
    assert result.attempted_channels == ("lark",)
    assert api.calls == []
    assert not store.has_notification_mapping(event.event_fingerprint())


def test_lark_wrong_returned_destination_records_no_mapping(tmp_path):
    config = NotificationConfig(lark=lark_config())
    store = LarkThreadStore(tmp_path, config.lark.scope)
    seed_route(store, "lark", config.lark.scope, THREAD_A, LARK_BOUND)
    api = FakeApi(lambda destination: {"chat_id": LARK_CHAT, "message_id": "om_reply00000005"})  # wrong chat
    notifier = EnvironmentNotifier(tmp_path, config, lark_api=api, lark_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    result = notifier.notify(event)

    assert result.status == "delivery_failed"
    assert not store.has_notification_mapping(event.event_fingerprint())


def test_lark_uncertain_delivery_is_never_resent(tmp_path):
    config = NotificationConfig(lark=lark_config())
    store = LarkThreadStore(tmp_path, config.lark.scope)
    seed_route(store, "lark", config.lark.scope, THREAD_A, LARK_BOUND)
    api = FakeApi(lambda destination: {"chat_id": LARK_CHAT, "message_id": "om_reply00000006"})  # wrong chat
    notifier = EnvironmentNotifier(tmp_path, config, lark_api=api, lark_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    first = notifier.notify(event)
    second = notifier.notify(event)

    assert first.status == second.status == "delivery_failed"
    assert len(api.calls) == 1  # the second attempt never re-sent


# ── OneBot ────────────────────────────────────────────────────────────────

def test_onebot_unbound_thread_uses_configured_default_destination(tmp_path):
    config = NotificationConfig(onebot=onebot_config())
    store = OneBotThreadStore(tmp_path, config.onebot.scope)
    api = FakeApi(lambda destination: {"chat_id": destination or config.onebot.destination, "message_id": "1001"})
    notifier = EnvironmentNotifier(tmp_path, config, onebot_api=api, onebot_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    result = notifier.notify(event)

    assert result.status == "sent"
    assert api.calls[0]["destination"] is None


def test_onebot_bound_thread_sends_to_bound_destination_and_records_mapping(tmp_path):
    config = NotificationConfig(onebot=onebot_config())
    store = OneBotThreadStore(tmp_path, config.onebot.scope)
    bound = "group:" + ONEBOT_BOUND_GROUP
    seed_route(store, "onebot", config.onebot.scope, THREAD_A, bound)
    api = FakeApi(lambda destination: {"chat_id": destination, "message_id": "1002"})
    notifier = EnvironmentNotifier(tmp_path, config, onebot_api=api, onebot_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    result = notifier.notify(event)

    assert result.status == "sent"
    assert api.calls[0]["destination"] == bound
    assert store.has_notification_mapping(event.event_fingerprint())


def test_onebot_second_session_without_route_stays_at_default(tmp_path):
    config = NotificationConfig(onebot=onebot_config())
    store = OneBotThreadStore(tmp_path, config.onebot.scope)
    bound = "group:" + ONEBOT_BOUND_GROUP
    seed_route(store, "onebot", config.onebot.scope, THREAD_A, bound)
    api = FakeApi(lambda destination: {"chat_id": destination or config.onebot.destination, "message_id": "1003"})
    notifier = EnvironmentNotifier(tmp_path, config, onebot_api=api, onebot_thread_store=store)
    event = make_event("workspace-b", RelayTarget("workspace-b", THREAD_B, "process_local"))

    result = notifier.notify(event)

    assert result.status == "sent"
    assert api.calls[0]["destination"] is None


def test_onebot_malformed_route_fails_closed_without_any_api_call(tmp_path):
    config = NotificationConfig(onebot=onebot_config())
    store = OneBotThreadStore(tmp_path, config.onebot.scope)
    seed_route(store, "onebot", config.onebot.scope, THREAD_A, "not-a-destination")
    api = FakeApi(lambda destination: {"chat_id": destination, "message_id": "1004"})
    notifier = EnvironmentNotifier(tmp_path, config, onebot_api=api, onebot_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    result = notifier.notify(event)

    assert result.status == "delivery_failed"
    assert result.attempted_channels == ("onebot",)
    assert api.calls == []
    assert not store.has_notification_mapping(event.event_fingerprint())


def test_onebot_wrong_returned_destination_records_no_mapping(tmp_path):
    config = NotificationConfig(onebot=onebot_config())
    store = OneBotThreadStore(tmp_path, config.onebot.scope)
    bound = "group:" + ONEBOT_BOUND_GROUP
    seed_route(store, "onebot", config.onebot.scope, THREAD_A, bound)
    api = FakeApi(lambda destination: {"chat_id": config.onebot.destination, "message_id": "1005"})  # wrong chat
    notifier = EnvironmentNotifier(tmp_path, config, onebot_api=api, onebot_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    result = notifier.notify(event)

    assert result.status == "delivery_failed"
    assert not store.has_notification_mapping(event.event_fingerprint())


def test_onebot_uncertain_delivery_is_never_resent(tmp_path):
    config = NotificationConfig(onebot=onebot_config())
    store = OneBotThreadStore(tmp_path, config.onebot.scope)
    bound = "group:" + ONEBOT_BOUND_GROUP
    seed_route(store, "onebot", config.onebot.scope, THREAD_A, bound)
    api = FakeApi(lambda destination: {"chat_id": config.onebot.destination, "message_id": "1006"})  # wrong chat
    notifier = EnvironmentNotifier(tmp_path, config, onebot_api=api, onebot_thread_store=store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    first = notifier.notify(event)
    second = notifier.notify(event)

    assert first.status == second.status == "delivery_failed"
    assert len(api.calls) == 1


# ── Cross-provider isolation ─────────────────────────────────────────────

def test_binding_one_provider_never_affects_the_others_default(tmp_path):
    """A route bound only for lark must not change onebot's default for the same thread."""
    config = NotificationConfig(lark=lark_config(), onebot=onebot_config(),
                                interactive_transport="lark+onebot")
    lark_store = LarkThreadStore(tmp_path, config.lark.scope)
    onebot_store = OneBotThreadStore(tmp_path, config.onebot.scope)
    seed_route(lark_store, "lark", config.lark.scope, THREAD_A, LARK_BOUND)
    lark_api = FakeApi(lambda destination: {"chat_id": destination, "message_id": "om_reply00000007"})
    onebot_api = FakeApi(lambda destination: {"chat_id": destination or config.onebot.destination, "message_id": "1007"})
    notifier = EnvironmentNotifier(tmp_path, config, lark_api=lark_api, lark_thread_store=lark_store,
                                   onebot_api=onebot_api, onebot_thread_store=onebot_store)
    event = make_event("workspace-a", RelayTarget("workspace-a", THREAD_A, "process_local"))

    result = notifier.notify(event)

    assert result.status == "sent"
    assert lark_api.calls[0]["destination"] == LARK_BOUND
    assert onebot_api.calls[0]["destination"] is None
