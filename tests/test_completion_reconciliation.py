"""Completion evidence can settle without boot/epoch admission or task replay."""
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from codex_watchdog import control_state as cs, linux_auto, linux_binding, relay_completion_reconciliation as reconciliation
from codex_watchdog.models import sha256_text
from codex_watchdog.relay_completion_reconciliation import reconcile_existing_native_completions
from codex_watchdog.relay_native_completion import completion_progress
from codex_watchdog.relay_authority import SessionRelayAuthority
from test_linux_node import nodes
from test_relay_authority import (
    LOGIN3, _admit, _admitted_native_fixture, _completed_native_receipt,
    _node, _park, _rows, _stamp,
)


@pytest.fixture
def existing(nodes):
    first, harness, args = _admitted_native_fixture(nodes)
    manifest = cs.control_read_json(first.manifest)
    manifest["plan_sha256"] = cs.control_read_json(first.root / "cluster.json")["plan_sha256"]
    cs.control_atomic_json(first.manifest, manifest)
    receipt = _completed_native_receipt(first, args)
    first.record_native_delivery(harness.store, args["event_key"], receipt)
    harness.store.finish_reply(args["event_key"], state_value="delivered", delivery_status="enqueued")
    # Real normal observation has this existing cursor namespace. The proof-only
    # path must not create it, schema migrations, markers or sources itself.
    first.write_cursor("slack", "poll", {"future_field": {"keep": True}, "after": "fixture"})
    _park(nodes, first)
    return first, harness, args, receipt


def protected_rows(database):
    return [row for row in _rows(database)
            if row[1] not in ("events", "relay_native_receipts", "relay_reconcile_cursor")]


def event_row(database, event_key):
    key = sha256_text(event_key)
    return next(row for row in _rows(database) if row[1:3] == ("events", key))


def test_changed_boot_without_writer_settles_only_original_receipt(existing, nodes, monkeypatch):
    first, harness, args, receipt = existing
    home, repo, _, _, pid = nodes
    changed = cs.ControlStore(home, first.thread_id, repo, clock=lambda: 200, boot_id="new-boot")
    assert pid[0] is None
    assert changed.claim_remote("new-dog", "locality", "absent", host_observer=True) is None
    before = protected_rows(harness.store.journal.database)
    files = {path: path.read_bytes() for path in
             (first.store.path, first.observation.path, first.manifest,
              first.root / "cluster.json", home / "queue_1.sqlite", home / "state_5.sqlite")}
    def forbidden(*args, **kwargs):
        pytest.fail("completion-only invoked an ownership or execution API")
    monkeypatch.setattr(changed, "claim_remote", forbidden)
    monkeypatch.setattr(first, "activate", forbidden)
    result = reconcile_existing_native_completions(changed)
    assert result["examined"] == result["completed"] == 1
    original = json.loads(event_row(harness.store.journal.database, args["event_key"])[3])
    assert original["native_completed"] is True
    assert original["instruction_id"] == receipt["instruction_id"]
    assert original["native_completion_turn_id"] == "44444444-4444-4444-8444-444444444444"
    assert protected_rows(harness.store.journal.database) == before
    assert {path: path.read_bytes() for path in files} == files
    assert changed.read()["native_boot_id"] != changed.boot_id
    assert reconcile_existing_native_completions(changed)["completed"] == 0
    # Existing UUID/physical claims and one-shot closed ticket remain durable.
    assert harness.store.claim_reply(**args) == (False, "enqueued")


def test_completed_relay_preserves_independent_old_git_wake_without_handoff_gate(existing, nodes):
    first, harness, _, _ = existing
    with first.store.guard(first.token) as value:
        value["remote_state"] = dict(pending_instruction_id="git:fixture:24f2", future_choice={"keep": True})
        cs.control_atomic_json(first.store.path, value)
    assert _admit(first, nodes[0])
    saved_owner, saved_observer = first.store.path.read_bytes(), first.observation.path.read_bytes()
    assert reconcile_existing_native_completions(first.store)["completed"] == 1
    candidate, _ = _node(nodes, LOGIN3, activate=False, clock=200, instance="replacement")
    assert _admit(candidate, nodes[0])
    assert first.store.path.read_bytes() == saved_owner
    assert first.store.read()["remote_state"] == dict(
        pending_instruction_id="git:fixture:24f2", future_choice={"keep": True})
    current = cs.control_read_json(candidate.observation.path)
    assert current["node"] == candidate.observation.node
    assert current["relay_epoch"] == json.loads(saved_observer)["relay_epoch"] + 1
    assert candidate.expected is not None


def test_exact_settlement_enables_only_normal_verified_handoff(existing, nodes):
    first, harness, args, _ = existing
    saved_owner = first.store.path.read_bytes()
    assert reconcile_existing_native_completions(first.store)["completed"] == 1
    candidate, current = _node(nodes, LOGIN3, activate=False, clock=200, instance="replacement")
    assert candidate.expected is None
    assert _admit(candidate, nodes[0])
    assert candidate.expected["relay_epoch"] == first.expected["relay_epoch"] + 1
    assert first.store.path.read_bytes() == saved_owner
    assert current.store.claim_reply(**args) == (False, "enqueued")


@pytest.mark.parametrize("problem", ["missing-db", "schema", "cursor-namespace", "index", "full-index", "manifest", "observer"])
def test_existing_state_refusals_never_create_or_repair_state(existing, problem):
    first, harness, _, _ = existing
    database = harness.store.journal.database
    if problem == "missing-db":
        cluster = cs.control_read_json(first.root / "cluster.json")
        cluster["providers"] = ["slack"]
        cs.control_atomic_json(first.root / "cluster.json", cluster)
        database.unlink()
    elif problem == "schema":
        with sqlite3.connect(database) as db:
            db.execute("PRAGMA user_version=1")
    elif problem == "cursor-namespace":
        with sqlite3.connect(database) as db:
            db.execute("DELETE FROM sources WHERE namespace='slack/authority-cursors.json'")
    elif problem in ("index", "full-index"):
        with sqlite3.connect(database) as db:
            db.execute("DROP INDEX session_relay_pending")
            if problem == "full-index":
                db.execute("CREATE INDEX session_relay_pending ON records(thread_id,namespace,key)")
    elif problem == "manifest":
        first.manifest.unlink()
    else:
        first.observation.path.unlink()
    before = {path: path.read_bytes() for path in first.root.rglob("*") if path.is_file()}
    owner = first.store.path.read_bytes()
    if problem in ("manifest", "observer"):
        with pytest.raises(cs.ControlError):
            reconcile_existing_native_completions(first.store)
    else:
        assert reconcile_existing_native_completions(first.store)["refusals"]
    assert {path: path.read_bytes() for path in first.root.rglob("*") if path.is_file()} == before
    assert first.store.path.read_bytes() == owner
    assert not database.with_name(database.name + ".v1-backup").exists()


@pytest.mark.parametrize("linked", ["database", "manifest", "rollout"])
def test_symlinked_evidence_is_refused(existing, linked):
    first, harness, args, receipt = existing
    path = (harness.store.journal.database if linked == "database" else
            first.manifest if linked == "manifest" else Path(receipt["rollout_path"]))
    saved = path.with_name(path.name + ".original")
    path.rename(saved)
    try:
        path.symlink_to(saved)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable (WinError 1314)")
        raise
    original_event = event_row(harness.store.journal.database, args["event_key"])
    if linked == "rollout":
        assert reconcile_existing_native_completions(first.store)["completed"] == 0
    else:
        if linked == "manifest":
            with pytest.raises(cs.ControlError):
                reconcile_existing_native_completions(first.store)
        else:
            assert reconcile_existing_native_completions(first.store)["refusals"]
    assert event_row(harness.store.journal.database, args["event_key"]) == original_event


@pytest.mark.parametrize("mismatch", ["thread", "instruction", "prompt", "queue", "baseline", "proof-thread"])
def test_exact_immutable_native_proof_is_required(existing, mismatch):
    first, harness, args, _ = existing
    journal, key = harness.store.journal, sha256_text(args["event_key"])
    with journal.transaction() as db:
        proof = journal.get(db, "relay_native_receipts", key)
        field, value = {"thread": ("thread_id", "22222222-2222-4222-8222-222222222222"),
            "instruction": ("instruction_id", "slackbot:" + "a" * 40),
            "prompt": ("prompt_sha256", "f" * 64),
            "queue": ("queue_message_id", "22222222-2222-4222-8222-222222222222"),
            "baseline": ("rollout_baseline_offset", 1),
            "proof-thread": ("thread_id", "22222222-2222-4222-8222-222222222222")}[mismatch]
        (proof if mismatch == "proof-thread" else proof["receipt"])[field] = value
        journal.put(db, "relay_native_receipts", key, proof)
    original = event_row(journal.database, args["event_key"])
    assert reconcile_existing_native_completions(first.store)["completed"] == 0
    assert event_row(journal.database, args["event_key"]) == original
    assert first.handoff_safe()  # Refused proof does not gate independent work.


@pytest.mark.parametrize("problem", ["queued", "queue-missing", "wrong-turn", "incomplete", "after-terminal", "replaced"])
def test_nonterminal_or_uncertain_native_state_does_not_clear_event(existing, nodes, problem):
    first, harness, args, receipt = existing
    path = Path(receipt["rollout_path"])
    if problem == "queued":
        with sqlite3.connect(nodes[0] / "queue_1.sqlite") as db:
            db.execute("INSERT INTO queued_items VALUES (?,?,?)",
                       (receipt["queue_message_id"], first.thread_id, "{}"))
    elif problem == "queue-missing":
        (nodes[0] / "queue_1.sqlite").unlink()
    elif problem in ("wrong-turn", "incomplete"):
        events = path.read_text().splitlines(keepends=True)
        if problem == "incomplete":
            path.write_text(events[0])
        else:
            terminal = json.loads(events[1])
            terminal["payload"]["turn_id"] = "55555555-5555-4555-8555-555555555555"
            path.write_text(events[0] + json.dumps(terminal) + "\n")
    elif problem == "after-terminal":
        with path.open("a") as stream:
            stream.write(path.read_text().splitlines(keepends=True)[0])
    else:
        old = path.with_name(path.name + ".old")
        path.rename(old)
        path.write_bytes(old.read_bytes())
    original = event_row(harness.store.journal.database, args["event_key"])
    assert reconcile_existing_native_completions(first.store)["completed"] == 0
    assert event_row(harness.store.journal.database, args["event_key"]) == original


def test_linux_auto_standby_preserves_optional_completion_evidence(existing, nodes, monkeypatch):
    first, harness, args, _ = existing
    home, repo, host, roots, pid = nodes
    pid[0] = None
    changed = cs.ControlStore(home, first.thread_id, repo, clock=lambda: 200, boot_id="new-boot")
    monkeypatch.setattr(linux_auto, "locality_identity", lambda: "fixture-host")
    monkeypatch.setattr(linux_auto, "writer_pid", lambda *args: None)
    monkeypatch.setattr(linux_auto, "vscode_writer", lambda *args: False)
    dog = linux_auto.LinuxAutoWatchdog(cs.control_state_home(home) / "runtime", home,
        store_factory=lambda *args: changed,
        owner_factory=lambda *args, **kwargs: pytest.fail("created native owner"),
        service_factory=lambda *args, **kwargs: pytest.fail("created provider service"))
    monkeypatch.setattr(dog, "_discover_native", lambda: [])
    owner, observer = first.store.path.read_bytes(), first.observation.path.read_bytes()
    before = _rows(harness.store.journal.database)
    assert dog.step()[0]["state"] == "standby"
    assert json.loads(event_row(harness.store.journal.database, args["event_key"])[3]).get("native_completed") is not True
    assert _rows(harness.store.journal.database) == before
    assert not dog.controllers
    assert first.store.path.read_bytes() == owner and first.observation.path.read_bytes() == observer


@pytest.mark.parametrize("invalid", ["cluster-schema-bool", "manifest-schema-bool", "plan"])
def test_cluster_and_manifest_schema_and_plan_must_match(existing, invalid):
    first, harness, _, _ = existing
    path = first.root / "cluster.json" if invalid == "cluster-schema-bool" else first.manifest
    value = cs.control_read_json(path)
    value["plan_sha256" if invalid == "plan" else "schema_version"] = "b" * 64 if invalid == "plan" else True
    cs.control_atomic_json(path, value)
    before = _rows(harness.store.journal.database)
    with pytest.raises(cs.ControlError, match="manifest_invalid"):
        reconcile_existing_native_completions(first.store)
    assert _rows(harness.store.journal.database) == before


def test_unknown_evidence_and_cursor_fields_survive(existing):
    first, harness, args, _ = existing
    journal, key = harness.store.journal, sha256_text(args["event_key"])
    with journal.transaction() as db:
        for kind in ("events", "relay_native_receipts"):
            value = journal.get(db, kind, key)
            value["future_evidence"] = {"retain": [1, 2, 3]}
            journal.put(db, kind, key, value)
    cursor = first.journal("slack")
    with cursor.transaction() as db:
        cursor.put(db, "relay_reconcile_cursor", first.thread_id,
                   dict(after=["", ""], thread_id=first.thread_id, future_evidence={"keep": True}))
    assert reconcile_existing_native_completions(first.store)["completed"] == 1
    with journal.transaction() as db:
        assert journal.get(db, "events", key)["future_evidence"] == {"retain": [1, 2, 3]}
        assert journal.get(db, "relay_native_receipts", key)["future_evidence"] == {"retain": [1, 2, 3]}
    with cursor.transaction() as db:
        assert cursor.get(db, "relay_reconcile_cursor", first.thread_id)["future_evidence"] == {"keep": True}


def _copy_provider(first, source, provider):
    destination = first.runtime / provider / "reply-tickets.sqlite3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as old, sqlite3.connect(destination) as current:
        old.backup(current)
        current.execute("UPDATE sources SET namespace=replace(namespace,'slack/','" + provider + "/')")
        current.execute("UPDATE records SET namespace=replace(namespace,'slack/','" + provider + "/')")
    (first.runtime / "locks" / (provider + "-authority.lock")).touch()
    return destination


def test_empty_compatible_provider_needs_no_cursor_namespace_or_new_lock(existing):
    first, harness, _, _ = existing
    empty = _copy_provider(first, harness.store.journal.database, "lark")
    with sqlite3.connect(empty) as db:
        db.execute("DELETE FROM records")
        db.execute("DELETE FROM sources WHERE namespace='lark/authority-cursors.json'")
    lock = first.runtime / "locks/lark-authority.lock"
    lock.unlink()
    before = empty.read_bytes()
    result = reconcile_existing_native_completions(first.store)
    assert result["completed"] == 1 and not result["refusals"]
    assert empty.read_bytes() == before and not lock.exists()


def test_refused_provider_does_not_starve_other_existing_provider(existing):
    first, harness, _, _ = existing
    lark = _copy_provider(first, harness.store.journal.database, "lark")
    with sqlite3.connect(harness.store.journal.database) as db:
        db.execute("DELETE FROM sources WHERE namespace='slack/authority-cursors.json'")
    before = _rows(harness.store.journal.database)
    result = reconcile_existing_native_completions(first.store)
    assert result["completed"] == 1
    assert result["refusals"] == [dict(provider="slack", reason="relay_completion_namespace_missing")]
    assert _rows(harness.store.journal.database) == before
    assert any(row[1] == "events" and json.loads(row[3]).get("native_completed") is True for row in _rows(lark))


@pytest.mark.parametrize("problem", ["missing-namespace", "recursive-proof", "large-proof", "duplicate-proof"])
def test_bad_parent_refuses_and_rotates_without_starving_valid_parent(existing, problem, monkeypatch):
    first, harness, args, _ = existing
    journal = harness.store.journal
    with journal.transaction() as db:
        entry = journal.get(db, "events", sha256_text(args["event_key"]))
        proof = journal.get(db, "relay_native_receipts", sha256_text(args["event_key"]))
        namespace = "!missing-source" if problem == "missing-namespace" else "!existing-source"
        if problem != "missing-namespace":
            db.execute("INSERT INTO sources VALUES (?)", (namespace,))
        raw_proof = json.dumps(proof)
        if problem == "recursive-proof":
            # Native JSON recursion limits differ among supported Pythons;
            # inject that exact bounded decoder failure, not a platform skip.
            raw_proof = raw_proof[:-1] + ',"future_evidence":' + '[' * 100 + '0' + ']' * 100 + '}'
            decoder = json.loads
            def decode(value, *args, **kwargs):
                if value == raw_proof:
                    raise RecursionError("fixture decoder limit")
                return decoder(value, *args, **kwargs)
            monkeypatch.setattr(json, "loads", decode)
        elif problem == "large-proof":
            raw_proof = raw_proof[:-1] + ',"future_evidence":"' + 'x' * 131072 + '"}'
        elif problem == "duplicate-proof":
            raw_proof = raw_proof[:-1] + ',"thread_id":' + json.dumps(first.thread_id) + '}'
        for index in range(4):
            key = "0" * 63 + str(index)
            db.execute("INSERT INTO records(namespace,kind,key,value,thread_id,active) VALUES(?,?,?,?,?,1)",
                       (namespace, "events", key, json.dumps(entry), first.thread_id))
            db.execute("INSERT INTO records(namespace,kind,key,value,thread_id) VALUES(?,?,?,?,?)",
                       (namespace, "relay_native_receipts", key, raw_proof, first.thread_id))
    result = reconcile_existing_native_completions(first.store)
    assert result["completed"] == 0
    if problem == "missing-namespace":
        assert len(result["refusals"]) == 4
    assert event_row(journal.database, args["event_key"])[5] == 1
    result = reconcile_existing_native_completions(first.store)
    assert result["completed"] == 1
    assert all(row[5] == 1 for row in _rows(journal.database) if row[0] == namespace and row[1] == "events")
    with sqlite3.connect(journal.database) as db:
        assert db.execute("SELECT 1 FROM sources WHERE namespace='!missing-source'").fetchone() is None


@pytest.mark.parametrize("value", [None, [], {"thread_id": "foreign", "after": ["", ""]},
    {"after": ["", ""]}, {"after": None}, {"after": 3}, {"after": ["only-one"]}])
def test_incompatible_present_cursor_is_refused_without_repair(existing, value):
    first, harness, _, _ = existing
    if (isinstance(value, dict) and "thread_id" not in value
            and value.get("after") != ["", ""]):
        value = dict(value, thread_id=first.thread_id)
    with sqlite3.connect(harness.store.journal.database) as db:
        db.execute("INSERT INTO records(namespace,kind,key,value,thread_id) VALUES(?,?,?,?,?)",
                   ("slack/authority-cursors.json", "relay_reconcile_cursor", first.thread_id,
                    json.dumps(value), first.thread_id))
    before = _rows(harness.store.journal.database)
    result = reconcile_existing_native_completions(first.store)
    assert result["completed"] == 0
    assert result["refusals"] == [dict(provider="slack", reason="relay_completion_cursor_invalid")]
    assert _rows(harness.store.journal.database) == before


def test_host_cycle_leaves_historical_proofs_for_explicit_forensic_api(existing, nodes, monkeypatch):
    first, harness, args, receipt = existing
    journal = harness.store.journal
    with journal.transaction() as db:
        key = sha256_text(args["event_key"])
        original = journal.get(db, "events", key)
        original_proof = journal.get(db, "relay_native_receipts", key)
        for index in range(7):
            # Active-history structural fixture; all rows remain original
            # indexed admissions with immutable, fully validated native proof.
            journal.put(db, "events", sha256_text("pending-" + str(index)), original)
            journal.put(db, "relay_native_receipts", sha256_text("pending-" + str(index)), original_proof)
    _copy_provider(first, journal.database, "lark")
    _copy_provider(first, journal.database, "onebot")
    home, repo, host, _, pid = nodes
    pid[0] = 123
    monkeypatch.setattr(linux_auto, "locality_identity", lambda: "fixture-host")
    monkeypatch.setattr(linux_binding, "locality_identity", lambda: "fixture-host")
    monkeypatch.setattr(linux_auto, "writer_pid", lambda *args: 123)
    monkeypatch.setattr(linux_auto, "vscode_writer", lambda *args: True)
    current = cs.ControlStore(home, first.thread_id, repo, clock=lambda: 400, boot_id=first.store.boot_id)
    class Owner:
        client = None
        release_requested = False
        rollout_stamp = _stamp(home)
        def step(self, **kwargs):
            return dict(owner_state="observing", thread_status="idle")
        def report_observation_health(self):
            pass
    config = SimpleNamespace(slack_configured=True)
    def service(*args, **kwargs):
        return SimpleNamespace(notifier=SimpleNamespace(config=config), slack_reply_relay=None)
    dog = linux_auto.LinuxAutoWatchdog(cs.control_state_home(home) / "runtime", home,
        store_factory=lambda *args: current, owner_factory=lambda *args, **kwargs: Owner(),
        service_factory=service)
    monkeypatch.setattr(dog, "_discover_native", lambda: [])
    calls = []
    foreground = []
    monkeypatch.setattr(dog, "_cycle", lambda item: foreground.append(len(calls)) or SimpleNamespace(status="completed"))
    def counted(receipt, progress=None, budget=65536):
        calls.append(budget)
        return completion_progress(receipt, progress, budget)
    monkeypatch.setattr(reconciliation, "completion_progress", counted)
    monkeypatch.setattr(reconciliation, "reconcile_existing_native_completions",
                        lambda store: pytest.fail("routine cycle scanned historical proof"))
    monkeypatch.setattr(SessionRelayAuthority, "reconcile_completions",
                        lambda self: pytest.fail("duplicate normal completion scan"))
    try:
        result = dog.step()
        assert result[0]["state"] == "observing", result
        assert calls == []
        calls.clear()
        assert dog.step()[0]["state"] == "observing"
        assert calls == []
        assert foreground == [0, 0]  # Neither cycle waits for old proof parsing.
    finally:
        for item in dog.controllers.values():
            item["locks"].close()
