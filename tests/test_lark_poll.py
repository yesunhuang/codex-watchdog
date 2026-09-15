from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from codex_watchdog.lark_poll import LarkReplyPoller
from codex_watchdog.lark_relay import LarkReplyRelay
from codex_watchdog.lark_transport import LarkConfig, LarkTransportError
from codex_watchdog.relay import RelayTarget
from codex_watchdog.storage import FileLock, StoreBusyError


CHAT = "oc_fixture000001"
USER = "ou_fixture000001"
ROOT = "om_notification01"
THREAD = "11111111-2222-4333-8444-555555555555"
OTHER = "22222222-2222-4333-8444-555555555555"


def message(mid="om_reply00000001", parent=ROOT, created=101000, text="  reply \u98de\u4e66\n"):
    return dict(message_id=mid, root_id=parent, parent_id=parent, chat_id=CHAT,
                create_time=str(created), update_time=str(created + 100), updated=True,
                deleted=False, msg_type="text", sender=dict(id=USER, id_type="open_id", sender_type="user"),
                body=dict(content=json.dumps(dict(text=text))))


class Api:
    def __init__(self):
        self.pages = []
        self.calls = []
        self.acks = []

    def history(self, *args):
        self.calls.append(args)
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def send(self, *args, **kwargs):
        self.acks.append((args, kwargs))


def setup(root, *, parent=ROOT, tid=THREAD, api=None):
    api = api or Api()
    cfg = LarkConfig("cli_fixture000001", "fixture-secret", CHAT, (USER,))
    calls = []
    queue = SimpleNamespace(dispatch=lambda *args: calls.append(args) or SimpleNamespace(status="enqueued"))
    relay = LarkReplyRelay(root, cfg, queue_dispatcher=queue, remote_ssh_adapter=None, api=api)
    relay.thread_store.cache_mappings([dict(provider="lark", scope=cfg.scope, chat_id=CHAT,
        message_id=parent, event_fingerprint="a" * 64)], RelayTarget("workspace", tid, "process_local"))
    clock = [100]
    poller = LarkReplyPoller(relay, api=api, clock=lambda: clock[0])
    poller._read()
    clock[0] = 110
    return relay, poller, api, calls, clock


def page(*messages, **fields):
    return dict(items=list(messages), has_more=False, **fields)


def test_two_runtimes_same_app_chat_route_only_their_own_notifications(tmp_path):
    a, ap, aa, ac, _ = setup(tmp_path / "a")
    b, bp, ba, bc, _ = setup(tmp_path / "b", parent="om_notification02", tid=OTHER)
    history = page(message(), message("om_reply00000002", "om_notification02"),
                   message("om_reply00000003", "om_unmapped00001"))
    aa.pages.append(history); ba.pages.append(history)
    ap.poll_once(); bp.poll_once()
    assert len(ac) == len(bc) == 1
    assert ac[0][0] == THREAD and bc[0][0] == OTHER
    assert ac[0][2] == "  reply \u98de\u4e66\n"
    assert len(aa.acks) == len(ba.acks) == 1
    assert not a.thread_store.lookup_thread(CHAT, "om_notification02")
    assert not b.thread_store.lookup_thread(CHAT, ROOT)


def test_overlap_restart_and_socket_event_cannot_duplicate_a_reply(tmp_path):
    relay, poller, api, calls, clock = setup(tmp_path)
    m = message()
    api.pages.append(page(m)); poller.poll_once()
    restarted = LarkReplyPoller(relay, api=api, clock=lambda: 115)
    api.pages.append(page(message(created=104000)))
    assert restarted.poll_once()[0]["duplicate"]
    payload = {"schema": "2.0", "header": {"app_id": relay.config.app_id,
        "event_id": "original-socket-id", "event_type": "im.message.receive_v1"}, "event": {
        "sender": {"sender_type": "user", "sender_id": {"open_id": USER}},
        "message": dict(message_id=m["message_id"], chat_id=CHAT, message_type="text",
                        root_id=ROOT, parent_id=ROOT, content=m["body"]["content"])}}
    assert relay.handle_event(payload).duplicate
    assert len(calls) == len(api.acks) == 1


def test_first_poll_transition_does_not_replay_historical_commands(tmp_path):
    relay, poller, api, calls, clock = setup(tmp_path)
    api.pages.append(page(message(created=100000)))
    poller.started_ms = 100500
    poller.path.unlink()  # Isolated fixture creates its first cursor half a second later.
    poller.poll_once()
    assert not calls
    assert poller._read()["not_before_ms"] == 100500


def test_previously_delivered_socket_receipt_is_reused_in_place(tmp_path):
    relay, poller, api, calls, _ = setup(tmp_path)
    m = message()
    payload = {"schema": "2.0", "header": {"app_id": relay.config.app_id,
        "event_id": "old-socket-delivery", "event_type": "im.message.receive_v1"}, "event": {
        "sender": {"sender_type": "user", "sender_id": {"open_id": USER}},
        "message": dict(message_id=m["message_id"], chat_id=CHAT, message_type="text",
                        root_id=ROOT, parent_id=ROOT, content=m["body"]["content"])}}
    assert relay.handle_event(payload).status == "queued"
    before = relay.thread_store.path.read_bytes()
    api.pages.append(page(m))
    assert poller.poll_once()[0]["duplicate"]
    assert relay.thread_store.path.read_bytes() == before
    assert len(calls) == 1 and not api.acks


def test_page_token_and_window_are_retained_across_restart(tmp_path):
    relay, poller, api, calls, clock = setup(tmp_path)
    api.pages.append(dict(items=[message()], has_more=True, page_token="fixture-token"))
    poller.poll_once()
    restarted = LarkReplyPoller(relay, api=api, clock=lambda: 1000)
    api.pages.append(page(message("om_reply00000002", created=102000)))
    restarted.poll_once()
    assert api.calls == [(100, 108, None), (100, 108, "fixture-token")]
    assert restarted._read()["after"] == 108 and len(calls) == 2


@pytest.mark.parametrize("change", ["chat", "message_id", "timestamp", "sender", "deleted", "page"])
def test_invalid_entire_page_does_not_partially_dispatch_or_advance(tmp_path, change):
    relay, poller, api, calls, _ = setup(tmp_path)
    bad = message("om_reply00000002")
    if change == "chat": bad["chat_id"] = "oc_otherchat0001"
    elif change == "message_id": bad["message_id"] = "bad"
    elif change == "timestamp": bad["create_time"] = "99000"
    elif change == "sender": bad["sender"] = None
    elif change == "deleted": bad["deleted"] = "false"
    data = page(message(), bad)
    if change == "page": data["has_more"] = True
    before = poller.path.read_bytes()
    api.pages.append(data)
    with pytest.raises(LarkTransportError): poller.poll_once()
    assert poller.path.read_bytes() == before and calls == []


@pytest.mark.parametrize("change", ["user", "bot", "deleted", "parent", "kind", "empty", "id_type"])
def test_poll_keeps_sender_mapping_and_content_gates(tmp_path, change):
    _, poller, api, calls, _ = setup(tmp_path)
    m = message()
    if change == "user": m["sender"]["id"] = "ou_intruder00001"
    elif change == "bot": m["sender"]["sender_type"] = "app"
    elif change == "deleted": m["deleted"] = True
    elif change == "parent": m["root_id"] = m["parent_id"] = None
    elif change == "kind": m["msg_type"] = "image"
    elif change == "empty": m["body"]["content"] = '{"text":""}'
    elif change == "id_type": m["sender"]["id_type"] = "user_id"
    api.pages.append(page(m)); poller.poll_once()
    assert not calls and not api.acks


def test_busy_mapping_store_retries_page_before_admission(tmp_path):
    relay, poller, api, calls, _ = setup(tmp_path)
    before = poller.path.read_bytes()
    api.pages.extend([page(message()), page(message())])
    with FileLock(relay.thread_store.lock_path):
        assert poller.poll_once()[0]["status"] == "deferred"
    assert poller.path.read_bytes() == before
    poller.poll_once()
    assert len(calls) == 1


def test_uncertain_dispatch_and_later_edit_never_repeat_command(tmp_path):
    relay, poller, api, calls, clock = setup(tmp_path)
    def fail(*args):
        calls.append(args)
        raise RuntimeError("private failure")
    relay.queue_dispatcher.dispatch = fail
    api.pages.append(page(message(created=104000))); poller.poll_once()
    clock[0] = 115
    api.pages.append(page(message(created=104000, text="edited command"))); poller.poll_once()
    assert len(calls) == 1 and not api.acks
    saved = "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    assert "private failure" not in saved and "edited command" not in saved


def test_history_failure_keeps_cursor_and_records_safe_health(tmp_path):
    _, poller, api, calls, _ = setup(tmp_path)
    before = poller.path.read_bytes()
    api.pages.append(LarkTransportError("lark_history_unavailable_check_permissions"))
    poller.stop.wait = lambda _: poller.stop.set()
    poller._run()
    assert poller.path.read_bytes() == before and not calls
    assert json.loads(poller.health_path.read_text())["reason"] == "lark_history_unavailable_check_permissions"


def test_default_poller_does_not_open_socket_and_owns_listener_lock(tmp_path):
    relay, _, api, _, _ = setup(tmp_path)
    relay.connection_factory = lambda *args: pytest.fail("must not compete for socket events")
    relay.start(); relay.start()
    try:
        assert relay._poller is not None and relay._connection is None
        with pytest.raises(StoreBusyError):
            with FileLock(relay._listener_lock.path): pass
    finally:
        relay.close(); relay.close()
    assert relay._listener_lock is None and relay._poller is None


def test_reply_mode_environment_is_explicit_and_validated():
    assert LarkConfig.from_environment({}).reply_mode == "poll"
    assert LarkConfig.from_environment({"CODEX_WATCHDOG_LARK_REPLY_MODE": "socket"}).reply_mode == "socket"
    with pytest.raises(ValueError):
        LarkConfig.from_environment({"CODEX_WATCHDOG_LARK_REPLY_MODE": "broadcast"})
