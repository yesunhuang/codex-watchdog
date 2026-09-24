"""Tests for runtime-local exact Codex session destination bindings."""
import json

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.relay import RelayTarget
from codex_watchdog.session_routes import SessionRoutes
from codex_watchdog.slack_mapping import SlackThreadStore

CHANNEL = "C12345678"
SCOPE = "C00000001"
USER = "U12345678"
DEST = "C99999999"
DEST2 = "C88888888"

TS_100 = "1789111600.000100"
TS_200 = "1789111600.000200"
TS_300 = "1789111600.000300"
TS_050 = "1789111600.000050"


def target(n: int) -> RelayTarget:
    return RelayTarget(
        "workspace-" + str(n),
        "11111111-2222-4333-8444-{:012d}".format(n),
        "process_local",
    )


def thread_ts(n: int) -> str:
    return "1789111600.{:06d}".format(n)


def record_ticket(store: SlackThreadStore, n: int, channel: str = CHANNEL) -> str:
    fp = sha256_text("notice:" + str(n))
    store.record_thread(channel, thread_ts(n), target(n), fp)
    return target(n).thread_id


def make_routes(store: SlackThreadStore) -> SessionRoutes:
    return SessionRoutes(store, provider="slack", scope=SCOPE)


def active_count(store: SlackThreadStore) -> int:
    journal = store.journal
    with journal.transaction() as db:
        return len(journal.active(db))


def close_ticket(store: SlackThreadStore, n: int) -> None:
    journal = store.journal
    key = store.thread_key(CHANNEL, thread_ts(n))
    with journal.transaction() as db:
        db.execute(
            "UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
            (journal.namespace, key),
        )


# ── Persistence ─────────────────────────────────────────────────────────────

def test_bind_and_destination_persists_across_restart(tmp_path):
    store = SlackThreadStore(tmp_path)
    thread_id = record_ticket(store, 1)
    routes = make_routes(store)
    result = routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                          command_ts=TS_100)
    assert result == {"status": "bound", "thread_id": thread_id, "destination": DEST, "ack_pending": True}

    routes2 = make_routes(SlackThreadStore(tmp_path))
    assert routes2.destination(thread_id) == DEST


# ── Active and closed tickets ────────────────────────────────────────────────

def test_bind_active_ticket_closes_it(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    assert active_count(store) == 1

    routes = make_routes(store)
    result = routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                          command_ts=TS_100)
    assert result["status"] == "bound"
    assert active_count(store) == 0


def test_bind_closed_ticket_accepted_for_control(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    close_ticket(store, 1)
    assert active_count(store) == 0

    routes = make_routes(store)
    result = routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                          command_ts=TS_100)
    assert result["status"] == "bound"
    assert routes.destination(target(1).thread_id) == DEST


# ── 4-ticket count invariant ─────────────────────────────────────────────────

def test_four_ticket_count_unchanged_only_source_closed(tmp_path):
    store = SlackThreadStore(tmp_path)
    for n in range(1, 5):
        record_ticket(store, n)
    assert active_count(store) == 4

    routes = make_routes(store)
    routes.apply("event:E002", CHANNEL, thread_ts(2), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    assert active_count(store) == 3  # only ticket 2 closed

    journal = store.journal
    with journal.transaction() as db:
        count = db.execute("SELECT COUNT(*) FROM records WHERE kind='threads'").fetchone()[0]
        assert count == 4
        row = db.execute(
            "SELECT active FROM records WHERE kind='threads' AND key=?",
            (store.thread_key(CHANNEL, thread_ts(2)),),
        ).fetchone()
        assert row is not None and row[0] == 0
        for n in (1, 3, 4):
            row = db.execute(
                "SELECT active FROM records WHERE kind='threads' AND key=?",
                (store.thread_key(CHANNEL, thread_ts(n)),),
            ).fetchone()
            assert row is not None and row[0] == 1


# ── Duplicate detection ──────────────────────────────────────────────────────

def test_duplicate_event_key_returns_duplicate_does_not_reopen(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)

    r1 = routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                      command_ts=TS_100)
    assert r1["status"] == "bound"
    assert active_count(store) == 0

    r2 = routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                      command_ts=TS_100)
    assert r2["status"] == "duplicate"
    assert r2["destination"] == DEST
    assert active_count(store) == 0  # not reopened


def test_duplicate_does_not_overwrite_later_route_change(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)

    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "unbind", None,
                 command_ts=TS_200)
    assert routes.destination(target(1).thread_id) is None

    r = routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                     command_ts=TS_100)
    assert r["status"] == "duplicate"
    assert routes.destination(target(1).thread_id) is None  # later unbind preserved


# ── Session independence ─────────────────────────────────────────────────────

def test_second_session_independent(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    record_ticket(store, 2)
    routes = make_routes(store)

    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    assert routes.destination(target(2).thread_id) is None

    routes.apply("event:E002", CHANNEL, thread_ts(2), USER, "bind " + DEST2, DEST2,
                 command_ts=TS_200)
    assert routes.destination(target(1).thread_id) == DEST
    assert routes.destination(target(2).thread_id) == DEST2


def test_unbind_only_removes_one_route(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    record_ticket(store, 2)
    routes = make_routes(store)

    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    routes.apply("event:E002", CHANNEL, thread_ts(2), USER, "bind " + DEST2, DEST2,
                 command_ts=TS_200)

    r = routes.apply("event:E003", CHANNEL, thread_ts(1), USER, "unbind", None,
                     command_ts=TS_300)
    assert r["status"] == "unbound"
    assert routes.destination(target(1).thread_id) is None
    assert routes.destination(target(2).thread_id) == DEST2  # unaffected


# ── Collision ────────────────────────────────────────────────────────────────

def test_event_collision_raises_and_leaves_state_unchanged(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    record_ticket(store, 2)
    routes = make_routes(store)

    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    with pytest.raises(ValueError, match="route_command_collision"):
        routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST2,
                     command_ts=TS_100)

    assert routes.destination(target(1).thread_id) == DEST  # unchanged


# ── Unknown source ───────────────────────────────────────────────────────────

def test_unknown_source_ticket_raises_value_error(tmp_path):
    store = SlackThreadStore(tmp_path)
    routes = make_routes(store)
    with pytest.raises(ValueError, match="route_source_unknown"):
        routes.apply("event:E001", CHANNEL, thread_ts(99), USER, "bind " + DEST, DEST,
                     command_ts=TS_100)


# ── Schema version guards ────────────────────────────────────────────────────

def test_unknown_schema_version_in_route_fails_closed(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    journal = store.journal
    with journal.transaction() as db:
        rows = db.execute(
            "SELECT key, value FROM records WHERE namespace=? AND kind='session_routes'",
            (journal.namespace,),
        ).fetchall()
        assert len(rows) == 1
        key, val = rows[0]
        entry = json.loads(val)
        entry["schema_version"] = 99
        journal.put(db, "session_routes", key, entry)

    with pytest.raises(ValueError, match="session_route_schema_invalid"):
        routes.destination(target(1).thread_id)


def test_unknown_schema_version_in_command_raises_on_duplicate(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    journal = store.journal
    with journal.transaction() as db:
        rows = db.execute(
            "SELECT key, value FROM records WHERE namespace=? AND kind='route_commands'",
            (journal.namespace,),
        ).fetchall()
        assert len(rows) == 1
        key, val = rows[0]
        entry = json.loads(val)
        entry["schema_version"] = 99
        journal.put(db, "route_commands", key, entry)

    with pytest.raises(ValueError, match="route_command_schema_invalid"):
        routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                     command_ts=TS_100)


# ── claim_ack ────────────────────────────────────────────────────────────────

def test_claim_ack_returns_true_once_then_false(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    assert routes.claim_ack("event:E001") is True
    assert routes.claim_ack("event:E001") is False

    routes2 = make_routes(SlackThreadStore(tmp_path))
    assert routes2.claim_ack("event:E001") is False


def test_claim_ack_false_for_unknown_event(tmp_path):
    store = SlackThreadStore(tmp_path)
    routes = make_routes(store)
    assert routes.claim_ack("event:nonexistent") is False


def test_duplicate_ack_pending_reflects_claim_state(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    r = routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                     command_ts=TS_100)
    assert r["status"] == "duplicate" and r["ack_pending"] is True

    routes.claim_ack("event:E001")

    r = routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                     command_ts=TS_100)
    assert r["status"] == "duplicate" and r["ack_pending"] is False


# ── Journal isolation ────────────────────────────────────────────────────────

def test_existing_journal_thread_records_untouched_beyond_source_closure(tmp_path):
    store = SlackThreadStore(tmp_path)
    for n in range(1, 5):
        record_ticket(store, n)

    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    journal = store.journal
    with journal.transaction() as db:
        thread_count = db.execute(
            "SELECT COUNT(*) FROM records WHERE namespace=? AND kind='threads'",
            (journal.namespace,),
        ).fetchone()[0]
        assert thread_count == 4

        total_threads = db.execute("SELECT COUNT(*) FROM records WHERE kind='threads'").fetchone()[0]
        assert total_threads == 4

        route_count = db.execute("SELECT COUNT(*) FROM records WHERE kind='session_routes'").fetchone()[0]
        cmd_count = db.execute("SELECT COUNT(*) FROM records WHERE kind='route_commands'").fetchone()[0]
        assert route_count == 1 and cmd_count == 1


def test_destination_none_when_no_route(tmp_path):
    store = SlackThreadStore(tmp_path)
    routes = make_routes(store)
    assert routes.destination("11111111-2222-4333-8444-000000000001") is None


def test_unbind_stores_tombstone_destination_is_none(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)

    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    assert routes.destination(target(1).thread_id) == DEST

    routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "unbind", None,
                 command_ts=TS_200)
    assert routes.destination(target(1).thread_id) is None

    journal = store.journal
    with journal.transaction() as db:
        count = db.execute("SELECT COUNT(*) FROM records WHERE kind='session_routes'").fetchone()[0]
        assert count == 1  # tombstone record remains


# ── Regression: corrupt route identity ──────────────────────────────────────

def test_destination_raises_on_provider_mismatch_in_stored_route(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    journal = store.journal
    with journal.transaction() as db:
        rows = db.execute(
            "SELECT key, value FROM records WHERE namespace=? AND kind='session_routes'",
            (journal.namespace,),
        ).fetchall()
        key, val = rows[0]
        entry = json.loads(val)
        entry["provider"] = "other_provider"
        journal.put(db, "session_routes", key, entry)

    with pytest.raises(ValueError, match="session_route_identity_mismatch"):
        routes.destination(target(1).thread_id)


def test_destination_raises_on_invalid_destination_in_stored_route(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    journal = store.journal
    with journal.transaction() as db:
        rows = db.execute(
            "SELECT key, value FROM records WHERE namespace=? AND kind='session_routes'",
            (journal.namespace,),
        ).fetchall()
        key, val = rows[0]
        entry = json.loads(val)
        entry["destination"] = True  # bool, not a channel ID string
        journal.put(db, "session_routes", key, entry)

    with pytest.raises(ValueError, match="session_route_destination_invalid"):
        routes.destination(target(1).thread_id)


def test_apply_raises_on_unknown_schema_in_existing_route(tmp_path):
    """Unknown/malformed persisted schema must never be silently overwritten."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    journal = store.journal
    with journal.transaction() as db:
        rows = db.execute(
            "SELECT key, value FROM records WHERE namespace=? AND kind='session_routes'",
            (journal.namespace,),
        ).fetchall()
        key, val = rows[0]
        entry = json.loads(val)
        entry["schema_version"] = 99
        journal.put(db, "session_routes", key, entry)

    with pytest.raises(ValueError, match="session_route_schema_invalid"):
        routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "unbind", None,
                     command_ts=TS_200)


# ── Regression: uppercase UUID canonical lookup ──────────────────────────────

def test_destination_uppercase_uuid_finds_lowercase_stored_route(tmp_path):
    store = SlackThreadStore(tmp_path)
    thread_id = record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    upper = thread_id.upper()
    assert routes.destination(upper) == DEST


# ── Regression: stale command ordering ──────────────────────────────────────

def test_newer_unbind_then_older_new_bind_remains_unbound(tmp_path):
    """Older previously-unseen bind cannot override a newer unbind."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)

    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "unbind", None,
                 command_ts=TS_200)
    assert routes.destination(target(1).thread_id) is None

    # Old bind event arrives late (never seen before, but ts < unbind ts)
    r = routes.apply("event:E003", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                     command_ts=TS_050)
    assert r["status"] == "stale"
    assert r["ack_pending"] is False
    assert routes.destination(target(1).thread_id) is None  # still unbound
    # Stale receipt should not allow claim_ack
    assert routes.claim_ack("event:E003") is False


def test_newer_bind_then_older_new_unbind_remains_bound(tmp_path):
    """Older previously-unseen unbind cannot override a newer bind."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)

    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_200)
    assert routes.destination(target(1).thread_id) == DEST

    # Old unbind event arrives late (never seen, ts < bind ts)
    r = routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "unbind", None,
                     command_ts=TS_050)
    assert r["status"] == "stale"
    assert r["ack_pending"] is False
    assert routes.destination(target(1).thread_id) == DEST  # still bound


def test_stale_equal_timestamp_different_event_fails_closed(tmp_path):
    """Equal timestamps with different events fail closed via stale."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)

    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    r = routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "unbind", None,
                     command_ts=TS_100)
    assert r["status"] == "stale"
    assert routes.destination(target(1).thread_id) == DEST  # unchanged


def test_stale_repeat_never_acks_or_mutates(tmp_path):
    """A second arrival of a stale event also returns stale without mutation."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)

    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_200)

    r1 = routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "unbind", None,
                      command_ts=TS_050)
    assert r1["status"] == "stale"

    # Second arrival of the same stale event: dedupe receipt already written
    r2 = routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "unbind", None,
                      command_ts=TS_050)
    assert r2["status"] == "duplicate"
    assert r2["ack_pending"] is False  # stale receipt has ack_claimed=True
    assert routes.destination(target(1).thread_id) == DEST  # unchanged
    assert routes.claim_ack("event:E002") is False


# ── Regression: strict API boolean checks ───────────────────────────────────

def test_apply_invalid_destination_bool_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    with pytest.raises(ValueError, match="route_apply_destination_invalid"):
        routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind", True,
                     command_ts=TS_100)


def test_apply_invalid_command_ts_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    with pytest.raises(ValueError, match="route_apply_command_ts_invalid"):
        routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                     command_ts="not-a-ts")


@pytest.mark.parametrize("patch", [
    {"schema_version": True}, {"destination": "not-a-channel"},
    {"last_command_ts": None}, {"thread_id": target(2).thread_id},
])
def test_corrupt_route_blocks_both_read_and_overwrite_without_ticket_change(tmp_path, patch):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("first", CHANNEL, thread_ts(1), USER, "bind #first", DEST,
                 command_ts=TS_100)
    journal = store.journal
    with journal.transaction() as db:
        key = routes._route_key(target(1).thread_id)
        entry = journal.get(db, "session_routes", key)
        entry.update(patch)
        journal.put(db, "session_routes", key, entry)
    with pytest.raises(ValueError):
        routes.destination(target(1).thread_id)
    with pytest.raises(ValueError):
        routes.apply("second", CHANNEL, thread_ts(1), USER, "unbind", None,
                     command_ts=TS_200)
    with journal.transaction() as db:
        assert journal.get(db, "session_routes", key) == entry
        assert journal.get(db, "route_commands", routes._command_key("second")) is None


def test_numeric_timestamp_equality_cannot_reverse_unbind(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("new", CHANNEL, thread_ts(1), USER, "unbind", None,
                 command_ts="1789111601.1")
    result = routes.apply("old", CHANNEL, thread_ts(1), USER, "bind #old", DEST,
                          command_ts="1789111601.10")
    assert result["status"] == "stale"
    assert routes.destination(target(1).thread_id) is None
    assert routes.claim_ack("old") is False


def test_command_must_be_newer_than_its_parent(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    with pytest.raises(ValueError, match="command_ts"):
        make_routes(store).apply("old", CHANNEL, thread_ts(1), USER, "unbind", None,
                                 command_ts=thread_ts(1))
    assert active_count(store) == 1


# ── claim_binding_hello ──────────────────────────────────────────────────────

def test_claim_binding_hello_returns_target_dest_fingerprint(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    result = routes.claim_binding_hello("event:E001")
    assert result is not None
    tgt, dest, fp = result
    assert tgt.thread_id == target(1).thread_id
    assert dest == DEST
    assert isinstance(fp, str) and len(fp) == 64


def test_claim_binding_hello_returns_none_second_call(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    assert routes.claim_binding_hello("event:E001") is not None
    assert routes.claim_binding_hello("event:E001") is None


def test_claim_binding_hello_returns_none_for_absent(tmp_path):
    store = SlackThreadStore(tmp_path)
    routes = make_routes(store)
    assert routes.claim_binding_hello("event:nonexistent") is None


def test_claim_binding_hello_returns_none_for_unbind(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "unbind", None,
                 command_ts=TS_200)
    # E002 is an unbind, no hello
    assert routes.claim_binding_hello("event:E002") is None


def test_claim_binding_hello_returns_none_for_stale(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_200)
    r = routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                     command_ts=TS_050)
    assert r["status"] == "stale"
    assert routes.claim_binding_hello("event:E002") is None


def test_claim_binding_hello_returns_none_for_superseded(tmp_path):
    """A newer bind overwrites the route; the older command's hello is denied."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    # Supersede with a different destination
    routes.apply("event:E002", CHANNEL, thread_ts(1), USER, "bind " + DEST2, DEST2,
                 command_ts=TS_200)
    assert routes.claim_binding_hello("event:E001") is None


def test_claim_binding_hello_stable_fingerprint_deterministic(tmp_path):
    """Same event_key always produces the same fingerprint."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    result = routes.claim_binding_hello("event:E001")
    assert result is not None
    _, _, fp = result
    # Fingerprint must equal sha256 of the canonical form
    from codex_watchdog.models import sha256_text
    expected = sha256_text(f"hello\0{SCOPE}\0event:E001")
    assert fp == expected


# ── finish_binding_hello ──────────────────────────────────────────────────────

def test_finish_binding_hello_records_sent(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    routes.claim_binding_hello("event:E001")
    routes.finish_binding_hello("event:E001", status="sent")

    journal = store.journal
    with journal.transaction() as db:
        key = routes._command_key("event:E001")
        entry = journal.get(db, "route_commands", key)
    assert entry["hello_claimed"] is True
    assert entry["hello_status"] == "sent"


def test_finish_binding_hello_records_uncertain(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    routes.claim_binding_hello("event:E001")
    routes.finish_binding_hello("event:E001", status="uncertain")

    journal = store.journal
    with journal.transaction() as db:
        key = routes._command_key("event:E001")
        entry = journal.get(db, "route_commands", key)
    assert entry["hello_status"] == "uncertain"


def test_finish_binding_hello_noop_if_not_claimed(tmp_path):
    """finish_binding_hello does nothing when hello_claimed is absent/False."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)
    # Do not claim first — finish must be a no-op
    routes.finish_binding_hello("event:E001", status="sent")

    journal = store.journal
    with journal.transaction() as db:
        key = routes._command_key("event:E001")
        entry = journal.get(db, "route_commands", key)
    assert entry.get("hello_claimed", False) is False
    assert "hello_status" not in entry


def test_finish_binding_hello_noop_for_absent_command(tmp_path):
    store = SlackThreadStore(tmp_path)
    routes = make_routes(store)
    routes.finish_binding_hello("event:nonexistent", status="sent")  # must not raise


def test_finish_binding_hello_invalid_status_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    routes = make_routes(store)
    with pytest.raises(ValueError):
        routes.finish_binding_hello("event:E001", status="claimed")


def test_existing_command_without_hello_claimed_compatible(tmp_path):
    """Old command records lacking hello_claimed are still usable by claim_ack."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    # Strip hello_claimed field to simulate an old record
    journal = store.journal
    with journal.transaction() as db:
        key = routes._command_key("event:E001")
        entry = journal.get(db, "route_commands", key)
        entry.pop("hello_claimed", None)
        entry.pop("hello_status", None)
        journal.put(db, "route_commands", key, entry)

    # claim_ack still works on the old record
    assert routes.claim_ack("event:E001") is True
    # claim_binding_hello sees absent hello_claimed as False and can claim
    # (but ack already claimed, so it verifies ack_claimed independently)
    # Rebuild with fresh apply since ack was consumed
    store2 = SlackThreadStore(tmp_path)
    record_ticket(store2, 2)
    routes2 = make_routes(store2)
    routes2.apply("event:E002", CHANNEL, thread_ts(2), USER, "bind " + DEST, DEST,
                  command_ts=TS_100)
    journal2 = store2.journal
    with journal2.transaction() as db:
        key2 = routes2._command_key("event:E002")
        entry2 = journal2.get(db, "route_commands", key2)
        entry2.pop("hello_claimed", None)
        entry2.pop("hello_status", None)
        journal2.put(db, "route_commands", key2, entry2)
    # claim_binding_hello treats absent as False → can claim
    result = routes2.claim_binding_hello("event:E002")
    assert result is not None


@pytest.mark.parametrize("damage", [
    {"hello_claimed": "yes"},
    {"hello_claimed": False, "hello_status": "sent"},
    {"hello_claimed": False, "hello_status": "invalid"},
    {"hello_claimed": False, "hello_status": []},
])
def test_malformed_hello_claimed_fails_closed(tmp_path, damage):
    """Non-boolean hello_claimed in a stored record must be rejected by claim_binding_hello."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("event:E001", CHANNEL, thread_ts(1), USER, "bind " + DEST, DEST,
                 command_ts=TS_100)

    journal = store.journal
    with journal.transaction() as db:
        key = routes._command_key("event:E001")
        entry = journal.get(db, "route_commands", key)
        entry.update(damage)
        journal.put(db, "route_commands", key, entry)

    # claim_binding_hello must not treat "yes" as False and proceed
    # The entry.get("hello_claimed", False) is True check will be False (string "yes" is not True)
    # but "yes" is truthy. We need to verify strict bool check.
    # Since "yes" is not True, it will attempt to claim — but we mandate strict bool.
    # The implementation uses `is True` which is strict. "yes" is not True → allowed to claim?
    # Per spec: "malformed claimed/status state fails closed". We must add a check.
    # This test verifies the implementation rejects it.
    result = routes.claim_binding_hello("event:E001")
    # With strict `is True` check: "yes" is not True, so it would proceed to claim.
    # But "yes" is also not False. The spec says malformed must fail closed.
    # The validate path must detect this.
    assert result is None  # malformed state must block the claim


def test_malformed_ack_receipt_is_never_claimed(tmp_path):
    store = SlackThreadStore(tmp_path)
    record_ticket(store, 1)
    routes = make_routes(store)
    routes.apply("first", CHANNEL, thread_ts(1), USER, "unbind", None, command_ts=TS_100)
    journal = store.journal
    with journal.transaction() as db:
        key = routes._command_key("first")
        entry = journal.get(db, "route_commands", key)
        entry["ack_claimed"] = 0
        journal.put(db, "route_commands", key, entry)
    with pytest.raises(ValueError, match="route_command_state_invalid"):
        routes.claim_ack("first")
