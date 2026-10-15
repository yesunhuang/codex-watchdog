"""Behavioral tests for the bounded per-session activity lifecycle."""

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest


def test_redirected_snapshot_refuses_only_that_session(tmp_path, monkeypatch):
    from pathlib import Path
    from codex_watchdog.session_activity import SessionActivity
    thread = "11111111-1111-4111-8111-111111111111"
    other = "22222222-2222-4222-8222-222222222222"
    store = SessionActivity(tmp_path, clock=lambda: 100)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == thread + ".json" or original(path))
    with pytest.raises(ValueError, match="redirected"):
        store.observe(thread, genuine_at=100)
    assert store.observe(other, genuine_at=100)["activated"]

from codex_watchdog.session_activity import (
    ACTIVE,
    DORMANT,
    MAX_SNAPSHOT_BYTES,
    TTL_SECONDS,
    SessionActivity,
    hello_claim_key,
)
from codex_watchdog.storage import InstructionStore

HOUR = 60 * 60
T0 = 1_700_000_000.0
FP_A = "a" * 64
FP_B = "b" * 64


def sid(n):
    return f"00000000-0000-4000-8000-{n:012x}"


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return SessionActivity(tmp_path, clock=clock)


def state_path(runtime, session_id):
    return Path(runtime) / "lifecycle" / f"{session_id}.json"


def read_bytes(runtime, session_id):
    return state_path(runtime, session_id).read_bytes()


def activate(store, clock, session_id):
    result = store.observe(session_id, genuine_at=clock())
    assert result["status"] == ACTIVE and result["hello_eligible"] is True
    return result


def make_dormant(store, clock, session_id):
    clock.advance(TTL_SECONDS)
    result = store.observe(session_id, turn_active=False, queue_active=False)
    assert result["status"] == DORMANT
    return result


# Lifecycle transitions


@pytest.mark.parametrize(
    "age, expected",
    [(TTL_SECONDS - 60, ACTIVE), (TTL_SECONDS, DORMANT), (TTL_SECONDS + 1, DORMANT)],
    ids=["71h59m", "exactly-72h", "beyond-72h"],
)
def test_dormancy_boundary_is_exactly_72_hours(store, clock, age, expected):
    s = sid(1)
    store.observe(s, genuine_at=T0)
    clock.advance(age)
    result = store.observe(s, turn_active=False, queue_active=False)
    assert result["status"] == expected
    assert result["dormant"] is (expected == DORMANT)
    assert result["last_activity_at"] == T0
    assert result["generation"] == 1
    assert store.is_active(s) is (expected == ACTIVE)


@pytest.mark.parametrize(
    "turn, queue",
    [(True, False), (False, True), (True, True), (None, False), (False, None), (None, None)],
)
def test_live_or_unknown_work_blocks_dormancy_without_refreshing(store, clock, turn, queue):
    s = sid(2)
    store.observe(s, genuine_at=T0)
    clock.advance(10 * TTL_SECONDS)
    result = store.observe(s, turn_active=turn, queue_active=queue)
    assert result["status"] == ACTIVE and result["dormant"] is False
    assert result["last_activity_at"] == T0
    clock.advance(1)
    assert store.observe(s, turn_active=False, queue_active=False)["status"] == DORMANT


def test_unknown_sensor_is_monitored_but_never_manufactures_activity(store, clock):
    s = sid(3)
    first = store.observe(s)
    assert (first["status"], first["generation"], first["hello_eligible"]) == (ACTIVE, 0, False)
    assert first["last_activity_at"] is None and first["activated"] is False
    assert store.claim_hello(s, "slack", "team", FP_A) is False

    clock.advance(10 * TTL_SECONDS)
    later = store.observe(s, turn_active=False, queue_active=False)
    assert later["status"] == ACTIVE and later["last_activity_at"] is None
    assert later["changed"] is False

    # The first known genuine activity behaves like a fresh first discovery.
    found = store.observe(s, genuine_at=clock() - HOUR)
    assert (found["generation"], found["hello_eligible"], found["activated"]) == (1, True, True)


def test_missing_state_reads_as_active_without_creating_snapshot(tmp_path, store):
    s = sid(4)
    assert store.is_active(s) is True
    assert store.snapshot(s) is None
    assert store.claim_hello(s, "lark", "chat", FP_A) is False
    assert not (tmp_path / "lifecycle").exists()


def test_reads_never_transition_state(tmp_path, store, clock):
    s = sid(5)
    store.observe(s, genuine_at=T0)
    before = read_bytes(tmp_path, s)
    clock.advance(10 * TTL_SECONDS)
    assert store.is_active(s) is True
    assert store.snapshot(s)["status"] == ACTIVE
    assert read_bytes(tmp_path, s) == before


def test_old_idle_discovery_starts_dormant_without_hello(store):
    s = sid(6)
    result = store.observe(s, genuine_at=T0 - TTL_SECONDS, turn_active=False, queue_active=False)
    assert (result["status"], result["generation"], result["hello_eligible"]) == (DORMANT, 0, False)
    assert result["dormant"] is True and result["activated"] is False
    assert store.claim_hello(s, "slack", "team", FP_A) is False


def test_old_activity_with_live_work_is_discovered_active_without_hello(store):
    s = sid(7)
    result = store.observe(s, genuine_at=T0 - 5 * TTL_SECONDS, turn_active=True)
    assert (result["status"], result["generation"], result["hello_eligible"]) == (ACTIVE, 0, False)
    assert result["last_activity_at"] == T0 - 5 * TTL_SECONDS


def test_fresh_discovery_is_hello_eligible_once_across_duplicates_and_restarts(
    tmp_path, store, clock
):
    s = sid(8)
    seen_at = T0 - (TTL_SECONDS - 60)
    first = store.observe(s, genuine_at=seen_at)
    assert (first["status"], first["generation"], first["hello_eligible"]) == (ACTIVE, 1, True)
    assert first["activated"] is True and first["changed"] is True
    assert store.claim_hello(s, "slack", "team", FP_A) is True

    again = store.observe(s, genuine_at=seen_at)
    assert again["changed"] is False and again["activated"] is False

    restarted = SessionActivity(tmp_path, clock=clock)
    clock.advance(HOUR)
    newer = restarted.observe(s, genuine_at=clock())
    assert newer["generation"] == 1 and newer["activated"] is False
    assert newer["changed"] is True and newer["last_activity_at"] == clock()
    assert restarted.claim_hello(s, "slack", "team", FP_A) is False
    assert len(restarted.snapshot(s)["hello_claims"]) == 1


def test_explicit_fresh_enrollment_activates_and_stale_enrollment_does_not(store, clock):
    s = sid(9)
    enrolled = store.observe(s, enrollment_at=T0)
    assert (enrolled["generation"], enrolled["hello_eligible"], enrolled["activated"]) == (1, True, True)
    make_dormant(store, clock, s)
    clock.advance(HOUR)
    stale = store.observe(s, enrollment_at=T0)
    assert stale["status"] == DORMANT and stale["activated"] is False
    fresh = store.observe(s, enrollment_at=clock())
    assert (fresh["status"], fresh["generation"], fresh["hello_eligible"]) == (ACTIVE, 2, True)
    assert fresh["activated"] is True


def test_stale_or_busy_signals_cannot_reactivate_dormant_session(store, clock):
    s = sid(10)
    store.observe(s, genuine_at=T0)
    assert store.claim_hello(s, "slack", "team", FP_A) is True
    make_dormant(store, clock, s)
    clock.advance(10 * HOUR)
    for kwargs in (
        {"genuine_at": T0},
        {"genuine_at": T0 + HOUR},
        {"turn_active": True, "queue_active": True},
    ):
        result = store.observe(s, **kwargs)
        assert result["status"] == DORMANT and result["generation"] == 1
        assert result["activated"] is False
    assert store.snapshot(s)["last_activity_at"] == T0 + HOUR

    revived = store.observe(s, genuine_at=clock() - 60)
    assert (revived["status"], revived["generation"], revived["hello_eligible"]) == (ACTIVE, 2, True)
    assert revived["hello_claims"] == {}
    assert store.claim_hello(s, "slack", "team", FP_A) is True


def test_clock_rollback_cannot_resurrect_expired_activity(store, clock):
    s = sid(11)
    store.observe(s, genuine_at=T0)
    clock.advance(TTL_SECONDS + HOUR)
    held = store.observe(s, turn_active=True, queue_active=False)
    assert held["status"] == ACTIVE

    clock.now = T0 + HOUR  # wall clock jumps backwards
    result = store.observe(s, turn_active=False, queue_active=False)
    assert result["status"] == DORMANT
    assert result["checked_at"] == T0 + TTL_SECONDS + HOUR
    assert store.observe(s, genuine_at=T0)["status"] == DORMANT


def test_timestamps_are_bounded_by_monotonic_effective_now(tmp_path, store, clock):
    s = sid(12)
    store.observe(s, genuine_at=T0)
    clock.now = T0 - HOUR
    # Ahead of the rolled-back clock but not of the persisted check time.
    accepted = store.observe(s, genuine_at=T0 - 60)
    assert accepted["last_activity_at"] == T0 and accepted["checked_at"] == T0
    before = read_bytes(tmp_path, s)
    with pytest.raises(ValueError):
        store.observe(s, genuine_at=T0 + 1)
    assert read_bytes(tmp_path, s) == before


def test_node_handoff_on_same_runtime_preserves_generation_and_claims(tmp_path, clock):
    s = sid(13)
    node_a = SessionActivity(tmp_path, clock=clock)
    behind = Clock(T0 - 10 * 60)  # the second node's clock lags
    node_b = SessionActivity(tmp_path, clock=behind)
    node_a.observe(s, genuine_at=T0)
    assert node_a.claim_hello(s, "onebot", "group", FP_A) is True

    handed = node_b.observe(s, genuine_at=T0 - 60)
    assert handed["generation"] == 1 and handed["activated"] is False
    assert handed["last_activity_at"] == T0 and handed["checked_at"] == T0
    assert node_b.claim_hello(s, "onebot", "group", FP_B) is False

    behind.now = T0 + TTL_SECONDS
    assert node_b.observe(s, turn_active=False, queue_active=False)["status"] == DORMANT
    clock.now = T0 + TTL_SECONDS + HOUR
    back = node_a.observe(s, genuine_at=clock())
    assert (back["generation"], back["hello_eligible"]) == (2, True)
    assert node_b.claim_hello(s, "onebot", "group", FP_B) is True
    assert node_a.claim_hello(s, "onebot", "group", FP_A) is False


# Legacy migration


def test_legacy_migration_suppresses_only_the_initial_hello(store, clock):
    s = sid(14)
    seeded = store.observe(s, genuine_at=T0, legacy=True)
    assert (seeded["status"], seeded["generation"], seeded["hello_eligible"]) == (ACTIVE, 1, False)
    assert seeded["activated"] is False
    assert store.claim_hello(s, "lark", "chat", FP_A) is False

    clock.advance(HOUR)
    assert store.observe(s, genuine_at=clock())["hello_eligible"] is False
    assert store.observe(s, genuine_at=clock(), legacy=True)["changed"] is False

    make_dormant(store, clock, s)
    clock.advance(HOUR)
    revived = store.observe(s, genuine_at=clock(), legacy=True)
    assert (revived["generation"], revived["hello_eligible"], revived["activated"]) == (2, True, True)
    assert store.claim_hello(s, "lark", "chat", FP_A) is True

    repeat = store.observe(s, legacy=True)
    assert repeat["hello_eligible"] is True and repeat["changed"] is False
    assert len(repeat["hello_claims"]) == 1


def test_legacy_seeding_over_unknown_placeholder_prevents_later_spam(store, clock):
    s = sid(15)
    store.observe(s)
    seeded = store.observe(s, legacy=True)
    assert (seeded["generation"], seeded["hello_eligible"]) == (1, False)
    later = store.observe(s, genuine_at=clock())
    assert (later["generation"], later["hello_eligible"], later["activated"]) == (1, False, False)


# Hello claims


def test_claims_are_independent_per_uuid_provider_and_scope(tmp_path, store, clock):
    a, b = sid(16), sid(17)
    activate(store, clock, a)
    assert store.claim_hello(a, "slack", "team-1", FP_A) is True
    assert store.claim_hello(a, "slack", "team-2", FP_A) is True
    assert store.claim_hello(a, "lark", "team-1", FP_A) is True
    assert store.claim_hello(a, "slack", "team-1", FP_B) is False
    # A claim on one UUID grants nothing to another.
    assert store.claim_hello(b, "slack", "team-1", FP_A) is False
    activate(store, clock, b)
    assert store.claim_hello(b, "slack", "team-1", FP_A) is True

    claims = store.snapshot(a)["hello_claims"]
    expected = hashlib.sha256(b"slack" + bytes(1) + b"team-1").hexdigest()
    assert hello_claim_key("slack", "team-1") == expected
    assert len(claims) == 3
    assert claims[expected]["generation"] == 1
    assert claims[expected]["route_fingerprint"] == FP_A
    assert b"team-1" not in read_bytes(tmp_path, a)


def test_hello_claim_does_not_touch_activity_or_dormancy_timing(store, clock):
    s = sid(18)
    store.observe(s, genuine_at=T0)
    clock.advance(TTL_SECONDS - 60)
    store.observe(s)
    before = store.snapshot(s)
    clock.advance(30)
    assert store.claim_hello(s, "slack", "team", FP_A) is True
    after = store.snapshot(s)
    for field in ("status", "generation", "hello_eligible", "last_activity_at", "checked_at"):
        assert after[field] == before[field]
    clock.advance(30)
    assert store.observe(s, turn_active=False, queue_active=False)["status"] == DORMANT


def test_claim_is_final_for_generation_even_if_send_fails_or_process_crashes(
    tmp_path, store, clock
):
    s = sid(19)
    activate(store, clock, s)
    assert store.claim_hello(s, "onebot", "group", FP_A) is True
    # Delivery fails or the process dies before sending; no result is recorded.
    restarted = SessionActivity(tmp_path, clock=clock)
    assert restarted.claim_hello(s, "onebot", "group", FP_A) is False
    assert restarted.claim_hello(s, "onebot", "group", FP_B) is False
    clock.advance(HOUR)
    observed = restarted.observe(s, genuine_at=clock(), turn_active=True)
    assert observed["generation"] == 1 and observed["last_activity_at"] == clock()
    assert restarted.claim_hello(s, "onebot", "group", FP_A) is False


def test_snapshot_stays_bounded_across_many_generations(tmp_path, store, clock):
    s = sid(20)
    activate(store, clock, s)
    sizes = []
    for _ in range(20):
        for scope in ("one", "two"):
            assert store.claim_hello(s, "slack", scope, FP_A) is True
        sizes.append(len(read_bytes(tmp_path, s)))
        make_dormant(store, clock, s)
        clock.advance(HOUR)
        store.observe(s, genuine_at=clock())
    final = store.snapshot(s)
    assert final["generation"] == 21
    assert final["hello_claims"] == {}
    assert max(sizes) - min(sizes) < 64
    assert list((tmp_path / "lifecycle").iterdir()) == [state_path(tmp_path, s)]


# Input validation


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-uuid",
        sid(0xABCDEF).upper(),
        "{" + sid(21) + "}",
        sid(21).replace("-", ""),
        " " + sid(21),
        "urn:uuid:" + sid(21),
        sid(21) + "/../x",
        uuid.UUID(sid(21)),
        21,
        None,
    ],
)
def test_non_canonical_session_ids_are_rejected_everywhere(tmp_path, store, bad):
    with pytest.raises(ValueError):
        store.observe(bad, genuine_at=T0)
    with pytest.raises(ValueError):
        store.claim_hello(bad, "slack", "team", FP_A)
    with pytest.raises(ValueError):
        store.is_active(bad)
    with pytest.raises(ValueError):
        store.snapshot(bad)
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"genuine_at": True},
        {"genuine_at": "1700000000"},
        {"genuine_at": float("nan")},
        {"genuine_at": float("inf")},
        {"genuine_at": -1.0},
        {"genuine_at": 10 ** 400},
        {"genuine_at": T0 + HOUR},
        {"enrollment_at": T0 + TTL_SECONDS},
        {"enrollment_at": False},
        {"turn_active": 1},
        {"queue_active": "no"},
        {"legacy": None},
    ],
)
def test_invalid_signals_are_refused_without_mutation(tmp_path, store, clock, kwargs):
    known, fresh = sid(22), sid(23)
    store.observe(known, genuine_at=T0 - HOUR)
    before = read_bytes(tmp_path, known)
    clock.advance(1)
    with pytest.raises(ValueError):
        store.observe(known, **kwargs)
    assert read_bytes(tmp_path, known) == before
    with pytest.raises(ValueError):
        store.observe(fresh, **kwargs)
    assert not state_path(tmp_path, fresh).exists()


@pytest.mark.parametrize("bad_now", [float("nan"), float("inf"), -5.0, True, "now", None])
def test_invalid_clock_is_refused(tmp_path, bad_now):
    store = SessionActivity(tmp_path, clock=lambda: bad_now)
    with pytest.raises(ValueError):
        store.observe(sid(24))
    assert not state_path(tmp_path, sid(24)).exists()


@pytest.mark.parametrize(
    "provider, scope, fingerprint",
    [
        ("", "team", FP_A),
        ("Slack", "team", FP_A),
        ("discord", "team", FP_A),
        (None, "team", FP_A),
        ("slack", "", FP_A),
        ("slack", None, FP_A),
        ("slack", "team", FP_A.upper()),
        ("slack", "team", "a" * 63),
        ("slack", "team", "g" * 64),
        ("slack", "team", FP_A + "\n"),
        ("slack", "team", None),
    ],
)
def test_invalid_claim_arguments_are_rejected(tmp_path, store, clock, provider, scope, fingerprint):
    s = sid(25)
    activate(store, clock, s)
    before = read_bytes(tmp_path, s)
    with pytest.raises(ValueError):
        store.claim_hello(s, provider, scope, fingerprint)
    assert read_bytes(tmp_path, s) == before
    assert store.claim_hello(s, "slack", "team", FP_A) is True


# Malformed state is isolated to its own session


def _claim(record):
    return next(iter(record["hello_claims"].values()))


CORRUPTIONS = {
    "schema-version": lambda r: r.update(schema_version=2),
    "schema-bool": lambda r: r.update(schema_version=True),
    "schema-missing": lambda r: r.pop("schema_version"),
    "other-identity": lambda r: r.update(session_id=sid(99)),
    "status": lambda r: r.update(status="SLEEPING"),
    "generation-negative": lambda r: r.update(generation=-1),
    "generation-float": lambda r: r.update(generation=1.0),
    "eligible-int": lambda r: r.update(hello_eligible=1),
    "checked-text": lambda r: r.update(checked_at="soon"),
    "activity-after-check": lambda r: r.update(last_activity_at=r["checked_at"] + 1),
    "dormant-without-activity": lambda r: r.update(
        status=DORMANT, hello_eligible=False, last_activity_at=None
    ),
    "eligible-generation-zero": lambda r: r.update(generation=0),
    "claims-list": lambda r: r.update(hello_claims=[]),
    "claim-raw-scope-key": lambda r: r.update(hello_claims={"team": _claim(r)}),
    "claim-fingerprint": lambda r: _claim(r).update(route_fingerprint="A" * 64),
    "claim-generation-float": lambda r: _claim(r).update(generation=1.5),
    "claim-future-generation": lambda r: _claim(r).update(generation=2),
}


def _valid_record(runtime, store, clock, session_id):
    activate(store, clock, session_id)
    assert store.claim_hello(session_id, "slack", "team", FP_A) is True
    return json.loads(read_bytes(runtime, session_id))


def _assert_isolated_failure(runtime, store, clock, broken, healthy):
    path = state_path(runtime, broken)
    before = path.read_bytes()
    calls = (
        lambda: store.observe(broken, genuine_at=clock()),
        lambda: store.is_active(broken),
        lambda: store.snapshot(broken),
        lambda: store.claim_hello(broken, "lark", "chat", FP_A),
    )
    for call in calls:
        with pytest.raises(ValueError):
            call()
    assert path.read_bytes() == before
    assert store.is_active(healthy) is True
    assert store.claim_hello(healthy, "lark", "chat", FP_A) is True


@pytest.mark.parametrize("name", sorted(CORRUPTIONS))
def test_malformed_snapshot_fails_only_that_session(tmp_path, store, clock, name):
    broken, healthy = sid(30), sid(31)
    record = _valid_record(tmp_path, store, clock, broken)
    activate(store, clock, healthy)
    CORRUPTIONS[name](record)
    state_path(tmp_path, broken).write_text(json.dumps(record), encoding="utf-8")
    _assert_isolated_failure(tmp_path, store, clock, broken, healthy)


@pytest.mark.parametrize(
    "payload",
    [b"", b"{not json", b"null", b"[]", bytes([0xFF, 0xFE, 0x7B]), b"[" * 60000],
    ids=["empty", "syntax", "null", "list", "not-utf8", "deep-nesting"],
)
def test_unparseable_snapshot_fails_only_that_session(tmp_path, store, clock, payload):
    broken, healthy = sid(32), sid(33)
    activate(store, clock, broken)
    activate(store, clock, healthy)
    state_path(tmp_path, broken).write_bytes(payload)
    _assert_isolated_failure(tmp_path, store, clock, broken, healthy)


def test_snapshot_read_limit_is_inclusive_and_oversize_is_refused(tmp_path, store, clock):
    broken, healthy = sid(34), sid(35)
    body = json.dumps(_valid_record(tmp_path, store, clock, broken)).encode("utf-8")
    path = state_path(tmp_path, broken)
    path.write_bytes(body + b" " * (MAX_SNAPSHOT_BYTES - len(body)))
    assert store.snapshot(broken)["generation"] == 1
    path.write_bytes(body + b" " * (MAX_SNAPSHOT_BYTES + 1 - len(body)))
    activate(store, clock, healthy)
    _assert_isolated_failure(tmp_path, store, clock, broken, healthy)


def test_unknown_keys_survive_updates(tmp_path, store, clock):
    s = sid(36)
    record = _valid_record(tmp_path, store, clock, s)
    record["future_field"] = {"note": "kept"}
    _claim(record)["future_claim_field"] = 7
    state_path(tmp_path, s).write_text(json.dumps(record), encoding="utf-8")
    clock.advance(HOUR)
    store.observe(s, genuine_at=clock())
    assert store.claim_hello(s, "lark", "chat", FP_B) is True
    stored = json.loads(read_bytes(tmp_path, s))
    assert stored["future_field"] == {"note": "kept"}
    assert stored["hello_claims"][hello_claim_key("slack", "team")]["future_claim_field"] == 7
    assert store.snapshot(s)["future_field"] == {"note": "kept"}


# Storage behavior


def test_writer_failure_preserves_prior_snapshot_and_grants_no_claim(
    tmp_path, store, clock, monkeypatch
):
    s = sid(37)
    store.observe(s, genuine_at=T0)
    make_dormant(store, clock, s)
    before = read_bytes(tmp_path, s)

    def boom(path, value):
        raise OSError("simulated publication failure")

    with monkeypatch.context() as patched:
        patched.setattr(InstructionStore, "_atomic_json", staticmethod(boom))
        clock.advance(HOUR)
        with pytest.raises(OSError):
            store.observe(s, genuine_at=clock())
        assert read_bytes(tmp_path, s) == before

    revived = store.observe(s, genuine_at=clock())
    assert revived["activated"] is True
    with monkeypatch.context() as patched:
        patched.setattr(InstructionStore, "_atomic_json", staticmethod(boom))
        with pytest.raises(OSError):
            store.claim_hello(s, "slack", "team", FP_A)
    assert store.snapshot(s)["hello_claims"] == {}
    assert store.claim_hello(s, "slack", "team", FP_A) is True


def test_point_operations_never_scan_directories(tmp_path, store, clock, monkeypatch):
    lifecycle = tmp_path / "lifecycle"
    locks = tmp_path / "locks"
    lifecycle.mkdir()
    locks.mkdir()
    for n in range(200):
        (lifecycle / f"{sid(1000 + n)}.json").write_text("{corrupt history")
        (lifecycle / f"history-{n}.log").write_text("old")
        (locks / f"lifecycle-{sid(1000 + n)}.lock").write_text("")

    def refuse(*args, **kwargs):
        raise AssertionError("directory scan attempted")

    for name in ("glob", "rglob", "iterdir"):
        monkeypatch.setattr(Path, name, refuse)
    monkeypatch.setattr(os, "listdir", refuse)
    monkeypatch.setattr(os, "scandir", refuse)

    s = sid(38)
    assert store.is_active(s) is True
    activate(store, clock, s)
    assert store.claim_hello(s, "slack", "team", FP_A) is True
    make_dormant(store, clock, s)
    assert store.is_active(s) is False
    assert store.snapshot(s)["status"] == DORMANT
    with pytest.raises(ValueError):
        store.observe(sid(1000), genuine_at=clock())
