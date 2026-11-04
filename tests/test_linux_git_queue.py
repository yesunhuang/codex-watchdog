from contextlib import nullcontext
import json
import sqlite3
from types import SimpleNamespace
import time

import pytest

from codex_watchdog import linux_git_queue
from codex_watchdog.app_server import AppServerError
from codex_watchdog.control_state import control_courier_path, control_state_home
from codex_watchdog.linux_continuation import LinuxContinuation
from codex_watchdog.linux_git_queue import LinuxGitQueueStart
from codex_watchdog.models import sha256_text
from codex_watchdog.queue_wake import REMOTE_UPDATE_PROMPT
from codex_watchdog.storage import InstructionStore
from codex_watchdog.workspace_registry import TrackedWorkspace

SID = "11111111-2222-4333-8444-555555555555"
QID = "41111111-2222-4333-8444-555555555555"
TURN = "31111111-2222-4333-8444-555555555555"
INTERRUPTED = "21111111-2222-4333-8444-555555555555"


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    home, repo, runtime = tmp_path / "home", tmp_path / "repo", tmp_path / "runtime"
    home.mkdir(); repo.mkdir(); runtime.mkdir()
    workspace = TrackedWorkspace.create("fixture", repo, SID)
    instruction = "git:" + sha256_text(workspace.workspace_id)[:16] + ":" + "a" * 40
    identity = dict(thread_id=SID, queue_message_id=QID, instruction_id=instruction,
                    prompt_sha256=sha256_text(REMOTE_UPDATE_PROMPT))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT,payload_json TEXT,queue_order INTEGER)")
        db.execute("CREATE INDEX thread_order ON queued_items(thread_id,queue_order)")
        payload = {"UserInput": {"content": [{"type": "text", "text":
            "[CODEX_WATCHDOG_WAKE id=" + instruction + " sha256=" + identity["prompt_sha256"] + "]\n" + REMOTE_UPDATE_PROMPT}]}}
        db.execute("INSERT INTO queued_items VALUES (?,?,?,0)", (QID, SID, json.dumps(payload)))
    state_home = control_state_home(home)
    association = control_courier_path(home, SID, QID)
    receipt = state_home / "remote-wake" / (sha256_text(instruction) + ".json")
    InstructionStore._atomic_json(association, dict(schema_version=1, kind="current_courier",
        node=linux_git_queue.control_node_name(), **identity))
    InstructionStore._atomic_json(receipt, dict(identity, state="enqueued"))
    binding = SimpleNamespace(runtime=runtime, codex_home=home,
        load=lambda: dict(state="armed", expires_at=time.time() + 600))
    token = dict(role="remote")
    store = SimpleNamespace(codex_home=home, thread_id=SID, repo_path=str(repo),
        read=lambda: {}, _check=lambda value, selected: None)
    monkeypatch.setattr(linux_git_queue, "current_control", lambda: (store, token))
    monkeypatch.setattr(linux_git_queue, "current_effect", lambda purpose: nullcontext())
    scope = dict(node="fixture", epoch=1, instance="fixture-owner")
    proof = dict(owner=scope, thread_read={"thread": {"id": SID, "cwd": str(repo), "status": {"type": "idle"}}})
    calls = []
    def request(method, params):
        calls.append((method, params))
        return {"turn": {"id": TURN, "status": "inProgress"}}
    owner = SimpleNamespace(node_local=True, resumed=True, thread=SID, thread_status="idle",
        approval_required=False, release_requested=False, yield_requested=False,
        client=SimpleNamespace(request=request), native_work=SimpleNamespace(
            current_native=lambda: proof, current_owner=lambda: dict(owner=scope)))
    return SimpleNamespace(home=home, repo=repo, runtime=runtime, workspace=workspace,
        binding=binding, owner=owner, calls=calls, identity=identity, association=association,
        receipt=receipt, proof=proof, store=store, latest=lambda owner: (INTERRUPTED, "interrupted"))


def test_interrupted_git_head_starts_existing_id_once(scenario):
    s = scenario
    start = LinuxGitQueueStart(s.binding)
    assert start.step(s.owner, s.workspace, s.latest)["status"] == "requested"
    assert s.calls == [("thread/queue/start", {"threadId": SID, "queuedSubmissionId": QID})]
    # Retain the same native row to exercise lost/late native-queue observation.
    assert LinuxGitQueueStart(s.binding).step(s.owner, s.workspace, s.latest)["status"] == "claimed"
    assert len(s.calls) == 1


def test_lost_native_reply_and_owner_restart_never_retry(scenario):
    s = scenario
    def lost(method, params):
        s.calls.append((method, params))
        raise AppServerError("app_server_request_timeout")
    s.owner.client.request = lost
    assert LinuxGitQueueStart(s.binding).step(s.owner, s.workspace, s.latest)["status"] == "uncertain"
    assert LinuxGitQueueStart(s.binding).step(s.owner, s.workspace, s.latest)["status"] == "claimed"
    assert len(s.calls) == 1


def test_older_continuation_state_does_not_gate_new_current_git(scenario):
    s = scenario
    continuation = LinuxContinuation(s.binding, None, None)
    # Exact dogfood shape: an unrelated old continuation must not be consulted
    # before starting the current admitted Git head.
    InstructionStore._atomic_json(continuation.path, dict(schema_version=1, status="started",
        instruction_id="old-continuation", continuation_turn_id=TURN))
    continuation._latest = s.latest
    result = continuation.step(s.owner, s.workspace)
    assert result["git_queue"]["status"] == "requested" and len(s.calls) == 1


@pytest.mark.parametrize("kind", ["human_head", "foreign_node", "wrong_prompt", "unproved_receipt", "completed_turn"])
def test_unsafe_or_ineligible_head_is_preserved(scenario, kind):
    s = scenario
    if kind == "human_head":
        with sqlite3.connect(s.home / "queue_1.sqlite") as db:
            db.execute("INSERT INTO queued_items VALUES ('human',?,'{}',-1)", (SID,))
    elif kind == "foreign_node":
        value = json.loads(s.association.read_text()); value["node"] = "other-node"
        InstructionStore._atomic_json(s.association, value)
    elif kind == "wrong_prompt":
        with sqlite3.connect(s.home / "queue_1.sqlite") as db:
            db.execute("UPDATE queued_items SET payload_json='{}'")
    elif kind == "unproved_receipt":
        value = json.loads(s.receipt.read_text()); value["state"] = "uncertain"
        InstructionStore._atomic_json(s.receipt, value)
    else:
        s.latest = lambda owner: (INTERRUPTED, "completed")
    assert LinuxGitQueueStart(s.binding).step(s.owner, s.workspace, s.latest) is None
    assert s.calls == []
    with sqlite3.connect(s.home / "queue_1.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM queued_items WHERE id=?", (QID,)).fetchone()[0] == 1


@pytest.mark.parametrize("condition", ["active", "approval", "release", "handoff", "writer_changed"])
def test_current_native_fences_refuse_start(scenario, condition):
    s = scenario
    if condition == "active": s.owner.thread_status = "active"
    elif condition == "approval": s.owner.approval_required = True
    elif condition == "release": s.owner.release_requested = True
    elif condition == "handoff": s.owner.yield_requested = True
    else: s.owner.native_work.current_owner = lambda: dict(owner=dict(epoch=2))
    assert LinuxGitQueueStart(s.binding).step(s.owner, s.workspace, s.latest) is None
    assert not s.calls


def test_queue_head_change_during_native_read_refuses(scenario):
    s = scenario
    def changed():
        with sqlite3.connect(s.home / "queue_1.sqlite") as db:
            db.execute("INSERT INTO queued_items VALUES ('human',?,'{}',-1)", (SID,))
        return s.proof
    s.owner.native_work.current_native = changed
    assert LinuxGitQueueStart(s.binding).step(s.owner, s.workspace, s.latest) is None
    assert not s.calls


def test_head_lookup_is_indexed_and_ignores_unrelated_history(scenario):
    s = scenario
    with sqlite3.connect(s.home / "queue_1.sqlite") as db:
        db.executemany("INSERT INTO queued_items VALUES (?,?,?,?)",
            [("other-" + str(i), "other", "{}", i) for i in range(1000)])
    assert LinuxGitQueueStart(s.binding).step(s.owner, s.workspace, s.latest)["status"] == "requested"
    assert len(s.calls) == 1
