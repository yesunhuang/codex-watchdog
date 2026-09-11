from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from codex_watchdog.writer_process import writer_lock_process
from codex_watchdog.workspace_discovery import writer_lock_is_held


@pytest.mark.skipif(os.name != "nt", reason="requires Windows resource queries")
def test_windows_identifies_only_fixture_writer_and_leaves_it_running(tmp_path: Path) -> None:
    lock = tmp_path / "fixture.lock"
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", """
import ctypes as c
import os
import sys
k = c.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.argtypes = [c.c_wchar_p, c.c_uint32, c.c_uint32, c.c_void_p, c.c_uint32, c.c_uint32, c.c_void_p]
k.CreateFileW.restype = c.c_void_p
k.CloseHandle.argtypes = [c.c_void_p]
h = k.CreateFileW(sys.argv[1], 0xC0000000, 0, None, 4, 0x80, None)
assert h != c.c_void_p(-1).value
print(os.getpid(), flush=True)
sys.stdin.readline()
k.CloseHandle(h)
""", str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        # A Windows virtualenv executable can be a launcher with a worker child.
        writer_pid = int(child.stdout.readline().strip())
        assert writer_lock_is_held(lock)
        assert writer_lock_process(lock) == writer_pid
        assert child.poll() is None
        assert writer_lock_is_held(lock)
    finally:
        child.communicate("done\n", timeout=10)
    assert child.returncode == 0
    assert not writer_lock_is_held(lock)
    assert writer_lock_process(lock) is None


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux kernel FLOCK evidence")
def test_linux_identifies_held_fixture_without_changing_lock(tmp_path: Path) -> None:
    import fcntl
    lock = tmp_path / "fixture.lock"
    with lock.open("wb") as handle:
        assert writer_lock_process(lock) is None
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert writer_lock_process(lock) == os.getpid()
        assert writer_lock_is_held(lock)
    assert writer_lock_process(lock) is None


def test_missing_writer_resource_is_not_an_owner(tmp_path: Path) -> None:
    assert writer_lock_process(tmp_path / "missing.lock") is None
