from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from codex_watchdog.control_state import ControlStore
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.onebot_relay import OneBotReplyRelay, OneBotThreadStore, human_message
from codex_watchdog.onebot_transport import OneBotConfig, identifier, valid_endpoint
from codex_watchdog.relay import RelayTarget, reply_relay_from_config


THREAD = "11111111-2222-4333-8444-555555555555"
OTHER = "11111111-2222-4333-8444-666666666666"
TARGET = RelayTarget("workspace", THREAD, "process_local")
CFG = OneBotConfig("ws://127.0.0.1:3001", "onebot-fixture-token", "12345", "group", "67890", ("54321",))


def incoming(*, reply=-100, message_id=200, text="literal human instruction"):
    return dict(post_type="message", self_id=12345, message_type="group", user_id=54321,
                group_id=67890, message_id=message_id, sender={"user_id": 54321},
                message=[{"type": "reply", "data": {"id": str(reply)}},
                         {"type": "text", "data": {"text": text}}])


class Api:
    def __init__(self, message_id="-100", failed=False):
        self.calls, self.message_id, self.failed = [], message_id, failed

    def send(self, text, operation_id):
        self.calls.append((text, operation_id))
        if self.failed:
            raise TimeoutError("private fixture error")
        return dict(chat_id=CFG.destination, message_id=self.message_id)


def notify(runtime, *, api=None, target=TARGET, event_id="onebot-test"):
    api = api or Api()
    event = NotificationEvent(target.workspace_id, "stopped", event_id, "Done", "Completed", target)
    notifier = EnvironmentNotifier(runtime, NotificationConfig(onebot=CFG, interactive_transport="onebot"),
                                   onebot_api=api)
    return notifier, notifier.notify(event), event, api


def relay(runtime, *, config=CFG, dispatch=None):
    calls = []
    def queued(*args):
        calls.append(args)
        return SimpleNamespace(status="enqueued")
    instance = OneBotReplyRelay(runtime, config, queue_dispatcher=SimpleNamespace(dispatch=dispatch or queued),
                               remote_ssh_adapter=SimpleNamespace())
    return instance, calls


def test_notification_machine_label_reply_and_restart_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_watchdog.notifications.socket.gethostname", lambda: "fixture-machine")
    notifier, result, event, api = notify(tmp_path)
    assert result.status == "sent" and result.channel == "onebot"
    assert "Machine: fixture-machine" in api.calls[0][0]
    instance, calls = relay(tmp_path)
    assert instance.handle_event(incoming()).status == "queued"
    restarted, again = relay(tmp_path)
    assert restarted.handle_event(incoming()).duplicate and not again
    assert calls == [(THREAD, calls[0][1], "literal human instruction", "onebot_reply")]
    assert notifier.notify(event).duplicate and len(api.calls) == 1
    assert "literal human instruction" not in instance.thread_store.path.read_text()
    assert CFG.access_token not in instance.thread_store.path.read_text()


def test_shared_bot_cannot_wake_other_machine_or_thread(tmp_path):
    first, second = tmp_path / "machine-a", tmp_path / "machine-b"
    notify(first)
    notify(second, api=Api("-101"), target=RelayTarget("other", OTHER, "process_local"))
    a, calls_a = relay(first)
    b, calls_b = relay(second)
    assert a.handle_event(incoming()).status == "queued"
    assert b.handle_event(incoming()).status == "ignored_unknown_thread"
    assert a.handle_event(incoming(reply=-101, message_id=201)).status == "ignored_unknown_thread"
    assert b.handle_event(incoming(reply=-101, message_id=201)).status == "queued"
    assert [call[0] for call in calls_a] == [THREAD]
    assert [call[0] for call in calls_b] == [OTHER]


@pytest.mark.parametrize("change", [
    lambda p: p.update(self_id=99999),
    lambda p: p.update(user_id=99999, sender={"user_id": 99999}),
    lambda p: p.update(sender={"user_id": 99999}),
    lambda p: p.update(group_id=99999),
    lambda p: p.update(post_type="message_sent"),
    lambda p: p.update(anonymous={"id": 1}),
    lambda p: p.update(message_id=True),
    lambda p: p.update(message="[CQ:reply,id=-100]command"),
    lambda p: p["message"].append({"type": "reply", "data": {"id": "-100"}}),
    lambda p: p["message"].append({"type": "image", "data": {"url": "https://invalid.example"}}),
    lambda p: p["message"].pop(0),
    lambda p: p["message"][0]["data"].update(id="-999"),
    lambda p: p["message"][1]["data"].update(text=""),
])
def test_authorization_and_exact_quote_gates(tmp_path, change):
    notify(tmp_path)
    instance, calls = relay(tmp_path)
    payload = incoming()
    change(payload)
    assert instance.handle_event(payload).status.startswith("ignored_")
    assert not calls


def test_same_message_id_with_changed_content_is_rejected(tmp_path):
    notify(tmp_path)
    instance, calls = relay(tmp_path)
    assert instance.handle_event(incoming()).status == "queued"
    assert instance.handle_event(incoming(text="altered")).status == "rejected_state_or_collision"
    assert len(calls) == 1


def test_uncertain_wake_is_not_replayed_after_restart(tmp_path):
    notify(tmp_path)
    calls = []
    def uncertain(*args):
        calls.append(args)
        raise TimeoutError()
    instance, _ = relay(tmp_path, dispatch=uncertain)
    assert instance.handle_event(incoming()).status == "uncertain"
    restarted, _ = relay(tmp_path, dispatch=uncertain)
    assert restarted.handle_event(incoming()).duplicate
    assert len(calls) == 1


def test_uncertain_outbound_is_not_replayed(tmp_path):
    api = Api(failed=True)
    notifier, result, event, _ = notify(tmp_path, api=api)
    assert result.status == "delivery_failed"
    api.failed = False
    assert notifier.notify(event).status == "delivery_failed"
    assert len(api.calls) == 1
    assert notifier.relay_thread_store.notification_mappings(event.event_fingerprint()) == []


def test_native_mapping_roundtrip_and_collision_preserve_exact_target(tmp_path):
    notifier, result, event, _ = notify(tmp_path)
    mappings = notifier.relay_thread_store.notification_mappings(event.event_fingerprint())
    owner = ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", clock=lambda: 100, boot_id="fixture")
    token = owner.attach("local", "host", "vscode")
    prepared = owner.prepare_notification(token, event.event_fingerprint(), event.event_fingerprint())
    owner.finish_notification(token, event.event_fingerprint(), prepared["operation_id"], result.to_dict(), mappings)
    destination = OneBotThreadStore(tmp_path / "native", CFG.scope)
    destination.cache_mappings(owner.relay_mappings(), TARGET)
    assert destination.lookup_thread(CFG.destination, "-100").target == TARGET
    with pytest.raises(Exception, match="collision"):
        destination.cache_mappings(owner.relay_mappings(), RelayTarget("other", THREAD, "process_local"))
    assert destination.lookup_thread(CFG.destination, "-100").target == TARGET


def test_private_text_and_bot_mention_keep_literal_text():
    payload = incoming()
    payload.update(message_type="private")
    payload.pop("group_id")
    payload["message"].append({"type": "at", "data": {"qq": "12345"}})
    value = human_message(payload, "12345")
    assert value["chat_id"] == "private:54321"
    assert value["text"] == "literal human instruction"


def test_all_providers_retain_legacy_receipts_and_routes(tmp_path):
    cfg = NotificationConfig(interactive_transport="all", onebot=CFG,
        slack_bot_token="xoxb-fixture", slack_app_token="xapp-fixture", slack_channel_id="C12345678",
        slack_allowed_user_ids=("U12345678",),
        lark=LarkConfig("cli_fixture000001", "fixture", "oc_fixture000001", ("ou_fixture000001",)))
    calls = []
    def slack(*args):
        calls.append("slack")
        return dict(ok=True, channel="C12345678", ts="1760000000.000100")
    def lark(*args):
        calls.append("lark")
        return dict(chat_id="oc_fixture000001", message_id="om_notification01")
    api = Api(failed=True)
    event = NotificationEvent("workspace", "stopped", "triple", "Done", "Completed", TARGET)
    notifier = EnvironmentNotifier(tmp_path, cfg, onebot_api=api,
                                   lark_api=SimpleNamespace(send=lark), slack_api_post=slack)
    result = notifier.notify(event)
    assert result.status == "delivery_failed" and result.channel == "slack+lark"
    assert calls == ["slack", "lark"] and len(api.calls) == 1
    api.failed = False
    assert notifier.notify(event).status == "delivery_failed"
    assert calls == ["slack", "lark"] and len(api.calls) == 1
    receipts = json.loads((tmp_path / "notifications/dual-deliveries.json").read_text())
    assert receipts["events"][event.event_fingerprint()] == {"slack": "sent", "lark": "sent"}
    instance = reply_relay_from_config(tmp_path, cfg, queue_dispatcher=SimpleNamespace(), remote_ssh_adapter=None)
    instance.thread_store.cache_mappings(notifier.relay_thread_store.notification_mappings(event.event_fingerprint()), TARGET)
    assert len(instance.relays) == 3


@pytest.mark.parametrize("value", [True, False, 1.5, "01", "-0", " 12", "1e3", str(2**63), None])
def test_identifiers_reject_coercion_and_overflow(value):
    assert identifier(value, message=True) is None


@pytest.mark.parametrize("url", ["http://localhost", "ws://user:secret@localhost", "ws://localhost/?access_token=secret", "ws://localhost/#fragment", "ws://localhost:99999", "ws://local host", None])
def test_endpoint_rejects_embedded_credentials_and_ambiguous_urls(url):
    assert not valid_endpoint(url)


def test_config_repr_does_not_include_credentials_or_private_address():
    assert CFG.configured and CFG.relay_configured
    assert CFG.ws_url not in repr(CFG) and CFG.access_token not in repr(CFG)
    assert not replace(CFG, allowed_user_ids=()).relay_configured
