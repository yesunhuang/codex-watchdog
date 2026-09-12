from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from codex_watchdog.slack_mapping import SlackRelayTarget, SlackThreadStore
from codex_watchdog.slack_mapping import valid_slack_channel_id
from codex_watchdog.slack_relay import SlackReplyRelay
from codex_watchdog.slack_relay import SlackReplyResult as ReplyResult
from codex_watchdog import slack_presentation
from codex_watchdog.storage import FileLock, StoreBusyError


THREAD = "11111111-2222-4333-8444-555555555555"
CHANNEL = "C12345678"
USER = "U12345678"
THREAD_TS = "1760000000.000100"
MESSAGE_TS = "1760000001.000200"


class FakeQueue:
    def __init__(self, status: str = "enqueued") -> None:
        self.status = status
        self.calls = []

    def dispatch(self, thread_id: str, instruction_id: str, prompt: str, source: str):
        self.calls.append((thread_id, instruction_id, prompt, source))
        return SimpleNamespace(status=self.status)


class FakeRemoteAdapter:
    def __init__(self, result=None) -> None:
        self.result = result or {
            "status": "ok",
            "wake": {"state": "enqueued"},
        }
        self.calls = []

    def probe(self, target, **kwargs):
        self.calls.append((target, kwargs))
        return self.result


def local_target() -> SlackRelayTarget:
    return SlackRelayTarget(
        workspace_id="watchdog", thread_id=THREAD, execution_locality="process_local",
    )


def reply_event(**updates):
    value = {
        "type": "message",
        "user": USER,
        "channel": CHANNEL,
        "thread_ts": THREAD_TS,
        "ts": MESSAGE_TS,
        "client_msg_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "text": "use B, then continue",
    }
    value.update(updates)
    return value


def relay(
    tmp_path: Path,
    *,
    queue: Optional[FakeQueue] = None,
    remote: Optional[FakeRemoteAdapter] = None,
) -> SlackReplyRelay:
    return SlackReplyRelay(
        tmp_path,
        bot_token="xoxb-test-secret",
        app_token="xapp-test-secret",
        channel_id=CHANNEL,
        allowed_user_ids=(USER,),
        queue_dispatcher=queue or FakeQueue(),
        remote_ssh_adapter=remote or FakeRemoteAdapter(),
    )


def record_mapping(store: SlackThreadStore, target: SlackRelayTarget) -> None:
    store.record_thread(CHANNEL, THREAD_TS, target, "a" * 64)


def test_allowlisted_known_thread_reply_is_queued_verbatim_once(
    tmp_path: Path,
) -> None:
    queue = FakeQueue()
    service = relay(tmp_path, queue=queue)
    record_mapping(service.thread_store, local_target())

    first = service.handle_message(reply_event(), event_id="Ev12345678")
    second = service.handle_message(reply_event(), event_id="Ev12345678")

    assert first.status == "queued"
    assert first.workspace_id == "watchdog"
    assert first.delivery_status == "enqueued"
    assert second.status == "duplicate"
    assert second.duplicate is True
    assert len(queue.calls) == 1
    assert queue.calls[0][0] == THREAD
    assert queue.calls[0][2] == "use B, then continue"
    assert queue.calls[0][3] == "slack_reply"
    durable = service.thread_store.path.read_text(encoding="utf-8")
    assert "use B, then continue" not in durable
    assert json.loads(durable)["events"]


def test_unknown_thread_unauthorized_user_and_bot_messages_are_ignored(
    tmp_path: Path,
) -> None:
    queue = FakeQueue()
    service = relay(tmp_path, queue=queue)

    assert service.handle_message(reply_event()).status == "ignored_unknown_thread"
    record_mapping(service.thread_store, local_target())
    assert (
        service.handle_message(reply_event(user="U99999999")).status
        == "ignored_unauthorized"
    )
    assert (
        service.handle_message(reply_event(bot_id="B12345678")).status
        == "ignored_bot_or_subtype"
    )
    assert service.handle_message(reply_event(thread_ts=None)).status == (
        "ignored_not_thread_reply"
    )
    assert queue.calls == []


def test_uncertain_delivery_is_not_blindly_retried(tmp_path: Path) -> None:
    queue = FakeQueue(status="uncertain")
    service = relay(tmp_path, queue=queue)
    record_mapping(service.thread_store, local_target())

    first = service.handle_message(reply_event(), event_id="EvUncertain1")
    duplicate = service.handle_message(reply_event(), event_id="EvUncertain1")

    assert first.status == "uncertain"
    assert duplicate.status == "duplicate"
    assert len(queue.calls) == 1


def test_remote_reply_uses_existing_remote_adapter_and_exact_session(
    tmp_path: Path,
) -> None:
    remote = FakeRemoteAdapter()
    service = relay(tmp_path, remote=remote)
    target = SlackRelayTarget(
        workspace_id="vscode-remote-test",
        thread_id=THREAD,
        execution_locality="remote_ssh",
        remote_authority="ssh-remote+gpu-lab-personal",
        remote_repo_path="/home/user/repo",
        remote_storage_key="a" * 32,
    )
    record_mapping(service.thread_store, target)

    result = service.handle_message(reply_event(), event_id="EvRemote123")

    assert result.status == "queued"
    assert len(remote.calls) == 1
    remote_target, options = remote.calls[0]
    assert remote_target.expected_session_ids == (THREAD,)
    assert options["wake"]["prompt"] == "use B, then continue"
    assert options["wake"]["instruction_id"] == result.instruction_id


def test_only_one_socket_listener_can_own_a_runtime(tmp_path: Path) -> None:
    service = relay(tmp_path)
    listener_lock = FileLock(tmp_path / "locks" / "slack-socket-mode.lock")

    with listener_lock:
        with pytest.raises(StoreBusyError, match="already held"):
            service.start()


def test_direct_message_id_is_not_a_supported_relay_channel() -> None:
    assert valid_slack_channel_id("C12345678") is True
    assert valid_slack_channel_id("G12345678") is True
    assert valid_slack_channel_id("D12345678") is False


@pytest.mark.parametrize("system", ["win32", "linux", "darwin"])
@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.parametrize("status", ["queued", "uncertain"])
def test_reply_acknowledgement_identifies_sender_and_keeps_exact_parent(tmp_path, monkeypatch, system, controlled, status):
    monkeypatch.setattr(slack_presentation, "host_platform", system)
    monkeypatch.setattr(slack_presentation, "gethostname", lambda: "courier-a.example")
    service = relay(tmp_path)
    sends = []
    events = []
    def notify(target, token, event, notifier):
        events.append(event)
        return notifier.notify(event)
    control = (SimpleNamespace(notify=notify), object(), object()) if controlled else None
    result = ReplyResult(status, "watchdog", "instruction-a", control=control)
    service.acknowledge(reply_event(), result, SimpleNamespace(chat_postMessage=lambda **args: sends.append(args)))
    text = ("Queued for the exact existing Codex thread." if status == "queued" else
            "WatchDog could not confirm exact-thread delivery and will not blindly resend this reply.")
    prefix = "WatchDog host: courier-a.example\n" if system != "darwin" else ""
    assert sends == [dict(channel=CHANNEL, thread_ts=THREAD_TS, text=prefix + text,
                          unfurl_links=False, unfurl_media=False)]
    assert len(events) == int(controlled)
    # Ignored/duplicate events still produce no new Slack acknowledgement.
    service.acknowledge(reply_event(), ReplyResult("duplicate"),
                        SimpleNamespace(chat_postMessage=lambda **args: sends.append(args)))
    assert len(sends) == 1
