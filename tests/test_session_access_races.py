"""Admission races after real relay lookup and before the durable reply claim."""
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.relay import RelayTarget
from codex_watchdog.session_acl import SessionACL

import test_session_access_relays as fixtures


REMOTE_TARGET = RelayTarget(
    "fixture-workspace", fixtures.TARGET_THREAD, "remote_ssh",
    "ssh-remote+fixture-original", "/srv/fixture-original", "a" * 32,
)


@pytest.fixture(params=("slack", "lark", "onebot"))
def case(request, tmp_path, monkeypatch):
    provider = request.param
    calls, queue = fixtures._queue()
    if provider == "slack":
        sender, _ = fixtures.slack_access_sender()
        relay = fixtures.slack_relay(
            tmp_path, queue=queue, access_sender=sender,
            access_api=fixtures.slack_access_api(fixtures.DELEGATE_USER),
        )
        admin, delegate = fixtures.ADMIN_USER, fixtures.DELEGATE_USER
        source, grant_source, confirmation = (
            fixtures.THREAD_TS, fixtures.THREAD_TS2, fixtures.NEW_CONF_TS,
        )
        record = partial(fixtures.record_slack_notification, relay.thread_store,
                         target=REMOTE_TARGET)
        record_at = lambda address: record(ts=address)
        key = partial(relay.thread_store.thread_key, fixtures.CHANNEL)
        grant_event = fixtures.slack_reply_event(
            thread_ts=grant_source, ts=fixtures.ADMIN_MSG_TS2,
            client_msg_id="race-grant",
        )
        reply = lambda user: fixtures.slack_reply_event(
            user=user, ts=fixtures.DELEGATE_MSG_TS2, text="Continue the fixture task",
            client_msg_id="race-ordinary",
        )
        handle, scope = relay.handle_message, fixtures.CHANNEL
        changed = replace(REMOTE_TARGET, remote_authority="ssh-remote+fixture-changed")
    elif provider == "lark":
        relay = fixtures.lark_relay(tmp_path, queue=queue, api=fixtures.LarkApi())
        admin, delegate = fixtures.LARK_ADMIN, fixtures.LARK_DELEGATE
        source, grant_source, confirmation = (
            fixtures.LARK_NOTIF_MSG, fixtures.LARK_NOTIF_MSG2, fixtures.LARK_CONF_MSG,
        )
        record = partial(fixtures.record_lark_notification, relay.thread_store,
                         target=REMOTE_TARGET)
        record_at = lambda address: record(msg_id=address)
        key = partial(relay.thread_store._address, fixtures.LARK_CHAT)
        grant_event = fixtures.lark_access_event(
            root_id=grant_source, parent_id=grant_source,
            msg_id=fixtures.LARK_ADMIN_CMD2, event_id="race-grant",
        )
        reply = lambda user: fixtures.lark_plain_event(open_id=user, event_id="race-ordinary")
        handle, scope = relay.handle_event, relay.config.scope
        changed = replace(REMOTE_TARGET, remote_repo_path="/srv/fixture-changed")
    else:
        relay = fixtures.onebot_relay(tmp_path, queue=queue, api=fixtures.OneBotApi())
        admin, delegate = fixtures.OB_ADMIN, fixtures.OB_DELEGATE
        source, grant_source, confirmation = (
            fixtures.OB_NOTIF_MSG, fixtures.OB_NOTIF_MSG2, fixtures.OB_CONF_MSG,
        )
        record = partial(fixtures.record_ob_notification, relay.thread_store,
                         target=REMOTE_TARGET)
        record_at = lambda address: record(msg_id=address)
        key = partial(relay.thread_store._address, fixtures.OB_DESTINATION)
        grant_event = fixtures.ob_access_event(
            reply_to=grant_source, msg_id=fixtures.OB_ADMIN_CMD2,
        )
        reply = lambda user: fixtures.ob_event(user_id=user, msg_id=fixtures.OB_DELEGATE_REPLY)
        handle, scope = relay.handle_event, relay.config.scope
        changed = replace(REMOTE_TARGET, remote_storage_key="b" * 32)

    record_at(source)
    journal = relay.thread_store.journal
    with journal.transaction() as db:
        original = journal.get(db, "threads", key(source))
    # These spies retain the real ownership and dispatch implementation.
    controlled = Mock(wraps=relay._controlled_reply)
    dispatch = Mock(wraps=relay._dispatch)
    monkeypatch.setattr(relay, "_controlled_reply", controlled)
    monkeypatch.setattr(relay, "_dispatch", dispatch)
    return SimpleNamespace(
        relay=relay, journal=journal, acl=SessionACL(relay.thread_store, provider, scope),
        admin=admin, delegate=delegate, source_key=key(source),
        confirmation_key=key(confirmation), grant_source=grant_source,
        grant_event=grant_event, record=record_at, handle=handle, reply=reply,
        original=original, changed=changed, calls=calls,
        controlled=controlled, dispatch=dispatch,
    )


def grant_delegate(case):
    """Grant through the provider's real add handler, preserving the older source."""
    case.record(case.grant_source)
    result = case.handle(case.grant_event)
    assert (result.status, result.delivery_status) == ("access_applied", "added")
    with case.journal.transaction() as db:
        assert case.acl.delegates(db, REMOTE_TARGET.thread_id) == [case.delegate]
        assert case.confirmation_key in case.journal.active(db)


def interleave(case, monkeypatch, user, change):
    """Run a committed change after lookup captured the target, before claim."""
    store = case.relay.thread_store
    lookup, claim = store.lookup_thread, store.claim_reply
    captured, outcomes = [], []

    def observed_lookup(*args, **kwargs):
        mapping = lookup(*args, **kwargs)
        captured.append(mapping.target)
        return mapping

    def changed_claim(**kwargs):
        assert captured == [REMOTE_TARGET]
        with case.journal.transaction() as db:
            assert case.acl.admit(db, case.source_key, user, (case.admin,), REMOTE_TARGET)
            assert 1 <= len(case.journal.active(db)) <= 4
        change()
        result = claim(**kwargs)
        outcomes.append(result)
        return result

    monkeypatch.setattr(store, "lookup_thread", observed_lookup)
    monkeypatch.setattr(store, "claim_reply", changed_claim)
    result = case.handle(case.reply(user))
    assert captured == [REMOTE_TARGET]
    assert outcomes == [(False, "unauthorized")]
    return result


def assert_denied(case, result, expected_target):
    assert result.status == "ignored_unauthorized"
    case.controlled.assert_not_called()
    case.dispatch.assert_not_called()
    assert case.calls == []
    with case.journal.transaction() as db:
        assert case.journal.get(db, "threads", case.source_key) == dict(
            case.original, target=expected_target.to_dict(),
        )
        active = case.journal.active(db)
        assert case.source_key in active
        assert 1 <= len(active) <= 4
        assert db.execute(
            "SELECT COUNT(*) FROM records WHERE namespace=? AND kind IN ('events', 'messages')",
            (case.journal.namespace,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("role", ("admin", "delegate"))
def test_full_remote_target_change_between_lookup_and_claim(case, monkeypatch, role):
    if role == "delegate":
        grant_delegate(case)
    user = getattr(case, role)

    def change_target():
        with case.journal.transaction() as db:
            entry = case.journal.get(db, "threads", case.source_key)
            assert entry == case.original
            # Only one remote routing field changes: UUID, workspace, address,
            # and the SQL active flag all retain their original values.
            case.journal.put(db, "threads", case.source_key,
                             dict(entry, target=case.changed.to_dict()))

    result = interleave(case, monkeypatch, user, change_target)
    assert_denied(case, result, case.changed)


def test_delegate_revoked_between_lookup_and_claim_on_older_ticket(case, monkeypatch):
    grant_delegate(case)
    assert case.confirmation_key != case.source_key

    def revoke():
        result = case.acl.apply(
            event_key="race-revoke-event", message_key="race-revoke-message",
            source_key=case.confirmation_key, user_id=case.admin,
            operation="remove", delegate_id=case.delegate,
            payload_sha256=sha256_text("remove fixture delegate"),
            admin_ids=(case.admin,), expected_target=REMOTE_TARGET,
        )
        assert result["status"] == "removed"

    result = interleave(case, monkeypatch, case.delegate, revoke)
    assert_denied(case, result, REMOTE_TARGET)
    with case.journal.transaction() as db:
        assert case.acl.delegates(db, REMOTE_TARGET.thread_id) == []
        assert case.confirmation_key not in case.journal.active(db)
