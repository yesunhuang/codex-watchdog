"""Focused tests for SessionACL bounded exact-session delegation storage."""
import json
import pytest

from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.models import sha256_text, utc_now
from codex_watchdog.onebot_relay import OneBotThreadStore
from codex_watchdog.relay import RelayTarget
from codex_watchdog.session_acl import SessionACL
from codex_watchdog.slack_mapping import SlackThreadStore

# ── Provider-specific constants ───────────────────────────────────────────────

CHANNEL = "C12345678"
SLACK_SCOPE = "C00000001"

LARK_SCOPE = "a" * 64
ONEBOT_SCOPE = "b" * 64

SLACK_ADMIN = "U00000001"
SLACK_USER  = "U00000002"
SLACK_DELEG = "U00000003"
SLACK_DELEG2 = "U00000004"

LARK_ADMIN  = "ou_admin00000001"
LARK_USER   = "ou_user000000001"
LARK_DELEG  = "ou_deleg0000001a"
LARK_DELEG2 = "ou_deleg0000002b"

OB_ADMIN  = "11111"
OB_USER   = "22222"
OB_DELEG  = "33333"
OB_DELEG2 = "44444"

LCHAT  = "oc_fixture000001"
OBDEST = "private:54321"


def target(n: int) -> RelayTarget:
    return RelayTarget("ws-" + str(n), "11111111-2222-4333-8444-{:012d}".format(n),
                       "process_local")


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


def ids(provider):
    if provider == "slack":
        return SLACK_ADMIN, SLACK_USER, SLACK_DELEG, SLACK_DELEG2
    if provider == "lark":
        return LARK_ADMIN, LARK_USER, LARK_DELEG, LARK_DELEG2
    return OB_ADMIN, OB_USER, OB_DELEG, OB_DELEG2


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
    src_key = record_ticket(store, provider, n, session=session)
    s = n if session is None else session
    return acl.apply(
        event_key="evt:" + str(n),
        message_key="msg:" + str(n),
        source_key=src_key,
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


# ── Constructor validation ─────────────────────────────────────────────────────

def test_invalid_provider_raises():
    store = SlackThreadStore.__new__(SlackThreadStore)
    with pytest.raises(ValueError, match="session_acl_provider_invalid"):
        SessionACL(store, provider="SLACK", scope=SLACK_SCOPE)


def test_invalid_scope_raises():
    store = SlackThreadStore.__new__(SlackThreadStore)
    with pytest.raises(ValueError, match="session_acl_scope_invalid"):
        SessionACL(store, provider="slack", scope="not-a-channel")


# ── Three provider baseline ────────────────────────────────────────────────────

@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_admin_add_remove_access_baseline(tmp_path, provider):
    store = make_store(tmp_path, provider)
    acl = make_acl(store, provider)
    admin, _, deleg, _ = ids(provider)

    src1 = record_ticket(store, provider, 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src1,
                  user_id=admin, operation="add", delegate_id=deleg,
                  payload_sha256=sha256_text("p1"), admin_ids=(admin,),
                  expected_target=target(1))
    assert r["status"] == "added"
    assert deleg in r["delegates"]
    assert r["target"] == target(1)
    assert active_count(store) == 0  # ticket consumed

    src2 = record_ticket(store, provider, 2, session=1)
    r2 = acl.apply(event_key="e2", message_key="m2", source_key=src2,
                   user_id=admin, operation="remove", delegate_id=deleg,
                   payload_sha256=sha256_text("p2"), admin_ids=(admin,),
                   expected_target=target(1))
    assert r2["status"] == "removed"
    assert deleg not in r2["delegates"]

    src3 = record_ticket(store, provider, 3, session=1)
    r3 = acl.apply(event_key="e3", message_key="m3", source_key=src3,
                   user_id=admin, operation="access", delegate_id=None,
                   payload_sha256=sha256_text("p3"), admin_ids=(admin,),
                   expected_target=target(1))
    assert r3["status"] == "access"
    assert r3["delegates"] == []


# ── Non-admin cannot call apply ───────────────────────────────────────────────

@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_non_admin_apply_raises(tmp_path, provider):
    store = make_store(tmp_path, provider)
    acl = make_acl(store, provider)
    admin, user, deleg, _ = ids(provider)
    src = record_ticket(store, provider, 1)
    with pytest.raises(ValueError, match="acl_apply_not_admin"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=user, operation="add", delegate_id=deleg,
                  payload_sha256=sha256_text("p1"), admin_ids=(admin,),
                  expected_target=target(1))
    assert active_count(store) == 1  # ticket untouched


# ── admit: admin always admitted ──────────────────────────────────────────────

@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_admit_admin_always_true(tmp_path, provider):
    store = make_store(tmp_path, provider)
    acl = make_acl(store, provider)
    admin, _, _, _ = ids(provider)
    src = record_ticket(store, provider, 1)
    journal = store.journal
    with journal.transaction() as db:
        result = acl.admit(db, src, admin, (admin,), target(1))
    assert result is True


# ── admit: no-ACL unauthorized ────────────────────────────────────────────────

@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_admit_nonadmin_without_acl_false(tmp_path, provider):
    store = make_store(tmp_path, provider)
    acl = make_acl(store, provider)
    admin, user, _, _ = ids(provider)
    src = record_ticket(store, provider, 1)
    journal = store.journal
    with journal.transaction() as db:
        result = acl.admit(db, user, user, (admin,), target(1))
    assert result is False


# ── admit: delegate admitted after add ───────────────────────────────────────

@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_admit_delegate_after_add(tmp_path, provider):
    store = make_store(tmp_path, provider)
    acl = make_acl(store, provider)
    admin, _, deleg, _ = ids(provider)

    apply_add(acl, store, provider, 1, deleg, admin=admin)

    src2 = record_ticket(store, provider, 2, session=1)
    journal = store.journal
    with journal.transaction() as db:
        assert acl.admit(db, src2, deleg, (admin,), target(1)) is True


# ── admit: immediate revoke on old active ticket ──────────────────────────────

@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_admit_revoked_delegate_false_after_remove(tmp_path, provider):
    store = make_store(tmp_path, provider)
    acl = make_acl(store, provider)
    admin, _, deleg, _ = ids(provider)

    apply_add(acl, store, provider, 1, deleg, admin=admin)

    # Remove delegate (same session 1)
    src2 = record_ticket(store, provider, 2, session=1)
    acl.apply(event_key="e2", message_key="m2", source_key=src2,
              user_id=admin, operation="remove", delegate_id=deleg,
              payload_sha256=sha256_text("p2"), admin_ids=(admin,),
              expected_target=target(1))

    # Now get a fresh ticket and try to admit the revoked delegate
    src3 = record_ticket(store, provider, 3, session=1)
    journal = store.journal
    with journal.transaction() as db:
        assert acl.admit(db, src3, deleg, (admin,), target(1)) is False


# ── admit: unknown source returns False ───────────────────────────────────────

def test_admit_unknown_source_false(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    fake_key = sha256_text("nonexistent")
    journal = store.journal
    with journal.transaction() as db:
        result = acl.admit(db, fake_key, SLACK_ADMIN, (SLACK_ADMIN,), target(1))
    assert result is False


# ── admit: closed ticket returns False ───────────────────────────────────────

def test_admit_closed_ticket_false(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    journal = store.journal
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                   (journal.namespace, src))
        result = acl.admit(db, src, SLACK_ADMIN, (SLACK_ADMIN,), target(1))
    assert result is False


# ── admit: wrong target returns False ────────────────────────────────────────

def test_admit_wrong_target_false(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    journal = store.journal
    with journal.transaction() as db:
        result = acl.admit(db, src, SLACK_ADMIN, (SLACK_ADMIN,), target(99))
    assert result is False


# ── apply: already_present and not_present ────────────────────────────────────

def test_already_present_no_acl_change(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)
    src2 = record_ticket(store, "slack", 2, session=1)
    r = acl.apply(event_key="e2", message_key="m2", source_key=src2,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p2"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert r["status"] == "already_present"
    assert SLACK_DELEG in r["delegates"]


def test_not_present_no_acl_change(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="remove", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert r["status"] == "not_present"


# ── apply: adding/removing admin gives already_admin ─────────────────────────

def test_add_admin_gives_already_admin(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_ADMIN,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert r["status"] == "already_admin"
    assert SLACK_ADMIN not in r["delegates"]


def test_remove_admin_gives_already_admin(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="remove", delegate_id=SLACK_ADMIN,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert r["status"] == "already_admin"


# ── Different sessions have separate ACLs ─────────────────────────────────────

def test_different_sessions_independent_acl(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)

    # Add delegate to session 1
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    # Session 2 has no ACL
    src2 = record_ticket(store, "slack", 2)
    journal = store.journal
    with journal.transaction() as db:
        assert acl.admit(db, src2, SLACK_DELEG, (SLACK_ADMIN,), target(2)) is False

    # Session 1 ticket (re-recorded) still admits delegate
    src1b = record_ticket(store, "slack", 3, session=1)
    with journal.transaction() as db:
        assert acl.admit(db, src1b, SLACK_DELEG, (SLACK_ADMIN,), target(1)) is True


# ── Provider/scope separation ─────────────────────────────────────────────────

def test_provider_scope_separation(tmp_path):
    store_slack = SlackThreadStore(tmp_path)
    store_lark = LarkThreadStore(tmp_path, LARK_SCOPE)
    acl_slack = SessionACL(store_slack, provider="slack", scope=SLACK_SCOPE)
    acl_lark = SessionACL(store_lark, provider="lark", scope=LARK_SCOPE)

    # Add delegate via slack ACL to session 1
    apply_add(acl_slack, store_slack, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    # Lark ACL for session 1 (same thread_id) has no delegates
    src_lark = record_ticket(store_lark, "lark", 2, session=1)
    journal = store_lark.journal
    with journal.transaction() as db:
        lark_delegates = acl_lark.delegates(db, target(1).thread_id)
    assert lark_delegates == []


def test_same_provider_different_scopes_independent(tmp_path):
    store1 = LarkThreadStore(tmp_path, LARK_SCOPE)
    scope2 = "c" * 64
    store2 = LarkThreadStore(tmp_path, scope2)
    acl1 = SessionACL(store1, provider="lark", scope=LARK_SCOPE)
    acl2 = SessionACL(store2, provider="lark", scope=scope2)

    # Add delegate via scope1
    src1 = record_ticket(store1, "lark", 1)
    acl1.apply(event_key="e1", message_key="m1", source_key=src1,
               user_id=LARK_ADMIN, operation="add", delegate_id=LARK_DELEG,
               payload_sha256=sha256_text("p1"), admin_ids=(LARK_ADMIN,),
               expected_target=target(1))

    # scope2 ACL is independent
    journal2 = store2.journal
    with journal2.transaction() as db:
        assert acl2.delegates(db, target(1).thread_id) == []


# ── Full target collision raises ValueError ───────────────────────────────────

def test_full_target_collision_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    with pytest.raises(ValueError, match="acl_target_mismatch"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(99))
    assert active_count(store) == 1


# ── Closed source raises ──────────────────────────────────────────────────────

def test_closed_source_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    journal = store.journal
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                   (journal.namespace, src))
    with pytest.raises(ValueError, match="acl_source_closed"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


def test_unknown_source_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    with pytest.raises(ValueError, match="acl_source_unknown"):
        acl.apply(event_key="e1", message_key="m1",
                  source_key=sha256_text("nonexistent"),
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


# ── Persistence: restart ──────────────────────────────────────────────────────

def test_acl_persists_across_restart(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    # Restart: new store and ACL objects
    store2 = SlackThreadStore(tmp_path)
    acl2 = SessionACL(store2, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store2, "slack", 5, session=1)
    journal = store2.journal
    with journal.transaction() as db:
        assert acl2.admit(db, src, SLACK_DELEG, (SLACK_ADMIN,), target(1)) is True


# ── Duplicate event deduplication ────────────────────────────────────────────

def test_duplicate_event_returns_duplicate_no_mutation(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r1 = acl.apply(event_key="e1", message_key="m1", source_key=src,
                   user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                   payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                   expected_target=target(1))
    assert r1["status"] == "added"

    # Same event_key again
    r2 = acl.apply(event_key="e1", message_key="m1", source_key=src,
                   user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                   payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                   expected_target=target(1))
    assert r2["status"] == "duplicate"
    assert r2["operation_key"] == r1["operation_key"]
    assert active_count(store) == 0  # still closed


# ── Duplicate message deduplication (different event, same message) ───────────

def test_different_event_same_message_deduplicates(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r1 = acl.apply(event_key="evt-original", message_key="msg-physical",
                   source_key=src, user_id=SLACK_ADMIN, operation="add",
                   delegate_id=SLACK_DELEG, payload_sha256=sha256_text("p1"),
                   admin_ids=(SLACK_ADMIN,), expected_target=target(1))
    assert r1["status"] == "added"

    # Different event_key but same message_key → message-level dedup
    r2 = acl.apply(event_key="evt-retry", message_key="msg-physical",
                   source_key=src, user_id=SLACK_ADMIN, operation="add",
                   delegate_id=SLACK_DELEG, payload_sha256=sha256_text("p1"),
                   admin_ids=(SLACK_ADMIN,), expected_target=target(1))
    assert r2["status"] == "duplicate"
    assert r2["operation_key"] == r1["operation_key"]


def test_message_id_collision_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    acl.apply(event_key="e1", message_key="m1", source_key=src,
              user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
              payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))
    # Same message_key, different payload
    with pytest.raises(ValueError, match="acl_message_id_collision"):
        acl.apply(event_key="e2", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("different"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


def test_event_key_collision_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src1 = record_ticket(store, "slack", 1)
    acl.apply(event_key="e1", message_key="m1", source_key=src1,
              user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
              payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))
    # Same event_key, different operation
    with pytest.raises(ValueError, match="acl_command_collision"):
        acl.apply(event_key="e1", message_key="m1-other",
                  source_key=src1, user_id=SLACK_ADMIN, operation="remove",
                  delegate_id=SLACK_DELEG, payload_sha256=sha256_text("p1"),
                  admin_ids=(SLACK_ADMIN,), expected_target=target(1))


# ── Confirmation lifecycle ────────────────────────────────────────────────────

def test_claim_confirmation_returns_once(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    op_key = r["operation_key"]

    conf = acl.claim_confirmation(op_key)
    assert conf is not None
    assert conf["target"] == target(1)
    assert conf["source_key"] == src
    assert SLACK_DELEG in conf["delegates"]
    assert conf["status"] == "added"
    assert len(conf["fingerprint"]) == 64

    # Second call returns None
    assert acl.claim_confirmation(op_key) is None


def test_claim_confirmation_none_for_unknown(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    assert acl.claim_confirmation(sha256_text("nonexistent")) is None


def test_finish_confirmation_sent(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    op_key = r["operation_key"]
    acl.claim_confirmation(op_key)
    acl.finish_confirmation(op_key, "sent")

    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
        entry = journal.get(db, _KIND_ACL_COMMANDS, op_key)
    assert entry["confirmation_status"] == "sent"


def test_finish_confirmation_no_retry_after_uncertain(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    op_key = r["operation_key"]
    acl.claim_confirmation(op_key)
    acl.finish_confirmation(op_key, "uncertain")
    # Second finish is a no-op (already uncertain, not "claimed")
    acl.finish_confirmation(op_key, "sent")  # must not raise and must not change
    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
        entry = journal.get(db, _KIND_ACL_COMMANDS, op_key)
    assert entry["confirmation_status"] == "uncertain"


def test_confirmation_fingerprint_deterministic(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    conf = acl.claim_confirmation(r["operation_key"])
    expected_fp = sha256_text(f"acl_confirm\0{SLACK_SCOPE}\0{sha256_text('e1')}")
    assert conf["fingerprint"] == expected_fp


# ── delegates() query ─────────────────────────────────────────────────────────

def test_delegates_empty_when_no_acl(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    journal = store.journal
    with journal.transaction() as db:
        result = acl.delegates(db, target(1).thread_id)
    assert result == []


def test_delegates_sorted(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    # Add two delegates in reverse order
    apply_add(acl, store, "slack", 1, SLACK_DELEG2, admin=SLACK_ADMIN)
    apply_add(acl, store, "slack", 2, SLACK_DELEG, admin=SLACK_ADMIN, session=1)
    journal = store.journal
    with journal.transaction() as db:
        result = acl.delegates(db, target(1).thread_id)
    assert result == sorted(result)
    assert set(result) == {SLACK_DELEG, SLACK_DELEG2}


# ── Malformed stored ACL fails closed ─────────────────────────────────────────

def test_malformed_acl_schema_version_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL
        acl_key = acl._acl_key(target(1).thread_id)
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["schema_version"] = 99
        journal.put(db, _KIND_ACL, acl_key, entry)

    with pytest.raises(ValueError, match="session_acl_schema_invalid"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


def test_malformed_acl_bool_schema_version_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL
        acl_key = acl._acl_key(target(1).thread_id)
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["schema_version"] = True  # bool not int
        journal.put(db, _KIND_ACL, acl_key, entry)

    with pytest.raises(ValueError, match="session_acl_schema_invalid"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


def test_malformed_acl_delegates_not_sorted_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL
        acl_key = acl._acl_key(target(1).thread_id)
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["delegates"] = [SLACK_DELEG2, SLACK_DELEG]  # unsorted
        journal.put(db, _KIND_ACL, acl_key, entry)

    with pytest.raises(ValueError, match="session_acl_delegates_invalid"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


def test_malformed_acl_identity_mismatch_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL
        acl_key = acl._acl_key(target(1).thread_id)
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["provider"] = "other"
        journal.put(db, _KIND_ACL, acl_key, entry)

    with pytest.raises(ValueError, match="session_acl_identity_mismatch"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


def test_malformed_acl_raises_even_for_noop_apply(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL
        acl_key = acl._acl_key(target(1).thread_id)
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["schema_version"] = 99
        journal.put(db, _KIND_ACL, acl_key, entry)

    src = record_ticket(store, "slack", 5, session=1)
    with pytest.raises(ValueError, match="session_acl_schema_invalid"):
        acl.apply(event_key="e5", message_key="m5", source_key=src,
                  user_id=SLACK_ADMIN, operation="access", delegate_id=None,
                  payload_sha256=sha256_text("p5"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


def test_malformed_command_receipt_raises_on_duplicate(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
        entry = journal.get(db, _KIND_ACL_COMMANDS, r["operation_key"])
        entry["confirmation_claimed"] = "yes"  # not bool
        journal.put(db, _KIND_ACL_COMMANDS, r["operation_key"], entry)

    with pytest.raises(ValueError, match="acl_command_state_invalid"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


def test_malformed_command_claimed_true_no_status_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
        entry = journal.get(db, _KIND_ACL_COMMANDS, r["operation_key"])
        entry["confirmation_claimed"] = True
        entry["confirmation_status"] = None  # invalid: claimed but no status
        journal.put(db, _KIND_ACL_COMMANDS, r["operation_key"], entry)

    with pytest.raises(ValueError, match="acl_command_state_invalid"):
        acl.claim_confirmation(r["operation_key"])


# ── Confirmation not replayable ────────────────────────────────────────────────

def test_confirmation_not_replayable_after_restart(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    op_key = r["operation_key"]
    assert acl.claim_confirmation(op_key) is not None

    # Restart
    store2 = SlackThreadStore(tmp_path)
    acl2 = SessionACL(store2, provider="slack", scope=SLACK_SCOPE)
    assert acl2.claim_confirmation(op_key) is None


# ── Transaction rollback preserves active source ──────────────────────────────

def test_rollback_on_apply_failure_preserves_active_ticket(tmp_path, monkeypatch):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)

    from codex_watchdog.reply_tickets import ReplyTickets
    original_put = ReplyTickets.put

    call_count = [0]
    def failing_put(self, db, kind, key, value):
        call_count[0] += 1
        if kind == "acl_commands":
            raise RuntimeError("simulated failure")
        return original_put(self, db, kind, key, value)

    monkeypatch.setattr(ReplyTickets, "put", failing_put)

    with pytest.raises(RuntimeError, match="simulated failure"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))

    assert active_count(store) == 1  # ticket still active after rollback


# ── Admin non-revocable ───────────────────────────────────────────────────────

def test_admin_in_admin_ids_always_admitted_even_after_remove_attempt(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)

    # Try to remove admin from delegates (admin is in admin_ids, never in delegates)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="remove", delegate_id=SLACK_ADMIN,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert r["status"] == "already_admin"

    # Admin still admitted via admin_ids
    src2 = record_ticket(store, "slack", 2)
    journal = store.journal
    with journal.transaction() as db:
        assert acl.admit(db, src2, SLACK_ADMIN, (SLACK_ADMIN,), target(2)) is True


# ── Route move same-session delegate admission ────────────────────────────────

def test_route_move_does_not_invalidate_acl(tmp_path):
    """ACL is session-scoped; source address changes don't affect delegate list."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)

    # Add delegate to session 1 using first address
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    # Record a new ticket for session 1 at a different address (simulates route move)
    src_new = record_ticket(store, "slack", 10, session=1)
    journal = store.journal
    with journal.transaction() as db:
        # Delegate should still be admitted for the same session
        assert acl.admit(db, src_new, SLACK_DELEG, (SLACK_ADMIN,), target(1)) is True


# ── One-shot admission / race ─────────────────────────────────────────────────

def test_one_shot_admission_callback_prevents_double_claim(tmp_path):
    """Admission callback returning False prevents claim; True allows exactly once."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    admin, _, deleg, _ = ids("slack")

    apply_add(acl, store, "slack", 1, deleg, admin=admin)
    src = record_ticket(store, "slack", 2)

    # Build admission callback: deleg is admitted to session 2 (same thread via session=2 → different session)
    # Actually let's test with an admin callback that allows
    expected = target(2)
    def make_cb(uid, admins, tgt):
        def cb(db):
            return acl.admit(db, src, uid, admins, tgt)
        return cb

    # Admin callback should succeed and claim
    ok_cb = make_cb(admin, (admin,), expected)
    claimed, status = store.claim_reply(
        event_key="reply-1", channel_id=CHANNEL,
        thread_ts="1789111600.{:06d}".format(2),
        instruction_id="iid-1", text="hello", admission=ok_cb)
    assert claimed is True
    assert status is None
    assert active_count(store) == 0  # ticket claimed

    # Duplicate reply: existing receipt, admission not called
    claimed2, status2 = store.claim_reply(
        event_key="reply-1", channel_id=CHANNEL,
        thread_ts="1789111600.{:06d}".format(2),
        instruction_id="iid-1", text="hello")
    assert claimed2 is False


def test_unauthorized_admission_callback_prevents_claim(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)

    def deny_cb(db):
        return False

    claimed, status = store.claim_reply(
        event_key="reply-x", channel_id=CHANNEL,
        thread_ts="1789111600.{:06d}".format(1),
        instruction_id="iid-x", text="hi", admission=deny_cb)
    assert claimed is False
    assert status == "unauthorized"
    assert active_count(store) == 1  # ticket preserved


def test_lark_claim_reply_admission_callback(tmp_path):
    store = LarkThreadStore(tmp_path, LARK_SCOPE)
    acl = SessionACL(store, provider="lark", scope=LARK_SCOPE)
    apply_add(acl, store, "lark", 1, LARK_DELEG, admin=LARK_ADMIN)

    src = record_ticket(store, "lark", 2, session=1)
    mid = msg_id("lark", 2)

    def deny_cb(db):
        return False

    claimed, status = store.claim_reply(
        event_key="ev-lark", message_key="mk-lark",
        payload_sha256=sha256_text("lark-payload"),
        instruction_id="iid-l1", text="hello",
        chat_id=LCHAT, parent_id=mid, admission=deny_cb)
    assert claimed is False
    assert status == "unauthorized"
    assert active_count(store) == 1  # ticket preserved


def test_lark_claim_reply_admin_admission_allows(tmp_path):
    store = LarkThreadStore(tmp_path, LARK_SCOPE)
    acl = SessionACL(store, provider="lark", scope=LARK_SCOPE)
    src = record_ticket(store, "lark", 1)
    mid = msg_id("lark", 1)

    def allow_cb(db):
        return acl.admit(db, src, LARK_ADMIN, (LARK_ADMIN,), target(1))

    claimed, status = store.claim_reply(
        event_key="ev-lark", message_key="mk-lark",
        payload_sha256=sha256_text("lark-payload"),
        instruction_id="iid-l1", text="hello",
        chat_id=LCHAT, parent_id=mid, admission=allow_cb)
    assert claimed is True
    assert status is None


# ── Unknown fields preserved on mutation ──────────────────────────────────────

def test_unknown_acl_fields_preserved_on_mutation(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    # Inject a future field
    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL
        acl_key = acl._acl_key(target(1).thread_id)
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["future_field"] = "preserve_me"
        journal.put(db, _KIND_ACL, acl_key, entry)

    # Now apply a change
    src = record_ticket(store, "slack", 5, session=1)
    acl.apply(event_key="e5", message_key="m5", source_key=src,
              user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG2,
              payload_sha256=sha256_text("p5"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))

    with journal.transaction() as db:
        entry2 = journal.get(db, _KIND_ACL, acl_key)
    assert entry2.get("future_field") == "preserve_me"


# ── ACL snapshot never used as ticket ────────────────────────────────────────

def test_acl_records_never_open_as_thread_ticket(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)
    # Only the original thread ticket was consumed; no new active tickets from ACL
    assert active_count(store) == 0


# ── Issue 1: UUID canonicalization ───────────────────────────────────────────

def test_uppercase_uuid_target_grant_delegates_admission(tmp_path):
    """ACL key must use canonical lowercase UUID regardless of how target was stored."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)

    upper_tid = "11111111-2222-4333-8444-AABBCCDDEEFF"
    canonical_tid = upper_tid.lower()
    tgt_upper = RelayTarget("ws-upper", upper_tid, "process_local")

    channel, ts = CHANNEL, "1789111600.999001"
    store.record_thread(channel, ts, tgt_upper, sha256_text("notice-upper"))
    src_key = store.thread_key(channel, ts)

    r = acl.apply(event_key="eu1", message_key="mu1", source_key=src_key,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("pu1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=tgt_upper)
    assert r["status"] == "added"
    assert SLACK_DELEG in r["delegates"]

    journal = store.journal
    with journal.transaction() as db:
        # Both uppercase and lowercase input to delegates() must find the same ACL
        assert acl.delegates(db, upper_tid) == [SLACK_DELEG]
        assert acl.delegates(db, canonical_tid) == [SLACK_DELEG]

    # Restart: ACL persists
    store2 = SlackThreadStore(tmp_path)
    acl2 = SessionACL(store2, provider="slack", scope=SLACK_SCOPE)
    ts2 = "1789111600.999002"
    store2.record_thread(channel, ts2, tgt_upper, sha256_text("notice-upper2"))
    src2 = store2.thread_key(channel, ts2)
    journal2 = store2.journal
    with journal2.transaction() as db:
        assert acl2.admit(db, src2, SLACK_DELEG, (SLACK_ADMIN,), tgt_upper) is True
        assert acl2.delegates(db, canonical_tid) == [SLACK_DELEG]


# ── Issue 2: Null stored records fail closed ──────────────────────────────────

def _store_raw_null(store, kind, key):
    journal = store.journal
    with journal.transaction() as db:
        db.execute(
            "INSERT INTO records(namespace,kind,key,value,active) VALUES(?,?,?,?,0)"
            " ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value",
            (journal.namespace, kind, key, "null"))


def test_null_acl_record_fails_closed_on_delegates(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    from codex_watchdog.session_acl import _KIND_ACL
    acl_key = acl._acl_key(target(1).thread_id)
    _store_raw_null(store, _KIND_ACL, acl_key)
    journal = store.journal
    with pytest.raises(ValueError, match="session_acl_null_record"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


def test_null_acl_record_fails_closed_on_apply(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    # First add, then corrupt the ACL record
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)
    from codex_watchdog.session_acl import _KIND_ACL
    acl_key = acl._acl_key(target(1).thread_id)
    _store_raw_null(store, _KIND_ACL, acl_key)
    src = record_ticket(store, "slack", 2, session=1)
    with pytest.raises(ValueError, match="session_acl_null_record"):
        acl.apply(event_key="e2", message_key="m2", source_key=src,
                  user_id=SLACK_ADMIN, operation="access", delegate_id=None,
                  payload_sha256=sha256_text("p2"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert active_count(store) == 1  # source ticket preserved


def test_null_command_record_fails_closed_on_duplicate(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
    _store_raw_null(store, _KIND_ACL_COMMANDS, r["operation_key"])
    with pytest.raises(ValueError, match="acl_command_null_record"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


def test_null_message_record_fails_closed_on_dedup(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    acl.apply(event_key="e1", message_key="m1", source_key=src,
              user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
              payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))
    from codex_watchdog.session_acl import _KIND_ACL_MESSAGES
    msg_key = acl._msg_dedup_key("m1")
    _store_raw_null(store, _KIND_ACL_MESSAGES, msg_key)
    src2 = record_ticket(store, "slack", 2, session=1)
    with pytest.raises(ValueError, match="acl_message_null_record"):
        acl.apply(event_key="e2", message_key="m1", source_key=src2,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert active_count(store) == 1  # source ticket preserved


# ── Issue 3: Duplicate delegate IDs, unsupported provider, provider/scope in receipt ──

def test_duplicate_delegate_ids_in_acl_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)
    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL
        acl_key = acl._acl_key(target(1).thread_id)
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["delegates"] = [SLACK_DELEG, SLACK_DELEG]  # duplicate
        journal.put(db, _KIND_ACL, acl_key, entry)
    with pytest.raises(ValueError, match="session_acl_delegates_invalid"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


def test_unsupported_provider_raises():
    store = SlackThreadStore.__new__(SlackThreadStore)
    with pytest.raises(ValueError, match="session_acl_provider_invalid"):
        SessionACL(store, provider="smtp", scope=SLACK_SCOPE)


def test_command_receipt_has_provider_scope(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    journal = store.journal
    with journal.transaction() as db:
        from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
        entry = journal.get(db, _KIND_ACL_COMMANDS, r["operation_key"])
    assert entry["provider"] == "slack"
    assert entry["scope"] == SLACK_SCOPE


# ── Issue 4: Message-level dedup checks ALL immutable fields ──────────────────

def test_same_message_different_target_raises_binding_collision(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src1 = record_ticket(store, "slack", 1)
    acl.apply(event_key="e1", message_key="m-shared", source_key=src1,
              user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
              payload_sha256=sha256_text("p-same"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))
    # Different event, same message+payload, but different source and target
    src2 = record_ticket(store, "slack", 2)
    with pytest.raises(ValueError, match="acl_message_binding_collision"):
        acl.apply(event_key="e2", message_key="m-shared", source_key=src2,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p-same"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(2))
    assert active_count(store) == 1  # src2 still active (no mutation)


def test_same_message_different_operation_raises_binding_collision(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src1 = record_ticket(store, "slack", 1)
    acl.apply(event_key="e1", message_key="m-op", source_key=src1,
              user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
              payload_sha256=sha256_text("p-op"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))
    # Same message, same payload, same target, but different operation
    src2 = record_ticket(store, "slack", 2, session=1)
    with pytest.raises(ValueError, match="acl_message_binding_collision"):
        acl.apply(event_key="e2", message_key="m-op", source_key=src2,
                  user_id=SLACK_ADMIN, operation="remove", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p-op"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


def test_same_message_same_all_fields_deduplicates(tmp_path):
    """Physical message retry with same actor/target/command still deduplicates (not a collision)."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r1 = acl.apply(event_key="e1", message_key="m-retry", source_key=src,
                   user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                   payload_sha256=sha256_text("p-retry"), admin_ids=(SLACK_ADMIN,),
                   expected_target=target(1))
    assert r1["status"] == "added"
    # Same physical message, different event_key, all other fields identical
    r2 = acl.apply(event_key="e2-retry", message_key="m-retry", source_key=src,
                   user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                   payload_sha256=sha256_text("p-retry"), admin_ids=(SLACK_ADMIN,),
                   expected_target=target(1))
    assert r2["status"] == "duplicate"


# ── Issue 5: claim_confirmation validates source address ──────────────────────

def test_claim_confirmation_corrupted_source_address_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    op_key = r["operation_key"]

    # Corrupt the source entry's address (change thread_ts → recomputed key won't match)
    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, "threads", src)
        entry["thread_ts"] = "9999999999.000001"  # different address
        journal.put(db, "threads", src, entry)

    with pytest.raises(ValueError, match="acl_confirmation_source_mismatch"):
        acl.claim_confirmation(op_key)


def test_claim_confirmation_changed_channel_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, "threads", src)
        entry["channel_id"] = "C99999999"  # different channel
        journal.put(db, "threads", src, entry)

    with pytest.raises(ValueError, match="acl_confirmation_source_mismatch"):
        acl.claim_confirmation(r["operation_key"])


# ── Issue 6: Admission callback regression ────────────────────────────────────

def test_closed_ticket_with_allowing_admission_returns_ticket_closed(tmp_path):
    """Admission allowing but ticket already closed → ticket_closed, not unauthorized."""
    store = SlackThreadStore(tmp_path)
    src = record_ticket(store, "slack", 1)
    journal = store.journal
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                   (journal.namespace, src))

    def allow_cb(db):
        return True

    claimed, status = store.claim_reply(
        event_key="reply-closed", channel_id=CHANNEL,
        thread_ts="1789111600.{:06d}".format(1),
        instruction_id="iid-c", text="hi", admission=allow_cb)
    assert claimed is False
    assert status == "ticket_closed"


def test_duplicate_reply_admission_not_called(tmp_path):
    """On existing receipt, admission callback must NOT be called."""
    store = SlackThreadStore(tmp_path)
    record_ticket(store, "slack", 1)
    claimed1, _ = store.claim_reply(
        event_key="reply-dup", channel_id=CHANNEL,
        thread_ts="1789111600.{:06d}".format(1),
        instruction_id="iid-d", text="hi")
    assert claimed1 is True

    called = [False]

    def deny_if_called(db):
        called[0] = True
        return False

    claimed2, status2 = store.claim_reply(
        event_key="reply-dup", channel_id=CHANNEL,
        thread_ts="1789111600.{:06d}".format(1),
        instruction_id="iid-d", text="hi", admission=deny_if_called)
    assert claimed2 is False
    assert not called[0]  # callback never invoked for duplicate


def test_lark_closed_ticket_allowing_admission_returns_ticket_closed(tmp_path):
    store = LarkThreadStore(tmp_path, LARK_SCOPE)
    src = record_ticket(store, "lark", 1)
    journal = store.journal
    with journal.transaction() as db:
        db.execute("UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                   (journal.namespace, src))
    mid = msg_id("lark", 1)

    claimed, status = store.claim_reply(
        event_key="ev-lc", message_key="mk-lc",
        payload_sha256=sha256_text("lark-closed"),
        instruction_id="iid-lc", text="hi",
        chat_id=LCHAT, parent_id=mid, admission=lambda db: True)
    assert claimed is False
    assert status == "ticket_closed"


# ── Finding 1: claim_confirmation uses strict ACL getter ──────────────────────

def test_claim_confirmation_null_acl_record_raises_preserves_command(tmp_path):
    """Null ACL record must raise, not silently return empty delegates; command unchanged."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    op_key = r["operation_key"]

    from codex_watchdog.session_acl import _KIND_ACL, _KIND_ACL_COMMANDS
    acl_key = acl._acl_key(target(1).thread_id)
    _store_raw_null(store, _KIND_ACL, acl_key)

    with pytest.raises(ValueError, match="session_acl_null_record"):
        acl.claim_confirmation(op_key)

    # Command record must NOT have been mutated to claimed=True
    journal = store.journal
    with journal.transaction() as db:
        cmd = journal.get(db, _KIND_ACL_COMMANDS, op_key)
    assert cmd["confirmation_claimed"] is False


# ── Finding 2: _validate_acl deliberate error on non-string delegate ──────────

def test_validate_acl_non_string_delegate_raises_deliberately(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)

    from codex_watchdog.session_acl import _KIND_ACL
    acl_key = acl._acl_key(target(1).thread_id)
    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL, acl_key)
        entry["delegates"] = [123, SLACK_DELEG]  # integer is not a string
        journal.put(db, _KIND_ACL, acl_key, entry)

    with pytest.raises(ValueError, match="session_acl_delegates_invalid"):
        with journal.transaction() as db:
            acl.delegates(db, target(1).thread_id)


# ── Finding 2: _validate_command strict user_id per provider ─────────────────

def test_validate_command_invalid_user_id_raises_on_duplicate(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))

    from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL_COMMANDS, r["operation_key"])
        entry["user_id"] = "not-a-slack-id"
        journal.put(db, _KIND_ACL_COMMANDS, r["operation_key"], entry)

    with pytest.raises(ValueError, match="acl_command_state_invalid"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


# ── Finding 2: _validate_command strict status ────────────────────────────────

def test_validate_command_invalid_status_raises_on_duplicate(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))

    from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL_COMMANDS, r["operation_key"])
        entry["status"] = "approved"  # not in _VALID_STATUSES
        journal.put(db, _KIND_ACL_COMMANDS, r["operation_key"], entry)

    with pytest.raises(ValueError, match="acl_command_state_invalid"):
        acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))


# ── Finding 2: _validate_command delegate/operation cross-validation ──────────

def test_validate_command_access_with_nonnull_delegate_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))

    from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL_COMMANDS, r["operation_key"])
        entry["operation"] = "access"
        # delegate_id is still SLACK_DELEG → invalid for "access"
        journal.put(db, _KIND_ACL_COMMANDS, r["operation_key"], entry)

    with pytest.raises(ValueError, match="acl_command_state_invalid"):
        acl.claim_confirmation(r["operation_key"])


# ── Finding 3: _validate_message requires digest command_key ──────────────────

def test_validate_message_nondigest_command_key_raises(tmp_path):
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    acl.apply(event_key="e1", message_key="m1", source_key=src,
              user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
              payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))

    from codex_watchdog.session_acl import _KIND_ACL_MESSAGES
    msg_key = acl._msg_dedup_key("m1")
    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL_MESSAGES, msg_key)
        entry["command_key"] = "not-a-digest"  # not hex64
        journal.put(db, _KIND_ACL_MESSAGES, msg_key, entry)

    src2 = record_ticket(store, "slack", 2, session=1)
    with pytest.raises(ValueError, match="acl_message_state_invalid"):
        acl.apply(event_key="e2", message_key="m1", source_key=src2,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert active_count(store) == 1  # src2 ticket preserved


# ── Finding 3: message dedup cross-checks ─────────────────────────────────────

def test_message_dedup_event_key_digest_mismatch_raises(tmp_path):
    """event_key_digest in message record disagrees with original command → raises."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    acl.apply(event_key="e1", message_key="m1", source_key=src,
              user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
              payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
              expected_target=target(1))

    from codex_watchdog.session_acl import _KIND_ACL_MESSAGES
    msg_key = acl._msg_dedup_key("m1")
    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL_MESSAGES, msg_key)
        entry["event_key_digest"] = sha256_text("wrong-event")  # disagrees with cmd
        journal.put(db, _KIND_ACL_MESSAGES, msg_key, entry)

    src2 = record_ticket(store, "slack", 2, session=1)
    with pytest.raises(ValueError, match="acl_message_receipt_mismatch"):
        acl.apply(event_key="e2", message_key="m1", source_key=src2,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert active_count(store) == 1  # src2 ticket preserved


def test_message_dedup_message_key_digest_mismatch_raises(tmp_path):
    """Stored command's message_key_digest disagrees with current message_key → raises."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)
    r = acl.apply(event_key="e1", message_key="m1", source_key=src,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))

    from codex_watchdog.session_acl import _KIND_ACL_COMMANDS
    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, _KIND_ACL_COMMANDS, r["operation_key"])
        entry["message_key_digest"] = sha256_text("different-msg-key")  # tampered
        journal.put(db, _KIND_ACL_COMMANDS, r["operation_key"], entry)

    src2 = record_ticket(store, "slack", 2, session=1)
    with pytest.raises(ValueError, match="acl_message_receipt_mismatch"):
        acl.apply(event_key="e2", message_key="m1", source_key=src2,
                  user_id=SLACK_ADMIN, operation="add", delegate_id=SLACK_DELEG,
                  payload_sha256=sha256_text("p1"), admin_ids=(SLACK_ADMIN,),
                  expected_target=target(1))
    assert active_count(store) == 1  # src2 ticket preserved


# ── Finding 4: native source validators reject syntactically invalid fields ───

def test_recompute_source_invalid_channel_id_raises_identity_mismatch(tmp_path):
    """Invalid channel_id in stored thread entry fails source identity check."""
    store = SlackThreadStore(tmp_path)
    acl = SessionACL(store, provider="slack", scope=SLACK_SCOPE)
    src = record_ticket(store, "slack", 1)

    journal = store.journal
    with journal.transaction() as db:
        entry = journal.get(db, "threads", src)
        entry["channel_id"] = "not-valid"  # fails valid_slack_channel_id
        journal.put(db, "threads", src, entry)

    with journal.transaction() as db:
        with pytest.raises(ValueError, match="session_acl_source_identity_mismatch"):
            acl.admit(db, src, SLACK_ADMIN, (SLACK_ADMIN,), target(1))


# ── Finding 4: store provider/scope enforcement ───────────────────────────────

def test_session_acl_store_provider_mismatch_raises(tmp_path):
    store = LarkThreadStore(tmp_path, LARK_SCOPE)
    with pytest.raises(ValueError, match="session_acl_store_provider_mismatch"):
        SessionACL(store, provider="onebot", scope=LARK_SCOPE)


def test_session_acl_store_scope_mismatch_raises(tmp_path):
    store = LarkThreadStore(tmp_path, LARK_SCOPE)
    different_scope = "c" * 64
    with pytest.raises(ValueError, match="session_acl_store_scope_mismatch"):
        SessionACL(store, provider="lark", scope=different_scope)


@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
@pytest.mark.parametrize("decision", [None, 0, 1, "yes", [], {}])
def test_admission_requires_explicit_true(tmp_path, provider, decision):
    store = make_store(tmp_path, provider)
    source = record_ticket(store, provider, 1)
    args = dict(event_key="invalid-decision", instruction_id="instruction", text="continue",
                admission=lambda db: decision)
    if provider == "slack":
        args.update(channel_id=CHANNEL, thread_ts="1789111600.000001")
    else:
        args.update(message_key="physical-message", payload_sha256=sha256_text("continue"),
                    chat_id=LCHAT if provider == "lark" else OBDEST,
                    parent_id=msg_id(provider, 1))
    assert store.claim_reply(**args) == (False, "unauthorized")
    journal = store.journal
    with journal.transaction() as db:
        assert source in journal.active(db)
        assert journal.get(db, "events", sha256_text("invalid-decision")) is None


def test_confirmation_rejects_status_for_different_operation(tmp_path):
    store = make_store(tmp_path, "slack")
    acl = make_acl(store, "slack")
    result = apply_add(acl, store, "slack", 1, SLACK_DELEG, admin=SLACK_ADMIN)
    journal = store.journal
    with journal.transaction() as db:
        receipt = journal.get(db, "acl_commands", result["operation_key"])
        receipt["status"] = "removed"
        journal.put(db, "acl_commands", result["operation_key"], receipt)
    with pytest.raises(ValueError, match="acl_command_state_invalid"):
        acl.claim_confirmation(result["operation_key"])
    with journal.transaction() as db:
        assert journal.get(db, "acl_commands", result["operation_key"]) == receipt


def test_slack_store_rejects_another_provider_acl(tmp_path):
    with pytest.raises(ValueError, match="session_acl_store_provider_mismatch"):
        SessionACL(SlackThreadStore(tmp_path), provider="lark", scope=LARK_SCOPE)


@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_closed_ticket_checked_before_real_acl_admission(tmp_path, provider):
    store = make_store(tmp_path, provider)
    acl = make_acl(store, provider)
    source = record_ticket(store, provider, 1)
    journal = store.journal
    with journal.transaction() as db:
        assert journal.claim(db, source)
    calls = []
    admin = ids(provider)[0]
    def admission(db):
        calls.append(True)
        return acl.admit(db, source, admin, (admin,), target(1))
    args = dict(event_key="closed-source", instruction_id="instruction", text="continue",
                admission=admission)
    if provider == "slack":
        args.update(channel_id=CHANNEL, thread_ts="1789111600.000001")
    else:
        args.update(message_key="physical-message", payload_sha256=sha256_text("continue"),
                    chat_id=LCHAT if provider == "lark" else OBDEST,
                    parent_id=msg_id(provider, 1))
    assert store.claim_reply(**args) == (False, "ticket_closed")
    assert calls == []
