"""Active native-thread polling with isolated provider, journal and queue fixtures."""
from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import socket
import sys
from types import SimpleNamespace
import uuid

import pytest

from codex_watchdog.lark_poll import LarkReplyPoller
from codex_watchdog.lark_relay import LarkReplyRelay
from codex_watchdog.lark_transport import LarkApi, LarkConfig, LarkTransportError
from codex_watchdog.models import sha256_text
from codex_watchdog.relay import RelayTarget, ReplyResult


CHAT = "oc_fixture000001"
OTHER_CHAT = "oc_fixture000002"
USER = "ou_fixture000001"
ROOT = "om_notification01"
OTHER_ROOT = "om_notification02"
THREAD = "11111111-2222-4333-8444-555555555555"
OTHER_THREAD = "22222222-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    if sys.platform == "win32":
        import asyncio

        # Windows needs a local self-pipe when the SDK first requests its loop.
        # Prepare only that standard fixture before denying every later connect.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            asyncio.set_event_loop(asyncio.new_event_loop())

    def forbidden(*_args, **_kwargs):
        pytest.fail("the polling fixture must not contact a provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def native_id(root):
    return "omt_" + root.removeprefix("om_")


def message(root=ROOT, *, mid="om_reply00000001", chat=CHAT, created=101000,
            text="literal reply \u98de\u4e66\n"):
    return dict(message_id=mid, chat_id=chat, root_id=root, parent_id=root,
                thread_id=native_id(root), create_time=str(created), update_time=str(created),
                updated=False, deleted=False,
                sender=dict(id=USER, id_type="open_id", sender_type="user"),
                msg_type="text", body=dict(content=json.dumps(dict(text=text))))


def page(*messages, **kwargs):
    return dict(items=list(messages), has_more=False, **kwargs)


class Api:
    """Both old chat history and the exact native-thread provider interface."""
    def __init__(self):
        self.calls, self.acks = [], []
        self.roots = {}
        self.pages = defaultdict(deque)
        self.legacy_fault = None
        self.history_items = []
        self.unrelated_history = 0
        self.history_offset = 0
        self.messages = {}

    def history(self, start, end, token=None, *, destination=None):
        chat = destination or CHAT
        self.calls.append(("history", chat, start, end, token))
        if chat == CHAT and self.legacy_fault is not None:
            raise self.legacy_fault
        if chat == CHAT and self.history_offset < self.unrelated_history:
            amount = min(50, self.unrelated_history - self.history_offset)
            items = [message("om_unmapped00001", mid="om_history%08d" % i)
                     for i in range(self.history_offset, self.history_offset + amount)]
            self.history_offset += amount
            return dict(items=items, has_more=True, page_token=str(self.history_offset))
        return page(*(m for m in self.history_items if m["chat_id"] == chat))

    def thread_for_root(self, root, *, destination=None):
        chat = destination or CHAT
        self.calls.append(("root", chat, root))
        value = self.roots.get((chat, root), native_id(root))
        if isinstance(value, Exception):
            raise value
        return value

    def thread_history(self, thread_id, token=None):
        self.calls.append(("thread", thread_id, token))
        values = self.pages[thread_id]
        value = values.popleft() if len(values) > 1 else (values[0] if values else page())
        if isinstance(value, Exception):
            raise value
        return value

    def get_message(self, message_id):
        self.calls.append(("get", message_id))
        value = self.messages[message_id]
        if isinstance(value, Exception):
            raise value
        return value

    def send(self, *args, **kwargs):
        self.acks.append((args, kwargs))


class Rig:
    def __init__(self, path):
        self.clock = [100]
        self.api = Api()
        self.queued = []
        cfg = LarkConfig("cli_fixture000001", "synthetic-secret", CHAT, (USER,))
        dispatcher = SimpleNamespace(dispatch=self.dispatch)
        self.relay = LarkReplyRelay(path, cfg, queue_dispatcher=dispatcher,
                                   remote_ssh_adapter=None, api=self.api)
        self.poller = self.restart()
        # Preserve a pre-upgrade chat cursor without resuming its history.
        self.poller._read(baseline_ms=100000)
        self.clock[0] = 110

    def dispatch(self, *args):
        self.queued.append(args)
        return SimpleNamespace(status="enqueued")

    def add(self, root=ROOT, *, chat=CHAT, thread=THREAD, created=100):
        stamp = datetime.fromtimestamp(created, timezone.utc).isoformat().replace("+00:00", "Z")
        entry = dict(provider="lark", scope=self.relay.config.scope, ticket_schema=1,
                     chat_id=chat, message_id=root, created_at=stamp,
                     event_fingerprint=sha256_text(chat + root))
        self.relay.thread_store.cache_mappings([entry], RelayTarget("workspace", thread, "process_local"))

    def offer(self, item):
        self.api.pages[item["thread_id"]].append(page(item))
        self.api.history_items.append(item)
        self.api.messages[item["message_id"]] = item

    def restart(self):
        poller = LarkReplyPoller(self.relay, api=self.api, clock=lambda: self.clock[0])
        poller._binding_discovery = SimpleNamespace(poll_once=lambda: [])
        self.poller = poller
        return poller

    def active(self):
        journal = self.relay.thread_store.journal
        with journal.transaction() as db:
            return journal.active(db)


@pytest.mark.parametrize("fault", ["api", "deferred"])
def test_faulty_default_parent_does_not_starve_other_active_chat(tmp_path, fault):
    r = Rig(tmp_path)
    r.add()
    r.add(OTHER_ROOT, chat=OTHER_CHAT, thread=OTHER_THREAD)
    bad, healthy = message(), message(OTHER_ROOT, chat=OTHER_CHAT, mid="om_reply00000002")
    r.offer(bad); r.offer(healthy)
    if fault == "api":
        error = LarkTransportError("lark_history_unavailable_check_permissions")
        r.api.legacy_fault = error
        r.api.roots[(CHAT, ROOT)] = error
    else:
        original = r.relay.handle_event
        r.relay.handle_event = lambda event: (ReplyResult("deferred")
            if event["event"]["message"]["root_id"] == ROOT else original(event))
    for _ in range(4):
        try:
            r.poller.poll_once()
        except LarkTransportError:
            pass
    assert len(r.queued) == 1
    assert r.queued[0][0] == OTHER_THREAD
    assert not any(call[0] == "history" for call in r.api.calls)


@pytest.mark.parametrize("history_size", [0, 50, 500, 5000, 10000])
def test_unrelated_channel_history_does_not_delay_an_active_reply(tmp_path, history_size):
    r = Rig(tmp_path)
    r.add()
    r.offer(message())
    r.api.unrelated_history = history_size
    old = r.poller.path.read_bytes()
    r.poller.poll_once()
    assert len(r.queued) == 1
    assert r.api.calls == [("root", CHAT, ROOT), ("thread", native_id(ROOT), None)]
    assert r.poller.path.read_bytes() == old


@pytest.mark.parametrize("chat", [CHAT, OTHER_CHAT])
@pytest.mark.parametrize("fault", ["root_api", "page_api", "handler", "ack", "deferred", "page", "message"])
def test_parent_fault_keeps_unread_position_and_healthy_parent_progress(tmp_path, chat, fault):
    r = Rig(tmp_path)
    r.add(); r.add(OTHER_ROOT, chat=chat, thread=OTHER_THREAD)
    r.offer(message()); r.offer(message(OTHER_ROOT, chat=chat, mid="om_reply00000002"))
    old = r.poller.path.read_bytes()
    if fault == "root_api":
        r.api.roots[(CHAT, ROOT)] = RuntimeError("private provider detail")
    elif fault == "page_api":
        r.api.pages[native_id(ROOT)] = deque([RuntimeError("private provider detail")])
    elif fault in ("handler", "deferred"):
        original = r.relay.handle_event
        def handle(payload):
            if payload["event"]["message"]["root_id"] == ROOT:
                if fault == "deferred": return ReplyResult("deferred")
                raise RuntimeError("private handler detail")
            return original(payload)
        r.relay.handle_event = handle
    elif fault == "ack":
        original = r.relay.acknowledge
        def ack(mid, result, chat_id):
            if mid == "om_reply00000001": raise RuntimeError("private ACK detail")
            return original(mid, result, chat_id)
        r.relay.acknowledge = ack
    elif fault == "page":
        r.api.pages[native_id(ROOT)] = deque([dict(items=[], has_more="false")])
    else:
        r.api.pages[native_id(ROOT)][0]["items"][0]["chat_id"] = OTHER_CHAT
    r.poller.poll_once()
    assert any(call[0] == OTHER_THREAD for call in r.queued)
    assert len(r.queued) == (2 if fault == "ack" else 1)
    cursor = json.loads(r.poller.active_path.read_text())
    key = r.relay.thread_store._address(CHAT, ROOT)
    if fault != "ack":
        failed = cursor["parents"][key]
        assert failed["after_ms"] == 100000 and failed["seen_ids"] == []
        assert (failed["pending"] is not None) == (fault in ("handler", "deferred"))
    r.restart().poll_once()
    assert len(r.queued) == (2 if fault == "ack" else 1)
    assert r.poller.path.read_bytes() == old
    assert "private" not in r.poller.health_path.read_text()


def test_scheduler_rotation_survives_fault_and_restart(tmp_path):
    r = Rig(tmp_path)
    r.add(); r.add(OTHER_ROOT, chat=OTHER_CHAT, thread=OTHER_THREAD)
    r.offer(message()); r.offer(message(OTHER_ROOT, chat=OTHER_CHAT, mid="om_reply00000002"))
    ordered = sorted(r.active().items())
    bad_key, bad = ordered[0]
    good_key, good = ordered[1]
    r.api.roots[(bad["chat_id"], bad["message_id"])] = LarkTransportError("lark_history_failed_or_timed_out")
    r.poller.PARENTS_PER_TICK = 1
    r.poller.poll_once()
    assert r.queued == []
    assert json.loads(r.poller.active_path.read_text())["after_key"] == bad_key
    r.restart().PARENTS_PER_TICK = 1
    r.poller.poll_once()
    assert len(r.queued) == 1 and r.queued[0][0] == good["target"]["thread_id"]
    assert json.loads(r.poller.active_path.read_text())["after_key"] == good_key


@pytest.mark.parametrize("sessions", [1, 2, 8])
def test_last_four_are_per_session_and_closed_evicted_roots_are_never_polled(tmp_path, sessions):
    r = Rig(tmp_path)
    excluded, expected = set(), set()
    for session in range(sessions):
        tid = str(uuid.UUID(int=session + 1))
        roots = ["om_session%04droot%02d" % (session, i) for i in range(5)]
        for i, root in enumerate(roots):
            r.add(root, thread=tid, created=100 + i)
        excluded.add(roots[0])  # Evicted by the existing last-four policy.
        key = r.relay.thread_store._address(CHAT, roots[1])
        journal = r.relay.thread_store.journal
        with journal.transaction() as db: assert journal.claim(db, key)
        excluded.add(roots[1])
        expected.update(roots[2:])
    assert len(r.active()) == sessions * 3
    for _ in range((sessions * 3 + 3) // 4): r.restart().poll_once()
    roots = [call[2] for call in r.api.calls if call[0] == "root"]
    assert set(roots) == expected and not set(roots) & excluded
    assert not any(call[0] == "history" for call in r.api.calls)
    assert len(roots) <= ((sessions * 3 + 3) // 4) * 4
    assert set(json.loads(r.poller.active_path.read_text())["parents"]) == set(r.active())


def test_provider_session_cap_spans_app_namespaces_without_becoming_global(tmp_path):
    r = Rig(tmp_path)
    for i in range(4): r.add("om_firstscope%04d" % i, created=100 + i)
    cfg = LarkConfig("cli_fixture000002", "synthetic-secret", OTHER_CHAT, (USER,))
    other = LarkReplyRelay(tmp_path, cfg, queue_dispatcher=SimpleNamespace(dispatch=r.dispatch),
                           remote_ssh_adapter=None, api=r.api)
    other.thread_store.cache_mappings([dict(provider="lark", scope=cfg.scope, ticket_schema=1,
        chat_id=OTHER_CHAT, message_id=OTHER_ROOT, created_at="1970-01-01T00:01:44Z",
        event_fingerprint="b" * 64)], RelayTarget("workspace", THREAD, "process_local"))
    r.add("om_othersession01", thread=OTHER_THREAD, created=105)
    assert len(r.active()) == 4  # Three from first session plus other session.
    journal = other.thread_store.journal
    with journal.transaction() as db: assert len(journal.active(db)) == 1
    r.poller.poll_once()
    assert not any(call[0] == "root" and call[2] == "om_firstscope0000" for call in r.api.calls)


def test_no_valid_ticket_makes_no_default_chat_or_thread_api_call(tmp_path):
    r = Rig(tmp_path)
    old = r.poller.path.read_bytes()
    assert r.poller.poll_once() == []
    assert r.api.calls == [] and r.queued == []
    assert r.poller.path.read_bytes() == old


def test_root_without_native_thread_waits_and_rechecks_without_chat_fallback(tmp_path):
    r = Rig(tmp_path); r.add()
    r.api.roots[(CHAT, ROOT)] = None
    assert r.poller.poll_once() == [{"status": "waiting_for_native_thread"}]
    r.restart().poll_once()
    assert r.api.calls == [("root", CHAT, ROOT), ("root", CHAT, ROOT)]
    assert not r.queued


@pytest.mark.parametrize("history_size", [0, 1, 50, 500, 10000, 100000])
def test_old_active_thread_history_before_proven_floor_has_constant_work(tmp_path, history_size):
    r = Rig(tmp_path); r.add()
    legacy = r.poller._read()
    legacy.update(after=108, future_unknown={"preserve": True})
    r.poller.path.write_text(json.dumps(legacy))
    old = r.poller.path.read_bytes()
    fresh = message(created=109000)
    tail = [message(mid="om_prior%08d" % i, created=107000 - i)
            for i in range(min(history_size, 49))]
    r.api.pages[native_id(ROOT)].append(dict(items=[fresh] + tail,
        has_more=history_size > 49, page_token="older-than-proven-floor"))
    handled = []
    original = r.relay.handle_event
    r.relay.handle_event = lambda payload: handled.append(payload) or original(payload)
    r.poller.poll_once()
    assert len(r.queued) == len(handled) == len(r.api.acks) == 1
    assert r.api.calls == [("root", CHAT, ROOT), ("thread", native_id(ROOT), None)]
    assert r.poller.path.read_bytes() == old


@pytest.mark.parametrize("floor", ["missing", "incomplete", "equal_tail"])
def test_unknown_unread_tail_saturates_without_loss_and_other_root_progresses(tmp_path, floor):
    r = Rig(tmp_path); r.add(); r.add(OTHER_ROOT, chat=OTHER_CHAT, thread=OTHER_THREAD)
    r.offer(message(OTHER_ROOT, chat=OTHER_CHAT, mid="om_reply00000002"))
    if floor == "missing": r.poller.path.unlink()
    else:
        legacy = r.poller._read()
        legacy.update(after=108)
        if floor == "incomplete": legacy.update(window_end=110, page_token="incomplete-page")
        r.poller.path.write_text(json.dumps(legacy))
    messages = [message(mid="om_unread%08d" % i, created=109049 - i) for i in range(50)]
    if floor == "equal_tail": messages[-1]["create_time"] = "108000"
    r.api.pages[native_id(ROOT)].append(dict(items=messages, has_more=True, page_token="unknown-tail"))
    r.poller.poll_once()
    key = r.relay.thread_store._address(CHAT, ROOT)
    before = json.loads(r.poller.active_path.read_text())["parents"][key]
    assert before["after_ms"] == (108000 if floor == "equal_tail" else 100000)
    assert before["seen_ids"] == [] and before["pending"] is None
    assert len(r.queued) == 1 and r.queued[0][0] == OTHER_THREAD
    assert json.loads(r.poller.health_path.read_text())["reason"] == "lark_poll_reply_window_saturated"
    r.restart().poll_once()
    assert json.loads(r.poller.active_path.read_text())["parents"][key] == before
    assert len(r.queued) == 1
    assert not any(call[0] == "thread" and call[2] is not None for call in r.api.calls)


def defer_one(r):
    original = r.relay.handle_event
    r.relay.handle_event = lambda payload: ReplyResult("deferred")
    r.poller.poll_once()
    r.relay.handle_event = original
    return json.loads(r.poller.active_path.read_text())["parents"][r.relay.thread_store._address(CHAT, ROOT)]


@pytest.mark.parametrize("flag", ["updated", "edited"])
@pytest.mark.parametrize("text", ["ordinary command", "access", "bind"])
def test_first_read_edited_reply_never_reaches_handler_or_ack(tmp_path, flag, text):
    r = Rig(tmp_path); r.add()
    edited = message(text=text)
    edited.update({flag: True, "update_time": "101001"})
    r.offer(edited)
    r.add(OTHER_ROOT, chat=OTHER_CHAT, thread=OTHER_THREAD)
    r.offer(message(OTHER_ROOT, chat=OTHER_CHAT, mid="om_reply00000002"))
    original = r.relay.handle_event
    handled = []
    def handle(payload):
        handled.append(payload["event"]["message"]["message_id"])
        return original(payload)
    r.relay.handle_event = handle
    results = r.poller.poll_once()
    key = r.relay.thread_store._address(CHAT, ROOT)
    cursor = json.loads(r.poller.active_path.read_text())["parents"][key]
    assert {"status": "ignored_edited"} in results
    assert handled == ["om_reply00000002"]
    assert len(r.queued) == len(r.api.acks) == 1 and r.queued[0][0] == OTHER_THREAD
    assert key in r.active() and cursor["pending"] is None
    assert cursor["after_ms"] == 101000 and cursor["seen_ids"] == ["om_reply00000001"]
    r.restart().poll_once()
    assert len(r.queued) == len(r.api.acks) == 1


@pytest.mark.parametrize("echo_update_time", [True, False])
def test_unedited_provider_reply_preserves_echoed_timestamp_without_edit_inference(tmp_path, echo_update_time):
    r = Rig(tmp_path); r.add()
    unedited = message()
    if not echo_update_time:
        unedited.pop("update_time")
    r.offer(unedited)
    original = r.relay.handle_event
    payloads = []
    def handle(payload):
        payloads.append(payload["event"]["message"])
        return original(payload)
    r.relay.handle_event = handle
    assert r.poller.poll_once()[0]["status"] == "queued"
    assert len(r.queued) == len(r.api.acks) == 1
    assert payloads[0]["edited"] is False and payloads[0]["updated"] is False
    assert payloads[0]["update_time"] == unedited.get("update_time")
    r.restart().poll_once()
    assert len(r.queued) == len(r.api.acks) == 1


@pytest.mark.parametrize("change", ["updated", "edited", "update_time", "edit_then_revert"])
def test_pending_same_body_edit_metadata_change_retains_exact_unread_identity(tmp_path, change):
    r = Rig(tmp_path); r.add(); r.offer(message())
    before = defer_one(r)
    altered = json.loads(json.dumps(r.api.messages["om_reply00000001"]))
    if change in ("updated", "edited"):
        altered[change] = True
    else:
        altered["update_time"] = "101001"
        # Reverting the text and edit flag still leaves a changed update time.
        if change == "edit_then_revert":
            altered["updated"] = False
    r.api.messages["om_reply00000001"] = altered
    r.add(OTHER_ROOT, chat=OTHER_CHAT, thread=OTHER_THREAD)
    r.offer(message(OTHER_ROOT, chat=OTHER_CHAT, mid="om_reply00000002", created=110001))
    r.api.calls.clear()
    r.restart().poll_once()
    key = r.relay.thread_store._address(CHAT, ROOT)
    assert json.loads(r.poller.active_path.read_text())["parents"][key] == before
    assert key in r.active() and len(r.queued) == len(r.api.acks) == 1
    assert r.queued[0][0] == OTHER_THREAD
    assert r.api.calls.count(("get", "om_reply00000001")) == 1
    assert not any(call[0] == "thread" and call[1] == native_id(ROOT) for call in r.api.calls)
    assert json.loads(r.poller.health_path.read_text())["reason"] == "lark_poll_pending_changed"


@pytest.mark.parametrize("flag", ["updated", "edited"])
@pytest.mark.parametrize("value", ["true", 1, None])
def test_malformed_provider_edit_flag_rejects_whole_page_and_rotates_healthy_root(tmp_path, flag, value):
    r = Rig(tmp_path); r.add()
    valid, invalid = message(), message(mid="om_reply00000003", created=102000)
    invalid[flag] = value
    r.offer(valid)
    r.api.pages[native_id(ROOT)] = deque([page(invalid, valid)])
    r.add(OTHER_ROOT, chat=OTHER_CHAT, thread=OTHER_THREAD)
    r.offer(message(OTHER_ROOT, chat=OTHER_CHAT, mid="om_reply00000002"))
    r.poller.poll_once()
    key = r.relay.thread_store._address(CHAT, ROOT)
    cursor = json.loads(r.poller.active_path.read_text())["parents"][key]
    assert key in r.active() and cursor["after_ms"] == 100000
    assert cursor["seen_ids"] == [] and cursor["pending"] is None
    assert len(r.queued) == len(r.api.acks) == 1 and r.queued[0][0] == OTHER_THREAD
    assert json.loads(r.poller.health_path.read_text())["reason"] == "lark_history_message_invalid"


def test_deferred_exact_reply_is_retrieved_after_it_leaves_newest_page_and_restart(tmp_path):
    r = Rig(tmp_path); r.add(); r.offer(message())
    pending = defer_one(r)
    assert pending["after_ms"] == 100000 and pending["pending"]["message_id"] == "om_reply00000001"
    assert "literal reply" not in r.poller.active_path.read_text()
    r.api.pages[native_id(ROOT)] = deque([dict(items=[message(mid="om_newnoise%08d" % i,
        created=109000 - i) for i in range(50)], has_more=True, page_token="now-off-page")])
    r.api.calls.clear()
    r.restart().poll_once()
    assert r.api.calls == [("get", "om_reply00000001")]
    assert len(r.queued) == len(r.api.acks) == 1
    assert r.queued[0][2] == "literal reply \u98de\u4e66\n"
    assert not json.loads(r.poller.active_path.read_text())["parents"]
    r.restart().poll_once()
    assert len(r.queued) == 1


@pytest.mark.parametrize("change", ["body", "deleted", "edited", "chat", "root", "thread", "message_id", "time", "api"])
def test_pending_identity_change_fails_closed_without_loss_and_healthy_root_progresses(tmp_path, change):
    r = Rig(tmp_path); r.add(); r.offer(message())
    before = defer_one(r)
    altered = json.loads(json.dumps(r.api.messages["om_reply00000001"]))
    if change == "body": altered["body"]["content"] = '{"text":"changed private prompt"}'
    elif change == "deleted": altered["deleted"] = True
    elif change == "edited": altered["edited"] = True
    elif change == "chat": altered["chat_id"] = OTHER_CHAT
    elif change == "root": altered["root_id"] = OTHER_ROOT
    elif change == "thread": altered["thread_id"] = native_id(OTHER_ROOT)
    elif change == "message_id": altered["message_id"] = "om_different0001"
    elif change == "time": altered["create_time"] = "102000"
    else: altered = LarkTransportError("lark_history_failed_or_timed_out")
    r.api.messages["om_reply00000001"] = altered
    r.add(OTHER_ROOT, chat=OTHER_CHAT, thread=OTHER_THREAD)
    r.offer(message(OTHER_ROOT, chat=OTHER_CHAT, mid="om_reply00000002", created=110001))
    r.restart().poll_once()
    key = r.relay.thread_store._address(CHAT, ROOT)
    assert json.loads(r.poller.active_path.read_text())["parents"][key] == before
    assert len(r.queued) == 1 and r.queued[0][0] == OTHER_THREAD
    assert "changed private prompt" not in r.poller.active_path.read_text()


def test_pending_remains_deferred_with_only_one_exact_lookup_each_tick(tmp_path):
    r = Rig(tmp_path); r.add(); r.offer(message())
    before = defer_one(r)
    r.relay.handle_event = lambda _payload: ReplyResult("deferred")
    r.api.calls.clear()
    for _ in range(3): r.restart().poll_once()
    assert r.api.calls == [("get", "om_reply00000001")] * 3
    assert json.loads(r.poller.active_path.read_text())["parents"][r.relay.thread_store._address(CHAT, ROOT)] == before
    assert r.queued == []


@pytest.mark.parametrize("where", ["root", "page", "pending"])
def test_actual_run_applies_provider_wide_rate_limit_backoff(tmp_path, where):
    r = Rig(tmp_path); r.add()
    if where == "pending":
        r.offer(message()); defer_one(r)
    r.add(OTHER_ROOT, chat=OTHER_CHAT, thread=OTHER_THREAD)
    ordered = sorted(r.active().items())
    bad_key, bad = ordered[0]
    if where == "pending":
        # Select the pending root first without changing its unread state.
        state = json.loads(r.poller.active_path.read_text())
        bad_key = r.relay.thread_store._address(CHAT, ROOT)
        bad = r.active()[bad_key]
        pending_index = [key for key, _ in ordered].index(bad_key)
        state["after_key"] = ordered[pending_index - 1][0]
        r.poller.active_path.write_text(json.dumps(state))
    error = LarkTransportError("lark_history_rate_limited")
    if where == "root": r.api.roots[(bad["chat_id"], bad["message_id"])] = error
    elif where == "page": r.api.pages[native_id(bad["message_id"])] = deque([error])
    else: r.api.messages["om_reply00000001"] = error
    r.api.calls.clear()
    waits = []
    def wait(delay):
        waits.append(delay); r.poller.stop.set()
    r.poller.stop.wait = wait
    r.poller._run()
    assert waits == [60] and r.queued == []
    assert json.loads(r.poller.health_path.read_text())["reason"] == "lark_history_rate_limited"
    assert json.loads(r.poller.active_path.read_text())["after_key"] == bad_key
    assert not any(call[0] == "root" and call[2] != bad["message_id"] for call in r.api.calls)


def test_unknown_cursor_fields_and_legacy_bytes_survive_restart(tmp_path):
    r = Rig(tmp_path); r.add(); r.poller.poll_once()
    key = r.relay.thread_store._address(CHAT, ROOT)
    state = json.loads(r.poller.active_path.read_text())
    state["future_unknown"] = {"keep": [1, 2]}
    state["parents"][key]["future_parent"] = "preserve"
    r.poller.active_path.write_text(json.dumps(state))
    legacy = r.poller.path.read_bytes()
    r.restart().poll_once()
    after = json.loads(r.poller.active_path.read_text())
    assert after["future_unknown"] == {"keep": [1, 2]}
    assert after["parents"][key]["future_parent"] == "preserve"
    assert r.poller.path.read_bytes() == legacy


def test_first_adoption_without_old_cursor_does_not_replay_prestartup_reply(tmp_path):
    r = Rig(tmp_path); r.add(); r.poller.path.unlink()
    r.poller.started_ms = 100500
    r.offer(message(created=100000))
    r.poller.poll_once()
    cursor = json.loads(r.poller.active_path.read_text())["parents"][r.relay.thread_store._address(CHAT, ROOT)]
    assert cursor["not_before_ms"] == cursor["after_ms"] == 100500
    assert not r.queued and not r.poller.path.exists()


def test_incomplete_legacy_window_keeps_startup_floor_but_not_unproven_after(tmp_path):
    r = Rig(tmp_path); r.add()
    legacy = r.poller._read()
    legacy.update(not_before_ms=100500, after=108, window_end=110, page_token="unfinished")
    r.poller.path.write_text(json.dumps(legacy))
    before = r.poller.path.read_bytes()
    r.offer(message(created=100250))
    r.poller.poll_once()
    key = r.relay.thread_store._address(CHAT, ROOT)
    cursor = json.loads(r.poller.active_path.read_text())["parents"][key]
    assert cursor["not_before_ms"] == cursor["after_ms"] == 100500 and not r.queued
    r.api.pages[native_id(ROOT)] = deque([page(message(created=101000))])
    r.restart().poll_once()  # Existing active floor survives the later start.
    assert len(r.queued) == 1 and r.poller.path.read_bytes() == before


@pytest.mark.parametrize("corruption", ["scope", "chat", "json", "time"])
def test_bad_legacy_chat_cursor_is_local_and_does_not_starve_healthy_chat(tmp_path, corruption):
    r = Rig(tmp_path); r.add(); r.add(OTHER_ROOT, chat=OTHER_CHAT, thread=OTHER_THREAD)
    r.offer(message()); r.offer(message(OTHER_ROOT, chat=OTHER_CHAT, mid="om_reply00000002"))
    legacy = r.poller._read()
    if corruption == "scope": legacy["scope"] = "f" * 64
    elif corruption == "chat": legacy["chat_sha256"] = sha256_text(OTHER_CHAT)
    elif corruption == "time": legacy["after"] = True
    r.poller.path.write_text("{not-json" if corruption == "json" else json.dumps(legacy))
    before = r.poller.path.read_bytes()
    r.poller.poll_once()
    assert len(r.queued) == 1 and r.queued[0][0] == OTHER_THREAD
    assert r.poller.path.read_bytes() == before
    assert json.loads(r.poller.health_path.read_text())["reason"] == "lark_poll_cursor_invalid"
    r.restart().poll_once()
    assert len(r.queued) == 1 and r.poller.path.read_bytes() == before
    assert not any(call[0] == "root" and call[1] == CHAT for call in r.api.calls)


def test_all_active_floors_are_retained_before_first_visit_beyond_tick_budget(tmp_path):
    r = Rig(tmp_path)
    for session in range(3):
        for i in range(4):
            r.add("om_budget%04droot%02d" % (session, i), thread=str(uuid.UUID(int=session + 1)))
    ordered = sorted(r.active().items())
    bad_key, bad = ordered[0]
    good_key, good = ordered[1]
    r.api.roots[(CHAT, bad["message_id"])] = LarkTransportError("lark_history_failed_or_timed_out")
    r.offer(message(good["message_id"], mid="om_unvisited0001"))
    r.poller.PARENTS_PER_TICK = 1
    r.poller.poll_once()
    state = json.loads(r.poller.active_path.read_text())
    assert len(state["parents"]) == 12 and state["after_key"] == bad_key
    assert state["parents"][good_key]["after_ms"] == 100000
    r.restart().PARENTS_PER_TICK = 1
    r.poller.poll_once()
    assert len(r.queued) == 1 and r.queued[0][0] == good["target"]["thread_id"]
    assert len(r.active()) == 11


def test_ticket_closed_during_exact_root_lookup_is_not_listed_or_reopened(tmp_path):
    r = Rig(tmp_path); r.add(); r.offer(message())
    original = r.api.thread_for_root
    key = r.relay.thread_store._address(CHAT, ROOT)
    def close(root, **kwargs):
        journal = r.relay.thread_store.journal
        with journal.transaction() as db: assert journal.claim(db, key)
        return original(root, **kwargs)
    r.api.thread_for_root = close
    r.poller.poll_once()
    assert r.api.calls == [("root", CHAT, ROOT)]
    assert not r.queued and not json.loads(r.poller.active_path.read_text())["parents"]
    r.restart().poll_once()
    assert r.api.calls == [("root", CHAT, ROOT)]


@pytest.mark.parametrize("change", ["boolean_version", "missing_after_key", "missing_thread_id", "missing_pending"])
def test_new_active_cursor_requires_explicit_schema_and_fields_without_rewriting(tmp_path, change):
    r = Rig(tmp_path); r.add(); r.poller.poll_once()
    state = json.loads(r.poller.active_path.read_text())
    key = r.relay.thread_store._address(CHAT, ROOT)
    if change == "boolean_version": state["schema_version"] = True
    elif change == "missing_after_key": state.pop("after_key")
    elif change == "missing_thread_id": state["parents"][key].pop("thread_id")
    else: state["parents"][key].pop("pending")
    r.poller.active_path.write_text(json.dumps(state))
    before = r.poller.active_path.read_bytes()
    r.api.calls.clear()
    with pytest.raises(LarkTransportError, match="^lark_poll_cursor_invalid$"):
        r.restart().poll_once()
    assert r.poller.active_path.read_bytes() == before and r.api.calls == [] and not r.queued


@pytest.mark.parametrize("fault", ["api", "malformed", "rate_limit"])
def test_actual_pending_binding_discovery_fault_preserves_healthy_active_progress(tmp_path, fault):
    from codex_watchdog.lark_binding import LarkBindingDiscovery
    r = Rig(tmp_path); r.add(); r.offer(message())
    # Model an explicitly pending, short-lived binding; its real discovery loop
    # remains separate from the exact-root reply loop and keeps its own bounds.
    r.relay.binding.store.pending = lambda: [dict(key="c" * 64, created_at=100, expires_at=200)]
    r.api.conversations = lambda: [(CHAT, "Synthetic pending binding conversation")]
    r.poller._binding_discovery = LarkBindingDiscovery(r.relay, r.api, clock=lambda: r.clock[0])
    if fault == "api": r.api.legacy_fault = LarkTransportError("lark_history_unavailable_check_permissions")
    elif fault == "rate_limit": r.api.legacy_fault = LarkTransportError("lark_history_rate_limited")
    else: r.api.history_items = [None]
    legacy = r.poller.path.read_bytes()
    if fault == "rate_limit":
        waits = []
        def wait(delay): waits.append(delay); r.poller.stop.set()
        r.poller.stop.wait = wait
        r.poller._run()
        assert waits == [60]
    else:
        r.poller.poll_once()
    assert len(r.queued) == len(r.api.acks) == 1
    assert r.api.calls[:2] == [("root", CHAT, ROOT), ("thread", native_id(ROOT), None)]
    assert len([call for call in r.api.calls if call[0] == "history"]) == 1
    assert r.poller.path.read_bytes() == legacy
    assert not json.loads(r.poller.active_path.read_text())["parents"]
    assert json.loads(r.poller.health_path.read_text())["status"] == "retrying"


class SdkClient:
    def __init__(self):
        self.calls = []
        self.root = dict(message_id=ROOT, chat_id=CHAT, root_id=None, parent_id=None,
                         deleted=False, thread_id=native_id(ROOT))
        self.status, self.success = 200, True
        self.data = page()
        self.im = SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(get=self.get, list=self.list)))

    def response(self, data):
        return SimpleNamespace(success=lambda: self.success,
            raw=SimpleNamespace(status_code=self.status, content=json.dumps(dict(data=data)).encode()))

    def get(self, request):
        self.calls.append(("get", request))
        return self.response(dict(items=[self.root]))

    def list(self, request):
        self.calls.append(("list", request))
        return self.response(self.data)


def sdk_api(client):
    return LarkApi(LarkConfig("cli_fixture000001", "synthetic-secret", CHAT, (USER,)), client=client)


def test_real_sdk_builders_scope_get_to_root_and_list_to_native_thread_only():
    client = SdkClient(); api = sdk_api(client)
    tid = api.thread_for_root(ROOT, destination=CHAT)
    assert tid == native_id(ROOT)
    assert api.thread_history(tid) == page()
    get, request = client.calls[0]
    assert get == "get" and request.paths == {"message_id": ROOT} and request.user_id_type == "open_id"
    action, request = client.calls[1]
    assert action == "list" and request.container_id_type == "thread" and request.container_id == tid
    assert request.sort_type == "ByCreateTimeDesc" and request.page_size == 50
    assert request.start_time is None and request.end_time is None and request.page_token is None
    api.thread_history(tid, "opaque-next-page")
    assert client.calls[-1][1].page_token == "opaque-next-page"


@pytest.mark.parametrize("field,value", [("chat_id", OTHER_CHAT), ("message_id", OTHER_ROOT),
    ("deleted", True), ("deleted", "false"), ("root_id", OTHER_ROOT), ("parent_id", OTHER_ROOT),
    ("thread_id", "invalid"), ("thread_id", 7)])
def test_exact_root_sdk_resolution_rejects_malformed_or_foreign_identity(field, value):
    client = SdkClient(); client.root[field] = value
    with pytest.raises(LarkTransportError): sdk_api(client).thread_for_root(ROOT, destination=CHAT)
    assert len(client.calls) == 1 and client.calls[0][0] == "get"


@pytest.mark.parametrize("thread_id", [None, ""])
def test_exact_root_sdk_resolution_missing_thread_is_waiting_metadata(thread_id):
    client = SdkClient(); client.root["thread_id"] = thread_id
    assert sdk_api(client).thread_for_root(ROOT) is None
    assert len(client.calls) == 1


@pytest.mark.parametrize("status,success,code", [(429, True, "lark_history_rate_limited"),
    (403, False, "lark_history_unavailable_check_permissions"),
    (200, False, "lark_history_unavailable_check_permissions"),
    (True, True, "lark_history_unavailable_check_permissions")])
@pytest.mark.parametrize("operation", ["root", "page"])
def test_sdk_scoped_failure_keeps_fixed_safe_diagnostics(status, success, code, operation):
    client = SdkClient(); client.status, client.success = status, success
    api = sdk_api(client)
    with pytest.raises(LarkTransportError, match="^" + code + "$"):
        if operation == "root": api.thread_for_root(ROOT)
        else: api.thread_history(native_id(ROOT))


@pytest.mark.parametrize("operation,value", [("get", "bad"), ("root", "bad"),
    ("thread", "bad"), ("thread", "oc_fixture000001"), ("destination", "bad"), ("token", "")])
def test_invalid_scoped_request_is_rejected_before_sdk_call(operation, value):
    client = SdkClient(); api = sdk_api(client)
    with pytest.raises(LarkTransportError):
        if operation == "get": api.get_message(value)
        elif operation == "root": api.thread_for_root(value)
        elif operation == "thread": api.thread_history(value)
        elif operation == "destination": api.thread_for_root(ROOT, destination=value)
        else: api.thread_history(native_id(ROOT), value)
    assert client.calls == []
