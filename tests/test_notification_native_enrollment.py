"""New native receipt enrollment uses the real committed current-claim ledger.

All roots, identities and provider endpoints are disposable fixtures. These tests
prove notification persistence boundaries, never live native ownership or admission.
"""
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import uuid

import pytest

from codex_watchdog import notification_receipt_retention as finite
from codex_watchdog import notification_root_retention as root
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.models import sha256_text
from codex_watchdog.notification_receipts import DualDeliveryReceipts, FallbackDeliveryReceipts
from codex_watchdog.notification_senders import sender_fence
from codex_watchdog.notification_state import CurrentNotificationState
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
from codex_watchdog.storage import FileLock, StoreBusyError
from test_notification_root_integration import NOW, SID, event
from test_notification_sender_integration import busy, held_elsewhere


PROVIDERS = ("slack", "lark", "smtp", "windows_msg")


def native(index=1, *, born=NOW + 1):
    return replace(event(born=born, state="native-" + str(index)),
                   workspace_id="new-native-" + str(index),
                   source_thread_id=str(uuid.UUID(int=index + 1, version=4)))


def prepare(runtime):
    return root.prepare_notification_root(runtime,
        scopes=[(SID, sha256_text("fixture"), provider) for provider in PROVIDERS],
        current_keys=[], contexts=[], current_pins=(), sender_fence=nullcontext,
        clock=lambda: NOW)


def database(runtime, provider):
    family = "dual" if provider in ("slack", "lark") else "fallback"
    return runtime / "notifications" / (finite.NAMES[family] + ".sqlite3")


def boundary(runtime, current, provider):
    with closing(sqlite3.connect(database(runtime, provider))) as db:
        return db.execute("SELECT floor,floor_key,checked FROM boundaries "
            "WHERE thread_id=? AND scope=? AND provider=?",
            (current.source_thread_id, sha256_text(current.workspace_id), provider)).fetchone()


def retained(runtime, fingerprint, provider):
    with closing(sqlite3.connect(database(runtime, provider))) as db:
        return db.execute("SELECT * FROM retention WHERE fingerprint=? AND provider=?",
                          (fingerprint, provider)).fetchone()


def context(current, provider):
    return dict(schema_version=1, thread_id=current.source_thread_id,
        scope_sha256=sha256_text(current.workspace_id), born=current.source_born,
        fingerprint=current.event_fingerprint(), providers={provider:dict(
            target_sha256=sha256_text("fixture-target"), payload_sha256=sha256_text("fixture-payload"))})


def adapter(runtime, current, provider="slack", *, now=NOW + 2, current_key=None):
    cls = DualDeliveryReceipts if provider in ("slack", "lark") else FallbackDeliveryReceipts
    return cls(runtime, receipt_context=context(current, provider), current_key=current_key,
               clock=lambda: now)


def claim(receipts, current, provider):
    if provider in ("slack", "lark"):
        return receipts.claim(current.event_fingerprint(), provider)
    return receipts.claim(current.event_fingerprint(), provider,
                          **context(current, provider)["providers"][provider])


@contextmanager
def guards(runtime):
    with sender_fence(runtime), FileLock(runtime / "locks/notifications.lock"):
        yield


def notifier(runtime, provider, clock, calls, on_send=None):
    def sent():
        busy(runtime)
        with pytest.raises(StoreBusyError):
            with FileLock(runtime / "locks/notifications.lock"):
                pytest.fail("notification lock absent at provider attempt")
        if on_send is not None:
            on_send()
        calls.append(provider)

    config = {
        "slack": NotificationConfig(slack_bot_token="xoxb-fixture", slack_channel_id="C12345678"),
        "lark": NotificationConfig(interactive_transport="lark", lark=LarkConfig(
            "cli_" + "a" * 20, "fixture-secret", "oc_fixture000000001", ())),
        "smtp": NotificationConfig(smtp_host="mail.example.test", smtp_port=587,
            smtp_sender="from@example.test", smtp_recipients=("to@example.test",)),
        "windows_msg": NotificationConfig(windows_message_enabled=True,
                                          windows_message_target="fixture-user"),
    }[provider]

    def post(token, method, payload, timeout):
        sent()
        return dict(channel=payload["channel"], ts="1792008000.000001")

    class LarkEndpoint:
        def send(self, text, fingerprint, **kwargs):
            sent()
            return dict(chat_id=config.lark.chat_id, message_id="om_fixture00000001")

    class NotificationOnlyLarkStore:
        def prepare_notification(self, *args, **kwargs):
            return None

        def finish_notification(self, *args, **kwargs):
            pass

    result = EnvironmentNotifier(runtime, config, slack_api_post=post,
        lark_api=LarkEndpoint(), lark_thread_store=NotificationOnlyLarkStore(),
        clock=lambda: clock[0])
    result._send_smtp = lambda current: sent()
    result._send_windows_message = lambda current: sent()
    return result


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("provider network is outside the disposable fixture")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def no_cold_work(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("ordinary native enrollment invoked cold/history work")
    monkeypatch.setattr(finite, "_legacy_rows", refuse)
    monkeypatch.setattr(finite, "prepare_notification_receipts", refuse)
    monkeypatch.setattr(root, "prepare_notification_root", refuse)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_actual_notifier_new_native_provider_claim_is_committed_once(tmp_path, monkeypatch, provider):
    prepared = prepare(tmp_path)
    current = native()
    calls, clock = [], [NOW + 2]
    assert boundary(tmp_path, current, provider) is None

    def before_attempt():
        # A different connection must see the committed pre-attempt reservation.
        row = retained(tmp_path, current.event_fingerprint(), provider)
        assert row[2:4] == (current.source_thread_id, sha256_text(current.workspace_id))
        assert row[4:7] == (NOW + 1, NOW + 1 + finite.TTL, 0)

    sender = notifier(tmp_path, provider, clock, calls, before_attempt)
    no_cold_work(monkeypatch)
    assert sender.notify(current).status == "sent"
    assert calls == [provider]
    value = boundary(tmp_path, current, provider)
    assert value == (NOW, "~", NOW + 2)
    assert CurrentNotificationState(tmp_path).claim_time(
        current.dedupe_key(), current.event_fingerprint()) == NOW + 2
    assert sender.notify(current).status == "suppressed" and calls == [provider]
    selected = root.read_notification_root(tmp_path)
    assert selected["floor"] == prepared["floor"] and selected["plan_sha256"] == prepared["plan_sha256"]


@pytest.mark.parametrize("born,now", [
    (None, NOW + 2), (NOW - 1, NOW + 2), (NOW, NOW + 2),
    (NOW + 20, NOW + 2), (NOW + 1, NOW + finite.TTL + 2),
])
def test_unknown_old_future_or_expired_original_birth_cannot_rebirth_new_scope(tmp_path, born, now):
    prepare(tmp_path)
    current = native(born=born)
    calls, clock = [], [now]
    sender = notifier(tmp_path, "slack", clock, calls)
    assert sender.notify(current).status in ("suppressed", "delivery_failed")
    assert not calls and boundary(tmp_path, current, "slack") is None
    assert retained(tmp_path, current.event_fingerprint(), "slack") is None
    clock[0] = max(now, NOW + 3) + 1
    fresh = native(2, born=clock[0])
    assert sender.notify(fresh).status == "sent" and calls == ["slack"]


def test_new_scope_clock_rewind_uses_real_root_global_committed_claim_time(tmp_path):
    prepare(tmp_path)
    calls, clock = [], [NOW + finite.TTL + 10]
    sender = notifier(tmp_path, "slack", clock, calls)
    forward = native(1, born=clock[0])
    assert sender.notify(forward).status == "sent"
    high = clock[0]
    clock[0] = NOW + 3
    old = native(2, born=NOW + 2)
    assert sender.notify(old).status == "delivery_failed"
    assert CurrentNotificationState(tmp_path).claim_time(old.dedupe_key(), old.event_fingerprint()) == high
    assert boundary(tmp_path, old, "slack") is None and calls == ["slack"]
    clock[0] = high + 1
    assert sender.notify(native(3, born=clock[0])).status == "sent"
    assert calls == ["slack", "slack"]
    assert sender.notify(forward).status == "suppressed" and len(calls) == 2


@pytest.mark.parametrize("family,damage", [
    ("dual", "missing"), ("fallback", "missing"), ("dual", "metadata"),
    ("dual", "foreign"), ("fallback", "foreign"),
])
def test_new_scope_does_not_recreate_or_repair_bad_selected_family(tmp_path, family, damage):
    selected, foreign = tmp_path / "selected", tmp_path / "foreign"
    prepare(selected)
    provider = "slack" if family == "dual" else "smtp"
    path = database(selected, provider)
    if damage == "missing":
        path.unlink()
    elif damage == "metadata":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE finite_metadata SET value='not-json' WHERE id=1")
    else:
        root.prepare_notification_root(foreign,
            scopes=[(SID, sha256_text("fixture"), provider)], current_keys=[], current_pins=(),
            sender_fence=nullcontext, clock=lambda: NOW - 100)
        shutil.copyfile(database(foreign, provider), path)
        if family == "dual":
            shutil.copyfile(foreign / "notifications/dual-deliveries.json",
                            selected / "notifications/dual-deliveries.json")
    before = path.read_bytes() if path.exists() else None
    ready = (selected / "notifications" / root.READY).read_bytes()
    calls, clock = [], [NOW + 2]
    assert notifier(selected, provider, clock, calls).notify(native()).status == "delivery_failed"
    assert calls == [] and (path.read_bytes() if path.exists() else None) == before
    assert (selected / "notifications" / root.READY).read_bytes() == ready
    healthy = "smtp" if family == "dual" else "slack"
    assert notifier(selected, healthy, clock, calls).notify(native(2)).status == "sent"
    assert calls == [healthy]


def test_existing_high_or_malformed_boundary_is_not_replaced_by_enrollment(tmp_path):
    prepare(tmp_path)
    current = native(born=NOW + 50)
    subject = (current.source_thread_id, sha256_text(current.workspace_id), "slack")
    path = database(tmp_path, "slack")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO boundaries VALUES(?,?,?,?,?,?)", (*subject, NOW + 100, "~", NOW + 200))
    calls, clock = [], [NOW + 201]
    sender = notifier(tmp_path, "slack", clock, calls)
    assert sender.notify(current).status == "delivery_failed" and not calls
    assert boundary(tmp_path, current, "slack") == (NOW + 100, "~", NOW + 200)
    fresh = replace(current, transition_fingerprint="later", source_born=NOW + 201)
    assert sender.notify(fresh).status == "sent" and calls == ["slack"]
    assert boundary(tmp_path, current, "slack")[:2] == (NOW + 100, "~")
    bad = native(2)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO boundaries VALUES(?,?,?,?,?,?)",
                   (bad.source_thread_id, sha256_text(bad.workspace_id), "slack", "bad", "~", NOW))
    assert sender.notify(bad).status == "delivery_failed" and len(calls) == 1
    assert boundary(tmp_path, bad, "slack") == ("bad", "~", NOW)


def test_missing_boundary_with_current_protected_receipt_is_damage_not_new_scope(tmp_path):
    prepare(tmp_path)
    current = native()
    calls, clock = [], [NOW + 2]
    sender = notifier(tmp_path, "slack", clock, calls)
    assert sender.notify(current).status == "sent"
    scope = (current.source_thread_id, sha256_text(current.workspace_id), "slack")
    finite.maintain_notification_receipts(tmp_path, scopes=[scope],
        pin_changes=[(current.event_fingerprint(), "slack", True)], complete=True,
        sender_fence=nullcontext, clock=lambda: NOW + 2)
    before = retained(tmp_path, current.event_fingerprint(), "slack")
    assert before[6] == 1
    with sqlite3.connect(database(tmp_path, "slack")) as db:
        db.execute("DELETE FROM boundaries WHERE thread_id=? AND scope=? AND provider=?", scope)
    later = replace(current, transition_fingerprint="different-source", source_born=NOW + 3)
    clock[0] = NOW + 3
    assert sender.notify(later).status == "delivery_failed" and len(calls) == 1
    assert boundary(tmp_path, current, "slack") is None
    assert retained(tmp_path, current.event_fingerprint(), "slack") == before
    assert retained(tmp_path, later.event_fingerprint(), "slack") is None


def test_new_scope_preserves_other_current_pins_and_native_state(tmp_path):
    prepare(tmp_path)
    old = event()
    calls, clock = [], [NOW + 2]
    sender = notifier(tmp_path, "slack", clock, calls)
    assert sender.notify(old).status == "sent"
    old_scope = (SID, sha256_text("fixture"), "slack")
    finite.maintain_notification_receipts(tmp_path, scopes=[old_scope],
        pin_changes=[(old.event_fingerprint(), "slack", True)], complete=True,
        sender_fence=nullcontext, clock=lambda: NOW + 2)
    old_row = retained(tmp_path, old.event_fingerprint(), "slack")
    old_boundary = boundary(tmp_path, old, "slack")
    sentinel = tmp_path / "native-provider-current-state.sentinel"
    sentinel.write_bytes(b"current native/provider state is not notification enrollment")
    clock[0] = NOW + finite.TTL + 5
    assert sender.notify(native(born=clock[0])).status == "sent"
    assert retained(tmp_path, old.event_fingerprint(), "slack") == old_row
    assert boundary(tmp_path, old, "slack") == old_boundary
    assert sentinel.read_bytes() == b"current native/provider state is not notification enrollment"


@pytest.mark.parametrize("key_mode", ["absent", "wrong", "uncommitted-fingerprint"])
def test_absent_or_wrong_committed_claim_capability_cannot_enroll(tmp_path, key_mode):
    prepare(tmp_path)
    current = native()
    with guards(tmp_path):
        ledger = CurrentNotificationState(tmp_path, clock=lambda: NOW + 2)
        assert ledger.claim(current.dedupe_key(), current.event_fingerprint())
        key = None if key_mode == "absent" else sha256_text("wrong-key") if key_mode == "wrong" else current.dedupe_key()
        if key_mode == "uncommitted-fingerprint":
            current = replace(current, transition_fingerprint="never-claimed")
        with pytest.raises((ValueError, sqlite3.Error)):
            with adapter(tmp_path, current, current_key=key) as receipts:
                claim(receipts, current, "slack")
    assert boundary(tmp_path, current, "slack") is None
    assert retained(tmp_path, current.event_fingerprint(), "slack") is None


@pytest.mark.parametrize("index_state", ["missing", "partial", "wrong-columns"])
def test_bad_new_scope_probe_index_is_not_repaired_or_required_by_existing_scope(tmp_path, index_state):
    prepare(tmp_path)
    path = database(tmp_path, "slack")
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX retention_subject")
        if index_state == "partial":
            db.execute("CREATE INDEX retention_subject ON retention(thread_id,scope,provider) WHERE protected=0")
        elif index_state == "wrong-columns":
            db.execute("CREATE INDEX retention_subject ON retention(thread_id,provider,scope)")
        before = db.execute("SELECT sql FROM sqlite_master WHERE name='retention_subject'").fetchall()
    calls, clock = [], [NOW + 2]
    sender = notifier(tmp_path, "slack", clock, calls)
    current = native()
    assert sender.notify(current).status == "delivery_failed" and not calls
    assert boundary(tmp_path, current, "slack") is None
    assert retained(tmp_path, current.event_fingerprint(), "slack") is None
    # The cold-prepared exact scope does not need this missing-scope probe.
    assert sender.notify(event()).status == "sent" and calls == ["slack"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='retention_subject'").fetchall() == before


def test_existing_legacy_native_scope_does_not_require_new_observer_indexes(tmp_path):
    finite.prepare_notification_receipts(tmp_path,
        scopes=[(SID, sha256_text("fixture"), "slack")], contexts=[],
        sender_fence=nullcontext, clock=lambda: NOW)
    path = database(tmp_path, "slack")
    with sqlite3.connect(path) as db:
        meta = json.loads(db.execute("SELECT value FROM finite_metadata WHERE id=1").fetchone()[0])
        meta.pop("observer_retention")
        db.execute("UPDATE finite_metadata SET value=? WHERE id=1", (json.dumps(meta),))
        for name in ("observer_receipt_expiry", "observer_boundary_expiry", "retention_subject"):
            db.execute("DROP INDEX " + name)
    old = event()
    with adapter(tmp_path, old) as receipts:
        assert claim(receipts, old, "slack")
    with adapter(tmp_path, old) as receipts:
        assert not claim(receipts, old, "slack")
    with pytest.raises(finite.RetentionError):
        with adapter(tmp_path, native()) as receipts:
            claim(receipts, native(), "slack")
    assert root.read_notification_root(tmp_path) is None


def test_busy_sender_and_failed_current_commit_cannot_authorize_attempt(tmp_path, monkeypatch):
    prepare(tmp_path)
    calls, clock = [], [NOW + 2]
    sender = notifier(tmp_path, "slack", clock, calls)
    current = native()
    with held_elsewhere(tmp_path):
        with pytest.raises(StoreBusyError):
            sender.notify(current)
    assert not calls and boundary(tmp_path, current, "slack") is None
    real_connect = sqlite3.connect
    failures = []
    class FailCommit:
        def __init__(self, db):
            self.db = db
        def __getattr__(self, name):
            return getattr(self.db, name)
        def commit(self):
            failures.append("current-receipt-commit")
            raise sqlite3.OperationalError("disposable commit failure")
    def connect(path, *args, **kwargs):
        db = real_connect(path, *args, **kwargs)
        return FailCommit(db) if "dual-deliveries.sqlite3" in str(path) else db
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        assert sender.notify(current).status == "delivery_failed"
    assert failures == ["current-receipt-commit"] and not calls
    assert boundary(tmp_path, current, "slack") is None
    assert retained(tmp_path, current.event_fingerprint(), "slack") is None
    assert sender.notify(current).status == "suppressed" and not calls


def seed_history(runtime, history):
    with sqlite3.connect(database(runtime, "slack")) as db:
        db.executemany("INSERT INTO boundaries VALUES(?,?,?,?,?,?)", (
            (str(uuid.UUID(int=1000 + i, version=4)), sha256_text("history-scope-" + str(i)),
             "slack", NOW, "~", NOW) for i in range(history)))
        db.executemany("INSERT INTO receipts VALUES(?,'uncertain',NULL)",
                       ((sha256_text("history-" + str(i)),) for i in range(history)))
        db.executemany("INSERT INTO retention VALUES(?,?,?,?,?,?,0,?,?)", (
            (sha256_text("history-" + str(i)), "slack", SID, sha256_text("fixture"),
             NOW - 1000, NOW - 1000 + finite.TTL, sha256_text("target"), sha256_text("payload"))
            for i in range(history)))


def measured_new_scope(runtime, monkeypatch):
    calls, clock = [], [NOW + 2]
    sender = notifier(runtime, "slack", clock, calls)
    statements, steps, connections = [], [0], []
    real_connect = sqlite3.connect
    def step():
        steps[0] += 1
        return 0
    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        connections.append(str(args[0]))
        db.set_trace_callback(statements.append)
        db.set_progress_handler(step, 1)
        return db
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        no_cold_work(patch)
        assert sender.notify(native()).status == "sent"
    assert calls == ["slack"]
    with closing(real_connect(database(runtime, "slack"))) as db:
        plans = [db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall() for sql, params in (
            ("SELECT 1 FROM boundaries WHERE thread_id=? AND scope=? AND provider=?",
             (native().source_thread_id, sha256_text(native().workspace_id), "slack")),
            ("SELECT 1 FROM retention INDEXED BY retention_subject WHERE thread_id=? AND scope=? AND provider=? LIMIT 1",
             (native().source_thread_id, sha256_text(native().workspace_id), "slack")),
        )]
    for plan in plans:
        detail = " ".join(row[3] for row in plan)
        assert "SEARCH" in detail and "SCAN" not in detail and "TEMP B-TREE" not in detail
    assert not any("FROM HISTORY" in sql.upper() or "COUNT(" in sql.upper() for sql in statements)
    return dict(sql=len(statements), vm=steps[0], connections=len(connections), plans=plans)


@pytest.mark.parametrize("history", [0, 1000, 10000, 100000])
def test_new_native_claim_work_is_indexed_at_fixed_current_work(tmp_path, monkeypatch, capsys, history):
    baseline, selected = tmp_path / "baseline", tmp_path / "selected"
    prepare(baseline)
    prepare(selected)
    seed_history(selected, history)
    small = measured_new_scope(baseline, monkeypatch)
    large = measured_new_scope(selected, monkeypatch)
    assert large["sql"] == small["sql"] and large["connections"] == small["connections"]
    assert large["vm"] <= small["vm"] + 128  # Fixed indexed seek/write variance, not H rows.
    with capsys.disabled():
        print("C345_NATIVE_ENROLLMENT_COUNTS", json.dumps(dict(
            history=history, baseline=small, selected=large), sort_keys=True))
