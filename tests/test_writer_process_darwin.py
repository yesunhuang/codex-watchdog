from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys

import pytest

from codex_watchdog import observed_writer, writer_process as writer


def resource(path, identity, pid=123):
    return (f"p{pid}\nu{getattr(os, 'getuid', lambda: 0)()}\nf17\ntREG\n"
            f"D0x{identity.st_dev:x}\ni{identity.st_ino}\nn{path}\n").encode()


@pytest.mark.parametrize("fault", [None, "second_opener", "wrong_inode", "wrong_uid",
    "wrong_path", "truncated", "oversized", "unlocked", "released", "reused_pid",
    "opener_changed", "file_replaced", "birth_unavailable", "final_lock_birth_changed"])
def test_darwin_association_requires_all_independent_proofs(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(writer.os, "getuid", lambda: 505, raising=False)
    path = tmp_path / "writer.lock"
    identity = SimpleNamespace(st_dev=9, st_ino=17, st_uid=0, st_mode=0o100600)
    raw = resource(path, identity)
    bad = raw
    if fault == "second_opener": bad += raw.replace(b"p123", b"p124")
    if fault == "wrong_inode": bad = raw.replace(b"i17", b"i18")
    if fault == "wrong_uid": bad = raw.replace(b"\nu" + str(getattr(os, 'getuid', lambda: 0)()).encode(), b"\nu9999999")
    if fault == "wrong_path": bad = raw.replace(str(path).encode(), b"/other.lock")
    if fault == "truncated": bad = raw[:-1]
    if fault == "oversized": bad = b"x" * (writer._DARWIN_RESOURCE_BYTES + 1)
    samples = iter([bad, raw.replace(b"p123", b"p124") if fault == "opener_changed" else raw])
    monkeypatch.setattr(writer, "_darwin_resource_query", lambda p, deadline: next(samples))
    identities = iter([identity, SimpleNamespace(**{**vars(identity), "st_ino": 18})
                       if fault == "file_replaced" else identity])
    monkeypatch.setattr(writer, "_darwin_file_identity", lambda p: next(identities))
    held = iter([fault != "unlocked", fault != "released"])
    lock_checks = []
    def lock_held(p, i):
        lock_checks.append(True)
        return next(held)
    monkeypatch.setattr(writer, "_darwin_lock_held", lock_held)
    births = iter(["darwin:100:1", "darwin:100:2" if fault == "reused_pid" else "darwin:100:1"])
    def birth(pid):
        if fault == "birth_unavailable": raise ValueError("unavailable")
        if fault == "final_lock_birth_changed" and len(lock_checks) == 2:
            return "darwin:100:2"
        return next(births)
    monkeypatch.setattr(observed_writer, "_darwin_birth", birth)
    if fault == "birth_unavailable":
        with pytest.raises(ValueError): writer._darwin_resource_process(path)
    else:
        assert writer._darwin_resource_process(path) == (123 if fault is None else None)


@pytest.mark.parametrize("fault", [None, "timeout", "overflow", "nonzero"])
def test_resource_query_is_exact_bounded_and_cleans_up_only_inspection_child(tmp_path, monkeypatch, fault):
    path = tmp_path / "writer.lock"
    events, reads = [], []
    pipe = SimpleNamespace(fileno=lambda: 77, close=lambda: events.append("closed"))
    state = [None]
    def wait(timeout):
        assert 0 < timeout <= writer._DARWIN_RESOURCE_SECONDS
        state[0] = 1 if fault == "nonzero" else 0
        return state[0]
    def kill(): events.append("kill"); state[0] = -9
    child = SimpleNamespace(stdout=pipe, wait=wait, poll=lambda: state[0], kill=kill)
    def create(args, **kwargs):
        assert args == ["/usr/sbin/lsof", "-nP", "-F", "pufDint", "--", str(path)]
        assert kwargs["stdin"] == writer.subprocess.DEVNULL
        assert kwargs["stderr"] == writer.subprocess.DEVNULL
        assert set(kwargs["env"]) == {"PATH", "LC_ALL", "LANG"}
        return child
    monkeypatch.setattr(writer.subprocess, "Popen", create)
    monkeypatch.setattr(writer.time, "monotonic", lambda: 100)
    monkeypatch.setattr(writer.select, "select", lambda *args: ([], [], []) if fault == "timeout" else ([pipe], [], []))
    chunks = iter([b"proof", b""])
    def read(fd, size):
        reads.append(size)
        return b"x" * size if fault == "overflow" else next(chunks)
    monkeypatch.setattr(writer.os, "read", read)
    if fault is None: assert writer._darwin_resource_query(path, 102) == b"proof"
    else:
        with pytest.raises(ValueError): writer._darwin_resource_query(path, 102)
    assert "closed" in events and all(0 < n <= 8192 for n in reads)
    assert ("kill" in events) == (fault in ("timeout", "overflow"))


@pytest.fixture
def native_holder(tmp_path):
    if sys.platform != "darwin": pytest.skip("requires native Darwin libproc/lsof/FLOCK")
    path = tmp_path / "native.lock"
    child = subprocess.Popen([sys.executable, "-u", "-c", """
import fcntl,os,sys
with open(sys.argv[1], 'wb') as f:
 fcntl.flock(f,fcntl.LOCK_EX)
 print(os.getpid(),flush=True)
 for line in sys.stdin:
  if line.strip()=='release':
   fcntl.flock(f,fcntl.LOCK_UN);print('released',flush=True)
  else:break
""", str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        pid = int(child.stdout.readline().strip())
        yield path, child, pid
    finally:
        child.communicate("exit\n", timeout=5)
        assert child.returncode == 0


def test_native_darwin_sole_opener_and_independent_held_lock(native_holder):
    path, child, pid = native_holder
    birth = observed_writer._darwin_birth(pid)
    assert writer.writer_lock_process(path) == pid
    assert observed_writer._darwin_birth(pid) == birth and child.poll() is None
    link = path.with_name("alias.lock")
    link.symlink_to(path)
    assert writer.writer_lock_process(link) is None
    child.stdin.write("release\n"); child.stdin.flush()
    assert child.stdout.readline().strip() == "released"
    # A live sole opener without a held lock is insufficient.
    assert writer.writer_lock_process(path) is None and child.poll() is None


def test_native_darwin_multiple_openers_refuse_without_touching_writer(native_holder):
    path, child, pid = native_holder
    peer = subprocess.Popen([sys.executable, "-u", "-c",
        "import sys; f=open(sys.argv[1],'rb'); print('open',flush=True); sys.stdin.readline()", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert peer.stdout.readline().strip() == "open"
        assert writer.writer_lock_process(path) is None
        assert child.poll() is None and peer.poll() is None
    finally:
        peer.communicate("exit\n", timeout=5)
    assert writer.writer_lock_process(path) == pid


def test_native_darwin_history_attachment_uses_real_writer_and_refuses_release(native_holder, tmp_path):
    from test_workspace_discovery import new_format_discovery, target_window, SESSION_CURRENT
    from codex_watchdog.workspace_discovery import (
        HISTORY_ATTACHED_SOURCE, LiveVSCodeWindow, writer_lock_is_held,
    )

    path, child, pid = native_holder
    discovery, log, windows = new_format_discovery(tmp_path)
    lock = tmp_path / ".codex/thread-writer-locks" / (SESSION_CURRENT + ".lock")
    lock.parent.mkdir()
    path.rename(lock)
    windows["target"] = LiveVSCodeWindow("1", 100, pid, log)
    discovery.session_resolver.writer_process_probe = writer.writer_lock_process
    discovery.session_resolver.lock_probe = writer_lock_is_held
    selected = target_window(discovery.snapshot())
    assert selected.session_id == SESSION_CURRENT
    assert selected.session_source == HISTORY_ATTACHED_SOURCE
    windows["target"] = LiveVSCodeWindow("1", 100, pid + 1, log)
    assert target_window(discovery.snapshot()).session_id is None
    windows["target"] = LiveVSCodeWindow("1", 100, pid, log)
    child.stdin.write("release\n"); child.stdin.flush()
    assert child.stdout.readline().strip() == "released"
    assert target_window(discovery.snapshot()).session_id is None
    assert child.poll() is None
