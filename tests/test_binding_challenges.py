"""Tests for durable Feishu/Lark and OneBot exact-session binding challenges."""
import time

import pytest

from codex_watchdog.binding_challenges import BindingChallenges
from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.models import sha256_text
from codex_watchdog.onebot_relay import OneBotThreadStore
from codex_watchdog.relay import RelayTarget


def target(n: int) -> RelayTarget:
    return RelayTarget(
        "workspace-" + str(n),
        "22222222-3333-4444-8888-{:012d}".format(n),
        "process_local",
    )


class ProviderCtx:
    def __init__(self, provider):
        self.provider = provider
        self.scope = sha256_text("bind-fixture\0" + provider)
        if provider == "lark":
            self.store_cls = LarkThreadStore
            self.chat = "oc_" + "a" * 12
            self.user = "ou_" + "b" * 12
            self.other_user = "ou_" + "d" * 12
            self.dest = "oc_" + "c" * 12
            self.dest2 = "oc_" + "e" * 12
        else:
            self.store_cls = OneBotThreadStore
            self.chat = "group:1000001"
            self.user = "1000002"
            self.other_user = "1000003"
            self.dest = "private:2000001"
            self.dest2 = "private:2000002"

    def mid(self, tag, n):
        base = 3_000_000 + tag * 1_000_000 + n
        if self.provider == "lark":
            return "om_" + "{:010d}".format(base)
        return str(base)

    def store(self, tmp_path):
        return self.store_cls(tmp_path, self.scope)

    def challenges(self, store, *, clock=time.time, lifetime=300):
        return BindingChallenges(store, self.provider, self.scope, clock=clock, lifetime=lifetime)


PROVIDERS = ("lark", "onebot")


def ctx(provider):
    return ProviderCtx(provider)


def record_ticket(store, cx, n, *, session=None, chat=None):
    session = n if session is None else session
    chat = chat or cx.chat
    fp = sha256_text(cx.provider + "-notice:" + str(n))
    parent = cx.mid(0, n)
    store.prepare_notification(fp, fp)
    store.finish_notification(fp, chat, parent, target(session))
    return parent, target(session).thread_id


def ticket_active(store, chat, parent):
    key = sha256_text(chat + "\0" + parent)
    journal = store.journal
    with journal.transaction() as db:
        row = db.execute(
            "SELECT active FROM records WHERE namespace=? AND kind='threads' AND key=?",
            (journal.namespace, key),
        ).fetchone()
        return row is not None and row[0] == 1


class FakeClock:
    def __init__(self, start):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, delta):
        self.value += delta


def base_time():
    return time.time() + 3600  # comfortably after any real ticket's recorded created_at


# ── begin() / pending route ─────────────────────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_unbound_session_default_destination_none(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    _, thread_id = record_ticket(store, cx, 1)
    challenges = cx.challenges(store)
    assert challenges.routes.destination(thread_id) is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_begin_consumes_ticket_creates_no_route(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                              message_id=cx.mid(1, 1), created_at=now + 1)
    assert result["status"] == "pending"
    assert result["target"].thread_id == thread_id
    assert isinstance(result["challenge"], str) and result["challenge"].startswith(
        "WD-BIND-LARK-" if provider == "lark" else "WD-BIND-ONEBOT-")
    assert challenges.routes.destination(thread_id) is None
    assert not ticket_active(store, cx.chat, parent)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_begin_rejects_non_bind_text(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    challenges = cx.challenges(store)
    now = base_time()
    with pytest.raises(ValueError, match="bind_text_invalid"):
        challenges.begin("event:1", cx.chat, parent, cx.user, "bind now",
                         message_id=cx.mid(1, 1), created_at=now + 1)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("text", ["bind", "BIND", "  bind  ", "Bind"])
def test_begin_text_case_and_whitespace_insensitive(tmp_path, provider, text):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    challenges = cx.challenges(store)
    now = base_time()
    result = challenges.begin("event:1", cx.chat, parent, cx.user, text,
                              message_id=cx.mid(1, 1), created_at=now + 1)
    assert result["status"] == "pending"


# ── complete() ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_correct_user_and_destination_completes_bind(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    clock = FakeClock(now)
    challenges = cx.challenges(store, clock=clock)
    begin_result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                                    message_id=cx.mid(1, 1), created_at=now + 1)
    token = begin_result["challenge"]

    complete_result = challenges.complete(token, cx.user, cx.dest,
                                          message_id=cx.mid(2, 1), created_at=now + 2)
    assert complete_result["status"] == "bound"
    assert complete_result["target"].thread_id == thread_id
    assert complete_result["destination"] == cx.dest
    assert challenges.routes.destination(thread_id) == cx.dest

    claimed = challenges.claim_hello(complete_result["operation_key"])
    assert claimed is not None
    claimed_target, claimed_dest, fingerprint = claimed
    assert claimed_target.thread_id == thread_id
    assert claimed_dest == cx.dest
    assert isinstance(fingerprint, str) and len(fingerprint) == 64

    # Hello can only be claimed once.
    assert challenges.claim_hello(complete_result["operation_key"]) is None
    challenges.finish_hello(complete_result["operation_key"], "sent")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_wrong_user_cannot_complete(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    begin_result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                                    message_id=cx.mid(1, 1), created_at=now + 1)
    token = begin_result["challenge"]

    with pytest.raises(ValueError, match="bind_challenge_wrong_user"):
        challenges.complete(token, cx.other_user, cx.dest, message_id=cx.mid(2, 1), created_at=now + 2)
    assert challenges.routes.destination(thread_id) is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_garbage_token_cannot_complete(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                     message_id=cx.mid(1, 1), created_at=now + 1)

    with pytest.raises(ValueError, match="bind_challenge_not_found"):
        challenges.complete("not-the-real-token", cx.user, cx.dest,
                           message_id=cx.mid(2, 1), created_at=now + 2)
    assert challenges.routes.destination(thread_id) is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_wrong_provider_challenge_cannot_complete_in_other_namespace(tmp_path, provider):
    other = "onebot" if provider == "lark" else "lark"
    cx, other_cx = ctx(provider), ctx(other)
    store, other_store = cx.store(tmp_path), other_cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    other_challenges = other_cx.challenges(other_store, clock=FakeClock(now))
    begin_result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                                    message_id=cx.mid(1, 1), created_at=now + 1)

    with pytest.raises(ValueError, match="bind_challenge_not_found"):
        other_challenges.complete(begin_result["challenge"], other_cx.user, other_cx.dest,
                                  message_id=other_cx.mid(2, 1), created_at=now + 2)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_expired_challenge_cannot_complete(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    clock = FakeClock(now)
    challenges = cx.challenges(store, clock=clock, lifetime=300)
    begin_result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                                    message_id=cx.mid(1, 1), created_at=now + 1)
    clock.advance(301)

    with pytest.raises(ValueError, match="bind_challenge_expired"):
        challenges.complete(begin_result["challenge"], cx.user, cx.dest,
                           message_id=cx.mid(2, 1), created_at=clock.value + 1)
    assert challenges.routes.destination(thread_id) is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_replayed_complete_is_duplicate_and_inert(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    begin_result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                                    message_id=cx.mid(1, 1), created_at=now + 1)
    confirm_id = cx.mid(2, 1)
    first = challenges.complete(begin_result["challenge"], cx.user, cx.dest,
                                message_id=confirm_id, created_at=now + 2)
    assert first["status"] == "bound"

    second = challenges.complete(begin_result["challenge"], cx.user, cx.dest,
                                 message_id=confirm_id, created_at=now + 2)
    assert second["status"] == "duplicate"
    assert second["target"].thread_id == thread_id

    # The hello was already claimable exactly once; replay never reclaims it.
    challenges.claim_hello(first["operation_key"])
    assert challenges.claim_hello(second["operation_key"]) is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_modified_confirm_message_cannot_rebind(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    begin_result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                                    message_id=cx.mid(1, 1), created_at=now + 1)
    confirm_id = cx.mid(2, 1)
    challenges.complete(begin_result["challenge"], cx.user, cx.dest, message_id=confirm_id, created_at=now + 2)

    with pytest.raises(ValueError, match="bind_completion_collision"):
        challenges.complete(begin_result["challenge"], cx.user, cx.dest2, message_id=confirm_id, created_at=now + 2)
    assert challenges.routes.destination(thread_id) == cx.dest  # unchanged


# ── begin()/unbind() duplicate detection and collisions ─────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_identical_duplicate_begin_returns_no_token(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    r1 = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    r2 = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    assert r2["status"] == "duplicate"
    assert "challenge" not in r2
    assert r2["operation_key"] == r1["operation_key"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_event_key_alias_collision_raises(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    parent2, _ = record_ticket(store, cx, 2)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                     message_id=cx.mid(1, 1), created_at=now + 1)
    with pytest.raises(ValueError, match="bind_operation_collision"):
        challenges.begin("event:1", cx.chat, parent2, cx.user, "bind",
                         message_id=cx.mid(1, 2), created_at=now + 1)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_message_id_alias_collision_raises(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    parent2, _ = record_ticket(store, cx, 2)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    shared_message_id = cx.mid(1, 1)
    challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                     message_id=shared_message_id, created_at=now + 1)
    with pytest.raises(ValueError, match="bind_message_collision"):
        challenges.begin("event:2", cx.chat, parent2, cx.user, "bind",
                         message_id=shared_message_id, created_at=now + 1)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_message_id_alias_duplicate_via_second_event_key_returns_original(tmp_path, provider):
    """Same message re-admitted under a different event_key with identical payload is a duplicate."""
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    message_id = cx.mid(1, 1)
    r1 = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                          message_id=message_id, created_at=now + 1)
    r2 = challenges.begin("event:2", cx.chat, parent, cx.user, "bind",
                          message_id=message_id, created_at=now + 1)
    assert r2["status"] == "duplicate"
    assert r2["operation_key"] == r1["operation_key"]


# ── wrong user / unauthorized begin ─────────────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_begin_invalid_user_id_raises(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    challenges = cx.challenges(store)
    now = base_time()
    with pytest.raises(ValueError, match="bind_user_id_invalid"):
        challenges.begin("event:1", cx.chat, parent, "not-a-valid-user", "bind",
                         message_id=cx.mid(1, 1), created_at=now + 1)


# ── second session isolation and independent provider scopes ───────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_second_session_remains_default_and_binds_independently(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent1, thread1 = record_ticket(store, cx, 1)
    parent2, thread2 = record_ticket(store, cx, 2)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))

    r1 = challenges.begin("event:1", cx.chat, parent1, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    challenges.complete(r1["challenge"], cx.user, cx.dest, message_id=cx.mid(2, 1), created_at=now + 2)
    assert challenges.routes.destination(thread1) == cx.dest
    assert challenges.routes.destination(thread2) is None

    r2 = challenges.begin("event:2", cx.chat, parent2, cx.user, "bind",
                          message_id=cx.mid(1, 2), created_at=now + 1)
    challenges.complete(r2["challenge"], cx.user, cx.dest2, message_id=cx.mid(2, 2), created_at=now + 2)
    assert challenges.routes.destination(thread1) == cx.dest
    assert challenges.routes.destination(thread2) == cx.dest2


def test_independent_provider_scopes_do_not_interfere(tmp_path):
    lark_cx, onebot_cx = ctx("lark"), ctx("onebot")
    lark_store, onebot_store = lark_cx.store(tmp_path), onebot_cx.store(tmp_path)
    lark_parent, lark_thread = record_ticket(lark_store, lark_cx, 1)
    onebot_parent, onebot_thread = record_ticket(onebot_store, onebot_cx, 1)
    now = base_time()
    lark_challenges = lark_cx.challenges(lark_store, clock=FakeClock(now))
    onebot_challenges = onebot_cx.challenges(onebot_store, clock=FakeClock(now))

    r1 = lark_challenges.begin("event:1", lark_cx.chat, lark_parent, lark_cx.user, "bind",
                               message_id=lark_cx.mid(1, 1), created_at=now + 1)
    lark_challenges.complete(r1["challenge"], lark_cx.user, lark_cx.dest,
                             message_id=lark_cx.mid(2, 1), created_at=now + 2)

    assert lark_challenges.routes.destination(lark_thread) == lark_cx.dest
    assert onebot_challenges.routes.destination(onebot_thread) is None


# ── unbind ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_unbind_returns_target_and_default_destination(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent1, thread1 = record_ticket(store, cx, 1)
    parent3, _ = record_ticket(store, cx, 3, session=1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))

    r1 = challenges.begin("event:1", cx.chat, parent1, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    challenges.complete(r1["challenge"], cx.user, cx.dest, message_id=cx.mid(2, 1), created_at=now + 2)
    assert challenges.routes.destination(thread1) == cx.dest

    r2 = challenges.unbind("event:2", cx.chat, parent3, cx.user, "unbind",
                          message_id=cx.mid(1, 3), created_at=now + 3)
    assert r2["status"] == "unbound"
    assert r2["target"].thread_id == thread1
    assert r2["destination"] is None
    assert challenges.routes.destination(thread1) is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_unbind_only_affects_one_session(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent1, thread1 = record_ticket(store, cx, 1)
    parent3, _ = record_ticket(store, cx, 3, session=1)
    parent2, thread2 = record_ticket(store, cx, 2)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))

    r1 = challenges.begin("event:1", cx.chat, parent1, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    challenges.complete(r1["challenge"], cx.user, cx.dest, message_id=cx.mid(2, 1), created_at=now + 2)
    r2 = challenges.begin("event:2", cx.chat, parent2, cx.user, "bind",
                          message_id=cx.mid(1, 2), created_at=now + 1)
    challenges.complete(r2["challenge"], cx.user, cx.dest2, message_id=cx.mid(2, 2), created_at=now + 2)

    challenges.unbind("event:3", cx.chat, parent3, cx.user, "unbind",
                      message_id=cx.mid(1, 3), created_at=now + 3)
    assert challenges.routes.destination(thread1) is None
    assert challenges.routes.destination(thread2) == cx.dest2  # unaffected


@pytest.mark.parametrize("provider", PROVIDERS)
def test_unbind_duplicate_returns_duplicate_without_target_change(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))

    r1 = challenges.unbind("event:1", cx.chat, parent, cx.user, "unbind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    assert r1["status"] == "unbound"
    r2 = challenges.unbind("event:1", cx.chat, parent, cx.user, "unbind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    assert r2["status"] == "duplicate"
    assert challenges.routes.destination(thread_id) is None


# ── superseding a pending challenge ──────────────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_second_begin_supersedes_first_pending_challenge(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent1, thread_id = record_ticket(store, cx, 1)
    parent3, _ = record_ticket(store, cx, 3, session=1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))

    r1 = challenges.begin("event:1", cx.chat, parent1, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    r2 = challenges.begin("event:2", cx.chat, parent3, cx.user, "bind",
                          message_id=cx.mid(1, 3), created_at=now + 2)
    assert r1["challenge"] != r2["challenge"]

    with pytest.raises(ValueError, match="bind_challenge_not_found"):
        challenges.complete(r1["challenge"], cx.user, cx.dest, message_id=cx.mid(2, 1), created_at=now + 2)
    assert challenges.routes.destination(thread_id) is None

    completed = challenges.complete(r2["challenge"], cx.user, cx.dest2,
                                    message_id=cx.mid(2, 3), created_at=now + 2)
    assert completed["status"] == "bound"
    assert challenges.routes.destination(thread_id) == cx.dest2


@pytest.mark.parametrize("provider", PROVIDERS)
def test_unbind_supersedes_pending_challenge_without_route_change(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent1, thread_id = record_ticket(store, cx, 1)
    parent3, _ = record_ticket(store, cx, 3, session=1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))

    r1 = challenges.begin("event:1", cx.chat, parent1, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    challenges.unbind("event:2", cx.chat, parent3, cx.user, "unbind",
                      message_id=cx.mid(1, 3), created_at=now + 2)

    with pytest.raises(ValueError, match="bind_challenge_not_found"):
        challenges.complete(r1["challenge"], cx.user, cx.dest, message_id=cx.mid(2, 1), created_at=now + 3)
    assert challenges.routes.destination(thread_id) is None
    assert challenges.pending() == []


# ── pending() / lookup() ─────────────────────────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_pending_lists_unexpired_challenge_without_raw_token(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                              message_id=cx.mid(1, 1), created_at=now + 1)

    rows = challenges.pending()
    assert len(rows) == 1
    row = rows[0]
    assert row["target"]["thread_id"] == thread_id
    assert row["user_id"] == cx.user
    # OneBot event timestamps have whole-second precision.
    expected_start = int(now) if provider == "onebot" else now
    assert row["created_at"] == expected_start
    assert row["expires_at"] == expected_start + challenges.lifetime
    assert "token_hash" not in row and "challenge" not in row
    for value in row.values():
        assert value != result["challenge"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_pending_excludes_expired_challenge(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    now = base_time()
    clock = FakeClock(now)
    challenges = cx.challenges(store, clock=clock, lifetime=300)
    challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                     message_id=cx.mid(1, 1), created_at=now + 1)
    clock.advance(301)
    assert challenges.pending() == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_lookup_finds_exact_pending_challenge(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                              message_id=cx.mid(1, 1), created_at=now + 1)

    found = challenges.lookup(result["challenge"])
    assert found is not None
    assert found["target"]["thread_id"] == thread_id
    assert challenges.lookup("wrong-token") is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_lookup_returns_none_for_expired_challenge(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, _ = record_ticket(store, cx, 1)
    now = base_time()
    clock = FakeClock(now)
    challenges = cx.challenges(store, clock=clock, lifetime=300)
    result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                              message_id=cx.mid(1, 1), created_at=now + 1)
    clock.advance(301)
    assert challenges.lookup(result["challenge"]) is None


# ── bounded pending challenges per namespace ────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_pending_challenges_bounded_per_namespace(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))

    tickets = [record_ticket(store, cx, n) for n in range(1, 33)]
    for n, (parent, _) in enumerate(tickets, start=1):
        result = challenges.begin("event:{}".format(n), cx.chat, parent, cx.user, "bind",
                                  message_id=cx.mid(1, n), created_at=now + 1)
        assert result["status"] == "pending"
    assert len(challenges.pending()) == 32

    overflow_parent, _ = record_ticket(store, cx, 33)
    with pytest.raises(ValueError, match="bind_pending_limit_reached"):
        challenges.begin("event:33", cx.chat, overflow_parent, cx.user, "bind",
                         message_id=cx.mid(1, 33), created_at=now + 1)
    # Refused before claim: the 33rd session's ticket remains untouched.
    assert ticket_active(store, cx.chat, overflow_parent)
    assert len(challenges.pending()) == 32


# ── restart durability ───────────────────────────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_restart_preserves_route_and_consumed_challenge(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    r1 = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    challenges.complete(r1["challenge"], cx.user, cx.dest, message_id=cx.mid(2, 1), created_at=now + 2)

    restarted_store = cx.store(tmp_path)
    restarted_challenges = cx.challenges(restarted_store, clock=FakeClock(now + 10))
    assert restarted_challenges.routes.destination(thread_id) == cx.dest
    # The consumed challenge never revives after restart.
    with pytest.raises(ValueError, match="bind_challenge_not_found"):
        restarted_challenges.complete(r1["challenge"], cx.user, cx.dest2,
                                     message_id=cx.mid(2, 2), created_at=now + 11)


# ── malformed persisted state fails closed ──────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_malformed_route_fails_closed(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    r1 = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    challenges.complete(r1["challenge"], cx.user, cx.dest, message_id=cx.mid(2, 1), created_at=now + 2)

    journal = store.journal
    with journal.transaction() as db:
        key = challenges.routes._route_key(thread_id)
        entry = journal.get(db, "session_routes", key)
        entry["destination"] = "not-a-real-destination"
        journal.put(db, "session_routes", key, entry)

    with pytest.raises(ValueError, match="session_route_destination_invalid"):
        challenges.routes.destination(thread_id)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_malformed_challenge_fails_closed_on_pending_and_lookup(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    result = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                              message_id=cx.mid(1, 1), created_at=now + 1)

    journal = store.journal
    with journal.transaction() as db:
        key = challenges.routes._route_key(thread_id)
        rows = db.execute("SELECT key, value FROM records WHERE namespace=? AND kind='bind_challenges'",
                          (journal.namespace,)).fetchall()
        assert len(rows) == 1
        entry = journal.get(db, "bind_challenges", key)
        entry["generation"] = "not-an-int"
        journal.put(db, "bind_challenges", key, entry)

    with pytest.raises(ValueError, match="bind_challenge_state_invalid"):
        challenges.pending()
    with pytest.raises(ValueError, match="bind_challenge_state_invalid"):
        challenges.lookup(result["challenge"])


# ── uncertain hello never retries ────────────────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_uncertain_hello_status_never_retried(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    parent, thread_id = record_ticket(store, cx, 1)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    r1 = challenges.begin("event:1", cx.chat, parent, cx.user, "bind",
                          message_id=cx.mid(1, 1), created_at=now + 1)
    completed = challenges.complete(r1["challenge"], cx.user, cx.dest,
                                    message_id=cx.mid(2, 1), created_at=now + 2)

    claimed = challenges.claim_hello(completed["operation_key"])
    assert claimed is not None
    challenges.finish_hello(completed["operation_key"], "uncertain")

    assert challenges.claim_hello(completed["operation_key"]) is None
    assert challenges.routes.destination(thread_id) == cx.dest  # route unaffected


def test_finish_hello_invalid_status_raises(tmp_path):
    cx = ctx("lark")
    store = cx.store(tmp_path)
    challenges = cx.challenges(store)
    with pytest.raises(ValueError, match="bind_hello_status_invalid"):
        challenges.finish_hello("whatever", "claimed")


# ── existing four-ticket retention is untouched by binding admission ───────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_four_ticket_retention_unchanged_by_binding_claim(tmp_path, provider):
    cx = ctx(provider)
    store = cx.store(tmp_path)
    for n in range(1, 5):
        record_ticket(store, cx, n, session=1)
    journal = store.journal
    with journal.transaction() as db:
        assert len(journal.active(db)) == 4

    parent2 = cx.mid(0, 2)
    now = base_time()
    challenges = cx.challenges(store, clock=FakeClock(now))
    challenges.begin("event:1", cx.chat, parent2, cx.user, "bind",
                     message_id=cx.mid(1, 1), created_at=now + 1)

    with journal.transaction() as db:
        assert len(journal.active(db)) == 3  # only the claimed ticket closed


# ── Slack semantics remain untouched ────────────────────────────────────────

def test_slack_session_routes_still_work(tmp_path):
    from codex_watchdog.slack_mapping import SlackThreadStore
    from codex_watchdog.session_routes import SessionRoutes

    store = SlackThreadStore(tmp_path)
    fp = sha256_text("slack-notice:1")
    store.record_thread("C12345678", "1789111600.000100", target(1), fp)
    routes = SessionRoutes(store, provider="slack", scope="C00000001")
    result = routes.apply("event:E001", "C12345678", "1789111600.000100", "U12345678",
                          "bind C99999999", "C99999999", command_ts="1789111600.000200")
    assert result["status"] == "bound"
    assert routes.destination(target(1).thread_id) == "C99999999"
