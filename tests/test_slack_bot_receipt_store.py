"""Synthetic durable metadata receipts without provider or native-session effects."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
import threading

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.relay import RelayTarget
from codex_watchdog.slack_bot_acl import BotSessionAccess
from codex_watchdog.slack_bot_identity import BotPrincipal
from codex_watchdog.slack_bot_receipts import BotCommandReceipts, BotReceiptContext, _KIND, _NAMESPACE
from codex_watchdog.slack_mapping import SlackThreadStore
from codex_watchdog.slack_poll import SlackPollingThreadStore


CHANNEL = "C00000001"
BOT = BotPrincipal("T00000001", "U00000001", "B00000001", "A00000001")
TARGET = RelayTarget("fixture", "11111111-1111-4111-8111-111111111111", "process_local")
REQUEST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROMPT = "fixture instruction never stored in receipt"


class Harness:
    def __init__(self, runtime, store_type=SlackThreadStore, offset=0):
        self.store = store_type(runtime)
        self.access = BotSessionAccess(self.store, CHANNEL)
        self.receipts = BotCommandReceipts(self.access)
        self.sequence = offset

    def context(self, target=TARGET, request_id=REQUEST, principal=BOT):
        self.sequence += 1
        parent = f"1760000000.{self.sequence:06d}"
        timestamp = f"1760000100.{self.sequence:06d}"
        self.store.record_thread(CHANNEL, parent, target, sha256_text(parent))
        return BotReceiptContext(self.store.thread_key(CHANNEL, parent), principal, target,
            request_id, "fixture-event:" + timestamp, CHANNEL + "\0" + timestamp,
            sha256_text(PROMPT + timestamp))

    def grant(self, target=TARGET, principal=BOT, remove=False):
        context = self.context(target)
        return self.access.control(event_key=context.event_key, message_key=context.message_key,
            source_key=context.source_key, user_id="U00000009", operation="remove" if remove else "add",
            principal=principal, expected_target=target, admin_ids=("U00000009",),
            payload_sha256=context.payload_sha256)

    def admit(self, context, delivery="enqueued"):
        admission = self.access.make_admission(context.source_key, context.principal, context.target,
            request_id=context.request_id, event_key=context.event_key, message_key=context.message_key,
            payload_sha256=context.payload_sha256)
        mapping = self.store.lookup_thread(CHANNEL, self.parent(context))
        assert mapping is not None
        assert self.store.claim_reply(event_key=context.event_key, channel_id=CHANNEL,
            thread_ts=mapping.thread_ts, instruction_id="slackbot:" + sha256_text(context.event_key)[:40],
            text=PROMPT, admission=admission, message_key=context.message_key,
            payload_sha256=context.payload_sha256) == (True, None)
        if delivery is not None:
            self.store.finish_reply(context.event_key,
                state_value="delivered" if delivery == "enqueued" else "uncertain", delivery_status=delivery)

    def parent(self, context):
        with self.store.journal.transaction() as db:
            return self.access._get(db, "threads", context.source_key)["thread_ts"]

    def close(self, context):
        with self.store.journal.transaction() as db:
            assert self.store.journal.claim(db, context.source_key)

    def row(self, kind, key, namespace=None):
        with self.store.journal.transaction() as db:
            row = db.execute("SELECT value FROM records WHERE namespace=? AND kind=? AND key=?",
                (namespace or self.store.journal.namespace, kind, key)).fetchone()
            return json.loads(row[0]) if row else None

    def write(self, kind, key, value, namespace=None):
        with self.store.journal.transaction() as db:
            db.execute("INSERT INTO records(namespace,kind,key,value) VALUES(?,?,?,?) "
                "ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value",
                (namespace or self.store.journal.namespace, kind, key, json.dumps(value)))

    def snapshot(self, *, protected=False):
        with self.store.journal.transaction() as db:
            return db.execute("SELECT namespace,kind,key,value,active FROM records "
                + ("WHERE namespace != ? " if protected else "") + "ORDER BY namespace,kind,key",
                (_NAMESPACE,) if protected else ()).fetchall()


@pytest.fixture(params=(SlackThreadStore, SlackPollingThreadStore), ids=("socket", "poll"))
def h(tmp_path, request):
    harness = Harness(tmp_path, request.param)
    harness.grant()
    return harness


def test_claim_is_durable_immutable_hash_only_and_does_not_change_admission(h):
    context = h.context()
    h.admit(context)
    before = h.snapshot(protected=True)
    claim = h.receipts.claim(context, "queued")
    assert claim.context == context
    assert (claim.channel_id, claim.thread_ts) == (CHANNEL, h.parent(context))
    with pytest.raises(FrozenInstanceError):
        claim.outcome = "uncertain"
    with pytest.raises(FrozenInstanceError):
        context.request_id = REQUEST
    row = h.row(_KIND, claim.key, _NAMESPACE)
    assert row["state"] == "claimed" and row["message_ts"] is None
    assert PROMPT not in json.dumps(row) and context.event_key not in json.dumps(row)
    assert context.message_key not in json.dumps(row)
    assert h.snapshot(protected=True) == before
    restarted = BotCommandReceipts(BotSessionAccess(type(h.store)(h.store.runtime), CHANNEL))
    assert restarted.claim(context, "queued") is None
    restarted.finish(claim, status="sent", message_ts="1760000200.000001")
    finished = h.row(_KIND, claim.key, _NAMESPACE)
    assert finished["state"] == "sent" and finished["message_ts"] == "1760000200.000001"
    restarted.finish(claim, status="uncertain")
    assert h.row(_KIND, claim.key, _NAMESPACE) == finished
    assert h.snapshot(protected=True) == before


@pytest.mark.parametrize("delivery", (None, "exception"))
def test_uncertain_send_or_crash_is_permanent_and_primary_category_is_shared(h, delivery):
    context = h.context()
    h.admit(context, delivery)
    claim = h.receipts.claim(context, "uncertain")
    assert claim is not None
    h.receipts.finish(claim, status="uncertain")
    original = h.row(_KIND, claim.key, _NAMESPACE)
    h.receipts.finish(claim, status="sent", message_ts="1760000200.000001")
    assert h.row(_KIND, claim.key, _NAMESPACE) == original
    h.store.finish_reply(context.event_key, state_value="delivered", delivery_status="enqueued")
    assert h.receipts.claim(context, "queued") is None


@pytest.mark.parametrize("delivery,outcome", (("enqueued", "uncertain"), (None, "queued"), ("exception", "queued")))
def test_outcome_cannot_overstate_or_change_admission_evidence(h, delivery, outcome):
    context = h.context()
    h.admit(context, delivery)
    before = h.snapshot()
    with pytest.raises(ValueError, match="outcome_invalid"):
        h.receipts.claim(context, outcome)
    assert h.snapshot() == before


def test_physical_and_logical_duplicates_are_bounded_across_changed_events_and_parents(h):
    original = h.context()
    h.admit(original)
    assert h.receipts.claim(original, "queued") is not None
    retry = replace(original, event_key="different-provider-event")
    assert h.receipts.claim(retry, "duplicate") is not None
    next_parent = h.context()
    before = h.snapshot()
    assert h.receipts.claim(next_parent, "duplicate") is None
    assert h.snapshot() == before


def test_duplicate_targets_current_authorized_session_without_old_surface_disclosure(h):
    original = h.context()
    h.admit(original)
    other_target = replace(TARGET, workspace_id="second-workspace",
                           thread_id="22222222-2222-4222-8222-222222222222")
    current = h.context(other_target)
    before = h.snapshot()
    assert h.receipts.claim(current, "duplicate") is None
    assert h.snapshot() == before
    h.grant(other_target)
    claim = h.receipts.claim(current, "duplicate")
    assert claim.context.target == other_target
    assert claim.thread_ts == h.parent(current) and claim.thread_ts != h.parent(original)
    row = h.row(_KIND, claim.key, _NAMESPACE)
    assert row["target"] == other_target.to_dict()
    assert row["proof"]["event_key_digest"] == sha256_text(original.event_key)


@pytest.mark.parametrize("outcome", ("queued", "uncertain", "duplicate", "rejected"))
def test_revoke_after_context_creation_denies_every_receipt_without_mutation(h, outcome):
    context = h.context()
    if outcome in ("queued", "duplicate", "uncertain"):
        h.admit(context, None if outcome == "uncertain" else "enqueued")
    else:
        h.close(context)
    h.grant(remove=True)
    assert h.access.known_user(BOT.user_id)
    before = h.snapshot()
    assert h.receipts.claim(context, outcome) is None
    assert h.snapshot() == before


def test_closed_ticket_rejection_is_distinct_from_an_admitted_duplicate(h):
    context = h.context()
    assert h.receipts.claim(context, "rejected") is None
    h.close(context)
    before = h.snapshot(protected=True)
    assert h.receipts.claim(context, "duplicate") is None
    claim = h.receipts.claim(context, "rejected")
    assert claim is not None
    row = h.row(_KIND, claim.key, _NAMESPACE)
    assert row["proof"] == dict(request_key=None, event_key_digest=None, message_key_digest=None)
    assert h.snapshot(protected=True) == before
    assert h.receipts.claim(context, "rejected") is None
    second = h.context()
    h.close(second)
    assert h.receipts.claim(second, "rejected") is None


def test_admitted_physical_event_cannot_be_reported_as_rejected(h):
    context = h.context()
    h.admit(context)
    before = h.snapshot()
    assert h.receipts.claim(context, "rejected") is None
    assert h.snapshot() == before


def test_new_message_on_closed_parent_reports_existing_uuid_as_duplicate(h):
    original = h.context()
    h.admit(original)
    retry = replace(original, event_key="new-envelope", message_key=CHANNEL + "\0" + "1760000999.000001",
                    payload_sha256=sha256_text("new physical message"))
    claim = h.receipts.claim(retry, "rejected")
    assert claim.outcome == "duplicate" and claim.thread_ts == h.parent(original)
    assert h.receipts.claim(replace(retry, event_key="another-envelope"), "rejected") is None


def test_changed_event_envelope_retains_original_physical_admission_proof(h):
    original = h.context()
    h.admit(original)
    retry = replace(original, event_key="retry-envelope")
    claim = h.receipts.claim(retry, "queued")
    assert claim is not None
    row = h.row(_KIND, claim.key, _NAMESPACE)
    assert row["event_key_digest"] == sha256_text(retry.event_key)
    assert row["proof"]["event_key_digest"] == sha256_text(original.event_key)


@pytest.mark.parametrize("field,value", (("workspace_id", "different"),
    ("remote_authority", "ssh-remote+changed"), ("remote_repo_path", "/changed"),
    ("remote_storage_key", "b" * 32),
    ("thread_id", "33333333-3333-4333-8333-333333333333")))
def test_full_source_target_is_rechecked(h, field, value):
    target = RelayTarget("fixture-remote", TARGET.thread_id, "remote_ssh",
                         "ssh-remote+fixture", "/fixture", "a" * 32)
    h.grant(target)
    context = h.context(target)
    h.admit(context)
    entry = h.row("threads", context.source_key)
    entry["target"] = replace(target, **{field: value}).to_dict()
    h.write("threads", context.source_key, entry)
    before = h.snapshot()
    assert h.receipts.claim(context, "queued") is None
    assert h.snapshot() == before


@pytest.mark.parametrize("field,value", (("payload_sha256", "f" * 64),
    ("message_key", CHANNEL + "\0" + "1760000999.000001")))
def test_primary_context_collision_fails_atomically(h, field, value):
    context = h.context()
    h.admit(context)
    before = h.snapshot()
    with pytest.raises(ValueError):
        h.receipts.claim(replace(context, **{field: value}), "queued")
    assert h.snapshot() == before


def test_event_identity_already_used_by_another_request_fails_closed(h):
    original = h.context()
    h.admit(original)
    second = h.context(request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    h.admit(second)
    before = h.snapshot()
    with pytest.raises(ValueError, match="identity_collision"):
        h.receipts.claim(replace(original, event_key=second.event_key), "queued")
    assert h.snapshot() == before


@pytest.mark.parametrize("kind,field,value", (
    ("slack_bot_requests", "principal", None), ("slack_bot_requests", "scope", None),
    ("slack_bot_requests", "source_key", "a" * 64),
    ("slack_bot_requests", "request_id", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    ("slack_bot_requests", "payload_sha256", "f" * 64),
    ("events", "state", "invalid"), ("events", "admission_schema", True),
    ("events", "instruction_id", "human-instruction"), ("events", "message_key", "a" * 64),
    ("messages", "schema_version", True), ("messages", "event_key", "a" * 64),
    ("messages", "payload_sha256", "a" * 64)))
def test_corrupt_request_chain_fails_closed_and_preserves_records(h, kind, field, value):
    context = h.context()
    h.admit(context)
    key = {"slack_bot_requests": h.access._request_key(BOT, REQUEST),
           "events": sha256_text(context.event_key), "messages": sha256_text(context.message_key)}[kind]
    entry = h.row(kind, key)
    entry[field] = value
    h.write(kind, key, entry)
    before = h.snapshot()
    with pytest.raises(ValueError):
        h.receipts.claim(context, "queued")
    assert h.snapshot() == before


@pytest.mark.parametrize("kind", ("slack_bot_grants", "slack_bot_users", "slack_bot_requests", "events", "messages", "threads"))
@pytest.mark.parametrize("value", (None, False, []))
def test_null_or_nonobject_evidence_is_never_overwritten(h, kind, value):
    context = h.context()
    h.admit(context)
    key = {"slack_bot_grants": h.access._grant_key(TARGET.thread_id),
           "slack_bot_users": h.access._user_key(BOT.user_id),
           "slack_bot_requests": h.access._request_key(BOT, REQUEST),
           "events": sha256_text(context.event_key), "messages": sha256_text(context.message_key),
           "threads": context.source_key}[kind]
    h.write(kind, key, value)
    before = h.snapshot()
    with pytest.raises(ValueError):
        h.receipts.claim(context, "queued")
    assert h.snapshot() == before


@pytest.mark.parametrize("value", (None, False, [], {"schema_version": True}))
def test_corrupt_existing_receipt_is_not_replaced(h, value):
    context = h.context()
    h.admit(context)
    claim = h.receipts.claim(context, "queued")
    h.write(_KIND, claim.key, value, _NAMESPACE)
    before = h.snapshot()
    with pytest.raises(ValueError):
        h.receipts.claim(context, "queued")
    with pytest.raises(ValueError):
        h.receipts.finish(claim, status="uncertain")
    assert h.snapshot() == before


@pytest.mark.parametrize("field,value", (("principal", None), ("request_id", None),
    ("target", None), ("source_key", "f" * 64), ("category", "duplicate"),
    ("state", "retry"), ("message_ts", "1760000200.000001"),
    ("proof", {"request_key": None, "event_key_digest": None, "message_key_digest": None})))
def test_receipt_identity_and_state_corruption_cannot_reopen_send(h, field, value):
    context = h.context()
    h.admit(context)
    claim = h.receipts.claim(context, "queued")
    entry = h.row(_KIND, claim.key, _NAMESPACE)
    entry[field] = value
    h.write(_KIND, claim.key, entry, _NAMESPACE)
    before = h.snapshot()
    with pytest.raises(ValueError):
        h.receipts.claim(context, "queued")
    with pytest.raises(ValueError):
        h.receipts.finish(claim, status="uncertain")
    assert h.snapshot() == before


def test_source_address_collision_and_human_control_receipt_are_inert(h):
    context = h.context()
    h.close(context)
    source = h.row("threads", context.source_key)
    source["channel_id"] = "C00000002"
    h.write("threads", context.source_key, source)
    before = h.snapshot()
    with pytest.raises(ValueError):
        h.receipts.claim(context, "rejected")
    assert h.snapshot() == before
    source["channel_id"] = CHANNEL
    h.write("threads", context.source_key, source)
    h.write("route_messages", sha256_text(context.message_key), None)
    before = h.snapshot()
    with pytest.raises(ValueError, match="identity_collision"):
        h.receipts.claim(context, "rejected")
    assert h.snapshot() == before


def test_unknown_admission_metadata_is_preserved(h):
    context = h.context()
    h.admit(context)
    key = h.access._request_key(BOT, REQUEST)
    entry = h.row("slack_bot_requests", key)
    entry["future_field"] = {"keep": True}
    h.write("slack_bot_requests", key, entry)
    before = h.snapshot(protected=True)
    assert h.receipts.claim(context, "queued") is not None
    assert h.snapshot(protected=True) == before


def test_exception_after_sql_insert_rolls_back_receipt_and_preserves_admission(h, monkeypatch):
    import codex_watchdog.slack_bot_receipts as receipts_module
    context = h.context()
    h.admit(context)
    before = h.snapshot()

    def interrupted_claim(*args):
        raise RuntimeError("synthetic interruption before commit")

    monkeypatch.setattr(receipts_module, "BotReceiptClaim", interrupted_claim)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        h.receipts.claim(context, "queued")
    assert h.snapshot() == before


def test_known_user_registry_requires_exact_principal_not_only_user_id(h):
    context = h.context()
    h.admit(context)
    key = h.access._user_key(BOT.user_id)
    entry = h.row("slack_bot_users", key)
    entry["principals"] = [replace(BOT, bot_id="B00000002").to_dict()]
    h.write("slack_bot_users", key, entry)
    before = h.snapshot()
    assert h.receipts.claim(context, "queued") is None
    assert h.snapshot() == before


def test_unknown_fields_survive_finish_and_invalid_finish_never_changes_intent(h):
    context = h.context()
    h.admit(context)
    claim = h.receipts.claim(context, "queued")
    entry = h.row(_KIND, claim.key, _NAMESPACE)
    entry["future_field"] = {"keep": True}
    h.write(_KIND, claim.key, entry, _NAMESPACE)
    before = h.snapshot()
    with pytest.raises(ValueError):
        h.receipts.finish(claim)
    with pytest.raises(ValueError):
        h.receipts.finish(replace(claim, thread_ts="1760000000.999999"), status="uncertain")
    assert h.snapshot() == before
    h.receipts.finish(claim, status="uncertain")
    assert h.row(_KIND, claim.key, _NAMESPACE)["future_field"] == {"keep": True}


def test_socket_poll_global_send_cap_does_not_merge_authority_or_admission(tmp_path):
    socket = Harness(tmp_path, SlackThreadStore)
    poll = Harness(tmp_path, SlackPollingThreadStore, offset=100)
    socket.grant()
    first = socket.context()
    socket.admit(first)
    current = poll.context()
    poll.close(current)
    assert poll.receipts.claim(current, "rejected") is None
    assert poll.access.principals(TARGET.thread_id) == []
    poll.grant()
    current = poll.context()
    poll.admit(current)
    barrier = threading.Barrier(2)

    def claim(harness, context):
        barrier.wait(timeout=5)
        return harness.receipts.claim(context, "queued")

    before = socket.snapshot(protected=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda args: claim(*args), ((socket, first), (poll, current))))
    assert sum(result is not None for result in results) == 1
    assert socket.snapshot(protected=True) == before
    assert socket.receipts.claim(first, "queued") is None
    assert poll.receipts.claim(current, "queued") is None


def test_distinct_principals_and_requests_have_independent_caps(h):
    first = h.context()
    h.admit(first)
    assert h.receipts.claim(first, "queued") is not None
    second_bot = BotPrincipal("T00000001", "U00000002", "B00000002", "A00000002")
    h.grant(principal=second_bot)
    second = h.context(principal=second_bot)
    h.admit(second)
    assert h.receipts.claim(second, "queued") is not None
    third = h.context(request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    h.admit(third)
    assert h.receipts.claim(third, "queued") is not None
