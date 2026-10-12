"""One-attempt IDs preserve uncertainty without blocking independent work."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.models import sha256_text
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.node_observation import observation_lock
from codex_watchdog.queue_wake import QueueWakeDispatcher
from test_queue_wake import FakeRunner, THREAD_ID
from test_node_observation import observer, admitted, foreign_absence
from test_linux_node import nodes
from test_mvp_service import (
    FakeAdapter, FakeNotifier, FakeQueue, FakeRemoteRegistry, FakeRemoteSshAdapter,
    OID_A, OID_B, OID_C, THREAD_A,
)


@pytest.mark.parametrize("returncode", [0, 1])
def test_wake_shared_claim_suppresses_same_id_across_runtimes_but_not_new_id(tmp_path, returncode):
    home = tmp_path / "home"
    runner = FakeRunner(returncode=returncode)
    first = QueueWakeDispatcher(tmp_path / "node-a", codex_home=home, runner=runner,
                                codex_executable="fixture-codex")
    second = QueueWakeDispatcher(tmp_path / "node-b", codex_home=home, runner=runner,
                                 codex_executable="fixture-codex")
    first.dispatch(THREAD_ID, "git:old", "old prompt", "remote_git")
    old_record = next(first.records.glob("*.json")).read_bytes()
    duplicate = second.dispatch(THREAD_ID, "git:old", "old prompt", "remote_git")
    assert duplicate.deduplicated and duplicate.status == "uncertain"
    assert not list(second.records.glob("*.json"))
    second.dispatch(THREAD_ID, "git:new", "new prompt", "remote_git")
    assert len(runner.calls) == 2
    assert next(first.records.glob("*.json")).read_bytes() == old_record


def test_death_after_claim_before_courier_is_terminal_without_fake_delivery(tmp_path, monkeypatch):
    class ProcessDeath(BaseException):
        pass
    runner = FakeRunner()
    home = tmp_path / "home"
    first = QueueWakeDispatcher(tmp_path / "first", codex_home=home, runner=runner,
                                codex_executable="fixture-codex")
    def die(*args):
        raise ProcessDeath()
    monkeypatch.setattr(first.store, "_atomic_json", die)
    with pytest.raises(ProcessDeath):
        first.dispatch(THREAD_ID, "git:abandoned", "prompt", "remote_git")
    claim = cs.control_wake_attempt_read(home, "git:abandoned", THREAD_ID, sha256_text("prompt"))
    assert claim["state"] == "claimed" and not runner.calls
    restarted = QueueWakeDispatcher(tmp_path / "second", codex_home=home, runner=runner,
                                    codex_executable="fixture-codex")
    result = restarted.dispatch(THREAD_ID, "git:abandoned", "prompt", "remote_git")
    assert result.status == "uncertain" and result.deduplicated and not runner.calls


def test_corrupt_attempt_fails_only_its_id_and_history_is_not_scanned(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    with cs.control_wake_attempt_lock(home, "bad"):
        cs.control_wake_attempt_claim(home, "bad", THREAD_ID, sha256_text("prompt"))
    bad = cs.control_wake_attempt_path(home, "bad")
    bad.write_bytes(b"null")
    for index in range(10000):
        (bad.parent / (sha256_text(str(index)) + ".json")).write_text("historical corruption")
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(tmp_path / "runtime", codex_home=home, runner=runner,
                                     codex_executable="fixture-codex")
    glob = Path.glob
    def bounded_glob(path, pattern):
        assert path != bad.parent and path != dispatcher.records
        return glob(path, pattern)
    monkeypatch.setattr(Path, "glob", bounded_glob)
    rejected = dispatcher.dispatch(THREAD_ID, "bad", "prompt", "remote_git")
    assert rejected.status == "rejected" and not runner.calls
    assert dispatcher.dispatch(THREAD_ID, "independent", "new", "remote_git").status == "enqueued"
    assert len(runner.calls) == 1 and bad.read_bytes() == b"null"


def test_live_shared_observer_lock_still_excludes_other_node(nodes):
    home, repo, host, _, _ = nodes
    a = observer(home, repo)
    assert admitted(a)
    host[0] = "login-b.example"
    b = observer(home, repo)
    before = a.path.read_bytes()
    with observation_lock(a.lock_path), ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(cs.ControlBusy, match="observation_busy"):
            pool.submit(admitted, b).result(timeout=5)
    assert a.path.read_bytes() == before
    b.foreign_writer_check = lambda expected: foreign_absence(expected, home)
    assert admitted(b)


@pytest.mark.parametrize("kind", [None, "unknown", "vscode", "remote", "absent", "wrong_target", "timeout"])
def test_saved_foreign_writer_requires_exact_fresh_native_absence(nodes, kind):
    home, repo, host, _, _ = nodes
    a = observer(home, repo)
    assert admitted(a)
    old_owner = a.store.path.read_bytes()
    host[0] = "login-b.example"
    b = observer(home, repo)
    expected_observer = json.loads(a.path.read_bytes())
    calls = []
    def verify(expected):
        calls.append(expected)
        assert expected["node"] == "login-a.example"
        assert expected["thread_id"] == THREAD_ID and expected["repo_path"] == str(repo)
        assert expected["relay_owner"] == expected_observer["relay_owner"]
        assert expected["relay_epoch"] == expected_observer["relay_epoch"]
        if kind == "timeout":
            raise TimeoutError()
        return dict(foreign_absence(expected, home), boot_id="fresh-native-boot",
                    writer_kind="absent" if kind == "wrong_target" else kind,
                    thread_id="other" if kind == "wrong_target" else THREAD_ID)
    b.foreign_writer_check = verify if kind is not None else None
    assert admitted(b) is (kind == "absent")
    if kind != "absent":
        assert b.reason in ("control_node_foreign_writer_unverified",
                            "control_node_observation_other_work_pending")
        assert json.loads(a.path.read_bytes()) == expected_observer
    assert a.store.path.read_bytes() == old_owner


def test_writer_started_inside_admission_is_recorded_before_releasing_shared_lock(nodes):
    home, repo, _, _, pid = nodes
    a = observer(home, repo)
    assert admitted(a)
    pid[0] = None
    with a.guard(rollout_stamp=None) as active:
        assert active
        pid[0] = 123
    assert json.loads(a.path.read_bytes())["writer_live"] is True


@pytest.mark.parametrize("field,bad", [
    ("uid", -1), ("uid", True), ("writer_lock_inode", 0),
    ("writer_lock_inode", True), ("observed_at", 0),
    ("observed_at", float("nan")), ("observed_at", float("inf")),
    ("observed_at", "future"), ("boot_id", ""),
    ("writer_lock_path", None), ("writer_lock_path", "relative/thread-writer-locks/" + THREAD_ID + ".lock"),
    ("writer_lock_path", "/fixture/thread-writer-locks/other.lock"),
    ("writer_lock_path", "/fixture/../native/thread-writer-locks/" + THREAD_ID + ".lock"),
    ("writer_lock_path", "/fixture//native/thread-writer-locks/" + THREAD_ID + ".lock"),
])
def test_foreign_transition_requires_fresh_same_user_native_metadata(nodes, field, bad):
    home, repo, host, _, pid = nodes
    a = observer(home, repo)
    assert admitted(a)
    pid[0] = None
    assert admitted(a)  # A saved false writer flag is not current absence proof.
    host[0], pid[0] = "login-b.example", 123
    b = observer(home, repo)
    b.foreign_writer_check = lambda expected: dict(foreign_absence(expected, home), **{
        field: time.time() + 60 if bad == "future" else bad,
    })
    before = a.path.read_bytes()
    assert not admitted(b)
    assert b.reason == "control_node_foreign_writer_unverified"
    assert a.path.read_bytes() == before


@pytest.mark.parametrize("native_alias", [False, True])
def test_foreign_native_inode_is_not_the_current_nodes_inode(nodes, tmp_path, monkeypatch, native_alias):
    home, repo, host, _, _ = nodes
    old = observer(home, repo)
    assert admitted(old)
    host[0] = "login-b.example"
    current = observer(home, repo)
    local_locks = home / "thread-writer-locks"
    if native_alias:
        if os.name == "nt":
            pytest.skip("Linux native alias permission boundary")
        native_locks = tmp_path / "local-native-runtime" / "thread-writer-locks"
        native_locks.parent.mkdir(mode=0o700)
        local_locks.rename(native_locks)
        native_locks.chmod(0o700)
        try:
            local_locks.symlink_to(native_locks, target_is_directory=True)
        except OSError:
            pytest.skip("native directory symlinks unavailable")
        monkeypatch.setattr(cs, "control_native_runtime_root", lambda: native_locks.parent)
        assert cs.control_native_writer_lock(home, THREAD_ID) == native_locks / (THREAD_ID + ".lock")
    foreign_locks = tmp_path / "foreign-native-runtime" / "thread-writer-locks"
    foreign_locks.mkdir(parents=True, mode=0o700)
    foreign_lock = foreign_locks / (THREAD_ID + ".lock")
    foreign_lock.touch(mode=0o600)
    assert foreign_lock.stat().st_ino != (local_locks / foreign_lock.name).stat().st_ino
    current.foreign_writer_check = lambda expected: dict(
        foreign_absence(expected, home), writer_lock_inode=foreign_lock.stat().st_ino,
        writer_lock_path=foreign_lock.as_posix() if foreign_lock.as_posix().startswith("/")
        else "/fixture/foreign-native/thread-writer-locks/" + foreign_lock.name,
    )
    before = old.store.path.read_bytes()
    assert admitted(current)
    assert old.store.path.read_bytes() == before
    assert local_locks.is_symlink() is native_alias


@pytest.mark.parametrize("fault", [None, "missing_exists", "missing_inode", "integer_exists", "existing_inode",
                                  "zero_parent", "bool_parent", "missing_parent", "wrong_parent", "live_writer"])
def test_foreign_missing_leaf_requires_explicit_validated_parent_proof(nodes, fault):
    home, repo, host, _, _ = nodes
    old = observer(home, repo)
    assert admitted(old)
    host[0] = "login-b.example"
    current = observer(home, repo)
    path = "/run/user/fixture/codex-native-runtime/thread-writer-locks/" + THREAD_ID + ".lock"
    def proof(expected):
        result = dict(foreign_absence(expected, home), boot_id="new-foreign-boot",
                      writer_lock_exists=False, writer_lock_inode=None, writer_lock_path=path,
                      writer_lock_parent_inode=987654, writer_lock_parent_path=path.rsplit("/", 1)[0])
        if fault == "missing_exists":
            del result["writer_lock_exists"]
        elif fault == "missing_inode":
            del result["writer_lock_inode"]
        elif fault == "integer_exists":
            result["writer_lock_exists"] = 0
        elif fault == "existing_inode":
            result["writer_lock_inode"] = 12345
        elif fault == "zero_parent":
            result["writer_lock_parent_inode"] = 0
        elif fault == "bool_parent":
            result["writer_lock_parent_inode"] = True
        elif fault == "missing_parent":
            del result["writer_lock_parent_path"]
        elif fault == "wrong_parent":
            result["writer_lock_parent_path"] = "/unrelated/thread-writer-locks"
        elif fault == "live_writer":
            result["writer_kind"] = "vscode"
        return result
    current.foreign_writer_check = proof
    old_owner, old_observer = old.store.path.read_bytes(), old.path.read_bytes()
    assert admitted(current) is (fault is None)
    assert old.store.path.read_bytes() == old_owner
    if fault is not None:
        assert current.reason == "control_node_foreign_writer_unverified"
        assert old.path.read_bytes() == old_observer


@pytest.mark.parametrize("history", [513, 10000, 1000000])
def test_optional_local_delivery_confirmation_has_fixed_record_and_byte_budget(tmp_path, monkeypatch, history):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"{}\n" * history)
    record = dict(rollout_path=str(path), rollout_baseline_offset=0,
                  thread_id=THREAD_ID, instruction_id="git:old", prompt_sha256=sha256_text("prompt"))
    original_open = Path.open
    read_counts = dict(bytes=0, records=0)
    class Counted:
        def __init__(self, file):
            self.file = file
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.file.close()
        def __getattr__(self, name):
            return getattr(self.file, name)
        def read(self, *args):
            raw = self.file.read(*args)
            read_counts["bytes"] += len(raw)
            return raw
        def readline(self, *args):
            raw = self.file.readline(*args)
            read_counts["bytes"] += len(raw)
            read_counts["records"] += 1
            return raw
    def counted_open(selected, *args, **kwargs):
        file = original_open(selected, *args, **kwargs)
        return Counted(file) if selected == path else file
    monkeypatch.setattr(Path, "open", counted_open)
    assert QueueWakeDispatcher._rollout_started(record) is None
    assert read_counts["bytes"] <= 2 * 1024 * 1024 and read_counts["records"] <= 512


@pytest.mark.parametrize("outcome", ["uncertain", "failed", "timeout", "rejected"])
def test_remote_old_outcome_never_blocks_new_oid_or_retries_old_oid(tmp_path, outcome):
    runtime = tmp_path / "runtime"
    def probe(oid):
        return dict(status="ok", session_id=THREAD_A, completion=None,
                    git=dict(head_oid=OID_A, upstream_oid=oid, topology="remote_changed", blockers=[]))
    b, c = probe(OID_B), probe(OID_C)
    adapter = FakeRemoteSshAdapter([b, dict(b, wake={"state": outcome}), b,
                                    c, dict(c, wake={"state": "uncertain"}), c])
    options = dict(registry=FakeRemoteRegistry(runtime), git_adapter=FakeAdapter({}),
                   notifier=FakeNotifier(runtime), queue_dispatcher=FakeQueue(),
                   remote_ssh_adapter=adapter, codex_home=runtime / "codex-home")
    service = MvpWatchdogService(runtime, **options)
    for _ in range(3):
        assert service.run_once().workspaces[0].status == "completed"
    # Restart retains eligibility independently from unresolved old outcomes.
    assert MvpWatchdogService(runtime, **options).run_once().workspaces[0].status == "completed"
    wakes = [kwargs["wake"] for _, kwargs in adapter.calls if "wake" in kwargs]
    assert [item["instruction_id"].split(":")[-1] for item in wakes] == [OID_B, OID_C]
    assert all("pending_instruction_id" not in kwargs for _, kwargs in adapter.calls)


def test_remote_pending_git_does_not_suppress_new_attention(tmp_path):
    runtime = tmp_path / "runtime"
    base = dict(status="ok", session_id=THREAD_A, completion=None,
                git=dict(head_oid=OID_A, upstream_oid=OID_B, topology="remote_changed", blockers=[]))
    changed = dict(base, git=dict(base["git"], blockers=["merge_in_progress"]))
    notifier = FakeNotifier(runtime)
    adapter = FakeRemoteSshAdapter([base, dict(base, wake={"state": "uncertain"}),
                                    changed, dict(changed, wake={"state": "uncertain"}), changed])
    service = MvpWatchdogService(runtime, registry=FakeRemoteRegistry(runtime),
        git_adapter=FakeAdapter({}), notifier=notifier, queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=adapter, codex_home=runtime / "codex-home")
    for _ in range(3):
        assert service.run_once().workspaces[0].status == "completed"
    wakes = [kwargs["wake"] for _, kwargs in adapter.calls if "wake" in kwargs]
    assert len(wakes) == 2
    assert wakes[0]["instruction_id"].startswith("git:")
    assert wakes[1]["instruction_id"].startswith("git-attention:")
