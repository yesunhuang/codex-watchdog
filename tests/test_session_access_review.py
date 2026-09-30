"""Focused review tests for supervisor-identified final fixes.

Covers exactly:
1. Slack default sender: WebClient(retry_handlers=[]), uncertain-on-raise, no replay-send.
2. Slack _response_text: access_verify_missing_scope explains users:read; malformed access
   explains add/remove/access (not bind); raw provider text must not leak.
3. Positive plain 'access' for Slack and Lark: new mapped same-target notification, zero
   queue dispatch.
4. SessionAccess.preflight: malformed ACL blocks operations without API/send, preserves
   ticket; closed ticket returns ignored_closed_ticket (parameterized providers).
5. Lark relay integration boundaries: early invalid Open ID inert; verify_user different
   immutable ID rejected (no grant/confirmation); safe missing-permission diagnostic
   retained; unknown exceptions redacted.
6. OneBot: confirmation returns different valid chat -> uncertain; no mapping for either
   address; no repeat send after replay.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.lark_transport import LarkTransportError
from codex_watchdog.models import sha256_text
from codex_watchdog.onebot_relay import OneBotThreadStore
from codex_watchdog.relay import ReplyResult
from codex_watchdog.slack_mapping import SlackThreadStore
from codex_watchdog.slack_relay import SlackReplyRelay

# Re-use test fixtures from the supervised full-flow relay test.
from tests.test_session_access_relays import (
    CHANNEL, ADMIN_USER, DELEGATE_USER,
    THREAD_TS, ADMIN_MSG_TS, NEW_CONF_TS,
    TARGET, TARGET_THREAD,
    LARK_CHAT, LARK_ADMIN, LARK_DELEGATE,
    LARK_NOTIF_MSG, LARK_CONF_MSG,
    LARK_ADMIN_CMD,
    OB_SELF, OB_GROUP, OB_ADMIN, OB_DELEGATE,
    OB_DESTINATION, OB_NOTIF_MSG, OB_CONF_MSG, OB_ADMIN_CMD,
    lark_cfg, _queue,
    slack_relay, lark_relay, onebot_relay,
    record_slack_notification, record_lark_notification, record_ob_notification,
    slack_access_api, slack_access_sender,
    LarkApi, OneBotApi,
    slack_reply_event, lark_access_event, ob_access_event,
)


# ── Local helpers ─────────────────────────────────────────────────────────────

def lark_plain_access_payload(
    *,
    open_id=LARK_ADMIN,
    chat_id=LARK_CHAT,
    msg_id=LARK_ADMIN_CMD,
    root_id=LARK_NOTIF_MSG,
    parent_id=LARK_NOTIF_MSG,
    event_id="evt-access-plain-001",
):
    """Lark event for plain 'access' command with no mention."""
    content = json.dumps({"text": "access"})
    msg = {
        "message_id": msg_id, "chat_id": chat_id, "message_type": "text",
        "root_id": root_id, "parent_id": parent_id, "content": content,
    }
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id, "app_id": lark_cfg().app_id,
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": open_id}},
            "message": msg,
        },
    }


def lark_add_event_bad_sender(*, bad_open_id, event_id="evt-bad-sender-001"):
    """Lark event with an invalid sender open_id."""
    content = json.dumps({"text": "add @_user_1"})
    mention = {"key": "@_user_1", "id": {"open_id": LARK_DELEGATE}}
    msg = {
        "message_id": LARK_ADMIN_CMD, "chat_id": LARK_CHAT, "message_type": "text",
        "root_id": LARK_NOTIF_MSG, "parent_id": LARK_NOTIF_MSG,
        "content": content, "mentions": [mention],
    }
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id, "app_id": lark_cfg().app_id,
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": bad_open_id}},
            "message": msg,
        },
    }


# ── 1. Slack default sender: retry_handlers=[] and uncertain-on-raise ─────────

def test_slack_default_sender_empty_retry_handlers_and_uncertain_on_raise(tmp_path, monkeypatch):
    """Default sender builds WebClient(retry_handlers=[]) and leaves uncertain on raise."""
    slack_sdk = pytest.importorskip("slack_sdk")

    captured_init = {}
    post_calls = []

    class FakeWebClient:
        def __init__(self, **kwargs):
            captured_init.update(kwargs)

        def chat_postMessage(self, **kwargs):
            post_calls.append(kwargs)
            raise RuntimeError("simulated_transport_failure")

    monkeypatch.setattr(slack_sdk, "WebClient", FakeWebClient)

    # No access_sender → relay uses the default sender which constructs WebClient.
    relay = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER))
    record_slack_notification(relay.thread_store)

    r = relay.handle_message(slack_reply_event())

    # Client was initialized with exactly retry_handlers=[].
    assert captured_init.get("retry_handlers") == []

    # One post attempt was made and it raised → confirmation uncertain.
    assert len(post_calls) == 1
    assert r.status == "uncertain"

    # Replay the same event: ticket is consumed so no second send occurs.
    # After apply claims the ticket the preflight returns None (ignored_closed_ticket)
    # before reaching the sender, so the status is not "duplicate" but also not a send.
    r2 = relay.handle_message(slack_reply_event())
    assert r2.status in ("duplicate", "ignored_closed_ticket")
    assert len(post_calls) == 1  # still only the one failed attempt


# ── 2. Slack _response_text diagnostic strings ────────────────────────────────

def test_slack_response_text_missing_scope_explains_users_read():
    """access_verify_missing_scope delivery_status → users:read explanation."""
    result = ReplyResult(
        "rejected_route_command", delivery_status="access_verify_missing_scope"
    )
    text = SlackReplyRelay._response_text(result)
    assert text is not None
    assert "users:read" in text
    assert "add" in text.lower()


def test_slack_response_text_malformed_access_explains_add_remove_access_not_bind():
    """access_* delivery_status → add/remove/access guidance; no 'bind' mention."""
    access_codes = [
        "access_command_invalid_mention",
        "access_command_missing_target",
        "access_command_extra_targets",
        "access_command_multiline",
        "access_verify_api_error",
    ]
    for code in access_codes:
        result = ReplyResult("rejected_route_command", delivery_status=code)
        text = SlackReplyRelay._response_text(result)
        assert text is not None, f"No response text for code {code!r}"
        assert "add" in text.lower(), f"Missing add-guidance for {code!r}"
        assert "access" in text.lower(), f"Missing access-guidance for {code!r}"
        # Must not redirect to bind (which is a routing command, not access).
        assert "bind" not in text, f"Incorrect 'bind' mention for {code!r}"
        # Raw code must not appear verbatim in the user-facing text.
        assert code not in text, f"Raw code {code!r} leaked into response"


def test_slack_response_text_no_raw_provider_exception_text():
    """Provider exception text in delivery_status must not appear in response."""
    # Simulate a code that would carry provider-specific secret detail.
    secret_code = "access_internal_exception_with_s3cr3t_data"
    result = ReplyResult("rejected_route_command", delivery_status=secret_code)
    text = SlackReplyRelay._response_text(result)
    # Whatever text is returned, it must not contain the raw delivery_status string.
    if text is not None:
        assert secret_code not in text


# ── 3. Positive plain 'access' command ────────────────────────────────────────

def test_slack_plain_access_new_mapped_notification_zero_dispatch(tmp_path):
    """Plain 'access' creates new mapped same-target confirmation; no queue dispatch."""
    sender, sent = slack_access_sender(NEW_CONF_TS)
    calls, queue = _queue()
    relay = slack_relay(
        tmp_path,
        access_api=slack_access_api(DELEGATE_USER),
        access_sender=sender,
        queue=queue,
    )
    record_slack_notification(relay.thread_store)

    r = relay.handle_message(
        slack_reply_event(text="access", client_msg_id="access-plain-1")
    )

    assert r.status == "access_applied"
    assert r.delivery_status == "access"
    assert len(sent) == 1
    assert calls == []  # zero queue dispatch

    # Confirmation creates a new mapping for the same target.
    new_mapping = relay.thread_store.lookup_thread(CHANNEL, NEW_CONF_TS)
    assert new_mapping is not None
    assert new_mapping.target == TARGET


def test_lark_plain_access_new_mapped_notification_zero_dispatch(tmp_path):
    """Lark 'access' creates new mapped same-target notification; no queue dispatch."""
    calls, queue = _queue()
    api = LarkApi(conf_msg=LARK_CONF_MSG)
    relay = lark_relay(tmp_path, api=api, queue=queue)
    record_lark_notification(relay.thread_store)

    r = relay.handle_event(lark_plain_access_payload(event_id="evt-access-plain-lark"))

    assert r.status == "access_applied"
    assert r.delivery_status == "access"
    assert calls == []  # zero queue dispatch
    assert any(c[0] == "send" for c in api.calls)  # confirmation sent exactly once

    # New mapping for the confirmation message pointing to same target.
    new_mapping = relay.thread_store.lookup_thread(LARK_CHAT, LARK_CONF_MSG)
    assert new_mapping is not None
    assert new_mapping.target == TARGET


# ── 4. SessionAccess.preflight: malformed ACL and closed ticket ───────────────

def test_preflight_malformed_acl_blocks_slack_access_no_api_no_send_preserves_ticket(tmp_path):
    """Malformed ACL in journal raises during preflight: blocks all ops, no API/send."""
    api_calls = []

    def spy_api(method, params):
        api_calls.append(params)
        uid = params.get("user", "")
        return {"ok": True, "user": {
            "id": uid, "is_bot": False, "is_app_user": False,
            "deleted": False, "bot_id": None, "api_app_id": None,
            "is_agentforce_bot": False,
        }}

    sent = []

    def spy_sender(text, op, dest):
        sent.append(op)
        return {"chat_id": dest, "message_id": NEW_CONF_TS}

    relay = slack_relay(tmp_path, access_api=spy_api, access_sender=spy_sender)
    record_slack_notification(relay.thread_store)

    # Inject a malformed ACL entry (wrong schema_version) for this thread.
    acl_key = sha256_text(f"slack\0{CHANNEL}\0acl\0{TARGET_THREAD}")
    journal = relay.thread_store.journal
    with journal.transaction() as db:
        journal.put(db, "session_acl", acl_key, {"schema_version": 99, "malformed": True})

    # add command must be blocked.
    r = relay.handle_message(slack_reply_event())
    assert r.status == "rejected_state_or_collision"
    assert api_calls == []
    assert sent == []

    # Source ticket must still be active (apply was never called).
    source_key = SlackThreadStore.thread_key(CHANNEL, THREAD_TS)
    with journal.transaction() as db:
        row = db.execute(
            "SELECT active FROM records WHERE namespace=? AND kind='threads' AND key=?",
            (journal.namespace, source_key),
        ).fetchone()
    assert row is not None and row[0] == 1


@pytest.mark.parametrize("provider", ["lark", "onebot"])
def test_preflight_closed_ticket_blocks_access_command(tmp_path, provider):
    """Closed ticket makes preflight return None → ignored_closed_ticket; no API/send."""
    if provider == "lark":
        api = LarkApi(conf_msg=LARK_CONF_MSG)
        relay = lark_relay(tmp_path, api=api)
        record_lark_notification(relay.thread_store)
        journal = relay.thread_store.journal
        src = LarkThreadStore._address(LARK_CHAT, LARK_NOTIF_MSG)
        with journal.transaction() as db:
            db.execute(
                "UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                (journal.namespace, src),
            )
        r = relay.handle_event(lark_access_event(event_id="evt-closed-lark"))
        # No API call (verify_user was never reached); no confirmation send.
        assert r.status == "ignored_closed_ticket"
        assert not any(c[0] == "send" for c in api.calls)
    else:
        api = OneBotApi(conf_msg=OB_CONF_MSG)
        relay = onebot_relay(tmp_path, api=api)
        record_ob_notification(relay.thread_store)
        journal = relay.thread_store.journal
        src = OneBotThreadStore._address(OB_DESTINATION, OB_NOTIF_MSG)
        with journal.transaction() as db:
            db.execute(
                "UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                (journal.namespace, src),
            )
        r = relay.handle_event(ob_access_event())
        assert r.status == "ignored_closed_ticket"
        assert not any(c[0] == "send" for c in api.calls)


# ── 5. Lark relay integration boundaries ─────────────────────────────────────

def test_lark_early_invalid_open_id_inert(tmp_path):
    """Sender with invalid open_id prefix is rejected before any ACL/API lookup."""
    api = LarkApi()
    relay = lark_relay(tmp_path, api=api)
    record_lark_notification(relay.thread_store)

    # open_id does not start with "ou_"
    r = relay.handle_event(
        lark_add_event_bad_sender(bad_open_id="badid_notvalid_12345", event_id="evt-bad-1")
    )
    assert r.status == "ignored_unauthorized"
    # No verification API was consulted.
    assert not any(c[0] == "verify_user" for c in api.calls)


def test_lark_verify_user_different_id_rejected_no_grant_no_confirmation(tmp_path):
    """verify_user returning a different immutable ID → rejected; no grant, no send."""
    different_id = "ou_verifiedother00001"

    class MismatchApi:
        calls = []

        def send(self, text, operation_id, **kwargs):
            self.calls.append(("send", text, operation_id))
            return {"chat_id": LARK_CHAT, "message_id": LARK_CONF_MSG}

        def verify_user(self, open_id):
            self.calls.append(("verify_user", open_id))
            return different_id  # Returns a different ID than requested.

    api = MismatchApi()
    relay = lark_relay(tmp_path, api=api)
    record_lark_notification(relay.thread_store)

    r = relay.handle_event(lark_access_event(event_id="evt-mismatch-1"))

    assert r.status == "rejected_route_command"
    assert r.delivery_status == "lark_user_id_mismatch"
    # No grant: confirmation send was never attempted.
    assert not any(c[0] == "send" for c in api.calls)


def test_lark_verify_user_missing_permission_safe_diagnostic_retained(tmp_path):
    """verify_user raises safe LarkTransportError → exact code propagated to delivery_status."""
    safe_code = "lark_user_verify_failed_check_permissions"

    class PermissionErrorApi:
        calls = []

        def send(self, text, operation_id, **kwargs):
            self.calls.append(("send", text, operation_id))
            return {"chat_id": LARK_CHAT, "message_id": LARK_CONF_MSG}

        def verify_user(self, open_id):
            self.calls.append(("verify_user", open_id))
            raise LarkTransportError(safe_code)

    api = PermissionErrorApi()
    relay = lark_relay(tmp_path, api=api)
    record_lark_notification(relay.thread_store)

    r = relay.handle_event(lark_access_event(event_id="evt-perm-1"))

    assert r.status == "rejected_route_command"
    assert r.delivery_status == safe_code
    assert not any(c[0] == "send" for c in api.calls)


def test_lark_verify_user_unknown_exception_redacted(tmp_path):
    """verify_user raises an unknown exception → generic code, no leak of exception text."""
    secret_text = "INTERNAL_TOKEN_abc123def456_LEAKED"

    class LeakyApi:
        calls = []

        def send(self, text, operation_id, **kwargs):
            self.calls.append(("send", text, operation_id))
            return {"chat_id": LARK_CHAT, "message_id": LARK_CONF_MSG}

        def verify_user(self, open_id):
            self.calls.append(("verify_user", open_id))
            raise RuntimeError(secret_text)

    api = LeakyApi()
    relay = lark_relay(tmp_path, api=api)
    record_lark_notification(relay.thread_store)

    r = relay.handle_event(lark_access_event(event_id="evt-redact-1"))

    assert r.status == "rejected_route_command"
    # Generic fallback code, not the raw exception message.
    assert r.delivery_status == "lark_user_verify_failed"
    assert secret_text not in (r.delivery_status or "")
    assert not any(c[0] == "send" for c in api.calls)


# ── 6. OneBot confirmation with wrong chat ────────────────────────────────────

def test_onebot_confirmation_wrong_chat_uncertain_no_mapping_no_replay_send(tmp_path):
    """Confirmation returning a different valid chat → uncertain; no mapping; no replay send."""

    class WrongChatApi:
        def __init__(self):
            self.calls = []

        def send(self, text, operation_id, **kwargs):
            self.calls.append(("send", text, operation_id))
            # Different group ID from OB_DESTINATION.
            return {"chat_id": "group:99999", "message_id": OB_CONF_MSG}

    api = WrongChatApi()
    relay = onebot_relay(tmp_path, api=api)
    record_ob_notification(relay.thread_store)

    r = relay.handle_event(ob_access_event())

    # Validation rejects the wrong chat_id → uncertain.
    assert r.status == "uncertain"
    assert len(api.calls) == 1  # exactly one send attempt

    # No thread mapping for the wrong address.
    assert relay.thread_store.lookup_thread("group:99999", OB_CONF_MSG) is None
    # No thread mapping for the correct address either (finish_notification not called).
    assert relay.thread_store.lookup_thread(OB_DESTINATION, OB_CONF_MSG) is None

    # Replay the same event: ticket is consumed so preflight returns None (no second send).
    r2 = relay.handle_event(ob_access_event())
    assert r2.status in ("duplicate", "ignored_closed_ticket")
    assert len(api.calls) == 1  # still only the original failed attempt
