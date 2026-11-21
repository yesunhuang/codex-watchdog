"""Identify a held thread lock's process without controlling that process."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
import time
from typing import Optional


def writer_lock_process(path: Path) -> Optional[int]:
    if not path.is_file():
        return None
    if os.name == "nt":
        try:
            return _windows_resource_process(path)
        except (OSError, ValueError):
            return None
    if sys.platform == "linux":
        from .control_state import ControlError, control_kernel_owner
        try:
            return control_kernel_owner(path)
        except ControlError:
            return None
    if sys.platform == "darwin":
        try:
            return _darwin_resource_process(path)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    # Do not infer a writer from old logs on a host without process evidence.
    return None


_DARWIN_RESOURCE_BYTES = 65536
_DARWIN_RESOURCE_SECONDS = 2.0


def _darwin_resource_query(path: Path, deadline: float) -> bytes:
    """One exact file query, with a shared deadline and a pre-decode byte cap."""
    child = subprocess.Popen(
        ["/usr/sbin/lsof", "-nP", "-F", "pufDint", "--", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        close_fds=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"},
    )
    raw = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                raise ValueError("writer_resource_timeout")
            block = os.read(child.stdout.fileno(), min(8192, _DARWIN_RESOURCE_BYTES + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
            if len(raw) > _DARWIN_RESOURCE_BYTES:
                raise ValueError("writer_resource_oversized")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.wait(timeout=remaining) != 0:
            raise ValueError("writer_resource_unavailable")
        return bytes(raw)
    finally:
        if child.poll() is None:
            child.kill()  # Only our inspection child; never the native writer.
            child.wait(timeout=_DARWIN_RESOURCE_SECONDS)
        child.stdout.close()


def _darwin_resource_pid(raw: bytes, path: Path, identity) -> Optional[int]:
    # A sole opener is association evidence only. Independent held-lock and
    # same-user microsecond process-birth checks below are also mandatory.
    if len(raw) > _DARWIN_RESOURCE_BYTES or not raw.endswith(b"\n"):
        return None
    fields = {}
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if not line or line[0] in fields:
            return None  # Includes another process or another file descriptor.
        fields[line[0]] = line[1:]
    if (set(fields) != set("pufDint") or not fields["p"].isdigit()
            or int(fields["p"]) <= 0 or fields["u"] != str(os.getuid())
            or not fields["f"].isdigit() or fields["t"] != "REG"
            or fields["n"] != str(path)
            or re.fullmatch(r"0x[0-9a-fA-F]+", fields["D"]) is None
            or int(fields["D"], 16) != identity.st_dev
            or not fields["i"].isdigit() or int(fields["i"]) != identity.st_ino):
        return None
    return int(fields["p"])


def _darwin_file_identity(path: Path):
    value = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.getuid() or value.st_nlink != 1):
        raise ValueError("writer_resource_identity")
    return value


def _darwin_lock_held(path: Path, identity) -> bool:
    import errno
    import fcntl

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino):
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _darwin_resource_process(path: Path) -> Optional[int]:
    from .observed_writer import _darwin_birth

    path = Path(os.path.abspath(path))
    deadline = time.monotonic() + _DARWIN_RESOURCE_SECONDS
    before = _darwin_file_identity(path)
    pid = _darwin_resource_pid(_darwin_resource_query(path, deadline), path, before)
    if pid is None:
        return None
    birth = _darwin_birth(pid)  # libproc: same effective/real UID, alive, exact birth.
    if not _darwin_lock_held(path, before):
        return None
    if _darwin_resource_pid(_darwin_resource_query(path, deadline), path, before) != pid:
        return None
    after = _darwin_file_identity(path)
    if ((before.st_dev, before.st_ino, before.st_uid, before.st_mode)
            != (after.st_dev, after.st_ino, after.st_uid, after.st_mode)
            or not _darwin_lock_held(path, after) or _darwin_birth(pid) != birth
            or time.monotonic() >= deadline):
        return None
    return pid


def _windows_resource_process(path: Path) -> Optional[int]:
    identity = _windows_resource_details(path)
    return identity[0] if identity is not None else None


def windows_resource_identity(path: Path):
    """Return the actual Restart Manager PID/FILETIME, never a guessed birth."""
    identity = _windows_resource_details(path)
    return identity if identity is not None and identity[1] > 0 else None


def _windows_resource_details(path: Path):
    """Query the sole resource user; the caller separately verifies the lock.

    Restart Manager registration is temporary. Only query/cleanup functions are
    used: never RmShutdown/RmRestart, handle closing, or process termination.
    """
    from ctypes import wintypes as w

    class UniqueProcess(ctypes.Structure):
        _fields_ = [("pid", w.DWORD), ("started", w.FILETIME)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("process", UniqueProcess), ("app_name", w.WCHAR * 256),
            ("service_name", w.WCHAR * 64), ("app_type", ctypes.c_int),
            ("status", w.ULONG), ("session_id", w.DWORD), ("restartable", w.BOOL),
        ]

    api = ctypes.WinDLL("Rstrtmgr.dll")
    api.RmStartSession.argtypes = [ctypes.POINTER(w.DWORD), w.DWORD, w.LPWSTR]
    api.RmRegisterResources.argtypes = [
        w.DWORD, w.UINT, ctypes.POINTER(w.LPCWSTR), w.UINT,
        ctypes.POINTER(UniqueProcess), w.UINT, ctypes.POINTER(w.LPCWSTR),
    ]
    api.RmGetList.argtypes = [
        w.DWORD, ctypes.POINTER(w.UINT), ctypes.POINTER(w.UINT),
        ctypes.POINTER(ProcessInfo), ctypes.POINTER(w.DWORD),
    ]
    api.RmEndSession.argtypes = [w.DWORD]
    for name in ("RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession"):
        getattr(api, name).restype = w.DWORD

    handle, key = w.DWORD(), ctypes.create_unicode_buffer(33)
    if api.RmStartSession(ctypes.byref(handle), 0, key) != 0:
        return None
    try:
        files = (w.LPCWSTR * 1)(str(path.resolve()))
        if api.RmRegisterResources(handle, 1, files, 0, None, 0, None) != 0:
            return None
        needed, count, reasons = w.UINT(), w.UINT(), w.DWORD()
        entries = None
        for _ in range(3):
            code = api.RmGetList(
                handle, ctypes.byref(needed), ctypes.byref(count), entries,
                ctypes.byref(reasons),
            )
            if code == 0:
                if entries is not None and count.value == 1:
                    pid = int(entries[0].process.pid)
                    started = entries[0].process.started
                    birth = (int(started.dwHighDateTime) << 32) | int(started.dwLowDateTime)
                    return (pid, birth) if pid > 0 else None
                return None
            if code != 234 or not 0 < needed.value <= 100:
                return None
            count.value = needed.value
            entries = (ProcessInfo * count.value)()
        return None
    finally:
        api.RmEndSession(handle)
