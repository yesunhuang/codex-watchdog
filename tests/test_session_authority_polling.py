"""Polling authority contract: provider reads are unlocked, ingress is fenced.

The in-memory capability isolates poller behavior from native host discovery;
session-authority storage and native handoff have their own integration tests.
"""
from contextlib import contextmanager
import copy
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

from codex_watchdog.control_state import ControlBusy
from codex_watchdog.lark_poll import LarkReplyPoller
from codex_watchdog.lark_transport import LarkTransportError
from codex_watchdog.relay import ReplyResult
from codex_watchdog.slack_poll import SlackReplyPoller
from test_lark_poll_fairness import OTHER_ROOT, Rig as LarkRig, message as lark_message
from test_slack_poll_fairness import PollRig as SlackRig


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    if sys.platform == "win32":
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            asyncio.set_event_loop(asyncio.new_event_loop())
    def forbidden(*args, **kwargs):
        pytest.fail("authority polling tests must not contact a provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class Authority:
    def __init__(self, thread_id, shared=None):
        self.thread_id = thread_id
        self.shared = shared or SimpleNamespace(epoch=1, cursors={}, lock=threading.RLock())
        self.epoch = self.shared.epoch
        self.depth = 0
        self.fail_write = None

    @contextmanager
    def guard(self):
        with self.shared.lock:
            if self.epoch != self.shared.epoch:
                raise ControlBusy("session_authority_stale_epoch")
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

    def read_cursor(self, provider, key, default):
        assert self.depth > 0
        return copy.deepcopy(self.shared.cursors.get((self.thread_id, provider, key), default))

    def write_cursor(self, provider, key, state):
        assert self.depth > 0
        if self.fail_write is not None and self.fail_write(state):
            raise OSError("fixture cursor write failed")
        self.shared.cursors[(self.thread_id, provider, key)] = copy.deepcopy(state)


def setup(tmp_path, provider):
    rig = SlackRig(tmp_path, tickets=1) if provider == "slack" else LarkRig(tmp_path)
    if provider == "lark":
        rig.add()
    relay = rig.relay
    target = rig.targets[0] if provider == "slack" else next(iter(rig.active().values()))["target"]
    thread_id = target.thread_id if provider == "slack" else target["thread_id"]
    authority = Authority(thread_id)
    relay.relay_authority = authority
    if provider == "slack":
        parent = rig.parents()[0]
        rig.pages[parent] = [rig.message(parent)]
        poller = SlackReplyPoller(relay, api=rig.api)
        cursor_key, root_key = "slack-active", rig.key(parent)
        handler_name = "handle_polled_message"
        queued = rig.deliveries
    else:
        rig.offer(lark_message())
        poller = rig.restart()
        poller.started_ms = 100000
        cursor_key, root_key = "lark-active:" + relay.config.scope, next(iter(rig.active()))
        handler_name = "handle_event"
        queued = rig.queued
    return SimpleNamespace(rig=rig, relay=relay, authority=authority, poller=poller,
        provider=provider, cursor_key=cursor_key, root_key=root_key,
        handler_name=handler_name, queued=queued)


def shared_cursor(case):
    return copy.deepcopy(case.authority.shared.cursors[
        (case.authority.thread_id, case.provider, case.cursor_key)])


def restart(case, runtime=None, authority=None):
    relay = copy.copy(case.relay)
    relay.runtime = Path(runtime or relay.runtime)
    relay.relay_authority = authority or case.authority
    if case.provider == "slack":
        return SlackReplyPoller(relay, api=case.rig.api)
    poller = LarkReplyPoller(relay, api=case.rig.api, clock=lambda: 110)
    poller._binding_discovery = SimpleNamespace(poll_once=lambda: [])
    return poller


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_handoff_retains_unread_cursor_without_reading_new_node_legacy_file(tmp_path, provider):
    c = setup(tmp_path / "login6", provider)
    setattr(c.relay, c.handler_name, lambda event: ReplyResult("deferred"))
    assert c.poller.poll_once()[0]["status"] == "deferred"
    before = shared_cursor(c)
    assert not c.queued
    delattr(c.relay, c.handler_name)
    c.authority.shared.epoch += 1
    selected = Authority(c.authority.thread_id, c.authority.shared)
    new = restart(c, tmp_path / "login3", selected)
    local = new.path if provider == "slack" else new.active_path
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("{not a valid legacy cursor}")
    assert new.poll_once()[0]["status"] == "queued"
    assert len(c.queued) == 1 and local.read_text() == "{not a valid legacy cursor}"
    with pytest.raises(ControlBusy, match="stale_epoch"):
        c.poller.poll_once()
    selected.shared.epoch += 1
    newer = Authority(selected.thread_id, selected.shared)
    assert restart(c, tmp_path / "login4", newer).poll_once() == []
    assert len(c.queued) == 1
    if provider == "slack":
        assert before["threads"] == {}
    else:
        assert before["parents"][c.root_key]["pending"] is not None


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_stale_before_tick_performs_no_provider_read_or_cursor_mutation(tmp_path, provider):
    c = setup(tmp_path, provider)
    c.authority.shared.epoch += 1
    before = copy.deepcopy(c.authority.shared.cursors)
    with pytest.raises(ControlBusy, match="stale_epoch"):
        c.poller.poll_once()
    assert c.authority.shared.cursors == before and not c.queued
    assert c.rig.calls == [] if provider == "slack" else c.rig.api.calls == []


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_handoff_during_provider_page_prevents_claim_cursor_and_ack(tmp_path, provider):
    c = setup(tmp_path, provider)
    seen = []
    original = getattr(c.relay, c.handler_name)
    setattr(c.relay, c.handler_name, lambda event: seen.append(event) or original(event))
    snapshot = {}
    if provider == "slack":
        original_api = c.rig.api
        def api(method, params):
            assert c.authority.depth == 0
            result = original_api(method, params)
            snapshot.update(cursor=shared_cursor(c))
            c.authority.shared.epoch += 1
            return result
        c.poller.api = api
    else:
        original_api = c.rig.api.thread_history
        def api(*args):
            assert c.authority.depth == 0
            result = original_api(*args)
            snapshot.update(cursor=shared_cursor(c))
            c.authority.shared.epoch += 1
            return result
        c.rig.api.thread_history = api
    with pytest.raises(ControlBusy, match="stale_epoch"):
        c.poller.poll_once()
    assert not seen and not c.queued and shared_cursor(c) == snapshot["cursor"]
    if provider == "slack":
        assert not any(method == "chat.postMessage" for method, params in c.rig.calls)
    else:
        assert c.rig.api.acks == []


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_provider_fetch_is_unlocked_but_handler_and_ack_share_authority_guard(tmp_path, provider):
    c = setup(tmp_path, provider)
    called = []
    handler = getattr(c.relay, c.handler_name)
    def handle(event):
        assert c.authority.depth > 0
        called.append("handler")
        return handler(event)
    setattr(c.relay, c.handler_name, handle)
    acknowledge = c.relay.acknowledge
    def ack(*args):
        assert c.authority.depth > 0
        called.append("ack")
        return acknowledge(*args)
    c.relay.acknowledge = ack
    if provider == "slack":
        api = c.rig.api
        def read(method, params):
            if method == "conversations.replies":
                assert c.authority.depth == 0
            else:
                assert c.authority.depth > 0
            return api(method, params)
        c.poller.api = read
    else:
        for name in ("thread_for_root", "thread_history"):
            api = getattr(c.rig.api, name)
            def read(*args, _api=api, **kwargs):
                assert c.authority.depth == 0
                return _api(*args, **kwargs)
            setattr(c.rig.api, name, read)
    assert c.poller.poll_once()[0]["status"] == "queued"
    assert called == ["handler", "ack"] and len(c.queued) == 1


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_crash_after_claim_before_cursor_write_never_dispatches_twice(tmp_path, provider):
    c = setup(tmp_path, provider)
    c.authority.fail_write = (lambda state: bool(state["threads"])) if provider == "slack" else (
        lambda state: any(parent["after_ms"] > 100000 for parent in state["parents"].values()))
    if provider == "slack":
        with pytest.raises(OSError, match="cursor write"):
            c.poller.poll_once()
    else:
        c.poller.poll_once()  # The parent fault is scoped; its ticket is already closed.
    assert len(c.queued) == 1
    c.authority.fail_write = None
    assert restart(c).poll_once() == []
    assert len(c.queued) == 1


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_shared_cursor_preserves_unknown_fields_and_session_isolation(tmp_path, provider):
    c = setup(tmp_path, provider)
    default = (dict(schema_version=1, after=None, threads={}) if provider == "slack" else
               dict(schema_version=1, scope=c.relay.config.scope, after_key=None, parents={}))
    default["future_unknown"] = {"preserve": [1, 2]}
    with c.authority.guard():
        c.authority.write_cursor(provider, c.cursor_key, default)
    other = Authority("22222222-2222-4333-8444-555555555555", c.authority.shared)
    with other.guard():
        other.write_cursor(provider, c.cursor_key, {"other_session": "unchanged"})
    assert c.poller.poll_once()[0]["status"] == "queued"
    assert shared_cursor(c)["future_unknown"] == default["future_unknown"]
    with other.guard():
        assert other.read_cursor(provider, c.cursor_key, {}) == {"other_session": "unchanged"}


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_guarded_refresh_does_not_regress_cursor_advanced_during_provider_read(tmp_path, provider):
    c = setup(tmp_path, provider)
    if provider == "slack":
        api = c.rig.api
        def read(method, params):
            result = api(method, params)
            with c.authority.guard():
                state = c.authority.read_cursor(provider, c.cursor_key, {})
                state["threads"][c.root_key] = result["messages"][0]["ts"]
                c.authority.write_cursor(provider, c.cursor_key, state)
            return result
        c.poller.api = read
    else:
        api = c.rig.api.thread_history
        def read(*args):
            result = api(*args)
            item = result["items"][0]
            with c.authority.guard():
                state = c.authority.read_cursor(provider, c.cursor_key, {})
                state["parents"][c.root_key].update(after_ms=int(item["create_time"]),
                    seen_ids=[item["message_id"]], pending=None)
                c.authority.write_cursor(provider, c.cursor_key, state)
            return result
        c.rig.api.thread_history = read
    assert c.poller.poll_once() == [] and not c.queued
    cursor = shared_cursor(c)
    if provider == "slack":
        assert cursor["threads"][c.root_key] == "1789112244.000000"
    else:
        assert cursor["parents"][c.root_key]["after_ms"] == 101000


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_bad_parent_rotates_shared_scheduler_and_preserves_unread_position(tmp_path, provider):
    c = setup(tmp_path, provider)
    if provider == "slack":
        c.relay.thread_store.record_thread("C12345678", "1789111619.000001",
            c.rig.targets[0], "f" * 64)
        bad, healthy = c.rig.parents()
        c.rig.pages[healthy] = [c.rig.message(healthy)]
        api = c.rig.api
        def read(method, params):
            if method == "conversations.replies" and params["ts"] == bad:
                raise OSError("fixture inaccessible parent")
            return api(method, params)
        c.poller.api = read
        with pytest.raises(OSError, match="inaccessible"):
            c.poller.poll_once()
        assert shared_cursor(c)["threads"] == {}
        next_poller = restart(c)
        next_poller.api = read
        assert next_poller.poll_once()[0]["status"] == "queued"
        assert c.rig.key(bad) not in shared_cursor(c)["threads"]
    else:
        c.rig.add(OTHER_ROOT)
        ordered = sorted(c.rig.active().items())
        bad_key, bad = ordered[0]
        _, healthy = ordered[1]
        c.rig.api.roots[(bad["chat_id"], bad["message_id"])] = LarkTransportError(
            "lark_history_failed_or_timed_out")
        c.rig.offer(lark_message(healthy["message_id"], mid="om_healthy000001"))
        c.poller.poll_once()
        cursor = shared_cursor(c)["parents"][bad_key]
        assert cursor["after_ms"] == 100000 and cursor["seen_ids"] == []
    assert len(c.queued) == 1


@pytest.mark.parametrize("where", ["root", "pending"])
def test_lark_handoff_during_root_or_pending_read_stops_before_ingress(tmp_path, where):
    c = setup(tmp_path, "lark")
    if where == "pending":
        setattr(c.relay, c.handler_name, lambda event: ReplyResult("deferred"))
        c.poller.poll_once()
        delattr(c.relay, c.handler_name)
    name = "thread_for_root" if where == "root" else "get_message"
    api = getattr(c.rig.api, name)
    snapshot = {}
    def read(*args, **kwargs):
        assert c.authority.depth == 0
        result = api(*args, **kwargs)
        snapshot.update(cursor=shared_cursor(c))
        c.authority.shared.epoch += 1
        return result
    setattr(c.rig.api, name, read)
    with pytest.raises(ControlBusy, match="stale_epoch"):
        c.poller.poll_once()
    assert shared_cursor(c) == snapshot["cursor"] and not c.queued and not c.rig.api.acks


def test_lark_listener_can_start_before_observer_selects_authority_without_state_write(tmp_path, monkeypatch):
    c = setup(tmp_path, "lark")
    c.authority.shared.epoch += 1  # Not yet selected for ingress.
    calls = []
    class Thread:
        def __init__(self, **kwargs):
            self.target = kwargs["target"]
        def start(self):
            calls.append("start")
        def join(self):
            calls.append("join")
    monkeypatch.setattr("codex_watchdog.lark_poll.threading.Thread", Thread)
    c.poller.start()
    assert calls == ["start"] and c.authority.shared.cursors == {}
    c.poller.close()
    assert calls == ["start", "join"]
