"""Exercise the real dispatcher and service boundaries, without native clients."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import uuid

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.queue_wake import QueueWakeDispatcher, REMOTE_UPDATE_PROMPT
from codex_watchdog.control_state import control_wake_attempt_read
from codex_watchdog.remote_ssh import _REMOTE_SCRIPT
from test_mvp_service import (
    FakeAdapter, FakeMutator, FakeNotifier, FakeQueue, FakeRemoteRegistry,
    FakeRemoteSshAdapter, THREAD_A, THREAD_B, OID_A, OID_B, OID_C,
    build_service, observation, receipt, workspace, MvpWatchdogService,
)


def native_queue(home):
    home.mkdir(parents=True, exist_ok=True)
    database = home / "queue_1.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT,payload_json TEXT)")
        db.execute("CREATE INDEX by_thread ON queued_items(thread_id)")
        db.execute("CREATE TABLE queued_thread_revisions(thread_id TEXT PRIMARY KEY,revision INTEGER)")
    return database


def append(database, thread, text):
    queue_id = str(uuid.uuid4())
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO queued_items VALUES(?,?,?)", (queue_id, thread,
            json.dumps({"UserInput": {"content": [{"type": "text", "text": text}]}})))
    return queue_id


def dispatcher(runtime, home, database, calls):
    def runner(command, **kwargs):
        thread = command[command.index("--thread") + 1]
        queue_id = append(database, thread, command[-1])
        calls.append((thread, command[-1]))
        return subprocess.CompletedProcess(command, 0,
            "Queued message {} for thread {}.".format(queue_id, thread), "")
    return QueueWakeDispatcher(runtime, codex_home=home, queue_database=database, runner=runner)


def test_many_updates_share_one_live_git_wake_and_latest_follows_after_consumption(tmp_path):
    home = tmp_path / "home"
    database = native_queue(home)
    calls = []
    queue = dispatcher(tmp_path / "runtime", home, database, calls)
    first = queue.dispatch_remote_update(THREAD_A, OID_B)
    for n in range(1, 101):
        tip = format(n, "040x")
        value = queue.dispatch_remote_update(THREAD_A, tip)
        assert value.status == "coalesced"
        assert value.queue_message_id == first.queue_message_id
        assert control_wake_attempt_read(home, value.instruction_id) is None
    assert len(calls) == 1
    # Consumption is a native action; the observer never deletes the queue row.
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM queued_items WHERE id=?", (first.queue_message_id,))
    assert queue.dispatch_remote_update(THREAD_A, tip).status == "enqueued"
    assert len(calls) == 2
    # The exact first effect is never retried, even after consumption.
    assert queue.dispatch_remote_update(THREAD_A, OID_B).deduplicated
    assert len(calls) == 2


def test_other_sessions_and_manual_resume_provider_commands_still_append(tmp_path):
    home = tmp_path / "home"
    database = native_queue(home)
    calls = []
    queue = dispatcher(tmp_path / "runtime", home, database, calls)
    assert queue.dispatch_remote_update(THREAD_A, OID_B).status == "enqueued"
    assert queue.dispatch_remote_update(THREAD_B, OID_C).status == "enqueued"
    for source in ("resume_prompt", "slack", "lark", "manual"):
        assert queue.dispatch(THREAD_A, source + ":test", "Continue the task", source).status == "enqueued"
    assert len(calls) == 6


def test_independent_dispatchers_serialize_check_and_append(tmp_path):
    home = tmp_path / "home"
    database = native_queue(home)
    calls = []
    queues = [dispatcher(tmp_path / ("runtime" + str(n)), home, database, calls) for n in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(q.dispatch_remote_update, THREAD_A, tip)
                   for q, tip in zip(queues, (OID_B, OID_C))]
        results = [f.result() for f in futures]
    assert sorted(v.status for v in results) == ["coalesced", "enqueued"]
    assert len(calls) == 1


def test_unknown_current_queue_defers_only_git_without_claiming_tip(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "uncertain")
    queue = QueueWakeDispatcher(tmp_path / "runtime", codex_home=home, runner=runner)
    value = queue.dispatch_remote_update(THREAD_A, OID_B)
    assert value.status == "deferred"
    assert control_wake_attempt_read(home, value.instruction_id) is None
    assert not calls
    assert queue.dispatch(THREAD_A, "slack:independent", "Proceed", "slack").status == "uncertain"
    assert len(calls) == 1
    database = native_queue(home)
    # Historical uncertain manual work does not become a Git wake fence.
    queue.runner = dispatcher(tmp_path / "unused", home, database, calls).runner
    assert queue.dispatch_remote_update(THREAD_A, OID_C).status == "enqueued"


@pytest.mark.parametrize("status", ["coalesced", "deferred"])
def test_service_retains_latest_unattempted_tip_and_drops_it_when_head_catches_up(tmp_path, status):
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = tmp_path / "runtime"
    queue = FakeQueue(remote_receipts=[receipt(THREAD_A, "git:b"),
        receipt(THREAD_A, "git:c", status=status), receipt(THREAD_A, "git:c")])
    states = [observation(repo, topology="remote_ahead", head=OID_A, upstream=tip)
              for tip in (OID_B, OID_C, OID_C)]
    states.append(observation(repo, topology="equal", head=OID_C, upstream=OID_C))
    service = build_service(runtime, [workspace(repo)],
        FakeAdapter({str(repo.resolve()): states}), FakeMutator(), FakeNotifier(runtime), queue)
    service.run_once()
    service.run_once()
    pending = json.loads(service.state_path("watchdog").read_text())
    assert pending["last_attempted_remote_oid"] == OID_B
    assert pending["pending_remote_oid"] == OID_C
    service.run_once()
    assert queue.remote_calls == [(THREAD_A, OID_B, "watchdog"),
                                  (THREAD_A, OID_C, "watchdog"), (THREAD_A, OID_C, "watchdog")]
    service.run_once()
    final = json.loads(service.state_path("watchdog").read_text())
    assert final["pending_remote_oid"] is None
    assert len(queue.remote_calls) == 3


def test_bare_remote_probe_coalesces_before_claim_and_never_replays(tmp_path):
    home = tmp_path / "home"
    database = native_queue(home)
    namespace = {"__name__": "probe_test"}
    exec(_REMOTE_SCRIPT, namespace)
    namespace["remote_codex_home"] = lambda: home
    namespace["queue_database"] = lambda: database
    records = tmp_path / "receipts"
    records.mkdir()
    namespace["wake_record_path"] = lambda identifier: records / (sha256_text(identifier) + ".json")
    calls = []
    def attempt(request, thread):
        calls.append(request["instruction_id"])
        text = "[CODEX_WATCHDOG_WAKE id={} sha256={}]\n{}".format(
            request["instruction_id"], sha256_text(request["prompt"]), request["prompt"])
        append(database, thread, text)
        return {"state": "enqueued"}
    namespace["_dispatch_unretired_wake"] = attempt
    request = {"instruction_id": "git:" + "0" * 16 + ":" + OID_B,
               "prompt": REMOTE_UPDATE_PROMPT}
    assert namespace["dispatch_wake"](request, THREAD_A)["state"] == "enqueued"
    newer = dict(request, instruction_id="git:" + "0" * 16 + ":" + OID_C)
    assert namespace["dispatch_wake"](newer, THREAD_A)["state"] == "coalesced"
    assert control_wake_attempt_read(home, newer["instruction_id"]) is None
    assert namespace["dispatch_wake"](request, THREAD_A)["state"] == "suppressed"
    assert len(calls) == 1


def test_remote_service_keeps_coalesced_tip_eligible(tmp_path):
    runtime = tmp_path / "runtime"
    git = dict(status="observed",topology="remote_ahead",head_oid=OID_A,
               upstream_oid=OID_B,dirty_tracked=False,untracked_present=False,blockers=[])
    base = dict(status="ok",session_id=THREAD_A,repo_path="/home/user/repo",git=git,completion=None)
    later = dict(base,git=dict(git,upstream_oid=OID_C))
    adapter = FakeRemoteSshAdapter([base,dict(base,wake={"state":"enqueued"}),
        later,dict(later,wake={"state":"coalesced"}),later,dict(later,wake={"state":"enqueued"})])
    service = MvpWatchdogService(runtime,registry=FakeRemoteRegistry(runtime),
        git_adapter=FakeAdapter({}),notifier=FakeNotifier(runtime),queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=adapter,codex_home=runtime/"home")
    for _ in range(3):
        service.run_once()
    wakes = [call[1]["wake"] for call in adapter.calls if call[1].get("wake")]
    assert len(wakes) == 3
    assert wakes[1]["instruction_id"] == wakes[2]["instruction_id"]
