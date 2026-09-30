"""A physical provider message cannot cross from ACL commands to ordinary replies.

All cases use two active notifications for the same exact target and verify that
changed redelivery leaves the second source and durable journal unchanged.
"""
from __future__ import annotations


from test_session_access_relays import (
    # Slack constants
    CHANNEL, ADMIN_USER, DELEGATE_USER,
    TARGET,
    # Lark constants
    LARK_CHAT, LARK_ADMIN, LARK_ADMIN_CMD, LARK_DELEGATE,
    LARK_NOTIF_MSG, LARK_NOTIF_MSG2,
    # OneBot constants
    OB_ADMIN, OB_DELEGATE, OB_NOTIF_MSG, OB_NOTIF_MSG2, OB_ADMIN_CMD,
    # Helpers
    _queue,
    slack_relay, lark_relay, onebot_relay,
    record_slack_notification, record_lark_notification, record_ob_notification,
    slack_access_api, slack_access_sender,
    LarkApi, OneBotApi,
    # Event builders
    slack_reply_event, lark_access_event, lark_plain_event,
    ob_event, ob_access_event,
)

# Slack timestamps for collision tests — intentionally later than any constant in
# test_session_access_relays to avoid sharing notification state across test files.
# COLL_PHYS_TS must be a reply ts that follows BOTH parent timestamps (Slack constraint).
COLL_THREAD_A_TS = "1760000040.000100"   # source A notification thread_ts
COLL_THREAD_B_TS = "1760000041.000100"   # source B notification thread_ts
COLL_PHYS_TS = "1760000042.000100"       # physical message ts (identical for both events)
COLL_CONF_TS = "1760000043.000100"       # stub confirmation ts returned by access_sender


def snapshot(relay, source_key, expected_delegates):
    journal = relay.thread_store.journal
    with journal.transaction() as db:
        assert source_key in journal.active(db)
        assert journal.get(db, "threads", source_key)["target"] == TARGET.to_dict()
        assert relay._get_session_access()._acl.delegates(db, TARGET.thread_id) == expected_delegates
        return db.execute("SELECT * FROM records ORDER BY namespace, kind, key").fetchall()


# ── Slack ──────────────────────────────────────────────────────────────────────

def test_slack_acl_then_ordinary_same_physical_id(tmp_path):
    sender, sent = slack_access_sender(COLL_CONF_TS)
    calls, queue = _queue()
    relay = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER),
                        access_sender=sender, queue=queue)

    # Two active notifications for the SAME session target (TARGET, not TARGET2).
    record_slack_notification(relay.thread_store, ts=COLL_THREAD_A_TS, target=TARGET)
    record_slack_notification(relay.thread_store, ts=COLL_THREAD_B_TS, target=TARGET)

    # Flow 1: ACL add command on source A; claims and closes source A ticket.
    r1 = relay.handle_message(slack_reply_event(
        user=ADMIN_USER, channel=CHANNEL, thread_ts=COLL_THREAD_A_TS,
        ts=COLL_PHYS_TS, text="add <@" + DELEGATE_USER + ">",
        client_msg_id="msg-coll-d1-slk"), event_id="event-acl-slack-1")
    assert r1.status == "access_applied", f"setup failed: {r1.status}"

    source_key = relay.thread_store.thread_key(CHANNEL, COLL_THREAD_B_TS)
    before = snapshot(relay, source_key, [DELEGATE_USER])

    # Flow 2: provider re-delivers same physical ts + client_msg_id on source B,
    # changed text (not an ACL command), different active mapped parent.
    r2 = relay.handle_message(slack_reply_event(
        user=ADMIN_USER, channel=CHANNEL, thread_ts=COLL_THREAD_B_TS,
        ts=COLL_PHYS_TS, text="Hello",
        client_msg_id="msg-coll-d1-slk"), event_id="event-ordinary-slack-1")

    assert calls == [], (
        f"BUG: same physical Slack message dispatched on source B "
        f"after ACL command receipt on source A "
        f"({len(calls)} dispatch(es), r2.status={r2.status!r})"
    )
    assert snapshot(relay, source_key, [DELEGATE_USER]) == before
    assert len(sent) == 1

def test_slack_ordinary_then_acl_same_physical_id(tmp_path):
    sender, sent = slack_access_sender(COLL_CONF_TS)
    calls, queue = _queue()
    relay = slack_relay(tmp_path, access_api=slack_access_api(DELEGATE_USER),
                        access_sender=sender, queue=queue)

    record_slack_notification(relay.thread_store, ts=COLL_THREAD_A_TS, target=TARGET)
    record_slack_notification(relay.thread_store, ts=COLL_THREAD_B_TS, target=TARGET)

    # Flow 1: ordinary admin reply on source A; queued exactly once.
    r1 = relay.handle_message(slack_reply_event(
        user=ADMIN_USER, channel=CHANNEL, thread_ts=COLL_THREAD_A_TS,
        ts=COLL_PHYS_TS, text="Hello",
        client_msg_id="msg-coll-d2-slk"), event_id="event-ordinary-slack-2")
    assert r1.status == "queued", f"setup failed: {r1.status}"
    assert len(calls) == 1

    source_key = relay.thread_store.thread_key(CHANNEL, COLL_THREAD_B_TS)
    before = snapshot(relay, source_key, [])

    # Flow 2: provider re-delivers same physical ts + client_msg_id on source B,
    # changed text (now an ACL add command), different active mapped parent.
    r2 = relay.handle_message(slack_reply_event(
        user=ADMIN_USER, channel=CHANNEL, thread_ts=COLL_THREAD_B_TS,
        ts=COLL_PHYS_TS, text="add <@" + DELEGATE_USER + ">",
        client_msg_id="msg-coll-d2-slk"), event_id="event-acl-slack-2")

    assert r2.status != "access_applied", (
        f"BUG: ACL grant applied on source B using same physical Slack message "
        f"already queued on source A "
        f"(r2.status={r2.status!r}, delivery={r2.delivery_status!r})"
    )
    assert len(calls) == 1, f"queue dispatch count changed to {len(calls)}"
    assert snapshot(relay, source_key, []) == before
    assert len(sent) == 0


# ── Lark ───────────────────────────────────────────────────────────────────────

def test_lark_acl_then_ordinary_same_physical_id(tmp_path):
    api = LarkApi()
    calls, queue = _queue()
    relay = lark_relay(tmp_path, api=api, queue=queue)

    # Two active notifications for the SAME session target.
    record_lark_notification(relay.thread_store, msg_id=LARK_NOTIF_MSG, target=TARGET)
    record_lark_notification(relay.thread_store, msg_id=LARK_NOTIF_MSG2, target=TARGET)

    # Flow 1: ACL add command on source A (parent=LARK_NOTIF_MSG); claims source A.
    r1 = relay.handle_event(lark_access_event(
        open_id=LARK_ADMIN, msg_id=LARK_ADMIN_CMD,
        root_id=LARK_NOTIF_MSG, parent_id=LARK_NOTIF_MSG,
        event_id="evt-col-acl-L1"))
    assert r1.status == "access_applied", f"setup failed: {r1.status}"

    source_key = relay.thread_store._address(LARK_CHAT, LARK_NOTIF_MSG2)
    before = snapshot(relay, source_key, [LARK_DELEGATE])

    # Flow 2: provider re-delivers same physical message_id on source B
    # (parent=LARK_NOTIF_MSG2), changed text, different event_id.
    r2 = relay.handle_event(lark_plain_event(
        open_id=LARK_ADMIN, msg_id=LARK_ADMIN_CMD,
        root_id=LARK_NOTIF_MSG2, parent_id=LARK_NOTIF_MSG2,
        text="Hello", event_id="evt-col-ord-L1"))

    assert calls == [], (
        f"BUG: same physical Lark message_id dispatched on source B "
        f"after ACL command receipt on source A "
        f"({len(calls)} dispatch(es), r2.status={r2.status!r})"
    )
    assert snapshot(relay, source_key, [LARK_DELEGATE]) == before
    assert sum(call[0] == "send" for call in api.calls) == 1

def test_lark_ordinary_then_acl_same_physical_id(tmp_path):
    api = LarkApi()
    calls, queue = _queue()
    relay = lark_relay(tmp_path, api=api, queue=queue)

    record_lark_notification(relay.thread_store, msg_id=LARK_NOTIF_MSG, target=TARGET)
    record_lark_notification(relay.thread_store, msg_id=LARK_NOTIF_MSG2, target=TARGET)

    # Flow 1: ordinary admin reply on source A; queued exactly once.
    r1 = relay.handle_event(lark_plain_event(
        open_id=LARK_ADMIN, msg_id=LARK_ADMIN_CMD,
        root_id=LARK_NOTIF_MSG, parent_id=LARK_NOTIF_MSG,
        text="Hello", event_id="evt-col-ord-L2"))
    assert r1.status == "queued", f"setup failed: {r1.status}"
    assert len(calls) == 1

    source_key = relay.thread_store._address(LARK_CHAT, LARK_NOTIF_MSG2)
    before = snapshot(relay, source_key, [])

    # Flow 2: provider re-delivers same physical message_id on source B
    # (parent=LARK_NOTIF_MSG2), changed shape (ACL add command), different event_id.
    r2 = relay.handle_event(lark_access_event(
        open_id=LARK_ADMIN, msg_id=LARK_ADMIN_CMD,
        root_id=LARK_NOTIF_MSG2, parent_id=LARK_NOTIF_MSG2,
        event_id="evt-col-acl-L2"))

    assert r2.status != "access_applied", (
        f"BUG: Lark ACL grant applied on source B using same physical message_id "
        f"already queued on source A "
        f"(r2.status={r2.status!r})"
    )
    assert len(calls) == 1, f"queue dispatch count changed to {len(calls)}"
    assert snapshot(relay, source_key, []) == before
    assert sum(call[0] == "send" for call in api.calls) == 0


# ── OneBot ─────────────────────────────────────────────────────────────────────

def test_onebot_acl_then_ordinary_same_physical_id(tmp_path):
    api = OneBotApi()
    calls, queue = _queue()
    relay = onebot_relay(tmp_path, api=api, queue=queue)

    # Two active notifications for the SAME session target.
    record_ob_notification(relay.thread_store, msg_id=OB_NOTIF_MSG, target=TARGET)
    record_ob_notification(relay.thread_store, msg_id=OB_NOTIF_MSG2, target=TARGET)

    # Flow 1: ACL add command on source A (reply_to=OB_NOTIF_MSG); claims source A.
    r1 = relay.handle_event(ob_access_event(
        user_id=OB_ADMIN, msg_id=OB_ADMIN_CMD,
        reply_to=OB_NOTIF_MSG, delegate_id=OB_DELEGATE, command="add"))
    assert r1.status == "access_applied", f"setup failed: {r1.status}"

    source_key = relay.thread_store._address(relay.config.destination, OB_NOTIF_MSG2)
    before = snapshot(relay, source_key, [OB_DELEGATE])

    # Flow 2: provider re-delivers same physical message_id on source B
    # (reply_to=OB_NOTIF_MSG2), changed text (plain message, no AT mention).
    r2 = relay.handle_event(ob_event(
        user_id=OB_ADMIN, msg_id=OB_ADMIN_CMD, reply_to=OB_NOTIF_MSG2))

    assert calls == [], (
        f"BUG: same physical OneBot message_id dispatched on source B "
        f"after ACL command receipt on source A "
        f"({len(calls)} dispatch(es), r2.status={r2.status!r})"
    )
    assert snapshot(relay, source_key, [OB_DELEGATE]) == before
    assert sum(call[0] == "send" for call in api.calls) == 1

def test_onebot_ordinary_then_acl_same_physical_id(tmp_path):
    api = OneBotApi()
    calls, queue = _queue()
    relay = onebot_relay(tmp_path, api=api, queue=queue)

    record_ob_notification(relay.thread_store, msg_id=OB_NOTIF_MSG, target=TARGET)
    record_ob_notification(relay.thread_store, msg_id=OB_NOTIF_MSG2, target=TARGET)

    # Flow 1: ordinary admin reply on source A; queued exactly once.
    r1 = relay.handle_event(ob_event(
        user_id=OB_ADMIN, msg_id=OB_ADMIN_CMD, reply_to=OB_NOTIF_MSG))
    assert r1.status == "queued", f"setup failed: {r1.status}"
    assert len(calls) == 1

    source_key = relay.thread_store._address(relay.config.destination, OB_NOTIF_MSG2)
    before = snapshot(relay, source_key, [])

    # Flow 2: provider re-delivers same physical message_id on source B
    # (reply_to=OB_NOTIF_MSG2), changed shape (ACL add command with AT mention).
    r2 = relay.handle_event(ob_access_event(
        user_id=OB_ADMIN, msg_id=OB_ADMIN_CMD,
        reply_to=OB_NOTIF_MSG2, delegate_id=OB_DELEGATE, command="add"))

    assert r2.status != "access_applied", (
        f"BUG: OneBot ACL grant applied on source B using same physical message_id "
        f"already queued on source A "
        f"(r2.status={r2.status!r})"
    )
    assert len(calls) == 1, f"queue dispatch count changed to {len(calls)}"
    assert snapshot(relay, source_key, []) == before
    assert sum(call[0] == "send" for call in api.calls) == 0
