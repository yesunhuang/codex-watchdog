"""Simulated OS point samples: never inspect a running native process."""
import ctypes
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from codex_watchdog import observed_writer as observed
from codex_watchdog import writer_process
from codex_watchdog.native_inflight import NativeInflightError

THREAD = "11111111-2222-4333-8444-555555555555"
BOOT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PID = 321
SAMPLE = observed._sample


class Function:
    def __init__(self, body): self.body = body
    def __call__(self, *args): return self.body(*args)


@pytest.fixture(autouse=True)
def no_native_calls(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("A simulated fixture attempted a real native query/child")
    monkeypatch.setattr(observed.ctypes, "CDLL", deny)
    monkeypatch.setattr(observed.ctypes, "WinDLL", deny, raising=False)
    monkeypatch.setattr(observed.subprocess, "Popen", deny)
    monkeypatch.setattr(observed, "_sample", deny)
    # Simulated POSIX fixtures remain runnable on Windows; these are not
    # assertions about that host's native UID or device encoding.
    if not hasattr(os, "getuid"):
        monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    if not hasattr(os, "major"):
        monkeypatch.setattr(os, "major", lambda device: device >> 8, raising=False)
        monkeypatch.setattr(os, "minor", lambda device: device & 255, raising=False)


def simulate_alias(monkeypatch, logical, physical):
    """Exact alias metadata, without requesting a Windows symlink privilege."""
    old_resolve, old_link, old_lstat = Path.resolve, Path.is_symlink, Path.lstat
    def resolve(path, *args, **kwargs):
        try: suffix = path.relative_to(logical)
        except ValueError: return old_resolve(path, *args, **kwargs)
        return old_resolve(physical / suffix, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == logical or old_link(path))
    monkeypatch.setattr(Path, "lstat", lambda path, *args, **kwargs:
        old_lstat(physical, *args, **kwargs) if path == logical else old_lstat(path, *args, **kwargs))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir(); repo.mkdir()
    locks = home / "thread-writer-locks"
    locks.mkdir()
    lock = locks / (THREAD + ".lock")
    lock.write_bytes(b"original native lock fixture")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,cwd,source,thread_source,archived)")
        db.execute("INSERT INTO threads VALUES(?,?,'vscode','user',0)", (THREAD, str(repo)))
    calls = []
    def sample(path, identity, pid, platform):
        calls.append((path, identity.st_dev, identity.st_ino, pid, platform))
        birth = {"linux": "linux:" + BOOT + ":123", "darwin": "darwin:1790000000:123456",
                 "win32": "win32:133000000000000000"}[platform]
        return pid, birth
    monkeypatch.setattr(observed, "_sample", sample)
    monkeypatch.setattr(observed.socket, "gethostname", lambda: "fixture-node")
    return SimpleNamespace(home=home, repo=repo, lock=lock, calls=calls, sample=sample,
        capture=lambda clock=lambda: 1800000000.0:
            observed.capture_writer(home, THREAD, repo, PID, clock=clock))


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_exact_point_envelope_has_actual_writer_subject_and_no_epoch(rig, monkeypatch, platform):
    monkeypatch.setattr(observed.sys, "platform", platform)
    before = {p: p.read_bytes() for p in rig.home.rglob("*") if p.is_file()}
    value = rig.capture()
    assert set(value) == {"codex_home", "repo_path", "thread_id", "observed_at", "writer"}
    assert set(value["writer"]) == {"kind", "platform", "node", "pid", "process_birth",
        "lock_path", "lock_device", "lock_inode"}
    assert value["writer"]["kind"] == "observed_native_writer"
    assert value["writer"]["pid"] == PID and value["writer"]["platform"] == platform
    assert value["writer"]["lock_path"] == str(rig.lock)
    assert value["writer"]["lock_inode"] == rig.lock.stat().st_ino
    assert len(rig.calls) == 2
    assert {p: p.read_bytes() for p in rig.home.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("fault", ["pid", "birth", "invalid_birth", "lock_replaced", "scope_changed"])
def test_replacement_or_malformed_subject_never_returns_positive(rig, monkeypatch, fault):
    monkeypatch.setattr(observed.sys, "platform", "darwin")
    count = [0]
    def sample(*args):
        count[0] += 1
        result = rig.sample(*args)
        if count[0] == 2:
            if fault == "pid": return PID + 1, result[1]
            if fault == "birth": return PID, "darwin:1790000000:123457"
            if fault == "invalid_birth": return PID, "invented-clock"
            if fault == "lock_replaced":
                replacement = rig.lock.with_suffix(".next")
                replacement.write_bytes(b"replacement")
                replacement.replace(rig.lock)
            if fault == "scope_changed":
                with sqlite3.connect(rig.home / "state_5.sqlite") as db:
                    db.execute("UPDATE threads SET archived=1")
        if fault == "invalid_birth": return PID, "invented-clock"
        return result
    monkeypatch.setattr(observed, "_sample", sample)
    with pytest.raises(NativeInflightError): rig.capture()


@pytest.mark.parametrize("pid", [True, None, 0, -1, 2**31, "321"])
def test_pid_must_be_a_real_selected_positive_native_pid(rig, pid):
    with pytest.raises(NativeInflightError):
        observed.capture_writer(rig.home, THREAD, rig.repo, pid, clock=lambda: 1800000000)
    assert not rig.calls


@pytest.mark.parametrize("times", [(100, 99), (100, 106), (float("nan"), 100)])
def test_stale_or_reversed_capture_cannot_be_freshened(rig, monkeypatch, times):
    monkeypatch.setattr(observed.sys, "platform", "darwin")
    values = iter(times)
    with pytest.raises(NativeInflightError): rig.capture(lambda: next(values))


@pytest.mark.parametrize("fault", ["missing", "archived", "wrong_repo", "view", "no_index"])
def test_exact_native_thread_repo_is_checked_before_os_queries(rig, fault):
    path = rig.home / "state_5.sqlite"
    with sqlite3.connect(path) as db:
        if fault == "missing": db.execute("DELETE FROM threads")
        elif fault == "archived": db.execute("UPDATE threads SET archived=1")
        elif fault == "wrong_repo": db.execute("UPDATE threads SET cwd=?", (str(rig.home),))
        else:
            db.execute("ALTER TABLE threads RENAME TO original_threads")
            if fault == "view": db.execute("CREATE VIEW threads AS SELECT * FROM original_threads")
            else: db.execute("CREATE TABLE threads AS SELECT * FROM original_threads")
    with pytest.raises(NativeInflightError): rig.capture()
    assert not rig.calls


@pytest.mark.parametrize("fault", ["symlink", "hardlink", "foreign_user"])
def test_unknown_lock_alias_or_foreign_owner_is_refused(rig, monkeypatch, fault):
    monkeypatch.setattr(observed.sys, "platform", "darwin")
    if fault == "symlink":
        saved = rig.lock.with_suffix(".saved")
        rig.lock.rename(saved)
        simulate_alias(monkeypatch, rig.lock, saved)
    elif fault == "hardlink": os.link(rig.lock, rig.lock.with_suffix(".alias"))
    else:
        original = observed._lock_identity
        def foreign(path, platform):
            info = path.lstat()
            assert info.st_uid == os.getuid()
            monkeypatch.setattr(observed.os, "getuid", lambda: info.st_uid + 1)
            return original(path, platform)
        monkeypatch.setattr(observed, "_lock_identity", foreign)
    with pytest.raises(NativeInflightError): rig.capture()
    assert not rig.calls


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX UID alias fixture; unchanged assertions also run on Linux")
def test_existing_linux_private_runtime_alias_keeps_logical_subject(rig, monkeypatch):
    from codex_watchdog import control_state
    monkeypatch.setattr(observed.sys, "platform", "linux")
    native = rig.home.parent / "private-user" / "codex-native-runtime"
    physical = native / "thread-writer-locks"
    physical.mkdir(parents=True, mode=0o700)
    for directory in (native.parent, native, physical): directory.chmod(0o700)
    rig.lock.rename(physical / rig.lock.name)
    rig.lock.parent.rmdir()
    simulate_alias(monkeypatch, rig.lock.parent, physical)
    monkeypatch.setattr(control_state, "control_native_runtime_root", lambda: native)
    value = rig.capture()
    assert value["writer"]["lock_path"] == str(rig.lock)
    assert all(call[0] == physical / rig.lock.name for call in rig.calls)
    assert value["writer"]["lock_inode"] == (physical / rig.lock.name).stat().st_ino


@pytest.mark.parametrize("fault", ["foreign_alias", "changed_alias"])
def test_linux_unknown_or_changed_runtime_alias_is_not_a_writer(rig, monkeypatch, fault):
    monkeypatch.setattr(observed.sys, "platform", "linux")
    if fault == "foreign_alias":
        other = rig.home / "foreign"
        rig.lock.parent.rename(other)
        simulate_alias(monkeypatch, rig.lock.parent, other)
    else:
        original = observed._resource_path
        calls = [0]
        def changed(*args):
            calls[0] += 1
            path = original(*args)
            return path if calls[0] == 1 else path.with_suffix(".changed")
        monkeypatch.setattr(observed, "_resource_path", changed)
    with pytest.raises(NativeInflightError): rig.capture()


def linux_lock(info, pid=PID, mode=b"FLOCK ADVISORY WRITE", end=b"0 EOF"):
    device = "{:x}:{:x}:{}".format(os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    return b"1: " + mode + b" " + str(pid).encode() + b" " + device.encode() + b" " + end + b"\n"


@pytest.mark.parametrize("fault", [None, "none", "multiple", "read", "partial", "foreign_inode"])
def test_linux_holder_uses_current_full_write_flock_identity(rig, monkeypatch, fault):
    info = rig.lock.stat()
    raw = linux_lock(info)
    if fault == "none": raw = b""
    if fault == "multiple": raw += linux_lock(info, PID + 1)
    if fault == "read": raw = linux_lock(info, mode=b"FLOCK ADVISORY READ")
    if fault == "partial": raw = linux_lock(info, end=b"10 20")
    if fault == "foreign_inode": raw = raw.replace(str(info.st_ino).encode(), str(info.st_ino + 1).encode())
    monkeypatch.setattr(observed, "_read_bounded", lambda path, limit: raw)
    if fault is None: assert observed._linux_holder(rig.lock, info) == PID
    else:
        with pytest.raises(NativeInflightError): observed._linux_holder(rig.lock, info)


@pytest.mark.parametrize("fault", [None, "zombie", "bad_ticks", "foreign_user", "bad_boot"])
def test_linux_birth_uses_boot_and_actual_start_ticks(rig, monkeypatch, fault):
    original = Path.stat
    def proc_stat(path, *args, **kwargs):
        if path == Path("/proc") / str(PID):
            return SimpleNamespace(st_uid=os.getuid() + int(fault == "foreign_user"))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", proc_stat)
    fields = [b"Z" if fault == "zombie" else b"S"] + [b"0"] * 18 + [b"0" if fault == "bad_ticks" else b"4321"]
    data = (str(PID) + " (app ) server) ").encode() + b" ".join(fields)
    monkeypatch.setattr(observed, "_read_bounded", lambda path, limit:
        (b"bad" if fault == "bad_boot" else BOOT.encode()) if path.name == "boot_id" else data)
    if fault is None: assert observed._linux_birth(PID) == "linux:" + BOOT + ":4321"
    else:
        with pytest.raises((NativeInflightError, ValueError)): observed._linux_birth(PID)


@pytest.mark.parametrize("fault", [None, "short", "wrong_pid", "foreign_user", "zombie", "exiting", "bad_usec"])
def test_darwin_birth_uses_exact_proc_bsdinfo_not_wall_clock(monkeypatch, fault):
    def query(pid, flavor, arg, pointer, size):
        assert (pid, flavor, arg, size) == (PID, 3, 0, 136)
        value = ctypes.cast(pointer, ctypes.POINTER(observed._ProcBSDInfo)).contents
        value.pid = PID + int(fault == "wrong_pid")
        value.uid = value.ruid = os.getuid() + int(fault == "foreign_user")
        value.status = 5 if fault == "zombie" else 2
        value.flags = 4 if fault == "exiting" else 0
        value.start_seconds = 1790000000
        value.start_microseconds = 1000000 if fault == "bad_usec" else 123456
        return 0 if fault == "short" else size
    api = SimpleNamespace(proc_pidinfo=Function(query))
    monkeypatch.setattr(observed.ctypes, "CDLL", lambda name, **kwargs: api)
    if fault is None: assert observed._darwin_birth(PID) == "darwin:1790000000:123456"
    else:
        with pytest.raises(NativeInflightError): observed._darwin_birth(PID)


def lsof_data(rig):
    info = rig.lock.stat()
    return ("p{}\nu{}\nf17\nD0x{:x}\ni{}\nn{}\ntREG\nlW\n".format(
        PID, os.getuid(), info.st_dev, info.st_ino, rig.lock)).encode()


@pytest.mark.parametrize("fault", [None, "no_lock", "partial", "read", "unknown", "wrong_pid", "wrong_uid",
    "wrong_device", "wrong_inode", "wrong_path", "duplicate", "truncated", "oversized"])
def test_darwin_openers_without_explicit_exact_write_lock_are_not_owners(rig, monkeypatch, fault):
    raw = lsof_data(rig)
    if fault == "no_lock": raw = raw.replace(b"lW\n", b"")
    if fault in ("partial", "read", "unknown"):
        raw = raw.replace(b"lW", {"partial": b"lw", "read": b"lR", "unknown": b"lU"}[fault])
    if fault == "wrong_pid": raw = raw.replace(("p" + str(PID)).encode(), b"p999")
    if fault == "wrong_uid": raw = raw.replace(("u" + str(os.getuid())).encode(), b"u999999")
    if fault == "wrong_device": raw = raw.replace(("D0x{:x}".format(rig.lock.stat().st_dev)).encode(), b"D0x0")
    if fault == "wrong_inode": raw = raw.replace(("i" + str(rig.lock.stat().st_ino)).encode(), b"i1")
    if fault == "wrong_path": raw = raw.replace(str(rig.lock).encode(), str(rig.repo).encode())
    if fault == "duplicate": raw += raw[raw.index(b"f17"):]
    if fault == "truncated": raw = raw[:-1]
    if fault == "oversized": raw = b"x" * (observed.LSOF_BYTES + 1)
    monkeypatch.setattr(observed, "_run_lsof", lambda path, pid: raw)
    if fault is None: assert observed._darwin_holder(rig.lock, rig.lock.stat(), PID) == PID
    else:
        with pytest.raises(NativeInflightError): observed._darwin_holder(rig.lock, rig.lock.stat(), PID)


@pytest.mark.parametrize("fault", [None, "timeout", "overflow", "nonzero"])
def test_lsof_child_has_fixed_args_timeout_and_predecode_output_cap(rig, monkeypatch, fault):
    events, reads = [], []
    pipe = SimpleNamespace(fileno=lambda: 77, close=lambda: events.append("closed"))
    state = [None]
    def wait(timeout):
        events.append(("wait", timeout))
        state[0] = 1 if fault == "nonzero" else 0
        return state[0]
    def kill(): events.append("kill"); state[0] = -9
    child = SimpleNamespace(stdout=pipe, wait=wait, poll=lambda: state[0], kill=kill)
    def create(args, **kwargs):
        assert args == ["/usr/sbin/lsof", "-nP", "-Di", "-a", "-p", str(PID),
            "-F", "pufDintl", "--", str(rig.lock)]
        assert kwargs["stderr"] == observed.subprocess.DEVNULL
        assert set(kwargs["env"]) == {"PATH", "LC_ALL", "LANG"}
        return child
    monkeypatch.setattr(observed.subprocess, "Popen", create)
    monkeypatch.setattr(observed.time, "monotonic", lambda: 100)
    monkeypatch.setattr(observed.select, "select", lambda *args: ([], [], []) if fault == "timeout" else ([pipe], [], []))
    output = iter([b"proof", b""])
    def read(fd, size):
        reads.append(size)
        return b"x" * size if fault == "overflow" else next(output)
    monkeypatch.setattr(observed.os, "read", read)
    if fault is None: assert observed._run_lsof(rig.lock, PID) == b"proof"
    else:
        with pytest.raises(NativeInflightError): observed._run_lsof(rig.lock, PID)
    assert "closed" in events and all(0 < size <= 8192 for size in reads)
    assert ("kill" in events) == (fault in ("timeout", "overflow"))


def fake_rm(monkeypatch, pid=PID, birth=123456789, count=1):
    events = []
    def get_list(handle, needed, size, entries, reason):
        if entries is None:
            needed._obj.value = count
            return 234
        size._obj.value = count
        entries[0].process.pid = pid
        entries[0].process.started.dwHighDateTime = birth >> 32
        entries[0].process.started.dwLowDateTime = birth & 0xffffffff
        return 0
    api = SimpleNamespace(RmStartSession=Function(lambda *args: 0),
        RmRegisterResources=Function(lambda *args: events.append("register") or 0),
        RmGetList=Function(get_list), RmEndSession=Function(lambda *args: events.append("end") or 0))
    monkeypatch.setattr(writer_process.ctypes, "WinDLL", lambda name: api)
    return events


@pytest.mark.parametrize("birth", [0, 123456789, (1 << 48) + 456])
def test_rm_birth_exposure_preserves_previous_pid_api(rig, monkeypatch, birth):
    events = fake_rm(monkeypatch, birth=birth)
    assert writer_process._windows_resource_process(rig.lock) == PID
    assert writer_process.windows_resource_identity(rig.lock) == ((PID, birth) if birth else None)
    assert events == ["register", "end", "register", "end"]


def test_rm_multiple_resource_users_never_selects_one(rig, monkeypatch):
    events = fake_rm(monkeypatch, count=2)
    assert writer_process.windows_resource_identity(rig.lock) is None
    assert events == ["register", "end"]


@pytest.mark.parametrize("fault", [None, "birth", "exited", "exits_during_read", "process_user", "file_user", "open_refused"])
def test_windows_same_handle_birth_liveness_and_sids_are_required(rig, monkeypatch, fault):
    closed, freed, exits = [], [], []
    def number(value): return value.value if hasattr(value, "value") else value
    def process_times(handle, created, *rest):
        created._obj.dwLowDateTime = 123456790 if fault == "birth" else 123456789
        return 1
    def exit_code(handle, pointer):
        exits.append(handle)
        pointer._obj.value = 0 if fault == "exited" or fault == "exits_during_read" and len(exits) > 1 else 259
        return 1
    def token(handle, access, pointer): pointer._obj.value = 3 if handle == 1 else 4; return 1
    def token_info(handle, kind, buffer, size, needed):
        needed._obj.value = ctypes.sizeof(ctypes.c_void_p)
        if buffer is not None:
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = 200 if number(handle) == 4 and fault == "process_user" else 100
            return 1
        return 0
    def file_owner(path, kind, flags, owner, group, acl, sacl, descriptor):
        assert path == str(rig.lock) and (kind, flags) == (1, 1)
        owner._obj.value = 300 if fault == "file_user" else 100
        descriptor._obj.value = 444
        return 0
    kernel = SimpleNamespace(GetCurrentProcess=Function(lambda: 1),
        OpenProcess=Function(lambda *args: 0 if fault == "open_refused" else 2),
        GetProcessTimes=Function(process_times), GetExitCodeProcess=Function(exit_code),
        CloseHandle=Function(lambda value: closed.append(number(value)) or 1),
        LocalFree=Function(lambda value: freed.append(number(value)) or 0))
    security = SimpleNamespace(OpenProcessToken=Function(token), GetTokenInformation=Function(token_info),
        EqualSid=Function(lambda a, b: number(a) == number(b)), GetNamedSecurityInfoW=Function(file_owner))
    monkeypatch.setattr(observed.ctypes, "WinDLL", lambda name, **kwargs: kernel if name == "kernel32" else security)
    assert observed._windows_same_user(rig.lock, PID, 123456789) is (fault is None)
    if fault == "open_refused": assert not closed
    elif fault in ("birth", "exited"): assert closed == [2]
    else: assert sorted(closed) == [2, 3, 4] and freed == [444]


def test_bounded_metadata_reader_does_not_transfer_oversized_native_file(tmp_path):
    path = tmp_path / "metadata"
    path.write_bytes(b"x" * 100)
    with pytest.raises(NativeInflightError, match="metadata_oversized"):
        observed._read_bounded(path, 10)
