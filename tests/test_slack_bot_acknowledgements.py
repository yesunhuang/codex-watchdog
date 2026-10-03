"""Sender-visible bot command receipts with entirely synthetic Slack providers."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
import subprocess
from threading import Barrier
from types import SimpleNamespace
import urllib.request

import pytest

from codex_watchdog.control_state import ControlError
from codex_watchdog.relay import ReplyResult

from test_slack_bot_relay import (
    ADMIN,
    BOT_USER,
    CHANNEL,
    OTHER_TARGET,
    OTHER_TEAM,
    OWN_APP,
    OWN_BOT,
    OWN_USER,
    PROMPT,
    REQUEST,
    TARGET,
    TEAM,
    Rig,
    instruction_event,
    journal_rows,
)


QUEUED = (f"WatchDog request {REQUEST}: accepted and queued. "
          "This receipt does not confirm execution or completion.")
UNCERTAIN = (f"WatchDog request {REQUEST}: delivery is uncertain. "
             "The request ID remains reserved; do not replay or resend it.")
DUPLICATE = (f"WatchDog request {REQUEST}: request ID was already reserved. "
             "This message queued no additional task; prior execution is not confirmed.")
CLOSED = (f"WatchDog request {REQUEST}: not accepted: this reply ticket is closed. "
          "This message queued no task.")


@pytest.fixture(autouse=True)
def external_effects_are_forbidden(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("receipt fixtures must not contact providers or execute commands")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


class ReceiptRig(Rig):
    """Real socket/poll relay paths; metadata, dispatch, and sends are fixtures."""

    def __init__(self, path, mode):
        self.send_outcome = "sent"
        super().__init__(path, mode)

    def post(self, **params):
        self.acks.append(deepcopy(params))
        if isinstance(self.send_outcome, BaseException):
            raise self.send_outcome
        if self.send_outcome != "sent":
            return deepcopy(self.send_outcome)
        return dict(ok=True, channel=params["channel"],
                    ts=f"1789111900.{len(self.acks):06d}")

    def provider(self, method, params):
        if method == "chat.postMessage":
            return self.post(**params)
        return super().provider(method, params)

    def socket(self, event, *, body=None):
        envelope = dict(team_id=TEAM, api_app_id=OWN_APP,
                        event_id="Ev" + event["client_msg_id"])
        if body is not None:
            envelope.update(body)
        result = self.relay._handle_bolt_message(
            event, envelope, SimpleNamespace(chat_postMessage=self.post))
        return result.to_dict()

    def redeliver(self, event, index=0):
        if self.mode == "socket":
            return self.deliver(event, body=dict(event_id=f"Ev-physical-retry-{index}"))
        # An already validated event can be concurrently in flight even after
        # its parent closes. Exercise its receipt path without rearming it.
        result = self.relay.handle_polled_message(deepcopy(event))
        self.relay.acknowledge(event, result, SimpleNamespace(chat_postMessage=self.post))
        return [result.to_dict()]


@pytest.fixture(params=("socket", "poll"))
def receipt_rig(request, tmp_path):
    return ReceiptRig(tmp_path, request.param)


def assert_correlated_receipt(rig, parent, *, request_id=REQUEST):
    assert len(rig.acks) == 1, "the sender needs one receipt for the admitted UUID"
    receipt = rig.acks[0]
    assert receipt["channel"] == CHANNEL
    assert receipt["thread_ts"] == parent
    assert receipt["unfurl_links"] is False
    assert receipt["unfurl_media"] is False
    assert request_id in receipt["text"]
    assert PROMPT not in receipt["text"]
    return receipt["text"]


def saved_receipt(rig, category="primary"):
    with rig.store.journal.transaction() as db:
        rows = db.execute("SELECT value FROM records WHERE namespace=? AND kind=?",
                          ("slack/bot-command-receipts-v1", "slack_bot_receipts")).fetchall()
    entries = [json.loads(row[0]) for row in rows]
    matches = [entry for entry in entries
               if entry["request_id"] == REQUEST and entry["category"] == category]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize("inline", (False, True), ids=("legacy", "inline"))
def test_admitted_command_has_correlated_queued_receipt_not_execution(receipt_rig, inline):
    rig = receipt_rig
    parent = rig.grant()
    result = rig.deliver(instruction_event(rig, parent, inline=inline))
    assert result[0]["status"] == "bot_queued"
    assert len(rig.calls) == 1
    assert rig.calls[0][0] == TARGET.thread_id
    assert rig.calls[0][2] == PROMPT
    assert not rig.state(parent)["active"]
    text = assert_correlated_receipt(rig, parent)
    assert text.endswith(QUEUED)
    assert rig.store.lookup_thread(CHANNEL, "1789111900.000001") is None
    assert "bot_receipt" not in result[0]
    saved = saved_receipt(rig)
    assert (saved["state"], saved["outcome"], saved["message_ts"]) == (
        "sent", "queued", "1789111900.000001")


@pytest.mark.parametrize("inline", (False, True), ids=("legacy", "inline"))
def test_uncertain_dispatch_has_correlated_non_replay_receipt(receipt_rig, inline):
    rig = receipt_rig
    parent = rig.grant()
    rig.outcome = TimeoutError("synthetic dispatch uncertainty: do not expose this detail")
    result = rig.deliver(instruction_event(rig, parent, inline=inline))
    assert result[0]["status"] == "bot_uncertain"
    assert len(rig.calls) == 1
    assert not rig.state(parent)["active"]
    text = assert_correlated_receipt(rig, parent)
    assert text.endswith(UNCERTAIN)
    assert "synthetic dispatch uncertainty" not in text
    assert rig.store.lookup_thread(CHANNEL, "1789111900.000001") is None
    saved = saved_receipt(rig)
    assert (saved["state"], saved["outcome"]) == ("sent", "uncertain")


@pytest.mark.parametrize("uncertain", (False, True), ids=("queued", "uncertain"))
@pytest.mark.parametrize("inline", (False, True), ids=("legacy", "inline"))
def test_provider_retries_and_restart_never_create_a_duplicate_receipt(receipt_rig, uncertain, inline):
    rig = receipt_rig
    parent = rig.grant()
    if uncertain:
        rig.outcome = TimeoutError("synthetic queue uncertainty")
    event = instruction_event(rig, parent, inline=inline)
    rig.deliver(event)
    text = assert_correlated_receipt(rig, parent)
    assert text.endswith(UNCERTAIN if uncertain else QUEUED)
    for index in range(3):
        rig.restart()
        rig.redeliver(event, index)
    assert len(rig.calls) == len(rig.acks) == 1
    assert not rig.state(parent)["active"]


@pytest.mark.parametrize("uncertain", (False, True), ids=("queued", "uncertain"))
@pytest.mark.parametrize("inline_first", (False, True), ids=("legacy-first", "inline-first"))
def test_new_physical_uuid_replays_have_one_generic_duplicate_receipt(receipt_rig, uncertain, inline_first):
    rig = receipt_rig
    parent = rig.grant()
    if uncertain:
        rig.outcome = TimeoutError("synthetic queue uncertainty")
    rig.deliver(instruction_event(rig, parent, inline=inline_first))
    assert_correlated_receipt(rig, parent)
    rig.restart()
    second = rig.record()
    before = rig.state(second)
    rig.deliver(instruction_event(rig, second, inline=not inline_first))
    assert len(rig.acks) == 2
    assert rig.acks[1]["channel"] == CHANNEL
    assert rig.acks[1]["thread_ts"] == second
    assert rig.acks[1]["text"].endswith(DUPLICATE)
    assert TARGET.thread_id not in rig.acks[1]["text"]
    assert parent not in rig.acks[1]["text"]
    rig.assert_inert(second, before, ack=False)
    for index in range(4):
        rig.restart()
        replay = instruction_event(rig, second, inline=bool(index % 2))
        rig.deliver(replay)
    assert len(rig.calls) == 1
    assert len(rig.acks) == 2
    assert rig.state(second)["active"]


def test_duplicate_feedback_cannot_disclose_another_session_or_consume_its_ticket(receipt_rig):
    rig = receipt_rig
    original = rig.grant()
    rig.deliver(instruction_event(rig, original))
    another_session = rig.grant(OTHER_TARGET)
    before = rig.state(another_session)
    rig.deliver(instruction_event(rig, another_session, inline=True))
    assert len(rig.acks) == 2
    receipt = rig.acks[1]
    assert receipt["thread_ts"] == another_session
    assert receipt["text"].endswith(DUPLICATE)
    assert TARGET.thread_id not in receipt["text"]
    assert OTHER_TARGET.thread_id not in receipt["text"]
    assert original not in receipt["text"]
    rig.assert_inert(another_session, before, ack=False)
    assert len(rig.calls) == 1


@pytest.mark.parametrize("uncertain", (False, True), ids=("queued", "uncertain"))
def test_new_physical_uuid_repeat_in_original_closed_parent_gets_one_duplicate_receipt(receipt_rig, uncertain):
    rig = receipt_rig
    parent = rig.grant()
    if uncertain:
        rig.outcome = TimeoutError("synthetic queue uncertainty")
    rig.deliver(instruction_event(rig, parent, inline=True))
    assert_correlated_receipt(rig, parent)
    rig.restart()
    rig.redeliver(instruction_event(rig, parent))
    assert len(rig.acks) == 2
    assert rig.acks[1]["thread_ts"] == parent
    assert rig.acks[1]["text"].endswith(DUPLICATE)
    assert saved_receipt(rig, "duplicate")["state"] == "sent"
    for index in range(3):
        rig.restart()
        rig.redeliver(instruction_event(rig, parent, inline=True), index)
    assert len(rig.calls) == 1 and len(rig.acks) == 2
    assert not rig.state(parent)["active"]


def test_closed_ticket_rejection_is_once_and_does_not_reserve_dispatch(receipt_rig):
    rig = receipt_rig
    parent = rig.grant()
    human = rig.event(parent, bot=False, user=ADMIN, text="Human fixture task")
    human_result = rig.relay.handle_message(human, event_id="Ev-human-first")
    assert human_result.status == "queued"
    assert len(rig.calls) == 1
    event = instruction_event(rig, parent, inline=True)
    # Polling intentionally stops visiting a consumed parent. The validated
    # event entry point still handles an in-flight page or explicit redelivery.
    result = (rig.relay.handle_polled_message(event) if rig.mode == "poll" else
              rig.relay.handle_message(event, event_id="Ev-closed-bot",
                  authenticated_team_id=TEAM, authenticated_app_id=OWN_APP))
    rig.relay.acknowledge(event, result, SimpleNamespace(chat_postMessage=rig.post))
    assert assert_correlated_receipt(rig, parent).endswith(CLOSED)
    for index in range(3):
        rig.restart()
        event = instruction_event(rig, parent, inline=bool(index % 2))
        result = (rig.relay.handle_polled_message(event) if rig.mode == "poll" else
                  rig.relay.handle_message(event, event_id=f"Ev-closed-{index}",
                      authenticated_team_id=TEAM, authenticated_app_id=OWN_APP))
        rig.relay.acknowledge(event, result, SimpleNamespace(chat_postMessage=rig.post))
    assert len(rig.acks) == len(rig.calls) == 1
    assert saved_receipt(rig, "rejection")["outcome"] == "rejected"
    fresh_parent = rig.record()
    assert rig.deliver(instruction_event(rig, fresh_parent))[0]["status"] == "bot_queued"
    assert len(rig.calls) == len(rig.acks) == 2
    assert rig.acks[1]["thread_ts"] == fresh_parent
    assert rig.acks[1]["text"].endswith(QUEUED)


@pytest.mark.parametrize("send_outcome", (
    TimeoutError("synthetic provider timeout with private detail"),
    dict(ok=False, error="synthetic_private_provider_failure"),
    dict(ok=True, channel="C87654321", ts="1789111900.000001"),
    dict(ok=True, channel=CHANNEL, ts="invalid"),
    dict(ok=True), None,
), ids=("timeout", "api-failure", "wrong-channel", "bad-ts", "incomplete", "missing"))
def test_failed_or_ambiguous_receipt_send_is_claimed_once_not_retried(receipt_rig, send_outcome):
    rig = receipt_rig
    parent = rig.grant()
    event = instruction_event(rig, parent, inline=True)
    rig.send_outcome = send_outcome
    result = rig.deliver(event)
    assert result[0]["status"] == "bot_queued"
    assert len(rig.calls) == len(rig.acks) == 1
    assert rig.acks[0]["text"].endswith(QUEUED)
    assert "private" not in rig.acks[0]["text"]
    saved = "\n".join(row[3] for row in journal_rows(rig))
    assert "synthetic provider timeout" not in saved
    assert "synthetic_private_provider_failure" not in saved
    saved = saved_receipt(rig)
    assert (saved["state"], saved["message_ts"]) == ("uncertain", None)
    rig.send_outcome = "sent"
    for index in range(3):
        rig.restart()
        rig.redeliver(event, index)
    assert len(rig.calls) == len(rig.acks) == 1
    assert saved_receipt(rig)["state"] == "uncertain"
    assert not rig.state(parent)["active"]


class SimulatedReceiptCrash(BaseException):
    pass


def test_crash_during_receipt_send_leaves_durable_no_retry_barrier(receipt_rig):
    rig = receipt_rig
    parent = rig.grant()
    event = instruction_event(rig, parent)
    rig.send_outcome = SimulatedReceiptCrash("synthetic crash after outbound attempt")
    with pytest.raises(SimulatedReceiptCrash):
        rig.deliver(event)
    assert len(rig.calls) == len(rig.acks) == 1
    assert saved_receipt(rig)["state"] == "claimed"
    rig.send_outcome = "sent"
    rig.restart()
    rig.redeliver(event)
    assert len(rig.calls) == len(rig.acks) == 1
    assert saved_receipt(rig)["state"] == "claimed"
    assert not rig.state(parent)["active"]


def test_metadata_deferral_is_silent_and_receipt_follows_later_admission(receipt_rig):
    rig = receipt_rig
    parent = rig.grant()
    rig.restart()
    event = instruction_event(rig, parent)
    before, rows = rig.state(parent), journal_rows(rig)
    rig.api.fail_once = TimeoutError("synthetic identity read timeout")
    assert rig.deliver(event)[0]["status"] == "deferred"
    rig.assert_inert(parent, before)
    assert journal_rows(rig) == rows
    if rig.mode == "poll":
        cursors = json.loads(rig.poller.path.read_text())["threads"]
        assert rig.store.thread_key(CHANNEL, parent) not in cursors
    assert rig.deliver(event)[0]["status"] == "bot_queued"
    assert len(rig.calls) == 1
    assert assert_correlated_receipt(rig, parent).endswith(QUEUED)


@pytest.mark.parametrize("denied", (False, True), ids=("current-owner", "fence-denied"))
def test_controlled_command_receipt_uses_captured_notification_fence(receipt_rig, monkeypatch, denied):
    rig = receipt_rig
    parent = rig.grant()
    target, capability, notifications = object(), object(), []

    def notify(actual_target, actual_capability, event, notifier):
        assert actual_target is target and actual_capability is capability
        notifications.append(event)
        if denied:
            raise ControlError("synthetic stale capability")
        return notifier.notify(event).to_dict()

    def controlled(mapping, event_key, instruction_id, prompt, **kwargs):
        assert mapping.target == TARGET and prompt == PROMPT
        return ReplyResult("queued", TARGET.workspace_id, instruction_id, "enqueued",
                           control=(SimpleNamespace(notify=notify), target, capability))

    monkeypatch.setattr(rig.relay, "_controlled_reply", controlled)
    event = instruction_event(rig, parent)
    assert rig.deliver(event)[0]["status"] == "bot_queued"
    assert len(notifications) == 1
    if denied:
        assert rig.acks == []
        assert saved_receipt(rig)["state"] == "uncertain"
    else:
        assert assert_correlated_receipt(rig, parent).endswith(QUEUED)
    rig.restart()
    rig.redeliver(event)
    assert len(notifications) == 1
    assert len(rig.acks) == int(not denied)
    assert rig.calls == []


@pytest.mark.parametrize("denied", (False, True), ids=("current-owner", "fence-denied"))
def test_controlled_finish_failure_keeps_captured_fence_for_uncertain_receipt(receipt_rig, monkeypatch, denied):
    rig = receipt_rig
    parent = rig.grant()
    target, capability, notifications, deliveries = object(), object(), [], []

    def notify(actual_target, actual_capability, event, notifier):
        assert actual_target is target and actual_capability is capability
        notifications.append(event)
        if denied:
            raise ControlError("synthetic stale capability")
        return notifier.notify(event).to_dict()

    def controlled(mapping, event_key, instruction_id, prompt, **kwargs):
        deliveries.append((mapping.target.thread_id, instruction_id, prompt))
        return ReplyResult("queued", TARGET.workspace_id, instruction_id, "enqueued",
                           control=(SimpleNamespace(notify=notify), target, capability))

    def finish_failure(*args, **kwargs):
        raise OSError("synthetic reply journal finish failure")

    monkeypatch.setattr(rig.relay, "_controlled_reply", controlled)
    monkeypatch.setattr(rig.store, "finish_reply", finish_failure)
    event = instruction_event(rig, parent)
    assert rig.deliver(event)[0]["status"] == "bot_uncertain"
    assert len(deliveries) == len(notifications) == 1
    if denied:
        assert rig.acks == []
        assert saved_receipt(rig)["state"] == "uncertain"
    else:
        assert assert_correlated_receipt(rig, parent).endswith(UNCERTAIN)
        assert saved_receipt(rig)["state"] == "sent"
    rig.restart()
    rig.redeliver(event)
    assert len(deliveries) == len(notifications) == 1
    assert len(rig.acks) == int(not denied)
    assert rig.calls == []


@pytest.mark.parametrize("controlled", (False, True), ids=("legacy", "controlled"))
def test_crash_before_first_acknowledgement_does_not_emit_unfenced_retry_feedback(receipt_rig, monkeypatch, controlled):
    rig = receipt_rig
    parent = rig.grant()
    controlled_deliveries, notifications = [], []

    def notify(*args):
        notifications.append(args)
        raise AssertionError("a crashed handler cannot recover its captured capability")

    def dispatch(mapping, event_key, instruction_id, prompt, **kwargs):
        controlled_deliveries.append((mapping.target.thread_id, instruction_id, prompt))
        return ReplyResult("queued", TARGET.workspace_id, instruction_id, "enqueued",
                           control=(SimpleNamespace(notify=notify), object(), object()))

    if controlled:
        monkeypatch.setattr(rig.relay, "_controlled_reply", dispatch)
    event = instruction_event(rig, parent, inline=True)
    result = (rig.relay.handle_polled_message(event) if rig.mode == "poll" else
              rig.relay.handle_message(event, event_id="Ev-before-crash",
                  authenticated_team_id=TEAM, authenticated_app_id=OWN_APP))
    assert result.status == "bot_queued"
    assert len(rig.calls) + len(controlled_deliveries) == 1
    # The process exits after durable admission, before any receipt claim.
    del result
    for index in range(3):
        rig.restart()
        rig.redeliver(event, index)
        assert rig.controlled.call_count == rig.dispatch.call_count == 0
    assert rig.acks == notifications == []
    assert len(rig.calls) + len(controlled_deliveries) == 1
    assert not any(row[1] == "slack_bot_receipts" for row in journal_rows(rig))
    assert not rig.state(parent)["active"]
    # A deliberate new physical message gets only generic UUID reservation
    # feedback; it cannot recover a primary receipt or enqueue another task.
    rig.redeliver(instruction_event(rig, parent))
    assert len(rig.acks) == 1 and rig.acks[0]["text"].endswith(DUPLICATE)
    assert len(rig.calls) + len(controlled_deliveries) == 1


@pytest.mark.parametrize("failure", ("deferred", "raised"))
def test_control_transport_failure_without_captured_capability_stays_silent(receipt_rig, monkeypatch, failure):
    rig = receipt_rig
    parent = rig.grant()
    attempts = []

    def controlled(mapping, event_key, instruction_id, prompt, **kwargs):
        attempts.append(instruction_id)
        if failure == "raised":
            raise OSError("synthetic control failure before capability capture")
        return ReplyResult("deferred", TARGET.workspace_id, instruction_id,
                           "control_transport_unavailable")

    monkeypatch.setattr(rig.relay, "_controlled_reply", controlled)
    event = instruction_event(rig, parent)
    assert rig.deliver(event)[0]["status"] == "bot_uncertain"
    assert len(attempts) == 1
    assert rig.acks == rig.calls == []
    rig.restart()
    rig.redeliver(event)
    assert rig.acks == rig.calls == []
    assert len(attempts) == 1
    assert not rig.state(parent)["active"]


class SdkShapedReceiptClient:
    def __init__(self, rig):
        self.rig = rig
        self.retry_handlers = [object()]
        self.timeout = 30
        self.attempts = []

    def chat_postMessage(self, **params):
        self.attempts.append((self.retry_handlers, self.timeout))
        try:
            return self.rig.post(**params)
        except TimeoutError:
            if self.retry_handlers:
                return self.rig.post(**params)
            raise


@pytest.mark.parametrize("uncertain_send", (False, True), ids=("sent", "timeout"))
def test_sdk_sender_copy_disables_internal_retries_and_preserves_listener(receipt_rig, uncertain_send):
    rig = receipt_rig
    parent = rig.grant()
    event = instruction_event(rig, parent)
    result = (rig.relay.handle_polled_message(event) if rig.mode == "poll" else
              rig.relay.handle_message(event, event_id="Ev-sdk-shaped",
                  authenticated_team_id=TEAM, authenticated_app_id=OWN_APP))
    sender = SdkShapedReceiptClient(rig)
    original_handlers = list(sender.retry_handlers)
    if uncertain_send:
        rig.send_outcome = TimeoutError("synthetic SDK connection failure")
    rig.relay.acknowledge(event, result, sender)
    assert len(sender.attempts) == len(rig.acks) == len(rig.calls) == 1
    assert sender.attempts == [([], 10)]
    assert sender.retry_handlers == original_handlers
    assert sender.timeout == 30
    assert saved_receipt(rig)["state"] == ("uncertain" if uncertain_send else "sent")
    rig.relay.acknowledge(event, result, sender)
    assert len(sender.attempts) == len(rig.acks) == 1


@pytest.mark.parametrize("change", ("revoke", "mapping-target"))
def test_current_grant_and_exact_mapping_are_rechecked_before_receipt_effect(receipt_rig, change):
    rig = receipt_rig
    parent = rig.grant()
    event = instruction_event(rig, parent)
    result = (rig.relay.handle_polled_message(event) if rig.mode == "poll" else
              rig.relay.handle_message(event, event_id="Ev-delayed-ack",
                  authenticated_team_id=TEAM, authenticated_app_id=OWN_APP))
    assert result.status == "bot_queued"
    if change == "revoke":
        rig.control("remove")
    else:
        journal = rig.store.journal
        with journal.transaction() as db:
            key = rig.store.thread_key(CHANNEL, parent)
            old = journal.get(db, "threads", key)
            journal.put(db, "threads", key, dict(old, target=OTHER_TARGET.to_dict()))
    rig.relay.acknowledge(event, result, SimpleNamespace(chat_postMessage=rig.post))
    assert len(rig.calls) == 1
    assert rig.acks == []


@pytest.mark.parametrize("status", ("bot_queued", "bot_uncertain", "bot_duplicate", "bot_rejected"))
def test_arbitrary_public_result_without_captured_admission_cannot_send_a_receipt(receipt_rig, status):
    rig = receipt_rig
    parent = rig.grant()
    event = instruction_event(rig, parent)
    before, rows = rig.state(parent), journal_rows(rig)
    rig.relay.acknowledge(event, ReplyResult(status), SimpleNamespace(chat_postMessage=rig.post))
    rig.assert_inert(parent, before)
    assert journal_rows(rig) == rows


def test_poll_receipt_destination_comes_only_from_the_requested_mapping(tmp_path):
    rig = ReceiptRig(tmp_path, "poll")
    parent = rig.grant()
    event = instruction_event(rig, parent)
    event["channel"] = "C87654321"
    rig.history[(CHANNEL, parent)] = [event]
    results = rig.poller.poll_once()
    assert results[0]["status"] == "bot_queued"
    assert assert_correlated_receipt(rig, parent).endswith(QUEUED)
    assert len(rig.calls) == 1


@pytest.mark.parametrize("uncertain", (False, True), ids=("queued", "uncertain"))
def test_concurrent_provider_deliveries_have_one_dispatch_and_one_primary_receipt(tmp_path, uncertain):
    first = ReceiptRig(tmp_path, "socket")
    parent = first.grant()
    second = ReceiptRig(tmp_path, "socket")
    if uncertain:
        first.outcome = second.outcome = TimeoutError("synthetic concurrent dispatch uncertainty")
    event = instruction_event(first, parent, inline=True)
    start = Barrier(2)

    def deliver(rig):
        start.wait(timeout=5)
        return rig.socket(deepcopy(event))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(deliver, rig) for rig in (first, second)]
        results = [future.result(timeout=10) for future in futures]
    assert any(value["status"] == ("bot_uncertain" if uncertain else "bot_queued")
               for value in results)
    assert len(first.calls) + len(second.calls) == 1
    receipts = first.acks + second.acks
    assert len(receipts) == 1
    assert receipts[0]["channel"] == CHANNEL and receipts[0]["thread_ts"] == parent
    assert receipts[0]["text"].endswith(UNCERTAIN if uncertain else (QUEUED, UNCERTAIN))


@pytest.mark.parametrize("kind", (
    "own-command", "own-receipt", "chatter", "malformed", "quoted", "edited",
    "wrong-bot", "wrong-app", "wrong-team", "absent-grant", "revoked", "other-session",
))
def test_unsafe_or_ungranted_messages_have_no_feedback_or_claim(receipt_rig, kind):
    rig = receipt_rig
    parent = rig.record() if kind == "absent-grant" else rig.grant()
    if kind == "revoked":
        rig.control("remove")
        rig.restart()
    elif kind == "other-session":
        parent = rig.record(OTHER_TARGET)
    event = instruction_event(rig, parent, inline=True)
    if kind in ("own-command", "own-receipt"):
        event.update(user=OWN_USER, bot_id=OWN_BOT, app_id=OWN_APP,
                     bot_profile=dict(id=OWN_BOT, app_id=OWN_APP, team_id=TEAM))
        if kind == "own-receipt":
            event["text"] = f"WatchDog request {REQUEST}: queued; execution not confirmed."
            event.pop("blocks")
    elif kind in ("chatter", "malformed", "quoted"):
        event["text"] = {
            "chatter": "Ordinary fixture bot chatter",
            "malformed": "!codex not-a-uuid -- Continue the fixture task",
            "quoted": f"> !codex {REQUEST} -- {PROMPT}",
        }[kind]
        event.pop("blocks")
    elif kind == "edited":
        event["edited"] = dict(user=BOT_USER, ts=event["ts"])
    elif kind == "wrong-bot":
        event["bot_id"] = "B99999999"
    elif kind == "wrong-app":
        event["app_id"] = "A99999999"
    elif kind == "wrong-team":
        event["team"] = OTHER_TEAM
    before, rows = rig.state(parent), journal_rows(rig)
    rig.deliver(event)
    rig.assert_inert(parent, before)
    assert journal_rows(rig) == rows
    assert rig.acks == rig.calls == rig.remote_calls == []


@pytest.mark.parametrize("field,value", (
    ("team_id", None), ("team_id", OTHER_TEAM),
    ("api_app_id", None), ("api_app_id", "A99999999"),
))
def test_socket_receipt_requires_authenticated_receiving_context(tmp_path, field, value):
    rig = ReceiptRig(tmp_path, "socket")
    parent = rig.grant()
    before, rows = rig.state(parent), journal_rows(rig)
    rig.deliver(instruction_event(rig, parent), body={field: value})
    rig.assert_inert(parent, before)
    assert journal_rows(rig) == rows
    assert rig.acks == rig.calls == []
