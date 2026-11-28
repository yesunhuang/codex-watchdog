"""A busy observer must not consume a provider ticket before native admission."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
import subprocess
import json

import pytest

from codex_watchdog.control_context import effect_guard
from codex_watchdog.queue_wake import QueueWakeDispatcher
from codex_watchdog.slack_mapping import SlackRelayTarget
from codex_watchdog.slack_relay import SlackReplyRelay


THREAD = "11111111-2222-4333-8444-555555555555"
CHANNEL = "C12345678"
USER = "U12345678"
PARENT = "1760000000.000100"


def fixture(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0,
            "Queued message 99999999-aaaa-4bbb-8ccc-dddddddddddd for thread " + THREAD + ".", "")

    queue = QueueWakeDispatcher(tmp_path / "runtime", codex_home=tmp_path / "home",
                                codex_executable="fixture-codex", runner=runner)
    relay = SlackReplyRelay(queue.runtime, bot_token="xoxb-fixture", app_token="xapp-fixture",
        channel_id=CHANNEL, allowed_user_ids=(USER,), queue_dispatcher=queue,
        remote_ssh_adapter=None)
    relay.thread_store.record_thread(CHANNEL, PARENT,
        SlackRelayTarget("fixture", THREAD, "process_local"), "a" * 64)
    return relay, queue, calls


def message():
    return dict(type="message", user=USER, channel=CHANNEL, thread_ts=PARENT,
                ts="1760000001.000200", text="Harmless fixture instruction")


@contextmanager
def busy_observer(home, thread_id=THREAD):
    held, release = Event(), Event()

    def observe():
        with effect_guard(home, thread_id):
            held.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        observer = pool.submit(observe)
        assert held.wait(5)
        try:
            yield
        finally:
            release.set()
        observer.result()


def test_busy_observer_defers_before_claim_then_delivers_once(tmp_path):
    relay, queue, calls = fixture(tmp_path)
    with busy_observer(queue.codex_home):
        busy = relay.handle_message(message(), event_id="EvNativeBusy")
        assert busy.status == "deferred"
        assert relay.thread_store.lookup_reply("event:EvNativeBusy") is None
        assert relay.thread_store.lookup_thread(CHANNEL, PARENT) is not None
        assert not calls
        assert not list(queue.records.glob("*.json"))

    delivered = relay.handle_message(message(), event_id="EvNativeBusy")
    assert delivered.status == "queued"
    assert delivered.delivery_status == "enqueued"
    assert relay.handle_message(message(), event_id="EvNativeBusy").status == "duplicate"
    assert len(calls) == 1
    assert relay.thread_store.lookup_reply("event:EvNativeBusy")["state"] == "delivered"


def test_uncertain_external_attempt_stays_terminal_after_restart(tmp_path):
    relay, queue, calls = fixture(tmp_path)

    def failed(argv, **kwargs):
        calls.append(argv)
        raise TimeoutError("uncertain fixture transport")

    queue.runner = failed
    assert relay.handle_message(message(), event_id="EvUncertain").status == "uncertain"
    before = relay.thread_store.lookup_reply("event:EvUncertain")
    restarted = SlackReplyRelay(queue.runtime, bot_token="xoxb-fixture", app_token="xapp-fixture",
        channel_id=CHANNEL, allowed_user_ids=(USER,), queue_dispatcher=queue,
        remote_ssh_adapter=None)
    assert restarted.handle_message(message(), event_id="EvUncertain").status == "duplicate"
    assert restarted.thread_store.lookup_reply("event:EvUncertain") == before
    assert len(calls) == 1


def test_same_context_owner_hold_is_reentrant(tmp_path):
    relay, queue, calls = fixture(tmp_path)
    with effect_guard(queue.codex_home, THREAD):
        assert relay.handle_message(message(), event_id="EvReentrant").status == "queued"
    assert len(calls) == 1


def test_busy_parent_keeps_poll_cursor_and_does_not_starve_healthy_parent(tmp_path):
    from codex_watchdog.slack_poll import SlackReplyPoller
    from test_slack_poll import fixture as poll_fixture, PARENT as poll_parent, event
    relay, calls, target = poll_fixture(tmp_path)
    home = tmp_path / "home"
    relay.queue_dispatcher.codex_home = home
    other = "22222222-2222-4333-8444-555555555555"
    other_parent = "1789111612.000100"
    relay.thread_store.record_thread(CHANNEL, other_parent,
        SlackRelayTarget("healthy", other, "process_local"), "b" * 64)
    acks = []

    def api(method, params):
        if method == "chat.postMessage":
            acks.append(params)
            return dict(ok=True)
        is_other = params["ts"] == other_parent
        return dict(messages=[event(thread_ts=params["ts"],
            ts="1789112245.000100" if is_other else "1789112244.814879",
            client_msg_id="healthy" if is_other else "busy")])

    poller = SlackReplyPoller(relay, api=api)
    with busy_observer(home):
        results = poller.poll_once() + poller.poll_once()
        assert sorted(r["status"] for r in results) == ["deferred", "queued"]
        assert len(calls) == 1 and calls[0][0] == other
        busy_key = relay.thread_store.thread_key(CHANNEL, poll_parent)
        assert busy_key not in json.loads(poller.path.read_text(encoding="utf-8"))["threads"]
        assert len(acks) == 1
    assert poller.poll_once()[0]["status"] == "queued"
    assert len(calls) == 2 and calls[1][0] == target.thread_id


@pytest.mark.parametrize("mode", ("poll", "socket"))
def test_bot_busy_deferral_does_not_reserve_uuid_or_consume_grant(tmp_path, mode):
    from test_slack_bot_relay import Rig, instruction_event
    rig = Rig(tmp_path, mode)
    parent = rig.grant()
    event = instruction_event(rig, parent)
    rig.relay.queue_dispatcher.codex_home = tmp_path / "home"
    before = rig.state(parent)
    with busy_observer(rig.relay.queue_dispatcher.codex_home):
        assert rig.deliver(event)[0]["status"] == "deferred"
        rig.assert_inert(parent, before)
    assert rig.deliver(event)[0]["status"] == "bot_queued"
    assert len(rig.calls) == 1


def test_feishu_busy_deferral_leaves_ticket_and_message_unclaimed(tmp_path):
    from test_lark_relay import config, event, CHAT, MESSAGE, THREAD as lark_thread
    from codex_watchdog.lark_relay import LarkReplyRelay
    relay, queue, calls = fixture(tmp_path)
    lark = LarkReplyRelay(queue.runtime, config(), queue_dispatcher=queue,
                          remote_ssh_adapter=None)
    lark.thread_store.prepare_notification("a" * 64, "b" * 64)
    lark.thread_store.finish_notification("a" * 64, CHAT, MESSAGE,
        SlackRelayTarget("fixture", lark_thread, "process_local"))
    before = journal_rows(lark.thread_store)
    with busy_observer(queue.codex_home):
        assert lark.handle_event(event()).status == "deferred"
        assert not calls
        assert journal_rows(lark.thread_store) == before
    assert lark.handle_event(event()).status == "queued"
    assert len(calls) == 1


def test_onebot_busy_deferral_leaves_ticket_and_message_unclaimed(tmp_path):
    from test_onebot_relay import relay as onebot_fixture, incoming, CFG
    relay, _ = onebot_fixture(tmp_path / "onebot")
    _, queue, calls = fixture(tmp_path)
    relay.queue_dispatcher = queue
    relay.thread_store.prepare_notification("a" * 64, "b" * 64)
    relay.thread_store.finish_notification("a" * 64, CFG.destination, "-100",
        SlackRelayTarget("fixture", THREAD, "process_local"))
    before = journal_rows(relay.thread_store)
    with busy_observer(queue.codex_home):
        assert relay.handle_event(incoming()).status == "deferred"
        assert not calls
        assert journal_rows(relay.thread_store) == before
    assert relay.handle_event(incoming()).status == "queued"
    assert len(calls) == 1


def journal_rows(store):
    with store.journal.transaction() as db:
        return db.execute("SELECT kind,key,value,active FROM records WHERE namespace=? "
                          "ORDER BY kind,key", (store.journal.namespace,)).fetchall()
