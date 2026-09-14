from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from codex_watchdog.control_state import ControlStore
from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.lark_relay import LarkReplyRelay
from codex_watchdog.lark_transport import LarkConfig, LarkTransportError
from codex_watchdog.models import sha256_text
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.relay import RelayTarget, reply_relay_from_config
from codex_watchdog.slack_mapping import SlackRelayTarget, SlackThreadStore


THREAD = "11111111-2222-4333-8444-555555555555"
OTHER_THREAD = "22222222-2222-4333-8444-555555555555"
CHAT = "oc_fixture000001"
MESSAGE = "om_notification01"
USER = "ou_fixture000001"
SECRET = "fixture-secret-not-a-provider-token"


def config(*, users=(USER,), domain="feishu"):
    return LarkConfig("cli_fixture000001", SECRET, CHAT, users, domain)


def event(*, text="  Literal reply\nwith Unicode \u2603  ", event_id="fixture-event-000001", message_id="om_reply00000001"):
    return {"schema": "2.0", "header": {"event_id": event_id, "app_id": config().app_id,
            "event_type": "im.message.receive_v1"}, "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": USER}},
                "message": {"message_id": message_id, "chat_id": CHAT, "message_type": "text",
                            "root_id": MESSAGE, "parent_id": MESSAGE,
                            "content": json.dumps({"text": text})}}}


class Api:
    def __init__(self):
        self.calls = []
        self.error = None

    def send(self, text, operation_id, **kwargs):
        self.calls.append((text, operation_id, kwargs))
        if self.error:
            raise self.error
        return {"chat_id": CHAT, "message_id": MESSAGE}


@pytest.fixture
def relay(tmp_path):
    calls = []
    queue = SimpleNamespace(dispatch=lambda *args: (calls.append(args) or SimpleNamespace(status="enqueued")))
    api = Api()
    cfg = config()
    notifier = EnvironmentNotifier(tmp_path, NotificationConfig(lark=cfg), lark_api=api)
    notification = NotificationEvent("workspace", "waiting", "fixture-notification", "Waiting", "Reply here",
                                     RelayTarget("workspace", THREAD, "process_local"))
    assert notifier.notify(notification).status == "sent"
    instance = LarkReplyRelay(tmp_path, cfg, queue_dispatcher=queue,
                              remote_ssh_adapter=SimpleNamespace(), api=api)
    return instance, notifier, api, calls, notification


def test_reply_is_verbatim_durable_and_delivered_once_after_restart(relay):
    instance, notifier, api, calls, notification = relay
    payload = event()
    assert instance.handle_event(payload).status == "queued"
    restarted = LarkReplyRelay(instance.runtime, instance.config, queue_dispatcher=instance.queue_dispatcher,
                               remote_ssh_adapter=instance.remote_ssh_adapter)
    assert restarted.handle_event(payload).duplicate
    assert len(calls) == 1
    assert calls[0][0] == THREAD and calls[0][2] == json.loads(payload["event"]["message"]["content"])["text"]
    assert calls[0][3] == "lark_reply"
    stored = instance.thread_store.path.read_text()
    assert "Literal reply" not in stored and SECRET not in stored
    assert len(api.calls) == 1  # handle_event itself never posts an acknowledgement.


def test_alternative_event_id_for_same_message_is_deduplicated(relay):
    instance, _, _, calls, _ = relay
    assert instance.handle_event(event()).status == "queued"
    assert instance.handle_event(event(event_id="fixture-event-retry2")).duplicate
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["text", "message", "thread"])
def test_event_id_collision_cannot_change_payload_or_target(relay, change):
    instance, _, _, calls, _ = relay
    assert instance.handle_event(event()).status == "queued"
    altered = event()
    if change == "text":
        altered["event"]["message"]["content"] = json.dumps({"text": "different"})
    elif change == "message":
        altered["event"]["message"]["message_id"] = "om_differentreply"
    else:
        instance.thread_store.cache_mappings([{"provider": "lark", "scope": instance.config.scope,
                                             "chat_id": CHAT, "message_id": "om_otherthread00",
                                             "event_fingerprint": "b" * 64}],
                                            RelayTarget("other", OTHER_THREAD, "process_local"))
        altered["event"]["message"].update(root_id="om_otherthread00", parent_id="om_otherthread00")
    assert instance.handle_event(altered).status == "rejected_state_or_collision"
    assert instance.handle_event(event()).duplicate and len(calls) == 1


def test_message_id_collision_with_new_event_is_rejected(relay):
    instance, _, _, calls, _ = relay
    instance.handle_event(event())
    assert instance.handle_event(event(text="changed", event_id="fixture-event-retry2")).status == "rejected_state_or_collision"
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [
    ("sender_type", "app"), ("user", "ou_notallowed001"), ("chat", "oc_notallowed001"),
    ("root", "om_unmapped00001"), ("app", "cli_otherapp001"), ("kind", "image"),
    ("event_id", None), ("schema", "1.0"), ("text", ""),
])
def test_sender_destination_event_and_mapping_gates(relay, field, value):
    instance, _, _, calls, _ = relay
    payload = event()
    message = payload["event"]["message"]
    if field == "sender_type": payload["event"]["sender"]["sender_type"] = value
    elif field == "user": payload["event"]["sender"]["sender_id"]["open_id"] = value
    elif field == "chat": message["chat_id"] = value
    elif field == "root": message.update(root_id=value, parent_id=value)
    elif field == "app": payload["header"]["app_id"] = value
    elif field == "kind": message["message_type"] = value
    elif field == "event_id": payload["header"]["event_id"] = value
    elif field == "schema": payload["schema"] = value
    else: message["content"] = json.dumps({"text": value})
    before = instance.thread_store.path.read_bytes()
    assert instance.handle_event(payload).status.startswith("ignored_")
    assert not calls and instance.thread_store.path.read_bytes() == before


def test_ambiguous_root_and_parent_fail_closed(relay):
    instance, _, _, calls, _ = relay
    instance.thread_store.cache_mappings([{"provider": "lark", "scope": instance.config.scope,
                                         "chat_id": CHAT, "message_id": "om_otherthread00",
                                         "event_fingerprint": "b" * 64}],
                                        RelayTarget("other", OTHER_THREAD, "process_local"))
    payload = event()
    payload["event"]["message"]["parent_id"] = "om_otherthread00"
    assert instance.handle_event(payload).status == "ignored_ambiguous_thread"
    assert not calls


def test_dispatch_uncertainty_is_not_blindly_replayed(relay):
    instance, _, _, calls, _ = relay
    def fail(*args):
        calls.append(args)
        raise RuntimeError("raw provider detail must not enter receipts")
    instance.queue_dispatcher.dispatch = fail
    result = instance.handle_event(event())
    assert result.status == "uncertain" and result.error_sha256
    assert instance.handle_event(event()).duplicate and len(calls) == 1
    assert "raw provider detail" not in instance.thread_store.path.read_text()


def test_reply_over_codex_queue_limit_is_rejected_before_claim_or_dispatch(relay):
    from codex_watchdog.models import MAX_PROMPT_CHARS
    instance, _, _, calls, _ = relay
    before = instance.thread_store.path.read_bytes()
    result = instance.handle_event(event(text="x" * (MAX_PROMPT_CHARS + 1)))
    assert result.status == "ignored_empty_or_oversize"
    assert calls == [] and instance.thread_store.path.read_bytes() == before


def test_parallel_provider_retries_admit_at_most_once(relay):
    instance, _, _, calls, _ = relay
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: instance.handle_event(event()), range(18)))
    instance.handle_event(event())  # A lock-busy attempt is safe to present again.
    assert len(calls) == 1


def test_mapping_collision_and_provider_scope_isolation(relay):
    instance, _, _, _, notification = relay
    entries = instance.thread_store.notification_mappings(notification.event_fingerprint())
    before = instance.thread_store.path.read_bytes()
    with pytest.raises(LarkTransportError, match="mapping_collision"):
        instance.thread_store.cache_mappings(entries, RelayTarget("other", OTHER_THREAD, "process_local"))
    assert instance.thread_store.path.read_bytes() == before
    overseas = LarkThreadStore(instance.runtime, config(domain="lark").scope)
    overseas.cache_mappings(entries, RelayTarget("other", OTHER_THREAD, "process_local"))
    assert overseas.lookup_thread(CHAT, MESSAGE) is None and not overseas.path.exists()


def test_notification_only_does_not_create_reply_mapping_or_listener(tmp_path):
    cfg = NotificationConfig(lark=config(users=()))
    notifier = EnvironmentNotifier(tmp_path, cfg, lark_api=Api())
    notification = NotificationEvent("workspace", "waiting", "notice-only", "Waiting", "No reply needed",
                                     RelayTarget("workspace", THREAD, "process_local"))
    assert notifier.notify(notification).channel == "lark"
    assert not notifier.relay_thread_store.has_notification_mapping(notification.event_fingerprint())
    assert reply_relay_from_config(tmp_path, cfg, queue_dispatcher=None, remote_ssh_adapter=None) is None


def test_authoritative_notification_retains_lark_routing_before_clearing_fence(relay, tmp_path):
    _, notifier, _, _, notification = relay
    store = ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", clock=lambda: 100, boot_id="fixture")
    token = store.attach("local", "host", "vscode")
    fingerprint = notification.event_fingerprint()
    prepared = store.prepare_notification(token, fingerprint, fingerprint)
    store.finish_notification(token, fingerprint, prepared["operation_id"], {"status": "sent", "channel": "lark"},
                              notifier.relay_thread_store.notification_mappings(fingerprint))
    assert store.read()["external_effect"] is None
    assert store.relay_mappings()[0]["message_id"] == MESSAGE
    assert store.slack_mappings() == []


def test_slack_target_alias_and_saved_mapping_format_remain_compatible(tmp_path):
    assert SlackRelayTarget is RelayTarget
    store = SlackThreadStore(tmp_path)
    target = SlackRelayTarget("workspace", THREAD, "process_local")
    store.record_thread("C1234567890", "1789000000.100001", target, "a" * 64)
    before = store.path.read_bytes()
    assert SlackThreadStore(tmp_path).lookup_thread("C1234567890", "1789000000.100001").target == target
    assert store.path.read_bytes() == before


def test_mixed_provider_control_cache_keeps_slack_schema_and_lark_scope(relay, tmp_path):
    instance, _, _, _, notification = relay
    entry = instance.thread_store.notification_mappings(notification.event_fingerprint())[0]
    slack_entry = {"channel_id": "C1234567890", "thread_ts": "1789000000.100001", "event_fingerprint": "a" * 64}
    store = ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", clock=lambda: 100, boot_id="fixture")
    token = store.attach("local", "host", "vscode")
    store.merge_relay_mappings(store.read(), [slack_entry, entry])
    mappings = store.relay_mappings()
    assert len(mappings) == 2 and store.slack_mappings() == [dict(schema_version=1, **slack_entry)]
    local_target = RelayTarget("workspace", THREAD, "process_local")
    slack = SlackThreadStore(tmp_path / "another-runtime")
    slack.cache_mappings(mappings, local_target)
    lark = LarkThreadStore(tmp_path / "another-runtime", instance.config.scope)
    lark.cache_mappings(mappings, local_target)
    assert lark.lookup_thread(CHAT, MESSAGE).target == local_target
    assert slack.lookup_thread(slack_entry["channel_id"], slack_entry["thread_ts"]).target == local_target
    with pytest.raises(Exception, match="lark_mapping_collision"):
        store.merge_relay_mappings(store.read(), [{**entry, "event_fingerprint": "c" * 64}])
    assert store.relay_mappings() == mappings


@pytest.mark.parametrize("field,value", [("scope", "../bad"), ("chat_id", "C1234567890"),
                                        ("message_id", "bad"), ("event_fingerprint", "short")])
def test_invalid_lark_control_mapping_does_not_clear_send_fence(relay, tmp_path, field, value):
    instance, _, _, _, notification = relay
    entry = instance.thread_store.notification_mappings(notification.event_fingerprint())[0]
    entry[field] = value
    store = ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", clock=lambda: 100, boot_id="fixture")
    token = store.attach("local", "host", "vscode")
    fingerprint = notification.event_fingerprint()
    prepared = store.prepare_notification(token, fingerprint, fingerprint)
    with pytest.raises(Exception, match="lark_mapping_invalid"):
        store.finish_notification(token, fingerprint, prepared["operation_id"], {"status": "sent", "channel": "lark"}, [entry])
    assert store.read()["external_effect"] is not None and store.relay_mappings() == []


def test_confirmed_provider_receipt_recovers_common_ledger_failure_without_resend(relay):
    _, notifier, api, _, notification = relay
    # Simulates a crash after the provider receipt/mapping was durably written,
    # before the shared notification ledger was committed.
    notifier._send_lark(notification)
    assert len(api.calls) == 1
    changed = NotificationEvent(notification.workspace_id, notification.event_type, notification.transition_fingerprint,
                                "Different body under the same identity", "Body", notification.relay_target)
    with pytest.raises(LarkTransportError, match="notification_id_collision"):
        notifier._send_lark(changed)
    assert len(api.calls) == 1
