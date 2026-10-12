"""Real host-helper dispatch under the non-reentrant controller owner lock."""
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.linux_auto import HostRemoteAdapter
from codex_watchdog.node_observation import NodeObservation
from codex_watchdog.queue_wake import REMOTE_UPDATE_PROMPT
from codex_watchdog.remote_ssh import RemoteSshTarget
from test_linux_node import THREAD, make_store, nodes


INSTRUCTION = "git:" + "a" * 40
QUEUE = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
OTHER_THREAD = "22222222-3333-4444-8555-666666666666"


@pytest.fixture
def host_git(nodes):
    home, repo, _, _, writer = nodes
    clock = [100.0]
    store = make_store(home, repo)
    store.clock = lambda: clock[0]
    store.enroll_node()
    token = store.claim_remote("dog", "native-host", "vscode", host_observer=True)
    observation = NodeObservation(store, token)
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id PRIMARY KEY,thread_id,payload_json)")
        db.execute("CREATE TABLE queued_thread_revisions(thread_id PRIMARY KEY,revision)")
        db.execute("INSERT INTO queued_thread_revisions VALUES (?,0)", (THREAD,))
    adapter = HostRemoteAdapter(home)
    ns = adapter.namespace
    embedded_store = ns["ControlStore"]

    def store_factory(codex_home, thread, repository):
        return embedded_store(codex_home, thread, repository,
                              clock=lambda: clock[0], boot_id=store.boot_id)

    ns.update(ControlStore=store_factory, ControlError=cs.ControlError,
              control_kernel_owner=lambda path: writer[0],
              control_vscode_writer=lambda pid: pid == 123,
              rollout_completion=lambda thread: None,
              codex_executable=lambda: "fixture-codex")
    git_reads, couriers = [], []

    def git_read(repository):
        assert repository == str(repo)
        git_reads.append(repository)
        return dict(status="observed", topology="remote_ahead", blockers=[],
                    head_oid="a" * 40, upstream_oid="b" * 40)

    def courier(argv, **kwargs):
        assert argv[:4] == ["fixture-codex", "queue", "--thread", THREAD]
        assert kwargs["env"]["CODEX_HOME"] == str(home)
        active_store, active_token = ns["_CONTROL_ACTIVE"]
        assert active_store.path == store.path and active_token == token
        assert type(active_store) is not type(store)
        # Do not replace the real kernel lock with a reentrant/fake guard.
        with pytest.raises(cs.ControlBusy, match="control_operation_in_progress"):
            with store.guard(token):
                pytest.fail("the courier must run while the owner lock remains held")
        couriers.append(argv)
        return subprocess.CompletedProcess(argv, 0,
            f"Queued message {QUEUE} for thread {THREAD}.\n", "")

    ns["git_observation"] = git_read
    ns["subprocess"] = SimpleNamespace(run=courier, DEVNULL=-3, PIPE=-1,
                                      SubprocessError=subprocess.SubprocessError)
    adapter.git_admission_check = observation.validate_snapshot
    target = RemoteSshTarget("ssh-remote+fixture.example", str(repo), "a" * 32, (THREAD,))
    return SimpleNamespace(home=home, repo=repo, store=store, token=token,
        observation=observation, adapter=adapter, target=target, writer=writer,
        clock=clock, git_reads=git_reads, couriers=couriers)


def probe(rig):
    return rig.adapter.probe(rig.target,
        control=dict(action="probe", thread_id=THREAD, token=rig.token),
        wake=dict(instruction_id=INSTRUCTION, prompt=REMOTE_UPDATE_PROMPT))


def assert_unattempted(rig):
    assert rig.couriers == []
    assert cs.control_wake_attempt_read(rig.home, INSTRUCTION) is None
    assert not rig.adapter.namespace["wake_record_path"](INSTRUCTION).exists()
    assert rig.adapter.namespace["_CONTROL_ACTIVE"] is None


@pytest.mark.parametrize("writer", [123, None], ids=["native-writer", "parked-empty-fifo"])
@pytest.mark.skipif(sys.platform == "win32", reason="host helper requires an absolute POSIX repository path")
def test_host_git_probe_dispatches_once_under_real_owner_lock(host_git, writer):
    rig = host_git
    rig.writer[0] = writer
    original = rig.store.path.read_bytes()
    with rig.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        first = probe(rig)
        assert first["status"] == "ok", first
        assert first["wake"]["state"] == "enqueued"
        assert first["wake"]["queue_message_id"] == QUEUE
        second = probe(rig)
        assert second["status"] == "ok" and second["wake"]["state"] == "enqueued"
    assert len(rig.git_reads) == 2 and len(rig.couriers) == 1
    assert rig.store.path.read_bytes() == original
    claim = cs.control_wake_attempt_read(rig.home, INSTRUCTION, THREAD)
    assert claim["state"] == "claimed"
    assert rig.adapter.namespace["_CONTROL_ACTIVE"] is None


@pytest.mark.parametrize("change,reason", [
    ("unknown-writer", "control_writer_unverified"),
    ("queue-after-detach", "control_node_observation_foreign_queue"),
])
@pytest.mark.skipif(sys.platform == "win32", reason="host helper requires an absolute POSIX repository path")
def test_host_git_rechecks_native_safety_after_git_read(host_git, change, reason):
    rig = host_git
    original = rig.store.path.read_bytes()
    git_read = rig.adapter.namespace["git_observation"]

    def changed_read(repository):
        result = git_read(repository)
        rig.writer[0] = 456 if change == "unknown-writer" else None
        if change == "queue-after-detach":
            with sqlite3.connect(rig.home / "queue_1.sqlite") as db:
                db.execute("INSERT INTO queued_items VALUES ('existing-fifo',?, '{}')", (THREAD,))
        return result

    rig.adapter.namespace["git_observation"] = changed_read
    with rig.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        try:
            result = probe(rig)
            assert result == dict(status="unavailable", reason=reason)
            assert_unattempted(rig)
        finally:
            rig.writer[0] = 123
    assert rig.store.path.read_bytes() == original
    if change == "queue-after-detach":
        with sqlite3.connect(rig.home / "queue_1.sqlite") as db:
            assert db.execute("SELECT id,thread_id,payload_json FROM queued_items").fetchall() == [
                ("existing-fifo", THREAD, "{}")]


@pytest.mark.parametrize("change", ["thread", "token", "home", "repo", "directory", "owner-path", "lock-path", "boot"])
def test_active_control_must_match_exact_admitted_capability(host_git, change):
    rig = host_git
    active_store = cs.ControlStore(rig.home, THREAD, rig.repo,
                                  clock=rig.store.clock, boot_id=rig.store.boot_id)
    active_token = dict(rig.token)
    if change == "token":
        active_token["epoch"] += 1
    else:
        attribute, value = {
            "thread": ("thread_id", OTHER_THREAD),
            "home": ("codex_home", rig.home / "other"),
            "repo": ("repo_path", str(rig.repo / "other")),
            "directory": ("directory", rig.store.directory / "other"),
            "owner-path": ("path", rig.store.path.with_name("other-owner.json")),
            "lock-path": ("lock_path", rig.store.lock_path.with_name("other-owner.lock")),
            "boot": ("boot_id", "another-boot"),
        }[change]
        setattr(active_store, attribute, value)
    before = rig.store.path.read_bytes()
    with rig.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        with rig.store.guard(rig.token):
            with pytest.raises(cs.ControlError, match="control_node_observation_capability_mismatch"):
                rig.observation.validate_snapshot(active_control=(active_store, active_token))
    assert rig.store.path.read_bytes() == before
    assert_unattempted(rig)


@pytest.mark.parametrize("change,reason", [
    ("epoch", "control_stale_epoch"),
    ("lease", "control_lease_expired"),
    ("handback", "control_handback_pending"),
])
@pytest.mark.skipif(sys.platform == "win32", reason="host helper requires an absolute POSIX repository path")
def test_active_control_rechecks_current_record_under_held_lock(host_git, change, reason):
    rig = host_git
    before = rig.store.path.read_bytes()
    checked = []

    def recheck(*, active_control=None):
        assert active_control is rig.adapter.namespace["_CONTROL_ACTIVE"]
        checked.append(True)
        value = rig.store.read()
        if change == "epoch":
            value["epoch"] += 1
        elif change == "lease":
            value["owner"]["expires"] = rig.clock[0]
        else:
            value["state"] = "HANDOFF"
            rig.writer[0] = None
        cs.control_atomic_json(rig.store.path, value)
        try:
            rig.observation.validate_snapshot(active_control=active_control)
        finally:
            rig.store.path.write_bytes(before)
            rig.writer[0] = 123

    rig.adapter.git_admission_check = recheck
    with rig.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        result = probe(rig)
        assert result == dict(status="unavailable", reason=reason)
        assert checked == [True]
        assert_unattempted(rig)
    assert rig.store.path.read_bytes() == before


def test_active_control_does_not_replace_observation_admission(host_git):
    rig = host_git
    with rig.store.guard(rig.token):
        with pytest.raises(cs.ControlBusy, match="control_node_observation_required"):
            rig.observation.validate_snapshot(active_control=(rig.store, rig.token))
    assert_unattempted(rig)


def test_standalone_snapshot_keeps_nonreentrant_owner_guard(host_git):
    rig = host_git
    with rig.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        rig.observation.validate_snapshot()
        with rig.store.guard(rig.token):
            with pytest.raises(cs.ControlBusy, match="control_operation_in_progress"):
                rig.observation.validate_snapshot()
    assert_unattempted(rig)


@pytest.mark.parametrize("shape", ["list", "short", "long"])
def test_active_control_requires_one_exact_pair(host_git, shape):
    rig = host_git
    pair = (rig.store, rig.token)
    invalid = {"list": list(pair), "short": pair[:1], "long": pair + (rig.token,)}[shape]
    with rig.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        with rig.store.guard(rig.token):
            with pytest.raises(cs.ControlError, match="control_node_observation_capability_mismatch"):
                rig.observation.validate_snapshot(active_control=invalid)
    assert_unattempted(rig)


def test_active_control_cannot_borrow_another_threads_observation(host_git):
    rig = host_git
    outcomes = []

    def other_thread():
        try:
            rig.observation.validate_snapshot(active_control=(rig.store, rig.token))
        except cs.ControlError as error:
            outcomes.append(error)
        else:
            outcomes.append(None)

    with rig.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        with rig.store.guard(rig.token):
            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert len(outcomes) == 1 and isinstance(outcomes[0], cs.ControlBusy)
            assert str(outcomes[0]) == "control_node_observation_required"
    assert_unattempted(rig)


def test_active_control_cannot_reuse_inherited_observation_in_child_pid(host_git, monkeypatch):
    from codex_watchdog import node_observation

    rig = host_git
    parent_pid = node_observation.os.getpid()
    with rig.observation.guard(rollout_stamp=None) as admitted:
        assert admitted
        with rig.store.guard(rig.token):
            with monkeypatch.context() as patch:
                patch.setattr(node_observation.os, "getpid", lambda: parent_pid + 1)
                with pytest.raises(cs.ControlBusy, match="control_node_observation_required"):
                    rig.observation.validate_snapshot(active_control=(rig.store, rig.token))
            assert node_observation.os.getpid() == parent_pid
    assert_unattempted(rig)
