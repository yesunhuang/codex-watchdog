from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from codex_watchdog import cli
from codex_watchdog import notification_receipts as storage
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.storage import FileLock, InstructionStore, StoreBusyError


EVENT = NotificationEvent("fixture", "stopped", "indexed-dual", "Fixture", "Fixture")
FP = EVENT.event_fingerprint()


def fingerprint(index):
    return hashlib.sha256(("history-" + str(index)).encode()).hexdigest()


def legacy(runtime, events):
    path = runtime / "notifications/dual-deliveries.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    InstructionStore._atomic_json(path, {"schema_version": 1, "events": events})
    return path


@contextmanager
def opened(runtime, **kwargs):
    with FileLock(runtime / "locks/notifications.lock"):
        with storage.DualDeliveryReceipts(runtime, **kwargs) as store:
            yield store


def notifier(runtime, *, failure=None):
    config = NotificationConfig(interactive_transport="both",
        slack_bot_token="xoxb-fixture", slack_channel_id="C12345678",
        lark=LarkConfig("cli_fixture000001", "fixture", "oc_fixture000001",
                        ("ou_fixture000001",), "feishu"))
    instance = EnvironmentNotifier(runtime, config)
    calls = []
    def send(provider, event):
        calls.append(provider)
        if provider == failure:
            raise TimeoutError("synthetic provider timeout")
    instance._send_slack = lambda event: send("slack", event)
    instance._send_lark = lambda event: send("lark", event)
    return instance, calls


def rows(runtime):
    with sqlite3.connect(runtime / "notifications/dual-deliveries.sqlite3") as db:
        return dict((fp, {provider: state for provider, state in (("slack", s), ("lark", l))
                          if state is not None})
                    for fp, s, l in db.execute("SELECT fingerprint,slack,lark FROM receipts"))


def test_legacy_import_preserves_every_state_and_exact_snapshot(tmp_path):
    events = {fingerprint(1): {}, fingerprint(2): {"slack": "uncertain"},
              fingerprint(3): {"lark": "sent"}, fingerprint(4): {"slack": "sent", "lark": "uncertain"}}
    path = legacy(tmp_path, events)
    original = path.read_bytes()
    with opened(tmp_path) as store:
        assert all(store.get(fp) == entry for fp, entry in events.items())
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    marker = path.read_bytes()
    backups = list(path.parent.glob(path.name + ".backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    with opened(tmp_path):
        pass
    assert path.read_bytes() == marker and list(path.parent.glob(path.name + ".backup-*")) == backups
    assert rows(tmp_path) == events


@pytest.mark.parametrize("bad", [None, [], {"schema_version": True, "events": {}},
    {"schema_version": 3, "events": {}}, {"schema_version": 1, "events": {}, "extra": 1},
    {"schema_version": 1, "events": {"bad": {}}},
    {"schema_version": 1, "events": {fingerprint(1): {"slack": "claimed"}}},
    {"schema_version": 1, "events": {fingerprint(1): {"onebot": "sent"}}},
    {"schema_version": 1, "events": {fingerprint(1): []}}])
def test_invalid_legacy_is_preserved_without_creating_store_or_sending(tmp_path, bad):
    path = tmp_path / "notifications/dual-deliveries.json"
    path.parent.mkdir()
    path.write_text(json.dumps(bad))
    original = path.read_bytes()
    instance, calls = notifier(tmp_path)
    with pytest.raises(storage.NotificationReceiptError):
        instance.notify(EVENT)
    assert path.read_bytes() == original and not calls
    assert not (path.parent / "dual-deliveries.sqlite3").exists()


@pytest.mark.parametrize("invalid", ["{", "not-json", "\ufeff{}"])
def test_invalid_json_is_preserved(tmp_path, invalid):
    path = tmp_path / "notifications/dual-deliveries.json"
    path.parent.mkdir()
    original = invalid.encode("utf-8")
    path.write_bytes(original)
    with pytest.raises(storage.NotificationReceiptError):
        with opened(tmp_path):
            pass
    assert path.read_bytes() == original


def test_marker_missing_database_never_creates_empty_replacement(tmp_path):
    path = legacy(tmp_path, {})
    InstructionStore._atomic_json(path, {"schema_version": 2, "storage": "dual-deliveries.sqlite3"})
    original = path.read_bytes()
    with pytest.raises(storage.NotificationReceiptError, match="database_missing"):
        with opened(tmp_path):
            pass
    assert path.read_bytes() == original and not (path.parent / "dual-deliveries.sqlite3").exists()


def test_future_database_version_preserves_files(tmp_path):
    with opened(tmp_path):
        pass
    path = tmp_path / "notifications/dual-deliveries.json"
    database = path.with_suffix(".sqlite3")
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version=9")
    before = (path.read_bytes(), database.read_bytes())
    with pytest.raises(storage.NotificationReceiptError, match="schema_invalid"):
        with opened(tmp_path):
            pass
    assert (path.read_bytes(), database.read_bytes()) == before


def test_removed_active_marker_refuses_reconstruction(tmp_path):
    with opened(tmp_path) as store:
        assert store.claim(FP, "slack")
    path = tmp_path / "notifications/dual-deliveries.json"
    path.unlink()  # Owned fixture simulates externally missing marker.
    with pytest.raises(storage.NotificationReceiptError, match="marker_missing"):
        with opened(tmp_path):
            pass
    assert rows(tmp_path)[FP] == {"slack": "uncertain"} and not path.exists()


@pytest.mark.parametrize("stage", ["backup", "import_commit", "marker_before_write", "marker_after_write", "activate_commit"])
def test_migration_failure_recovers_without_losing_history_or_sending(tmp_path, monkeypatch, stage):
    path = legacy(tmp_path, {fingerprint(1): {"slack": "sent", "lark": "uncertain"}})
    original = path.read_bytes()
    original_commit = storage.DualDeliveryReceipts._commit
    original_snapshot = storage.snapshot_state
    triggered = []
    commits = []
    def snapshot(target, value):
        if stage == "backup" and not triggered:
            triggered.append(True)
            raise OSError("synthetic backup failure")
        return original_snapshot(target, value)
    def commit(store):
        commits.append(True)
        target = 1 if stage == "import_commit" else 2
        if stage in ("import_commit", "activate_commit") and len(commits) == target and not triggered:
            triggered.append(True)
            raise OSError("synthetic commit failure")
        original_commit(store)
    def writer(target, value):
        if stage == "marker_before_write" and not triggered:
            triggered.append(True)
            raise OSError("synthetic marker failure")
        InstructionStore._atomic_json(target, value)
        if stage == "marker_after_write" and not triggered:
            triggered.append(True)
            raise OSError("synthetic crash after marker")
    monkeypatch.setattr(storage, "snapshot_state", snapshot)
    monkeypatch.setattr(storage.DualDeliveryReceipts, "_commit", commit)
    instance, calls = notifier(tmp_path)
    instance.atomic_writer = writer
    with pytest.raises(OSError):
        instance.notify(EVENT)
    assert triggered and not calls
    monkeypatch.setattr(storage, "snapshot_state", original_snapshot)
    monkeypatch.setattr(storage.DualDeliveryReceipts, "_commit", original_commit)
    with opened(tmp_path) as store:
        assert store.get(fingerprint(1)) == {"slack": "sent", "lark": "uncertain"}
    assert any(p.read_bytes() == original for p in path.parent.glob(path.name + ".backup-*"))
    assert rows(tmp_path) == {fingerprint(1): {"slack": "sent", "lark": "uncertain"}}


def test_fresh_creation_commit_before_marker_recovers(tmp_path):
    def failed_marker(*args):
        raise OSError("synthetic marker failure")
    instance, calls = notifier(tmp_path)
    instance.atomic_writer = failed_marker
    with pytest.raises(OSError):
        instance.notify(EVENT)
    assert not calls and not (tmp_path / "notifications/dual-deliveries.json").exists()
    with opened(tmp_path) as store:
        assert store.get(FP) == {} and store.claim(FP, "slack")


@pytest.mark.parametrize("provider", ["slack", "lark"])
@pytest.mark.parametrize("boundary", ["before_claim_commit", "after_claim_commit", "before_confirmation_commit", "after_confirmation_commit"])
def test_claim_and_confirmation_crashes_never_replay_attempted_provider(tmp_path, monkeypatch, provider, boundary):
    with opened(tmp_path):
        pass
    commit = storage.DualDeliveryReceipts._commit
    triggered = []
    target_state = "uncertain" if "claim" in boundary else "sent"
    def interrupted(store):
        if not triggered and store.get(FP).get(provider) == target_state:
            triggered.append(True)
            if boundary.startswith("after_"):
                commit(store)
            raise OSError("synthetic receipt commit failure")
        commit(store)
    monkeypatch.setattr(storage.DualDeliveryReceipts, "_commit", interrupted)
    instance, calls = notifier(tmp_path)
    with pytest.raises(OSError):
        instance.notify(EVENT)
    assert triggered
    attempted = list(calls)
    monkeypatch.setattr(storage.DualDeliveryReceipts, "_commit", commit)
    restarted, repeated = notifier(tmp_path)
    restarted.notify(EVENT)
    assert all(p not in repeated for p in attempted)
    if boundary == "before_claim_commit":
        assert provider not in attempted and provider in repeated
    elif boundary == "after_claim_commit":
        assert provider not in attempted and provider not in repeated
    else:
        assert provider in attempted and provider not in repeated


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_send_occurs_only_after_committed_claim_and_without_open_transaction(tmp_path, monkeypatch, provider):
    connection = sqlite3.connect
    connections = []
    def capture(*args, **kwargs):
        db = connection(*args, **kwargs)
        connections.append(db)
        return db
    monkeypatch.setattr(storage.sqlite3, "connect", capture)
    instance, calls = notifier(tmp_path)
    def inspect_send(event):
        db = connections[-1]
        assert not db.in_transaction
        assert db.execute("SELECT " + provider + " FROM receipts WHERE fingerprint=?", (FP,)).fetchone()[0] == "uncertain"
        calls.append(provider)
    setattr(instance, "_send_" + provider, inspect_send)
    instance.notify(EVENT)
    assert calls == ["slack", "lark"]


def test_duplicate_claim_and_confirmation_cannot_regress_sent(tmp_path):
    with opened(tmp_path) as store:
        assert store.claim(FP, "slack") and not store.claim(FP, "slack")
        store.confirm_sent(FP, "slack")
        assert not store.claim(FP, "slack") and store.get(FP) == {"slack": "sent"}
        with pytest.raises(storage.NotificationReceiptError, match="confirmation_invalid"):
            store.confirm_sent(FP, "slack")
    assert rows(tmp_path)[FP] == {"slack": "sent"}


def test_competing_notification_owners_cannot_send_same_event_twice(tmp_path):
    first, calls = notifier(tmp_path)
    entered, release = threading.Event(), threading.Event()
    failures = []
    def blocking_send(event):
        calls.append("slack")
        entered.set()
        assert release.wait(5)
    first._send_slack = blocking_send
    def run():
        try:
            first.notify(EVENT)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        competitor, repeated = notifier(tmp_path)
        with pytest.raises(StoreBusyError):
            competitor.notify(EVENT)
        assert not repeated
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not failures and calls == ["slack", "lark"]
    assert competitor.notify(EVENT).duplicate and not repeated


def test_export_contains_latest_receipts_not_only_original_snapshot(tmp_path):
    initial = {fingerprint(1): {"slack": "sent"}, fingerprint(2): {}}
    path = legacy(tmp_path, initial)
    original = path.read_bytes()
    with opened(tmp_path) as store:
        store.claim(FP, "slack")
        store.confirm_sent(FP, "slack")
        store.claim(FP, "lark")
    complete = {**initial, FP: {"slack": "sent", "lark": "uncertain"}}
    result = storage.export_legacy(tmp_path)
    assert result["status"] == "exported" and result["receipt_count"] == 3
    assert json.loads(path.read_text()) == {"schema_version": 1, "events": complete}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result["receipt_sha256"]
    assert rows(tmp_path) == complete
    assert any(p.read_bytes() == original for p in path.parent.glob(path.name + ".backup-*"))
    # An older sender's legitimate new events are retained on re-upgrade.
    exported = path.read_bytes()
    complete[fingerprint(3)] = {"slack": "uncertain", "lark": "sent"}
    legacy(tmp_path, complete)
    older_additions = path.read_bytes()
    with opened(tmp_path) as store:
        assert store.get(FP) == {"slack": "sent", "lark": "uncertain"}
    assert rows(tmp_path) == complete
    assert any(p.read_bytes() == older_additions for p in path.parent.glob(path.name + ".backup-*"))
    # Repeat export is deterministic and keeps all new receipts, not old bytes.
    storage.export_legacy(tmp_path)
    final = path.read_bytes()
    storage.export_legacy(tmp_path)
    assert path.read_bytes() == final and final != exported


@pytest.mark.parametrize("lost", ["event", "provider", "sent_downgrade"])
def test_restoring_stale_or_downgraded_legacy_state_is_rejected(tmp_path, lost):
    path = legacy(tmp_path, {})
    with opened(tmp_path) as store:
        store.claim(FP, "slack")
        store.confirm_sent(FP, "slack")
        store.claim(FP, "lark")
    storage.export_legacy(tmp_path)
    entries = {FP: {"slack": "sent", "lark": "uncertain"}}
    if lost == "event": entries = {}
    elif lost == "provider": entries[FP].pop("lark")
    else: entries[FP]["slack"] = "uncertain"
    legacy(tmp_path, entries)
    before = path.read_bytes()
    with pytest.raises(storage.NotificationReceiptError, match="reconcile_loss"):
        with opened(tmp_path):
            pass
    assert path.read_bytes() == before and rows(tmp_path)[FP] == {"slack": "sent", "lark": "uncertain"}


def test_unknown_marker_fields_survive_export_and_reupgrade(tmp_path):
    with opened(tmp_path):
        pass
    path = tmp_path / "notifications/dual-deliveries.json"
    marker = json.loads(path.read_text())
    marker["future_note"] = {"preserve": True}
    InstructionStore._atomic_json(path, marker)
    original = path.read_bytes()
    with opened(tmp_path):
        pass
    assert path.read_bytes() == original
    storage.export_legacy(tmp_path)
    with opened(tmp_path):
        pass
    assert json.loads(path.read_text()) == marker
    assert any(p.read_bytes() == original for p in path.parent.glob(path.name + ".backup-*"))


@pytest.mark.parametrize("stage", ["snapshot", "metadata_commit", "before_export_write", "after_export_write"])
def test_export_failure_keeps_complete_receipts_and_recovers(tmp_path, monkeypatch, stage):
    with opened(tmp_path) as store:
        store.claim(FP, "slack")
        store.confirm_sent(FP, "slack")
        store.claim(FP, "lark")
    prior = rows(tmp_path)
    commit = storage.DualDeliveryReceipts._commit
    snapshot = storage.snapshot_state
    def failed_commit(store):
        raise OSError("synthetic export commit failure")
    def failed_snapshot(*args):
        raise OSError("synthetic export snapshot failure")
    if stage == "snapshot": monkeypatch.setattr(storage, "snapshot_state", failed_snapshot)
    if stage == "metadata_commit": monkeypatch.setattr(storage.DualDeliveryReceipts, "_commit", failed_commit)
    def writer(path, value):
        if stage == "before_export_write": raise OSError("synthetic export failure")
        InstructionStore._atomic_json(path, value)
        if stage == "after_export_write": raise OSError("synthetic crash after export")
    with pytest.raises(OSError):
        storage.export_legacy(tmp_path, atomic_writer=writer)
    assert rows(tmp_path) == prior
    monkeypatch.setattr(storage.DualDeliveryReceipts, "_commit", commit)
    monkeypatch.setattr(storage, "snapshot_state", snapshot)
    with opened(tmp_path) as store:
        assert store.get(FP) == prior[FP]
    storage.export_legacy(tmp_path)
    assert json.loads((tmp_path / "notifications/dual-deliveries.json").read_text())["events"] == prior


def test_export_command_loads_no_provider_config_credentials_or_dispatcher(tmp_path, monkeypatch, capsys):
    with opened(tmp_path) as store:
        store.claim(FP, "slack")
    def forbidden(*args, **kwargs):
        raise AssertionError("export must not initialize provider/settings/dispatcher")
    monkeypatch.setattr("codex_watchdog.messaging_setup.prepare_launch", forbidden)
    monkeypatch.setattr(cli, "EnvironmentNotifier", forbidden)
    monkeypatch.setattr(cli, "QueueWakeDispatcher", forbidden)
    monkeypatch.setattr(NotificationConfig, "from_environment", forbidden)
    assert cli.main(["--runtime", str(tmp_path), "notification-receipts-export"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "exported" and output["receipt_count"] == 1
    assert "runtime" not in output


@pytest.mark.parametrize("lock_name,reason", [("foreground-run.lock", "foreground_run_lock_held"),
                                               ("notifications.lock", "notification_receipts_lock_held")])
def test_export_command_refuses_active_runtime_or_notification_owner(tmp_path, capsys, lock_name, reason):
    with opened(tmp_path) as store:
        store.claim(FP, "slack")
    path = tmp_path / "notifications/dual-deliveries.json"
    before = path.read_bytes()
    with FileLock(tmp_path / "locks" / lock_name):
        assert cli.main(["--runtime", str(tmp_path), "notification-receipts-export"]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "export_failed", "reason": reason}
    assert path.read_bytes() == before


def test_export_command_fixed_error_does_not_echo_private_exception(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("synthetic-private-detail")
    monkeypatch.setattr(storage, "export_legacy", fail)
    assert cli.main(["--runtime", str(tmp_path), "notification-receipts-export"]) == 1
    output = capsys.readouterr().out
    assert "synthetic-private-detail" not in output
    assert json.loads(output) == {"status": "export_failed", "reason": "notification_receipts_unavailable"}


def test_export_no_receipts_and_original_legacy_do_not_force_migration(tmp_path):
    assert storage.export_legacy(tmp_path)["status"] == "no_receipts"
    path = legacy(tmp_path, {FP: {"slack": "uncertain"}})
    before = path.read_bytes()
    assert storage.export_legacy(tmp_path)["status"] == "already_legacy"
    assert path.read_bytes() == before and not (path.parent / "dual-deliveries.sqlite3").exists()


@pytest.mark.parametrize("count", [0, 1000, 10000, 100000])
def test_history_growth_keeps_hot_path_point_queries_and_bounded_payloads(tmp_path, monkeypatch, count):
    entries = {fingerprint(n): {"slack": "sent", "lark": "uncertain"} for n in range(count)}
    path = legacy(tmp_path, entries)
    started = time.perf_counter_ns()
    with opened(tmp_path) as store:
        plan = store.db.execute("EXPLAIN QUERY PLAN SELECT slack,lark FROM receipts WHERE fingerprint=?", (FP,)).fetchall()
    migration_ns = time.perf_counter_ns() - started
    assert "SEARCH" in str(plan) and "INDEX" in str(plan) and "SCAN" not in str(plan)
    connect = sqlite3.connect
    trace = []
    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(trace.append)
        return db
    marker_reads = []
    read_bytes = Path.read_bytes
    def measured_read(target):
        value = read_bytes(target)
        if target == path: marker_reads.append(len(value))
        return value
    writes = []
    def measured_write(target, value):
        writes.append(len(storage._payload(value)))
        InstructionStore._atomic_json(target, value)
    monkeypatch.setattr(storage.sqlite3, "connect", traced)
    monkeypatch.setattr(Path, "read_bytes", measured_read)
    instance, calls = notifier(tmp_path)
    instance.atomic_writer = measured_write
    started = time.perf_counter_ns()
    result = instance.notify(EVENT)
    elapsed_ns = time.perf_counter_ns() - started
    assert result.status == "sent" and calls == ["slack", "lark"]
    receipt_selects = [sql for sql in trace if sql.startswith("SELECT") and " FROM receipts" in sql]
    assert len(receipt_selects) == 1 and "WHERE fingerprint=" in receipt_selects[0]
    assert len(trace) <= 24 and len(marker_reads) == 1 and marker_reads[0] < 256
    assert len(writes) == 1 and writes[0] < 300
    assert not any("SELECT fingerprint,slack,lark FROM receipts" in sql for sql in trace)
    print(json.dumps({"dual_receipt_growth": {"H": count, "sql_statements": len(trace),
        "receipt_point_selects": len(receipt_selects), "marker_bytes_read": sum(marker_reads),
        "outer_state_serialized_bytes": writes, "migration_ns": migration_ns,
        "instrumented_notify_ns": elapsed_ns, "query_plan": plan}}, sort_keys=True))
