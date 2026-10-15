"""Fresh local notification entry through real locks and receipt transactions.

Provider endpoints, source identities and all files are disposable fixtures.
These tests establish no native ownership, old-sender exclusion or live delivery.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
import json
from pathlib import Path
import socket
import sqlite3
from threading import Event

import pytest

from codex_watchdog import notification_fresh_entry as fresh
from codex_watchdog import notification_receipt_retention as finite
from codex_watchdog import notification_root_retention as root
from codex_watchdog import relay_notification_migration as migration
from codex_watchdog.models import sha256_text
from codex_watchdog.notification_state import CurrentNotificationState
from codex_watchdog.notification_senders import sender_fence
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.storage import FileLock, StoreBusyError
from test_notification_native_enrollment import (
    NOW, PROVIDERS, database, native, notifier, retained,
)
from test_notification_sender_integration import busy, held_elsewhere


def observed(index=1, *, born=NOW - 1):
    return NotificationEvent("fresh-observer-" + str(index), "discovery_attention",
        "verified-transition-" + str(index), "Subject", "Fixture output",
        source_observer=True, source_born=born)


def source(kind, index=1, *, born=NOW - 1):
    return observed(index, born=born) if kind == "observer" else native(index, born=born)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("fresh-entry fixture attempted provider network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def no_history(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("ordinary fresh entry attempted cold/history work")
    monkeypatch.setattr(finite, "_legacy_rows", refuse)
    monkeypatch.setattr(finite, "prepare_notification_receipts", refuse)
    monkeypatch.setattr(root, "prepare_notification_root", refuse)
    monkeypatch.setattr(migration, "_history_lookup", refuse)
    monkeypatch.setattr(CurrentNotificationState, "import_current", refuse)


def assert_fresh_root(runtime):
    value = root.validate_notification_root(runtime)
    assert value["origin"] == finite.FRESH_ORIGIN
    assert value["floor"] == 0 and value["state"] == "ready"
    plan = json.loads((runtime / "notifications/finite-receipts-preparation.json").read_bytes())
    assert plan["origin"] == finite.FRESH_ORIGIN and plan["born"] == 0
    assert plan["plan_sha256"] == value["plan_sha256"]
    assert plan["retained"] == {"dual": [], "fallback": []}
    return value


def provider_state(runtime, fingerprint, provider):
    with closing(sqlite3.connect(database(runtime, provider))) as db:
        if provider in ("slack", "lark"):
            return db.execute("SELECT " + provider + " FROM receipts WHERE fingerprint=?",
                              (fingerprint,)).fetchone()[0]
        return db.execute("SELECT state FROM receipts WHERE fingerprint=? AND provider=?",
                          (fingerprint, provider)).fetchone()[0]


@pytest.mark.parametrize("kind", ["observer", "native"])
@pytest.mark.parametrize("provider", PROVIDERS)
def test_first_verified_effect_initializes_before_claim_and_attempts_once(
        tmp_path, monkeypatch, kind, provider):
    current, calls, clock = source(kind), [], [NOW]
    claim = CurrentNotificationState.claim
    phases = []

    def checked_claim(self, *args, **kwargs):
        busy(tmp_path)
        with pytest.raises(StoreBusyError):
            with FileLock(tmp_path / "locks/notifications.lock"):
                pytest.fail("first current claim lacks notification fence")
        assert_fresh_root(tmp_path)
        phases.append("claim")
        return claim(self, *args, **kwargs)

    def before_send():
        assert phases == ["claim"]
        assert CurrentNotificationState(tmp_path).claim_time(
            current.dedupe_key(), current.event_fingerprint()) == NOW
        row = retained(tmp_path, current.event_fingerprint(), provider)
        assert row[4:7] == (NOW - 1, NOW - 1 + finite.TTL, 0)
        assert provider_state(tmp_path, current.event_fingerprint(), provider) == "uncertain"
        phases.append("provider")

    sender = notifier(tmp_path, provider, clock, calls, before_send)
    no_history(monkeypatch)
    monkeypatch.setattr(CurrentNotificationState, "claim", checked_claim)
    assert not (tmp_path / "notifications").exists()
    assert sender.notify(current).status == "sent"
    assert phases == ["claim", "provider"] and calls == [provider]
    ready = (tmp_path / "notifications" / root.READY).read_bytes()
    assert provider_state(tmp_path, current.event_fingerprint(), provider) == "sent"
    # A new notifier object reuses the terminal claim rather than retrying it.
    assert notifier(tmp_path, provider, clock, calls).notify(current).status == "suppressed"
    assert calls == [provider] and (tmp_path / "notifications" / root.READY).read_bytes() == ready


@pytest.mark.parametrize("kind", ["observer", "native"])
def test_explicit_fresh_zero_floor_includes_original_epoch_zero_birth(tmp_path, kind):
    calls, clock = [], [1.0]
    sender = notifier(tmp_path, "slack", clock, calls)
    for index in (1, 2):
        current = source(kind, index, born=0)
        assert sender.notify(current).status == "sent"
        row = retained(tmp_path, current.event_fingerprint(), "slack")
        assert row[4] == 0 and row[5] == finite.TTL
    assert calls == ["slack", "slack"]
    assert_fresh_root(tmp_path)


@pytest.mark.parametrize("born", [None, NOW + 1, NOW - finite.TTL])
def test_missing_future_or_expired_observer_birth_does_not_poison_absent_namespace(tmp_path, born):
    calls, clock = [], [NOW]
    sender = notifier(tmp_path, "slack", clock, calls)
    assert sender.notify(observed(born=born)).status in ("state_error", "suppressed", "delivery_failed")
    assert calls == [] and not (tmp_path / "notifications").exists()
    # A different genuine source still initializes; no source time is re-minted.
    assert sender.notify(observed(2)).status == "sent" and calls == ["slack"]


@pytest.mark.parametrize("damage", ["missing-provider", "bad-fingerprint", "bad-birth"])
def test_invalid_first_context_refuses_before_exclusive_directory_creation(tmp_path, damage):
    sender = notifier(tmp_path, "slack", [NOW], [])
    value = sender.receipt_context(observed())
    if damage == "missing-provider":
        value["providers"] = {}
    elif damage == "bad-fingerprint":
        value["fingerprint"] = "not-a-digest"
    else:
        value["born"] = True
    with sender_fence(tmp_path), FileLock(tmp_path / "locks/notifications.lock"):
        with pytest.raises(ValueError):
            fresh.ensure_fresh_notification_entry(tmp_path, value, clock=lambda: NOW)
    assert not (tmp_path / "notifications").exists()


@pytest.mark.parametrize("damage", ["empty", "legacy", "malformed-ready", "partial"])
def test_existing_namespace_is_not_initialized_imported_or_repaired(tmp_path, monkeypatch, damage):
    directory = tmp_path / "notifications"
    directory.mkdir()
    protected = {}
    if damage == "legacy":
        protected["last-events.json"] = json.dumps(dict(schema_version=1, last_events={})).encode()
        protected["dual-deliveries.json"] = json.dumps(dict(schema_version=1, events={})).encode()
    elif damage == "malformed-ready":
        protected[root.READY] = b'{"schema_version":1,"state":"broken"}'
    elif damage == "partial":
        protected[root.INTENT] = b'{"schema_version":1,"incomplete":true}'
        protected[".fresh-dual.sqlite3"] = b"partial fixture must remain unchanged"
    for name, raw in protected.items():
        (directory / name).write_bytes(raw)
    calls, clock = [], [NOW]
    no_history(monkeypatch)
    assert notifier(tmp_path, "slack", clock, calls).notify(observed()).status != "sent"
    assert calls == []
    for name, raw in protected.items():
        assert (directory / name).read_bytes() == raw
    for family in ("dual", "fallback"):
        assert not (directory / (finite.NAMES[family] + ".sqlite3")).exists()
    if damage != "malformed-ready":
        assert not (directory / root.READY).exists()
    # An outer current claim may remain terminal; it never licenses a legacy send.


@pytest.mark.parametrize("kind", ["directory-link", "dangling-link", "regular-file"])
def test_noncanonical_or_non_directory_namespace_is_never_fresh(tmp_path, kind):
    path, target = tmp_path / "notifications", tmp_path / "untouched-target"
    if kind == "regular-file":
        path.write_bytes(b"operator data")
    else:
        if kind == "directory-link":
            target.mkdir()
            (target / "sentinel").write_bytes(b"operator data")
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip("fixture platform cannot create directory symlink: " + str(exc))
    calls = []
    try:
        result = notifier(tmp_path, "slack", [NOW], calls).notify(observed())
    except (ValueError, OSError):
        # CurrentNotificationState.path already refuses a redirected namespace
        # before the guarded claim try-block. That is an existing safe refusal.
        pass
    else:
        assert result.status != "sent"
    assert not calls
    if kind == "regular-file":
        assert path.read_bytes() == b"operator data"
    elif kind == "directory-link":
        assert list(target.iterdir()) == [target / "sentinel"]
        assert (target / "sentinel").read_bytes() == b"operator data"
    else:
        assert path.is_symlink() and not target.exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_typed_native_hello_cannot_bypass_existing_unprepared_namespace(tmp_path, monkeypatch, legacy):
    directory = tmp_path / "notifications"
    directory.mkdir()
    original = b'{"schema_version":1,"events":{}}'
    if legacy:
        (directory / "dual-deliveries.json").write_bytes(original)
    calls = []

    def post(token, method, payload, timeout):
        calls.append(method)
        return dict(channel=payload["channel"], ts="1792008000.000001")

    config = NotificationConfig(slack_bot_token="xoxb-fixture", slack_channel_id="C12345678",
        slack_allowed_user_ids=("U12345678",), slack_reply_mode="poll")
    sender = EnvironmentNotifier(tmp_path, config, slack_api_post=post, clock=lambda: NOW)
    hello = replace(native(born=NOW - 1), event_type="session_hello",
                    provider_filter=("slack",))
    assert config.slack_relay_configured and sender.receipt_context(hello) is not None

    def no_claim(*args, **kwargs):
        pytest.fail("typed Hello claimed before verifying the prepared namespace")

    no_history(monkeypatch)
    monkeypatch.setattr(CurrentNotificationState, "claim", no_claim)
    assert sender.notify(hello).status == "state_error"
    assert calls == [] and not (directory / "recent-notification-claims.sqlite3").exists()
    assert not (directory / root.READY).exists()
    if legacy:
        assert (directory / "dual-deliveries.json").read_bytes() == original


def test_actual_selected_authority_and_busy_sender_fence_precede_initialization(tmp_path, monkeypatch):
    selected, stale, controller = (tmp_path / name for name in ("selected", "stale", "controller"))
    selected.mkdir()
    calls, clock = [], [NOW]
    sender = notifier(selected, "slack", clock, calls)
    sender.runtime, sender.relay_runtime = controller, stale
    sender.lock_path = stale / "locks/notifications.lock"
    phases = []

    class Authority:
        runtime = stale

        @contextmanager
        def guard(self):
            phases.append("authority")
            self.runtime = controller
            sender.relay_runtime = selected
            sender.lock_path = controller / "locks/notifications.lock"
            yield

    ensure = fresh.ensure_fresh_notification_entry

    def checked_entry(runtime, receipt_context, **kwargs):
        assert runtime == selected
        for actual in (controller, selected):
            busy(actual)
            with pytest.raises(StoreBusyError):
                with FileLock(actual / "locks/notifications.lock"):
                    pytest.fail("selected/controller notification fence absent")
        return ensure(runtime, receipt_context, **kwargs)

    monkeypatch.setattr(fresh, "ensure_fresh_notification_entry", checked_entry)
    sender.relay_authority = Authority()
    with held_elsewhere(selected):
        with pytest.raises(StoreBusyError):
            sender.notify(observed())
    assert calls == [] and not (selected / "notifications").exists()
    assert sender.notify(observed()).status == "sent" and calls == ["slack"]
    assert phases == ["authority", "authority"]
    assert_fresh_root(selected)
    assert not stale.exists() and not (controller / "notifications").exists()


def test_concurrent_first_notifiers_use_one_real_sender_hold_and_one_attempt(tmp_path):
    entered, release = Event(), Event()
    calls, clock = [], [NOW]

    def held_provider():
        entered.set()
        assert release.wait(10), "fixture release timed out"

    first = notifier(tmp_path, "slack", clock, calls, held_provider)
    second = notifier(tmp_path, "slack", clock, calls)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first.notify, observed())
        try:
            assert entered.wait(5), "first notifier never reached fake provider"
            with pytest.raises(StoreBusyError):
                second.notify(observed())
        finally:
            release.set()
        assert future.result(timeout=5).status == "sent"
    assert second.notify(observed()).status == "suppressed"
    assert calls == ["slack"] and assert_fresh_root(tmp_path)


class SimulatedProcessDeath(BaseException):
    pass


@pytest.mark.parametrize("stage", ["directory", "ready"])
def test_interrupted_initialization_is_preserved_and_not_automatically_resumed(tmp_path, monkeypatch, stage):
    calls, clock, hits = [], [NOW], []
    sender = notifier(tmp_path, "slack", clock, calls)
    mkdir, write = Path.mkdir, fresh._write_new

    def interrupted_mkdir(path, *args, **kwargs):
        result = mkdir(path, *args, **kwargs)
        if stage == "directory" and path == tmp_path / "notifications" and not hits:
            hits.append(stage)
            raise SimulatedProcessDeath()
        return result

    def interrupted_write(path, value):
        if stage == "ready" and path.name == root.READY and not hits:
            hits.append(stage)
            raise SimulatedProcessDeath()
        return write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", interrupted_mkdir)
        patch.setattr(fresh, "_write_new", interrupted_write)
        with pytest.raises(SimulatedProcessDeath):
            sender.notify(observed())
    directory = tmp_path / "notifications"
    assert hits == [stage] and directory.is_dir() and not calls
    assert not (directory / root.READY).exists()
    assert not (directory / "recent-notification-claims.sqlite3").exists()
    names = ("finite-receipts-preparation.json", root.INTENT, "dual-deliveries.json",
             "dual-deliveries.sqlite3", "fallback-deliveries.sqlite3")
    saved = {name: (directory / name).read_bytes() for name in names if (directory / name).exists()}
    assert notifier(tmp_path, "slack", clock, calls).notify(observed()).status != "sent"
    assert calls == [] and not (directory / root.READY).exists()
    assert all((directory / name).read_bytes() == raw for name, raw in saved.items())


@pytest.mark.parametrize("stage", ["plan", "dual"])
def test_explicit_offline_recovery_preserves_partial_fresh_zero_floor(tmp_path, monkeypatch, stage):
    current, calls, clock, hits = observed(born=0), [], [1.0], []
    sender = notifier(tmp_path, "slack", clock, calls)
    write, publish = fresh._write_new, fresh._publish_family

    def stop_after_plan(path, value):
        write(path, value)
        if stage == "plan" and path.name == "finite-receipts-preparation.json":
            hits.append(stage)
            raise SimulatedProcessDeath()

    def stop_after_dual(directory, family, plan):
        publish(directory, family, plan)
        if stage == "dual" and family == "dual":
            hits.append(stage)
            raise SimulatedProcessDeath()

    with monkeypatch.context() as patch:
        patch.setattr(fresh, "_write_new", stop_after_plan)
        patch.setattr(fresh, "_publish_family", stop_after_dual)
        with pytest.raises(SimulatedProcessDeath):
            sender.notify(current)
    directory = tmp_path / "notifications"
    plan_path = directory / "finite-receipts-preparation.json"
    original_plan = plan_path.read_bytes()
    plan = json.loads(original_plan)
    assert hits == [stage] and not calls
    assert not (directory / root.READY).exists()
    assert database(tmp_path, "slack").exists() is (stage == "dual")
    assert not database(tmp_path, "smtp").exists()
    value = sender.receipt_context(current)
    # The ordinary helper must not finish a partial namespace. Calling only
    # that helper here does not reserve the still-unattempted logical effect.
    with sender_fence(tmp_path), FileLock(tmp_path / "locks/notifications.lock"):
        try:
            existing = fresh.ensure_fresh_notification_entry(tmp_path, value, clock=lambda: 2.0)
        except finite.RetentionError:
            assert stage == "dual"
        else:
            assert existing["status"] == "existing" and stage == "plan"
    assert not (directory / root.READY).exists()
    assert not (directory / "recent-notification-claims.sqlite3").exists()
    assert plan_path.read_bytes() == original_plan

    # Explicit offline recovery is authorized only for this owned fixture;
    # nullcontext is not evidence about a real installed old sender.
    recovered = root.prepare_notification_root(tmp_path,
        scopes=plan["scopes"], current_keys=[], contexts=[], current_pins=(),
        sender_fence=nullcontext, clock=lambda: 2.0)
    assert recovered["status"] == "ready" and recovered["floor"] == 0
    assert recovered["plan_sha256"] == plan["plan_sha256"]
    assert plan_path.read_bytes() == original_plan
    assert_fresh_root(tmp_path)
    clock[0] = 2.0
    assert sender.notify(current).status == "sent"
    assert sender.notify(current).status == "suppressed"
    # A genuinely different verified observer scope enrolls inside its claim,
    # without a generic maintenance pass or a replacement initialization floor.
    assert sender.notify(observed(2, born=0)).status == "sent"
    assert calls == ["slack", "slack"]


def test_ready_published_before_unclaimed_crash_allows_first_attempt_without_reinitializing(tmp_path, monkeypatch):
    calls, clock = [], [NOW]
    sender = notifier(tmp_path, "slack", clock, calls)

    def stopped_before_claim(*args, **kwargs):
        assert_fresh_root(tmp_path)
        raise SimulatedProcessDeath()

    with monkeypatch.context() as patch:
        patch.setattr(CurrentNotificationState, "claim", stopped_before_claim)
        with pytest.raises(SimulatedProcessDeath):
            sender.notify(observed())
    ready = (tmp_path / "notifications" / root.READY).read_bytes()
    assert not calls and not (tmp_path / "notifications/recent-notification-claims.sqlite3").exists()
    assert notifier(tmp_path, "slack", clock, calls).notify(observed()).status == "sent"
    assert calls == ["slack"] and (tmp_path / "notifications" / root.READY).read_bytes() == ready


def test_failed_attempt_is_terminal_but_other_family_and_later_effect_progress(tmp_path):
    calls, clock = [], [NOW]
    first = observed()

    def lost_ack():
        calls.append("slack-attempt")
        raise OSError("disposable lost ACK")

    sender = notifier(tmp_path, "slack", clock, calls, lost_ack)
    assert sender.notify(first).status == "delivery_failed"
    assert provider_state(tmp_path, first.event_fingerprint(), "slack") == "uncertain"
    assert notifier(tmp_path, "slack", clock, calls).notify(first).status == "suppressed"
    assert notifier(tmp_path, "smtp", clock, calls).notify(observed(2)).status == "sent"
    assert notifier(tmp_path, "slack", clock, calls).notify(observed(3)).status == "sent"
    assert calls == ["slack-attempt", "smtp", "slack"]


def test_existing_ready_does_not_global_validate_or_recreate_unavailable_other_family(tmp_path):
    calls, clock = [], [NOW]
    assert notifier(tmp_path, "slack", clock, calls).notify(native(born=NOW - 1)).status == "sent"
    first = native(born=NOW - 1)
    path = database(tmp_path, "slack")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE retention SET protected=1 WHERE fingerprint=? AND provider='slack'",
                   (first.event_fingerprint(),))
    original_pin = retained(tmp_path, first.event_fingerprint(), "slack")
    ready = (tmp_path / "notifications" / root.READY).read_bytes()
    database(tmp_path, "smtp").unlink()
    assert notifier(tmp_path, "slack", clock, calls).notify(native(2, born=NOW - 1)).status == "sent"
    assert notifier(tmp_path, "smtp", clock, calls).notify(observed(3)).status == "delivery_failed"
    assert not database(tmp_path, "smtp").exists()
    assert notifier(tmp_path, "slack", clock, calls).notify(observed(4)).status == "sent"
    assert retained(tmp_path, first.event_fingerprint(), "slack") == original_pin
    assert (tmp_path / "notifications" / root.READY).read_bytes() == ready
    assert calls == ["slack", "slack", "slack"]


def measured_first_entry(runtime, monkeypatch):
    calls, sql, connections, opened, vm = [], [], [], [], [0]
    sender = notifier(runtime, "slack", [NOW], calls)
    connect, path_open = sqlite3.connect, Path.open

    def progress():
        vm[0] += 1
        return 0

    def measured_connect(path, *args, **kwargs):
        assert "unrelated-history" not in str(path)
        db = connect(path, *args, **kwargs)
        connections.append(Path(str(path).split("?", 1)[0]).name)
        db.set_trace_callback(sql.append)
        db.set_progress_handler(progress, 1)
        return db

    def measured_open(path, *args, **kwargs):
        assert path.name not in ("last-events.json", "unrelated-history.sqlite3")
        opened.append(path.name)
        return path_open(path, *args, **kwargs)

    def no_listing(*args, **kwargs):
        pytest.fail("ordinary first entry enumerated historical names")

    with monkeypatch.context() as patch:
        no_history(patch)
        patch.setattr(sqlite3, "connect", measured_connect)
        patch.setattr(Path, "open", measured_open)
        for name in ("glob", "rglob", "iterdir"):
            patch.setattr(Path, name, no_listing)
        assert sender.notify(observed()).status == "sent"
    assert calls == ["slack"]
    with closing(connect(database(runtime, "slack"))) as db:
        plans = [db.execute("EXPLAIN QUERY PLAN " + query, params).fetchall()
                 for query, params in (
            ("SELECT 1 FROM retention WHERE fingerprint=? AND provider=?",
             (observed().event_fingerprint(), "slack")),
            ("SELECT 1 FROM boundaries WHERE thread_id=? AND scope=? AND provider=?",
             ("observer:" + sha256_text(observed().workspace_id),
              sha256_text(observed().workspace_id), "slack")),
        )]
    assert all("SEARCH" in row[3] and "SCAN" not in row[3]
               for plan in plans for row in plan)
    return dict(sql=len(sql), vm=vm[0], connections=len(connections),
                point_opens=len(opened), plans=plans)


@pytest.mark.parametrize("history", [0, 1000, 10000, 100000])
def test_first_entry_and_existing_namespace_refusal_have_history_independent_work(
        tmp_path, monkeypatch, capsys, history):
    baseline, selected = tmp_path / "baseline", tmp_path / "selected"
    selected.mkdir()
    with sqlite3.connect(selected / "unrelated-history.sqlite3") as db:
        db.execute("CREATE TABLE history(id INTEGER PRIMARY KEY, disposition TEXT NOT NULL)")
        db.executemany("INSERT INTO history VALUES(?,'uncertain')", ((i,) for i in range(history)))
    baseline.mkdir()
    small = measured_first_entry(baseline, monkeypatch)
    large = measured_first_entry(selected, monkeypatch)
    assert large == small

    # An existing legacy namespace is not imported even when its file has H entries.
    legacy = tmp_path / "legacy"
    (legacy / "notifications").mkdir(parents=True)
    path = legacy / "notifications/dual-deliveries.json"
    raw = json.dumps(dict(schema_version=1, events={
        sha256_text("old-" + str(i)): dict(slack="uncertain") for i in range(history)})).encode()
    path.write_bytes(raw)
    calls, accesses = [], []
    sender = notifier(legacy, "slack", [NOW], calls)
    path_open = Path.open

    def no_legacy_read(item, *args, **kwargs):
        accesses.append(item.name)
        assert item != path, "typed current effect opened legacy H-sized file"
        return path_open(item, *args, **kwargs)

    with monkeypatch.context() as patch:
        no_history(patch)
        patch.setattr(Path, "open", no_legacy_read)
        assert sender.notify(observed()).status != "sent"
    assert calls == [] and path.read_bytes() == raw
    assert not (legacy / "notifications" / root.READY).exists()
    with capsys.disabled():
        print("C348_FRESH_ENTRY_COUNTS", json.dumps(dict(history=history,
            baseline=small, selected=large, legacy_history_reads=0,
            legacy_point_opens=len(accesses)), sort_keys=True))
