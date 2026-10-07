"""Offline notification portability uses real JSON/SQLite receipt stores."""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from codex_watchdog import relay_notification_migration as migration
from codex_watchdog.notification_receipts import DualDeliveryReceipts
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.storage import InstructionStore


def fp(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def write(runtime, name, value):
    path = runtime / "notifications" / name
    InstructionStore._atomic_json(path, value)
    return path


def last(runtime, events, **unknown):
    return write(runtime, "last-events.json", dict(schema_version=1, last_events=events, **unknown))


def dual(runtime, events):
    return write(runtime, "dual-deliveries.json", dict(schema_version=1, events=events))


def entry(fingerprint, **unknown):
    return dict(fingerprint=fingerprint, recorded_at="2026-10-07T00:00:00Z", **unknown)


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def native_symlink(link, target, *, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) in (50, 1314):
            pytest.skip("native Windows symlink capability unavailable (WinError {})".format(exc.winerror))
        raise


def event(value):
    return NotificationEvent("exact-session-fixture", "stopped", str(value), "Fixture", "Fixture")


def test_three_node_histories_preserve_conflicts_unknown_evidence_and_every_attempt(tmp_path):
    first, second, third = (tmp_path / node for node in ("login6", "login3", "login4"))
    shared = tmp_path / "shared"
    key, unambiguous = fp("same-key"), fp("other-key")
    last(first, {key: entry(fp(1), future={"kept": True}), unambiguous: entry(fp(4))}, vendor_field={"kept": 1})
    last(second, {key: entry(fp(2))})
    last(third, {key: entry(fp(1), recorded_hint="old-node")})
    dual(first, {fp(1): {"slack": "uncertain"}, fp(2): {"lark": "sent"}})
    dual(second, {fp(1): {"slack": "sent", "lark": "uncertain"}, fp(3): {"slack": "uncertain"}})
    dual(third, {fp(1): {"slack": "uncertain"}})
    with DualDeliveryReceipts(third) as receipts:
        assert receipts.claim(fp(5), "lark")
        receipts.db.execute("CREATE TABLE future_evidence(id INTEGER PRIMARY KEY, evidence BLOB)")
        receipts.db.execute("INSERT INTO future_evidence VALUES(1,?)", (b"retained-opaque-evidence",))
    before = [files(runtime) for runtime in (first, second, third)]
    plan = migration.plan_notification_migration((third, second, first, first))
    assert plan["summary"]["source_count"] == 3
    assert plan["summary"]["history_count"] == 4
    assert plan["last_events"] == {"schema_version": 1, "last_events": {}}
    result = migration.apply_notification_migration(shared, (first, second, third), expected_plan_sha256=plan["plan_sha256"])
    assert result["status"] == "migrated" and result["dual_count"] == 4
    assert [files(runtime) for runtime in (first, second, third)] == before
    assert all(migration.previous_notification(shared, fp(index)) for index in (1, 2, 4))
    assert not migration.previous_notification(shared, fp("new"))
    with DualDeliveryReceipts(shared) as receipts:
        assert receipts.get(fp(1)) == {"slack": "sent", "lark": "uncertain"}
        assert receipts.get(fp(2)) == {"lark": "sent"}
        assert receipts.get(fp(3)) == {"slack": "uncertain"}
        assert receipts.get(fp(5)) == {"lark": "uncertain"}
        assert not receipts.claim(fp(1), "slack")
        assert not receipts.claim(fp(1), "lark")
    evidence = shared / "notifications/source-evidence"
    for runtime, snapshot in zip((first, second, third), before):
        retained = evidence / hashlib.sha256(str(runtime).encode()).hexdigest()
        for name in ("last-events.json", "dual-deliveries.json"):
            assert (retained / name).read_bytes() == snapshot["notifications/" + name]
    third_copy = evidence / hashlib.sha256(str(third).encode()).hexdigest() / "dual-deliveries.sqlite3"
    with sqlite3.connect(third_copy) as db:
        assert db.execute("SELECT evidence FROM future_evidence").fetchone() == (b"retained-opaque-evidence",)


def test_generator_input_and_restart_are_idempotent_without_rewriting_current_receipts(tmp_path):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key"): entry(fp("old"))})
    dual(source, {fp("attempt"): {"lark": "uncertain"}})
    first = migration.apply_notification_migration(target, (path for path in (source,)))
    with DualDeliveryReceipts(target) as receipts:
        assert receipts.claim(fp("new-live-notification"), "slack")
        receipts.confirm_sent(fp("new-live-notification"), "slack")
    before = files(target)
    second = migration.apply_notification_migration(target, (path for path in (source,)))
    assert first["plan_sha256"] == second["plan_sha256"]
    assert second["status"] == "already_migrated" and files(target) == before


def test_refusal_markers_read_retained_original_without_reopening_old_state(tmp_path):
    source, target = tmp_path / "old", tmp_path / "shared"
    state = last(source, {fp("key"): entry(fp("old"))})
    marker = dual(source, {fp("uncertain"): {"slack": "uncertain"}})
    for path in (state, marker):
        path.with_name(path.name + ".relay-authority-v1-backup").write_bytes(path.read_bytes())
        InstructionStore._atomic_json(path, {"schema_version": 3, "storage": "cluster_provider_journals_v1"})
    before = files(source)
    migration.apply_notification_migration(target, [source])
    assert files(source) == before and migration.previous_notification(target, fp("old"))
    with DualDeliveryReceipts(target) as receipts:
        assert receipts.get(fp("uncertain")) == {"slack": "uncertain"}


def test_legacy_json_and_rollback_database_are_both_preserved_and_frozen(tmp_path):
    source, target = tmp_path / "old", tmp_path / "shared"
    dual(source, {fp("persisted"): {"slack": "sent"}})
    with DualDeliveryReceipts(source) as receipts:
        assert receipts.claim(fp("unknown-attempt"), "lark")
    # A retained new-store claim must survive an incomplete legacy rollback.
    dual(source, {fp("legacy-added"): {"lark": "sent"}})
    plan = migration.plan_notification_migration([source])
    assert plan["sources"][0]["dual_database_sha256"]
    migration.apply_notification_migration(target, [source], expected_plan_sha256=plan["plan_sha256"])
    with DualDeliveryReceipts(target) as receipts:
        assert receipts.get(fp("persisted")) == {"slack": "sent"}
        assert receipts.get(fp("unknown-attempt")) == {"lark": "uncertain"}
        assert receipts.get(fp("legacy-added")) == {"lark": "sent"}


def test_unknown_marker_fields_survive_in_active_metadata_and_snapshot(tmp_path):
    source, target = tmp_path / "old", tmp_path / "shared"
    with DualDeliveryReceipts(source):
        pass
    marker = {"schema_version": 2, "storage": "dual-deliveries.sqlite3", "future": {"receipt-policy": "kept"}}
    write(source, "dual-deliveries.json", marker)
    migration.apply_notification_migration(target, [source])
    assert json.loads((target / "notifications/dual-deliveries.json").read_bytes()) == marker
    with DualDeliveryReceipts(target) as receipts:
        assert json.loads(receipts.db.execute("SELECT marker_value FROM metadata").fetchone()[0]) == marker


def test_conflicting_unknown_marker_fields_fail_closed_without_changing_sources(tmp_path):
    sources = [tmp_path / node for node in ("node1", "node2")]
    target = tmp_path / "shared"
    for index, source in enumerate(sources):
        with DualDeliveryReceipts(source):
            pass
        write(source, "dual-deliveries.json", {"schema_version": 2, "storage": "dual-deliveries.sqlite3", "future": index})
    before = [files(source) for source in sources]
    with pytest.raises(migration.RelayNotificationMigrationError, match="marker_field_conflict"):
        migration.apply_notification_migration(target, sources)
    assert [files(source) for source in sources] == before and not target.exists()


@pytest.mark.parametrize("change", ["last-event", "dual-attempt", "unknown-table"])
def test_frozen_source_mutation_rejects_before_publication(tmp_path, change):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key"): entry(fp("old"))})
    with DualDeliveryReceipts(source):
        pass
    plan = migration.plan_notification_migration([source])
    if change == "last-event":
        last(source, {fp("key"): entry(fp("different"))})
    else:
        with DualDeliveryReceipts(source) as receipts:
            if change == "dual-attempt":
                assert receipts.claim(fp("attempt"), "slack")
            else:
                receipts.db.execute("CREATE TABLE future_evidence(id INTEGER PRIMARY KEY, value TEXT)")
                receipts.db.execute("INSERT INTO future_evidence VALUES(1,'changed')")
    before = files(source)
    with pytest.raises(migration.RelayNotificationMigrationError, match="source_changed"):
        migration.apply_notification_migration(target, [source], expected_plan_sha256=plan["plan_sha256"])
    assert files(source) == before and not target.exists()


def test_source_change_during_snapshot_removes_stage_and_can_resume_offline(tmp_path, monkeypatch):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key"): entry(fp("old"))})
    original = migration._sync_tree
    # Source revalidation occurs before publication, not after the shared
    # ready directory becomes visible to the first new sender.
    calls = []
    plan = migration.plan_notification_migration
    def mutated(runtimes):
        calls.append(True)
        if len(calls) == 2:
            last(source, {fp("key"): entry(fp("changed-during-import"))})
        return plan(runtimes)
    monkeypatch.setattr(migration, "plan_notification_migration", mutated)
    with pytest.raises(migration.RelayNotificationMigrationError, match="source_changed"):
        migration.apply_notification_migration(target, [source])
    assert not (target / "notifications").exists()
    assert not list(target.glob(".relay-notifications-*"))
    monkeypatch.setattr(migration, "plan_notification_migration", plan)
    monkeypatch.setattr(migration, "_sync_tree", original)
    assert migration.apply_notification_migration(target, [source])["status"] == "migrated"


def test_publication_failure_does_not_expose_partial_state_or_lose_evidence(tmp_path, monkeypatch):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key"): entry(fp("old"))})
    dual(source, {fp("attempt"): {"slack": "uncertain"}})
    before = files(source)
    replace = migration.os.replace
    def fail_ready(source_path, target_path):
        if Path(target_path) == target / "notifications":
            raise OSError("synthetic publication failure")
        return replace(source_path, target_path)
    monkeypatch.setattr(migration.os, "replace", fail_ready)
    with pytest.raises(OSError, match="publication failure"):
        migration.apply_notification_migration(target, [source])
    assert files(source) == before and not (target / "notifications").exists()
    assert not list(target.glob(".relay-notifications-*"))
    monkeypatch.setattr(migration.os, "replace", replace)
    assert migration.apply_notification_migration(target, [source])["status"] == "migrated"


@pytest.mark.parametrize("failure", [None, "last-events.json", "relay-migration-history.sqlite3"])
def test_staged_file_flushes_use_writable_handles_and_failure_prevents_readiness(tmp_path, monkeypatch, failure):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key"): entry(fp("old"), future={"retained": True})})
    dual(source, {fp("attempt"): {"slack": "uncertain"}})
    before = files(source)
    real_open, real_fsync = open, migration.os.fsync
    selected, flushed = {}, []
    @contextmanager
    def staged_open(path, mode):
        assert Path(path).is_relative_to(target) and mode == "r+b"
        with real_open(path, mode) as stream:
            selected[stream.fileno()] = (Path(path), stream)
            try:
                yield stream
            finally:
                selected.pop(stream.fileno())
    def writable_flush(descriptor):
        current = selected.get(descriptor)
        if current is not None:
            path, stream = current
            # Reproduces the Windows access boundary without claiming a
            # native Windows FlushFileBuffers call on this test machine.
            assert stream.writable(), "staged fsync requires a writable descriptor"
            flushed.append(path)
            if path.name == failure:
                raise OSError(errno.EBADF, "synthetic staged flush failure")
        return real_fsync(descriptor)
    monkeypatch.setattr(migration, "open", staged_open, raising=False)
    monkeypatch.setattr(migration.os, "fsync", writable_flush)
    if failure is not None:
        with pytest.raises(OSError, match="staged flush failure"):
            migration.apply_notification_migration(target, [source])
        assert not (target / "notifications").exists()
        assert not list(target.glob(".relay-notifications-*"))
    else:
        assert migration.apply_notification_migration(target, [source])["status"] == "migrated"
        evidence = target / "notifications/source-evidence" / hashlib.sha256(str(source).encode()).hexdigest()
        assert (evidence / "last-events.json").read_bytes() == before["notifications/last-events.json"]
        assert (evidence / "dual-deliveries.json").read_bytes() == before["notifications/dual-deliveries.json"]
        assert migration.previous_notification(target, fp("old"))
    assert flushed and files(source) == before


@pytest.mark.parametrize("name", ["last-events.json", "dual-deliveries.json", "dual-deliveries.sqlite3", "relay-migration-history.sqlite3"])
def test_completed_marker_with_missing_store_never_reconstructs_empty_state(tmp_path, name):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key"): entry(fp("old"))})
    dual(source, {fp("attempt"): {"slack": "sent"}})
    migration.apply_notification_migration(target, [source])
    (target / "notifications" / name).unlink()
    before = files(target)
    with pytest.raises(migration.RelayNotificationMigrationError):
        migration.apply_notification_migration(target, [source])
    assert files(target) == before


@pytest.mark.parametrize("name,value", [
    ("last-events.json", {"schema_version": True, "last_events": {}}),
    ("last-events.json", {"schema_version": 2, "last_events": {}}),
    ("last-events.json", {"schema_version": 1, "last_events": {fp("key"): {"fingerprint": "bad", "recorded_at": "time"}}}),
    ("dual-deliveries.json", {"schema_version": True, "events": {}}),
    ("dual-deliveries.json", {"schema_version": 1, "events": {fp("event"): {"slack": "replay"}}}),
    ("dual-deliveries.json", {"schema_version": 1, "events": {fp("event"): {"unknown-provider": "sent"}}}),
    ("dual-deliveries.json", {"schema_version": 2, "storage": "dual-deliveries.sqlite3"}),
    ("dual-deliveries.json", {"schema_version": 3, "storage": "relay-authority-v1"}),
])
def test_malformed_or_future_source_is_retained_without_empty_replacement(tmp_path, name, value):
    source, target = tmp_path / "old", tmp_path / "shared"
    write(source, name, value)
    before = files(source)
    with pytest.raises(migration.RelayNotificationMigrationError):
        migration.apply_notification_migration(target, [source])
    assert files(source) == before and not target.exists()


@pytest.mark.parametrize("symlink", ["runtime", "ancestor", "directory", "state", "target"])
def test_symlinked_runtime_state_or_target_is_not_used(tmp_path, symlink):
    source, target = tmp_path / "old", tmp_path / "shared"
    state = last(source, {fp("key"): entry(fp("old"))})
    if symlink == "runtime":
        alias = tmp_path / "old-alias"
        native_symlink(alias, source, target_is_directory=True)
        source = alias
    elif symlink == "ancestor":
        alias = tmp_path / "ancestor-alias"
        native_symlink(alias, tmp_path, target_is_directory=True)
        source = alias / source.name
    elif symlink == "directory":
        moved = tmp_path / "elsewhere"
        state.parent.rename(moved)
        native_symlink(source / "notifications", moved, target_is_directory=True)
    elif symlink == "state":
        moved = tmp_path / "elsewhere.json"
        state.rename(moved)
        native_symlink(state, moved)
    else:
        real = tmp_path / "real-target"
        real.mkdir()
        native_symlink(target, real, target_is_directory=True)
    before = files(tmp_path)
    with pytest.raises(migration.RelayNotificationMigrationError, match="path_invalid"):
        migration.apply_notification_migration(target, [source])
    assert files(tmp_path) == before


def test_unrecognized_future_schema_cannot_be_downgraded_through_a_backup(tmp_path):
    source, target = tmp_path / "old", tmp_path / "shared"
    state = last(source, {fp("key"): entry(fp("old"))})
    state.with_name(state.name + ".relay-authority-v1-backup").write_bytes(state.read_bytes())
    InstructionStore._atomic_json(state, {"schema_version": 3, "storage": "future-format"})
    before = files(source)
    with pytest.raises(migration.RelayNotificationMigrationError, match="last_events_invalid"):
        migration.apply_notification_migration(target, [source])
    assert files(source) == before and not target.exists()


def test_history_query_is_indexed_and_work_is_independent_of_closed_history(tmp_path, monkeypatch):
    runtimes = [tmp_path / "small", tmp_path / "large"]
    targets = [tmp_path / "small-shared", tmp_path / "large-shared"]
    for count, runtime, target in zip((1, 10000), runtimes, targets):
        last(runtime, {fp("key-" + str(index)): entry(fp(index)) for index in range(count)})
        migration.apply_notification_migration(target, [runtime])
        with sqlite3.connect(target / "notifications/relay-migration-history.sqlite3") as db:
            query_plan = db.execute("EXPLAIN QUERY PLAN SELECT 1 FROM history INDEXED BY notification_history_lookup "
                "WHERE fingerprint=? LIMIT 1", (fp(0),)).fetchall()
            assert all("SEARCH" in row[3] and "notification_history_lookup" in row[3] for row in query_plan)
    connect = migration.sqlite3.connect
    operations = []
    def measured(*args, **kwargs):
        db = connect(*args, **kwargs)
        count = [0]
        operations.append(count)
        def step():
            count[0] += 1
            return 0
        db.set_progress_handler(step, 1)
        return db
    monkeypatch.setattr(migration.sqlite3, "connect", measured)
    for target in targets:
        assert migration.previous_notification(target, fp(0))
        assert not migration.previous_notification(target, fp("not-in-history"))
    assert max(count[0] for count in operations) < 80
    assert operations[0][0] == operations[2][0] and operations[1][0] == operations[3][0]


@pytest.mark.parametrize("damage", ["history-schema", "history-index", "receipt", "orphan-history"])
def test_missing_or_damaged_migration_history_fails_closed_without_rebuilding(tmp_path, damage):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key"): entry(fp("old"))})
    migration.apply_notification_migration(target, [source])
    if damage in ("history-schema", "history-index"):
        with sqlite3.connect(target / "notifications/relay-migration-history.sqlite3") as db:
            db.execute("PRAGMA user_version=2" if damage == "history-schema" else "DROP INDEX notification_history_lookup")
    elif damage == "receipt":
        write(target, "relay-migration.json", {"schema_version": True, "state": "ready", "storage": "relay_notification_history_v1"})
    else:
        (target / "notifications/relay-migration.json").unlink()
    before = files(target)
    with pytest.raises(migration.RelayNotificationMigrationError):
        migration.previous_notification(target, fp("old"))
    assert files(target) == before


def test_target_with_existing_state_is_preserved_without_overlay(tmp_path):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key"): entry(fp("old"))})
    last(target, {fp("another-session"): entry(fp("current"))})
    before = files(target)
    with pytest.raises(migration.RelayNotificationMigrationError, match="target_not_empty"):
        migration.apply_notification_migration(target, [source])
    assert files(target) == before


def test_empty_history_has_no_receipt_and_never_creates_a_store_during_lookup(tmp_path):
    target = tmp_path / "unused-runtime"
    assert not migration.previous_notification(target, fp("new"))
    assert not target.exists()
    with pytest.raises(migration.RelayNotificationMigrationError, match="fingerprint_invalid"):
        migration.previous_notification(target, "invalid")
    assert not target.exists()


def test_ready_validation_is_read_only_and_rejects_missing_or_unactivated_metadata(tmp_path):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key"): entry(fp("old"))})
    result = migration.apply_notification_migration(target, [source])
    before = files(target)
    assert migration.validate_notification_migration(target, result["plan_sha256"]) == {
        key: result[key] for key in ("plan_sha256", "source_count", "history_count", "dual_count")}
    assert files(target) == before
    with pytest.raises(migration.RelayNotificationMigrationError, match="existing_migration_conflict"):
        migration.validate_notification_migration(target, fp("other-plan"))
    assert files(target) == before
    with sqlite3.connect(target / "notifications/dual-deliveries.sqlite3") as db:
        db.execute("UPDATE metadata SET activated=0")
    incomplete = files(target)
    with pytest.raises(migration.RelayNotificationMigrationError, match="dual_database_invalid"):
        migration.validate_notification_migration(target, result["plan_sha256"])
    assert files(target) == incomplete


def test_ready_validation_and_notifier_do_not_scan_imported_notification_history(tmp_path, monkeypatch):
    source, target = tmp_path / "old", tmp_path / "shared"
    last(source, {fp("key-" + str(index)): entry(fp(index)) for index in range(10000)})
    dual(source, {fp("dual-" + str(index)): {"slack": "sent"} for index in range(10000)})
    result = migration.apply_notification_migration(target, [source])
    state = json.loads((target / "notifications/last-events.json").read_bytes())
    assert state == {"schema_version": 1, "last_events": {}}
    connect = migration.sqlite3.connect
    operations, sql = [], []
    def measured(*args, **kwargs):
        db = connect(*args, **kwargs)
        count = [0]
        operations.append(count)
        def step():
            count[0] += 1
            return 0
        db.set_progress_handler(step, 1)
        db.set_trace_callback(sql.append)
        return db
    monkeypatch.setattr(migration.sqlite3, "connect", measured)
    assert migration.validate_notification_migration(target, result["plan_sha256"])["history_count"] == 10000
    assert max(count[0] for count in operations) < 200
    assert not any("FROM history" in statement and "WHERE fingerprint=" not in statement for statement in sql)
    assert not any("FROM receipts" in statement and "WHERE fingerprint=" not in statement for statement in sql)
    notifier = EnvironmentNotifier(tmp_path / "controller", NotificationConfig(interactive_transport="slack",
        slack_bot_token="xoxb-fixture", slack_channel_id="C12345678"))
    notifier.relay_runtime, notifier.state_path = target, target / "notifications/last-events.json"
    calls = []
    notifier._send_slack = lambda evt: calls.append(evt.event_fingerprint())
    fresh = event("new-after-10000-historical-events")
    assert notifier.notify(fresh).status == "sent"
    assert calls == [fresh.event_fingerprint()]
    after = json.loads(notifier.state_path.read_bytes())
    assert len(after["last_events"]) == 1 and after["last_events"][fresh.dedupe_key()]["fingerprint"] == fresh.event_fingerprint()


def test_single_slack_notifier_uses_conflicting_imported_history_after_further_notifications(tmp_path):
    first, second, target = (tmp_path / node for node in ("login6", "login3", "shared"))
    old1, old2, fresh = (event(value) for value in ("old1", "old2", "new"))
    last(first, {old1.dedupe_key(): entry(old1.event_fingerprint())})
    last(second, {old2.dedupe_key(): entry(old2.event_fingerprint())})
    migration.apply_notification_migration(target, [first, second])
    config = NotificationConfig(interactive_transport="slack", slack_bot_token="xoxb-fixture", slack_channel_id="C12345678")
    calls = []
    for node, events in (("new-controller", (old1, fresh, old2)), ("third-controller", (old1, old2, fresh))):
        notifier = EnvironmentNotifier(tmp_path / node, config)
        notifier.relay_runtime, notifier.state_path = target, target / "notifications/last-events.json"
        notifier._send_slack = lambda evt: calls.append(evt.event_fingerprint())
        for evt in events:
            result = notifier.notify(evt)
            assert result.status in ("sent", "suppressed")
            if evt in (old1, old2):
                assert result.duplicate and result.status == "suppressed"
    assert calls == [fresh.event_fingerprint()]


@pytest.mark.parametrize("provider", ["slack", "lark"])
@pytest.mark.parametrize("state", ["sent", "uncertain"])
def test_single_provider_delivery_retains_prior_dual_claims_without_retry(tmp_path, provider, state):
    source, target = tmp_path / "old", tmp_path / "shared"
    old = event("previous-dual-attempt")
    dual(source, {old.event_fingerprint(): {provider: state}})
    migration.apply_notification_migration(target, [source])
    config = NotificationConfig(interactive_transport=provider,
        slack_bot_token="xoxb-fixture", slack_channel_id="C12345678",
        lark=LarkConfig("cli_fixture000001", "fixture", "oc_fixture000001", ("ou_fixture000001",), "feishu"))
    notifier = EnvironmentNotifier(tmp_path / "controller", config)
    notifier.relay_runtime, notifier.state_path = target, target / "notifications/last-events.json"
    calls = []
    notifier._send_slack = lambda evt: calls.append("slack")
    notifier._send_lark = lambda evt: calls.append("lark")
    result = notifier.notify(old)
    assert not calls
    if state == "uncertain":
        assert result.status == "delivery_failed" and not result.state_persisted
    else:
        assert result.status in ("sent", "suppressed")
    with DualDeliveryReceipts(target) as receipts:
        assert receipts.get(old.event_fingerprint()) == {provider: state}


def test_dual_receipts_prevent_resends_while_allowing_the_missing_provider_only(tmp_path):
    source, target = tmp_path / "old", tmp_path / "shared"
    old, partial = event("old"), event("partial")
    dual(source, {old.event_fingerprint(): {"slack": "sent", "lark": "uncertain"},
                  partial.event_fingerprint(): {"slack": "sent"}})
    migration.apply_notification_migration(target, [source])
    config = NotificationConfig(interactive_transport="both", slack_bot_token="xoxb-fixture", slack_channel_id="C12345678",
        lark=LarkConfig("cli_fixture000001", "fixture", "oc_fixture000001", ("ou_fixture000001",), "feishu"))
    notifier = EnvironmentNotifier(tmp_path / "controller", config)
    notifier.relay_runtime = target
    notifier.state_path = target / "notifications/last-events.json"
    calls = []
    notifier._send_slack = lambda evt: calls.append(("slack", evt.event_fingerprint()))
    notifier._send_lark = lambda evt: calls.append(("lark", evt.event_fingerprint()))
    assert notifier.notify(old).status == "delivery_failed"
    assert not calls
    assert notifier.notify(partial).status == "sent"
    assert calls == [("lark", partial.event_fingerprint())]
    restarted = EnvironmentNotifier(tmp_path / "new-controller", config)
    restarted.relay_runtime, restarted.state_path = target, notifier.state_path
    restarted._send_slack = lambda evt: calls.append(("slack", evt.event_fingerprint()))
    restarted._send_lark = lambda evt: calls.append(("lark", evt.event_fingerprint()))
    assert restarted.notify(partial).status == "suppressed"
    assert restarted.notify(old).status == "delivery_failed"
    assert calls == [("lark", partial.event_fingerprint())]
