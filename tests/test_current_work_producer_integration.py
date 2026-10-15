"""The actual bounded stream producer composes with pre-prune point pins."""
from contextlib import nullcontext
from datetime import datetime, timezone
import json

import pytest

from codex_watchdog.native_inflight import collect_native_work
from codex_watchdog.native_inflight_pins import CurrentNativePins
from codex_watchdog.native_stream_work import NativeStreamWork
from codex_watchdog.recent_state import prune
from codex_watchdog.reply_tickets import ReplyTickets
from test_native_inflight import (THREAD, TURN, NS, rig, enqueue, remove_queue,
    rows_for, provider_db, association, sha, wrapped)


def current_owner(rig):
    assert rig.held
    return dict(codex_home=str(rig.home), thread_id=THREAD, repo_path=str(rig.repo),
                owner=dict(rig.owner), observed_at=rig.now)


def inventory(rig, producer):
    return collect_native_work(rig.home, THREAD, rig.repo,
        current_native=lambda: producer.observe(THREAD, rig.repo,
            current_owner=lambda: current_owner(rig), fence=nullcontext),
        fence=rig.fence, clock=lambda: rig.now)


def append(rig, kind, **fields):
    path = rig.home / "sessions" / ("rollout-current-" + THREAD + ".jsonl")
    event = dict(type="event_msg",
        timestamp=datetime.fromtimestamp(rig.now, timezone.utc).isoformat().replace("+00:00", "Z"),
        payload=dict(type=kind, thread_id=THREAD, turn_id=TURN, **fields))
    with path.open("a") as stream:
        stream.write(json.dumps(event) + "\n")


def begin_observed_turn(rig, identifier, prompt):
    append(rig, "task_started")
    append(rig, "item_completed", item=dict(type="UserMessage",
        content=[dict(type="text", text=wrapped(identifier, prompt))]))


def register(rig, db, rows, identifier, prompt, producer):
    pins = CurrentNativePins(rig.runtime, rig.home, THREAD, rig.repo, clock=lambda: rig.now)
    proof = inventory(rig, producer)
    db.execute("BEGIN IMMEDIATE")
    pins.register(association(rows, identifier, prompt), proof, db=db, fence=rig.fence)
    db.commit()
    return pins, proof


def test_queue_before_stream_baseline_pins_then_exact_terminal_retires(rig):
    identifier, prompt = "slackbot:" + sha("current-e2e")[:40], "synthetic same-turn work"
    rows = rows_for(identifier, prompt)
    old = rows_for("slackbot:" + sha("historical")[:40], "historical uncertainty", suffix="old")
    enqueue(rig, identifier, prompt)
    producer = NativeStreamWork(rig.home, clock=lambda: rig.now)
    db = provider_db(rig, rows + old)
    try:
        pins, proof = register(rig, db, rows, identifier, prompt, producer)
        assert proof["status"] == "unknown" and proof["queue"]["complete"] is True
        assert proof["native"]["status"] == "unknown"
        before = {(row["namespace"], row["kind"], row["key"]): row["value"] for row in rows}
        journal = ReplyTickets(rig.runtime / NS, rig.runtime / "locks/slack.lock",
            "slack", rig.runtime, lambda: {}, clock=lambda: rig.now)
        prune(journal, db)
        db.commit()
        for row in rows:
            assert db.execute("SELECT value FROM records WHERE namespace=? AND kind=? AND key=?",
                (row["namespace"], row["kind"], row["key"])).fetchone() == (row["value"],)
        # Native consumption alone is not a completed-turn receipt.
        remove_queue(rig)
        db.execute("BEGIN IMMEDIATE")
        assert pins.reconcile(inventory(rig, producer), db=db, fence=rig.fence)["retired"] == 0
        db.commit()
        rig.now += 1
        begin_observed_turn(rig, identifier, prompt)
        active = inventory(rig, producer)
        assert active["status"] == "complete" and active["native"]["instruction_id"] == identifier
        db.execute("BEGIN IMMEDIATE")
        assert pins.reconcile(active, db=db, fence=rig.fence)["retired"] == 0
        db.commit()
        rig.now += 1
        append(rig, "task_complete")
        completed = inventory(rig, producer)
        assert completed["status"] == "complete"
        db.execute("BEGIN IMMEDIATE")
        assert pins.reconcile(completed, db=db, fence=rig.fence)["retired"] == 1
        db.commit()
        assert pins.export()["bindings"] == []
        # GC pin removal changes no uncertain/sent/event receipt outcome.
        for row in rows:
            assert db.execute("SELECT value FROM records WHERE namespace=? AND kind=? AND key=?",
                (row["namespace"], row["kind"], row["key"])).fetchone() == (before[(row["namespace"], row["kind"], row["key"])],)
    finally:
        db.close()


@pytest.mark.parametrize("gap", ["restart", "owner_change"])
def test_historical_terminal_or_changed_owner_cannot_clear_current_pins(rig, gap):
    identifier, prompt = "slackbot:" + sha("current-gap")[:40], "synthetic protected work"
    rows = rows_for(identifier, prompt)
    enqueue(rig, identifier, prompt)
    producer = NativeStreamWork(rig.home, clock=lambda: rig.now)
    db = provider_db(rig, rows)
    try:
        pins, _ = register(rig, db, rows, identifier, prompt, producer)
        rig.now += 1
        begin_observed_turn(rig, identifier, prompt)
        assert inventory(rig, producer)["status"] == "complete"
        remove_queue(rig)
        rig.now += 1
        append(rig, "task_complete")
        if gap == "restart":
            producer = NativeStreamWork(rig.home, clock=lambda: rig.now)
        else:
            rig.owner["instance"] = "new-verified-owner"
        proof = inventory(rig, producer)
        assert proof["status"] == "unknown"
        db.execute("BEGIN IMMEDIATE")
        assert pins.reconcile(proof, db=db, fence=rig.fence)["retired"] == 0
        db.commit()
        assert pins.export()["bindings"]
    finally:
        db.close()
