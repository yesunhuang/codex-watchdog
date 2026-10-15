"""Point evidence for an existing native writer, without acquiring ownership.

The caller selects ``expected_pid`` from its existing exact editor route and
holds its observation/sender fences. This helper verifies only the current
resource holder, same-user process generation and exact native thread/repo.
It creates no control epoch, client, session or native state. A successful
sample is not idle, uninterrupted ownership, model completion or GUI proof.

Darwin requires lsof's explicit whole-file WRITE lock field. Builds which
report only an opener or omit lock attribution are unsupported and refuse.
"""
import ctypes
import os
from pathlib import Path
import re
import select
import socket
import stat
import subprocess
import sys
import time
import uuid

from .native_inflight import NativeInflightError, PROOF_SECONDS, _number, _scope


LOCK_BYTES = 1024 * 1024
LSOF_BYTES = 65536
LSOF_SECONDS = 2.0


def _read_bounded(path, limit):
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise NativeInflightError("observed_writer_metadata_oversized")
    return value


def _linux_birth(pid):
    directory = Path("/proc") / str(pid)
    if directory.stat().st_uid != os.getuid():
        raise NativeInflightError("observed_writer_foreign_user")
    raw = _read_bounded(directory / "stat", 16384)
    head, separator, tail = raw.rpartition(b") ")
    fields = tail.split()
    if (not separator or not head.startswith((str(pid) + " (").encode())
            or len(fields) < 20 or fields[0] in (b"Z", b"X", b"x")
            or not fields[19].isdigit() or int(fields[19]) <= 0):
        raise NativeInflightError("observed_writer_process_unavailable")
    boot = _read_bounded(Path("/proc/sys/kernel/random/boot_id"), 128).decode("ascii").strip()
    if str(uuid.UUID(boot)) != boot:
        raise NativeInflightError("observed_writer_process_unavailable")
    return "linux:{}:{}".format(boot, int(fields[19]))


def _linux_holder(path, identity):
    owners = set()
    for line in _read_bounded(Path("/proc/locks"), LOCK_BYTES).splitlines():
        fields = line.split()
        if (len(fields) != 8 or fields[1:4] != [b"FLOCK", b"ADVISORY", b"WRITE"]
                or fields[6:] != [b"0", b"EOF"]):
            continue
        try:
            major, minor, inode = fields[5].split(b":")
            if (int(major, 16), int(minor, 16), int(inode)) == (
                    os.major(identity.st_dev), os.minor(identity.st_dev), identity.st_ino):
                owners.add(int(fields[4]))
        except ValueError:
            raise NativeInflightError("observed_writer_lock_unavailable") from None
    if len(owners) != 1 or next(iter(owners)) <= 0:
        raise NativeInflightError("observed_writer_lock_unavailable")
    return owners.pop()


class _ProcBSDInfo(ctypes.Structure):
    # Apple's sys/proc_info.h: PROC_PIDTBSDINFO == 3, sizeof == 136.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
        ("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64)]


def _darwin_birth(pid):
    api = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = api.proc_pidinfo
    function.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                         ctypes.c_void_p, ctypes.c_int)
    function.restype = ctypes.c_int
    value = _ProcBSDInfo()
    size = ctypes.sizeof(value)
    count = function(pid, 3, 0, ctypes.byref(value), size)
    if (size != 136 or count != size or value.pid != pid or value.status == 5
            or value.flags & 4 or value.uid != os.getuid() or value.ruid != os.getuid()
            or value.start_seconds <= 0 or value.start_microseconds >= 1000000):
        raise NativeInflightError("observed_writer_process_unavailable")
    return "darwin:{}:{}".format(value.start_seconds, value.start_microseconds)


def _run_lsof(path, pid):
    """Bound the fixed local inspection child, including output before decode."""
    command = ["/usr/sbin/lsof", "-nP", "-Di", "-a", "-p", str(pid),
               "-F", "pufDintl", "--", str(path)]
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, close_fds=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"})
    deadline = time.monotonic() + LSOF_SECONDS
    raw = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                raise NativeInflightError("observed_writer_resource_timeout")
            block = os.read(child.stdout.fileno(), min(8192, LSOF_BYTES + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
            if len(raw) > LSOF_BYTES:
                raise NativeInflightError("observed_writer_metadata_oversized")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.wait(timeout=remaining) != 0:
            raise NativeInflightError("observed_writer_lock_unavailable")
        return bytes(raw)
    finally:
        if child.poll() is None:
            child.kill()  # Only our bounded inspection child, never the writer.
            child.wait(timeout=LSOF_SECONDS)
        child.stdout.close()


def _darwin_holder(path, identity, expected_pid):
    raw = _run_lsof(path, expected_pid)
    if len(raw) > LSOF_BYTES or not raw.endswith(b"\n"):
        raise NativeInflightError("observed_writer_lock_unavailable")
    process, files, current = {}, [], None
    for raw_line in raw.splitlines():
        line = raw_line.decode("utf-8", errors="strict")
        if not line:
            raise NativeInflightError("observed_writer_lock_unavailable")
        key, value = line[0], line[1:]
        if key == "p":
            if process or current is not None:
                raise NativeInflightError("observed_writer_lock_unavailable")
        if key == "f":
            if current is not None:
                files.append(current)
            current = {}
        target = process if current is None else current
        if key in target:
            raise NativeInflightError("observed_writer_lock_unavailable")
        target[key] = value
    if current is not None:
        files.append(current)
    if process != {"p": str(expected_pid), "u": str(os.getuid())} or len(files) != 1:
        raise NativeInflightError("observed_writer_lock_unavailable")
    file = files[0]
    if (set(file) != {"f", "D", "i", "n", "t", "l"} or not file["f"].isdigit()
            or file["l"] != "W" or file["t"] != "REG" or file["n"] != str(path)
            or re.fullmatch(r"0x[0-9a-fA-F]+", file["D"]) is None
            or not file["i"].isdigit() or int(file["D"], 16) != identity.st_dev
            or int(file["i"]) != identity.st_ino):
        raise NativeInflightError("observed_writer_lock_unavailable")
    return expected_pid


def _windows_same_user(path, pid, expected_birth):
    """Bind RM birth to one live process handle and same-user SIDs."""
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetProcessTimes.argtypes = (w.HANDLE, ctypes.POINTER(w.FILETIME),
        ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME))
    kernel.GetProcessTimes.restype = w.BOOL
    kernel.GetExitCodeProcess.argtypes = (w.HANDLE, ctypes.POINTER(w.DWORD))
    kernel.GetExitCodeProcess.restype = w.BOOL
    kernel.CloseHandle.argtypes = (w.HANDLE,)
    kernel.CloseHandle.restype = w.BOOL
    kernel.LocalFree.argtypes = (w.HLOCAL,)
    kernel.LocalFree.restype = w.HLOCAL
    security.OpenProcessToken.argtypes = (w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
    security.OpenProcessToken.restype = w.BOOL
    security.GetTokenInformation.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            w.DWORD, ctypes.POINTER(w.DWORD))
    security.GetTokenInformation.restype = w.BOOL
    security.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    security.EqualSid.restype = w.BOOL
    security.GetNamedSecurityInfoW.argtypes = (w.LPWSTR, ctypes.c_int, w.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    security.GetNamedSecurityInfoW.restype = w.DWORD
    process, handles, descriptor = None, [], ctypes.c_void_p()
    try:
        process = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            return False
        created, exited, system, user = (w.FILETIME() for _ in range(4))
        status = w.DWORD()
        if (not kernel.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(system), ctypes.byref(user))
                or ((int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)) != expected_birth
                or not kernel.GetExitCodeProcess(process, ctypes.byref(status)) or status.value != 259):
            return False
        def token_sid(handle):
            token = w.HANDLE()
            if not security.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
                raise OSError("token unavailable")
            handles.append(token)
            needed = w.DWORD()
            security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not 0 < needed.value <= 4096:
                raise OSError("token unavailable")
            buffer = ctypes.create_string_buffer(needed.value)
            if not security.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
                raise OSError("token unavailable")
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid:
                raise OSError("token unavailable")
            return buffer, sid
        current_buffer, current_sid = token_sid(kernel.GetCurrentProcess())
        process_buffer, process_sid = token_sid(process)
        owner = ctypes.c_void_p()
        if security.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(owner),
                None, None, None, ctypes.byref(descriptor)) != 0 or not owner.value:
            return False
        return bool(security.EqualSid(current_sid, process_sid)
                    and security.EqualSid(current_sid, owner)
                    and kernel.GetExitCodeProcess(process, ctypes.byref(status)) and status.value == 259)
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
        if process:
            kernel.CloseHandle(process)
        if descriptor.value:
            kernel.LocalFree(descriptor)


def _sample(path, identity, pid, platform):
    if platform == "linux":
        holder = _linux_holder(path, identity)
        if holder != pid:
            raise NativeInflightError("observed_writer_changed")
        return holder, _linux_birth(holder)
    if platform == "darwin":
        holder = _darwin_holder(path, identity, pid)
        return holder, _darwin_birth(holder)
    if platform == "win32":
        from .writer_process import windows_resource_identity
        from .workspace_discovery import writer_lock_is_held
        value = windows_resource_identity(path)
        if (value is None or value[0] != pid or not writer_lock_is_held(path)
                or not _windows_same_user(path, value[0], value[1])):
            raise NativeInflightError("observed_writer_lock_unavailable")
        return value[0], "win32:{}".format(value[1])
    raise NativeInflightError("observed_writer_platform_unsupported")


def _lock_identity(path, platform):
    value = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
            or type(value.st_dev) is not int or value.st_dev < 0
            or type(value.st_ino) is not int or value.st_ino <= 0
            or platform != "win32" and value.st_uid != os.getuid()):
        raise NativeInflightError("observed_writer_lock_unavailable")
    return value


def _resource_path(home, thread, platform):
    if platform == "linux":
        from .control_state import control_native_writer_lock
        return control_native_writer_lock(home, thread)
    return Path(home) / "thread-writer-locks" / (thread + ".lock")


def capture_writer(home, thread, repo, expected_pid, *, clock=None):
    """Capture two matching point samples under the caller's existing fences."""
    from .native_stream_work import stream_exact_thread
    clock = time.time if clock is None else clock
    try:
        scope = _scope(home, thread, repo)
        if type(expected_pid) is not int or not 0 < expected_pid < 2**31:
            raise NativeInflightError("observed_writer_pid_invalid")
        platform = sys.platform
        if platform not in ("linux", "darwin", "win32"):
            raise NativeInflightError("observed_writer_platform_unsupported")
        started = clock()
        if not _number(started) or not stream_exact_thread(Path(home), thread, Path(repo)):
            raise NativeInflightError("observed_writer_scope_unverified")
        logical = Path(home) / "thread-writer-locks" / (thread + ".lock")
        path = _resource_path(home, thread, platform)
        before = _lock_identity(path, platform)
        first = _sample(path, before, expected_pid, platform)
        second = _sample(path, before, expected_pid, platform)
        after = _lock_identity(path, platform)
        same_resource = _resource_path(home, thread, platform) == path
        same_scope = stream_exact_thread(Path(home), thread, Path(repo))
        current = clock()
        node = socket.gethostname()
        if (first != second or first[0] != expected_pid
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or not same_resource or not same_scope
                or not _number(current) or not 0 <= current - started <= PROOF_SECONDS
                or not isinstance(node, str) or not 1 <= len(node) <= 256
                or any(char in node for char in ("\0", "\n", "/", "\\"))):
            raise NativeInflightError("observed_writer_changed")
        writer = dict(kind="observed_native_writer", platform=platform, node=node,
            pid=expected_pid, process_birth=first[1], lock_path=str(logical),
            lock_device=after.st_dev, lock_inode=after.st_ino)
        from .native_observed_work import validate_writer
        writer = validate_writer(writer, scope)
        return dict(**scope, observed_at=current, writer=writer)
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError,
            subprocess.SubprocessError) as error:
        if isinstance(error, NativeInflightError):
            raise
        raise NativeInflightError("observed_writer_unavailable") from None
