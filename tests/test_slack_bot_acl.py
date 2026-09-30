"""Transactional Slack bot authority, revocation, replay and confirmation fences."""
from dataclasses import replace
import json
import uuid

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.relay import RelayTarget
from codex_watchdog.reply_tickets import ReplyTickets
from codex_watchdog.session_acl import SessionACL
from codex_watchdog.session_routes import SessionRoutes
from codex_watchdog.slack_bot_acl import BotSessionAccess
from codex_watchdog.slack_bot_identity import BotPrincipal
from codex_watchdog.slack_mapping import SlackThreadStore
from codex_watchdog.slack_poll import SlackPollingThreadStore


CHANNEL = "C00000001"
DESTINATION = "C00000002"
ADMIN = "U00000009"
BOT = BotPrincipal("T00000001", "U00000001", "B00000001", "A00000001")
TARGET = RelayTarget("fixture-workspace", "11111111-1111-4111-8111-111111111111", "process_local")
REQUEST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class Harness:
    def __init__(self, store):
        self.store = store
        self.acl = BotSessionAccess(store, CHANNEL)
        self.sequence = 0

    def source(self, target=TARGET, channel=CHANNEL):
        self.sequence += 1
        stamp = f"1760000000.{self.sequence:06d}"
        self.store.record_thread(channel, stamp, target, sha256_text(stamp))
        return self.store.thread_key(channel, stamp), channel, stamp

    def control(self, operation="add", principal=BOT, target=TARGET, source=None, **overrides):
        source = source or self.source(target)
        data = dict(event_key="control-event:" + source[2],
                    message_key=source[1] + "\0" + str(float(source[2]) + 1),
                    source_key=source[0], user_id=ADMIN, operation=operation,
                    principal=None if operation == "access" else principal,
                    expected_target=target, admin_ids=(ADMIN,), payload_sha256=sha256_text(operation))
        data.update(overrides)
        return self.acl.control(**data), data

    def admission(self, source, *, target=TARGET, principal=BOT, request=REQUEST, suffix="one"):
        data = dict(event_key="reply-event:" + suffix,
                    message_key=source[1] + "\0" + "1760000900." + suffix,
                    payload_sha256=sha256_text("fixture prompt"))
        callback = self.acl.make_admission(source[0], principal, target,
                                          request_id=request, **data)
        return dict(channel_id=source[1], thread_ts=source[2], text="fixture prompt",
                    instruction_id="fixture-instruction", admission=callback, **data)


@pytest.fixture(params=(SlackThreadStore, SlackPollingThreadStore), ids=("socket", "poll"))
def h(tmp_path, request):
    return Harness(request.param(tmp_path))


def snapshot(store):
    with store.journal.transaction() as db:
        return db.execute("SELECT namespace,kind,key,value,active FROM records ORDER BY namespace,kind,key").fetchall()


def raw(store, kind, key, value):
    with store.journal.transaction() as db:
        db.execute("INSERT INTO records(namespace,kind,key,value) VALUES(?,?,?,?) "
                   "ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value",
                   (store.journal.namespace, kind, key, json.dumps(value)))


def test_grant_revoke_restart_and_registry_prevent_human_fallback(h):
    assert not h.acl.known_user(BOT.user_id)
    result, _ = h.control()
    assert result["status"] == "added"
    assert h.acl.principals(TARGET.thread_id) == [BOT]
    old_source = h.source()
    args = h.admission(old_source)
    removed, _ = h.control("remove")
    assert removed["status"] == "removed"
    before = snapshot(h.store)
    assert h.store.claim_reply(**args) == (False, "unauthorized")
    assert snapshot(h.store) == before
    restarted = BotSessionAccess(type(h.store)(h.store.runtime), CHANNEL)
    assert restarted.principals(TARGET.thread_id) == []
    assert restarted.known_user(BOT.user_id)
    assert h.store.lookup_thread(old_source[1], old_source[2]) is not None
    with pytest.raises(ValueError, match="unauthorized"):
        h.control("access", user_id=BOT.user_id, admin_ids=(BOT.user_id,))


def test_exactly_once_event_physical_and_logical_request_receipts(h):
    h.control()
    first_source = h.source()
    args = h.admission(first_source)
    assert h.store.claim_reply(**args) == (True, None)
    h.store.finish_reply(args["event_key"], state_value="uncertain", delivery_status="exception")
    before = snapshot(h.store)
    assert h.store.claim_reply(**args) == (False, "exception")
    # Provider retries can change the event envelope while retaining message identity.
    assert h.store.claim_reply(**dict(args, event_key="reply-event:retry")) == (False, "exception")
    assert snapshot(h.store) == before
    second_source = h.source()
    before = snapshot(h.store)
    with pytest.raises(ValueError, match="request_reused"):
        h.store.claim_reply(**h.admission(second_source, suffix="two"))
    assert snapshot(h.store) == before
    assert h.store.claim_reply(**h.admission(second_source, request=str(uuid.uuid4()), suffix="two")) == (True, None)


def test_request_id_cannot_move_to_another_session_or_scope(h):
    h.control()
    first = h.source()
    assert h.store.claim_reply(**h.admission(first)) == (True, None)
    other = replace(TARGET, thread_id="22222222-2222-4222-8222-222222222222")
    h.acl = BotSessionAccess(h.store, DESTINATION)
    h.control(target=other)
    second = h.source(other, DESTINATION)
    before = snapshot(h.store)
    with pytest.raises(ValueError, match="request_reused"):
        h.store.claim_reply(**h.admission(second, target=other, suffix="two"))
    assert snapshot(h.store) == before


@pytest.mark.parametrize("change", ("team_id", "user_id", "bot_id", "app_id"))
def test_every_principal_component_is_required(h, change):
    h.control()
    source = h.source()
    principal = replace(BOT, **{change: getattr(BOT, change)[:-1] + "2"})
    before = snapshot(h.store)
    assert h.store.claim_reply(**h.admission(source, principal=principal)) == (False, "unauthorized")
    assert snapshot(h.store) == before


def test_missing_grant_is_denied_even_if_principal_user_is_human_delegate(h):
    source = h.source()
    human_acl = SessionACL(h.store, "slack", CHANNEL)
    raw(h.store, "session_acl", human_acl._acl_key(TARGET.thread_id), dict(
        schema_version=1, provider="slack", scope=CHANNEL, thread_id=TARGET.thread_id,
        delegates=[BOT.user_id], created_at="fixture"))
    before = snapshot(h.store)
    assert h.store.claim_reply(**h.admission(source)) == (False, "unauthorized")
    assert snapshot(h.store) == before


@pytest.mark.parametrize("field,new", (("workspace_id", "different-workspace"),
    ("remote_authority", "ssh-remote+other-host"), ("remote_repo_path", "/different-repo"),
    ("remote_storage_key", "b" * 32),
    ("thread_id", "22222222-2222-4222-8222-222222222222")))
def test_full_current_target_is_rechecked_for_admission(h, field, new):
    target = RelayTarget("fixture-workspace", TARGET.thread_id, "remote_ssh",
                         "ssh-remote+fixture-host", "/fixture-repo", "a" * 32)
    h.control(target=target)
    source = h.source(target)
    args = h.admission(source, target=target)
    journal = h.store.journal
    with journal.transaction() as db:
        entry = journal.get(db, "threads", source[0])
        entry["target"] = replace(target, **{field: new}).to_dict()
        journal.put(db, "threads", source[0], entry)
    before = snapshot(h.store)
    assert h.store.claim_reply(**args) == (False, "unauthorized")
    assert snapshot(h.store) == before


def test_destination_source_hash_is_recomputed_in_transaction(h):
    h.control()
    source = h.source()
    args = h.admission(source)
    journal = h.store.journal
    with journal.transaction() as db:
        entry = journal.get(db, "threads", source[0])
        entry["channel_id"] = DESTINATION
        journal.put(db, "threads", source[0], entry)
    before = snapshot(h.store)
    with pytest.raises(ValueError, match="source_invalid"):
        h.store.claim_reply(**args)
    assert snapshot(h.store) == before


@pytest.mark.parametrize("kind,value", (
    ("slack_bot_grants", None), ("slack_bot_grants", []),
    ("slack_bot_grants", {"schema_version": True}), ("slack_bot_users", None),
    ("slack_bot_users", {"schema_version": 2}), ("slack_bot_requests", None),
    ("slack_bot_requests", {"schema_version": 8})))
def test_malformed_or_null_records_preserve_ticket_and_bytes(h, kind, value):
    h.control()
    source = h.source()
    keys = {"slack_bot_grants": h.acl._grant_key(TARGET.thread_id),
            "slack_bot_users": h.acl._user_key(BOT.user_id),
            "slack_bot_requests": h.acl._request_key(BOT, REQUEST)}
    raw(h.store, kind, keys[kind], value)
    before = snapshot(h.store)
    with pytest.raises(ValueError):
        h.store.claim_reply(**h.admission(source))
    assert snapshot(h.store) == before


def test_controls_share_receipts_and_do_not_repeat_effects(h):
    result, data = h.control()
    before = snapshot(h.store)
    assert h.acl.control(**data)["status"] == "duplicate"
    replay = h.acl.control(**dict(data, event_key="changed-provider-envelope"))
    assert replay["status"] == "duplicate"
    assert replay["operation_key"] == result["operation_key"]
    assert snapshot(h.store) == before
    human = SessionACL(h.store, "slack", CHANNEL)
    with pytest.raises(ValueError, match="identity_collision"):
        human.apply(**{key: value for key, value in data.items() if key != "principal"},
                    delegate_id="U00000002")
    assert snapshot(h.store) == before


def test_control_statuses_and_verified_remove_keep_bot_knowledge(h):
    result, _ = h.control("remove")
    assert result["status"] == "not_present"
    assert h.acl.known_user(BOT.user_id)
    assert h.control()[0]["status"] == "added"
    assert h.control()[0]["status"] == "already_present"
    result, _ = h.control("access")
    assert result["status"] == "access"
    assert result["principals"] == [BOT]


@pytest.mark.parametrize("change", ("source", "text", "principal"))
def test_physical_and_event_collision_never_change_authority(h, change):
    _, data = h.control()
    if change == "source":
        data["source_key"] = h.source()[0]
    elif change == "text":
        data["payload_sha256"] = sha256_text("changed fixture input")
    else:
        data["principal"] = replace(BOT, app_id="A00000002")
    before = snapshot(h.store)
    with pytest.raises(ValueError, match="collision"):
        h.acl.control(**data)
    assert snapshot(h.store) == before


@pytest.mark.parametrize("value", (None, [], {"schema_version": True}))
def test_malformed_control_confirmation_is_inert(h, value):
    result, _ = h.control()
    key = result["operation_key"]
    raw(h.store, "slack_bot_controls", key, value)
    before = snapshot(h.store)
    with pytest.raises(ValueError):
        h.acl.claim_confirmation(key)
    assert snapshot(h.store) == before


def test_mismatched_control_target_preserves_active_source(h):
    source = h.source()
    before = snapshot(h.store)
    result, _ = h.control(source=source, target=replace(TARGET, workspace_id="elsewhere"))
    assert result["status"] == "unauthorized"
    assert snapshot(h.store) == before


@pytest.mark.parametrize("mutation", ("duplicate", "malformed_principal", "wrong_scope", "wrong_session"))
def test_noncanonical_grant_records_fail_closed(h, mutation):
    h.control()
    source = h.source()
    journal = h.store.journal
    with journal.transaction() as db:
        key = h.acl._grant_key(TARGET.thread_id)
        entry = journal.get(db, "slack_bot_grants", key)
        if mutation == "duplicate":
            entry["principals"].append(BOT.to_dict())
        elif mutation == "malformed_principal":
            entry["principals"][0]["app_id"] = None
        elif mutation == "wrong_scope":
            entry["scope"] = DESTINATION
        else:
            entry["thread_id"] = "22222222-2222-4222-8222-222222222222"
        journal.put(db, "slack_bot_grants", key, entry)
    before = snapshot(h.store)
    with pytest.raises(ValueError):
        h.store.claim_reply(**h.admission(source))
    assert snapshot(h.store) == before


@pytest.mark.parametrize("kind", ("acl_commands", "acl_messages", "route_commands", "route_messages"))
def test_existing_human_and_route_receipts_block_bot_claim(h, kind):
    h.control()
    source = h.source()
    args = h.admission(source)
    acl = SessionACL(h.store, "slack", CHANNEL)
    routes = SessionRoutes(h.store, "slack", CHANNEL)
    keys = {"acl_commands": acl._command_key(args["event_key"]),
            "acl_messages": acl._msg_dedup_key(args["message_key"]),
            "route_commands": routes._command_key(args["event_key"]),
            "route_messages": sha256_text(args["message_key"])}
    raw(h.store, kind, keys[kind], None)
    before = snapshot(h.store)
    with pytest.raises(ValueError, match="identity_collision"):
        h.store.claim_reply(**args)
    assert snapshot(h.store) == before


@pytest.mark.parametrize("operation", ("control", "request"))
def test_ticket_claim_and_authorization_records_rollback_together(h, monkeypatch, operation):
    if operation == "request":
        h.control()
    source = h.source()
    before = snapshot(h.store)
    put = ReplyTickets.put

    def failing_put(self, db, kind, key, value):
        if kind == "events":
            raise RuntimeError("synthetic receipt failure")
        return put(self, db, kind, key, value)

    monkeypatch.setattr(ReplyTickets, "put", failing_put)
    with pytest.raises(RuntimeError, match="synthetic"):
        if operation == "control":
            h.control(source=source)
        else:
            h.store.claim_reply(**h.admission(source))
    assert snapshot(h.store) == before


@pytest.mark.parametrize("finish", (None, "sent", "uncertain"))
def test_confirmation_claim_is_durable_and_never_replays(h, finish):
    result, _ = h.control()
    key = result["operation_key"]
    receipt = h.acl.claim_confirmation(key)
    assert receipt["target"] == TARGET
    assert receipt["principals"] == [BOT]
    assert receipt["address"].startswith(CHANNEL + "\0")
    assert len(receipt["fingerprint"]) == 64
    if finish:
        h.acl.finish_confirmation(key, finish)
    restarted = BotSessionAccess(type(h.store)(h.store.runtime), CHANNEL)
    before = snapshot(h.store)
    assert restarted.claim_confirmation(key) is None
    assert snapshot(h.store) == before


def test_confirmation_target_mutation_fails_closed_and_keeps_unknown_fields(h):
    result, data = h.control()
    journal = h.store.journal
    key = result["operation_key"]
    with journal.transaction() as db:
        receipt = journal.get(db, "slack_bot_controls", key)
        receipt["future_compatible"] = {"flag": True}
        journal.put(db, "slack_bot_controls", key, receipt)
        grant = journal.get(db, "slack_bot_grants", h.acl._grant_key(TARGET.thread_id))
        grant["future_compatible"] = {"flag": True}
        journal.put(db, "slack_bot_grants", h.acl._grant_key(TARGET.thread_id), grant)
        source = journal.get(db, "threads", data["source_key"])
        source["target"] = replace(TARGET, workspace_id="changed").to_dict()
        journal.put(db, "threads", data["source_key"], source)
    before = snapshot(h.store)
    assert h.acl.claim_confirmation(key) is None
    assert snapshot(h.store) == before
    h.control("remove")
    with journal.transaction() as db:
        grant = journal.get(db, "slack_bot_grants", h.acl._grant_key(TARGET.thread_id))
        assert grant["future_compatible"] == {"flag": True}
        source["target"] = TARGET.to_dict()
        journal.put(db, "threads", data["source_key"], source)
    assert h.acl.claim_confirmation(key)["principals"] == []
    h.acl.finish_confirmation(key, "uncertain")
    with journal.transaction() as db:
        receipt = journal.get(db, "slack_bot_controls", key)
        assert receipt["future_compatible"] == {"flag": True}


def test_session_scope_provider_and_transport_isolation(tmp_path):
    h = Harness(SlackThreadStore(tmp_path))
    h.control()
    assert BotSessionAccess(h.store, DESTINATION).principals(TARGET.thread_id) == []
    other_session = replace(TARGET, thread_id="22222222-2222-4222-8222-222222222222")
    assert h.acl.principals(other_session.thread_id) == []
    poll = Harness(SlackPollingThreadStore(tmp_path))
    assert poll.acl.principals(TARGET.thread_id) == []
    assert not poll.acl.known_user(BOT.user_id)
    poll.source()
    source = poll.source()
    before = snapshot(poll.store)
    assert poll.store.claim_reply(**poll.admission(source)) == (False, "unauthorized")
    assert snapshot(poll.store) == before
    class OtherProvider:
        provider = "onebot"
    with pytest.raises(ValueError, match="provider_invalid"):
        BotSessionAccess(OtherProvider(), CHANNEL)


def test_closed_control_cannot_grant_or_register_bot(h):
    source = h.source()
    with h.store.journal.transaction() as db:
        assert h.store.journal.claim(db, source[0])
    before = snapshot(h.store)
    result, _ = h.control(source=source)
    assert result["status"] == "closed"
    assert not h.acl.known_user(BOT.user_id)
    assert snapshot(h.store) == before


@pytest.mark.parametrize("request_id", ("not-a-uuid", REQUEST.upper(), None, REQUEST.replace("-", "")))
def test_request_ids_must_be_canonical(h, request_id):
    with pytest.raises(ValueError):
        h.admission(h.source(), request=request_id)
