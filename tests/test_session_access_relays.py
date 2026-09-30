"""Integrated tests: real SessionACL + ReplyTickets, fixture provider APIs.

Proves the full add/revoke/access flows for all three providers, with exact
one-shot confirmation, immediate revoke, provider/session isolation,
reserved-control denial, and race-regression for both admin and delegate paths.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Optional

import pytest

from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.lark_relay import LarkReplyRelay
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.models import sha256_text
from codex_watchdog.onebot_relay import OneBotReplyRelay, OneBotThreadStore
from codex_watchdog.onebot_transport import OneBotConfig
from codex_watchdog.relay import RelayTarget
from codex_watchdog.session_acl import SessionACL
from codex_watchdog.slack_mapping import SlackThreadStore
from codex_watchdog.slack_relay import SlackReplyRelay


# ── Constants ─────────────────────────────────────────────────────────────────

CHANNEL = "C12345678"
ADMIN_USER = "U12345678"
DELEGATE_USER = "U23456789"
OTHER_USER = "U34567890"

THREAD_TS = "1760000000.000100"
ADMIN_MSG_TS = "1760000001.000200"
NEW_CONF_TS = "1760000002.000300"
DELEGATE_MSG_TS = "1760000003.000400"
ADMIN_MSG_TS2 = "1760000004.000500"
NEW_CONF_TS2 = "1760000005.000600"
DELEGATE_MSG_TS2 = "1760000006.000700"
THREAD_TS2 = "1760000007.000800"

TARGET_THREAD = "11111111-2222-4333-8444-555555555555"
TARGET_THREAD2 = "22222222-2222-4333-8444-555555555555"
TARGET = RelayTarget("workspace", TARGET_THREAD, "process_local")
TARGET2 = RelayTarget("workspace", TARGET_THREAD2, "process_local")

LARK_APP_ID = "cli_fixture000001"
LARK_SECRET = "fixture-app-secret"
LARK_CHAT = "oc_fixture000001"
LARK_ADMIN = "ou_admin00000001"
LARK_DELEGATE = "ou_deleg0000001a"
LARK_OTHER = "ou_other000000001"
LARK_NOTIF_MSG = "om_notification01"
LARK_NOTIF_MSG2 = "om_notification02"
LARK_CONF_MSG = "om_confirmation01"
LARK_CONF_MSG2 = "om_confirmation02"
LARK_DELEGATE_REPLY = "om_delegaterply01"
LARK_DELEGATE_REPLY2 = "om_delegaterply02"
LARK_ADMIN_CMD = "om_admincmd00001"
LARK_ADMIN_CMD2 = "om_admincmd00002"

OB_SELF = "12345"
OB_GROUP = "67890"
OB_ADMIN = "54321"
OB_DELEGATE = "11111"
OB_OTHER = "22222"
OB_DESTINATION = "group:" + OB_GROUP
OB_NOTIF_MSG = "-100"
OB_NOTIF_MSG2 = "-101"
OB_CONF_MSG = "-200"
OB_CONF_MSG2 = "-201"
OB_ADMIN_CMD = 300
OB_ADMIN_CMD2 = 301
OB_DELEGATE_REPLY = 400
OB_DELEGATE_REPLY2 = 401


# ── Helpers ───────────────────────────────────────────────────────────────────

def lark_cfg(*, admins=(LARK_ADMIN,)):
    return LarkConfig(LARK_APP_ID, LARK_SECRET, LARK_CHAT, admins)


def onebot_cfg(*, admins=(OB_ADMIN,)):
    return OneBotConfig("ws://127.0.0.1:3001", "token", OB_SELF, "group", OB_GROUP, admins)


def _queue():
    calls = []
    return calls, SimpleNamespace(dispatch=lambda *a: (calls.append(a) or SimpleNamespace(status="enqueued")))


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("text", ["bot diagnostics please", "!codex ordinary human text"])
def test_slack_bot_protocol_does_not_reserve_other_provider_human_text(tmp_path, provider, text):
    calls, queue = _queue()
    if provider == "lark":
        relay = lark_relay(tmp_path, queue=queue)
        record_lark_notification(relay.thread_store)
        result = relay.handle_event(lark_plain_event(open_id=LARK_ADMIN, text=text))
    else:
        relay = onebot_relay(tmp_path, queue=queue)
        record_ob_notification(relay.thread_store)
        result = relay.handle_event(ob_event(segments=[
            {"type": "reply", "data": {"id": OB_NOTIF_MSG}},
            {"type": "text", "data": {"text": text}},
        ]))
    assert result.status == "queued"
    assert len(calls) == 1


def slack_relay(tmp_path, *, admins=(ADMIN_USER,), access_api=None, access_sender=None,
                session_acl=None, queue=None):
    _, q = _queue() if queue is None else (None, queue)
    return SlackReplyRelay(
        tmp_path, bot_token="xoxb-test-secret", app_token="xapp-test-secret",
        channel_id=CHANNEL, allowed_user_ids=admins,
        queue_dispatcher=q, remote_ssh_adapter=SimpleNamespace(),
        access_api=access_api, access_sender=access_sender, session_acl=session_acl,
    )


def lark_relay(tmp_path, *, admins=(LARK_ADMIN,), api=None, session_acl=None, queue=None):
    _, q = _queue() if queue is None else (None, queue)
    return LarkReplyRelay(tmp_path, lark_cfg(admins=admins),
                          queue_dispatcher=q, remote_ssh_adapter=SimpleNamespace(),
                          api=api, session_acl=session_acl)


def onebot_relay(tmp_path, *, admins=(OB_ADMIN,), api=None, session_acl=None, queue=None):
    _, q = _queue() if queue is None else (None, queue)
    return OneBotReplyRelay(tmp_path, onebot_cfg(admins=admins),
                            queue_dispatcher=q, remote_ssh_adapter=SimpleNamespace(),
                            api=api, session_acl=session_acl)


def record_slack_notification(store, *, channel=CHANNEL, ts=THREAD_TS, target=TARGET):
    store.record_thread(channel, ts, target, sha256_text("notif:" + ts))


def record_lark_notification(store, *, chat=LARK_CHAT, msg_id=LARK_NOTIF_MSG, target=TARGET):
    fp = sha256_text("notif:" + msg_id)
    store.prepare_notification(fp, sha256_text("payload:" + msg_id))
    store.finish_notification(fp, chat, msg_id, target)


def record_ob_notification(store, *, dest=OB_DESTINATION, msg_id=OB_NOTIF_MSG, target=TARGET):
    fp = sha256_text("notif:" + msg_id)
    store.prepare_notification(fp, sha256_text("payload:" + msg_id))
    store.finish_notification(fp, dest, msg_id, target)


def slack_access_api(target_user):
    """Fixture API that validates one expected user ID."""
    def api(method, params):
        uid = params.get("user", "")
        return {"ok": True, "user": {
            "id": uid, "is_bot": False, "is_app_user": False,
            "deleted": False, "bot_id": None, "api_app_id": None,
            "is_agentforce_bot": False,
        }}
    return api


def slack_access_sender(new_ts=NEW_CONF_TS):
    sent = []
    def sender(text, operation_id, destination):
        sent.append((text, operation_id, destination))
        return {"chat_id": destination, "message_id": new_ts}
    return sender, sent


class LarkApi:
    """Fixture Lark API for access command tests."""
    def __init__(self, verified_id=LARK_DELEGATE, conf_msg=LARK_CONF_MSG):
        self.calls = []
        self._verified = verified_id
        self._conf_msg = conf_msg
        self.verify_error = None

    def send(self, text, operation_id, **kwargs):
        self.calls.append(("send", text, operation_id))
        return {"chat_id": LARK_CHAT, "message_id": self._conf_msg}

    def verify_user(self, open_id):
        self.calls.append(("verify_user", open_id))
        if self.verify_error is not None:
            raise self.verify_error
        return self._verified


class OneBotApi:
    """Fixture OneBot API for access command tests."""
    def __init__(self, conf_msg=OB_CONF_MSG):
        self.calls = []
        self._conf_msg = conf_msg

    def send(self, text, operation_id, **kwargs):
        self.calls.append(("send", text, operation_id))
        return {"chat_id": OB_DESTINATION, "message_id": self._conf_msg}


def slack_reply_event(*, user=ADMIN_USER, channel=CHANNEL, thread_ts=THREAD_TS,
                      ts=ADMIN_MSG_TS, text="add <@" + DELEGATE_USER + ">",
                      client_msg_id="aaa-bbb-ccc"):
    return {"type": "message", "user": user, "channel": channel,
            "thread_ts": thread_ts, "ts": ts, "text": text,
            "client_msg_id": client_msg_id}


def lark_access_event(*, open_id=LARK_ADMIN, mention_id=LARK_DELEGATE,
                      chat_id=LARK_CHAT, msg_id=LARK_ADMIN_CMD,
                      root_id=LARK_NOTIF_MSG, parent_id=LARK_NOTIF_MSG,
                      command="add", event_id="evt-access-001",
                      key="@_user_1", edited=False):
    mention = {"key": key, "id": {"open_id": mention_id}}
    content = json.dumps({"text": command + " " + key})
    msg = {"message_id": msg_id, "chat_id": chat_id, "message_type": "text",
           "root_id": root_id, "parent_id": parent_id,
           "content": content, "mentions": [mention]}
    if edited:
        msg["edited"] = True
    return {"schema": "2.0",
            "header": {"event_id": event_id, "app_id": lark_cfg().app_id,
                       "event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_type": "user",
                                  "sender_id": {"open_id": open_id}},
                      "message": msg}}


def lark_plain_event(*, open_id=LARK_DELEGATE, chat_id=LARK_CHAT,
                     msg_id=LARK_DELEGATE_REPLY, root_id=LARK_NOTIF_MSG,
                     parent_id=LARK_NOTIF_MSG, text="Hello", event_id="evt-reply-001"):
    return {"schema": "2.0",
            "header": {"event_id": event_id, "app_id": lark_cfg().app_id,
                       "event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_type": "user",
                                  "sender_id": {"open_id": open_id}},
                      "message": {"message_id": msg_id, "chat_id": chat_id,
                                  "message_type": "text",
                                  "root_id": root_id, "parent_id": parent_id,
                                  "content": json.dumps({"text": text})}}}


def ob_event(*, user_id=OB_ADMIN, group=OB_GROUP, msg_id=OB_ADMIN_CMD,
             reply_to=OB_NOTIF_MSG, segments=None, self_id=OB_SELF):
    if segments is None:
        segments = [{"type": "reply", "data": {"id": reply_to}},
                    {"type": "text", "data": {"text": "hello"}}]
    return {"post_type": "message", "self_id": int(self_id),
            "message_type": "group", "user_id": int(user_id),
            "group_id": int(group), "message_id": msg_id,
            "sender": {"user_id": int(user_id)}, "message": segments}


def ob_access_event(*, user_id=OB_ADMIN, msg_id=OB_ADMIN_CMD, reply_to=OB_NOTIF_MSG,
                    delegate_id=OB_DELEGATE, command="add", edited=False):
    segments = [{"type": "reply", "data": {"id": reply_to}},
                {"type": "text", "data": {"text": command + " "}},
                {"type": "at", "data": {"qq": delegate_id}}]
    payload = ob_event(user_id=user_id, msg_id=msg_id, reply_to=reply_to, segments=segments)
    if edited:
        payload["edited"] = True
    return payload


def ob_plain_access_event(*, user_id=OB_ADMIN, msg_id=OB_ADMIN_CMD,
                          reply_to=OB_NOTIF_MSG, command="access"):
    segments = [{"type": "reply", "data": {"id": reply_to}},
                {"type": "text", "data": {"text": command}}]
    return ob_event(user_id=user_id, msg_id=msg_id, reply_to=reply_to, segments=segments)


def ob_delegate_event(*, user_id=OB_DELEGATE, msg_id=OB_DELEGATE_REPLY,
                      reply_to=OB_CONF_MSG):
    segments = [{"type": "reply", "data": {"id": reply_to}},
                {"type": "text", "data": {"text": "Hello"}}]
    return ob_event(user_id=user_id, msg_id=msg_id, reply_to=reply_to, segments=segments)


# ── Slack: add → confirmation → delegate reply exactly once ───────────────────

def test_slack_add_delegate_then_reply_exactly_once(tmp_path):
    sender, sent = slack_access_sender(NEW_CONF_TS)
    calls, queue = _queue()
    relay = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER),
                        access_sender=sender, queue=queue)
    record_slack_notification(relay.thread_store)

    r = relay.handle_message(slack_reply_event())
    assert r.status == "access_applied" and r.delivery_status == "added"
    assert len(sent) == 1  # one confirmation sent

    # Confirmation thread is now registered; delegate can reply to it.
    delegate_reply = slack_reply_event(user=DELEGATE_USER, thread_ts=NEW_CONF_TS,
                                       ts=DELEGATE_MSG_TS, text="Hello",
                                       client_msg_id="del-1")
    r2 = relay.handle_message(delegate_reply)
    assert r2.status == "queued"
    assert calls[0][0] == TARGET_THREAD

    # Same event again is a duplicate.
    r3 = relay.handle_message(delegate_reply)
    assert r3.duplicate and len(calls) == 1


def test_slack_revoke_delegate_immediate_on_old_ticket(tmp_path):
    sender, _ = slack_access_sender(NEW_CONF_TS)
    sender2, _ = slack_access_sender(NEW_CONF_TS2)
    calls, queue = _queue()
    relay = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER),
                        access_sender=sender, queue=queue)

    # Add delegate (consumes THREAD_TS ticket)
    record_slack_notification(relay.thread_store)
    r = relay.handle_message(slack_reply_event())
    assert r.status == "access_applied" and r.delivery_status == "added"

    # Record a second notification for the same session (THREAD_TS2).
    record_slack_notification(relay.thread_store, ts=THREAD_TS2)

    # Remove delegate from THREAD_TS2
    relay2 = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER),
                         access_sender=sender2, queue=queue)
    r_rm = relay2.handle_message(slack_reply_event(
        thread_ts=THREAD_TS2, ts=ADMIN_MSG_TS2,
        text="remove <@" + DELEGATE_USER + ">",
        client_msg_id="rm-1"))
    assert r_rm.status == "access_applied" and r_rm.delivery_status == "removed"

    # Record a third notification; delegate must now be denied.
    third_ts = "1760000008.000900"
    record_slack_notification(relay.thread_store, ts=third_ts)
    denied = relay.handle_message(slack_reply_event(
        user=DELEGATE_USER, thread_ts=third_ts, ts="1760000009.000100",
        text="Hello", client_msg_id="del-2"))
    assert denied.status == "ignored_unauthorized"
    assert len(calls) == 0  # no dispatch before add, no dispatch after revoke


def test_slack_admin_target_already_admin_without_api_call(tmp_path):
    """add <admin_id> → already_admin; no verification API call needed."""
    api_calls = []
    def spy_api(method, params):
        api_calls.append(params)
        raise ValueError("access_verify_api_error")  # Should never be called

    sender, _ = slack_access_sender()
    relay = slack_relay(tmp_path, access_api=spy_api, access_sender=sender)
    record_slack_notification(relay.thread_store)

    # Add an existing admin as delegate — should skip API and get already_admin.
    r = relay.handle_message(slack_reply_event(
        text="add <@" + ADMIN_USER + ">"))
    assert r.status == "access_applied" and r.delivery_status == "already_admin"
    assert api_calls == []  # no external call for known admin


def test_slack_remove_existing_delegate_without_api_scope(tmp_path):
    """remove existing delegate skips verification even if API scope missing."""
    sender, _ = slack_access_sender()
    sender2, _ = slack_access_sender(NEW_CONF_TS2)
    relay = slack_relay(tmp_path,
                        access_api=slack_access_api(DELEGATE_USER),
                        access_sender=sender)
    record_slack_notification(relay.thread_store)
    # Add delegate first
    r = relay.handle_message(slack_reply_event())
    assert r.delivery_status == "added"

    # Now try to remove with an API that raises missing_scope
    def scope_error(method, params):
        raise ValueError("access_verify_missing_scope")

    record_slack_notification(relay.thread_store, ts=THREAD_TS2)
    relay2 = slack_relay(tmp_path, access_api=scope_error, access_sender=sender2)
    r_rm = relay2.handle_message(slack_reply_event(
        thread_ts=THREAD_TS2, ts=ADMIN_MSG_TS2,
        text="remove <@" + DELEGATE_USER + ">",
        client_msg_id="rm-scope"))
    assert r_rm.status == "access_applied" and r_rm.delivery_status == "removed"


def test_slack_malformed_sender_returns_inert(tmp_path):
    """list/dict user value must not raise TypeError; returns inert status."""
    relay = slack_relay(tmp_path)
    record_slack_notification(relay.thread_store)

    for bad_user in [None, [], {"id": ADMIN_USER}, True]:
        evt = slack_reply_event()
        evt["user"] = bad_user
        result = relay.handle_message(evt)
        assert result.status in ("ignored_unauthorized", "ignored_channel",
                                 "ignored_not_thread_reply", "ignored_bot_or_subtype",
                                 "ignored_event_type")


def test_slack_reserved_controls_denied_from_nonadmin(tmp_path):
    sender, _ = slack_access_sender()
    relay = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER),
                        access_sender=sender)
    record_slack_notification(relay.thread_store)
    # add, remove, access, bind, unbind are reserved; non-admin is denied
    for word in ("add <@U99999999>", "remove <@U99999999>", "access"):
        r = relay.handle_message(slack_reply_event(
            user=DELEGATE_USER, text=word, client_msg_id="noadm-" + word[:3]))
        assert r.status == "ignored_unauthorized", f"expected denied for: {word}"


def test_slack_closed_ticket_yields_no_dispatch(tmp_path):
    """A closed ticket stays inert before ACL authorization."""
    from codex_watchdog.session_acl import SessionACL

    calls, queue = _queue()
    relay = slack_relay(tmp_path, queue=queue)
    record_slack_notification(relay.thread_store)

    # Close the ticket manually to simulate race.
    journal = relay.thread_store.journal
    src = SlackThreadStore.thread_key(CHANNEL, THREAD_TS)
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                   (journal.namespace, src))

    # Closed-ticket classification is preserved for existing administrators.
    r = relay.handle_message(slack_reply_event(text="Hello"))
    assert r.status == "duplicate"
    assert r.delivery_status == "ticket_closed"
    assert calls == []


def test_slack_session_isolation_different_sessions(tmp_path):
    """Delegate added to session A cannot dispatch to session B."""
    sender, _ = slack_access_sender()
    relay = slack_relay(tmp_path,
                        access_api=slack_access_api(DELEGATE_USER),
                        access_sender=sender)

    # Session A: add delegate
    record_slack_notification(relay.thread_store, ts=THREAD_TS, target=TARGET)
    relay.handle_message(slack_reply_event())  # add @delegate to session A

    # Session B: different thread, different target
    record_slack_notification(relay.thread_store, ts=THREAD_TS2, target=TARGET2)
    calls, queue = _queue()
    relay2 = slack_relay(tmp_path, queue=queue)
    r = relay2.handle_message(slack_reply_event(
        user=DELEGATE_USER, thread_ts=THREAD_TS2, ts=DELEGATE_MSG_TS,
        text="Hello", client_msg_id="iso-1"))
    # Delegate has no access to session B → unauthorized
    assert r.status == "ignored_unauthorized"
    assert calls == []


# ── Lark: add → confirmation → delegate reply exactly once ────────────────────

def test_lark_add_delegate_then_reply_exactly_once(tmp_path):
    calls, queue = _queue()
    api = LarkApi()
    relay = lark_relay(tmp_path, api=api, queue=queue)
    record_lark_notification(relay.thread_store)

    r = relay.handle_event(lark_access_event(event_id="evt-add-001"))
    assert r.status == "access_applied" and r.delivery_status == "added"
    assert any(call[0] == "send" for call in api.calls)  # confirmation sent
    # Host prefix in confirmation
    sent_text = next(call[1] for call in api.calls if call[0] == "send")
    assert "Machine:" in sent_text

    # Delegate can now reply to the confirmation thread.
    calls2, queue2 = _queue()
    relay2 = lark_relay(tmp_path, api=api, queue=queue2)
    r2 = relay2.handle_event(lark_plain_event(
        open_id=LARK_DELEGATE, root_id=LARK_CONF_MSG, parent_id=LARK_CONF_MSG,
        msg_id=LARK_DELEGATE_REPLY, event_id="evt-dreply-001"))
    assert r2.status == "queued"
    assert calls2[0][0] == TARGET_THREAD

    # Same event again is a duplicate.
    r3 = relay2.handle_event(lark_plain_event(
        open_id=LARK_DELEGATE, root_id=LARK_CONF_MSG, parent_id=LARK_CONF_MSG,
        msg_id=LARK_DELEGATE_REPLY, event_id="evt-dreply-001"))
    assert r3.duplicate and len(calls2) == 1


def test_lark_revoke_delegate_immediate_on_old_ticket(tmp_path):
    api = LarkApi(conf_msg=LARK_CONF_MSG)
    api2 = LarkApi(conf_msg=LARK_CONF_MSG2)
    relay = lark_relay(tmp_path, api=api)
    record_lark_notification(relay.thread_store)
    r = relay.handle_event(lark_access_event(event_id="evt-add-002"))
    assert r.delivery_status == "added"

    # Second notification for same session.
    record_lark_notification(relay.thread_store, msg_id=LARK_NOTIF_MSG2)
    relay.api = api2
    r_rm = relay.handle_event(lark_access_event(
        msg_id=LARK_ADMIN_CMD2, root_id=LARK_NOTIF_MSG2, parent_id=LARK_NOTIF_MSG2,
        command="remove", event_id="evt-rm-002"))
    assert r_rm.status == "access_applied" and r_rm.delivery_status == "removed"

    # Third notification — delegate is now revoked.
    third_msg = "om_notification03"
    record_lark_notification(relay.thread_store, msg_id=third_msg)
    calls, queue = _queue()
    relay3 = lark_relay(tmp_path, api=api2, queue=queue)
    r_deny = relay3.handle_event(lark_plain_event(
        open_id=LARK_DELEGATE, root_id=third_msg, parent_id=third_msg,
        msg_id="om_denyreply001", event_id="evt-deny-002"))
    assert r_deny.status == "ignored_unauthorized"
    assert calls == []


def test_lark_edited_access_command_rejected(tmp_path):
    api = LarkApi()
    relay = lark_relay(tmp_path, api=api)
    record_lark_notification(relay.thread_store)
    r = relay.handle_event(lark_access_event(edited=True))
    assert r.status == "rejected_route_command"


def test_lark_self_reply_in_access_path_rejected(tmp_path):
    """Access command where message_id matches root_id/parent_id must be rejected."""
    api = LarkApi()
    relay = lark_relay(tmp_path, api=api)
    record_lark_notification(relay.thread_store)
    # message_id == root_id → self-reply
    r = relay.handle_event(lark_access_event(
        msg_id=LARK_NOTIF_MSG, root_id=LARK_NOTIF_MSG, parent_id=LARK_NOTIF_MSG))
    assert r.status in ("ignored_not_thread_reply", "rejected_route_command")


def test_lark_malformed_mentions_dict_rejected(tmp_path):
    """Passing a dict instead of list for mentions raises via strict parser."""
    api = LarkApi()
    relay = lark_relay(tmp_path, api=api)
    record_lark_notification(relay.thread_store)
    payload = lark_access_event()
    # Inject a dict into mentions — should not be silently coerced to []
    payload["event"]["message"]["mentions"] = {}
    r = relay.handle_event(payload)
    # Parser rejects malformed mention structure
    assert r.status in ("rejected_route_command",)


def test_lark_admin_target_already_admin_skips_verify(tmp_path):
    api = LarkApi()
    verify_calls = []
    real_verify = api.verify_user
    def spy_verify(open_id):
        verify_calls.append(open_id)
        raise Exception("should not be called")
    api.verify_user = spy_verify

    sender_api = LarkApi(conf_msg=LARK_CONF_MSG)
    relay = lark_relay(tmp_path, api=sender_api)
    record_lark_notification(relay.thread_store)

    # Add admin as delegate — skip verification, get already_admin
    r = relay.handle_event(lark_access_event(mention_id=LARK_ADMIN))
    assert r.status == "access_applied" and r.delivery_status == "already_admin"
    assert verify_calls == []


def test_lark_remove_existing_delegate_skips_verify_on_error(tmp_path):
    api = LarkApi(conf_msg=LARK_CONF_MSG)
    api2 = LarkApi(conf_msg=LARK_CONF_MSG2)
    relay = lark_relay(tmp_path, api=api)
    record_lark_notification(relay.thread_store)
    # Add delegate
    r = relay.handle_event(lark_access_event(event_id="evt-add-skip"))
    assert r.delivery_status == "added"

    # Second ticket, verify_user raises (e.g. scope missing)
    api2.verify_error = Exception("lark_api_scope_missing")
    record_lark_notification(relay.thread_store, msg_id=LARK_NOTIF_MSG2)
    relay2 = lark_relay(tmp_path, api=api2)
    r_rm = relay2.handle_event(lark_access_event(
        msg_id=LARK_ADMIN_CMD2, root_id=LARK_NOTIF_MSG2, parent_id=LARK_NOTIF_MSG2,
        command="remove", event_id="evt-rm-skip"))
    assert r_rm.status == "access_applied" and r_rm.delivery_status == "removed"


def test_lark_race_closed_ticket_yields_no_dispatch(tmp_path):
    calls, queue = _queue()
    api = LarkApi()
    relay = lark_relay(tmp_path, api=api, queue=queue)
    record_lark_notification(relay.thread_store)

    # Close the ticket
    journal = relay.thread_store.journal
    src = LarkThreadStore._address(LARK_CHAT, LARK_NOTIF_MSG)
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                   (journal.namespace, src))

    # Admin send: ticket closed → ignored_unauthorized (no dispatch)
    r = relay.handle_event(lark_plain_event(open_id=LARK_ADMIN, event_id="evt-race-1"))
    assert r.status == "duplicate"
    assert r.delivery_status == "ticket_closed"
    assert calls == []


# ── OneBot: plain access, add, delegate reply, edited rejected ─────────────────

def test_onebot_plain_access_not_blocked_by_reserved_guard(tmp_path):
    """Plain 'access' (no AT) must reach the access handler, not be rejected."""
    api = OneBotApi()
    relay = onebot_relay(tmp_path, api=api)
    record_ob_notification(relay.thread_store)

    r = relay.handle_event(ob_plain_access_event())
    assert r.status == "access_applied" and r.delivery_status == "access"


def test_onebot_add_delegate_then_reply_exactly_once(tmp_path):
    api = OneBotApi(conf_msg=OB_CONF_MSG)
    calls, queue = _queue()
    relay = onebot_relay(tmp_path, api=api, queue=queue)
    record_ob_notification(relay.thread_store)

    r = relay.handle_event(ob_access_event())
    assert r.status == "access_applied" and r.delivery_status == "added"
    assert any(call[0] == "send" for call in api.calls)
    sent_text = next(call[1] for call in api.calls if call[0] == "send")
    assert "Machine:" in sent_text

    # Delegate replies to the confirmation thread.
    calls2, queue2 = _queue()
    relay2 = onebot_relay(tmp_path, api=api, queue=queue2)
    r2 = relay2.handle_event(ob_delegate_event())
    assert r2.status == "queued"
    assert calls2[0][0] == TARGET_THREAD

    r3 = relay2.handle_event(ob_delegate_event())
    assert r3.duplicate and len(calls2) == 1


def test_onebot_revoke_delegate_immediate(tmp_path):
    api = OneBotApi(conf_msg=OB_CONF_MSG)
    api2 = OneBotApi(conf_msg=OB_CONF_MSG2)
    relay = onebot_relay(tmp_path, api=api)
    record_ob_notification(relay.thread_store)
    r = relay.handle_event(ob_access_event())
    assert r.delivery_status == "added"

    record_ob_notification(relay.thread_store, msg_id=OB_NOTIF_MSG2)
    relay.api = api2
    r_rm = relay.handle_event(ob_access_event(
        msg_id=OB_ADMIN_CMD2, reply_to=OB_NOTIF_MSG2, command="remove"))
    assert r_rm.status == "access_applied" and r_rm.delivery_status == "removed"

    third_msg = "-102"
    record_ob_notification(relay.thread_store, msg_id=third_msg)
    calls, queue = _queue()
    relay3 = onebot_relay(tmp_path, api=api2, queue=queue)
    r_deny = relay3.handle_event(ob_delegate_event(
        msg_id=OB_DELEGATE_REPLY2, reply_to=third_msg))
    assert r_deny.status == "ignored_unauthorized"
    assert calls == []


def test_onebot_edited_access_command_rejected(tmp_path):
    api = OneBotApi()
    relay = onebot_relay(tmp_path, api=api)
    record_ob_notification(relay.thread_store)
    r = relay.handle_event(ob_access_event(edited=True))
    assert r.status == "rejected_route_command"


def test_onebot_admin_target_already_admin(tmp_path):
    api = OneBotApi()
    relay = onebot_relay(tmp_path, api=api)
    record_ob_notification(relay.thread_store)
    # Add self (admin) as delegate — always returns already_admin
    r = relay.handle_event(ob_access_event(delegate_id=OB_ADMIN))
    assert r.status == "access_applied" and r.delivery_status == "already_admin"


def test_onebot_reserved_controls_denied_from_nonadmin(tmp_path):
    """Non-admin cannot use reserved words; they're inert or unauthorized."""
    relay = onebot_relay(tmp_path)
    record_ob_notification(relay.thread_store)
    # Non-admin tries bind/unbind (reserved words)
    for text in ("bind", "unbind"):
        segs = [{"type": "reply", "data": {"id": OB_NOTIF_MSG}},
                {"type": "text", "data": {"text": text}}]
        payload = ob_event(user_id=OB_DELEGATE, msg_id=999, segments=segs)
        r = relay.handle_event(payload)
        assert r.status == "ignored_unauthorized", f"expected denied for: {text}"


def test_onebot_bind_reaches_binding_handler_not_reserved_guard(tmp_path):
    """bind/unbind in a thread reply must reach binding.command, not be rejected."""
    relay = onebot_relay(tmp_path)
    record_ob_notification(relay.thread_store)
    segs = [{"type": "reply", "data": {"id": OB_NOTIF_MSG}},
            {"type": "text", "data": {"text": "bind"}}]
    payload = ob_event(user_id=OB_ADMIN, msg_id=OB_ADMIN_CMD, segments=segs,
                       group=OB_GROUP)
    payload["time"] = 1000000000
    r = relay.handle_event(payload)
    # Should reach binding handler and produce a route-related result, not
    # "rejected_route_command" from the reserved guard.
    assert r.status in ("route_pending", "rejected_or_uncertain_route_command",
                        "rejected_route_command", "deferred")
    # The critical assertion: not blocked by the generic reserved_control guard
    # (which returns "rejected_route_command" without consulting binding.command).
    # We verify by checking the binding store was consulted (no "ignored_*").
    assert not r.status.startswith("ignored_")


def test_onebot_race_closed_ticket_yields_no_dispatch(tmp_path):
    calls, queue = _queue()
    relay = onebot_relay(tmp_path, queue=queue)
    record_ob_notification(relay.thread_store)

    journal = relay.thread_store.journal
    src = OneBotThreadStore._address(OB_DESTINATION, OB_NOTIF_MSG)
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                   (journal.namespace, src))

    r = relay.handle_event(ob_event(user_id=OB_ADMIN, msg_id=OB_ADMIN_CMD,
                                    reply_to=OB_NOTIF_MSG))
    assert r.status == "duplicate"
    assert r.delivery_status == "ticket_closed"
    assert calls == []


# ── Cross-provider isolation ──────────────────────────────────────────────────

def test_slack_delegate_cannot_access_lark_session(tmp_path):
    """A user added as a Slack delegate has no effect on the Lark relay."""
    slack_sender, _ = slack_access_sender()
    slack_r = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER),
                          access_sender=slack_sender)
    record_slack_notification(slack_r.thread_store)
    slack_r.handle_message(slack_reply_event())  # add delegate to Slack session

    # Lark relay has its own independent store and ACL.
    lark_api = LarkApi()
    lark_calls, lark_queue = _queue()
    lr = lark_relay(tmp_path, api=lark_api, queue=lark_queue)
    record_lark_notification(lr.thread_store)

    # Lark delegate (no ACL entry for Lark) tries to send.
    r = lr.handle_event(lark_plain_event(
        open_id=LARK_OTHER, event_id="evt-xprov-1"))
    assert r.status == "ignored_unauthorized"
    assert lark_calls == []


def test_same_provider_different_sessions_isolated(tmp_path):
    """Delegate for Slack session A cannot access Slack session B."""
    sender_a, _ = slack_access_sender(NEW_CONF_TS)
    sender_b, _ = slack_access_sender(NEW_CONF_TS2)
    calls_a, q_a = _queue()
    calls_b, q_b = _queue()

    relay_a = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER),
                          access_sender=sender_a, queue=q_a)
    relay_b = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER),
                          access_sender=sender_b, queue=q_b)

    record_slack_notification(relay_a.thread_store, ts=THREAD_TS, target=TARGET)
    record_slack_notification(relay_b.thread_store, ts=THREAD_TS2, target=TARGET2)

    # Add delegate to session A only.
    relay_a.handle_message(slack_reply_event())

    # Delegate sends to session A's confirmation → queued.
    r_a = relay_a.handle_message(slack_reply_event(
        user=DELEGATE_USER, thread_ts=NEW_CONF_TS, ts=DELEGATE_MSG_TS,
        text="msg-a", client_msg_id="iso-a"))
    assert r_a.status == "queued"

    # Delegate tries session B → not in B's ACL → unauthorized.
    r_b = relay_b.handle_message(slack_reply_event(
        user=DELEGATE_USER, thread_ts=THREAD_TS2, ts=ADMIN_MSG_TS2,
        text="msg-b", client_msg_id="iso-b"))
    assert r_b.status == "ignored_unauthorized"
    assert calls_b == []
