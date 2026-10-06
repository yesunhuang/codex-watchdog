import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from codex_watchdog.control_state import ControlBusy, ControlError, ControlStore, control_atomic_json
from codex_watchdog.notification_attempts import local_attempt
from codex_watchdog.notification_receipts import DualDeliveryReceipts
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent, NotificationResult
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.onebot_transport import OneBotConfig
from codex_watchdog.remote_control import notify_local_control, reconcile_local_notifications


THREAD = "11111111-2222-4333-8444-555555555555"
EVENT = NotificationEvent("fixture", "stopped", "first", "Fixture", "Fixture")
NEXT = NotificationEvent("fixture", "stopped", "next", "Fixture", "Fixture")


@pytest.fixture
def controlled(tmp_path):
    store = ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", boot_id="fixture")
    token = store.attach("sender", "host", "vscode")
    return store, token, tmp_path / "runtime"


def all_providers(runtime, fail=None):
    config = NotificationConfig(interactive_transport="all",
        slack_bot_token="xoxb-fixture", slack_channel_id="C12345678",
        lark=LarkConfig("cli_fixture000001", "fixture", "oc_fixture000001", ("ou_fixture000001",), "feishu"),
        onebot=OneBotConfig("ws://127.0.0.1:1/", "fixture", "12345", "group", "67890", ("54321",)))
    notifier = EnvironmentNotifier(runtime, config)
    calls = []
    def send(provider, event):
        calls.append((provider, event.event_fingerprint()))
        if provider == fail:
            raise TimeoutError("fixture timeout")
    for provider in ("slack", "lark", "onebot"):
        setattr(notifier, "_send_" + provider, lambda event, p=provider: send(p, event))
    return notifier, calls


@pytest.mark.parametrize("failed", ["slack", "lark", "onebot"])
def test_provider_uncertainty_is_terminal_and_next_fingerprint_progresses(controlled, failed):
    store, token, runtime = controlled
    notifier, calls = all_providers(runtime, failed)
    result = notify_local_control(store, token, EVENT, notifier)
    assert result["terminal"] and result["provider_outcomes"][failed] == "uncertain"
    assert all(result["provider_outcomes"][p] == "sent" for p in ("slack", "lark", "onebot") if p != failed)
    assert store.read()["external_effect"] is None
    assert notify_local_control(store, token, EVENT, notifier)["duplicate"]
    assert len(calls) == 3
    result = notify_local_control(store, token, NEXT, notifier)
    assert result["terminal"] and len(calls) == 6 and store.read()["external_effect"] is None


def test_live_send_lease_cannot_be_reconciled_or_taken_over(controlled):
    store, token, runtime = controlled
    entered, release = threading.Event(), threading.Event()
    errors = []
    def sending(event):
        entered.set()
        assert release.wait(5)
        return NotificationResult("sent", "fixture", event.event_fingerprint(), False, (), (), runtime, True)
    def run():
        try:
            notify_local_control(store, token, EVENT, SimpleNamespace(runtime=runtime, notify=sending))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        before = store.path.read_bytes()
        with pytest.raises(ControlBusy):
            reconcile_local_notifications(store, runtime)
        with pytest.raises(ControlError, match="external_effect_unresolved"):
            store.claim_remote("new", "host", "absent")
        assert store.path.read_bytes() == before
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors and store.read()["external_effect"] is None


def test_hard_crash_releases_sender_lease_and_preserves_provider_claims(tmp_path):
    # A real child exits without finally/unlocking after an issued provider
    # claim. Recovery obtains the kernel lease, not a TTL or stale-PID guess.
    code = '''
import os, sys
from pathlib import Path
from codex_watchdog.control_state import ControlStore
from codex_watchdog.notification_attempts import local_attempt
from codex_watchdog.notification_receipts import DualDeliveryReceipts
root = Path(sys.argv[1])
store = ControlStore(root / "codex", sys.argv[2], root / "repo", boot_id="fixture")
token = store.attach("crashed", "host", "vscode")
attempt = local_attempt(root / "runtime", store)
with attempt.lease():
    intent = attempt.start(token, sys.argv[3])
    store.prepare_notification(token, sys.argv[3], sys.argv[3], intent["operation_id"], intent["sender"])
    with DualDeliveryReceipts(root / "runtime") as receipts:
        receipts.claim(sys.argv[3], "slack")
        receipts.confirm_sent(sys.argv[3], "slack")
        receipts.claim(sys.argv[3], "lark")
    os._exit(17)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), THREAD, EVENT.event_fingerprint()],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 17, result.stderr
    store = ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", boot_id="fixture")
    before = store.read()["external_effect"]
    runtime = tmp_path / "runtime"
    reconcile_local_notifications(store, runtime)
    assert store.read()["external_effect"] is None
    receipt = json.loads(store.effect_path("notification", EVENT.event_fingerprint()).read_text())
    assert receipt["operation_id"] == before["id"] and receipt["result"]["status"] == "uncertain"
    with DualDeliveryReceipts(runtime) as receipts:
        assert receipts.get(EVENT.event_fingerprint()) == {"slack": "sent", "lark": "uncertain"}
    notifier, calls = all_providers(runtime)
    token = store._token(store.read())
    assert notify_local_control(store, token, EVENT, notifier)["duplicate"] and not calls
    assert notify_local_control(store, token, NEXT, notifier)["status"] == "sent" and len(calls) == 3


def test_crash_between_barrier_and_effect_file_can_be_reconciled(controlled, monkeypatch):
    from codex_watchdog import control_state
    store, token, runtime = controlled
    attempt = local_attempt(runtime, store)
    with attempt.lease():
        intent = attempt.start(token, EVENT.event_fingerprint())
        original = control_state.control_atomic_json
        def fail(path, value):
            if path == store.effect_path("notification", EVENT.event_fingerprint()):
                raise OSError("fixture effect file failure")
            original(path, value)
        monkeypatch.setattr(control_state, "control_atomic_json", fail)
        with pytest.raises(OSError):
            store.prepare_notification(token, intent["fingerprint"], intent["fingerprint"],
                                       intent["operation_id"], intent["sender"])
    monkeypatch.setattr(control_state, "control_atomic_json", original)
    reconcile_local_notifications(store, runtime)
    assert store.read()["external_effect"] is None


def test_generic_native_barrier_and_legacy_unknown_sender_stay_fenced(controlled):
    store, token, runtime = controlled
    store.begin_external(token, "native-writer-operation")
    reconcile_local_notifications(store, runtime)
    assert store.read()["external_effect"]["id"] == "native-writer-operation"
    store.finish_external(token, "native-writer-operation")
    legacy = store.prepare_notification(token, "legacy", "sha")
    reconcile_local_notifications(store, runtime)
    assert store.read()["external_effect"]["id"] == legacy["operation_id"]
    with pytest.raises(ControlError, match="sender_mismatch"):
        store.reconcile_notification(token, "legacy", "sha", legacy["operation_id"], "a" * 64,
                                     {"status": "uncertain"})


def test_dual_store_open_failure_does_not_suppress_independent_onebot(controlled, monkeypatch):
    store, token, runtime = controlled
    notifier, calls = all_providers(runtime)
    monkeypatch.setattr(DualDeliveryReceipts, "__enter__", lambda _: (_ for _ in ()).throw(OSError("fixture")))
    result = notify_local_control(store, token, EVENT, notifier)
    assert result["provider_outcomes"] == {"slack": "failed", "lark": "failed", "onebot": "sent"}
    assert calls == [("onebot", EVENT.event_fingerprint())] and store.read()["external_effect"] is None


def test_reconciliation_reads_only_exact_intent_and_does_not_scan_history(controlled, monkeypatch):
    from codex_watchdog import notification_attempts
    store, token, runtime = controlled
    attempt = local_attempt(runtime, store)
    with attempt.lease():
        intent = attempt.start(token, EVENT.event_fingerprint())
        store.prepare_notification(token, intent["fingerprint"], intent["fingerprint"],
                                   intent["operation_id"], intent["sender"])
    reads = []
    original = notification_attempts.control_read_json
    monkeypatch.setattr(notification_attempts, "control_read_json", lambda path: reads.append(path) or original(path))
    monkeypatch.setattr(Path, "glob", lambda *a, **kw: pytest.fail("history scan"))
    monkeypatch.setattr(Path, "rglob", lambda *a, **kw: pytest.fail("history scan"))
    reconcile_local_notifications(store, runtime)
    assert reads == [attempt.path] and store.read()["external_effect"] is None


def test_wrong_sender_cannot_clear_another_exact_operation(controlled):
    store, token, runtime = controlled
    attempt = local_attempt(runtime, store)
    with attempt.lease():
        intent = attempt.start(token, EVENT.event_fingerprint())
        store.prepare_notification(token, intent["fingerprint"], intent["fingerprint"],
                                   intent["operation_id"], intent["sender"])
    before = store.path.read_bytes(), store.effect_path("notification", intent["fingerprint"]).read_bytes()
    with pytest.raises(ControlError, match="sender_mismatch"):
        store.reconcile_notification(token, intent["fingerprint"], intent["fingerprint"],
                                     intent["operation_id"], "b" * 64, {"status": "uncertain"})
    assert before == (store.path.read_bytes(), store.effect_path("notification", intent["fingerprint"]).read_bytes())


def test_crash_left_mapping_recovery_preserves_only_active_ticket_with_bounded_lookup(controlled):
    from codex_watchdog.slack_mapping import SlackThreadStore, SlackRelayTarget
    store, token, runtime = controlled
    mappings = SlackThreadStore(runtime)
    target = SlackRelayTarget("fixture", THREAD, "process_local")
    fp = EVENT.event_fingerprint()
    mappings.record_thread("C12345678", "1760000000.000100", target, fp)
    journal = mappings.journal
    # Closed history with this same fingerprint is deliberately hostile to a
    # historical fingerprint scan. The active-session index visits <= N=4.
    with journal.transaction() as db:
        entry = journal.get(db, "threads", mappings.thread_key("C12345678", "1760000000.000100"))
        db.executemany("INSERT INTO records VALUES (?,'threads',?,?,?,?,0,?)",
            [(journal.namespace, "closed-" + str(n), json.dumps(entry), fp, THREAD, entry["created_at"])
             for n in range(10000)])
        plan = db.execute("EXPLAIN QUERY PLAN SELECT value FROM records INDEXED BY session_active_tickets "
            "WHERE namespace=? AND kind='threads' AND fingerprint=? AND thread_id COLLATE NOCASE=? "
            "AND active=1 LIMIT 4", (journal.namespace, fp, THREAD)).fetchall()
        assert any("SEARCH" in row[-1] and "session_active_tickets" in row[-1] for row in plan)
        assert not any("SCAN" in row[-1] for row in plan)
    attempt = local_attempt(runtime, store)
    with attempt.lease():
        intent = attempt.start(token, fp, [dict(provider="slack", namespace=journal.namespace)])
        store.prepare_notification(token, fp, fp, intent["operation_id"], intent["sender"])
    recovered, _, errors = attempt.durable_evidence(intent)
    assert not errors and len(recovered) == 1 and recovered[0]["thread_ts"] == "1760000000.000100"
    # A ticket closed after sending but before outer reconciliation stays closed.
    with journal.transaction() as db:
        assert journal.claim(db, mappings.thread_key("C12345678", "1760000000.000100"))
    intent = attempt.terminal(intent, {"status": "sent"}, recovered)
    reconcile_local_notifications(store, runtime)
    assert store.read()["external_effect"] is None and store.slack_mappings() == []
    with journal.transaction() as db:
        assert journal.active(db) == {} and db.execute("SELECT count(*) FROM records WHERE kind='threads'").fetchone()[0] == 10001
