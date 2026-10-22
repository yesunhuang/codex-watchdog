"""Read-only Windows evidence when VS Code's ``--status`` diagnostic degrades.

Windows has no lsof-style open-file attribution for extension-host logs, and
Restart Manager cannot reliably name exthost log holders. A host is therefore
bound to its exact current exthost log generation by PID plus kernel birth
ordering: native GUI birth <= host birth <= child birth. Output-channel timestamps are
receipt times and may follow child creation by many seconds. Log timestamps
reject pre-host history, not establish an upper bound on native child birth.
Current role/cache plus the exact sole live held writer bridge thread ownership;
spawn lines only delimit routing generations, never prove a child PID.

Only current-user process metadata is read. No process is signalled, no
foreign handle is closed, Restart Manager is used for query only, and no
runtime, lock, log or conversation is written. A fallback-selected thread is
admitted only with the original held writer lock whose sole Restart Manager
holder is this exact live app-server generation. Every refusal is local to one
candidate; one inaccessible process never aborts other windows.

Finite independent collection and completion ``WorkBudget`` allowances bound
every directory entry, byte and SQLite VM step, each with a monotonic deadline.
Completion ordering rotates so collection pressure cannot starve later targets. Historical log sessions, workspace
storage and thread rows are never enumerated or scanned: only the verified GUI generation log
locators, exact current storage keys and exact thread IDs named by current
routing evidence are touched. Work beyond the budget is deferred (rotating the
start position between snapshots), never guessed.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import ctypes
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
import time
from typing import Callable, Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple

from .platform_adapters import VSCodeStatusProbe, is_codex_app_server_description
from .workspace_discovery import (
    _CODEX_SESSION_CACHE_KEY,
    _CODEX_SESSION_RESOURCE_PREFIX,
    CodexSessionResolver,
    LiveVSCodeWindow,
    SessionResolution,
    VSCodeLiveWindowIndex,
    _canonical_session_id,
    _path_identity,
)


FILETIME_UNIX_EPOCH = 116444736000000000
PROCESS_RECORD_LIMIT = 8192
CANDIDATE_LIMIT = 64  # Host identity groups per snapshot; the rest rotate in later.
APP_CHILD_LIMIT = 8
COLLECTION_SECONDS = 5.0  # Shared by collection, resolution and admission.
COMMAND_BYTES = 65536
LOG_STARTUP_SECONDS = 64  # Bounded locator only; never ownership evidence.
LOG_SESSION_LIMIT = 4
LOG_SESSION_SCAN_LIMIT = 512
WINDOW_SCAN_LIMIT = 256
DIRECTORY_ENTRY_BUDGET = 16384
EXTHOST_LOG_BYTES = 8 * 1024 * 1024
CODEX_LOG_BYTES = 16 * 1024 * 1024
TOTAL_LOG_BYTES = 48 * 1024 * 1024
WORKSPACE_JSON_BYTES = 65536
CACHE_VALUE_BYTES = 4 * 1024 * 1024
METADATA_JSON_BYTES = 4 * 1024 * 1024
LOG_LINE_LIMIT = 262144
SESSION_LOOKUP_LIMIT = 256
SQL_STEP_BUDGET = 2_000_000
SQL_PROGRESS_STEPS = 64
ANCESTRY_DEPTH = 2

FALLBACK_SOURCE = "codex_state_vscode_windows_native_writer"
_FOLLOWER_SOURCE = "codex_state_vscode_active_follower_verified_owner"
_DEFERRED = "windows_native_budget_deferred"
_THREAD_COLUMNS = frozenset({"id", "cwd", "source", "thread_source", "archived"})
# Exact-ID lookup with the same loaded-thread predicate as the shared resolver.
THREAD_LOOKUP = (
    "SELECT id, cwd FROM threads WHERE id = ? AND archived = 0 "
    "AND source = 'vscode' AND thread_source = 'user' LIMIT 4"
)
CACHE_SIZE_LOOKUP = "SELECT length(CAST(value AS BLOB)) FROM ItemTable WHERE key = ? LIMIT 2"

_SAFE_REASON = re.compile(r"[a-z0-9_]{1,80}")
_SESSION_NAME = re.compile(r"[0-9]{8}T[0-9]{6}")
_WINDOW_NAME = re.compile(r"window([0-9]{1,6})")
_TIMESTAMP = re.compile(rb"^([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3})\s")
_HOST_START = re.compile(rb"Extension host with pid ([0-9]+) started", re.IGNORECASE)
_HOST_EXIT = re.compile(rb"Extension host with pid ([0-9]+) exiting with code", re.IGNORECASE)
_HOST_TERMINATING = re.compile(rb"Extension host terminating:", re.IGNORECASE)
_STORAGE = re.compile(rb"workspaceStorage[\\/]([^\\/\s.]+)", re.IGNORECASE)
_SPAWN = b"[CodexMcpConnection] Spawning codex app-server"
_CONVERSATION = re.compile(rb"(?:^|\s)conversationId=([^\s]+)(?=\s|$)")
_ROLE = re.compile(rb"(?:^|\s)role=([^\s]+)(?=\s|$)")
_ASSIGNED_ROLE = re.compile(rb"(?:^|\s)assignedStreamRole=([^\s]+)(?=\s|$)")
_ACTIVE = re.compile(rb"(?:^|\s)active=(true|false)(?=\s|$)")
_USER_DATA_DIR = re.compile(r'--user-data-dir=(?:"([^"]*)"|(\S+))')
_UTILITY_HOST = re.compile(r"(?:^|\s)--type=utility(?:\s|$)")
_LEGACY_HOST = re.compile(r"(?:^|\s)--type=extensionHost(?:\s|$)")


class WorkBudget:
    """One discovery snapshot's shared byte, entry, SQL-step and time allowance."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.deadline = clock() + COLLECTION_SECONDS
        self.bytes = TOTAL_LOG_BYTES
        self.entries = DIRECTORY_ENTRY_BUDGET
        self.sql_steps = SQL_STEP_BUDGET
        self.bytes_charged = 0
        self.cache_charged = 0
        self.sql_used = 0
        self.reason: Optional[str] = None

    def live(self) -> bool:
        if self.reason is None and self.clock() > self.deadline:
            self.reason = "deadline_exceeded"
        return self.reason is None

    def exhaust(self, reason: str = "budget_exceeded") -> None:
        if self.reason is None:
            self.reason = reason

    def charge(self, size: int) -> bool:
        self.bytes -= size
        self.bytes_charged += size
        if self.bytes < 0:
            self.exhaust()
        return self.live()

    def meter(self, connection) -> None:
        """Abort any SQLite statement once the shared step budget is spent."""

        def progress() -> int:
            self.sql_steps -= SQL_PROGRESS_STEPS
            self.sql_used += SQL_PROGRESS_STEPS
            if self.sql_steps < 0:
                self.exhaust()
            return 0 if self.live() else 1

        connection.set_progress_handler(progress, SQL_PROGRESS_STEPS)


def _metadata_integer(token: str) -> int:
    # Match modern Python's decimal conversion ceiling even on older supported
    # Python 3.9 builds, before their quadratic unbounded int conversion runs.
    if len(token.lstrip("-")) > 4300:
        raise ValueError("metadata integer exceeds conversion bound")
    return int(token)


def read_metadata_json(path: Path, budget: WorkBudget, sleep) -> Optional[object]:
    """Bound input before parsing global-window or saved-workspace documents.

    This Windows-only reader starts before native-index collection. All retry
    bytes share one snapshot allowance. Oversized/unsafe targets refuse unread;
    concurrent growth can consume at most the cap plus one byte, never read(-1).
    """
    for attempt in range(3):
        if not budget.live():
            return None
        try:
            with path.open("rb") as handle:
                status = os.fstat(handle.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_size > METADATA_JSON_BYTES:
                    return None
                data = handle.read(min(METADATA_JSON_BYTES, max(0, budget.bytes)) + 1)
            if not budget.charge(len(data)) or len(data) > METADATA_JSON_BYTES:
                return None
            result = json.loads(data.decode("utf-8"), parse_int=_metadata_integer)
            return result if budget.live() else None
        except (OSError, UnicodeError, ValueError, RecursionError):
            if attempt < 2 and budget.live():
                sleep(0.02)
    return None


@dataclass(frozen=True)
class WindowsProcessEntry:
    pid: int
    parent_pid: int
    name: str


@dataclass(frozen=True)
class WindowsProcessIdentity:
    pid: int
    birth: int  # Kernel FILETIME creation time, 100 ns units.
    image: str
    command: str
    same_user: bool
    alive: bool


@dataclass(frozen=True)
class WindowsNativeProof:
    window_number: str
    storage_key: str
    host: WindowsProcessIdentity
    app: WindowsProcessIdentity
    exthost_log: Path
    codex_log: Path
    log_start_ms: int
    spawn_ms: int
    # Byte fingerprints of the exact generations, re-read at final admission.
    exthost_offset: int = 0
    exthost_line: bytes = b""
    codex_offset: int = 0
    codex_line: bytes = b""
    main: Optional[WindowsProcessIdentity] = None


@dataclass(frozen=True)
class CodexLogGeneration:
    """Routing evidence from the last App Server generation of one Codex.log."""

    spawn_ms: Optional[int]
    spawn_offset: Optional[int]
    spawn_line: bytes
    states: Dict[str, Tuple[Optional[str], Optional[bool]]]
    markers: FrozenSet[str]


def _lines(data: bytes, base: int) -> Iterator[Tuple[int, bytes]]:
    """Yield ``(absolute offset, line)`` split on LF like binary iteration."""
    position = 0
    while position < len(data):
        end = data.find(b"\n", position)
        stop = len(data) if end < 0 else end + 1
        yield base + position, data[position:stop]
        position = stop


def _conversation(line: bytes) -> Optional[str]:
    for match in _CONVERSATION.finditer(line):
        value = match.group(1).decode("ascii", errors="ignore")
        if _canonical_session_id(value) == value:
            return value
    return None


def filetime_ms(value: int) -> int:
    return (value - FILETIME_UNIX_EPOCH) // 10000


def local_log_time_ms(stamp: bytes) -> Optional[int]:
    """Convert VS Code's local ``YYYY-MM-DD HH:MM:SS.mmm`` log prefix."""
    try:
        parsed = time.strptime(stamp[:19].decode("ascii"), "%Y-%m-%d %H:%M:%S")
        return int(time.mktime(parsed)) * 1000 + int(stamp[20:23])
    except (ValueError, OverflowError, UnicodeDecodeError):
        return None


class Win32ProcessReader:
    """Documented read-only Win32 queries; each call refuses independently."""

    def __init__(self) -> None:
        from ctypes import wintypes as w

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                ("szExeFile", w.WCHAR * 260),
            ]

        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT),
                        ("Buffer", ctypes.c_void_p)]

        self._w, self._entry, self._unicode = w, ProcessEntry, UnicodeString
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        kernel.CreateToolhelp32Snapshot.argtypes = (w.DWORD, w.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        for name in ("Process32FirstW", "Process32NextW"):
            getattr(kernel, name).argtypes = (w.HANDLE, ctypes.POINTER(ProcessEntry))
            getattr(kernel, name).restype = w.BOOL
        kernel.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
        kernel.OpenProcess.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = (w.HANDLE, w.DWORD)
        kernel.WaitForSingleObject.restype = w.DWORD
        kernel.GetProcessTimes.argtypes = (w.HANDLE,) + (ctypes.POINTER(w.FILETIME),) * 4
        kernel.GetProcessTimes.restype = w.BOOL
        kernel.QueryFullProcessImageNameW.argtypes = (
            w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
        kernel.QueryFullProcessImageNameW.restype = w.BOOL
        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = (w.HANDLE,)
        kernel.CloseHandle.restype = w.BOOL
        security.OpenProcessToken.argtypes = (w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
        security.OpenProcessToken.restype = w.BOOL
        security.GetTokenInformation.argtypes = (
            w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD))
        security.GetTokenInformation.restype = w.BOOL
        security.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        security.EqualSid.restype = w.BOOL
        ntdll.NtQueryInformationProcess.argtypes = (
            w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG))
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        self._kernel, self._security, self._ntdll = kernel, security, ntdll

    def entries(self, limit: int) -> Optional[Tuple[WindowsProcessEntry, ...]]:
        kernel = self._kernel
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return None
        try:
            entry = self._entry()
            entry.dwSize = ctypes.sizeof(entry)
            rows: List[WindowsProcessEntry] = []
            found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                if len(rows) >= limit:
                    return None
                rows.append(WindowsProcessEntry(
                    int(entry.th32ProcessID), int(entry.th32ParentProcessID),
                    str(entry.szExeFile),
                ))
                found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
            return tuple(rows)
        finally:
            kernel.CloseHandle(snapshot)  # Our own snapshot handle only.

    def identity(self, pid: int) -> Optional[WindowsProcessIdentity]:
        w, kernel = self._w, self._kernel
        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE: no read/write/terminate.
        process = kernel.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not process:
            return None
        try:
            alive = kernel.WaitForSingleObject(process, 0) == 0x102  # WAIT_TIMEOUT
            times = [w.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(process, *(ctypes.byref(item) for item in times)):
                return None
            birth = (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)
            size = w.DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
                return None
            command = self._command(process)
            if command is None or birth <= 0:
                return None
            return WindowsProcessIdentity(
                pid, birth, image.value, command, self._same_user(process), alive,
            )
        finally:
            kernel.CloseHandle(process)

    def _command(self, process) -> Optional[str]:
        returned = self._w.ULONG()
        buffer = ctypes.create_string_buffer(COMMAND_BYTES)
        # ProcessCommandLineInformation; a larger command line is refused.
        if self._ntdll.NtQueryInformationProcess(
                process, 60, buffer, COMMAND_BYTES, ctypes.byref(returned)) != 0:
            return None
        value = self._unicode.from_buffer(buffer)
        start, base = value.Buffer or 0, ctypes.addressof(buffer)
        if (value.Length % 2 or not base <= start
                or start + value.Length > base + COMMAND_BYTES):
            return None
        return ctypes.wstring_at(start, value.Length // 2) if value.Length else ""

    def _sid(self, process, handles):
        w = self._w
        token = w.HANDLE()
        if not self._security.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            return None
        handles.append(token)
        needed = w.DWORD()
        self._security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value <= 4096:
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        if not self._security.GetTokenInformation(
                token, 1, buffer, needed.value, ctypes.byref(needed)):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        return (buffer, sid) if sid else None

    def _same_user(self, process) -> bool:
        handles: list = []
        try:
            current = self._sid(self._kernel.GetCurrentProcess(), handles)
            other = self._sid(process, handles)
            return bool(current and other and self._security.EqualSid(current[1], other[1]))
        finally:
            for handle in handles:
                self._kernel.CloseHandle(handle)

    def writer(self, lock: Path) -> Optional[Tuple[int, int]]:
        from .writer_process import windows_resource_identity
        return windows_resource_identity(lock)

    def lock_held(self, lock: Path) -> bool:
        from .workspace_discovery import writer_lock_is_held
        return writer_lock_is_held(lock)

    def writer_same_user(self, lock: Path, pid: int, birth: int) -> bool:
        from .observed_writer import _windows_same_user
        return _windows_same_user(lock, pid, birth)


class WindowsNativeCollector:
    """Collect exact live host/app-server/log generations once per snapshot."""

    def __init__(
        self,
        user_data_root: Path,
        *,
        reader=None,
        log_time_ms: Callable[[bytes], Optional[int]] = local_log_time_ms,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.user_data_root = Path(user_data_root)
        self._reader = reader
        self.log_time_ms = log_time_ms
        self.clock = clock
        self.refusals: Dict[str, int] = {}
        # Rotation cursors persist in memory only, so deferred work moves forward.
        self._cursors: Dict[str, int] = {}
        self._reset_snapshot()

    @property
    def reader(self):
        if self._reader is None:
            self._reader = Win32ProcessReader()
        return self._reader

    def _refuse(self, reason: str) -> None:
        self.refusals[reason] = self.refusals.get(reason, 0) + 1

    def _defer(self, budget: WorkBudget) -> None:
        # Records why work stopped; a per-snapshot work cap leaves the budget intact.
        self._refuse(budget.reason or "budget_exceeded")

    def _reset_snapshot(self) -> None:
        self.proof_budget = None
        self._exthost_cache: Dict[Path, Optional[tuple]] = {}
        self._exthost_readable = set()
        self._codex_cache: Dict[Path, Optional[CodexLogGeneration]] = {}
        self._session_cache: Optional[List[Path]] = None
        self._sessions_listed = False
        self._host_mains = {}
        self._main_sessions = {}

    def _rotated(self, name: str, items: list) -> list:
        if not items:
            return items
        start = self._cursors.get(name, 0) % len(items)
        return items[start:] + items[:start]

    def _advance(self, name: str, count: int) -> None:
        self._cursors[name] = self._cursors.get(name, 0) + count

    def collect(self, budget: Optional[WorkBudget] = None) -> Optional[Tuple[WindowsNativeProof, ...]]:
        budget = WorkBudget(self.clock) if budget is None else budget
        self.refusals = {}
        self._reset_snapshot()
        try:
            reader = self.reader
            entries = reader.entries(PROCESS_RECORD_LIMIT)
        except (OSError, AttributeError, ValueError, TypeError):
            entries = None
        if entries is None:
            self._refuse("unavailable")
            return None
        table, duplicates = self._table(entries)
        expected_host = self.user_data_root.parent.name + ".exe"
        # Table-only ancestry filtering costs no process queries.
        groups: Dict[int, List[int]] = {}
        for pid in sorted(pid for pid, entry in table.items()
                          if entry.name.casefold() == "codex.exe"):
            parent = self._parent(table, duplicates, pid, expected_host)
            if parent is not None:
                groups.setdefault(parent, []).append(pid)
        verified = {}
        processed = 0
        for parent in self._rotated("hosts", sorted(groups)):
            if processed >= CANDIDATE_LIMIT or not budget.live():
                # Defer, not refuse: later snapshots start at the next group.
                self._defer(budget)
                self._advance("hosts", processed)
                break
            processed += 1
            if len(groups[parent]) > APP_CHILD_LIMIT:
                self._refuse("multiple_app_servers")
                continue
            host = self._identity(reader, parent)
            if host is None or not self._is_host(host, expected_host):
                continue
            found = []
            for pid in groups[parent]:
                app = self._identity(reader, pid)
                if app is None or not self._is_app_server(app):
                    continue
                if host.birth > app.birth:
                    # A reused parent PID was born after its recorded child.
                    self._refuse("impossible_parent_edge")
                    continue
                found.append((host, app))
            if len(found) == 1:
                main = self._main_for_host(table, duplicates, found[0][0])
                if main is not None:
                    self._host_mains[parent] = main
                    verified[parent] = found[0]
            elif found:
                self._refuse("multiple_app_servers")
        if not verified:
            return ()
        mapped = self._map_logs(verified, budget)
        # A later incomplete GUI domain cannot consume the allowance needed to
        # finish proofs for an earlier completely checked independent domain.
        budget = self.proof_budget = WorkBudget(self.clock)
        proofs = []
        ordered = self._rotated("codex", sorted(verified))
        for position, parent in enumerate(ordered):
            host, app = verified[parent]
            mapping = mapped.get(parent)
            if mapping is None:
                continue
            number, storage_key, exthost_log, start_ms, offset, line = mapping
            if not filetime_ms(host.birth) <= start_ms:
                self._refuse("stale_host_generation")
                continue
            if not budget.live():
                self._defer(budget)
                self._advance("codex", position)
                break
            codex_log = exthost_log.parent / "openai.chatgpt" / "Codex.log"
            generation = self.codex_generation(codex_log, budget)
            if generation is None and not budget.live():
                self._defer(budget)
                self._advance("codex", position)
                break
            spawn_ms = generation.spawn_ms if generation is not None else None
            if spawn_ms is None or spawn_ms < filetime_ms(host.birth):
                self._refuse("stale_app_server_generation")
                continue
            proofs.append(WindowsNativeProof(
                number, storage_key, host, app, exthost_log, codex_log, start_ms, spawn_ms,
                offset, line, generation.spawn_offset or 0, generation.spawn_line,
                self._host_mains.get(parent),
            ))
        if not proofs:
            return ()
        # Recheck exact process identities after every log read.
        try:
            after = reader.entries(PROCESS_RECORD_LIMIT)
        except (OSError, AttributeError, ValueError, TypeError):
            after = None
        if after is None:
            self._refuse("unavailable")
            return None
        later, later_duplicates = self._table(after)
        current = []
        for proof in proofs:
            app_entry = later.get(proof.app.pid)
            host_entry = later.get(proof.host.pid)
            if (app_entry is None or host_entry is None
                    or {proof.app.pid, proof.host.pid} & later_duplicates
                    or app_entry != table.get(proof.app.pid)
                    or host_entry != table.get(proof.host.pid)
                    or self._identity(reader, proof.app.pid) != proof.app
                    or self._identity(reader, proof.host.pid) != proof.host):
                self._refuse("identity_changed")
                continue
            current.append(proof)
        return tuple(current)

    def app_generation_current(self, proof: WindowsNativeProof) -> bool:
        """Fresh table: same host, and this app is its only Codex child."""
        try:
            rows = self.reader.entries(PROCESS_RECORD_LIMIT)
        except (OSError, AttributeError, ValueError, TypeError):
            rows = None
        if rows is None:
            return False
        table, _duplicates = self._table(rows)
        host, app = table.get(proof.host.pid), table.get(proof.app.pid)
        children = [pid for pid, entry in table.items()
                    if entry.parent_pid == proof.host.pid
                    and entry.name.casefold() == "codex.exe"]
        expected = self.user_data_root.parent.name + ".exe"
        return (self.main_current(proof)
                and host is not None and app is not None
                and host.name.casefold() == expected.casefold()
                and app.parent_pid == proof.host.pid
                and children == [proof.app.pid])

    @staticmethod
    def _table(entries: Sequence[WindowsProcessEntry]):
        table: Dict[int, WindowsProcessEntry] = {}
        duplicates = set()
        for entry in entries:
            if entry.pid in table:
                duplicates.add(entry.pid)
            table[entry.pid] = entry
        for pid in duplicates:
            table.pop(pid, None)
        return table, duplicates

    def _parent(self, table, duplicates, pid: int, expected_host: str) -> Optional[int]:
        """Walk at most ANCESTRY_DEPTH recorded edges; reject impossible ones."""
        visited = {pid}
        chain = [pid]
        current = pid
        for _ in range(ANCESTRY_DEPTH):
            entry = table.get(current)
            if entry is None:
                break
            parent = entry.parent_pid
            if parent in visited or parent in duplicates:
                self._refuse("impossible_parent_edge")
                return None
            if parent <= 4 or parent not in table:
                break
            visited.add(parent)
            chain.append(parent)
            current = parent
        if len(chain) < 2 or table[chain[1]].name.casefold() != expected_host.casefold():
            return None
        return chain[1]

    def _identity(self, reader, pid: int) -> Optional[WindowsProcessIdentity]:
        try:
            identity = reader.identity(pid)
        except (OSError, ValueError, TypeError):
            identity = None
        if identity is None:
            self._refuse("process_inaccessible")
            return None
        if identity.pid != pid or not identity.alive:
            self._refuse("process_exited")
            return None
        if not identity.same_user:
            self._refuse("foreign_user")
            return None
        return identity

    @staticmethod
    def _is_app_server(identity: WindowsProcessIdentity) -> bool:
        image = identity.image.replace("\\", "/").casefold()
        return (
            PureWindowsPath(identity.image).name.casefold() == "codex.exe"
            and "/extensions/openai.chatgpt-" in image
            and identity.image.casefold() in identity.command.casefold()
            and is_codex_app_server_description(identity.command.replace('"', " "))
        )

    def _is_host(self, identity: WindowsProcessIdentity, expected: str) -> bool:
        command = identity.command
        if (PureWindowsPath(identity.image).name.casefold() != expected.casefold()
                or identity.image.casefold() not in command.casefold()):
            return False
        if not (
            ("--utility-sub-type=node.mojom.NodeService" in command
             and _UTILITY_HOST.search(command) is not None)
            or _LEGACY_HOST.search(command) is not None
        ):
            return False
        data_dirs = [quoted or bare for quoted, bare in _USER_DATA_DIR.findall(command)]
        if len(data_dirs) > 1:
            return False
        if data_dirs and _windows_identity(data_dirs[0]) != _windows_identity(
                str(self.user_data_root.parent)):
            self._refuse("wrong_profile")
            return False
        return True

    def _bounded_dirs(self, root: Path, pattern, limit: int,
                      budget: WorkBudget) -> Optional[List[Path]]:
        if not budget.live():
            self._defer(budget)
            return None
        found = []
        try:
            with os.scandir(root) as iterator:
                for count, entry in enumerate(iterator):
                    if count >= limit:
                        return None
                    budget.entries -= 1
                    if budget.entries < 0:
                        budget.exhaust()
                    if (count & 63) == 63 or budget.entries < 0:
                        if not budget.live():
                            self._defer(budget)
                            return None
                    if pattern.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                        found.append(Path(entry.path))
        except OSError:
            return None
        return found

    def read_from(self, path: Path, offset: int, limit: int,
                  budget: WorkBudget) -> Optional[bytes]:
        """Read ``path[offset:]`` within ``limit`` and the shared budget.

        A file already larger than ``limit`` is refused unread; every byte
        actually attempted is charged, including data that is then refused.
        """
        if not budget.live():
            self._defer(budget)
            return None
        try:
            with path.open("rb") as handle:
                status = os.fstat(handle.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_size < offset:
                    return None
                if status.st_size - offset > limit:
                    self._refuse("metadata_oversized")
                    return None
                if offset:
                    handle.seek(offset)
                data = handle.read(min(limit, max(budget.bytes, 0)) + 1)
        except OSError:
            return None
        if not budget.charge(len(data)):
            self._defer(budget)
            return None
        if len(data) > limit or data.count(b"\n") > LOG_LINE_LIMIT:
            self._refuse("metadata_oversized")
            return None
        return data

    def read_limited(self, path: Path, limit: int, budget: WorkBudget) -> Optional[bytes]:
        return self.read_from(path, 0, limit, budget)

    def _stamp(self, line: bytes) -> Optional[int]:
        match = _TIMESTAMP.match(line)
        return self.log_time_ms(match.group(1)) if match is not None else None

    def _current_generation(self, data: bytes, base: int = 0):
        """Mirror the status path's exact-generation rules for the last host.

        Returns ``(pid, start_ms, storage_keys, start_offset, start_line)``.
        """
        current = None
        pending = 0
        for offset, line in _lines(data, base):
            start = _HOST_START.search(line)
            exited = _HOST_EXIT.search(line)
            if start is not None:
                current = (int(start.group(1)), self._stamp(line), set(), offset,
                           line.rstrip(b"\r\n"))
                pending = 0
            elif _HOST_TERMINATING.search(line) is not None:
                if current is not None:
                    pending += 1
            elif exited is not None:
                if current is not None and int(exited.group(1)) == current[0]:
                    current = None
                    pending = 0
                elif pending:
                    pending -= 1
            if current is not None:
                current[2].update(
                    match.group(1).decode("ascii", errors="ignore")
                    for match in _STORAGE.finditer(line)
                )
        return None if current is None or pending else current

    def codex_log_generation(self, data: bytes, base: int = 0) -> CodexLogGeneration:
        """Same routing-marker rules as the shared resolver, for all thread IDs."""
        states: Dict[str, Tuple[Optional[str], Optional[bool]]] = {}
        markers = set()
        spawn: Optional[Tuple[int, bytes]] = None
        for offset, line in _lines(data, base):
            if _SPAWN in line:
                states, markers = {}, set()
                spawn = (offset, line.rstrip(b"\r\n"))
                continue
            session = _conversation(line)
            if session is None:
                continue
            role, active = states.get(session, (None, None))
            if b"thread_stream_role_changed" in line:
                match = _ROLE.search(line)
                role = match.group(1).decode("ascii", errors="ignore") if match else None
            elif b"maybe_resume_success" in line:
                match = _ASSIGNED_ROLE.search(line)
                role = match.group(1).decode("ascii", errors="ignore") if match else None
            elif b"maybe_resume_failed" in line or b"inactive_thread_unsubscribed" in line:
                role = active = None
            elif b"thread_stream_view_activity_changed" in line:
                match = _ACTIVE.search(line)
                active = match.group(1) == b"true" if match is not None else None
            else:
                continue
            markers.add(session)
            states[session] = (role, active)
        return CodexLogGeneration(
            self._stamp(spawn[1]) if spawn is not None else None,
            spawn[0] if spawn is not None else None,
            spawn[1] if spawn is not None else b"",
            states, frozenset(markers),
        )

    def codex_generation(self, codex_log: Path, budget: WorkBudget) -> Optional[CodexLogGeneration]:
        """One bounded full read per Codex.log per snapshot, shared by all users."""
        if codex_log not in self._codex_cache:
            data = self.read_limited(codex_log, CODEX_LOG_BYTES, budget)
            self._codex_cache[codex_log] = (
                None if data is None else self.codex_log_generation(data))
        return self._codex_cache[codex_log]

    def exthost_generation(self, log: Path, budget: WorkBudget):
        if log not in self._exthost_cache:
            data = self.read_limited(log, EXTHOST_LOG_BYTES, budget)
            if data is not None:
                self._exthost_readable.add(log)
            self._exthost_cache[log] = None if data is None else self._current_generation(data)
        return self._exthost_cache[log]

    def _main_for_host(self, table, duplicates, host):
        entry = table.get(host.pid)
        if entry is None or entry.parent_pid in duplicates:
            return None
        main = self._identity(self.reader, entry.parent_pid)
        if (main is None or main.birth > host.birth
                or _windows_identity(main.image) != _windows_identity(host.image)
                or main.image.casefold() not in main.command.casefold()
                or re.search(r"(?:^|\s)--type(?:=|\s)", main.command)):
            self._refuse("main_generation_unverified")
            return None
        dirs = [quoted or bare for quoted, bare in _USER_DATA_DIR.findall(main.command)]
        if len(dirs) > 1 or (dirs and _windows_identity(dirs[0]) !=
                             _windows_identity(str(self.user_data_root.parent))):
            self._refuse("wrong_profile")
            return None
        return main

    def main_current(self, proof):
        return proof.main is not None and self._identity(self.reader, proof.main.pid) == proof.main

    def sessions_for_main(self, main, budget):
        """Probe a fixed startup interval for the verified GUI generation.

        VS Code names log homes with local initialization time. CLI diagnostics
        create later homes too, so newest-directory ranking is not a locator.
        These names only locate candidates; exact current exthost/app generation
        and held writer evidence still decide every admission. Startup beyond
        this bounded interval is explicitly unresolved.
        """
        if main.pid in self._main_sessions:
            return self._main_sessions[main.pid]
        root = self.user_data_root.parent / "logs"
        born = filetime_ms(main.birth) // 1000
        result = []
        for second in range(born, born + LOG_STARTUP_SECONDS + 1):
            if not budget.live():
                self._defer(budget)
                self._main_sessions[main.pid] = None
                return None
            budget.entries -= 1
            if budget.entries < 0:
                budget.exhaust()
                self._main_sessions[main.pid] = None
                return None
            candidate = root / time.strftime("%Y%m%dT%H%M%S", time.localtime(second))
            if candidate.is_dir() and not candidate.is_symlink():
                result.append(candidate)
        self._main_sessions[main.pid] = result
        return result

    def sessions(self, budget: WorkBudget):
        result = set()
        for main in self._host_mains.values():
            sessions = self.sessions_for_main(main, budget)
            if sessions is None:
                return None
            result.update(sessions)
        return sorted(result)

    def cli_window(self, number: str, host_pid: int,
                   budget: WorkBudget) -> Optional[Tuple[str, Optional[Path]]]:
        """Bounded equivalent of the shared newest-session exthost lookup."""
        main = self._host_mains.get(host_pid)
        for session in (self.sessions_for_main(main, budget) or ()) if main is not None else ():
            log = session / ("window" + number) / "exthost" / "exthost.log"
            if not log.is_file():
                continue
            current = self.exthost_generation(log, budget)
            if current is None or current[0] != host_pid or len(current[2]) != 1:
                return None
            codex_log = log.parent / "openai.chatgpt" / "Codex.log"
            return next(iter(current[2])), codex_log if codex_log.is_file() else None
        return None

    def _map_logs(self, verified, budget: WorkBudget):
        # Uniqueness is complete within each verified GUI generation, not merely
        # one match seen before an I/O/budget failure. An incomplete domain is a
        # visible exclusion. Rotate GUI domains so it cannot starve another GUI.
        domains = {}
        for pid in verified:
            main = self._host_mains[pid]
            domains.setdefault(main.pid, []).append(pid)
        result = {}
        for main_pid in self._rotated("main_domains", sorted(domains)):
            pids = domains[main_pid]
            main = self._host_mains[pids[0]]
            sessions = self.sessions_for_main(main, budget)
            complete = sessions is not None
            matches = {}
            earliest_ns = min((verified[pid][0].birth - FILETIME_UNIX_EPOCH) * 100
                              for pid in pids)
            for session in sessions or ():
                windows = self._bounded_dirs(session, _WINDOW_NAME, WINDOW_SCAN_LIMIT, budget)
                if windows is None:
                    complete = False
                    break
                for window in sorted(windows, key=lambda item: item.name):
                    log = window / "exthost" / "exthost.log"
                    try:
                        status = log.stat()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        complete = False
                        break
                    if not stat.S_ISREG(status.st_mode) or status.st_mtime_ns < earliest_ns:
                        continue
                    current = self.exthost_generation(log, budget)
                    if current is None and log not in self._exthost_readable:
                        # An unreadable generation cannot be ruled out
                        # as a conflicting current holder in this GUI domain.
                        complete = False
                        break
                    if current is not None and current[0] in pids:
                        number = _WINDOW_NAME.fullmatch(window.name).group(1)
                        matches.setdefault(current[0], []).append((number, current, log))
                if not complete:
                    break
            if not complete:
                self._refuse("log_domain_incomplete")
                continue
            for pid, found in matches.items():
                if len(found) != 1:
                    self._refuse("ambiguous_host_log")
                    continue
                number, (_pid, start_ms, keys, offset, line), log = found[0]
                if start_ms is None or len(keys) != 1:
                    self._refuse("ambiguous_workspace_storage")
                    continue
                key = next(iter(keys))
                if not (self.user_data_root / "workspaceStorage" / key).is_dir():
                    self._refuse("workspace_storage_missing")
                    continue
                result[pid] = (number, key, log, start_ms, offset, line)
        self._advance("main_domains", 1)
        return result

    def last_spawn_ms(self, codex_log: Path, budget: Optional[WorkBudget] = None) -> Optional[int]:
        if not codex_log.is_file():
            return None
        generation = self.codex_generation(
            codex_log, WorkBudget(self.clock) if budget is None else budget)
        return generation.spawn_ms if generation is not None else None


def _windows_identity(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value.strip().rstrip("\\/")))


class WindowsVSCodeLiveWindowIndex(VSCodeLiveWindowIndex):
    """Windows-only live index: exact CLI windows plus verified native fallback.

    The snapshot merges CLI and native windows directly with the shared
    fail-closed conflict rules, using only bounded current-generation metadata;
    it never calls the inherited whole-history log path. Native proofs stay
    private to this adapter; discovery reads them only through the guarded
    ``windows_*`` hooks.
    """

    def __init__(
        self,
        user_data_root: Path,
        *,
        status_probe: Callable[[], VSCodeStatusProbe],
        reader=None,
        log_time_ms: Callable[[bytes], Optional[int]] = local_log_time_ms,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            user_data_root, status_runner=self._status_output, native_runner=lambda: (),
        )
        self.status_probe = status_probe
        self.collector = WindowsNativeCollector(
            self.user_data_root, reader=reader, log_time_ms=log_time_ms, clock=clock,
        )
        self._status_text: Optional[str] = None
        self._proofs: Dict[int, WindowsNativeProof] = {}
        self._fallback: Dict[str, WindowsNativeProof] = {}
        self._issues: set = set()
        self._selection_paths: Dict[Path, Tuple[Path, ...]] = {}
        self.last_status: Optional[VSCodeStatusProbe] = None
        self.last_budget: Optional[WorkBudget] = None
        self.last_collection_budget: Optional[WorkBudget] = None
        self._completion_cursor = 0

    def _status_output(self) -> Optional[str]:
        return self._status_text

    def _work_budget(self) -> WorkBudget:
        if self.last_budget is None:
            self.last_budget = WorkBudget(self.collector.clock)
        return self.last_budget

    def snapshot(self) -> Optional[Dict[str, LiveVSCodeWindow]]:
        self._proofs, self._fallback, self._issues = {}, {}, set()
        self._selection_paths = {}
        try:
            probe = self.status_probe()
        except Exception:
            probe = VSCodeStatusProbe("error", "vscode_status_failed")
        self.last_status = probe
        available = (probe.status == "available" and isinstance(probe.output, str)
                     and bool(probe.output.strip()))
        self._status_text = probe.output if available else None
        if not available:
            reason = probe.reason if isinstance(probe.reason, str) else ""
            self._issues.add(reason if _SAFE_REASON.fullmatch(reason) else "vscode_status_failed")
        cli = self._parse_status(self._status_text) if self._status_text else {}
        # One budget covers this snapshot's collection, resolution and admission.
        budget = self.last_collection_budget = WorkBudget(self.collector.clock)
        proofs = self.collector.collect(budget) or ()
        self._proofs = {proof.host.pid: proof for proof in proofs}
        windows = self._merge(cli, proofs, budget)
        # Reserve a full independent completion allowance. Collection exhaustion
        # cannot revoke proofs from a completely checked GUI domain.
        self.last_budget = WorkBudget(self.collector.clock)
        for reason in ("unavailable", "budget_exceeded", "deadline_exceeded", "log_domain_incomplete"):
            if reason in self.collector.refusals:
                self._issues.add("windows_native_" + reason)
        if windows is None:
            return None
        if self._fallback:
            self._issues.add("vscode_windows_native_fallback")
        return windows

    def _merge(self, cli, proofs, budget: WorkBudget) -> Optional[Dict[str, LiveVSCodeWindow]]:
        """The shared CLI/native merge rules over bounded per-window evidence."""
        processes = dict(cli)
        native: Dict[str, WindowsNativeProof] = {}
        conflicts = set()
        for proof in proofs:
            number, pair = proof.window_number, (proof.host.pid, proof.app.pid)
            if number in native or processes.get(number) not in (None, pair, (proof.host.pid, None)):
                conflicts.add(number)
            processes[number] = pair
            native[number] = proof
        if not processes:
            return None
        if conflicts:
            self._issues.add("windows_native_evidence_conflict")
        by_storage: Dict[str, list] = {}
        for number, (host_pid, codex_pid) in sorted(processes.items()):
            if number in conflicts:
                continue
            proof = native.get(number)
            match = ((proof.storage_key, proof.codex_log) if proof is not None
                     else self.collector.cli_window(number, host_pid, budget))
            if match is None:
                continue
            storage_key, codex_log = match
            by_storage.setdefault(storage_key, []).append(
                (LiveVSCodeWindow(number, host_pid, codex_pid, codex_log), proof))
        current = {}
        for storage_key, found in by_storage.items():
            if len(found) != 1:
                continue
            window, proof = found[0]
            current[storage_key] = window
            if proof is not None and cli.get(window.window_number) != (
                    window.extension_host_pid, window.codex_app_server_pid):
                self._fallback[storage_key] = proof
        return current

    def windows_discovery_issues(self) -> Tuple[str, ...]:
        return tuple(sorted(self._issues))

    def windows_order_entries(self, entries):
        """Fair completion under finite work: no permanent active-session cap."""
        entries = list(entries)
        if not entries:
            return entries
        start = self._completion_cursor % len(entries)
        self._completion_cursor += 1
        return entries[start:] + entries[:start]

    def windows_storage_keys(
        self, live_windows, sleep: Callable[[float], None] = lambda _seconds: None,
    ) -> Dict[str, Tuple[str, ...]]:
        """Map workspace URIs from the exact current live storage keys only."""
        budget = self._work_budget()
        root = self.user_data_root / "workspaceStorage"
        matches: Dict[str, List[str]] = {}
        for storage_key in sorted(live_windows):
            document = None
            for attempt in range(3):
                data = self.collector.read_from(
                    root / storage_key / "workspace.json", 0, WORKSPACE_JSON_BYTES, budget)
                if data is not None:
                    try:
                        document = json.loads(data.decode("utf-8"))
                        break
                    except (UnicodeError, ValueError):
                        pass
                if not budget.live():
                    self._issues.add(_DEFERRED)
                    break
                if attempt < 2:
                    sleep(0.02)
            if not isinstance(document, dict):
                continue
            for field in ("folder", "workspace"):
                value = document.get(field)
                if isinstance(value, str) and value:
                    matches.setdefault(value, []).append(storage_key)
        return {uri: tuple(keys) for uri, keys in matches.items()}

    def windows_resolve(
        self,
        paths,
        *,
        codex_log: Optional[Path],
        window_state_database: Optional[Path],
        live_windows: Sequence[LiveVSCodeWindow],
        session_resolver,
    ) -> Optional[SessionResolution]:
        """Bounded resolution; ``None`` leaves an unknown resolver type unchanged."""
        if not isinstance(session_resolver, CodexSessionResolver):
            return None
        paths = tuple(paths)
        budget = self._work_budget()
        reserve = EXTHOST_LOG_BYTES + CODEX_LOG_BYTES + 4 * CACHE_VALUE_BYTES
        if codex_log in {proof.codex_log for proof in self._fallback.values()} and (
                not budget.live() or budget.bytes < reserve):
            self._issues.add(_DEFERRED)
            return SessionResolution("unresolved", None, None, _DEFERRED)
        if window_state_database is not None:
            self._selection_paths[window_state_database] = paths
        return self._select(
            paths, codex_log, window_state_database, tuple(live_windows),
            session_resolver, self._work_budget(),
        )

    def _cache_sessions(self, database: Optional[Path], budget: WorkBudget) -> Optional[set]:
        """Bound size, parse and materialize from one metered read transaction."""
        if database is None or not budget.live() or not database.is_file():
            return None
        try:
            uri = database.resolve(strict=True).as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as connection:
                budget.meter(connection)
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                rows = connection.execute(CACHE_SIZE_LOOKUP, (_CODEX_SESSION_CACHE_KEY,)).fetchall()
                if (len(rows) != 1 or not isinstance(rows[0][0], int)
                        or rows[0][0] > CACHE_VALUE_BYTES or not budget.charge(rows[0][0])):
                    return None
                budget.cache_charged += rows[0][0]
                shape = connection.execute(
                    "SELECT json_valid(value), json_type(value) FROM ItemTable WHERE key = ?",
                    (_CODEX_SESSION_CACHE_KEY,)).fetchall()
                if shape != [(1, "array")]:
                    return None
                entries = connection.execute(
                    "SELECT entry.type, json_extract(entry.value, '$.providerType'), "
                    "json_type(entry.value, '$.providerType'), "
                    "json_extract(entry.value, '$.resource'), json_type(entry.value, '$.resource') "
                    "FROM ItemTable JOIN json_each(ItemTable.value) AS entry WHERE ItemTable.key = ?",
                    (_CODEX_SESSION_CACHE_KEY,))
                candidates = set()
                for count, (kind, provider, provider_type, resource, resource_type) in enumerate(entries):
                    if count >= SESSION_LOOKUP_LIMIT or not budget.live():
                        return None
                    if kind != "object" or provider_type != "text":
                        return None
                    if provider != "openai-codex":
                        continue
                    if (resource_type != "text" or not isinstance(resource, str)
                            or not resource.startswith(_CODEX_SESSION_RESOURCE_PREFIX)):
                        return None
                    suffix = resource[len(_CODEX_SESSION_RESOURCE_PREFIX):]
                    canonical = _canonical_session_id(suffix)
                    if canonical is None or len(suffix) != 36:
                        return None
                    candidates.add(canonical)
                return candidates
        except (OSError, RuntimeError, sqlite3.Error):
            return None

    def _loaded(self, sessions, paths, resolver, budget: WorkBudget):
        """Exact-ID loaded-thread lookups; never an all-thread scan."""
        database = resolver.codex_home / "state_5.sqlite"
        if not database.is_file():
            return set(), "unavailable"
        if not budget.live():
            return set(), "deferred"
        try:
            identities = {_path_identity(Path(path)) for path in paths}
        except (OSError, RuntimeError, ValueError):
            return set(), "unavailable"
        loaded = set()
        try:
            uri = database.resolve(strict=True).as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as connection:
                budget.meter(connection)
                connection.execute("PRAGMA query_only = ON")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
                if not _THREAD_COLUMNS.issubset(columns):
                    return set(), "unavailable"
                for session in sorted(sessions):
                    for raw_id, raw_cwd in connection.execute(THREAD_LOOKUP, (session,)).fetchall():
                        if _canonical_session_id(raw_id) != session or not isinstance(raw_cwd, str):
                            continue
                        try:
                            cwd_identity = _path_identity(Path(raw_cwd))
                        except (OSError, RuntimeError, ValueError):
                            continue
                        if cwd_identity not in identities:
                            continue
                        lock = resolver.codex_home / "thread-writer-locks" / f"{session}.lock"
                        if resolver.lock_probe(lock):
                            loaded.add(session)
                        break
        except (OSError, RuntimeError, sqlite3.Error):
            return set(), "deferred" if budget.reason else "unavailable"
        return loaded, "available"

    def _recorded_workspace(self, session: str, resolver, budget: WorkBudget) -> Tuple[Path, ...]:
        """The selected thread's own recorded cwd, by exact indexed ID."""
        database = resolver.codex_home / "state_5.sqlite"
        if not budget.live() or not database.is_file():
            return ()
        try:
            uri = database.resolve(strict=True).as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as connection:
                budget.meter(connection)
                connection.execute("PRAGMA query_only = ON")
                rows = connection.execute(THREAD_LOOKUP, (session,)).fetchall()
        except (OSError, RuntimeError, sqlite3.Error):
            return ()
        cwds = {raw_cwd for raw_id, raw_cwd in rows
                if _canonical_session_id(raw_id) == session and isinstance(raw_cwd, str)}
        return (Path(next(iter(cwds))),) if len(cwds) == 1 else ()

    def _select(
        self, paths, codex_log: Optional[Path], database: Optional[Path],
        live_windows: Sequence[LiveVSCodeWindow], resolver, budget: WorkBudget,
        evidence: Optional[CodexLogGeneration] = None,
    ) -> SessionResolution:
        """The shared resolver's decision rules over bounded current evidence.

        Candidate IDs come only from this generation's routing markers and the
        window's own cache; each is checked by exact indexed ID with the same
        cwd/source/archived/subagent predicate and held writer lock.
        """

        def unresolved(reason: str) -> SessionResolution:
            return SessionResolution("unresolved", None, None, reason)

        if codex_log is None:
            return unresolved("vscode_codex_log_unavailable")
        window_sessions = self._cache_sessions(database, budget)
        if evidence is None:
            evidence = self.collector.codex_generation(codex_log, budget)
        if evidence is None:
            return unresolved(_DEFERRED if budget.reason else "vscode_codex_log_unavailable")
        relevant = {session for session, state in evidence.states.items()
                    if state[0] == "owner" or state == ("follower", True)}
        if len(relevant) > SESSION_LOOKUP_LIMIT:
            return unresolved("ambiguous_loaded_threads")
        # A cache too large to check exactly cannot support marker-less selection.
        complete = window_sessions is not None and len(window_sessions) <= SESSION_LOOKUP_LIMIT
        loaded, database_status = self._loaded(
            relevant | (window_sessions if complete else set()), paths, resolver, budget)
        if database_status == "deferred":
            return unresolved(_DEFERRED)
        states = {session: evidence.states.get(session, (None, None)) for session in loaded}
        candidates = {session for session in loaded if states[session][0] == "owner"}
        followers = {session for session, state in states.items()
                     if state == ("follower", True)}
        for session in sorted(followers):
            lock = resolver.codex_home / "thread-writer-locks" / f"{session}.lock"
            writer_pid = resolver.writer_process_probe(lock)
            owners = set()
            for window in live_windows:
                if (writer_pid is None or window.codex_app_server_pid != writer_pid
                        or window.codex_log is None or window.codex_log == codex_log):
                    continue
                other = self.collector.codex_generation(window.codex_log, budget)
                if other is not None and other.states.get(session, (None, None))[0] == "owner":
                    owners.add(window.codex_log)
            if len(owners) != 1:
                return unresolved(
                    "ambiguous_vscode_thread_owner" if owners else "vscode_thread_owner_unverified")
            if not resolver.lock_probe(lock):
                return unresolved("vscode_thread_owner_unverified")
        candidates |= followers

        # The shared resolver's unique exact OS-writer rule, restricted to a
        # cached thread; admission of a native-only window still requires an
        # explicit owner marker, so this never selects a fallback owner.
        if not candidates and complete and len(loaded) == 1:
            session = next(iter(loaded))
            current = [window for window in live_windows if window.codex_log == codex_log]
            if (session not in evidence.markers and session in window_sessions
                    and len(current) == 1 and current[0].codex_app_server_pid is not None):
                lock = resolver.codex_home / "thread-writer-locks" / f"{session}.lock"
                if (resolver.writer_process_probe(lock) == current[0].codex_app_server_pid
                        and resolver.lock_probe(lock)):
                    return SessionResolution(
                        "resolved", session, "codex_state_vscode_native_writer", None)

        def resolved(session: str, source: str) -> SessionResolution:
            if session in followers:
                return SessionResolution(
                    "resolved", session, _FOLLOWER_SOURCE,
                    "vscode_thread_owned_by_another_window")
            return SessionResolution("resolved", session, source, None)

        if len(candidates) == 1:
            session = next(iter(candidates))
            return resolved(session, (
                "codex_state_vscode_window_cache_owner"
                if window_sessions is not None and session in window_sessions
                else "codex_state_vscode_live_owner"))
        if len(candidates) > 1:
            active = [session for session in candidates if states[session][1] is True]
            if len(active) == 1 and all(
                    states[session] == ("owner", False)
                    for session in candidates if session != active[0]):
                return resolved(active[0], "codex_state_vscode_active_owner")
            return unresolved("ambiguous_loaded_threads")
        if window_sessions is None:
            return unresolved("vscode_session_cache_unavailable")
        if not window_sessions:
            return unresolved("no_window_codex_thread")
        if database_status == "unavailable":
            return unresolved("codex_state_unavailable")
        return unresolved("no_loaded_vscode_thread")

    def windows_fallback_admission(
        self,
        storage_key: str,
        resolution: SessionResolution,
        *,
        codex_home: Path,
        window_state_database: Path,
        live_windows: Sequence[LiveVSCodeWindow],
        session_resolver,
    ) -> SessionResolution:
        """Independently re-prove a thread selected only via native evidence."""
        proof = self._fallback.get(storage_key)
        session = resolution.session_id
        if proof is None or session is None:
            return resolution

        def refuse(reason: str) -> SessionResolution:
            return SessionResolution("unresolved", None, None, "windows_native_" + reason)

        if _canonical_session_id(session) != session:
            return refuse("thread_invalid")
        if resolution.source == _FOLLOWER_SOURCE or resolution.reason is not None:
            return refuse("follower_rejected")
        expected = LiveVSCodeWindow(
            proof.window_number, proof.host.pid, proof.app.pid, proof.codex_log,
        )
        sharing = [
            window for window in live_windows
            if window.codex_log == proof.codex_log
            or window.extension_host_pid == proof.host.pid
            or window.codex_app_server_pid == proof.app.pid
        ]
        if sharing != [expected]:
            return refuse("window_ambiguous")
        if not isinstance(session_resolver, CodexSessionResolver):
            return refuse("owner_unverified")
        budget = self._work_budget()
        evidence = self.collector.codex_generation(proof.codex_log, budget)
        if evidence is None:
            return refuse("budget_deferred" if budget.reason else "owner_unverified")
        if evidence.spawn_offset != proof.codex_offset or evidence.spawn_ms != proof.spawn_ms:
            return refuse("stale_app_server_generation")
        role, active = evidence.states.get(session, (None, None))
        if role == "follower":
            return refuse("follower_rejected")
        if active is False:
            return refuse("inactive_rejected")
        if role != "owner":
            return refuse("owner_unverified")
        # Recent VS Code exposes a valid empty cache for live Codex views.
        # It is absence, not contradiction; explicit role plus exact held writer
        # remain mandatory. Nonempty conflicting or unavailable caches refuse.
        cached = self._cache_sessions(window_state_database, budget)
        if cached is None or (cached and session not in cached):
            return refuse("cache_mismatch")
        lock = Path(codex_home) / "thread-writer-locks" / f"{session}.lock"
        if not self._writer_matches(lock, proof):
            return refuse("writer_unverified")
        # A replacement window/app generation or a competing owner can appear
        # while the old host/app still drain with their lock: repeat the current
        # generations and the complete selection right before the final checks.
        changed = self._current_selection(
            proof, session, window_state_database, live_windows, session_resolver, budget)
        if changed is not None:
            return refuse(changed)
        if not self._identity_current(proof):
            return refuse("identity_changed")
        if not self._writer_matches(lock, proof):
            return refuse("writer_changed")
        return SessionResolution("resolved", session, FALLBACK_SOURCE, None)

    def _current_selection(
        self, proof: WindowsNativeProof, session: str, database: Path,
        live_windows: Sequence[LiveVSCodeWindow], resolver, budget: WorkBudget,
    ) -> Optional[str]:
        """Fresh generation fingerprints and complete owner selection, or a reason."""
        collector = self.collector
        if not collector.app_generation_current(proof):
            return "app_generation_changed"
        # Re-read only each generation's tail, from its fingerprinted start line.
        tail = collector.read_from(
            proof.exthost_log, proof.exthost_offset, EXTHOST_LOG_BYTES, budget)
        if tail is None:
            return "budget_deferred" if budget.reason else "stale_host_generation"
        current = collector._current_generation(tail, proof.exthost_offset)
        if (not proof.exthost_line or not tail.startswith(proof.exthost_line)
                or current is None
                or (current[0], current[1], current[2], current[3]) != (
                    proof.host.pid, proof.log_start_ms, {proof.storage_key},
                    proof.exthost_offset)):
            return "stale_host_generation"
        tail = collector.read_from(proof.codex_log, proof.codex_offset, CODEX_LOG_BYTES, budget)
        if tail is None:
            return "budget_deferred" if budget.reason else "stale_app_server_generation"
        fresh = collector.codex_log_generation(tail, proof.codex_offset)
        if (not proof.codex_line or not tail.startswith(proof.codex_line)
                or (fresh.spawn_offset, fresh.spawn_ms) != (proof.codex_offset, proof.spawn_ms)):
            return "stale_app_server_generation"
        role, active = fresh.states.get(session, (None, None))
        if role != "owner" or active is False:
            return "selection_changed"
        cached = self._cache_sessions(database, budget)
        if cached is None or (cached and session not in cached):
            return "cache_mismatch"
        paths = self._selection_paths.get(database) or self._recorded_workspace(
            session, resolver, budget)
        if not paths:
            return "budget_deferred" if budget.reason else "selection_changed"
        selection = self._select(
            paths, proof.codex_log, database, live_windows, resolver, budget, fresh)
        if (selection.session_id != session or selection.reason is not None
                or selection.source == _FOLLOWER_SOURCE):
            return "budget_deferred" if budget.reason else "selection_changed"
        return None

    def _writer_matches(self, lock: Path, proof: WindowsNativeProof) -> bool:
        reader = self.collector.reader
        try:
            return bool(
                reader.lock_held(lock)
                and reader.writer(lock) == (proof.app.pid, proof.app.birth)
                and reader.writer_same_user(lock, proof.app.pid, proof.app.birth)
                and reader.lock_held(lock)
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _identity_current(self, proof: WindowsNativeProof) -> bool:
        reader = self.collector.reader
        try:
            return (reader.identity(proof.app.pid) == proof.app
                    and reader.identity(proof.host.pid) == proof.host
                    and self.collector.main_current(proof)
                    and self.collector.app_generation_current(proof))
        except (OSError, ValueError, TypeError, AttributeError):
            return False
