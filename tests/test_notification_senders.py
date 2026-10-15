"""Focused tests for the common notification sender kernel fence."""

from __future__ import annotations

import contextvars
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from codex_watchdog import notification_senders
from codex_watchdog.notification_senders import SENDER_LOCK_NAME, sender_fence
from codex_watchdog.storage import FileLock, StoreBusyError

_PROBE = """
import os
import sys

with open(sys.argv[1], "r+b") as handle:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("busy")
    else:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        print("acquired")
"""


def test_case_equal_spelling_cannot_borrow_another_directory_resource(tmp_path, monkeypatch):
    first, second = tmp_path/'first', tmp_path/'second'
    with sender_fence(second):pass
    original=notification_senders._same_path
    def equal(left,right):
        return original(left,right) or {str(left),str(right)}=={str(first),str(second)}
    monkeypatch.setattr(notification_senders,'_same_path',equal)
    with sender_fence(first):
        with pytest.raises(ValueError,match='incoming runtime resource'):
            with sender_fence(second):pytest.fail('borrowed another physical directory')


def test_parent_components_cannot_flatten_redirects_before_validation(tmp_path):
    with pytest.raises(ValueError,match='parent components'):
        with sender_fence(tmp_path/'redirect'/'..'/'runtime'):
            pytest.fail('unresolved parent component was accepted')


class _Abort(BaseException):
    pass


def _lock_path(runtime: Path) -> Path:
    return runtime / "locks" / SENDER_LOCK_NAME


def _probe(lock_path: Path) -> str:
    """Try the real kernel lock from an independent isolated interpreter."""
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(lock_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def _leases():
    return notification_senders._LEASES.get()


def _attempt(runtime: Path, outcome: list) -> None:
    try:
        with sender_fence(runtime):
            outcome.append("entered")
    except StoreBusyError:
        outcome.append("busy")


def _symlink_or_skip(link: Path, target: Path, is_dir: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"OS cannot create symlink fixture: {exc}")


@pytest.fixture
def enters(monkeypatch):
    """Count real FileLock acquisitions without replacing their behaviour."""
    real_enter = FileLock.__enter__
    calls = []

    def counting_enter(self):
        calls.append(self.path)
        return real_enter(self)

    monkeypatch.setattr(FileLock, "__enter__", counting_enter)
    return calls


def test_nested_same_runtime_reuses_one_kernel_hold(tmp_path, enters):
    runtime = tmp_path / "runtime"
    lock_path = _lock_path(runtime)
    with sender_fence(runtime):
        outer = _leases()
        assert len(outer) == 1
        lease = outer[0]
        handle = lease.lock._handle
        assert lease.root == runtime
        assert _probe(lock_path) == "busy"
        with sender_fence(runtime):
            assert _leases() is outer
            assert lease.lock._handle is handle
            assert _probe(lock_path) == "busy"
        assert _leases() is outer
        assert not lease.closed
        assert lease.lock._handle is handle
        assert _probe(lock_path) == "busy"
    assert enters == [lock_path]
    assert _leases() == ()
    assert lease.closed
    assert lease.lock._handle is None
    assert _probe(lock_path) == "acquired"


@pytest.mark.parametrize("error", [RuntimeError, _Abort])
def test_every_exit_releases_kernel_lock(tmp_path, error):
    runtime = tmp_path / "runtime"
    with pytest.raises(error):
        with sender_fence(runtime):
            lease = _leases()[0]
            with sender_fence(runtime):
                raise error("stop")
    assert lease.closed
    assert _leases() == ()
    assert _probe(_lock_path(runtime)) == "acquired"


def test_independent_process_sees_busy_then_acquires(tmp_path):
    runtime = tmp_path / "runtime"
    with sender_fence(runtime):
        assert _probe(_lock_path(runtime)) == "busy"
    assert _probe(_lock_path(runtime)) == "acquired"
    with sender_fence(runtime):
        assert _probe(_lock_path(runtime)) == "busy"
    assert _probe(_lock_path(runtime)) == "acquired"


def test_other_thread_cannot_reenter_even_with_copied_context(tmp_path):
    runtime = tmp_path / "runtime"
    plain, copied = [], []
    with sender_fence(runtime):
        context = contextvars.copy_context()
        for target, args in (
            (_attempt, (runtime, plain)),
            (context.run, (_attempt, runtime, copied)),
        ):
            worker = threading.Thread(target=target, args=args)
            worker.start()
            worker.join(timeout=60)
        assert plain == ["busy"]
        assert copied == ["busy"]
    after = []
    worker = threading.Thread(target=_attempt, args=(runtime, after))
    worker.start()
    worker.join(timeout=60)
    assert after == ["entered"]


def test_closed_lease_in_escaped_same_thread_context_is_not_borrowed(
    tmp_path, enters
):
    runtime = tmp_path / "runtime"
    lock_path = _lock_path(runtime)
    with sender_fence(runtime):
        outer = _leases()[0]
        escaped = contextvars.copy_context()
    assert outer.closed
    seen = []

    def reenter():
        with sender_fence(runtime):
            seen.append((_leases(), _probe(lock_path)))

    escaped.run(reenter)
    ((stack, probe),) = seen
    assert len(stack) == 1 and outer not in stack and outer.closed
    fresh = stack[0]
    assert fresh is not outer
    assert fresh.lock is not outer.lock
    assert fresh.closed
    assert probe == "busy"
    assert enters == [lock_path, lock_path]
    assert _probe(lock_path) == "acquired"


def test_escaped_context_chain_does_not_accumulate_closed_history(tmp_path):
    current = contextvars.copy_context()
    for _ in range(100):
        def capture():
            with sender_fence(tmp_path):
                assert len(_leases()) == 1
                return contextvars.copy_context()
        current = current.run(capture)


def test_foreign_pid_must_acquire_real_exclusion(tmp_path, monkeypatch, enters):
    # Simulates a forked child's PID; the competing kernel lock is real.
    runtime = tmp_path / "runtime"
    with sender_fence(runtime):
        real_pid = os.getpid()
        with monkeypatch.context() as patch:
            patch.setattr(notification_senders.os, "getpid", lambda: real_pid + 1)
            with pytest.raises(StoreBusyError):
                with sender_fence(runtime):
                    pass
        assert len(_leases()) == 1
    assert enters == [_lock_path(runtime), _lock_path(runtime)]
    assert _probe(_lock_path(runtime)) == "acquired"


def test_nested_distinct_runtime_restores_outer_lease(tmp_path, enters):
    first = tmp_path / "first"
    second = tmp_path / "second"
    with sender_fence(first):
        outer = _leases()
        with sender_fence(second):
            inner = _leases()
            assert len(inner) == 2 and inner[0] is outer[0]
            assert _probe(_lock_path(second)) == "busy"
            with sender_fence(first):
                assert _leases() is inner
        assert _leases() is outer
        assert inner[1].closed and not outer[0].closed
        assert _probe(_lock_path(second)) == "acquired"
        assert _probe(_lock_path(first)) == "busy"
        with sender_fence(first):
            assert _leases() is outer
    assert enters == [_lock_path(first), _lock_path(second)]
    assert _probe(_lock_path(first)) == "acquired"


def test_relative_runtime_becomes_absolute_and_creates_only_fence_entries(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    with sender_fence("runtime"):
        lease = _leases()[0]
        assert lease.root.is_absolute()
        assert lease.root == tmp_path / "runtime"
    entries = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert entries == ["runtime", "runtime/locks", f"runtime/locks/{SENDER_LOCK_NAME}"]


def test_refuses_empty_missing_parent_and_nonstandard_entries(tmp_path):
    with pytest.raises(ValueError):
        with sender_fence(""):
            pass

    missing = tmp_path / "missing" / "runtime"
    with pytest.raises(OSError):
        with sender_fence(missing):
            pass
    assert not (tmp_path / "missing").exists()

    file_root = tmp_path / "file-root"
    file_root.write_bytes(b"")
    with pytest.raises(ValueError):
        with sender_fence(file_root):
            pass

    file_locks = tmp_path / "file-locks"
    file_locks.mkdir()
    (file_locks / "locks").write_bytes(b"")
    with pytest.raises(ValueError):
        with sender_fence(file_locks):
            pass

    dir_lock = tmp_path / "dir-lock"
    _lock_path(dir_lock).mkdir(parents=True)
    with pytest.raises(ValueError):
        with sender_fence(dir_lock):
            pass
    assert _leases() == ()


def test_refuses_symlinked_runtime_root_and_ancestor(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    _symlink_or_skip(link, target, True)
    for runtime in (link, link / "runtime"):
        with pytest.raises(ValueError):
            with sender_fence(runtime):
                pass
    assert list(target.iterdir()) == []


def test_refuses_symlinked_locks_directory(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    _symlink_or_skip(runtime / "locks", target, True)
    with pytest.raises(ValueError):
        with sender_fence(runtime):
            pass
    assert list(target.iterdir()) == []


def test_refuses_symlinked_lock_file(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "locks").mkdir(parents=True)
    target = tmp_path / "elsewhere.lock"
    target.write_bytes(b"")
    _symlink_or_skip(_lock_path(runtime), target, False)
    with pytest.raises(ValueError):
        with sender_fence(runtime):
            pass
    assert target.read_bytes() == b""


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_refuses_junction_runtime_root(tmp_path):
    winapi = pytest.importorskip("_winapi")
    if not hasattr(winapi, "CreateJunction"):
        pytest.skip("this Python cannot create a junction fixture")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "junction"
    try:
        winapi.CreateJunction(str(target), str(link))
    except OSError as exc:
        pytest.skip(f"OS cannot create junction fixture: {exc}")
    with pytest.raises(ValueError):
        with sender_fence(link):
            pass
    assert list(target.iterdir()) == []


def test_changed_runtime_resource_refuses_borrow(tmp_path):
    runtime = tmp_path / "runtime"
    moved = tmp_path / "moved"
    with sender_fence(runtime):
        lease = _leases()[0]
        try:
            os.replace(runtime, moved)
        except PermissionError as exc:
            pytest.skip(f"OS refuses to move a directory holding an open lock: {exc}")
        runtime.mkdir()
        with pytest.raises(ValueError):
            with sender_fence(runtime):
                pass
        assert _leases() == (lease,)
        assert not lease.closed
    assert lease.closed
    assert list(runtime.iterdir()) == []
    assert _probe(_lock_path(moved)) == "acquired"
