"""Identify a held thread lock's process without controlling that process."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
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
    # Do not infer a writer from old logs on a host without process evidence.
    return None


def _windows_resource_process(path: Path) -> Optional[int]:
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
                    return pid if pid > 0 else None
                return None
            if code != 234 or not 0 < needed.value <= 100:
                return None
            count.value = needed.value
            entries = (ProcessInfo * count.value)()
        return None
    finally:
        api.RmEndSession(handle)
