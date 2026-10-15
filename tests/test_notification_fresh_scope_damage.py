"""A missing current boundary cannot be repaired over retained fresh-root work.

Only disposable notification rows are deliberately damaged. Protected fixture
bits are not evidence about any actual native queue, writer or HTTP request.
"""
from contextlib import closing, nullcontext
from dataclasses import replace
import json
import socket
import sqlite3

import pytest

from codex_watchdog import notification_receipt_retention as finite
from codex_watchdog import notification_root_retention as root
from codex_watchdog.models import sha256_text
from codex_watchdog.notification_receipts import maintain_observer_receipts
from codex_watchdog.notification_senders import sender_fence
from codex_watchdog.storage import FileLock
from test_notification_fresh_entry import observed, no_history
from test_notification_native_enrollment import NOW, PROVIDERS, database, native, notifier, retained


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("damaged-scope fixture attempted provider network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def subject(current, provider="slack"):
    scope = sha256_text(current.workspace_id)
    thread = ("observer:" + scope if current.source_observer
              else current.source_thread_id)
    return thread, scope, provider


def current_boundary(runtime, current):
    with closing(sqlite3.connect(database(runtime, "slack"))) as db:
        return db.execute("SELECT floor,floor_key,checked FROM boundaries "
            "WHERE thread_id=? AND scope=? AND provider=?", subject(current)).fetchone()


def damaged_scope(runtime, kind, protected):
    calls, clock = [], [NOW]
    current = (observed(born=NOW - 1) if kind == "observer"
               else native(born=NOW - 1))
    sender = notifier(runtime, "slack", clock, calls)
    assert sender.notify(current).status == "sent" and calls == ["slack"]
    assert root.read_notification_root(runtime)["origin"] == finite.FRESH_ORIGIN
    with sender_fence(runtime), FileLock(runtime / "locks/notifications.lock"):
        with sqlite3.connect(database(runtime, "slack")) as db:
            db.execute("UPDATE retention SET protected=? WHERE fingerprint=? AND provider='slack'",
                       (int(protected), current.event_fingerprint()))
            assert db.execute("DELETE FROM boundaries WHERE thread_id=? AND scope=? AND provider=?",
                              subject(current)).rowcount == 1
    row = retained(runtime, current.event_fingerprint(), "slack")
    assert row[6] == int(protected) and current_boundary(runtime, current) is None
    return sender, current, row, calls


@pytest.mark.parametrize("protected", [False, True])
def test_ordinary_observer_maintenance_refuses_orphan_scope_but_enrolls_healthy(
        tmp_path, monkeypatch, protected):
    sender, damaged, row, calls = damaged_scope(tmp_path, "observer", protected)
    healthy = observed(2, born=NOW - 1)
    no_history(monkeypatch)
    result = maintain_observer_receipts(tmp_path,
        scopes=[subject(damaged), subject(healthy)], sender_fence=nullcontext,
        clock=lambda: NOW)
    dual = result["dual"]
    assert dual["scope_refused"] == 1
    assert dual["enrolled"] == 1 and dual["status"] == "partial"
    assert current_boundary(tmp_path, damaged) is None
    assert retained(tmp_path, damaged.event_fingerprint(), "slack") == row
    assert current_boundary(tmp_path, healthy) == (0, "", NOW)
    assert calls == ["slack"]  # Maintenance performs no external action.
    # A different effect in the damaged scope is still refused, while the
    # independently verified healthy scope can use its own first attempt.
    different = replace(damaged, transition_fingerprint="new-unsafe-current-effect")
    assert sender.notify(different).status == "delivery_failed"
    assert sender.notify(healthy).status == "sent"
    assert calls == ["slack", "slack"]
    assert current_boundary(tmp_path, damaged) is None
    assert retained(tmp_path, damaged.event_fingerprint(), "slack") == row


@pytest.mark.parametrize("kind,protected", [("observer", False), ("native", True)])
def test_explicit_cold_projection_refuses_damaged_fresh_scope_without_rebasing(
        tmp_path, kind, protected):
    sender, damaged, row, calls = damaged_scope(tmp_path, kind, protected)
    healthy = observed(2, born=NOW - 1)
    directory = tmp_path / "notifications"
    plan_path = directory / "finite-receipts-preparation.json"
    plan_bytes, ready_bytes = plan_path.read_bytes(), (directory / root.READY).read_bytes()
    plan = json.loads(plan_bytes)
    scopes = list(plan["scopes"]) + [subject(healthy, provider) for provider in PROVIDERS]
    contexts, pins = [], []
    if protected:
        # Exact existing notification metadata is the cold caller projection;
        # this never treats a protected bit as actual native-current proof.
        contexts = [dict(schema_version=1, thread_id=row[2], scope_sha256=row[3],
            born=row[4], fingerprint=damaged.event_fingerprint(), providers={
                "slack": dict(target_sha256=row[7], payload_sha256=row[8])})]
        pins = [(damaged.event_fingerprint(), "slack")]
    with pytest.raises(finite.RetentionError):
        root.prepare_notification_root(tmp_path, scopes=scopes, current_keys=[],
            contexts=contexts, current_pins=pins, sender_fence=nullcontext,
            clock=lambda: NOW)
    assert current_boundary(tmp_path, damaged) is None
    assert retained(tmp_path, damaged.event_fingerprint(), "slack") == row
    assert plan_path.read_bytes() == plan_bytes
    assert (directory / root.READY).read_bytes() == ready_bytes
    assert calls == ["slack"]
    # Failure of this explicit cold action does not revoke an accepted root or
    # block a different verified source. The damaged scope is not reset/replayed.
    assert sender.notify(healthy).status == "sent"
    assert sender.notify(replace(damaged,
        transition_fingerprint="new-unsafe-current-effect")).status == "delivery_failed"
    assert calls == ["slack", "slack"]
    assert current_boundary(tmp_path, damaged) is None
    assert retained(tmp_path, damaged.event_fingerprint(), "slack") == row
