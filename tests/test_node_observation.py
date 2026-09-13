"""Native proof, durable handoff and parked observation across shared homes."""
import json

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.node_observation import NodeObservation
from test_linux_node import nodes, make_store


STAMP = ("/fixture/rollout.jsonl", 4, 100, 200)


def observer(home, repo):
    store = make_store(home, repo)
    store.enroll_node()
    token = store.claim_remote("dog", "host", "vscode", host_observer=True)
    return NodeObservation(store, token)


def admitted(instance, stamp=STAMP, foreign_queue=False):
    with instance.guard(rollout_stamp=stamp, foreign_queue=foreign_queue) as result:
        return result


def test_native_node_handoff_keeps_old_parked_record_inactive(nodes):
    home, repo, host, _, pid = nodes
    a = observer(home, repo)
    assert admitted(a)
    pid[0] = None
    assert admitted(a)  # Release native writer, retain observation.
    assert admitted(a)
    assert cs.control_read_json(a.path)["node"] == "login-a.example"
    host[0] = "login-b.example"
    pid[0] = 123
    b = observer(home, repo)
    assert admitted(b)
    pid[0] = None
    assert admitted(b)
    host[0] = "login-a.example"
    restarted = NodeObservation(a.store, a.token)
    assert not admitted(restarted)
    assert restarted.reason == "control_node_observed_elsewhere"
    assert cs.control_read_json(a.path)["node"] == "login-b.example"


@pytest.mark.parametrize("pending", [False, True])
def test_another_node_cannot_take_observation_during_native_or_queued_work(nodes, pending):
    home, repo, host, _, pid = nodes
    a = observer(home, repo)
    assert admitted(a)
    if pending:
        with a.store.guard(a.token) as value:
            value["remote_state"] = dict(pending_instruction_id="git-attention:pending")
            cs.control_atomic_json(a.store.path, value)
        pid[0] = None
        assert admitted(a)
    host[0] = "login-b.example"
    pid[0] = 123
    b = observer(home, repo)
    before = a.path.read_bytes()
    assert not admitted(b)
    assert b.reason == "control_node_observation_other_work_pending"
    assert a.path.read_bytes() == before


def test_parked_history_or_foreign_queue_cannot_create_a_competing_wake(nodes):
    home, repo, _, _, pid = nodes
    a = observer(home, repo)
    assert admitted(a)
    pid[0] = None
    assert admitted(a)
    assert not admitted(a, foreign_queue=True)
    assert a.reason == "control_node_observation_foreign_queue"
    changed = (*STAMP[:2], 500, 900)
    assert not admitted(a, stamp=changed)
    assert a.reason == "control_node_observation_history_changed"
    restarted = NodeObservation(a.store, a.token)
    assert not admitted(restarted, stamp=changed)
    pid[0] = 123  # Only fresh native ownership makes the new history ours.
    assert admitted(restarted, stamp=changed)
    pid[0] = None
    assert admitted(restarted, stamp=changed)


def test_upgrade_reuses_unique_registration_but_does_not_choose_between_old_nodes(nodes):
    home, repo, host, _, pid = nodes
    a = observer(home, repo)
    pid[0] = None
    assert admitted(a)
    saved = a.path.read_bytes()
    assert admitted(a)
    assert a.path.read_bytes() == saved
    # An older multi-node installation has no shared observation receipt yet.
    a.path.unlink()
    host[0] = "login-b.example"
    pid[0] = 123
    observer(home, repo)
    host[0] = "login-a.example"
    pid[0] = None
    assert not admitted(a)
    assert not a.path.exists()
    assert a.reason == "control_node_observation_needs_native_writer"
    pid[0] = 123
    assert admitted(a)


def test_malformed_or_stale_observation_never_changes_ownership(nodes):
    home, repo, _, _, _ = nodes
    a = observer(home, repo)
    assert admitted(a)
    malformed = json.loads(a.path.read_text())
    malformed["repo_path"] = "/different-repo"
    a.path.write_text(json.dumps(malformed))
    before = a.path.read_bytes()
    with pytest.raises(cs.ControlError, match="observation_mismatch"):
        admitted(a)
    assert a.path.read_bytes() == before
    with a.store.guard(a.token) as value:
        value["epoch"] += 1
        cs.control_atomic_json(a.store.path, value)
    with pytest.raises(cs.ControlError, match="stale_epoch"):
        admitted(a)
    assert a.path.read_bytes() == before


def test_parked_history_is_rechecked_before_external_effects(nodes):
    home, repo, _, _, pid = nodes
    a = observer(home, repo)
    assert admitted(a)
    pid[0] = None
    rollout = home / "sessions/thread.jsonl"
    info = rollout.stat()
    stamp = (str(rollout), info.st_ino, info.st_size, info.st_mtime_ns)
    with pytest.raises(cs.ControlBusy, match="observation_required"):
        a.validate_snapshot()
    with a.guard(rollout_stamp=stamp) as active:
        assert active
        a.validate_snapshot()
        rollout.write_text('{"other_node":true}\n')
        with pytest.raises(cs.ControlBusy, match="history_changed"):
            a.validate_snapshot()
    assert not admitted(a, stamp=(str(rollout), info.st_ino, 100, 200))
    with pytest.raises(cs.ControlBusy, match="observation_required"):
        a.validate_snapshot()
