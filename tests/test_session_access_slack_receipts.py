"""Slack receipt compatibility, physical redelivery, and fail-closed collisions."""
import pytest

from codex_watchdog.models import sha256_text

import test_session_access_relays as fixtures


@pytest.fixture
def slack(tmp_path):
    calls, queue = fixtures._queue()
    relay = fixtures.slack_relay(
        tmp_path, queue=queue, admins=(fixtures.ADMIN_USER, fixtures.OTHER_USER),
    )
    fixtures.record_slack_notification(relay.thread_store)
    fixtures.record_slack_notification(relay.thread_store, ts=fixtures.THREAD_TS2)
    event = fixtures.slack_reply_event(
        ts="1760000100.000100", text="Continue the fixture task",
        client_msg_id="fixture-receipt-client",
    )
    return relay, calls, event


def snapshot(store):
    journal = store.journal
    with journal.transaction() as db:
        return db.execute(
            "SELECT kind,key,value,active FROM records WHERE namespace=? ORDER BY kind,key",
            (journal.namespace,),
        ).fetchall()


@pytest.mark.parametrize("delivery", ("enqueued", "exception"))
def test_legacy_event_receipt_remains_inert_with_new_relay_arguments(slack, delivery):
    relay, calls, event = slack
    store = relay.thread_store
    # The previous API wrote only an event receipt, without message identity.
    assert store.claim_reply(
        event_key="event:receipt-first", channel_id=fixtures.CHANNEL,
        thread_ts=fixtures.THREAD_TS, instruction_id="legacy-fixture",
        text=event["text"],
    ) == (True, None)
    store.finish_reply(
        "event:receipt-first",
        state_value="delivered" if delivery == "enqueued" else "uncertain",
        delivery_status=delivery,
    )
    assert "admission_schema" not in store.lookup_reply("event:receipt-first")
    before = snapshot(store)

    same_event = relay.handle_message(event, event_id="receipt-first")
    assert (same_event.status, same_event.delivery_status) == ("duplicate", delivery)
    # Old records cannot reconstruct physical identity, but their ticket stays closed.
    changed_event = relay.handle_message(event, event_id="receipt-second")
    assert (changed_event.status, changed_event.delivery_status) == ("duplicate", "ticket_closed")
    assert calls == []
    assert snapshot(store) == before


@pytest.mark.parametrize("event_id", ("receipt-second", None), ids=("new-event-id", "client-fallback"))
def test_same_physical_message_with_changed_event_identity_is_duplicate(slack, event_id):
    relay, calls, event = slack
    first = relay.handle_message(event, event_id="receipt-first")
    assert first.status == "queued"
    before = snapshot(relay.thread_store)

    replay = relay.handle_message(event, event_id=event_id)
    assert replay.duplicate
    assert (replay.status, replay.delivery_status) == ("duplicate", "enqueued")
    assert len(calls) == 1
    assert snapshot(relay.thread_store) == before


@pytest.mark.parametrize("change", ("source", "text", "sender"))
def test_physical_message_collision_cannot_change_its_binding(slack, change):
    relay, calls, event = slack
    assert relay.handle_message(event, event_id="receipt-first").status == "queued"
    before = snapshot(relay.thread_store)
    replay = dict(event)
    if change == "source":
        replay["thread_ts"] = fixtures.THREAD_TS2
    elif change == "text":
        replay["text"] = "Different fixture instruction"
    else:
        replay["user"] = fixtures.OTHER_USER  # Also an admin; binding must still match.

    result = relay.handle_message(replay, event_id="receipt-second")
    assert result.status == "rejected_state_or_collision"
    assert len(calls) == 1
    assert snapshot(relay.thread_store) == before


@pytest.mark.parametrize("kind", ("events", "messages"))
def test_null_ordinary_receipt_reserves_identity_without_consuming_ticket(slack, kind):
    relay, calls, event = slack
    journal = relay.thread_store.journal
    identity = "event:receipt-first" if kind == "events" else fixtures.CHANNEL + "\0" + event["ts"]
    with journal.transaction() as db:
        db.execute(
            "INSERT INTO records(namespace,kind,key,value) VALUES(?,?,?,?)",
            (journal.namespace, kind, sha256_text(identity), "null"),
        )
    before = snapshot(relay.thread_store)

    result = relay.handle_message(event, event_id="receipt-first")
    assert result.status == "rejected_state_or_collision"
    assert calls == []
    assert snapshot(relay.thread_store) == before


@pytest.mark.parametrize("stored", (None, "null", "[]", "{}"),
                         ids=("missing", "null", "list", "unbound-dict"))
def test_physical_receipt_requires_its_original_bound_event(slack, stored):
    relay, calls, event = slack
    assert relay.handle_message(event, event_id="receipt-first").status == "queued"
    journal = relay.thread_store.journal
    with journal.transaction() as db:
        address = (journal.namespace, sha256_text("event:receipt-first"))
        if stored is None:
            db.execute("DELETE FROM records WHERE namespace=? AND kind='events' AND key=?", address)
        else:
            db.execute("UPDATE records SET value=? WHERE namespace=? AND kind='events' AND key=?",
                       (stored, *address))
    before = snapshot(relay.thread_store)

    result = relay.handle_message(event, event_id="receipt-second")
    assert result.status == "rejected_state_or_collision"
    assert len(calls) == 1
    assert snapshot(relay.thread_store) == before
