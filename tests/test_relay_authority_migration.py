"""One-time imports retain authority and deduplication without choosing hosts."""
import hashlib
import itertools
import json
import os
import sqlite3
from datetime import datetime

import pytest

from codex_watchdog.binding_challenges import BindingChallenges
from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.models import sha256_text
from codex_watchdog.onebot_relay import OneBotThreadStore
from codex_watchdog.relay_authority_migration import (
    merge_poll_cursors, merge_records, merge_records_with_quarantine, read_snapshot,
)
from codex_watchdog.slack_mapping import SlackThreadStore
from codex_watchdog.relay import RelayTarget


SESSION = "11111111-2222-4333-8444-000000000001"
OTHER = "11111111-2222-4333-8444-000000000002"
STAMP = "2026-10-07T16:00:00Z"
PRINCIPAL = dict(team_id="T12345678", user_id="U12345678",
                 bot_id="B12345678", app_id="A12345678")


def row(kind, key, body, *, namespace="slack/poll-relay-state.json", active=0):
    return dict(namespace=namespace, kind=kind, key=key,
                value=json.dumps(body), fingerprint=body.get("event_fingerprint"),
                thread_id=body.get("target", {}).get("thread_id"), active=active,
                created_at=body.get("created_at"))


def ticket(number, *, session=SESSION, namespace="slack/poll-relay-state.json", active=1):
    return row("threads", "ticket-" + str(number), dict(
        target=dict(thread_id=session, remote_authority="ssh-remote+login6"),
        event_fingerprint=hashlib.sha256(str(number).encode()).hexdigest(),
        created_at="2026-10-07T16:00:{:02d}Z".format(number)),
        namespace=namespace, active=active)


def body(entry):
    return json.loads(entry["value"])


def test_login6_grant_merges_with_login3_ticket_without_widening_or_native_state():
    grant = row("slack_bot_grants", "grant", dict(schema_version=1, provider="slack",
        scope="C12345678", thread_id=SESSION, principals=[PRINCIPAL], created_at=STAMP))
    current_ticket = ticket(1)
    result = merge_records([("login6", [grant]), ("login3", [current_ticket])])
    assert body(next(entry for entry in result if entry["kind"] == "slack_bot_grants")) == body(grant)
    assert next(entry for entry in result if entry["kind"] == "threads")["active"] == 1
    assert body(grant)["principals"] == [PRINCIPAL]
    assert all(entry["kind"] not in ("owner", "native_queue", "writer_pid") for entry in result)


def test_closed_copy_dominates_active_regardless_of_node_order_or_repeat_import():
    live, closed = ticket(1), ticket(1, active=0)
    sources = [("login6", [closed]), ("login3", [live])]
    expected = merge_records(sources)
    assert expected[0]["active"] == 0
    assert merge_records(reversed(sources)) == expected
    assert merge_records([("canonical", expected), ("old-login3", [live])]) == expected
    assert live["active"] == 1  # No source mutation.


def test_last_four_is_per_provider_session_across_transport_namespaces():
    first = [ticket(i) for i in (1, 3, 5)]
    second = [ticket(i, namespace="slack/relay-state.json") for i in (2, 4, 6)]
    unrelated = [ticket(i, session=OTHER) for i in range(1, 5)]
    for entry in unrelated:
        entry["key"] = "other-" + entry["key"]
    feishu = [ticket(i, namespace="lark/fixture/relay-state.json") for i in range(1, 5)]
    result = merge_records([("login6", first + unrelated), ("login3", second + feishu)])
    slack = [entry for entry in result if entry["namespace"].startswith("slack/")
             and entry["thread_id"] == SESSION]
    assert {entry["key"] for entry in slack if entry["active"]} == {"ticket-3", "ticket-4", "ticket-5", "ticket-6"}
    assert sum(entry["active"] for entry in result if entry["thread_id"] == OTHER) == 4
    assert sum(entry["active"] for entry in result if entry["namespace"].startswith("lark/")) == 4


def test_equal_timestamp_retirement_is_deterministic_without_hostname_selection():
    entries = [ticket(i) for i in range(1, 7)]
    for entry in entries:
        value = body(entry)
        value["created_at"] = entry["created_at"] = STAMP
        entry["value"] = json.dumps(value)
    sources = [("login6", entries[:2]), ("login3", entries[2:4]), ("login5", entries[4:])]
    results = [merge_records(order) for order in itertools.permutations(sources)]
    assert all(result == results[0] for result in results)
    assert {entry["key"] for entry in results[0] if entry["active"]} == {"ticket-3", "ticket-4", "ticket-5", "ticket-6"}


@pytest.mark.parametrize("kind,field,first,second", [
    ("slack_bot_grants", "principals", [PRINCIPAL], []),
    ("session_acl", "delegates", ["U12345678"], []),
    ("session_routes", "destination", "C12345678", "C87654321"),
    ("events", "state", "uncertain", "delivered"),
    ("slack_bot_requests", "payload_sha256", "a" * 64, "b" * 64),
    ("unknown_future_kind", "choice", "old", "new"),
])
def test_conflicts_fail_closed_including_revocation_and_uncertain_receipts(kind, field, first, second):
    a = row(kind, "same", dict(created_at=STAMP, **{field: first}))
    b = row(kind, "same", dict(created_at=STAMP, **{field: second}))
    for sources in ([("6", [a]), ("3", [b])], [("3", [b]), ("6", [a])]):
        with pytest.raises(ValueError, match="^relay_authority_migration_record_conflict$"):
            merge_records(sources)


def test_unknown_nonconflicting_fields_retained_without_overwriting():
    a = row("future_kind", "same", dict(identity="fixed", custom_a={"flag": True}))
    b = row("future_kind", "same", dict(identity="fixed", custom_b=["retain"]))
    assert body(merge_records([("6", [a]), ("3", [b])])[0]) == dict(
        identity="fixed", custom_a={"flag": True}, custom_b=["retain"])


def test_compatible_registry_union_is_identity_only_and_does_not_create_grants():
    alternate = dict(PRINCIPAL, bot_id="B87654321", app_id="A87654321")
    a = row("slack_bot_users", "user", dict(schema_version=1, provider="slack",
        user_id=PRINCIPAL["user_id"], principals=[PRINCIPAL], created_at=STAMP))
    b = row("slack_bot_users", "user", dict(schema_version=1, provider="slack",
        user_id=PRINCIPAL["user_id"], principals=[alternate], created_at=STAMP))
    result = merge_records([("6", [a]), ("3", [b])])
    assert result == merge_records([("3", [b]), ("6", [a])])
    assert len(body(result[0])["principals"]) == 2
    assert len(result) == 1 and result[0]["kind"] == "slack_bot_users"


def test_invalid_registry_principal_is_never_imported():
    invalid = dict(PRINCIPAL, user_id="U87654321")
    entry = row("slack_bot_users", "user", dict(schema_version=1, provider="slack",
        user_id=PRINCIPAL["user_id"], principals=[invalid], created_at=STAMP))
    with pytest.raises(ValueError, match="^relay_authority_migration_record_invalid$"):
        merge_records([("6", [entry])])


def test_unique_dispatch_and_uncertainty_evidence_survives_merge_and_restart():
    entries = [row(kind, "evidence", dict(state="uncertain", identity=kind))
               for kind in ("events", "messages", "slack_bot_requests", "slack_bot_receipts", "notifications")]
    merged = merge_records([("6", entries), ("3", [])])
    assert merged == merge_records([("canonical", merged), ("6", entries)])
    assert len(merged) == len(entries)
    assert all(body(entry)["state"] == "uncertain" for entry in merged)


@pytest.mark.parametrize("state,pending", [("dispatching", 1), ("uncertain", 1), ("delivered", 0)])
def test_event_reference_closes_parent_and_derives_exact_pending_session(state, pending):
    parent = ticket(1)
    event = row("events", "event", dict(thread_key=parent["key"], state=state, created_at=STAMP))
    merged = merge_records([("login6", [parent]), ("login3", [event])])
    by_kind = {entry["kind"]: entry for entry in merged}
    assert by_kind["threads"]["active"] == 0
    assert by_kind["events"]["thread_id"] == SESSION
    assert by_kind["events"]["active"] == pending
    assert merge_records([("canonical", merged), ("old-login3", [event])]) == merged
    assert parent["active"] == 1 and event["thread_id"] is None


def test_event_parent_missing_or_different_session_is_not_assigned_arbitrarily():
    event = row("events", "event", dict(thread_key="ticket-1", state="uncertain", created_at=STAMP))
    with pytest.raises(ValueError, match="reference_invalid"):
        merge_records([("login3", [event])])
    event["thread_id"] = OTHER
    with pytest.raises(ValueError, match="record_conflict"):
        merge_records([("login3", [event, ticket(1)])])


@pytest.mark.parametrize("changes", [
    {"active": True}, {"active": 2}, {"value": "null"},
    {"value": '{"x":1,"x":2}'}, {"value": '{"x":NaN}'},
    {"created_at": "2026-10-07T16:00:00"}, {"thread_id": "wrong"},
    {"fingerprint": "not-a-hash"},
])
def test_malformed_rows_are_not_partial_fresh_authority(changes):
    entry = dict(ticket(1), **changes)
    with pytest.raises(ValueError, match="^relay_authority_migration_record_invalid$"):
        merge_records([("6", [entry])])


def test_same_source_or_duplicate_primary_key_is_invalid():
    entry = ticket(1)
    with pytest.raises(ValueError, match="source_invalid"):
        merge_records([("6", [entry]), ("6", [])])
    with pytest.raises(ValueError, match="source_invalid"):
        merge_records([("6", [entry, entry])])


def database(tmp_path):
    store = SlackThreadStore(tmp_path)
    store.record_thread("C12345678", "1791388800.000001",
                        RelayTarget("control-fixture", SESSION, "process_local"), "a" * 64)
    return store.journal.database


def test_real_v2_snapshot_is_read_only_and_repeatable(tmp_path):
    path = database(tmp_path)
    original = path.read_bytes()
    rows, digest = read_snapshot(path)
    assert path.read_bytes() == original
    assert len(rows) == 1 and rows[0]["active"] == 1
    assert read_snapshot(path) == (rows, digest)
    assert len(digest) == 64


def binding_fixture(runtime, monkeypatch, provider, state="pending", *, hello=None):
    """Persist established binding APIs, including updates retaining first metadata."""
    monkeypatch.setattr("codex_watchdog.lark_mapping.utc_now", lambda: STAMP)
    monkeypatch.setattr("codex_watchdog.binding_challenges.utc_now", lambda: STAMP)
    scope = sha256_text("migration-binding-fixture\0" + provider)
    if provider == "lark":
        store = LarkThreadStore(runtime, scope)
        chat, user, destination = "oc_" + "a" * 12, "ou_" + "b" * 12, "oc_" + "c" * 12
        message = lambda number: "om_" + str(3000000000 + number)
    else:
        store = OneBotThreadStore(runtime, scope)
        chat, user, destination = "group:1000001", "1000002", "private:2000001"
        message = lambda number: str(3000000 + number)
    target = RelayTarget("control-fixture", SESSION, "process_local")
    now = [datetime.fromisoformat(STAMP.replace("Z", "+00:00")).timestamp() + 30.1234567]
    challenges = BindingChallenges(store, provider, scope, clock=lambda: now[0])

    def parent(number):
        fingerprint = sha256_text("binding-notification:" + str(number))
        store.prepare_notification(fingerprint, fingerprint)
        store.finish_notification(fingerprint, chat, message(number), target)
        return message(number)

    result = challenges.begin("binding-event:1", chat, parent(1), user, "bind",
                              message_id=message(11), created_at=now[0] + 1.2345678)
    assert result["status"] == "pending"
    if state != "pending":
        completed = challenges.complete(result["challenge"], user, destination,
                                        message_id=message(21), created_at=now[0] + 1.9876543)
        assert completed["status"] == "bound"
        if hello is not None:
            assert challenges.claim_hello(completed["operation_key"]) is not None
            if hello != "claimed":
                challenges.finish_hello(completed["operation_key"], hello)
        if state in ("cleared", "rebound"):
            now[0] += 100.456789
            if state == "cleared":
                result = challenges.unbind("binding-event:2", chat, parent(2), user, "unbind",
                                           message_id=message(12), created_at=now[0] + 1.2345678)
                assert result["status"] == "unbound"
            else:
                result = challenges.begin("binding-event:2", chat, parent(2), user, "bind",
                                          message_id=message(12), created_at=now[0] + 1.2345678)
                assert result["status"] == "pending"
    return store, challenges


def raw_binding_rows(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(entry) for entry in db.execute(
            "SELECT * FROM records ORDER BY namespace,kind,key")]


def assert_binding_rows_valid(challenges, rows):
    validators = {"bind_challenges": challenges._validate_challenge,
                  "bind_operations": challenges._validate_operation,
                  "bind_completions": challenges._validate_completion,
                  "bind_operation_messages": challenges._validate_op_message}
    for entry in rows:
        if entry["kind"] in validators:
            validators[entry["kind"]](body(entry))


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("state", ["pending", "consumed", "cleared", "rebound"])
def test_real_binding_numeric_epochs_snapshot_without_rewriting_original(tmp_path, monkeypatch, provider, state):
    store, challenges = binding_fixture(tmp_path / "runtime", monkeypatch, provider, state)
    path = store.journal.database
    original, raw = path.read_bytes(), raw_binding_rows(path)
    assert_binding_rows_valid(challenges, raw)
    numeric = [entry for entry in raw if entry["kind"] in ("bind_challenges", "bind_operations")]
    assert numeric and all(isinstance(entry["created_at"], str) for entry in numeric)
    assert all(type(body(entry)["created_at"]) in (int, float) for entry in numeric)
    assert all(isinstance(float(entry["created_at"]), float) for entry in numeric)
    challenge = next(entry for entry in numeric if entry["kind"] == "bind_challenges")
    assert body(challenge)["status"] == {"pending": "pending", "consumed": "consumed",
                                        "cleared": "cleared", "rebound": "pending"}[state]
    if state in ("cleared", "rebound"):
        assert body(challenge)["generation"] == 2
        assert body(challenge)["created_at"] - float(challenge["created_at"]) >= 100
    rows, digest = read_snapshot(path)
    assert read_snapshot(path) == (rows, digest)
    assert path.read_bytes() == original and raw_binding_rows(path) == raw
    assert [(entry["namespace"], entry["kind"], entry["key"], entry["created_at"],
             entry["fingerprint"], entry["thread_id"], entry["active"], body(entry)) for entry in rows] == [
           (entry["namespace"], entry["kind"], entry["key"], entry["created_at"],
            entry["fingerprint"], entry["thread_id"], entry["active"], body(entry)) for entry in raw]
    assert merge_records([("source", rows)]) == rows


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("hello", [None, "claimed", "sent", "uncertain"])
def test_bound_completion_and_hello_statuses_keep_iso_metadata(tmp_path, monkeypatch, provider, hello):
    store, challenges = binding_fixture(tmp_path / "runtime", monkeypatch, provider, "consumed", hello=hello)
    path = store.journal.database
    original = path.read_bytes()
    rows, _ = read_snapshot(path)
    assert_binding_rows_valid(challenges, rows)
    completion = next(entry for entry in rows if entry["kind"] == "bind_completions")
    assert completion["created_at"] == body(completion)["created_at"] == STAMP
    assert body(completion)["status"] == "bound" and body(completion)["hello_status"] == hello
    assert path.read_bytes() == original


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("state", ["pending", "consumed", "cleared", "rebound"])
def test_binding_epochs_preserved_through_read_only_authority_plan(tmp_path, monkeypatch, provider, state):
    from codex_watchdog.linux_relay_migration import plan_authority
    home = (tmp_path / "codex-home").resolve()
    node = home / "watchdog-nodes" / "fixture-node"
    runtime = node / "watchdog-control" / SESSION / "runtime"
    runtime.mkdir(parents=True)
    (node / "node.json").write_text(json.dumps(dict(schema_version=1, node=node.name, codex_home=str(home))))
    repository = (tmp_path / "fixture-repo").resolve()
    repository.mkdir()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT,cwd TEXT,archived INTEGER,source TEXT,thread_source TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?,0,'vscode','user')", (SESSION, str(repository)))
    store, challenges = binding_fixture(runtime, monkeypatch, provider, state)
    raw = raw_binding_rows(store.journal.database)
    assert_binding_rows_valid(challenges, raw)
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    plan = plan_authority(home)
    assert plan["summary"]["sessions"] == {SESSION: str(repository)}
    assert plan["summary"]["records"][provider] == len(raw)
    assert len(plan["summary"]["plan_sha256"]) == 64
    assert {path: path.read_bytes() for path in home.rglob("*") if path.is_file()} == before
    assert raw_binding_rows(store.journal.database) == raw
    assert {entry["key"]: (entry["created_at"], body(entry)) for entry in plan["merged"][provider]} == {
           entry["key"]: (entry["created_at"], body(entry)) for entry in raw}


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("kind", ["bind_challenges", "bind_operations"])
def test_binding_sqlite_legacy_precision_and_unknown_fields_remain_verbatim(tmp_path, monkeypatch, provider, kind):
    store, _ = binding_fixture(tmp_path / "runtime", monkeypatch, provider)
    path = store.journal.database
    entry = next(entry for entry in raw_binding_rows(path) if entry["kind"] == kind)
    payload = body(entry)
    payload["unknown_binding_extension"] = {"retain": [True, "choice"]}
    # Older SQLite numeric-to-TEXT conversion retains 15 significant digits;
    # newer SQLite builds can preserve all digits. Do not depend on host SQLite.
    text_epoch = format(payload["created_at"], ".15g")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE records SET value=?,created_at=? WHERE kind=? AND key=?",
                   (json.dumps(payload), text_epoch, kind, entry["key"]))
    original, raw = path.read_bytes(), raw_binding_rows(path)
    rows, _ = read_snapshot(path)
    migrated = next(entry for entry in rows if entry["kind"] == kind)
    assert migrated["created_at"] == text_epoch and body(migrated) == payload
    assert path.read_bytes() == original and raw_binding_rows(path) == raw
    assert merge_records([("source", rows)]) == rows


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("kind", ["bind_challenges", "bind_operations"])
@pytest.mark.parametrize("metadata", [None, "not-an-epoch", "NaN", "Infinity", "-Infinity", "1e999",
                                      "true", "false", "+1", "01.2", ".5", "1.", " 1", STAMP])
def test_binding_snapshot_refuses_malformed_numeric_metadata_without_writes(tmp_path, monkeypatch, provider, kind, metadata):
    store, _ = binding_fixture(tmp_path / "runtime", monkeypatch, provider)
    path = store.journal.database
    with sqlite3.connect(path) as db:
        db.execute("UPDATE records SET created_at=? WHERE kind=?", (metadata, kind))
    original, raw = path.read_bytes(), raw_binding_rows(path)
    with pytest.raises(ValueError, match="^relay_authority_migration_snapshot_invalid$"):
        read_snapshot(path)
    assert path.read_bytes() == original and raw_binding_rows(path) == raw


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("kind", ["bind_challenges", "bind_operations"])
@pytest.mark.parametrize("metadata", [True, False, 1791388830, 1791388830.125])
def test_binding_merge_refuses_nontext_numeric_metadata(tmp_path, monkeypatch, provider, kind, metadata):
    store, _ = binding_fixture(tmp_path / "runtime", monkeypatch, provider)
    entry = next(entry for entry in raw_binding_rows(store.journal.database) if entry["kind"] == kind)
    entry["created_at"] = metadata
    with pytest.raises(ValueError, match="^relay_authority_migration_record_invalid$"):
        merge_records([("source", [entry])])


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("kind", ["bind_challenges", "bind_operations"])
@pytest.mark.parametrize("epoch", [True, False, None, "1791388830.125", "not-an-epoch",
                                   float("nan"), float("inf"), -float("inf"), {}, []])
def test_binding_snapshot_refuses_invalid_payload_epochs_without_writes(tmp_path, monkeypatch, provider, kind, epoch):
    store, _ = binding_fixture(tmp_path / "runtime", monkeypatch, provider)
    path = store.journal.database
    entry = next(entry for entry in raw_binding_rows(path) if entry["kind"] == kind)
    payload = body(entry)
    payload["created_at"] = epoch
    with sqlite3.connect(path) as db:
        db.execute("UPDATE records SET value=? WHERE kind=? AND key=?",
                   (json.dumps(payload), kind, entry["key"]))
    original, raw = path.read_bytes(), raw_binding_rows(path)
    with pytest.raises(ValueError, match="^relay_authority_migration_snapshot_invalid$"):
        read_snapshot(path)
    assert path.read_bytes() == original and raw_binding_rows(path) == raw


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("expires", [True, False, None, "1791388930", float("nan"), float("inf")])
def test_binding_pending_expiry_keeps_established_finite_number_guard(tmp_path, monkeypatch, provider, expires):
    store, _ = binding_fixture(tmp_path / "runtime", monkeypatch, provider)
    entry = next(entry for entry in raw_binding_rows(store.journal.database) if entry["kind"] == "bind_challenges")
    payload = body(entry)
    payload["expires_at"] = expires
    entry["value"] = json.dumps(payload)
    with pytest.raises(ValueError, match="^relay_authority_migration_record_invalid$"):
        merge_records([("source", [entry])])


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("kind,field,value", [
    ("bind_challenges", "schema_version", True),
    ("bind_challenges", "status", "bound"),
    ("bind_challenges", "generation", True),
    ("bind_challenges", "user_id", "invalid"),
    ("bind_challenges", "token_hash", "invalid"),
    ("bind_challenges", "thread_id", OTHER),
    ("bind_operations", "schema_version", True),
    ("bind_operations", "status", "bound"),
    ("bind_operations", "fingerprint", "invalid"),
    ("bind_operations", "recorded_at", ""),
    ("bind_operations", "op", "run"),
    ("bind_operations", "thread_id", OTHER),
])
def test_binding_migration_uses_existing_schema_and_exact_identity_guards(tmp_path, monkeypatch, provider, kind, field, value):
    store, _ = binding_fixture(tmp_path / "runtime", monkeypatch, provider)
    entry = next(entry for entry in raw_binding_rows(store.journal.database) if entry["kind"] == kind)
    payload = body(entry)
    payload[field] = value
    entry["value"] = json.dumps(payload)
    with pytest.raises(ValueError, match="^relay_authority_migration_record_invalid$"):
        merge_records([("source", [entry])])


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("kind", ["bind_challenges", "bind_operations"])
@pytest.mark.parametrize("change", [dict(namespace="slack/poll-relay-state.json"),
                                      dict(key="unrelated-key"), dict(thread_id=OTHER), dict(active=1)])
def test_binding_migration_refuses_wrong_namespace_key_session_or_active_ticket(tmp_path, monkeypatch, provider, kind, change):
    store, _ = binding_fixture(tmp_path / "runtime", monkeypatch, provider)
    entry = next(entry for entry in raw_binding_rows(store.journal.database) if entry["kind"] == kind)
    entry.update(change)
    with pytest.raises(ValueError, match="^relay_authority_migration_record_invalid$"):
        merge_records([("source", [entry])])


@pytest.mark.parametrize("provider", ["lark", "onebot"])
@pytest.mark.parametrize("kind", ["bind_challenges", "bind_operations"])
@pytest.mark.parametrize("change", ["metadata", "payload"])
def test_numeric_binding_duplicate_conflicts_never_choose_timestamp_winner(tmp_path, monkeypatch, provider, kind, change):
    store, _ = binding_fixture(tmp_path / "runtime", monkeypatch, provider)
    entry = next(entry for entry in raw_binding_rows(store.journal.database) if entry["kind"] == kind)
    alternate = dict(entry)
    if change == "metadata":
        alternate["created_at"] = "1791388899.25"
    else:
        payload = body(alternate)
        payload["created_at"] += 1
        alternate["value"] = json.dumps(payload)
    for copies in ([("old", [entry]), ("new", [alternate])],
                   [("new", [alternate]), ("old", [entry])]):
        with pytest.raises(ValueError, match="^relay_authority_migration_record_conflict$"):
            merge_records(copies)
        with pytest.raises(ValueError, match="^relay_authority_migration_record_conflict$"):
            merge_records_with_quarantine(copies)


@pytest.mark.parametrize("kind", ["threads", "notifications", "session_routes", "bind_completions", "future_kind"])
@pytest.mark.parametrize("stamp", ["1791388830.125", "1.79138883e9", "NaN", "2026-10-07T16:00:00"])
def test_numeric_binding_compatibility_does_not_relax_other_iso_timestamp_policy(kind, stamp):
    entry = ticket(1) if kind == "threads" else row(kind, "fixture", dict(created_at=STAMP))
    payload = body(entry)
    payload["created_at"] = stamp
    entry.update(created_at=stamp, value=json.dumps(payload))
    with pytest.raises(ValueError, match="^relay_authority_migration_record_invalid$"):
        merge_records([("source", [entry])])


def test_snapshot_includes_committed_wal_without_changing_original(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        before, digest_before = read_snapshot(path)
        writer.execute("UPDATE records SET active=0")
        writer.commit()
        main_bytes = path.read_bytes()
        after, digest_after = read_snapshot(path)
        assert before[0]["active"] == 1 and after[0]["active"] == 0
        assert digest_after != digest_before
        assert path.read_bytes() == main_bytes


@pytest.mark.parametrize("version", [0, 1, 3])
def test_snapshot_rejects_unsupported_schema_without_migrating(tmp_path, version):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=" + str(version))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="^relay_authority_migration_snapshot_invalid$"):
        read_snapshot(path)
    assert path.read_bytes() == original


def test_snapshot_rejects_missing_database(tmp_path):
    with pytest.raises(ValueError, match="snapshot_invalid"):
        read_snapshot(tmp_path / "missing.sqlite3")


def test_snapshot_rejects_symlinked_database(tmp_path):
    path = database(tmp_path)
    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(path)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) in (50, 1314):
            pytest.skip("native Windows symlink capability unavailable (WinError {})".format(exc.winerror))
        raise
    with pytest.raises(ValueError, match="snapshot_invalid"):
        read_snapshot(link)


def test_unknown_persistent_table_is_not_silently_dropped_from_snapshot(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE future_authorization(value TEXT)")
        db.execute("INSERT INTO future_authorization VALUES ('retain')")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="snapshot_invalid"):
        read_snapshot(path)
    assert path.read_bytes() == original


ROOT = "a" * 64
ROOT2 = "b" * 64
CLOSED = "c" * 64
SCOPE = "d" * 64


def slack_cursor(threads, *, after=None, **extra):
    return dict(schema_version=1, after=after, threads=threads, **extra)


def lark_parent(after, *, floor=1000, seen=(), thread_id="omt_fixture0001", pending=None, **extra):
    return dict(not_before_ms=floor, after_ms=after, seen_ids=list(seen),
                thread_id=thread_id, pending=pending, **extra)


def lark_cursor(parents, *, after_key=None, scope=SCOPE, **extra):
    return dict(schema_version=1, scope=scope, after_key=after_key, parents=parents, **extra)


def pending(mid="om_pending0001", stamp=3000, digest="e" * 64, **extra):
    return dict(message_id=mid, create_time=stamp, sha256=digest, **extra)


def test_slack_handoff_retains_later_cursor_and_retires_closed_parent_lane():
    old = slack_cursor({ROOT: "1791389141.470038", CLOSED: "1791389000.000001"},
                       after=ROOT2, closed_cursor={"old": "retire"}, old_custom=True)
    new = slack_cursor({ROOT: "1791389141.470039", ROOT2: "1791389000.000002"}, after=ROOT)
    merged = merge_poll_cursors("slack", [old, new], {ROOT, ROOT2})
    assert merged["threads"] == {ROOT: "1791389141.470039", ROOT2: "1791389000.000002"}
    assert merged["after"] == ROOT and merged["old_custom"] is True
    assert "closed_cursor" not in merged and CLOSED not in merged["threads"]
    assert merge_poll_cursors("slack", [new, old], {ROOT, ROOT2}) == merged
    assert "closed_cursor" in old  # Original state remains recoverable.


def test_slack_numeric_timestamp_order_does_not_use_float_or_text_order():
    first = slack_cursor({ROOT: "1791389141.999999999999999999"})
    second = slack_cursor({ROOT: "1791389142.000000000000000001"})
    assert merge_poll_cursors("slack", [first, second], {ROOT})["threads"][ROOT] == second["threads"][ROOT]
    alternate = slack_cursor({ROOT: "1791389142.0000000000000000010"})
    assert merge_poll_cursors("slack", [alternate, second], {ROOT}) == merge_poll_cursors(
        "slack", [second, alternate], {ROOT})


def test_slack_cursor_merge_is_idempotent_without_rewinding_historical_instruction():
    cursor = slack_cursor({ROOT: "1791389141.470039"}, after=ROOT)
    merged = merge_poll_cursors("slack", [cursor], {ROOT})
    assert merge_poll_cursors("slack", [merged, cursor], {ROOT}) == merged
    assert merged["threads"][ROOT] == "1791389141.470039"
    assert merge_poll_cursors("slack", [], {ROOT}) == slack_cursor({})


def test_lark_handoff_uses_highwater_equal_seen_union_and_pending_proof():
    lower = lark_cursor({ROOT: lark_parent(2000, seen=["om_old000001"], floor=1500)}, after_key=ROOT2)
    high_a = lark_cursor({ROOT: lark_parent(2500, seen=["om_seen00001"], pending=pending())}, after_key=ROOT)
    high_b = lark_cursor({ROOT: lark_parent(2500, seen=["om_seen00002"], pending=pending(custom="keep")),
                          CLOSED: lark_parent(7000)}, custom=True)
    merged = merge_poll_cursors("lark", [lower, high_a, high_b], {ROOT})
    parent = merged["parents"][ROOT]
    assert parent["not_before_ms"] == 1500 and parent["after_ms"] == 2500
    assert parent["seen_ids"] == ["om_seen00001", "om_seen00002"]
    assert parent["pending"] == pending(custom="keep")
    assert merged["after_key"] == ROOT and merged["custom"] is True
    assert CLOSED not in merged["parents"]
    assert merged == merge_poll_cursors("lark", [high_b, lower, high_a], {ROOT})
    assert merged == merge_poll_cursors("lark", [merged, lower, high_a], {ROOT})


@pytest.mark.parametrize("highwater,seen", [(4000, []), (3000, ["om_pending0001"])])
def test_lark_pending_covered_by_confirmed_later_cursor_is_retired_without_replay(highwater, seen):
    old = lark_cursor({ROOT: lark_parent(2000, pending=pending())})
    later = lark_cursor({ROOT: lark_parent(highwater, seen=seen)})
    merged = merge_poll_cursors("lark", [old, later], {ROOT})
    assert merged["parents"][ROOT]["pending"] is None
    assert merged["parents"][ROOT]["after_ms"] == highwater
    assert old["parents"][ROOT]["pending"] == pending()


@pytest.mark.parametrize("changes", [
    {"mid": "om_different0001"}, {"stamp": 3500}, {"digest": "f" * 64},
])
def test_lark_inconsistent_pending_identity_fails_closed_even_with_higher_cursor(changes):
    first = lark_cursor({ROOT: lark_parent(2000, pending=pending())})
    second = lark_cursor({ROOT: lark_parent(2500, pending=pending(**changes))})
    with pytest.raises(ValueError, match="^relay_authority_migration_cursor_conflict$"):
        merge_poll_cursors("lark", [first, second], {ROOT})


def test_lark_scope_and_native_provider_thread_collisions_fail_closed():
    first = lark_cursor({ROOT: lark_parent(2000)})
    for second in (
        lark_cursor({ROOT: lark_parent(2000)}, scope="f" * 64),
        lark_cursor({ROOT: lark_parent(2000, thread_id="omt_otherfixture")}),
    ):
        with pytest.raises(ValueError, match="cursor_conflict"):
            merge_poll_cursors("lark", [first, second], {ROOT})


def test_lark_unknown_thread_adopts_only_consistent_proven_thread():
    unknown = lark_cursor({ROOT: lark_parent(2000, thread_id=None)})
    known = lark_cursor({ROOT: lark_parent(2000)})
    merged = merge_poll_cursors("lark", [unknown, known], {ROOT})
    assert merged["parents"][ROOT]["thread_id"] == "omt_fixture0001"
    assert unknown["parents"][ROOT]["thread_id"] is None


def test_lark_seen_union_is_bounded_and_never_discards_equal_time_deduplication():
    first = lark_cursor({ROOT: lark_parent(2000, seen=["om_fixture{:04d}".format(n) for n in range(40)])})
    second = lark_cursor({ROOT: lark_parent(2000, seen=["om_fixture{:04d}".format(n) for n in range(30, 60)])})
    with pytest.raises(ValueError, match="cursor_conflict"):
        merge_poll_cursors("lark", [first, second], {ROOT})


@pytest.mark.parametrize("provider,copies", [
    ("slack", [slack_cursor({ROOT: "bad"})]),
    ("slack", [slack_cursor({ROOT: "1791389141.470039"}, after="bad")]),
    ("slack", [dict(schema_version=True, after=None, threads={})]),
    ("lark", [lark_cursor({ROOT: lark_parent(999, floor=1000)})]),
    ("lark", [lark_cursor({ROOT: lark_parent(2000, pending=pending(stamp=1999))})]),
    ("lark", [lark_cursor({ROOT: lark_parent(3000, seen=["om_pending0001"], pending=pending())})]),
    ("lark", []),
    ("onebot", []),
])
def test_invalid_cursor_cannot_become_fresh_authority(provider, copies):
    with pytest.raises(ValueError, match="^relay_authority_migration_cursor_invalid$"):
        merge_poll_cursors(provider, copies, {ROOT})


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_conflicting_unknown_cursor_metadata_is_preserved_by_refusal(provider):
    factory = slack_cursor if provider == "slack" else lark_cursor
    first, second = factory({}, private_choice="retain"), factory({}, private_choice="conflict")
    with pytest.raises(ValueError, match="cursor_conflict"):
        merge_poll_cursors(provider, [first, second], {ROOT})


def test_exact_session_filters_do_not_merge_other_sessions_parent_history():
    original = lark_cursor({ROOT: lark_parent(2000), ROOT2: lark_parent(4000)})
    session_a = merge_poll_cursors("lark", [original], {ROOT})
    session_b = merge_poll_cursors("lark", [original], {ROOT2})
    assert set(session_a["parents"]) == {ROOT}
    assert set(session_b["parents"]) == {ROOT2}


def test_ambiguous_historic_parent_is_inert_and_never_selects_an_old_hostname():
    old = ticket(1)
    new = ticket(1)
    changed = body(new)
    changed["target"]["remote_authority"] = "ssh-remote+login3"
    changed["created_at"] = new["created_at"] = "2026-10-07T16:01:00Z"
    new["value"] = json.dumps(changed)
    sources = [("login6", [old]), ("login3", [new])]
    rows, quarantines = merge_records_with_quarantine(sources)
    assert (rows, quarantines) == merge_records_with_quarantine(reversed(sources))
    assert rows[0]["active"] == 0 and rows[0]["thread_id"] == SESSION
    marker = body(rows[0])
    assert set(marker) == {"schema_version", "relay_authority_quarantine", "variant_sha256"}
    assert marker["relay_authority_quarantine"] is True and len(marker["variant_sha256"]) == 2
    assert "target" not in marker and rows[0]["created_at"] is None
    assert {body(entry)["target"]["remote_authority"] for entry in quarantines[0]["variants"]} == {
        "ssh-remote+login6", "ssh-remote+login3"}
    assert merge_records_with_quarantine([("canonical", rows), *sources]) == (rows, quarantines)
    assert merge_records([("canonical", rows)]) == rows
    with pytest.raises(ValueError, match="record_conflict"):
        merge_records(sources)  # The strict entry point still refuses ambiguity.


def test_historic_notification_payload_or_outcome_conflict_is_a_permanent_no_send_marker(tmp_path):
    from codex_watchdog.lark_mapping import LarkThreadStore
    scope, fingerprint = "d" * 64, "f" * 64
    store = LarkThreadStore(tmp_path, scope)
    namespace = "lark/" + scope + "/relay-state.json"
    old = row("notifications", fingerprint, dict(payload_sha256="a" * 64, state="uncertain"), namespace=namespace)
    new = row("notifications", fingerprint, dict(payload_sha256="b" * 64, state="sent",
              chat_id="oc_fixture000001", message_id="om_fixture000001"), namespace=namespace)
    merged, audit = merge_records_with_quarantine([("6", [old]), ("3", [new])])
    assert len(audit) == 1 and len(audit[0]["variants"]) == 2
    entry = merged[0]
    journal = store.journal
    with journal.transaction() as db:
        columns = list(entry)
        db.execute("INSERT INTO records(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                   [entry[key] for key in columns])
        assert db.execute("SELECT 1 FROM records WHERE namespace=? AND kind='notifications' AND key=?",
                          (namespace, fingerprint)).fetchone() == (1,)
    for payload in ("a" * 64, "b" * 64, "c" * 64):
        with pytest.raises(ValueError, match="historical_identity_quarantined"):
            store.prepare_notification(fingerprint, payload)


def test_quarantined_parent_retains_matching_event_session_and_pending_barrier():
    old, new = ticket(1), ticket(1)
    changed = body(new)
    changed["target"]["remote_authority"] = "ssh-remote+login3"
    new["value"] = json.dumps(changed)
    event = row("events", "event", dict(thread_key=old["key"], created_at=STAMP,
                state="delivered", delivery_status="enqueued"))
    rows, audit = merge_records_with_quarantine([("6", [old]), ("3", [new, event])])
    event_row = next(entry for entry in rows if entry["kind"] == "events")
    assert event_row["thread_id"] == SESSION and event_row["active"] == 1
    assert next(entry for entry in rows if entry["kind"] == "threads")["active"] == 0
    assert len(audit) == 1


@pytest.mark.parametrize("status,completed,expected", [
    ("enqueued", False, 1), ("consumed_or_started", False, 1), ("started", False, 1),
    ("enqueued", True, 0), ("not_delivered", False, 0),
])
def test_provider_queue_delivery_keeps_barrier_until_exact_native_completion(status, completed, expected):
    parent = ticket(1)
    event = row("events", "event", dict(thread_key=parent["key"], created_at=STAMP,
                state="delivered", delivery_status=status, native_completed=completed))
    merged = merge_records([("3", [parent, event])])
    assert next(entry for entry in merged if entry["kind"] == "events")["active"] == expected
    assert merge_records([("canonical", merged), ("old", [parent, event])]) == merged


@pytest.mark.parametrize("kind", ["session_acl", "slack_bot_grants", "slack_bot_requests", "messages", "events", "future_kind"])
def test_quarantine_entry_point_does_not_hide_permission_uuid_or_physical_claim_conflicts(kind):
    first, second = row(kind, "same", {"identity": "a"}), row(kind, "same", {"identity": "b"})
    with pytest.raises(ValueError, match="record_conflict"):
        merge_records_with_quarantine([("6", [first]), ("3", [second])])


def test_different_logical_sessions_cannot_be_retired_under_one_arbitrary_target():
    with pytest.raises(ValueError, match="record_conflict"):
        merge_records_with_quarantine([("6", [ticket(1)]), ("3", [ticket(1, session=OTHER)])])


def route_fixture(last_ts, created, *, destination="C87654321"):
    from codex_watchdog.session_routes import SessionRoutes
    routes = SessionRoutes(None, scope="C12345678")
    return row("session_routes", routes._route_key(SESSION), dict(schema_version=1, provider="slack",
        scope="C12345678", thread_id=SESSION, destination=destination,
        last_command_ts=last_ts, created_at=created))


def route_proof(stamp, *, stale=False, destination="C87654321"):
    from codex_watchdog.models import sha256_text
    command = row("route_commands", "e" * 64, dict(schema_version=1, text_hash="a" * 64,
        user_id="U12345678", channel_id="C12345678", parent_ts="1791388000.000001",
        command_ts=stamp, thread_id=SESSION, destination=destination,
        ack_claimed=True, stale=stale, created_at=STAMP))
    physical = row("route_messages", sha256_text("C12345678\0" + stamp),
                   dict(schema_version=1, command_key=command["key"], payload_sha256="f" * 64))
    return [command, physical]


def test_equal_effective_route_uses_proven_provider_stale_floor_and_retains_audit_creation_floor():
    first = route_fixture("1791389000.000001", "2026-10-07T16:00:00Z")
    second = route_fixture("1791389001.000001", "2026-10-07T16:01:00Z")
    sources = [("6", [first]), ("3", [second, *route_proof("1791389001.000001")])]
    rows, quarantines = merge_records_with_quarantine(sources)
    route = body(next(entry for entry in rows if entry["kind"] == "session_routes"))
    assert route["destination"] == "C87654321"
    assert route["last_command_ts"] == "1791389001.000001" and route["created_at"] == STAMP
    assert quarantines == []
    assert merge_records_with_quarantine(reversed(sources)) == (rows, quarantines)
    assert merge_records_with_quarantine([("canonical", rows), *sources]) == (rows, quarantines)
    with pytest.raises(ValueError, match="record_conflict"):
        merge_records(sources)


@pytest.mark.parametrize("proof", [[], route_proof("1791389000.000001"), route_proof("1791389001.000001", stale=True)])
def test_route_creation_clock_or_unproven_maximum_never_selects_effective_authority(proof):
    first = route_fixture("1791389000.000001", "2026-10-07T16:00:00Z")
    second = route_fixture("1791389001.000001", "2026-10-07T16:01:00Z")
    with pytest.raises(ValueError, match="record_conflict"):
        merge_records_with_quarantine([("6", [first]), ("3", [second, *proof])])


def test_different_route_destination_is_not_hidden_by_valid_later_receipt():
    first = route_fixture("1791389000.000001", STAMP)
    second = route_fixture("1791389001.000001", STAMP, destination="C11111111")
    with pytest.raises(ValueError, match="record_conflict"):
        merge_records_with_quarantine([("6", [first]), ("3", [second, *route_proof(
            "1791389001.000001", destination="C11111111")])])
