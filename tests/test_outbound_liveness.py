"""Crash-left notification claims suppress only their exact logical effect."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.notification_receipts import DualDeliveryReceipts, FallbackDeliveryReceipts, NotificationReceiptError
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.onebot_transport import OneBotConfig
from codex_watchdog.storage import InstructionStore


WEBHOOK = "https://hooks.slack.invalid/services/one-shot-fixture"
LARK = LarkConfig("cli_fixture000001", "fixture-secret", "oc_fixture000001",
                  ("ou_fixture000001",), "feishu")
ONEBOT = OneBotConfig("ws://127.0.0.1:3001", "fixture-token", "12345", "group", "67890", ("54321",))


def event(transition="old", workspace="one-shot-workspace"):
    return NotificationEvent(workspace, "stopped", transition, "Fixture subject", "Fixture output")


def config(provider="slack"):
    return NotificationConfig(interactive_transport=provider, slack_webhook_url=WEBHOOK,
                              lark=LARK if "lark" in provider or provider == "both" else LarkConfig(),
                              onebot=ONEBOT if "onebot" in provider else OneBotConfig())


class Calls:
    def __init__(self, failed=None):
        self.calls, self.failed = [], failed

    def slack(self, *args):
        self.calls.append("slack")
        if self.failed == "slack":
            raise TimeoutError("response lost after a possible send")
        return 200

    def send(self, text, operation_id, **kwargs):
        provider = "onebot" if kwargs.pop("onebot_fixture", False) else "lark"
        self.calls.append(provider)
        if self.failed == provider:
            raise TimeoutError("response lost after a possible send")
        return (dict(chat_id=ONEBOT.destination, message_id="100") if provider == "onebot"
                else dict(chat_id=LARK.chat_id, message_id="om_fixturemessage1"))

    def notifier(self, runtime, provider="slack", **kwargs):
        owner = self
        class OneBotApi:
            def send(self, text, operation_id, **arguments):
                return owner.send(text, operation_id, onebot_fixture=True, **arguments)
        return EnvironmentNotifier(runtime, config(provider), http_post=self.slack,
                                   lark_api=self, onebot_api=OneBotApi(), **kwargs)


@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_uncertain_single_provider_is_terminal_but_distinct_event_progresses(tmp_path, provider):
    calls = Calls(provider)
    first = calls.notifier(tmp_path, provider).notify(event())
    assert first.status == "delivery_failed" and calls.calls == [provider]
    calls.failed = None
    repeated = calls.notifier(tmp_path, provider).notify(event())
    assert repeated.status == "delivery_failed" and calls.calls == [provider]
    assert calls.notifier(tmp_path, provider).notify(event("new")).status == "sent"
    assert calls.calls == [provider, provider]


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_crash_after_committed_claim_before_send_is_not_retried(tmp_path, monkeypatch, provider):
    claim = DualDeliveryReceipts.claim
    def crash(store, fingerprint, selected):
        result = claim(store, fingerprint, selected)
        if fingerprint == event().event_fingerprint() and selected == provider:
            raise SystemExit("crash after durable claim")
        return result
    calls = Calls()
    monkeypatch.setattr(DualDeliveryReceipts, "claim", crash)
    with pytest.raises(SystemExit):
        calls.notifier(tmp_path, provider).notify(event())
    assert calls.calls == []
    monkeypatch.setattr(DualDeliveryReceipts, "claim", claim)
    assert calls.notifier(tmp_path, provider).notify(event()).status == "delivery_failed"
    assert calls.calls == []
    assert calls.notifier(tmp_path, provider).notify(event("new")).status == "sent"
    assert calls.calls == [provider]


@pytest.mark.parametrize("boundary", ["after_claim", "during_send"])
def test_actual_process_death_keeps_claim_and_later_event_eligible(tmp_path, boundary):
    script = r'''
from pathlib import Path
import os,sys
from codex_watchdog.notification_receipts import DualDeliveryReceipts
from codex_watchdog.notifications import EnvironmentNotifier,NotificationConfig,NotificationEvent
runtime=Path(sys.argv[1]); boundary=sys.argv[2]
claim=DualDeliveryReceipts.claim
def crash_after_claim(store,*args):
    result=claim(store,*args)
    os._exit(23)
def send(*args):
    (runtime/'actual-attempt').write_text('attempted')
    os._exit(23)
if boundary=='after_claim': DualDeliveryReceipts.claim=crash_after_claim
EnvironmentNotifier(runtime,NotificationConfig(slack_webhook_url='https://hooks.slack.invalid/fixture'),
    http_post=send).notify(NotificationEvent('one-shot-workspace','stopped','old','Fixture subject','Fixture output'))
'''
    completed = subprocess.run([sys.executable, "-c", script, str(tmp_path), boundary],
        env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        capture_output=True, text=True, timeout=15)
    assert completed.returncode == 23, completed.stderr
    assert (tmp_path / "actual-attempt").exists() == (boundary == "during_send")
    calls = Calls()
    assert calls.notifier(tmp_path).notify(event()).status == "delivery_failed"
    assert calls.calls == []
    assert calls.notifier(tmp_path).notify(event("new")).status == "sent"
    assert calls.calls == ["slack"]


def test_post_send_last_event_write_failure_never_replays_after_restart(tmp_path):
    calls = Calls()
    def fail_last_event(path, value):
        if path.name == "last-events.json":
            raise OSError("fixture disk failure after provider confirmation")
        InstructionStore._atomic_json(path, value)
    result = calls.notifier(tmp_path, atomic_writer=fail_last_event).notify(event())
    assert result.status == "state_error" and calls.calls == ["slack"]
    calls.notifier(tmp_path).notify(event())
    assert calls.calls == ["slack"]
    calls.notifier(tmp_path).notify(event("new"))
    assert calls.calls == ["slack", "slack"]


@pytest.mark.parametrize("failed", ["slack", "lark"])
def test_failed_provider_and_historical_claim_do_not_starve_other_or_later_session(tmp_path, failed):
    calls = Calls(failed)
    first = calls.notifier(tmp_path, "both").notify(event())
    healthy = "lark" if failed == "slack" else "slack"
    assert first.provider_outcomes == {failed: "uncertain", healthy: "sent"}
    calls.failed = None
    calls.notifier(tmp_path, "both").notify(event())
    assert calls.calls == ["slack", "lark"]
    assert calls.notifier(tmp_path, "both").notify(event("new", "different-workspace")).status == "sent"
    assert calls.calls == ["slack", "lark", "slack", "lark"]


def test_unavailable_dual_storage_drops_only_it_and_allows_onebot(tmp_path):
    path = tmp_path / "notifications/dual-deliveries.json"
    path.parent.mkdir()
    path.write_text("malformed retained old receipt")
    calls = Calls()
    result = calls.notifier(tmp_path, "slack+onebot").notify(event())
    assert result.status == "delivery_failed"
    assert result.provider_outcomes == {"slack": "failed", "onebot": "sent"}
    assert calls.calls == ["onebot"] and path.read_text() == "malformed retained old receipt"


def test_corrupt_last_event_cache_still_honors_imported_receipt(tmp_path, monkeypatch):
    calls = Calls()
    owner = calls.notifier(tmp_path)
    owner.relay_runtime = tmp_path / "shared"
    owner.state_path.parent.mkdir()
    owner.state_path.write_text("retained malformed cache")
    lookups = []
    def imported(runtime, fingerprint):
        lookups.append((runtime, fingerprint))
        return fingerprint == event().event_fingerprint()
    monkeypatch.setattr("codex_watchdog.relay_notification_migration.previous_notification", imported)
    assert owner.notify(event()).duplicate and calls.calls == []
    assert owner.notify(event("new")).channel == "slack" and calls.calls == ["slack"]
    assert lookups == [(owner.relay_runtime, event().event_fingerprint()),
                       (owner.relay_runtime, event("new").event_fingerprint())]
    assert owner.state_path.read_text() == "retained malformed cache"


def test_fallback_store_failure_keeps_bytes_and_does_not_block_independent_slack(tmp_path):
    directory = tmp_path / "notifications"
    directory.mkdir()
    database = directory / "fallback-deliveries.sqlite3"
    database.write_bytes(b"retained malformed fallback ledger")
    cfg = NotificationConfig(smtp_host="smtp.invalid", smtp_sender="fixture@example.invalid",
        smtp_recipients=("owner@example.invalid",))
    failed = EnvironmentNotifier(tmp_path, cfg)
    failed._send_smtp = lambda value: pytest.fail("unclaimed SMTP effect must not send")
    assert failed.notify(event()).status == "delivery_failed"
    assert database.read_bytes() == b"retained malformed fallback ledger"
    calls = Calls()
    assert calls.notifier(tmp_path).notify(event("new")).status == "sent"
    assert calls.calls == ["slack"]
    assert database.read_bytes() == b"retained malformed fallback ledger"


@pytest.mark.parametrize("provider", ["smtp", "windows_msg"])
def test_fallback_external_attempt_follows_committed_claim_without_transaction(tmp_path, provider):
    cfg = NotificationConfig(smtp_host="smtp.invalid", smtp_sender="fixture@example.invalid",
        smtp_recipients=("owner@example.invalid",), windows_message_enabled=True,
        windows_message_target="fixture-user")
    owner = EnvironmentNotifier(tmp_path, cfg)
    calls = []
    def inspect(value):
        with sqlite3.connect(tmp_path / "notifications/fallback-deliveries.sqlite3") as db:
            assert db.execute("SELECT state FROM receipts WHERE fingerprint=? AND provider=?",
                (value.event_fingerprint(), provider)).fetchone() == ("uncertain",)
            db.execute("BEGIN IMMEDIATE")  # No outstanding sender transaction.
            db.rollback()
        calls.append(provider)
    if provider == "windows_msg":
        owner._send_smtp = lambda value: (_ for _ in ()).throw(TimeoutError("SMTP unavailable"))
    setattr(owner, "_send_smtp" if provider == "smtp" else "_send_windows_message", inspect)
    assert owner.notify(event()).channel == provider and calls == [provider]


def test_fallback_identity_collision_preserves_original_claim(tmp_path):
    fp = event().event_fingerprint()
    with FallbackDeliveryReceipts(tmp_path) as receipts:
        assert receipts.claim(fp, "smtp", "1" * 64, "2" * 64) is None
        with pytest.raises(NotificationReceiptError, match="identity_collision"):
            receipts.claim(fp, "smtp", "3" * 64, "2" * 64)
        assert receipts.claim(fp, "smtp", "1" * 64, "2" * 64) == "uncertain"


@pytest.mark.parametrize("provider", ["smtp", "windows_msg"])
@pytest.mark.parametrize("boundary", ["after_claim", "during_send", "after_send"])
def test_fallback_crashes_and_lost_confirmation_are_per_effect_terminal(tmp_path, monkeypatch, provider, boundary):
    cfg = NotificationConfig(smtp_host="smtp.invalid", smtp_sender="fixture@example.invalid",
        smtp_recipients=("owner@example.invalid",),
        windows_message_enabled=True, windows_message_target="fixture-user")
    calls = []
    def owner():
        notifier = EnvironmentNotifier(tmp_path, cfg)
        notifier._send_smtp = lambda value: calls.append(("smtp", value.transition_fingerprint))
        notifier._send_windows_message = lambda value: calls.append(("windows_msg", value.transition_fingerprint))
        return notifier
    original_claim = FallbackDeliveryReceipts.claim
    original_confirmation = FallbackDeliveryReceipts.confirm_sent
    def crash_claim(store, fp, selected, *args):
        result = original_claim(store, fp, selected, *args)
        if selected == provider and fp == event().event_fingerprint():
            raise SystemExit("claimed; no external attempt")
        return result
    def lost_confirmation(store, fp, selected):
        if selected == provider and fp == event().event_fingerprint():
            raise SystemExit("sent; confirmation lost")
        return original_confirmation(store, fp, selected)
    notifier = owner()
    if boundary == "after_claim":
        monkeypatch.setattr(FallbackDeliveryReceipts, "claim", crash_claim)
    elif boundary == "after_send":
        monkeypatch.setattr(FallbackDeliveryReceipts, "confirm_sent", lost_confirmation)
    else:
        def crash_send(value):
            calls.append((provider, value.transition_fingerprint))
            raise SystemExit("crashed during external send")
        setattr(notifier, "_send_smtp" if provider == "smtp" else "_send_windows_message", crash_send)
    # Force the SMTP effect to be unavailable when testing the desktop fallback.
    if provider == "windows_msg":
        notifier._send_smtp = lambda value: (_ for _ in ()).throw(TimeoutError("SMTP unavailable"))
    with pytest.raises(SystemExit):
        notifier.notify(event())
    count = calls.count((provider, "old"))
    monkeypatch.setattr(FallbackDeliveryReceipts, "claim", original_claim)
    monkeypatch.setattr(FallbackDeliveryReceipts, "confirm_sent", original_confirmation)
    owner().notify(event())
    assert calls.count((provider, "old")) == count
    later = owner()
    if provider == "windows_msg":
        later._send_smtp = lambda value: (_ for _ in ()).throw(TimeoutError("SMTP unavailable"))
    assert later.notify(event("new")).channel == provider
    assert calls.count((provider, "new")) == 1


def test_two_independent_claimers_get_one_fallback_attempt(tmp_path):
    with FallbackDeliveryReceipts(tmp_path):
        pass
    fp, target, payload = (hashlib.sha256(value).hexdigest() for value in (b"event", b"target", b"payload"))
    def claim(_):
        with FallbackDeliveryReceipts(tmp_path) as receipts:
            return receipts.claim(fp, "smtp", target, payload)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert results.count(None) == 1 and results.count("uncertain") == 3


def test_previous_public_receipts_and_unknown_metadata_survive_distinct_send(tmp_path):
    old = event().event_fingerprint()
    with DualDeliveryReceipts(tmp_path) as receipts:
        assert receipts.claim(old, "slack")
    marker = tmp_path / "notifications/dual-deliveries.json"
    metadata = json.loads(marker.read_text())
    metadata["future_retained"] = {"schema": 99, "value": "unknown"}
    marker.write_text(json.dumps(metadata))
    before = marker.read_bytes()
    with FallbackDeliveryReceipts(tmp_path) as receipts:
        receipts.db.execute("ALTER TABLE receipts ADD COLUMN future_evidence TEXT")
        assert receipts.claim(old, "smtp", "1" * 64, "2" * 64) is None
        receipts.db.execute("UPDATE receipts SET future_evidence='retain' WHERE fingerprint=?", (old,))
    calls = Calls()
    calls.notifier(tmp_path).notify(event("new"))
    assert marker.read_bytes() == before
    with DualDeliveryReceipts(tmp_path) as receipts:
        assert receipts.get(old) == {"slack": "uncertain"}
    with FallbackDeliveryReceipts(tmp_path) as receipts:
        assert receipts.claim(old, "smtp", "1" * 64, "2" * 64) == "uncertain"
        assert receipts.db.execute("SELECT future_evidence FROM receipts WHERE fingerprint=?", (old,)).fetchone() == ("retain",)


@pytest.mark.parametrize("history", [0, 10000])
def test_fallback_history_keeps_indexed_point_claim_and_constant_operation_budget(tmp_path, history, capsys):
    with FallbackDeliveryReceipts(tmp_path) as receipts:
        receipts.db.executemany("INSERT INTO receipts VALUES(?,?,?,?,?)", (
            (hashlib.sha256(str(index).encode()).hexdigest(), "smtp", "1" * 64, "2" * 64, "uncertain")
            for index in range(history)))
        plan = receipts.db.execute("EXPLAIN QUERY PLAN SELECT target_sha256,payload_sha256,state "
            "FROM receipts WHERE fingerprint=? AND provider=?", ("a" * 64, "smtp")).fetchall()
        statements = []
        receipts.db.set_trace_callback(statements.append)
        assert receipts.claim("a" * 64, "smtp", "1" * 64, "2" * 64) is None
        receipts.confirm_sent("a" * 64, "smtp")
        assert receipts.claim("a" * 64, "smtp", "1" * 64, "2" * 64) == "sent"
    assert "SEARCH" in str(plan) and "INDEX" in str(plan) and "SCAN" not in str(plan)
    point_reads = [sql for sql in statements if sql.startswith("SELECT")]
    assert len(statements) == 10 and len(point_reads) == 2
    assert all("WHERE fingerprint=" in sql and "AND provider=" in sql for sql in point_reads)
    receipt = dict(history=history, sql_statements=len(statements), receipt_point_reads=len(point_reads), query_plan=plan)
    with capsys.disabled():
        print(json.dumps({"fallback_receipt_work": receipt}, sort_keys=True))
