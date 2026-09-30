"""Physical Slack route retries and legacy malformed-receipt boundaries."""
import json

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.slack_mapping import SlackThreadStore
from codex_watchdog.slack_poll import SlackPollingThreadStore

import test_session_routes as fixtures


EVENT = "event:route-replay-original"
RETRY = "event:route-replay-new-envelope"


@pytest.fixture(params=(SlackThreadStore, SlackPollingThreadStore), ids=("socket", "poll"))
def route(tmp_path, request):
    store = request.param(tmp_path)
    fixtures.record_ticket(store, 1)
    fixtures.record_ticket(store, 2, session=1)
    arguments = dict(event_key=EVENT, channel_id=fixtures.CHANNEL,
                     parent_ts=fixtures.thread_ts(1), user_id=fixtures.USER,
                     text="bind " + fixtures.DEST, destination=fixtures.DEST,
                     command_ts=fixtures.TS_100)
    return store, fixtures.make_routes(store), arguments


def snapshot(store):
    with store.journal.transaction() as db:
        return db.execute(
            "SELECT namespace,kind,key,value,active FROM records ORDER BY namespace,kind,key"
        ).fetchall()


def put_raw(store, kind, key, value):
    with store.journal.transaction() as db:
        db.execute(
            "INSERT INTO records(namespace,kind,key,value) VALUES(?,?,?,?) "
            "ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value",
            (store.journal.namespace, kind, key, json.dumps(value)),
        )


def physical_key(arguments):
    return sha256_text(arguments["channel_id"] + "\0" + arguments["command_ts"])


@pytest.mark.parametrize("ack_claimed", (False, True))
def test_changed_event_same_physical_command_is_inert_without_second_ack(route, ack_claimed):
    store, routes, arguments = route
    assert routes.apply(**arguments)["status"] == "bound"
    if ack_claimed:
        assert routes.claim_ack(EVENT) is True
    before = snapshot(store)

    restarted = fixtures.make_routes(type(store)(store.runtime))
    replay = restarted.apply(**dict(arguments, event_key=RETRY))
    assert replay == dict(status="duplicate", thread_id=fixtures.target(1).thread_id,
                          destination=fixtures.DEST, ack_pending=False)
    assert restarted.claim_ack(RETRY) is False
    assert restarted.claim_binding_hello(RETRY) is None
    assert snapshot(store) == before
    assert restarted.claim_ack(EVENT) is (not ack_claimed)
    assert restarted.claim_ack(EVENT) is False


@pytest.mark.parametrize("field,replacement", (
    ("text", "unbind"), ("destination", fixtures.DEST2),
    ("user_id", "U00000002"), ("parent_ts", fixtures.thread_ts(2)),
))
def test_changed_payload_or_parent_same_physical_message_rolls_back(route, field, replacement):
    store, routes, arguments = route
    assert routes.apply(**arguments)["status"] == "bound"
    before = snapshot(store)
    replay = dict(arguments, event_key=RETRY)
    replay[field] = replacement

    with pytest.raises(ValueError, match="collision"):
        routes.apply(**replay)
    assert snapshot(store) == before
    assert fixtures.active_count(store) == 1
    assert routes.destination(fixtures.target(1).thread_id) == fixtures.DEST


@pytest.mark.parametrize("receipt", (
    None, False, [], {}, {"schema_version": True},
    {"schema_version": 2, "payload_sha256": "a" * 64, "command_key": "b" * 64},
    {"schema_version": 1, "payload_sha256": "wrong", "command_key": "b" * 64},
))
def test_corrupt_physical_receipt_reserves_identity_without_consuming_ticket(route, receipt):
    store, routes, arguments = route
    put_raw(store, "route_messages", physical_key(arguments), receipt)
    before = snapshot(store)

    with pytest.raises(ValueError, match="route_message_collision"):
        routes.apply(**arguments)
    assert snapshot(store) == before
    assert fixtures.active_count(store) == 2
    assert routes.destination(fixtures.target(1).thread_id) is None


@pytest.mark.parametrize("original", ("missing", None, False, []))
def test_physical_receipt_requires_valid_original_command(route, original):
    store, routes, arguments = route
    assert routes.apply(**arguments)["status"] == "bound"
    key = routes._command_key(EVENT)
    if original == "missing":
        with store.journal.transaction() as db:
            db.execute("DELETE FROM records WHERE namespace=? AND kind='route_commands' AND key=?",
                       (store.journal.namespace, key))
    else:
        put_raw(store, "route_commands", key, original)
    before = snapshot(store)

    with pytest.raises(ValueError):
        routes.apply(**dict(arguments, event_key=RETRY))
    assert snapshot(store) == before
    assert fixtures.active_count(store) == 1


def test_previous_command_without_physical_receipt_remains_readable(route):
    store, routes, arguments = route
    assert routes.apply(**arguments)["status"] == "bound"
    with store.journal.transaction() as db:
        db.execute("DELETE FROM records WHERE namespace=? AND kind='route_messages' AND key=?",
                   (store.journal.namespace, physical_key(arguments)))
    restarted = fixtures.make_routes(type(store)(store.runtime))
    before = snapshot(store)

    receipt = restarted.command_source(EVENT, arguments["channel_id"], arguments["parent_ts"])
    assert receipt["destination"] == fixtures.DEST
    replay = restarted.apply(**arguments)
    assert replay["status"] == "duplicate"
    assert replay["ack_pending"] is True
    assert snapshot(store) == before
    assert restarted.claim_ack(EVENT) is True
    assert restarted.claim_ack(EVENT) is False
    hello = restarted.claim_binding_hello(EVENT)
    assert hello[:2] == (fixtures.target(1), fixtures.DEST)
    restarted.finish_binding_hello(EVENT, status="uncertain")
    assert restarted.claim_binding_hello(EVENT) is None
    assert fixtures.active_count(store) == 1


@pytest.mark.parametrize("receipt", (None, False, []))
@pytest.mark.parametrize("operation", ("apply", "source", "ack", "hello", "finish_hello"))
def test_malformed_legacy_command_is_never_absent_or_overwritten(route, receipt, operation):
    store, routes, arguments = route
    put_raw(store, "route_commands", routes._command_key(EVENT), receipt)
    before = snapshot(store)

    with pytest.raises(ValueError):
        if operation == "apply":
            routes.apply(**arguments)
        elif operation == "source":
            routes.command_source(EVENT, arguments["channel_id"], arguments["parent_ts"])
        elif operation == "ack":
            routes.claim_ack(EVENT)
        elif operation == "hello":
            routes.claim_binding_hello(EVENT)
        else:
            routes.finish_binding_hello(EVENT, status="uncertain")
    assert snapshot(store) == before
    assert fixtures.active_count(store) == 2
    assert routes.destination(fixtures.target(1).thread_id) is None
