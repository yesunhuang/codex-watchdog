"""Shared relay fencing follows verified native observation, never stale history."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.node_observation import NodeObservation, observation_lock
from test_linux_node import nodes, make_store


STAMP = ("/fixture/rollout.jsonl", 4, 100, 200)


def _observer(home, repo, *, instance="dog", boot="boot-a"):
    store = make_store(home, repo, boot=boot)
    store.enroll_node()
    token = store.claim_remote(instance, "host", "vscode", host_observer=True)
    return NodeObservation(store, token)


def _admit(observation):
    with observation.guard(rollout_stamp=STAMP) as admitted:
        return admitted


def _try_process_lock(path, pipe):
    try:
        with observation_lock(Path(path)):
            pipe.send("acquired")
    except cs.ControlBusy:
        pipe.send("busy")
    finally:
        pipe.close()


def _hold_process_lock(path, pipe):
    try:
        with observation_lock(Path(path)):
            pipe.send("acquired")
            if pipe.poll(10):
                pipe.recv()
    finally:
        pipe.close()


def _process_attempt(context, path):
    parent, child = context.Pipe()
    process = context.Process(target=_try_process_lock, args=(str(path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "lock probe did not finish"
        result = parent.recv()
        process.join(10)
        assert process.exitcode == 0
        return result
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(10)


def test_sibling_threads_cannot_enter_same_resolved_lock(tmp_path):
    path = tmp_path / "observation.lock"
    def attempt():
        try:
            with observation_lock(path.parent / "." / path.name):
                return "acquired"
        except cs.ControlBusy:
            return "busy"
    with observation_lock(path):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(attempt).result(timeout=5) == "busy"
    assert attempt() == "acquired"


def test_unrelated_session_lock_does_not_block_other_listener(tmp_path):
    with observation_lock(tmp_path / "session-a.lock"):
        def attempt():
            with observation_lock(tmp_path / "session-b.lock"):
                return "acquired"
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(attempt).result(timeout=5) == "acquired"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-lock proof")
def test_nested_lock_uses_one_fd_and_does_not_release_outer_lock(tmp_path, monkeypatch):
    path = tmp_path / "observation.lock"
    opens = []
    original = Path.open
    def opened(self, *args, **kwargs):
        if self.resolve() == path.resolve():
            opens.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    context = multiprocessing.get_context("spawn")
    with observation_lock(path):
        with observation_lock(path.parent / "." / path.name):
            assert len(opens) == 1
        assert _process_attempt(context, path) == "busy"
        assert len(opens) == 1
    assert _process_attempt(context, path) == "acquired"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-lock proof")
def test_two_processes_contend_and_exit_releases_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "observation.lock"
    parent, child = context.Pipe()
    holder = context.Process(target=_hold_process_lock, args=(str(path), child))
    holder.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() == "acquired"
        assert _process_attempt(context, path) == "busy"
        # Process exit releases authority even without the Python finally path.
        holder.terminate()
        holder.join(10)
        assert not holder.is_alive()
        assert _process_attempt(context, path) == "acquired"
    finally:
        parent.close()
        if holder.is_alive():
            holder.terminate()
            holder.join(10)


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="fork unavailable")
def test_fork_child_does_not_inherit_parent_nesting_authority(tmp_path):
    path = tmp_path / "observation.lock"
    with observation_lock(path):
        assert _process_attempt(multiprocessing.get_context("fork"), path) == "busy"


def test_three_node_handoffs_increment_epoch_and_fence_previous_callback(nodes):
    home, repo, host, _, pid = nodes
    a = _observer(home, repo)
    assert _admit(a)
    first = a.capture_relay_epoch()
    assert first["relay_epoch"] == 1
    assert _admit(a) and a.capture_relay_epoch() == first
    pid[0] = None
    assert _admit(a)
    native_before = (home / "state_5.sqlite").read_bytes()
    node_a_before = a.store.path.read_bytes()
    host[0], pid[0] = "login-b.example", 123
    b = _observer(home, repo, instance="dog-b", boot="boot-b")
    assert _admit(b)
    assert b.capture_relay_epoch()["relay_epoch"] == 2
    host[0] = "login-a.example"
    with pytest.raises(cs.ControlBusy, match="relay_not_selected"):
        a.capture_relay_epoch()
    pid[0] = None
    assert not _admit(a)
    host[0] = "login-b.example"
    assert _admit(b)
    host[0], pid[0] = "login-c.example", 123
    cs.control_atomic_json(cs.control_node_directory(home) / "node.json", dict(
        schema_version=1, node=host[0], codex_home=str(home)))
    c = _observer(home, repo, instance="dog-c", boot="boot-c")
    assert _admit(c)
    assert c.capture_relay_epoch()["relay_epoch"] == 3
    assert a.store.path.read_bytes() == node_a_before
    assert (home / "state_5.sqlite").read_bytes() == native_before
    assert a.store.directory != b.store.directory != c.store.directory


def test_controller_replacement_increments_epoch_without_changing_node(nodes):
    home, repo, _, _, pid = nodes
    first = _observer(home, repo)
    assert _admit(first)
    original = first.capture_relay_epoch()
    pid[0] = None
    assert _admit(first)
    first.store.release_remote(first.token, "absent")
    token = first.store.claim_remote("replacement", "host", "absent", host_observer=True)
    replacement = NodeObservation(first.store, token)
    with pytest.raises(cs.ControlBusy, match="relay_not_selected"):
        replacement.capture_relay_epoch()
    assert _admit(replacement)
    current = replacement.capture_relay_epoch()
    assert current["relay_epoch"] == original["relay_epoch"] + 1
    assert current["relay_owner"]["node"] == original["relay_owner"]["node"]
    assert current["relay_owner"]["local_epoch"] == token["epoch"]
    assert current["relay_owner"]["instance"] == "replacement"
    with pytest.raises(cs.ControlError, match="stale_epoch"):
        first.capture_relay_epoch()


def test_unrelated_sessions_keep_independent_relay_epochs(nodes):
    home, repo, _, _, _ = nodes
    a = _observer(home, repo)
    assert _admit(a)
    before = a.path.read_bytes()
    other_thread = "22222222-3333-4444-8555-666666666666"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("INSERT INTO threads VALUES (?,?,'vscode','user',0,?)",
                   (other_thread, str(repo), str(home / "sessions/thread.jsonl")))
    store = cs.ControlStore(home, other_thread, repo, clock=lambda: 100, boot_id="boot-a")
    store.enroll_node()
    token = store.claim_remote("other", "host", "vscode", host_observer=True)
    b = NodeObservation(store, token)
    assert _admit(b)
    assert b.capture_relay_epoch()["relay_epoch"] == 1
    assert a.capture_relay_epoch()["relay_epoch"] == 1
    assert a.path.read_bytes() == before
    assert a.path != b.path and a.lock_path != b.lock_path


def test_boot_change_requires_native_proof_before_epoch_change(nodes):
    home, repo, _, _, pid = nodes
    old = _observer(home, repo)
    assert _admit(old)
    before = old.path.read_bytes()
    pid[0] = None
    new_store = make_store(home, repo, boot="rebooted")
    assert new_store.claim_remote("new", "host", "absent", host_observer=True) is None
    assert old.path.read_bytes() == before
    pid[0] = 123
    new_token = new_store.claim_remote("new", "host", "vscode", host_observer=True)
    new = NodeObservation(new_store, new_token)
    assert _admit(new)
    assert new.capture_relay_epoch()["relay_epoch"] == 2
    assert new.capture_relay_epoch()["relay_owner"]["boot_id"] == "rebooted"


def test_legacy_selected_owner_upgrades_once_and_preserves_unknown_fields(nodes):
    home, repo, _, _, _ = nodes
    observation = _observer(home, repo)
    assert _admit(observation)
    value = cs.control_read_json(observation.path)
    value.pop("relay_epoch")
    value.pop("relay_owner")
    value["future_choice"] = {"preserve": True}
    cs.control_atomic_json(observation.path, value)
    with pytest.raises(cs.ControlBusy, match="relay_not_selected"):
        observation.capture_relay_epoch()
    assert _admit(observation)
    before = observation.path.read_bytes()
    assert observation.capture_relay_epoch()["relay_epoch"] == 1
    assert _admit(observation)
    assert observation.path.read_bytes() == before
    assert cs.control_read_json(observation.path)["future_choice"] == {"preserve": True}


def test_unknown_authority_fields_survive_node_handoff(nodes):
    home, repo, host, _, pid = nodes
    a = _observer(home, repo)
    assert _admit(a)
    value = cs.control_read_json(a.path)
    value["future_choice"] = {"preserve": True}
    value["relay_owner"]["future_hint"] = "keep"
    cs.control_atomic_json(a.path, value)
    pid[0] = None
    assert _admit(a)
    host[0], pid[0] = "login-b.example", 123
    b = _observer(home, repo, instance="dog-b", boot="boot-b")
    assert _admit(b)
    current = cs.control_read_json(b.path)
    assert current["future_choice"] == {"preserve": True}
    assert current["relay_owner"]["future_hint"] == "keep"
    assert b.capture_relay_epoch()["relay_epoch"] == 2


def test_pending_portable_effect_blocks_native_handoff_without_writes(nodes):
    home, repo, host, _, pid = nodes
    a = _observer(home, repo)
    assert _admit(a)
    pid[0] = None
    assert _admit(a)
    before = a.path.read_bytes()
    host[0], pid[0] = "login-b.example", 123
    b = _observer(home, repo, instance="dog-b", boot="boot-b")
    b.relay_handoff_safe = lambda: False
    assert not _admit(b)
    assert b.reason == "control_node_relay_pending"
    assert b.path.read_bytes() == before
    with pytest.raises(cs.ControlBusy, match="relay_not_selected"):
        b.capture_relay_epoch()
    b.relay_handoff_safe = lambda: True
    assert _admit(b)
    assert b.capture_relay_epoch()["relay_epoch"] == 2


@pytest.mark.parametrize("field,value", [
    ("relay_epoch", True), ("relay_epoch", 0), ("relay_epoch", -1),
    ("relay_owner", None),
    ("relay_owner", {"node": "login-a.example", "boot_id": "boot-a", "local_epoch": True, "instance": "dog"}),
])
def test_malformed_relay_authority_is_fail_closed(nodes, field, value):
    home, repo, _, _, _ = nodes
    observation = _observer(home, repo)
    assert _admit(observation)
    saved = json.loads(observation.path.read_text())
    saved[field] = value
    cs.control_atomic_json(observation.path, saved)
    before = observation.path.read_bytes()
    with pytest.raises(cs.ControlError, match="observation_mismatch"):
        observation.capture_relay_epoch()
    with pytest.raises(cs.ControlError, match="observation_mismatch"):
        _admit(observation)
    assert observation.path.read_bytes() == before
