from __future__ import annotations

from dataclasses import dataclass, field, replace
import ctypes
import errno
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit
import uuid

from .models import sha256_text, utc_now
from .platform_adapters import (
    HostPlatformAdapter,
    detect_platform_adapter,
    is_codex_app_server_description,
    run_vscode_status,
)
from .storage import InstructionStore
from .workspace_registry import TrackedWorkspace, WorkspaceRegistry
from .writer_process import writer_lock_process
from .vscode_processes import NativeVSCodeHost, native_vscode_hosts


DISCOVERY_SCHEMA_VERSION = 1
PROCESS_LOCAL = "process_local"
REMOTE_SSH = "remote_ssh"

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

GitRootResolver = Callable[[Path], Optional[Path]]
WriterLockProbe = Callable[[Path], bool]
SessionOwnerProbe = Callable[[Path, str], bool]
CodeStatusRunner = Callable[[], Optional[str]]
Sleep = Callable[[float], None]

_CODEX_SESSION_CACHE_KEY = "agentSessions.model.cache"
_CODEX_SESSION_RESOURCE_PREFIX = "openai-codex://route/local/"


def _canonical_session_id(value: Any) -> Optional[str]:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _strip_extended_windows_prefix(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _canonical_local_path(value: Path) -> Path:
    raw = _strip_extended_windows_prefix(os.fspath(value))
    return Path(raw).expanduser().resolve(strict=False)


def _path_identity(value: Path) -> str:
    return os.path.normcase(str(_canonical_local_path(value)))


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "file":
        raise ValueError("workspace URI is not a local file URI")
    authority = unquote(parsed.netloc)
    if authority and authority.lower() != "localhost":
        raise ValueError("UNC file URIs are not process-local workspaces")
    decoded = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:/", decoded):
        decoded = decoded[1:]
    if not decoded:
        raise ValueError("workspace file URI has no path")
    return _canonical_local_path(Path(decoded))


def _remote_uri_path(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "vscode-remote":
        raise ValueError("workspace URI is not a VS Code remote URI")
    path = unquote(parsed.path)
    if not path.startswith("/"):
        raise ValueError("remote workspace URI has no absolute path")
    return path


def _remote_authority(uri: str, recorded: Any) -> Optional[str]:
    if isinstance(recorded, str) and recorded:
        return recorded
    parsed = urlsplit(uri)
    authority = unquote(parsed.netloc)
    return authority or None


def _default_git_root(path: Path) -> Optional[Path]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    root = _canonical_local_path(Path(_strip_extended_windows_prefix(lines[0])))
    if not root.is_dir():
        return None
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return root


def _windows_writer_lock_is_held(path: Path) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int

    generic_read_write = 0x80000000 | 0x40000000
    open_existing = 3
    normal_attributes = 0x80
    invalid_handle = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path), generic_read_write, 0, None, open_existing, normal_attributes, None,
    )
    if handle != invalid_handle:
        close_handle(handle)
        return False
    return ctypes.get_last_error() in (32, 33)


def _posix_writer_lock_is_held(path: Path) -> bool:
    import fcntl

    try:
        descriptor = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if getattr(exc, "errno", None) in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EWOULDBLOCK,
            }:
                return True
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def writer_lock_is_held(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name == "nt":
        return _windows_writer_lock_is_held(path)
    return _posix_writer_lock_is_held(path)


def _default_code_status() -> Optional[str]:
    return run_vscode_status().output


def codex_log_owns_session(path: Path, session_id: str) -> bool:
    return codex_log_session_states(path, (session_id,)).get(session_id, (None, None))[0] == "owner"


def codex_log_session_states(
    path: Path, session_ids: Iterable[str],
) -> Dict[str, Tuple[Optional[str], Optional[bool]]]:
    """Read exact routing markers from one window and App Server generation."""
    return _codex_log_session_evidence(path, session_ids)[0]


_ROLE_FIELD = re.compile(rb"(?:^|\s)role=([^\s]+)(?=\s|$)")
_ASSIGNED_ROLE_FIELD = re.compile(rb"(?:^|\s)assignedStreamRole=([^\s]+)(?=\s|$)")
_ACTIVE_FIELD = re.compile(rb"(?:^|\s)active=(true|false)(?=\s|$)")
_ACTIVE_VALUE_FIELD = re.compile(rb"(?:^|\s)active=([^\s]*)(?=\s|$)")
_ATTACHMENT_KEY = b"assignedHistoryAttachment"
_ATTACHMENT_FIELD = re.compile(rb"(?:^|\s)assignedHistoryAttachment=([^\s]*)(?=\s|$)")
_VIEW_HISTORY_KEY = b"historyAttached"
_VIEW_HISTORY_FIELD = re.compile(rb"(?:^|\s)historyAttached=([^\s]*)(?=\s|$)")
_CONVERSATION_FIELD = re.compile(rb"(?:^|\s)conversationId=([^\s]*)(?=\s|$)")
_THREAD_FIELD = re.compile(rb"(?:^|\s)threadId=([^\s]*)(?=\s|$)")

HISTORY_ATTACHED_SOURCE = "codex_state_vscode_history_attached_writer"

# (legacy stream role, view active, new-format history attachment)
RoutingState = Tuple[Optional[str], Optional[bool], bool]


def _explicit_flag(line: bytes, key: bytes, field) -> Tuple[bool, Optional[bool]]:
    """``(present, value)``; a duplicated or non-boolean value is ``None``."""
    if key not in line:
        return False, None
    values = field.findall(line)
    if len(values) != 1 or line.count(key) != 1 or values[0] not in (b"true", b"false"):
        return True, None
    return True, values[0] == b"true"


def _single_thread_identity(line: bytes) -> bool:
    conversations = _CONVERSATION_FIELD.findall(line)
    threads = _THREAD_FIELD.findall(line)
    return (len(conversations) == 1 and len(threads) <= 1
            and all(thread == conversations[0] for thread in threads))


def _routing_transition(line: bytes, state: RoutingState) -> Optional[RoutingState]:
    """Apply one exact-conversation routing line; ``None`` if it is not one.

    Legacy role/activity semantics are unchanged. Current extensions instead
    report ``assignedHistoryAttachment`` on a successful resume. That boolean
    is only a necessary local-history gate, never a legacy owner role: it is
    kept separately and only an explicit, single, consistent ``true`` sets it.
    A new-format activity line whose ``historyAttached`` is not ``true``
    revokes it; ``true`` there merely keeps an earlier attachment. Explicit
    follower/unknown roles are never overridden by an attachment.
    """
    role, active, attached = state
    if b"thread_stream_role_changed" in line:
        match = _ROLE_FIELD.search(line)
        role = match.group(1).decode("ascii", errors="ignore") if match else None
        return role, active, False
    if b"maybe_resume_success" in line:
        match = _ASSIGNED_ROLE_FIELD.search(line)
        assigned = match.group(1).decode("ascii", errors="ignore") if match else None
        present, value = _explicit_flag(line, _ATTACHMENT_KEY, _ATTACHMENT_FIELD)
        if not present:
            return assigned, active, False
        if b"assignedStreamRole" in line and (
                line.count(b"assignedStreamRole") != 1
                or len(_ASSIGNED_ROLE_FIELD.findall(line)) != 1):
            return None, active, False
        prior = role if assigned is None else assigned
        if value is True and prior in (None, "owner") and _single_thread_identity(line):
            return assigned, active, True
        # Explicit false, malformed or conflicting fields: reject both.
        return None, active, False
    if b"maybe_resume_failed" in line or b"inactive_thread_unsubscribed" in line:
        return None, None, False
    if b"thread_stream_view_activity_changed" in line:
        match = _ACTIVE_FIELD.search(line)
        active = match.group(1) == b"true" if match is not None else None
        present, value = _explicit_flag(line, _VIEW_HISTORY_KEY, _VIEW_HISTORY_FIELD)
        if attached or present:
            values = _ACTIVE_VALUE_FIELD.findall(line)
            if (len(values) != 1 or values[0] not in (b"true", b"false")
                    or not _single_thread_identity(line)):
                return None, None, False
        if present and value is not True:
            attached = False
        return role, active, attached
    return None


# Typed fresh-creation event: the manager tag must open the log message (after
# only a timestamp and bracketed level/tags), so the phrase quoted inside some
# other line's field or message text is never a creation event.
_CREATED_EVENT = re.compile(
    rb"^[0-9TZ:+\-., ]*(?:\[[^\]\s=]*\]\s+)*"
    rb"\[extension-app-server-manager\] Conversation created(?=\s)"
)
_VM_EVENT_FIELD = re.compile(rb"(?:^|\s)vmEvent=([^\s]*)(?=\s|$)")
_ROUTING_MARKERS = (
    b"thread_stream_role_changed", b"maybe_resume_success", b"maybe_resume_failed",
    b"inactive_thread_unsubscribed", b"thread_stream_view_activity_changed",
)


def _fresh_creation_identity(line: bytes) -> bool:
    """One ``vmEvent=thread_created`` for one conversationId == one threadId."""
    conversations = _CONVERSATION_FIELD.findall(line)
    threads = _THREAD_FIELD.findall(line)
    return (line.count(b"Conversation created") == 1
            and line.count(b"conversationId") == 1 and line.count(b"threadId") == 1
            and len(conversations) == 1 and threads == conversations
            and line.count(b"vmEvent") == 1
            and _VM_EVENT_FIELD.findall(line) == [b"thread_created"])


def _arms_fresh_creation(line: bytes, step: RoutingState) -> bool:
    """A single explicit ``active=true historyAttached=true`` activity line."""
    role, active, _attached = step
    if (role is not None or active is not True
            or line.count(b"thread_stream_view_activity_changed") != 1
            or any(marker in line for marker in _ROUTING_MARKERS[:4])):
        return False
    present, value = _explicit_flag(line, _VIEW_HISTORY_KEY, _VIEW_HISTORY_FIELD)
    # With historyAttached present, _routing_transition already required one
    # boolean active field and a single nonconflicting conversation identity.
    return present and value is True


def _session_routing_step(
    line: bytes, state: RoutingState, armed: bool,
) -> Tuple[Optional[RoutingState], bool]:
    """Generation-local routing step for one exact SID: ``(step, armed)``.

    ``armed`` is the only extra state: the SID's most recent routing line in
    this App Server generation was an explicit single ``active=true
    historyAttached=true`` view with no legacy role. A genuinely new
    conversation has no ``maybe_resume_success``; only the typed
    ``Conversation created ... vmEvent=thread_created`` event for that same
    SID, with an identical threadId, then sets the history attachment. Any
    other routing line re-derives ``armed`` (so false, inactive, failed,
    unsubscribed, role and malformed evidence disarm it), creation consumes
    it, and callers reset it with every other state at each spawn. Unrelated
    non-routing lines leave it unchanged but can never arm it.
    """
    step, armed, _fresh = _session_routing_event(line, state, armed)
    return step, armed


# Typed successful resume: anchored like the creation event, so a quoted
# ``maybe_resume_success`` inside another message is never a reopen.
_RESUME_EVENT = re.compile(
    rb"^[0-9TZ:+\-., ]*(?:\[[^\]\s=]*\]\s+)*"
    rb"\[extension-app-server-manager\] maybe_resume_success(?=\s)"
)
_LOCAL_LOG_STAMP = re.compile(
    rb"^([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3})\s")


def _local_log_seconds(line: bytes) -> Optional[float]:
    """VS Code's local ``YYYY-MM-DD HH:MM:SS.mmm`` prefix as epoch seconds.

    Same local-time semantics as the Windows ``local_log_time_ms``; anything
    else (missing, malformed, out of range) is ``None`` and proves nothing.
    """
    match = _LOCAL_LOG_STAMP.match(line)
    if match is None:
        return None
    stamp = match.group(1)
    try:
        parsed = time.strptime(stamp[:19].decode("ascii"), "%Y-%m-%d %H:%M:%S")
        return (int(time.mktime(parsed)) * 1000 + int(stamp[20:23])) / 1000.0
    except (ValueError, OverflowError, UnicodeDecodeError):
        return None


def _resume_attachment_event(line: bytes, step: RoutingState) -> bool:
    """An exact typed successful resume that attached this thread's owner."""
    role, active, attached = step
    if (_RESUME_EVENT.match(line) is None or active is False
            or line.count(b"maybe_resume_success") != 1
            or line.count(b"assignedStreamRole") > 1 or line.count(_ATTACHMENT_KEY) > 1
            or not (attached and role is None or role == "owner")):
        return False
    conversations = _CONVERSATION_FIELD.findall(line)
    threads = _THREAD_FIELD.findall(line)
    return (line.count(b"conversationId") == 1 and line.count(b"threadId") == 1
            and len(conversations) == 1 and threads == conversations)


def _attachment_proof_holds(step: RoutingState) -> bool:
    """A later routing state still compatible with an earlier attachment."""
    role, active, attached = step
    return active is not False and (role == "owner" or (role is None and attached))


def _session_routing_event(
    line: bytes, state: RoutingState, armed: bool,
) -> Tuple[Optional[RoutingState], bool, bool]:
    """``_session_routing_step`` plus whether ``line`` is a fresh attachment.

    Only a typed successful resume of the exact owner/new-format attachment
    or an armed typed fresh creation qualifies. View polling, role changes,
    quoted or malformed events never do; they can only keep or clear it.
    """
    if _CREATED_EVENT.match(line) is not None:
        role, active, _attached = state
        if any(marker in line for marker in _ROUTING_MARKERS):
            return (None, None, False), False, False
        if not armed:
            # Keep this explicit routing marker even when it cannot initialize
            # an attachment. Otherwise a bare creation (including one after a
            # spawn reset) could fall through to marker-less native recovery.
            return state, False, False
        if role is None and active is True and _fresh_creation_identity(line):
            # A history attachment only; never a legacy owner role.
            return (None, True, True), False, True
        return (None, None, False), False, False
    step = _routing_transition(line, state)
    if step is None:
        return None, armed, False
    return step, _arms_fresh_creation(line, step), _resume_attachment_event(line, step)


def _update_attachment_proof(
    proofs: Dict[str, float], session: str, step: Optional[RoutingState],
    fresh: bool, stamp: Optional[float],
) -> None:
    """Shared per-SID attachment-proof bookkeeping for both log parsers."""
    if step is None:
        return
    if fresh and stamp is not None:
        proofs[session] = stamp
    elif fresh or not _attachment_proof_holds(step):
        # An unparseable fresh event proves nothing and supersedes older proof.
        proofs.pop(session, None)


def _history_attached_resolution(
    loaded: Iterable[str],
    states: Dict[str, Tuple[Optional[str], Optional[bool]]],
    attached: Iterable[str],
    window_sessions: Optional[set],
    current: Sequence["LiveVSCodeWindow"],
    codex_home: Path,
    writer_process_probe: Callable[[Path], Optional[int]],
    lock_probe: WriterLockProbe,
    work_live: Optional[Callable[[], bool]] = None,
    attachment_at: Optional[Dict[str, float]] = None,
) -> Optional["SessionResolution"]:
    """Select a new-format attached thread only with exact OS writer proof.

    ``None`` means no loaded thread carries attachment evidence. Otherwise the
    attachment must be unique, explicitly active, without a legacy role, in a
    readable non-conflicting window cache, in exactly one live window whose
    app-server PID is the actual writer of a held lock, with no other loaded
    thread that may belong to this same app-server and is not explicitly inactive.
    """
    loaded = set(loaded)
    evidence = sorted(session for session in loaded.intersection(attached)
                      if states.get(session, (None, None))[1] is not False)
    if not evidence:
        return None
    refused = SessionResolution(
        "unresolved", None, None, "vscode_history_attachment_unverified")
    if (len(evidence) != 1 or window_sessions is None or len(current) != 1
            or (work_live is not None and not work_live())):
        return refused
    session = evidence[0]
    pid = current[0].codex_app_server_pid
    if (states.get(session) != (None, True) or pid is None
            or (window_sessions and session not in window_sessions)):
        return refused
    locks = Path(codex_home) / "thread-writer-locks"
    for other in sorted(loaded - {session}):
        if states.get(other, (None, None))[1] is False:
            continue
        if work_live is not None and not work_live():
            return refused
        other_pid = writer_process_probe(locks / f"{other}.lock")
        if other_pid is None or other_pid == pid:
            return refused
    lock = locks / f"{session}.lock"
    if work_live is not None and not work_live():
        return refused
    if (writer_process_probe(lock) != pid or not lock_probe(lock)
            or (work_live is not None and not work_live())):
        return refused
    return SessionResolution(
        "resolved", session, HISTORY_ATTACHED_SOURCE, None,
        (attachment_at or {}).get(session),
    )


def _codex_log_session_evidence(
    path: Path, session_ids: Iterable[str], *,
    attachment_at: Optional[Dict[str, float]] = None,
):
    """``(states, markers, attached)``; optionally fills ``attachment_at``.

    ``attachment_at`` (cleared first) receives, per SID, the immutable local
    log time of its current-generation fresh/reopened exact attachment.
    """
    proofs: Dict[str, float] = {}
    if attachment_at is not None:
        attachment_at.clear()
    states = {session: (None, None) for session in session_ids
              if _canonical_session_id(session) == session}
    attached: set = set()
    if not states or not path.is_file():
        return states, None, attached
    markers = set()
    armed: set = set()  # Generation-local fresh-creation precondition per SID.
    conversation = re.compile(
        rb"(?:^|\s)conversationId=(" + b"|".join(
            re.escape(session.encode("ascii")) for session in states
        ) + rb")(?=\s|$)"
    )
    try:
        with path.open("rb") as handle:
            for line in handle:
                if b"[CodexMcpConnection] Spawning codex app-server" in line:
                    states = dict.fromkeys(states, (None, None))
                    markers.clear()
                    attached.clear()
                    armed.clear()
                    proofs.clear()
                    continue
                matched = conversation.search(line)
                if matched is None:
                    continue
                session = matched.group(1).decode("ascii")
                role, active = states[session]
                step, is_armed, fresh = _session_routing_event(
                    line, (role, active, session in attached), session in armed)
                _update_attachment_proof(
                    proofs, session, step, fresh,
                    _local_log_seconds(line) if fresh else None)
                if is_armed:
                    armed.add(session)
                else:
                    armed.discard(session)
                if step is not None:
                    markers.add(session)
                    role, active, is_attached = step
                    if is_attached:
                        attached.add(session)
                    else:
                        attached.discard(session)
                states[session] = (role, active)
    except OSError:
        return dict.fromkeys(states, (None, None)), None, set()
    if attachment_at is not None:
        attachment_at.update(proofs)
    return states, markers, attached


@dataclass(frozen=True)
class LiveVSCodeWindow:
    window_number: str
    extension_host_pid: int
    codex_app_server_pid: Optional[int]
    codex_log: Optional[Path]


class VSCodeLiveWindowIndex:
    """Correlate live `Code.exe --status` windows with privacy-safe log markers."""

    def __init__(
        self,
        user_data_root: Path,
        *,
        status_runner: CodeStatusRunner = _default_code_status,
        native_runner: Callable[[], Optional[Tuple[NativeVSCodeHost, ...]]] = native_vscode_hosts,
    ) -> None:
        self.user_data_root = _canonical_local_path(user_data_root)
        self.status_runner = status_runner
        self.native_runner = native_runner

    def snapshot(self) -> Optional[Dict[str, LiveVSCodeWindow]]:
        status = self.status_runner()
        processes = self._parse_status(status) if isinstance(status, str) else {}
        native_hosts = self.native_runner() or ()
        by_storage: Dict[str, List[LiveVSCodeWindow]] = {}
        logs_root = self.user_data_root.parent / "logs"
        try:
            log_sessions = sorted(
                (path for path in logs_root.iterdir() if path.is_dir()),
                key=lambda path: path.name,
                reverse=True,
            )
        except OSError:
            return None
        native_matches = {}
        conflicts = set()
        for host in native_hosts:
            try:
                parts = host.log.resolve().relative_to(logs_root.resolve()).parts
            except (OSError, ValueError):
                continue
            if (len(parts) != 4 or not re.fullmatch(r'window[0-9]+', parts[1])
                    or parts[2:] != ('exthost', 'exthost.log')):
                continue
            number = parts[1][6:]
            match = self._latest_window_log([logs_root / parts[0]], number, host.pid)
            if match is None:
                continue
            pair = (host.pid, host.codex_pid)
            previous = processes.get(number)
            if number in native_matches or previous not in (None, pair, (host.pid, None)):
                conflicts.add(number)
            processes[number] = pair
            native_matches[number] = match
        if not processes:
            return None
        for window_number, (extension_host_pid, codex_pid) in processes.items():
            if window_number in conflicts:
                continue
            match = native_matches.get(window_number) or self._latest_window_log(
                log_sessions, window_number, extension_host_pid
            )
            if match is None:
                continue
            storage_key, codex_log = match
            by_storage.setdefault(storage_key, []).append(
                LiveVSCodeWindow(
                    window_number, extension_host_pid, codex_pid, codex_log,
                )
            )
        return {
            storage_key: windows[0]
            for storage_key, windows in by_storage.items()
            if len(windows) == 1
        }

    @staticmethod
    def _parse_status(status: str) -> Dict[str, Tuple[int, Optional[int]]]:
        windows: Dict[str, Tuple[int, Optional[int]]] = {}
        current_window: Optional[str] = None
        for line in status.splitlines():
            fields = line.split("\t", 3)
            if len(fields) != 4 or not fields[2].strip().isdigit():
                current_window = None
                continue
            process_id = int(fields[2].strip())
            description = fields[3].rstrip()
            stripped = description.strip()
            window_match = re.fullmatch(r"extension-host \[([0-9]+)\]", stripped)
            if window_match is not None and description == stripped:
                current_window = window_match.group(1)
                windows[current_window] = (process_id, None)
                continue
            if description == stripped:
                current_window = None
                continue
            if current_window is None:
                continue
            if is_codex_app_server_description(stripped):
                extension_host_pid, existing = windows[current_window]
                windows[current_window] = (
                    extension_host_pid,
                    process_id if existing is None else -1,
                )
        return {
            window: (host_pid, codex_pid)
            for window, (host_pid, codex_pid) in windows.items()
            if codex_pid != -1
        }

    @staticmethod
    def _latest_window_log(
        log_sessions: Sequence[Path], window_number: str, extension_host_pid: int
    ) -> Optional[Tuple[str, Optional[Path]]]:
        marker = re.compile(rb"workspaceStorage[\\/]([^\\/\s.]+)", re.IGNORECASE)
        host_marker = re.compile(
            rb"Extension host with pid ([0-9]+) started", re.IGNORECASE
        )
        exit_marker = re.compile(
            rb"Extension host with pid ([0-9]+) exiting with code", re.IGNORECASE
        )
        termination_marker = re.compile(rb"Extension host terminating:", re.IGNORECASE)
        for log_session in log_sessions:
            extension_host = (
                log_session / f"window{window_number}" / "exthost" / "exthost.log"
            )
            if not extension_host.is_file():
                continue
            try:
                with extension_host.open("rb") as handle:
                    storage_keys = set()
                    target_started = False
                    pending_terminations = 0
                    for line in handle:
                        start = host_marker.search(line)
                        exited = exit_marker.search(line)
                        if start is not None:
                            # A reused window can switch workspace/locality while
                            # appending to the same log. Bind storage only to the
                            # current exact host generation, never an older one.
                            storage_keys.clear()
                            target_started = int(start.group(1)) == extension_host_pid
                            pending_terminations = 0
                        elif termination_marker.search(line) is not None:
                            if target_started:
                                pending_terminations += 1
                        elif exited is not None:
                            exited_pid = int(exited.group(1))
                            if exited_pid == extension_host_pid:
                                target_started = False
                                pending_terminations = 0
                            elif pending_terminations:
                                # A replacement can start before the old host emits
                                # its generic termination plus exact-PID exit.
                                pending_terminations -= 1
                        if target_started:
                            storage_keys.update(
                                match.group(1).decode("ascii", errors="ignore")
                                for match in marker.finditer(line)
                            )
            except OSError:
                return None
            if not target_started or pending_terminations or len(storage_keys) != 1:
                return None
            codex_log = extension_host.parent / "openai.chatgpt" / "Codex.log"
            return (
                next(iter(storage_keys)),
                codex_log if codex_log.is_file() else None,
            )
        return None


@dataclass(frozen=True)
class SessionResolution:
    status: str
    session_id: Optional[str]
    source: Optional[str]
    reason: Optional[str]
    # Transient: immutable local log time of a verified exact fresh/reopened
    # attachment; only on successful exact-current-owner resolutions. Never
    # serialized or compared, so routing decisions are unchanged.
    attachment_at: Optional[float] = field(default=None, compare=False, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "session_id": self.session_id,
            "source": self.source,
            "reason": self.reason,
        }


class CodexSessionResolver:
    """Resolve only exact loaded VS Code user threads for one local workspace."""

    def __init__(
        self,
        runtime: Path,
        codex_home: Path,
        *,
        lock_probe: WriterLockProbe = writer_lock_is_held,
        owner_probe: SessionOwnerProbe = codex_log_owns_session,
        writer_process_probe: Callable[[Path], Optional[int]] = writer_lock_process,
    ) -> None:
        self.runtime = Path(runtime)
        self.codex_home = _canonical_local_path(Path(codex_home))
        self.lock_probe = lock_probe
        self.owner_probe = owner_probe
        self.writer_process_probe = writer_process_probe

    def resolve(
        self,
        paths: Iterable[Path],
        *,
        codex_log: Optional[Path],
        window_state_database: Optional[Path],
        live_windows: Sequence[LiveVSCodeWindow] = (),
    ) -> SessionResolution:
        if codex_log is None:
            return SessionResolution(
                "unresolved", None, None, "vscode_codex_log_unavailable"
            )
        window_sessions = (
            self.window_session_candidates(window_state_database)
            if window_state_database is not None
            else None
        )
        identities = {_path_identity(path) for path in paths}
        loaded, database_status = self._database_candidates(identities)
        attachment_at: Dict[str, float] = {}
        states, markers, attached = _codex_log_session_evidence(
            codex_log, loaded, attachment_at=attachment_at)
        database_candidates = {
            session for session in loaded if self.owner_probe(codex_log, session)
        }
        # Codex can retain an existing thread's writer in another window. A
        # follower is a view of that exact thread, not a different conversation.
        # Only an explicitly active view plus one independently live owner can
        # establish this association. Global caches and timestamps cannot.
        followers = {
            session for session, state in states.items()
            if state == ("follower", True)
        }
        for session in followers:
            lock = self.codex_home / "thread-writer-locks" / f"{session}.lock"
            writer_pid = self.writer_process_probe(lock)
            owners = {
                window.codex_log for window in live_windows
                if writer_pid is not None
                and window.codex_app_server_pid == writer_pid
                and window.codex_log is not None and window.codex_log != codex_log
                and self.owner_probe(window.codex_log, session)
            }
            if len(owners) != 1:
                return SessionResolution(
                    "unresolved", None, None,
                    "ambiguous_vscode_thread_owner" if owners
                    else "vscode_thread_owner_unverified",
                )
            if not self.lock_probe(lock):
                return SessionResolution("unresolved", None, None, "vscode_thread_owner_unverified")
        database_candidates.update(followers)

        # Current extensions omit legacy roles and report only a local history
        # attachment. It never becomes an owner role; the exact live window's
        # OS writer must independently prove the association.
        if not database_candidates:
            attachment = _history_attached_resolution(
                loaded, states, attached, window_sessions,
                [window for window in live_windows if window.codex_log == codex_log],
                self.codex_home, self.writer_process_probe, self.lock_probe,
                attachment_at=attachment_at,
            )
            if attachment is not None:
                return attachment

        # Native logs and the window cache can both lose their earlier session
        # evidence while the same App Server still owns a thread. A unique
        # loaded exact thread can instead be proved by this window's actual OS
        # writer PID. An empty, readable cache carries no conflicting selection;
        # an unavailable cache or a different cached selection cannot qualify.
        # Never use this for a peer owner, ambiguous chat selection, unreadable log,
        # or explicit routing failure/inactivity in the current generation.
        if not database_candidates and len(loaded) == 1 and markers is not None:
            session = next(iter(loaded))
            current = [window for window in live_windows if window.codex_log == codex_log]
            if (session not in markers and window_sessions is not None
                    and (not window_sessions or session in window_sessions) and len(current) == 1
                    and current[0].codex_app_server_pid is not None):
                lock = self.codex_home / "thread-writer-locks" / f"{session}.lock"
                if (self.writer_process_probe(lock) == current[0].codex_app_server_pid
                        and self.lock_probe(lock)):
                    return SessionResolution(
                        "resolved", session, "codex_state_vscode_native_writer", None,
                    )

        def resolved(session: str, source: str) -> SessionResolution:
            if session in followers:
                return SessionResolution(
                    "resolved", session,
                    "codex_state_vscode_active_follower_verified_owner",
                    "vscode_thread_owned_by_another_window",
                )
            # Exact current owner of this window's generation only.
            proof = attachment_at.get(session) if states.get(session, (None,))[0] == "owner" else None
            return SessionResolution("resolved", session, source, None, proof)

        if len(database_candidates) == 1:
            session_id = next(iter(database_candidates))
            source = (
                "codex_state_vscode_window_cache_owner"
                if window_sessions is not None and session_id in window_sessions
                else "codex_state_vscode_live_owner"
            )
            return resolved(session_id, source)
        if len(database_candidates) > 1:
            # Switching chats can leave both writer locks and owner roles live.
            # Only explicit current-window activity can disambiguate them; the
            # cache and last-used timestamps alone are not ownership evidence.
            active = [session for session, (role, view_active) in states.items()
                      if session in database_candidates and view_active is True]
            if len(active) == 1 and all(
                role == "owner" and view_active is False
                for session, (role, view_active) in states.items()
                if session in database_candidates and session != active[0]
            ):
                return resolved(active[0], "codex_state_vscode_active_owner")
            return SessionResolution(
                "unresolved", None, None, "ambiguous_loaded_threads"
            )

        if window_sessions is None:
            reason = "vscode_session_cache_unavailable"
        elif not window_sessions:
            reason = "no_window_codex_thread"
        elif database_status == "unavailable":
            reason = "codex_state_unavailable"
        else:
            reason = "no_loaded_vscode_thread"
        return SessionResolution("unresolved", None, None, reason)

    @staticmethod
    def window_session_candidates(database: Path) -> Optional[set[str]]:
        """Select only Codex resource IDs from one VS Code window's private cache."""

        if not database.is_file():
            return None
        try:
            uri = database.resolve(strict=True).as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=0.25) as connection:
                connection.execute("PRAGMA query_only = ON")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "ItemTable" not in tables:
                    return None
                shape = list(
                    connection.execute(
                        "SELECT json_valid(value), json_type(value) "
                        "FROM ItemTable WHERE key = ?",
                        (_CODEX_SESSION_CACHE_KEY,),
                    )
                )
                if len(shape) != 1 or shape[0] != (1, "array"):
                    return None
                entries = list(
                    connection.execute(
                        "SELECT entry.type, "
                        "json_extract(entry.value, '$.providerType'), "
                        "json_type(entry.value, '$.providerType'), "
                        "json_extract(entry.value, '$.resource'), "
                        "json_type(entry.value, '$.resource') "
                        "FROM ItemTable JOIN json_each(ItemTable.value) AS entry "
                        "WHERE ItemTable.key = ?",
                        (_CODEX_SESSION_CACHE_KEY,),
                    )
                )
        except (OSError, RuntimeError, sqlite3.Error):
            return None

        candidates = set()
        for entry_type, provider, provider_type, resource, resource_type in entries:
            if entry_type != "object" or provider_type != "text":
                return None
            if provider != "openai-codex":
                continue
            if resource_type != "text" or not isinstance(resource, str):
                return None
            if not resource.startswith(_CODEX_SESSION_RESOURCE_PREFIX):
                return None
            suffix = resource[len(_CODEX_SESSION_RESOURCE_PREFIX) :]
            canonical = _canonical_session_id(suffix)
            if canonical is None or len(suffix) != 36:
                return None
            candidates.add(canonical)
        return candidates

    def _database_candidates(
        self, identities: set[str],
    ) -> Tuple[set[str], str]:
        database = self.codex_home / "state_5.sqlite"
        if not database.is_file():
            return set(), "unavailable"
        try:
            uri = database.resolve(strict=True).as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=0.25) as connection:
                connection.execute("PRAGMA query_only = ON")
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(threads)")
                }
                required = {
                    "id",
                    "cwd",
                    "source",
                    "thread_source",
                    "archived",
                }
                if not required.issubset(columns):
                    return set(), "unavailable"
                rows = connection.execute(
                    "SELECT id, cwd FROM threads "
                    "WHERE archived = 0 AND source = 'vscode' "
                    "AND thread_source = 'user'"
                )
                candidates = set()
                for raw_session_id, raw_cwd in rows:
                    session_id = _canonical_session_id(raw_session_id)
                    if session_id is None or not isinstance(raw_cwd, str):
                        continue
                    try:
                        cwd_identity = _path_identity(Path(raw_cwd))
                    except (OSError, RuntimeError, ValueError):
                        continue
                    if cwd_identity not in identities:
                        continue
                    lock = (
                        self.codex_home / "thread-writer-locks" / f"{session_id}.lock"
                    )
                    if self.lock_probe(lock):
                        candidates.add(session_id)
                return candidates, "available"
        except (OSError, RuntimeError, sqlite3.Error):
            return set(), "unavailable"


@dataclass(frozen=True)
class DiscoveredWindow:
    workspace_uri: str
    workspace_storage_key: Optional[str]
    locality: str
    remote_authority: Optional[str]
    workspace_path: Optional[str]
    repo_root: Optional[str]
    session_id: Optional[str]
    session_source: Optional[str]
    workspace_id: Optional[str]
    tracking_status: str
    reason: Optional[str]
    source: str
    session_candidates: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_uri": self.workspace_uri,
            "workspace_storage_key": self.workspace_storage_key,
            "locality": self.locality,
            "remote_authority": self.remote_authority,
            "workspace_path": self.workspace_path,
            "repo_root": self.repo_root,
            "session_id": self.session_id,
            "session_source": self.session_source,
            "workspace_id": self.workspace_id,
            "tracking_status": self.tracking_status,
            "reason": self.reason,
            "source": self.source,
        }


@dataclass(frozen=True)
class DiscoverySnapshot:
    status: str
    discovered_at: str
    windows: Tuple[DiscoveredWindow, ...]
    effective_workspaces: Tuple[TrackedWorkspace, ...]
    issues: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        remote_count = sum(
            window.locality == REMOTE_SSH and window.tracking_status == "remote_adapter"
            for window in self.windows
        )
        return {
            "schema_version": DISCOVERY_SCHEMA_VERSION,
            "status": self.status,
            "discovered_at": self.discovered_at,
            "window_count": len(self.windows),
            "tracked_workspace_count": len(self.effective_workspaces),
            "remote_workspace_count": remote_count,
            "windows": [window.to_dict() for window in self.windows],
            "effective_workspaces": [
                workspace.to_dict() for workspace in self.effective_workspaces
            ],
            "issues": list(self.issues),
        }


class VSCodeWorkspaceDiscovery:
    """Discover currently open VS Code workspaces without screen scraping."""

    def __init__(
        self,
        runtime: Path,
        *,
        codex_home: Optional[Path] = None,
        user_data_root: Optional[Path] = None,
        registry: Optional[WorkspaceRegistry] = None,
        session_resolver: Optional[CodexSessionResolver] = None,
        live_window_index: Optional[VSCodeLiveWindowIndex] = None,
        platform_adapter: Optional[HostPlatformAdapter] = None,
        git_root_resolver: GitRootResolver = _default_git_root,
        exclude: Sequence[str] = (),
        sleep: Sleep = time.sleep,
    ) -> None:
        self.runtime = Path(runtime)
        self.platform_adapter = (
            detect_platform_adapter()
            if platform_adapter is None
            else platform_adapter
        )
        selected_codex_home = (
            Path(codex_home)
            if codex_home is not None
            else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        )
        self.codex_home = _canonical_local_path(selected_codex_home)
        if user_data_root is None:
            self.user_data_root = (
                self.platform_adapter.primary_vscode_user_data_root()
            )
        else:
            self.user_data_root = _canonical_local_path(Path(user_data_root))
        self.registry = registry if registry is not None else WorkspaceRegistry(runtime)
        self.session_resolver = (
            session_resolver
            if session_resolver is not None
            else CodexSessionResolver(runtime, self.codex_home)
        )
        self.live_window_index = (
            live_window_index
            if live_window_index is not None
            else self._windows_live_window_index()
            if self.user_data_root is not None
            and self.platform_adapter.system == "windows" and os.name == "nt"
            else VSCodeLiveWindowIndex(
                self.user_data_root,
                status_runner=lambda: run_vscode_status(
                    self.platform_adapter
                ).output,
            )
            if self.user_data_root is not None
            else None
        )
        self.git_root_resolver = git_root_resolver
        self.exclude = tuple(item.casefold() for item in exclude if item)
        self.sleep = sleep
        self._windows_json_budget = None

    def _windows_live_window_index(self) -> VSCodeLiveWindowIndex:
        from .windows_vscode_discovery import WindowsVSCodeLiveWindowIndex

        assert self.user_data_root is not None
        return WindowsVSCodeLiveWindowIndex(
            self.user_data_root,
            status_probe=lambda: run_vscode_status(self.platform_adapter),
        )

    def _windows_hook(self, name: str):
        # Strict closure: Windows evidence hooks dispatch only on an actual
        # Windows host, even for an injected or custom live-window index.
        if self.platform_adapter.system != "windows" or os.name != "nt":
            return None
        return getattr(self.live_window_index, name, None)

    def _windows_live_issues(self) -> Tuple[str, ...]:
        # Only the Windows evidence adapter exposes this; other indexes add nothing.
        issues = self._windows_hook("windows_discovery_issues")
        return tuple(issues()) if issues is not None else ()

    def snapshot(self) -> DiscoverySnapshot:
        discovered_at = utc_now()
        if self.platform_adapter.system == "windows" and os.name == "nt":
            from .windows_vscode_discovery import WorkBudget

            self._windows_json_budget = WorkBudget()
        explicit = tuple(self.registry.list_workspaces())
        effective: List[TrackedWorkspace] = list(explicit)
        explicit_by_repo = {
            _path_identity(workspace.repo_root): workspace for workspace in explicit
        }
        explicit_ids = {workspace.workspace_id for workspace in explicit}

        if self.user_data_root is None:
            return DiscoverySnapshot(
                "error",
                discovered_at,
                (),
                tuple(sorted(effective, key=lambda item: item.workspace_id)),
                ("vscode_user_data_unavailable",),
            )

        storage = self._read_json_retry(
            self.user_data_root / "globalStorage" / "storage.json"
        )
        if not isinstance(storage, dict):
            return DiscoverySnapshot(
                "error",
                discovered_at,
                (),
                tuple(sorted(effective, key=lambda item: item.workspace_id)),
                ("vscode_window_state_unavailable",),
            )
        entries = self._open_window_entries(storage)
        if entries is None:
            return DiscoverySnapshot(
                "error",
                discovered_at,
                (),
                tuple(sorted(effective, key=lambda item: item.workspace_id)),
                ("vscode_window_state_malformed",),
            )
        current_storage_keys = self._windows_hook("windows_storage_keys")
        storage_keys = (
            self._workspace_storage_keys() if current_storage_keys is None else {}
        )
        live_windows = (
            self.live_window_index.snapshot()
            if self.live_window_index is not None
            else None
        )
        if live_windows is None:
            return DiscoverySnapshot(
                "error",
                discovered_at,
                (),
                tuple(sorted(effective, key=lambda item: item.workspace_id)),
                tuple(sorted({"vscode_live_status_unavailable", *self._windows_live_issues()})),
            )
        if current_storage_keys is not None:
            # Windows reads storage only for exact current live keys.
            storage_keys = current_storage_keys(live_windows, self.sleep)
        if self.platform_adapter.system == "windows" and os.name == "nt":
            from .windows_vscode_discovery import COLLECTION_SECONDS

            # Independent CLI/native work may have taken longer than the JSON
            # phase deadline. Renew time once for saved workspace documents,
            # retaining all charged bytes and retries; never renew per target.
            budget = self._windows_json_budget
            budget.deadline = budget.clock() + COLLECTION_SECONDS

        windows: List[DiscoveredWindow] = []
        candidates: List[Tuple[int, TrackedWorkspace]] = []
        issues: List[str] = list(self._windows_live_issues())
        matched_live_keys = set()
        windows_order = self._windows_hook("windows_order_entries")
        if windows_order is not None:
            entries = windows_order(entries)
        for entry in entries:
            uri = entry["uri"]
            matching_keys = storage_keys.get(uri, ())
            live_matching = tuple(
                storage_key
                for storage_key in matching_keys
                if storage_key in live_windows
            )
            matched_live_keys.update(live_matching)
            if len(live_matching) != 1:
                reason = (
                    "workspace_storage_ambiguous"
                    if len(live_matching) > 1
                    else "workspace_storage_missing"
                    if not matching_keys and current_storage_keys is None
                    else "vscode_live_window_unmapped"
                )
                if live_matching:
                    windows.append(
                        DiscoveredWindow(
                            uri,
                            None,
                            self._uri_locality(uri),
                            _remote_authority(uri, entry.get("remote_authority")),
                            None,
                            None,
                            None,
                            None,
                            None,
                            "unresolved",
                            reason,
                            "automatic",
                        )
                    )
                issues.append(reason)
                continue
            storage_key = live_matching[0]
            live_window = live_windows.get(storage_key)
            assert live_window is not None
            locality = self._uri_locality(uri)
            if locality == REMOTE_SSH:
                try:
                    remote_path = _remote_uri_path(uri)
                except ValueError:
                    windows.append(
                        DiscoveredWindow(
                            uri,
                            storage_key,
                            REMOTE_SSH,
                            _remote_authority(uri, entry.get("remote_authority")),
                            None,
                            None,
                            None,
                            None,
                            None,
                            "unresolved",
                            "remote_workspace_uri_invalid",
                            "automatic",
                        )
                    )
                    issues.append("remote_workspace_uri_invalid")
                    continue
                if self._is_remote_excluded(uri, remote_path):
                    windows.append(
                        DiscoveredWindow(
                            uri,
                            storage_key,
                            REMOTE_SSH,
                            _remote_authority(uri, entry.get("remote_authority")),
                            remote_path,
                            None,
                            None,
                            None,
                            None,
                            "excluded",
                            "tracking_excluded",
                            "automatic",
                        )
                    )
                    continue
                remote_sessions = self.session_resolver.window_session_candidates(
                    self.user_data_root
                    / "workspaceStorage"
                    / storage_key
                    / "state.vscdb"
                )
                claimed_session = (
                    next(iter(remote_sessions))
                    if remote_sessions is not None and len(remote_sessions) == 1
                    else None
                )
                windows.append(
                    DiscoveredWindow(
                        uri,
                        storage_key,
                        REMOTE_SSH,
                        _remote_authority(uri, entry.get("remote_authority")),
                        remote_path,
                        None,
                        claimed_session,
                        (
                            "vscode_window_cache_remote_claim"
                            if claimed_session is not None
                            else None
                        ),
                        None,
                        "remote_adapter",
                        None,
                        "automatic",
                        tuple(sorted(remote_sessions or ())),
                    )
                )
                continue
            if locality != PROCESS_LOCAL:
                windows.append(
                    DiscoveredWindow(
                        uri,
                        storage_key,
                        locality,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "unresolved",
                        "unsupported_workspace_uri",
                        "automatic",
                    )
                )
                issues.append("unsupported_workspace_uri")
                continue
            if live_window.codex_app_server_pid is None:
                windows.append(
                    DiscoveredWindow(
                        uri,
                        storage_key,
                        PROCESS_LOCAL,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "unresolved",
                        "vscode_codex_app_server_unavailable",
                        "automatic",
                    )
                )
                issues.append("vscode_codex_app_server_unavailable")
                continue

            local_paths, path_reason = self._local_paths(entry)
            if path_reason is not None:
                windows.append(
                    DiscoveredWindow(
                        uri,
                        storage_key,
                        PROCESS_LOCAL,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "unresolved",
                        path_reason,
                        "automatic",
                    )
                )
                issues.append(path_reason)
                continue
            for local_path in local_paths:
                self._inspect_local_window(
                    uri,
                    storage_key,
                    local_path,
                    live_window.codex_log,
                    tuple(live_windows.values()),
                    explicit_by_repo,
                    windows,
                    candidates,
                    issues,
                )

        if set(live_windows) - matched_live_keys:
            issues.append("vscode_live_window_state_unmatched")

        grouped: Dict[str, List[Tuple[int, TrackedWorkspace]]] = {}
        for index, workspace in candidates:
            grouped.setdefault(_path_identity(workspace.repo_root), []).append(
                (index, workspace)
            )
        for group in grouped.values():
            sessions = {workspace.session_id for _, workspace in group}
            if len(sessions) > 1:
                issues.append("ambiguous_repo_ownership")
                for index, _ in group:
                    windows[index] = replace(
                        windows[index],
                        workspace_id=None,
                        tracking_status="unresolved",
                        reason="ambiguous_repo_ownership",
                    )
                continue
            selected_index, selected = min(group, key=lambda pair: pair[1].workspace_id)
            if selected.workspace_id in explicit_ids:
                issues.append("workspace_id_collision")
                windows[selected_index] = replace(
                    windows[selected_index],
                    workspace_id=None,
                    tracking_status="unresolved",
                    reason="workspace_id_collision",
                )
                continue
            effective.append(selected)
            for index, workspace in group:
                if workspace.workspace_id == selected.workspace_id:
                    continue
                windows[index] = replace(
                    windows[index],
                    workspace_id=selected.workspace_id,
                    tracking_status="tracked_deduplicated",
                    reason="duplicate_repo_session",
                )

        status = "ok" if not issues else "partial"
        return DiscoverySnapshot(
            status,
            discovered_at,
            tuple(windows),
            tuple(sorted(effective, key=lambda item: item.workspace_id)),
            tuple(sorted(set(issues))),
        )

    def _inspect_local_window(
        self,
        uri: str,
        storage_key: str,
        local_path: Path,
        codex_log: Optional[Path],
        live_windows: Sequence[LiveVSCodeWindow],
        explicit_by_repo: Dict[str, TrackedWorkspace],
        windows: List[DiscoveredWindow],
        candidates: List[Tuple[int, TrackedWorkspace]],
        issues: List[str],
    ) -> None:
        if not local_path.is_dir():
            windows.append(
                DiscoveredWindow(
                    uri,
                    storage_key,
                    PROCESS_LOCAL,
                    None,
                    str(local_path),
                    None,
                    None,
                    None,
                    None,
                    "unresolved",
                    "workspace_path_missing",
                    "automatic",
                )
            )
            issues.append("workspace_path_missing")
            return
        try:
            repo_root = self.git_root_resolver(local_path)
        except Exception:
            repo_root = None
        if repo_root is None:
            windows.append(
                DiscoveredWindow(
                    uri,
                    storage_key,
                    PROCESS_LOCAL,
                    None,
                    str(local_path),
                    None,
                    None,
                    None,
                    None,
                    "unresolved",
                    "not_git_repository",
                    "automatic",
                )
            )
            issues.append("not_git_repository")
            return
        repo_root = _canonical_local_path(repo_root)
        repo_identity = _path_identity(repo_root)
        manual = explicit_by_repo.get(repo_identity)
        if manual is not None:
            windows.append(
                DiscoveredWindow(
                    uri,
                    storage_key,
                    PROCESS_LOCAL,
                    None,
                    str(local_path),
                    str(repo_root),
                    manual.session_id,
                    "manual_registry",
                    manual.workspace_id,
                    "tracked",
                    None,
                    "manual_override",
                )
            )
            return
        if self._is_excluded(uri, repo_root):
            windows.append(
                DiscoveredWindow(
                    uri,
                    storage_key,
                    PROCESS_LOCAL,
                    None,
                    str(local_path),
                    str(repo_root),
                    None,
                    None,
                    None,
                    "excluded",
                    "tracking_excluded",
                    "automatic",
                )
            )
            return
        window_state_database = (
            self.user_data_root / "workspaceStorage" / storage_key / "state.vscdb"
        )
        windows_resolve = self._windows_hook("windows_resolve")
        resolution = (
            # Windows resolves only current routed candidates by exact indexed ID.
            windows_resolve(
                (local_path, repo_root),
                codex_log=codex_log,
                window_state_database=window_state_database,
                live_windows=live_windows,
                session_resolver=self.session_resolver,
            )
            if windows_resolve is not None
            else None
        )
        if resolution is None:
            resolution = self.session_resolver.resolve(
                (local_path, repo_root),
                codex_log=codex_log,
                window_state_database=window_state_database,
                live_windows=live_windows,
            )
        admit = self._windows_hook("windows_fallback_admission")
        if admit is not None and resolution.session_id is not None:
            # Windows native-only windows need their own held-writer proof.
            resolution = admit(
                storage_key,
                resolution,
                codex_home=self.codex_home,
                window_state_database=window_state_database,
                live_windows=live_windows,
                session_resolver=self.session_resolver,
            )
        if resolution.session_id is None:
            windows.append(
                DiscoveredWindow(
                    uri,
                    storage_key,
                    PROCESS_LOCAL,
                    None,
                    str(local_path),
                    str(repo_root),
                    None,
                    resolution.source,
                    None,
                    "unresolved",
                    resolution.reason,
                    "automatic",
                )
            )
            issues.append(resolution.reason or "session_unresolved")
            return
        workspace_id = (
            "vscode-"
            + sha256_text(
                "\0".join(
                    (
                        str(self.user_data_root),
                        storage_key,
                        uri,
                        repo_identity,
                        resolution.session_id,
                    )
                )
            )[:32]
        )
        tracked = TrackedWorkspace.create(
            workspace_id, repo_root, resolution.session_id
        )
        if (resolution.status == "resolved" and resolution.reason is None
                and resolution.attachment_at is not None):
            # Transient verified-owner attachment proof; never persisted.
            tracked = replace(tracked, attachment_at=resolution.attachment_at)
        index = len(windows)
        windows.append(
            DiscoveredWindow(
                uri,
                storage_key,
                PROCESS_LOCAL,
                None,
                str(local_path),
                str(repo_root),
                resolution.session_id,
                resolution.source,
                workspace_id,
                "tracked",
                None,
                "automatic",
            )
        )
        candidates.append((index, tracked))
        if resolution.reason is not None:
            windows[index] = replace(windows[index], reason=resolution.reason)
            issues.append(resolution.reason)

    def _read_json_retry(self, path: Path) -> Optional[Any]:
        if self.platform_adapter.system == "windows" and os.name == "nt":
            from .windows_vscode_discovery import WorkBudget, read_metadata_json

            if self._windows_json_budget is None:
                self._windows_json_budget = WorkBudget()
            return read_metadata_json(path, self._windows_json_budget, self.sleep)
        for attempt in range(3):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, UnicodeError, json.JSONDecodeError):
                if attempt < 2:
                    self.sleep(0.02)
        return None

    @staticmethod
    def _open_window_entries(storage: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        windows_state = storage.get("windowsState")
        if not isinstance(windows_state, dict):
            return None
        raw_opened = windows_state.get("openedWindows")
        if not isinstance(raw_opened, list):
            return None
        raw_entries = list(raw_opened)
        last_active = windows_state.get("lastActiveWindow")
        if isinstance(last_active, dict):
            raw_entries.append(last_active)
        entries = []
        seen = set()
        for raw in raw_entries:
            if not isinstance(raw, dict):
                return None
            folder = raw.get("folder")
            workspace = raw.get("workspace")
            identifier = raw.get("workspaceIdentifier")
            if identifier is not None:
                if not isinstance(identifier, dict):
                    return None
                config_uri = identifier.get("configURIPath")
                if not isinstance(config_uri, str) or not config_uri:
                    return None
                # Current VS Code stores saved workspaces here. Conflicting
                # targets must not silently fall back to the legacy fields.
                if (isinstance(folder, str) and folder) or (
                    isinstance(workspace, str) and workspace and workspace != config_uri
                ):
                    return None
                workspace = config_uri
            if isinstance(folder, str) and folder:
                uri = folder
                kind = "folder"
            elif isinstance(workspace, str) and workspace:
                uri = workspace
                kind = "workspace"
            else:
                continue
            remote = raw.get("remoteAuthority")
            identity = (uri, remote if isinstance(remote, str) else None)
            if identity in seen:
                continue
            seen.add(identity)
            entries.append(
                {"uri": uri, "kind": kind, "remote_authority": identity[1],}
            )
        return entries

    def _workspace_storage_keys(self) -> Dict[str, Tuple[str, ...]]:
        assert self.user_data_root is not None
        root = self.user_data_root / "workspaceStorage"
        matches: Dict[str, List[str]] = {}
        if not root.is_dir():
            return {}
        try:
            directories = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError:
            return {}
        for directory in directories:
            if not directory.is_dir():
                continue
            document = self._read_json_retry(directory / "workspace.json")
            if not isinstance(document, dict):
                continue
            for field in ("folder", "workspace"):
                value = document.get(field)
                if isinstance(value, str) and value:
                    matches.setdefault(value, []).append(directory.name)
        return {uri: tuple(keys) for uri, keys in matches.items()}

    @staticmethod
    def _uri_locality(uri: str) -> str:
        scheme = urlsplit(uri).scheme.lower()
        if scheme == "file":
            return PROCESS_LOCAL
        if scheme == "vscode-remote":
            authority = unquote(urlsplit(uri).netloc).lower()
            if authority.startswith("ssh-remote+"):
                return REMOTE_SSH
            return "remote_other"
        return "unsupported"

    def _local_paths(
        self, entry: Dict[str, Any]
    ) -> Tuple[Tuple[Path, ...], Optional[str]]:
        uri = entry["uri"]
        try:
            if entry["kind"] == "folder":
                return (_file_uri_to_path(uri),), None
            workspace_file = _file_uri_to_path(uri)
            document = self._read_json_retry(workspace_file)
            if not isinstance(document, dict):
                return (), "workspace_file_unavailable"
            raw_folders = document.get("folders")
            if not isinstance(raw_folders, list) or not raw_folders:
                return (), "workspace_file_malformed"
            paths = []
            for raw_folder in raw_folders:
                if not isinstance(raw_folder, dict):
                    return (), "workspace_file_malformed"
                raw_path = raw_folder.get("path")
                raw_uri = raw_folder.get("uri")
                if isinstance(raw_path, str) and raw_path:
                    path = Path(raw_path)
                    if not path.is_absolute():
                        path = workspace_file.parent / path
                    paths.append(_canonical_local_path(path))
                elif isinstance(raw_uri, str) and raw_uri:
                    paths.append(_file_uri_to_path(raw_uri))
                else:
                    return (), "workspace_file_malformed"
            return tuple(paths), None
        except (OSError, RuntimeError, ValueError):
            return (), "workspace_uri_invalid"

    def _is_excluded(self, uri: str, repo_root: Path) -> bool:
        if not self.exclude:
            return False
        values = {
            uri.casefold(),
            str(repo_root).casefold(),
            repo_root.name.casefold(),
        }
        return any(excluded in values for excluded in self.exclude)

    def _is_remote_excluded(self, uri: str, remote_path: str) -> bool:
        if not self.exclude:
            return False
        values = {
            uri.casefold(),
            remote_path.casefold(),
            PurePosixPath(remote_path).name.casefold(),
        }
        return any(excluded in values for excluded in self.exclude)


class EffectiveWorkspaceCatalog:
    """Merge durable manual overrides with a fresh ephemeral VS Code snapshot."""

    def __init__(
        self,
        runtime: Path,
        *,
        codex_home: Optional[Path] = None,
        registry: Optional[WorkspaceRegistry] = None,
        discovery: Optional[VSCodeWorkspaceDiscovery] = None,
        exclude: Sequence[str] = (),
        atomic_writer: Callable[[Path, Dict[str, Any]], None] = (
            InstructionStore._atomic_json
        ),
    ) -> None:
        self.runtime = Path(runtime)
        self.registry = registry if registry is not None else WorkspaceRegistry(runtime)
        self.discovery = (
            discovery
            if discovery is not None
            else VSCodeWorkspaceDiscovery(
                runtime, codex_home=codex_home, registry=self.registry, exclude=exclude,
            )
        )
        self.atomic_writer = atomic_writer
        self.path = self.runtime / "service" / "workspace-discovery.json"
        self.last_snapshot: Optional[DiscoverySnapshot] = None

    def snapshot(self, *, persist: bool = False) -> DiscoverySnapshot:
        snapshot = self.discovery.snapshot()
        self.last_snapshot = snapshot
        if persist:
            self.atomic_writer(self.path, snapshot.to_dict())
        return snapshot

    def list_workspaces(self) -> List[TrackedWorkspace]:
        return list(self.snapshot(persist=True).effective_workspaces)

    def is_current(self, workspace: TrackedWorkspace) -> bool:
        manual_matches = [
            candidate
            for candidate in self.registry.list_workspaces()
            if candidate.workspace_id == workspace.workspace_id
        ]
        if manual_matches:
            return len(manual_matches) == 1 and manual_matches[0].has_same_registration(
                workspace
            )
        snapshot = self.snapshot(persist=False)
        if snapshot.status == "error":
            return False
        matches = [
            candidate
            for candidate in snapshot.effective_workspaces
            if candidate.workspace_id == workspace.workspace_id
        ]
        return len(matches) == 1 and matches[0].has_same_registration(workspace)
