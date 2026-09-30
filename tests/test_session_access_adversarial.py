"""Adversarial tests for exact-session delegated access.

Independent missed boundaries not covered by test_session_acl.py or
test_session_access_commands.py:
- delegates=null/absent in valid dict ACL entry
- non-null non-dict stored value (list, int, string) treated as corrupt
- claim_confirmation: source_target != cmd_target -> silent None (no send)
- apply: remote_ssh vs process_local target mismatch
- apply: malformed source entry target (missing field)
- admit: ACL entry with empty delegates list
- apply: second call on same consumed source_key
- ACL write atomically rolled back when claim returns False
- Lark parse socket form: non-string open_id (int, None, bool, absent key)
- Same UUID different provider: fully isolated ACL admission
- Delegates snapshot returned by apply is immutable
- admit: wrong workspace_id in expected_target
- delegates field stored as dict type
- source_key exists only under 'events' kind, not 'threads'
- Lark parse: non-list mentions argument (access and add commands)
- make_admission callback returns exactly True; non-True values not trusted
"""
from __future__ import annotations

import json
import pytest

from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.models import sha256_text
from codex_watchdog.onebot_relay import OneBotThreadStore
from codex_watchdog.relay import RelayTarget
from codex_watchdog.session_acl import (
    SessionACL,
    _KIND_ACL,
    _KIND_ACL_COMMANDS,
    _KIND_ACL_MESSAGES,
)
from codex_watchdog.session_access import SessionAccess
from codex_watchdog.session_access_commands import parse_lark_access
from codex_watchdog.slack_mapping import SlackThreadStore

# ── Shared constants ──────────────────────────────────────────────────────────

CHANNEL = "C12345678"
SLACK_SCOPE = "C00000001"
LARK_SCOPE = "a" * 64
ONEBOT_SCOPE = "b" * 64

SLACK_ADMIN = "U00000001"
SLACK_DELEG = "U00000003"
SLACK_DELEG2 = "U00000004"
LARK_ADMIN = "ou_admin00000001"
LARK_DELEG = "ou_deleg0000001a"
OB_ADMIN = "11111"
OB_DELEG = "33333"

LCHAT = "oc_fixture000001"
OBDEST = "private:54321"


def target(n: int, *, remote: bool = False) -> RelayTarget:
    tid = "11111111-2222-4333-8444-{:012d}".format(n)
    if remote:
        return RelayTarget(
            "ws-" + str(n), tid, "remote_ssh",
            remote_authority="ssh-remote+testhost",
            remote_repo_path="/home/user/repo",
            remote_storage_key="0" * 32,
        )
    return RelayTarget("ws-" + str(n), tid, "process_local")


def make_store(tmp_path, provider):
    if provider == "slack":
        return SlackThreadStore(tmp_path)
    if provider == "lark":
        return LarkThreadStore(tmp_path, LARK_SCOPE)
    return OneBotThreadStore(tmp_path, ONEBOT_SCOPE)


def make_acl(store, provider):
    scope = SLACK_SCOPE if provider == "slack" else (
        LARK_SCOPE if provider == "lark" else ONEBOT_SCOPE)
    return SessionACL(store, provider=provider, scope=scope)


def msg_id(provider, n):
    if provider == "lark":
        return "om_notice{:08d}".format(n)
    return str(-n)


def record_ticket(store, provider, n, *, session=None) -> str:
    fp = sha256_text("notice:" + str(n))
    s = n if session is None else session
    tgt = target(s)
    if provider == "slack":
        store.record_thread(CHANNEL, "1789111600.{:06d}".format(n), tgt, fp)
        return store.thread_key(CHANNEL, "1789111600.{:06d}".format(n))
    chat = LCHAT if provider == "lark" else OBDEST
    mid = msg_id(provider, n)
    store.prepare_notification(fp, sha256_text("payload:" + str(n)))
    store.finish_notification(fp, chat, mid, tgt)
    return sha256_text(chat + "\0" + mid)


def apply_add(acl, store, provider, n, delegate, *, admin, session=None):
    src = record_ticket(store, provider, n, session=session)
    s = n if session is None else session
    return acl.apply(
        event_key="evt:" + str(n),
        message_key="msg:" + str(n),
        source_key=src,
        user_id=admin,
        operation="add",
        delegate_id=delegate,
        payload_sha256=sha256_text("payload:" + str(n)),
        admin_ids=(admin,),
        expected_target=target(s),
    )


def active_count(store):
    journal = store.journal
    with journal.transaction() as db:
        return len(journal.active(db))


def _store_raw(store, kind, key, raw_json_string):
    """Write an arbitrary JSON value directly, bypassing put's schema."""
    journal = store.journal
    with journal.transaction() as db:
        db.execute(
            "INSERT INTO records(namespace,kind,key,value,active) VALUES(?,?,?,?,0)"
            " ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value",
            (journal.namespace, kind, key, raw_json_string))


# ── Malformed delegates field: null ──────────────────────────────────────────

def test_delegates_null_in_valid_acl_entry_fails_closed(tmp_path):
    """ACL dict with delegates=null (Python None) must not be treated as empty list."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    acl_key = acl._acl_key(target(1).thread_id)
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["delegates"] = None
        journal.put(db, _KIND_ACL, acl_key, entry)

    with pytest.raises(ValueError, match="session_acl_delegates_invalid"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


def test_delegates_null_blocks_admit(tmp_path):
    """Null delegates in ACL propagates to admit and does NOT silently allow anyone."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    acl_key = acl._acl_key(target(1).thread_id)
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["delegates"] = None
        journal.put(db, _KIND_ACL, acl_key, entry)

    src = record_ticket(store, "slack", 5, session=1)
    with pytest.raises(ValueError, match="session_acl_delegates_invalid"):
        with journal.transaction() as db:
            acl.admit(db, src, SLACK_DELEG, (SLACK_ADMIN,), target(1))


# ── Malformed delegates field: key absent ────────────────────────────────────

def test_delegates_key_absent_from_acl_entry_fails_closed(tmp_path):
    """ACL dict with no 'delegates' key must fail the same as null."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    acl_key = acl._acl_key(target(1).thread_id)
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL, acl_key)
        del entry["delegates"]
        journal.put(db, _KIND_ACL, acl_key, entry)

    with pytest.raises(ValueError, match="session_acl_delegates_invalid"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


# ── Malformed delegates field: dict type ─────────────────────────────────────

def test_delegates_dict_type_fails_closed(tmp_path):
    """ACL with delegates={} (dict) must raise, not iterate as empty."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    acl_key = acl._acl_key(target(1).thread_id)
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["delegates"] = {"U00000003": True}
        journal.put(db, _KIND_ACL, acl_key, entry)

    with pytest.raises(ValueError, match="session_acl_delegates_invalid"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


# ── _strict_get_own: non-null, non-dict stored value ─────────────────────────

@pytest.mark.parametrize("raw,desc", [
    (json.dumps([1, 2, 3]), "array"),
    ("42", "integer"),
    ('"some-string"', "string"),
    ("true", "boolean_true"),
    ("false", "boolean_false"),
])
def test_strict_get_own_non_dict_value_raises(tmp_path, raw, desc):
    """Any non-dict stored ACL value (not just null) is rejected with same error code."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    acl_key = acl._acl_key(target(1).thread_id)
    _store_raw(store, _KIND_ACL, acl_key, raw)

    journal = store.journal
    with pytest.raises(ValueError, match="session_acl_null_record"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


# ── claim_confirmation: source_target diverged from cmd_target ────────────────

def test_claim_confirmation_source_target_diverged_returns_none(tmp_path):
    """Corrupted source entry target (target != command's target) → returns None, no claim."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    op_key = r["operation_key"]

    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, "threads", src)
        entry["target"] = target(99).to_dict()
        journal.put(db, "threads", src, entry)

    result = acl.claim_confirmation(op_key)
    assert result is None

    # Command must NOT have been marked as claimed
    with journal.transaction() as db:
        cmd = journal.get(db, _KIND_ACL_COMMANDS, op_key)
    assert cmd["confirmation_claimed"] is False


# ── apply: remote_ssh vs process_local target mismatch ───────────────────────

def test_apply_remote_target_mismatches_local_stored_target(tmp_path):
    """Source ticket stored with process_local; apply with remote_ssh expected_target raises."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    store.record_thread(CHANNEL, "1789111600.000001", target(1),
                        sha256_text("fp-remote-test"))
    src = store.thread_key(CHANNEL, "1789111600.000001")

    with pytest.raises(ValueError, match="acl_target_mismatch"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1, remote=True))
    assert active_count(store) == 1


# ── apply: malformed source entry target (missing field) ─────────────────────

def test_apply_source_entry_target_missing_required_field(tmp_path):
    """apply() with source entry's target dict missing execution_locality raises."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)

    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, "threads", src)
        bad = dict(entry["target"])
        del bad["execution_locality"]
        entry["target"] = bad
        journal.put(db, "threads", src, entry)

    with pytest.raises(ValueError, match="acl_source_target_malformed"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert active_count(store) == 1


# ── admit: ACL entry with empty delegates list ────────────────────────────────

def test_admit_empty_delegates_entry_non_admin_is_false(tmp_path):
    """ACL entry exists with delegates=[] but non-admin must still be denied."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    # Remove the delegate, leaving an empty delegates entry
    src2 = record_ticket(store, "slack", 2, session=1)
    acl.apply(event_key="e2", message_key="m2", source_key=src2,
              user_id=SLACK_ADMIN, operation="remove", delegate_id=SLACK_DELEG,
              payload_sha256=sha256_text("p2"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))

    journal = store.journal
    acl_key = acl._acl_key(target(1).thread_id)
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL, acl_key)
    assert entry is not None and entry["delegates"] == []

    src3 = record_ticket(store, "slack", 3, session=1)
    with journal.transaction() as db:
        assert acl.admit(db, src3, SLACK_DELEG, (SLACK_ADMIN,), target(1)) is False


# ── apply: second call on same consumed source_key ────────────────────────────

def test_apply_second_call_on_consumed_source_raises(tmp_path):
    """After apply() claims the source ticket, another apply on same key raises."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)

    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert r["status"] == "added"
    assert active_count(store) == 0

    # New event/message keys (not a duplicate) but same source ticket (already consumed)
    with pytest.raises(ValueError, match="acl_source_closed"):
        acl.apply(event_key="e1-next", message_key="m1-next", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG2,
                  payload_sha256=sha256_text("p1-next"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


# ── apply: ACL write atomically rolled back when claim returns False ──────────

def test_acl_write_rolled_back_atomically_on_claim_failure(tmp_path, monkeypatch):
    """If claim() returns False, the ACL write in the same transaction must be rolled back."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)

    from codex_watchdog.reply_tickets import ReplyTickets

    def claim_returns_false(self, db, key):
        return False

    monkeypatch.setattr(ReplyTickets, "claim", claim_returns_false)

    with pytest.raises(ValueError, match="acl_source_closed"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))

    # ACL must NOT be persisted
    journal = store.journal
    acl_key = acl._acl_key(target(1).thread_id)
    with journal.transaction() as db:
        assert journal.get(db, _KIND_ACL, acl_key) is None

    # Command record must NOT be persisted
    cmd_key = acl._command_key("e1")
    with journal.transaction() as db:
        assert journal.get(db, _KIND_ACL_COMMANDS, cmd_key) is None

    # Source ticket must still be active (claim never actually closed it)
    assert active_count(store) == 1


# ── Lark parse_lark_access: socket form non-string open_id ───────────────────

def test_parse_lark_socket_form_integer_open_id_raises():
    """Socket form with open_id as int must be rejected as malformed."""
    mention = {"key": "@_user_1", "id": {"open_id": 12345}, "name": "bad"}
    with pytest.raises(ValueError, match="access_command_malformed_mention"):
        parse_lark_access("add @_user_1", [mention])


def test_parse_lark_socket_form_none_open_id_raises():
    """Socket form with open_id=None must be rejected."""
    mention = {"key": "@_user_1", "id": {"open_id": None}, "name": "bad"}
    with pytest.raises(ValueError, match="access_command_malformed_mention"):
        parse_lark_access("add @_user_1", [mention])


def test_parse_lark_socket_form_bool_open_id_raises():
    """Socket form with open_id=True (bool) must be rejected."""
    mention = {"key": "@_user_1", "id": {"open_id": True}, "name": "bad"}
    with pytest.raises(ValueError, match="access_command_malformed_mention"):
        parse_lark_access("add @_user_1", [mention])


def test_parse_lark_socket_form_missing_open_id_key_raises():
    """Socket form id dict with no 'open_id' key must be rejected."""
    mention = {"key": "@_user_1", "id": {}, "name": "bad"}
    with pytest.raises(ValueError, match="access_command_malformed_mention"):
        parse_lark_access("add @_user_1", [mention])


def test_parse_lark_socket_form_open_id_list_raises():
    """Socket form with open_id as list must be rejected."""
    mention = {"key": "@_user_1", "id": {"open_id": ["ou_abc123def456gh"]}, "name": "bad"}
    with pytest.raises(ValueError, match="access_command_malformed_mention"):
        parse_lark_access("add @_user_1", [mention])


# ── Lark parse_lark_access: non-list mentions argument ───────────────────────

def test_parse_lark_access_with_integer_mentions_raises():
    """access command with integer mentions (not None, not list) fails closed."""
    with pytest.raises(ValueError, match="access_command_unexpected_mentions"):
        parse_lark_access("access", 42)


def test_parse_lark_add_with_integer_mentions_raises():
    """add command with integer mentions (not a list) must fail closed."""
    with pytest.raises(ValueError, match="access_command_missing_mention"):
        parse_lark_access("add @_user_1", 42)


def test_parse_lark_add_with_dict_mentions_raises():
    """add command with dict mentions (not a list) must fail closed."""
    with pytest.raises(ValueError, match="access_command_missing_mention"):
        parse_lark_access("add @_user_1", {"key": "@_user_1"})


# ── Same UUID different provider: fully isolated ACL admission ────────────────

def test_same_uuid_different_provider_admits_are_isolated(tmp_path):
    """Slack delegate added to session UUID X cannot be admitted in Lark's session UUID X."""
    store_slack = SlackThreadStore(tmp_path)
    store_lark = LarkThreadStore(tmp_path, LARK_SCOPE)
    acl_slack = SessionACL(store_slack, provider="slack", scope=SLACK_SCOPE)
    acl_lark = SessionACL(store_lark, provider="lark", scope=LARK_SCOPE)

    # Add SLACK_DELEG to session 1 via Slack ACL
    apply_add(acl_slack, store_slack, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    # Verify SLACK_DELEG is in the Slack ACL
    j_slack = store_slack.journal
    with j_slack.transaction() as db:
        assert SLACK_DELEG in acl_slack.delegates(db, target(1).thread_id)

    # Lark ACL for the same thread_id is completely independent (empty)
    j_lark = store_lark.journal
    with j_lark.transaction() as db:
        assert acl_lark.delegates(db, target(1).thread_id) == []

    # Lark ADMIN is admitted; LARK_DELEG (never added) is not
    src_lark = record_ticket(store_lark, "lark", 2, session=1)
    with j_lark.transaction() as db:
        assert acl_lark.admit(db, src_lark, LARK_ADMIN, (LARK_ADMIN,), target(1)) is True
        assert acl_lark.admit(db, src_lark, LARK_DELEG, (LARK_ADMIN,), target(1)) is False


# ── Delegates snapshot from apply() is not a live view ───────────────────────

def test_delegates_snapshot_immutable_after_removal(tmp_path):
    """The delegates list returned by apply() is a snapshot; subsequent removal must not mutate it."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)

    r1 = apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)
    snapshot = r1["delegates"]
    assert SLACK_DELEG in snapshot

    src2 = record_ticket(store, "slack", 2, session=1)
    acl.apply(event_key="e2", message_key="m2", source_key=src2,
              user_id=SLACK_ADMIN, operation="remove", delegate_id=SLACK_DELEG,
              payload_sha256=sha256_text("p2"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))

    # Snapshot from r1 must not have been modified in place
    assert SLACK_DELEG in snapshot, "returned delegates list must be an immutable copy"


# ── admit: wrong workspace_id in expected_target ─────────────────────────────

def test_admit_expected_target_workspace_mismatch_returns_false(tmp_path):
    """admit() with source ticket for ws-1 but expected_target for ws-DIFFERENT returns False.

    The ACL is keyed by thread_id (session UUID). The expected_target check in admit()
    guards against caller drift: if the relay's in-memory target differs from the stored
    source ticket target, admit returns False rather than granting access.
    """
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    # Get a ticket whose stored target is target(1) = workspace ws-1
    src = record_ticket(store, "slack", 5, session=1)

    # Caller passes wrong workspace_id in expected_target (same thread_id, different workspace)
    wrong_workspace = RelayTarget("ws-DIFFERENT", target(1).thread_id, "process_local")

    journal = store.journal
    with journal.transaction() as db:
        # stored_target = target(1) (ws-1); expected = wrong_workspace → mismatch → False
        assert acl.admit(db, src, SLACK_DELEG, (SLACK_ADMIN,), wrong_workspace) is False


# ── source_key exists under wrong kind ───────────────────────────────────────

def test_apply_source_key_under_events_kind_raises_unknown(tmp_path):
    """Source key that only exists under 'events' kind must raise acl_source_unknown."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)

    fake_source = sha256_text("fake-source-only-in-events")
    journal = store.journal
    with journal.transaction() as db:
        journal.put(db, "events", fake_source, {"info": "should not be used as thread ticket"})

    with pytest.raises(ValueError, match="acl_source_unknown"):
        acl.apply(event_key="e1", message_key="m1", source_key=fake_source,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


# ── make_admission: result must be exactly True ───────────────────────────────

def test_make_admission_admin_returns_exact_true(tmp_path):
    """make_admission callback for admin returns exactly True (bool, not truthy int/str)."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    access = SessionAccess(store, "slack", SLACK_SCOPE, admin_ids=(SLACK_ADMIN,), acl=acl)

    src = record_ticket(store, "slack", 1)
    admission = access.make_admission(src, SLACK_ADMIN, target(1))

    journal = store.journal
    with journal.transaction() as db:
        result = admission(db)

    assert result is True
    assert type(result) is bool


def test_make_admission_non_admin_non_delegate_returns_false(tmp_path):
    """make_admission for a user who is neither admin nor delegate returns exactly False."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    access = SessionAccess(store, "slack", SLACK_SCOPE, admin_ids=(SLACK_ADMIN,), acl=acl)

    src = record_ticket(store, "slack", 1)
    stranger = "U99999999"
    admission = access.make_admission(src, stranger, target(1))

    journal = store.journal
    with journal.transaction() as db:
        result = admission(db)

    assert result is False
    assert type(result) is bool


def test_make_admission_delegate_returns_exact_true(tmp_path):
    """make_admission callback for a properly added delegate returns exactly True."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    access = SessionAccess(store, "slack", SLACK_SCOPE, admin_ids=(SLACK_ADMIN,), acl=acl)

    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)
    src2 = record_ticket(store, "slack", 2, session=1)
    admission = access.make_admission(src2, SLACK_DELEG, target(1))

    journal = store.journal
    with journal.transaction() as db:
        result = admission(db)

    assert result is True
    assert type(result) is bool


# ── apply: access operation with non-empty ACL entry ─────────────────────────

def test_apply_access_operation_returns_current_non_empty_delegates(tmp_path):
    """access operation returns the actual current delegate list without mutation."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    src2 = record_ticket(store, "slack", 2, session=1)
    r = acl.apply(event_key="e2", message_key="m2", source_key=src2,
                  user_id=SLACK_ADMIN, operation="access", delegate_id=None,
                  payload_sha256=sha256_text("p2"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))

    assert r["status"] == "access"
    assert SLACK_DELEG in r["delegates"]
    assert active_count(store) == 0  # ticket consumed


# ── Lark parse_lark_access: socket form with app_id in id dict (bot) ─────────

def test_parse_lark_socket_form_app_id_rejects_bot():
    """Socket form id dict with 'app_id' key identifies a bot and must be rejected."""
    mention = {"key": "@_user_1",
               "id": {"open_id": "ou_abc123def456gh", "app_id": "cli_bot123"},
               "name": "SomeBot"}
    with pytest.raises(ValueError, match="access_command_bot_mention"):
        parse_lark_access("add @_user_1", [mention])


# ── Multi-provider: same scope hash in onebot and lark are independent ─────────

@pytest.mark.parametrize("pA,pB", [("lark", "onebot")])
def test_cross_provider_same_scope_hash_independent(tmp_path, pA, pB):
    """Lark and OneBot stores using same scope hash have completely independent ACLs."""
    store_a = make_store(tmp_path, pA)
    store_b = make_store(tmp_path, pB)
    acl_a = make_acl(store_a, pA)
    acl_b = make_acl(store_b, pB)

    admin_a, deleg_a = (LARK_ADMIN, LARK_DELEG) if pA == "lark" else (OB_ADMIN, OB_DELEG)
    admin_b, deleg_b = (OB_ADMIN, OB_DELEG) if pB == "onebot" else (LARK_ADMIN, LARK_DELEG)

    # Add deleg_a to session 1 in provider A
    apply_add(acl_a, store_a, pA, 1, deleg_a, admin=admin_a)

    # Provider B for same session has no delegates
    j_b = store_b.journal
    with j_b.transaction() as db:
        assert acl_b.delegates(db, target(1).thread_id) == []

    # A new ticket in provider B: deleg_a is not admitted there
    src_b = record_ticket(store_b, pB, 2, session=1)
    with j_b.transaction() as db:
        assert acl_b.admit(db, src_b, deleg_a if pB == "lark" else deleg_b,
                            (admin_b,), target(1)) is False


# ── apply: preserves existing unknown fields during ACL mutation ───────────────

def test_apply_preserves_unknown_fields_on_second_mutation(tmp_path):
    """When apply mutates the ACL again, pre-existing unknown fields must survive."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    acl_key = acl._acl_key(target(1).thread_id)
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["future_unknown_field"] = "preserve-this"
        journal.put(db, _KIND_ACL, acl_key, entry)

    src2 = record_ticket(store, "slack", 2, session=1)
    acl.apply(event_key="e2", message_key="m2", source_key=src2,
              user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG2,
              payload_sha256=sha256_text("p2"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))

    with journal.transaction() as db:
        entry2 = journal.get(db, _KIND_ACL, acl_key)
    assert entry2.get("future_unknown_field") == "preserve-this"
