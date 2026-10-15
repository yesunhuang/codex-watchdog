"""Historical unknown outcomes retain deduplication without gating fresh work."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
from threading import Event
from types import SimpleNamespace
import urllib.request
import uuid

import pytest

from codex_watchdog import control_state as cs, linux_auto, linux_binding
from codex_watchdog.models import sha256_text
from codex_watchdog.relay import ExactThreadRelay
from codex_watchdog.relay_authority import SessionRelayAuthority
from codex_watchdog.slack_bot_acl import BotRequestReused
from test_linux_node import nodes
from test_linux_auto import queue_native_reply, save_native_reply_receipt, scenario
from test_relay_authority import (
    LOGIN3, _admitted_native_fixture, _completed_native_receipt, _grant,
    _grow_history, _node, _park, _rows, _stamp, _target,
)


@pytest.fixture(autouse=True)
def forbid_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("relay liveness fixtures must not contact providers or execute commands")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


@pytest.mark.parametrize("provider", ("slack", "lark", "onebot"))
@pytest.mark.parametrize("outcome", ("dispatching", "uncertain", "enqueued", "consumed_or_started", "started"))
def test_historical_provider_outcome_is_not_a_native_handoff_barrier(nodes, provider, outcome):
    authority, _ = _node(nodes)
    journal = authority.journal(provider)
    with journal.transaction() as db:
        journal.put(db, "events", "a" * 64, dict(
            instruction_id=provider + ":historical", text_sha256="b" * 64,
            state=outcome if outcome in ("dispatching", "uncertain") else "delivered",
            delivery_status=None if outcome == "dispatching" else outcome,
            unknown_audit={"retain": True}))
    before = _rows(journal.database)
    assert authority.handoff_safe()
    assert _rows(journal.database) == before


@pytest.mark.parametrize("provider", ("slack", "lark", "onebot"))
@pytest.mark.parametrize("fault", ("event_json", "proof_json", "oversized_event", "oversized_proof", "callback"))
def test_optional_reconciliation_rotates_bad_rows_and_preserves_unknown_history(nodes, monkeypatch, provider, fault):
    from codex_watchdog import relay_completion_reconciliation as reconciliation
    authority, _ = _node(nodes)
    journal = _grow_history(authority, provider, 0, 10000, 10000)
    good_key, bad_key = "{:064x}".format(2), "{:064x}".format(0)
    receipt = _completed_native_receipt(authority,
        dict(instruction_id="slackbot:stress-2", text="bounded fixture"))
    from codex_watchdog.relay_native_completion import completion_progress
    progress, _ = completion_progress(receipt, budget=32)
    with journal.transaction() as db:
        journal.put(db, "relay_native_receipts", good_key,
                    dict(thread_id=authority.thread_id, receipt=receipt, progress=progress, unknown={"keep": True}))
        raw = "not JSON" if "json" in fault else json.dumps(dict(oversized="x" * 131074))
        if fault == "callback":
            raw = json.dumps(dict(receipt="invalid"))
        kind = "events" if "event" in fault else "relay_native_receipts"
        db.execute("INSERT INTO records(namespace,kind,key,value,thread_id,active) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value",
            (journal.namespace, kind, bad_key, raw, authority.thread_id, int(kind == "events")))
    bad = next(row for row in _rows(journal.database) if row[1:3] == (kind, bad_key))
    parse = reconciliation._settle
    calls = []
    def tracked(db, store, namespace, key, raw, budget):
        calls.append((key, len(raw), budget))
        if fault == "callback" and key == bad_key:
            raise RuntimeError("fixture private details must not be exported")
        return parse(db, store, namespace, key, raw, budget)
    monkeypatch.setattr(reconciliation, "_settle", tracked)
    result = authority.reconcile_completions()
    assert result["examined"] == 4 and result["completed"] == 1
    assert len(calls) == 4 and all(width <= 131073 and budget == 2 * 1024 * 1024 for _, width, budget in calls)
    assert next(row for row in _rows(journal.database) if row[1:3] == (kind, bad_key)) == bad
    with journal.transaction() as db:
        assert journal.get(db, "events", good_key)["native_completed"] is True
        assert journal.get(db, "relay_native_receipts", good_key)["unknown"] == {"keep": True}
    calls.clear()
    authority.reconcile_completions()
    assert len(calls) == 4 and all(key > "{:064x}".format(3) for key, _, _ in calls)
    assert authority.handoff_safe()


@pytest.mark.parametrize("fault", ("cursor_json", "cursor_oversized", "callback", "commit"))
def test_optional_bad_provider_cannot_starve_other_provider_completion(nodes, monkeypatch, fault):
    from codex_watchdog import relay_completion_reconciliation as reconciliation
    authority, _ = _node(nodes)
    slack = _grow_history(authority, "slack", 0, 4, 10000)
    lark = _grow_history(authority, "lark", 0, 4, 10000)
    receipt = _completed_native_receipt(authority,
        dict(instruction_id="slackbot:stress-2", text="bounded fixture"))
    from codex_watchdog.relay_native_completion import completion_progress
    progress, _ = completion_progress(receipt, budget=32)
    for journal in (slack, lark):
        with journal.transaction() as db:
            journal.put(db, "relay_native_receipts", "{:064x}".format(2),
                dict(thread_id=authority.thread_id, receipt=receipt, progress=progress))
    if fault.startswith("cursor_"):
        cursor = "invalid" if fault == "cursor_json" else json.dumps(dict(huge="x" * 131074))
        current = authority.journal("slack")
        with current.transaction() as db:
            db.execute("INSERT INTO records(namespace,kind,key,value,thread_id) VALUES(?,?,?,?,?)",
                (current.namespace, "relay_reconcile_cursor", authority.thread_id, cursor, authority.thread_id))
    else:
        original = SessionRelayAuthority.journal
        def failed_journal(self, provider):
            journal = original(self, provider)
            if provider == "slack":
                if fault == "callback":
                    raise OSError("fixture private details")
                put = journal.put
                def failed_cursor(db, kind, key, value):
                    if kind == "relay_reconcile_cursor":
                        raise OSError("commit must not be reported as successful proof settlement")
                    return put(db, kind, key, value)
                journal.put = failed_cursor
            return journal
        monkeypatch.setattr(SessionRelayAuthority, "journal", failed_journal)
    before = _rows(slack.database)
    result = authority.reconcile_completions()
    assert result["examined"] == 4 and result["completed"] == 1
    assert result["refusals"] == [dict(provider="slack", reason="relay_completion_journal_unavailable")]
    assert _rows(slack.database) == before
    good = next(row for row in _rows(lark.database) if row[1:3] == ("events", "{:064x}".format(2)))
    assert json.loads(good[3])["native_completed"] is True
    assert authority.handoff_safe()


@pytest.fixture
def host_forensics(nodes, monkeypatch):
    first, _ = _node(nodes)
    home, repo = nodes[:2]
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
    try:
        yield dog, current
    finally:
        for item in dog.controllers.values():
            item["locks"].close()


def test_host_cycle_never_runs_historical_forensics(host_forensics, monkeypatch):
    from codex_watchdog import relay_completion_reconciliation as reconciliation, remote_control
    dog, store = host_forensics
    calls = []
    def cycle(item):
        calls.append("foreground")
        return SimpleNamespace(status="completed")
    def forensic(current):
        assert current is store
        calls.append("forensic")
        pytest.fail("routine work entered an optional historical reader")
    monkeypatch.setattr(dog, "_cycle", cycle)
    monkeypatch.setattr(reconciliation, "reconcile_existing_native_completions", forensic)
    monkeypatch.setattr(remote_control, "reconcile_local_notifications", forensic)
    monkeypatch.setattr(SessionRelayAuthority, "reconcile_completions", forensic)
    for _ in range(2):
        result = dog.step()
        assert result[0]["state"] == "observing"
        assert "forensics" not in result[0]
        assert dog.controllers[store.thread_id].get("failed") is not True
    assert calls == ["foreground", "foreground"]


@pytest.mark.parametrize("scenario", [True], indirect=True)
def test_foreign_selected_observer_refuses_before_native_resume(scenario):
    store, _, writer, _, clients, make_agent = scenario
    dog = make_agent()
    assert dog.step(observe=False)[0]["state"] == "observing"
    item = dog.controllers[store.thread_id]
    observation = item["observation"]
    selected = cs.control_read_json(observation.path)
    selected.update(node="node-b.example", boot_id="foreign-boot", writer_live=False)
    selected["relay_owner"].update(node=selected["node"], boot_id=selected["boot_id"])
    cs.control_atomic_json(observation.path, selected)
    before = observation.path.read_bytes()
    writer[0] = None

    result = dog.step(observe=False)[0]

    assert result["state"] == "standby"
    assert result["observation"] == "control_node_observed_elsewhere"
    assert clients == [] and writer[0] is None
    assert item["owner"].resume_requested is False
    assert observation.path.read_bytes() == before
    assert item["owner"].binding.load()["state"] == "armed"


@pytest.mark.parametrize("scenario", [True], indirect=True)
def test_own_native_resume_and_queued_restart_hold_shared_admission(scenario, monkeypatch):
    from codex_watchdog.node_observation import observation_lock
    store, _, writer, _, clients, make_agent = scenario
    dog = make_agent()
    assert dog.step(observe=False)[0]["state"] == "observing"
    item = dog.controllers[store.thread_id]
    owner, observation = item["owner"], item["observation"]
    factory = owner.client_factory
    admitted_resumes = []

    def checked_client(*args, **kwargs):
        client = factory(*args, **kwargs)
        request = client.request

        def checked_request(method, parameters):
            if method == "thread/resume":
                assert observation._admitted

                def competing_admission():
                    with pytest.raises(cs.ControlBusy, match="observation_busy"):
                        with observation_lock(observation.lock_path):
                            pytest.fail("native resume must hold the shared admission lock")

                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(competing_admission).result(timeout=2)
                admitted_resumes.append(parameters["threadId"])
            return request(method, parameters)

        client.request = checked_request
        return client

    monkeypatch.setattr(owner, "client_factory", checked_client)
    writer[0] = None
    assert dog.step(observe=False)[0]["state"] == "owned"
    assert cs.control_read_json(observation.path)["writer_live"] is True
    owner._idle_since = -1000000
    assert dog.step(observe=False)[0]["state"] == "parked"
    assert clients[0].closed and cs.control_read_json(observation.path)["writer_live"] is False

    queue_id = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
    receipt = queue_native_reply(store, queue_id)
    path = save_native_reply_receipt(store, receipt)
    original = path.read_bytes()
    assert dog.step(observe=False)[0]["state"] == "parked"
    # Explicit compatibility import uses only actual Q and its exact old receipt.
    assert cs.control_import_current_couriers(store.codex_home, store.thread_id)["imported"] == 1
    assert cs.control_import_current_couriers(store.codex_home, store.thread_id)["imported"] == 0

    assert dog.step(observe=False)[0]["state"] == "owned"
    assert admitted_resumes == [store.thread_id, store.thread_id]
    assert cs.control_read_json(observation.path)["writer_live"] is True
    assert path.read_bytes() == original
    with sqlite3.connect(store.codex_home / "queue_1.sqlite") as db:
        assert db.execute("SELECT id FROM queued_items WHERE thread_id=?", (store.thread_id,)).fetchall() == [(queue_id,)]

@pytest.mark.parametrize("outcome", ("dispatching", "uncertain", "enqueued", "consumed_or_started", "started"))
def test_native_executor_change_preserves_old_reservation_and_admits_independent_command(nodes, outcome):
    first, old = _node(nodes)
    target = _target(first)
    _grant(old, target)
    source = old.source(target)
    request = str(uuid.uuid4())
    args = old.admission(source, target=target, request=request)
    assert old.store.claim_reply(**args) == (True, None)
    if outcome != "dispatching":
        old.store.finish_reply(args["event_key"], state_value="uncertain" if outcome == "uncertain" else "delivered",
                               delivery_status=outcome)
    original = old.store.lookup_reply(args["event_key"])
    original_owner = first.store.path.read_bytes()
    principals = old.acl.principals(first.thread_id)
    _park(nodes, first)
    current, new = _node(nodes, LOGIN3)
    assert current.expected["relay_epoch"] == first.expected["relay_epoch"] + 1
    assert new.store.claim_reply(**args) == (False, original["delivery_status"])
    assert new.store.claim_reply(**dict(args, event_key="changed-retry-envelope")) == (False, original["delivery_status"])
    assert new.store.lookup_reply(args["event_key"]) == original
    new.sequence = old.sequence
    fresh_source = new.source(target)
    before = _rows(new.store.journal.database)
    with pytest.raises(BotRequestReused):
        new.store.claim_reply(**new.admission(fresh_source, target=target, request=request, suffix="logical-repeat"))
    assert _rows(new.store.journal.database) == before
    fresh = new.admission(fresh_source, target=target, request=str(uuid.uuid4()), suffix="independent")
    assert new.store.claim_reply(**fresh) == (True, None)
    assert new.acl.principals(current.thread_id) == principals
    assert first.store.path.read_bytes() == original_owner
    assert new.store.lookup_reply(args["event_key"]) == original


@pytest.mark.parametrize("history", (0, 10000))
def test_handoff_checks_native_fifo_without_reading_provider_history(nodes, monkeypatch, history):
    authority, _ = _node(nodes)
    originals = {}
    for provider in ("slack", "lark", "onebot"):
        journal = _grow_history(authority, provider, 0, history, history)
        originals[journal.database] = _rows(journal.database)
    connections = []
    connect = sqlite3.connect
    def native_only(path, *args, **kwargs):
        connections.append(str(path))
        assert "reply-tickets.sqlite3" not in str(path), "historical provider journal gated native handoff"
        return connect(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", native_only)
        assert authority.handoff_safe()
    assert len(connections) == 1 and "queue_1.sqlite" in connections[0]
    assert {database: _rows(database) for database in originals} == originals


def test_current_relay_epoch_survives_unrelated_native_log_append(nodes):
    authority, harness = _node(nodes)
    target = _target(authority)
    _grant(harness, target)
    source = harness.source(target)
    _park(nodes, authority)
    rollout = nodes[0] / "sessions/thread.jsonl"
    with rollout.open("ab") as stream:
        stream.write(b'{"type":"unrelated_native_audit"}\n')
    authority.write_cursor("slack", "current", {"future_field": "retained"})
    assert harness.store.claim_reply(**harness.admission(source, target=target, request=str(uuid.uuid4()))) == (True, None)
    assert authority.read_cursor("slack", "current", {}) == {"future_field": "retained"}


@pytest.mark.parametrize("fault", ("missing", "malformed", "wrong_session", "oversized"))
@pytest.mark.skipif(os.name == "nt", reason="Linux-native authority fixture uses POSIX repository paths")
def test_native_proof_capture_fault_preserves_admitted_dispatch_control(nodes, fault):
    authority, harness, args = _admitted_native_fixture(nodes)
    receipt = _completed_native_receipt(authority, args)
    if fault == "missing":
        receipt.pop("queue_message_id")
    elif fault == "malformed":
        receipt["rollout_baseline_offset"] = True
    elif fault == "wrong_session":
        receipt["thread_id"] = "22222222-2222-4222-8222-222222222222"
    else:
        receipt["rollout_path"] = "x" * 131073
    calls = []
    token = deepcopy(authority.token)
    def probe(target, **kwargs):
        calls.append((target, kwargs))
        return dict(status="ok", wake=receipt, control=dict(token=token))
    relay = ExactThreadRelay()
    relay.remote_ssh_adapter = SimpleNamespace(supports_control=True, probe=probe)
    relay.relay_authority = authority
    relay.thread_store = harness.store
    relay.runtime = Path(authority.store.read()["runtime_path"])
    relay.queue_dispatcher = None
    mapping = SimpleNamespace(target=_target(authority), channel_id=args["channel_id"], thread_ts=args["thread_ts"])
    before = harness.store.lookup_reply(args["event_key"])
    result = relay._controlled_reply(mapping, args["event_key"], args["instruction_id"], args["text"],
                                     check_legacy_receipt=False)
    assert result.status == "queued" and result.delivery_status == "enqueued"
    assert result.control[2] == token and len(calls) == 1
    harness.store.finish_reply(args["event_key"], state_value="delivered", delivery_status="enqueued")
    assert harness.store.claim_reply(**args) == (False, "enqueued")
    assert len(calls) == 1
    current = harness.store.lookup_reply(args["event_key"])
    assert current["instruction_id"] == before["instruction_id"] and current.get("native_completed") is not True
    assert authority.handoff_safe()


@pytest.mark.parametrize("mode", ("socket", "poll"))
@pytest.mark.parametrize("fault", ("malformed_proof", "capture_storage", "lost_ack"))
@pytest.mark.skipif(os.name == "nt", reason="Linux-native authority fixture uses POSIX repository paths")
def test_actual_bot_ingress_keeps_one_admission_and_one_feedback_attempt(nodes, monkeypatch, mode, fault):
    from codex_watchdog import relay as relay_module
    from test_slack_bot_relay import Rig, instruction_event, REQUEST
    authority, harness = _node(nodes)
    rig = Rig(Path(authority.store.read()["runtime_path"]), mode)
    rig.relay.thread_store = harness.store
    rig.relay.relay_authority = authority
    target = _target(authority)
    source = rig.grant(target)
    calls, feedback = [], []
    def probe(remote, **kwargs):
        calls.append((remote, kwargs))
        # Admission is the fixture adapter's result; missing native proof is
        # deliberately not upgraded into a model completion claim.
        return dict(status="ok", wake=dict(state="enqueued"), control=dict(token=deepcopy(authority.token)))
    rig.relay.remote_ssh_adapter = SimpleNamespace(supports_control=True, probe=probe)
    class Control:
        def __init__(self, adapter, runtime):
            assert adapter is rig.relay.remote_ssh_adapter
        def notify(self, remote, token, event, notifier):
            assert token == authority.token and remote.expected_session_ids == (authority.thread_id,)
            with authority.guard():
                feedback.append(event.transition_fingerprint)
                return notifier.notify(event)
    monkeypatch.setattr(relay_module, "RemoteControlClient", Control)
    if fault == "capture_storage":
        def broken_capture(*args, **kwargs):
            raise OSError("fixture optional proof storage failure")
        monkeypatch.setattr(authority, "_record_native_delivery", broken_capture)
    if fault == "lost_ack":
        def lost(**params):
            rig.acks.append(deepcopy(params))
            raise OSError("fixture connection lost after provider accepted the attempt")
        monkeypatch.setattr(rig, "post", lost)
    before = len(rig.acks)
    event = instruction_event(rig, source)
    result = rig.deliver(event)
    assert result[0]["status"] == "bot_queued" and result[0]["delivery_status"] == "enqueued"
    assert len(calls) == len(feedback) == 1 and len(rig.acks) == before + 1
    assert REQUEST in rig.acks[-1]["text"] and rig.acks[-1]["thread_ts"] == source
    journal = rig.store.journal
    with journal.transaction() as db:
        key = rig.relay._event_key(event, "Ev" + event["client_msg_id"] if mode == "socket" else None)
        admitted = journal.get(db, "events", sha256_text(key))
        receipts = db.execute("SELECT value FROM records WHERE namespace=? AND kind='slack_bot_receipts'",
                             ("slack/bot-command-receipts-v1",)).fetchall()
    assert admitted["state"] == "delivered" and admitted["delivery_status"] == "enqueued"
    assert admitted.get("native_completed") is not True and len(receipts) == 1
    receipt = json.loads(receipts[0][0])
    assert receipt["outcome"] == "queued" and receipt["state"] == ("uncertain" if fault == "lost_ack" else "sent")
    old = _rows(journal.database)
    _park(nodes, authority)
    replacement, new = _node(nodes, LOGIN3)
    rig.relay.thread_store = new.store
    rig.relay.relay_authority = replacement
    # Native executor change and original redelivery never authorize another
    # attempt or ACK, even after an unknown provider receipt.
    rig.socket(event)
    assert len(calls) == len(feedback) == 1 and len(rig.acks) == before + 1
    assert _rows(journal.database) == old


def test_current_held_relay_fence_blocks_competing_handoff(nodes):
    first, _ = _node(nodes)
    _park(nodes, first)
    second, _ = _node(nodes, LOGIN3, activate=False)
    before = first.observation.path.read_bytes()
    entered, release = Event(), Event()
    def in_flight():
        with first.guard():
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as executor:
        active = executor.submit(in_flight)
        assert entered.wait(5)
        try:
            with pytest.raises(cs.ControlBusy):
                from test_relay_authority import _admit
                _admit(second, nodes[0])
            assert second.expected is None and first.observation.path.read_bytes() == before
        finally:
            release.set()
        active.result(timeout=5)
    from test_relay_authority import _admit
    assert _admit(second, nodes[0])
    assert second.expected["relay_epoch"] == first.expected["relay_epoch"] + 1


@pytest.mark.parametrize("broken", ("slack", "lark"))
@pytest.mark.parametrize("fault", ("missing", "corrupt", "symlink"))
def test_current_provider_readiness_isolated_without_resetting_missing_history(nodes, monkeypatch, broken, fault):
    authority, harness = _node(nodes)
    healthy = "lark" if broken == "slack" else "slack"
    for provider in (broken, healthy):
        authority.write_cursor(provider, "preserved", {"unknown": {"retain": True}})
    cluster = cs.control_read_json(authority.root / "cluster.json")
    cluster["providers"] = ["slack", "lark"]
    cs.control_atomic_json(authority.root / "cluster.json", cluster)
    database = authority.runtime / broken / "reply-tickets.sqlite3"
    healthy_database = authority.runtime / healthy / "reply-tickets.sqlite3"
    snapshot = database.read_bytes()
    if fault == "missing":
        database.unlink()
    elif fault == "corrupt":
        database.write_bytes(b"fixture corrupt provider history must not be reset")
    else:
        check = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == database or check(path))
    original_bad = database.read_bytes() if database.exists() else None
    with authority.guard():
        pass  # Global exact owner/session selection remains valid.
    assert authority.read_cursor(healthy, "preserved", {}) == {"unknown": {"retain": True}}
    authority.write_cursor(healthy, "independent", {"retained": True})
    assert authority.read_cursor(healthy, "independent", {}) == {"retained": True}
    healthy_rows = _rows(healthy_database)
    with pytest.raises((cs.ControlError, sqlite3.DatabaseError)):
        authority.write_cursor(broken, "must_not_reset", {"forbidden": True})
    assert _rows(healthy_database) == healthy_rows
    assert (database.read_bytes() if database.exists() else None) == original_bad
    if fault == "missing":
        assert not database.exists() and snapshot
    assert authority.handoff_safe()


@pytest.mark.parametrize("problem", ("extra_invalid", "extra_valid", "symlink", "noncanonical", "malformed"))
def test_native_fifo_unknown_identity_still_blocks_handoff(nodes, monkeypatch, problem):
    from codex_watchdog.linux_owner import pending_count
    authority, _ = _node(nodes)
    home = nodes[0]
    queue = home / "queue_1.sqlite"
    if problem in ("extra_invalid", "extra_valid"):
        extra = home / "queue_2.sqlite"
        extra.write_bytes(b"invalid" if problem == "extra_invalid" else queue.read_bytes())
    elif problem == "malformed":
        queue.write_bytes(b"fixture invalid native queue")
    elif problem == "symlink":
        check = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == queue or check(path))
    else:
        resolve = Path.resolve
        monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs:
            home / "foreign-queue.sqlite" if path == queue else resolve(path, *args, **kwargs))
    protected = {path: path.read_bytes() for path in home.glob("queue_*.sqlite")}
    with pytest.raises(ValueError, match="queue_unavailable_or_ambiguous"):
        pending_count(home, authority.thread_id)
    assert authority.handoff_safe() is False
    assert {path: path.read_bytes() for path in protected} == protected


def test_native_fifo_discovery_is_bounded_before_reading_ambiguous_files(nodes, monkeypatch):
    from codex_watchdog.linux_owner import pending_count
    authority, _ = _node(nodes)
    home = nodes[0]
    discovered = []
    glob = Path.glob
    def history(path, pattern):
        if path == home and pattern == "queue_*.sqlite":
            def candidates():
                for index in range(1, 10001):
                    discovered.append(index)
                    yield home / ("queue_" + str(index) + ".sqlite")
            return candidates()
        return glob(path, pattern)
    monkeypatch.setattr(Path, "glob", history)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("ambiguous FIFO was opened"))
    with pytest.raises(ValueError, match="queue_unavailable_or_ambiguous"):
        pending_count(home, authority.thread_id)
    assert discovered == [1, 2]

