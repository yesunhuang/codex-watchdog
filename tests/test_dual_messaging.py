from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from codex_watchdog.control_state import ControlStore
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.relay import CombinedReplyRelays, RelayTarget, reply_relay_from_config
from codex_watchdog.storage import InstructionStore


THREAD = "11111111-2222-4333-8444-555555555555"
TARGET = RelayTarget("workspace", THREAD, "process_local")
EVENT = NotificationEvent("workspace", "stopped", "dual-example", "Stopped", "Completed", TARGET)


def config():
    return NotificationConfig(
        interactive_transport="both", slack_bot_token="xoxb-dual-fixture",
        slack_app_token="xapp-dual-fixture", slack_channel_id="C12345678",
        slack_allowed_user_ids=("U12345678",),
        lark=LarkConfig("cli_fixture000001", "dual-fixture-secret", "oc_fixture000001",
                        ("ou_fixture000001",), "feishu"))


class ProviderCalls:
    def __init__(self, failed=None):
        self.slack, self.lark, self.failed = [], [], failed

    def slack_post(self, token, method, body, timeout):
        self.slack.append(body)
        if self.failed == "slack":
            raise TimeoutError("fixture timeout")
        return {"ok": True, "channel": "C12345678", "ts": "1760000000.000100"}

    def send(self, text, operation_id, **kwargs):
        self.lark.append(text)
        if self.failed == "lark":
            raise TimeoutError("fixture timeout")
        return {"chat_id": "oc_fixture000001", "message_id": "om_notification01"}

    def notifier(self, runtime, cfg=None, **kwargs):
        return EnvironmentNotifier(runtime, cfg or config(), slack_api_post=self.slack_post,
                                   lark_api=self, **kwargs)


def test_both_is_opt_in_and_missing_provider_is_visible():
    cfg = config()
    assert cfg.interactive_relay_configured and not cfg.configuration_issues
    assert replace(cfg, interactive_transport=None).selected_interactive_transport == "slack"
    assert "lark_configuration_incomplete" in replace(cfg, lark=LarkConfig()).configuration_issues
    assert "slack_configuration_incomplete" in replace(
        cfg, slack_bot_token=None, slack_app_token=None).configuration_issues


def test_one_event_reaches_both_once_and_preserves_both_reply_routes(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_watchdog.notifications.socket.gethostname", lambda: "fixture-machine")
    calls = ProviderCalls()
    notifier = calls.notifier(tmp_path)
    result = notifier.notify(EVENT)
    assert result.status == "sent" and result.channel == "slack+lark"
    assert result.attempted_channels == ("slack", "lark")
    assert "Machine: fixture-machine" in calls.slack[0]["text"]
    assert "Machine: fixture-machine" in calls.lark[0]
    routes = notifier.relay_thread_store.notification_mappings(EVENT.event_fingerprint())
    assert len(routes) == 2 and sum(entry.get("provider") == "lark" for entry in routes) == 1
    store = ControlStore(tmp_path/"codex", THREAD, tmp_path/"repo", clock=lambda: 100, boot_id="fixture")
    token = store.attach("local", "host", "vscode")
    prepared = store.prepare_notification(token, EVENT.event_fingerprint(), EVENT.event_fingerprint())
    store.finish_notification(token, EVENT.event_fingerprint(), prepared["operation_id"], result.to_dict(), routes)
    assert len(store.relay_mappings()) == 2
    assert calls.notifier(tmp_path).notify(EVENT).status == "suppressed"
    # A rollback or explicit return to Slack does not replay the completed event.
    assert calls.notifier(tmp_path, replace(config(), interactive_transport="slack")).notify(EVENT).duplicate
    assert len(calls.slack) == len(calls.lark) == 1


@pytest.mark.parametrize("failed", ["slack", "lark"])
def test_provider_timeout_does_not_block_other_or_replay_either_after_restart(tmp_path, failed):
    calls = ProviderCalls(failed)
    result = calls.notifier(tmp_path).notify(EVENT)
    assert result.status == "delivery_failed" and not result.state_persisted
    assert result.channel == ("lark" if failed == "slack" else "slack")
    assert len(calls.slack) == len(calls.lark) == 1
    calls.failed = None
    repeated = calls.notifier(tmp_path).notify(EVENT)
    assert repeated.status == "delivery_failed" and repeated.attempted_channels == ()
    assert len(calls.slack) == len(calls.lark) == 1
    receipt = json.loads((tmp_path/"notifications/dual-deliveries.json").read_text())["events"][EVENT.event_fingerprint()]
    assert receipt[failed] == "uncertain"
    assert set(receipt.values()) == {"uncertain", "sent"}


def test_enabling_both_does_not_replay_legacy_events(tmp_path):
    calls = ProviderCalls()
    assert calls.notifier(tmp_path, replace(config(), interactive_transport="slack")).notify(EVENT).status == "sent"
    before = (tmp_path/"notifications/last-events.json").read_bytes()
    assert calls.notifier(tmp_path).notify(EVENT).duplicate
    assert len(calls.slack) == 1 and not calls.lark
    assert (tmp_path/"notifications/last-events.json").read_bytes() == before
    assert not (tmp_path/"notifications/dual-deliveries.json").exists()


def test_pre_send_journal_failure_prevents_network_and_preserves_legacy_state(tmp_path):
    calls = ProviderCalls()
    def fail(path, value):
        raise OSError("fixture disk failure")
    with pytest.raises(OSError):
        calls.notifier(tmp_path, atomic_writer=fail).notify(EVENT)
    assert not calls.slack and not calls.lark


def test_post_send_write_failure_is_not_replayed(tmp_path):
    calls = ProviderCalls()
    writes = []
    def fail_confirmation(path, value):
        writes.append(str(path))
        if len(writes) == 2:
            raise OSError("fixture confirmation failure")
        InstructionStore._atomic_json(path, value)
    with pytest.raises(OSError):
        calls.notifier(tmp_path, atomic_writer=fail_confirmation).notify(EVENT)
    result = calls.notifier(tmp_path).notify(EVENT)
    assert result.status == "delivery_failed"
    assert len(calls.slack) == len(calls.lark) == 1


def test_corrupt_dual_receipt_is_preserved_without_sending(tmp_path):
    path = tmp_path/"notifications/dual-deliveries.json"
    path.parent.mkdir()
    path.write_text('{"schema_version":2,"events":{}}')
    calls = ProviderCalls()
    with pytest.raises(ValueError):
        calls.notifier(tmp_path).notify(EVENT)
    assert not calls.slack and not calls.lark
    assert json.loads(path.read_text())["schema_version"] == 2


def test_both_provider_replies_share_exact_dispatcher_and_separate_dedupe(tmp_path):
    calls = ProviderCalls()
    notifier = calls.notifier(tmp_path)
    notifier.notify(EVENT)
    admitted = []
    queue = SimpleNamespace(dispatch=lambda *args: (admitted.append(args) or SimpleNamespace(status="enqueued")))
    relay = reply_relay_from_config(tmp_path, config(), queue_dispatcher=queue, remote_ssh_adapter=None)
    slack, lark = relay.relays
    assert len(relay.thread_store.notification_mappings(EVENT.event_fingerprint())) == 2
    slack_event = dict(type="message", user="U12345678", channel="C12345678", thread_ts="1760000000.000100",
                       ts="1760000001.000200", client_msg_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", text="Slack reply")
    lark_event = {"schema":"2.0", "header":{"event_id":"fixture-event-001", "app_id":"cli_fixture000001",
                  "event_type":"im.message.receive_v1"}, "event":{
        "sender":{"sender_type":"user", "sender_id":{"open_id":"ou_fixture000001"}},
        "message":{"message_id":"om_reply00000001", "chat_id":"oc_fixture000001", "message_type":"text",
                   "root_id":"om_notification01", "content":json.dumps({"text":"Feishu reply"})}}}
    assert slack.handle_message(slack_event, event_id="Ev12345678").status == "queued"
    assert lark.handle_event(lark_event).status == "queued"
    assert slack.handle_message(slack_event, event_id="Ev12345678").duplicate
    assert lark.handle_event(lark_event).duplicate
    assert [(item[0],item[2],item[3]) for item in admitted] == [
        (THREAD,"Slack reply","slack_reply"),(THREAD,"Feishu reply","lark_reply")]
    mirrored = reply_relay_from_config(tmp_path/"cache", config(), queue_dispatcher=queue, remote_ssh_adapter=None)
    entries = relay.thread_store.mappings_for_threads((THREAD,))
    mirrored.thread_store.cache_mappings(entries, TARGET)
    assert entries == []  # Consumed tickets must not be exported into a new active set.
    assert mirrored.thread_store.notification_mappings(EVENT.event_fingerprint()) == []
    assert relay.thread_store.has_notification_mapping(EVENT.event_fingerprint())


def test_listener_start_failure_closes_both_and_releases_first():
    lifecycle=[]
    first=SimpleNamespace(thread_store=None, start=lambda:lifecycle.append("slack-start"),
                          close=lambda:lifecycle.append("slack-close"))
    def fail():
        lifecycle.append("lark-start")
        raise RuntimeError("fixture listener failure")
    second=SimpleNamespace(thread_store=None, start=fail, close=lambda:lifecycle.append("lark-close"))
    with pytest.raises(RuntimeError, match="fixture listener failure"):
        CombinedReplyRelays([first,second]).start()
    assert lifecycle == ["slack-start","lark-start","lark-close","slack-close"]
