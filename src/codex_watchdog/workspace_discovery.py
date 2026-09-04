from __future__ import annotations

from dataclasses import dataclass, replace
import ctypes
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
from .storage import InstructionStore
from .workspace_registry import TrackedWorkspace, WorkspaceRegistry


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
            if getattr(exc, "errno", None) in (11, 13):
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
    candidates = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
        )
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(
            Path(program_files) / "Microsoft VS Code" / "bin" / "code.cmd"
        )
    script = next((path for path in candidates if path.is_file()), None)
    command_processor = os.environ.get("COMSPEC")
    if script is None or not command_processor:
        return None
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("CODEX_WATCHDOG_")
    }
    try:
        completed = subprocess.run(
            [command_processor, "/d", "/c", "call", str(script), "--status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return completed.stdout


def codex_log_owns_session(path: Path, session_id: str) -> bool:
    """Parse only privacy-safe stream ownership markers for one exact thread."""

    canonical = _canonical_session_id(session_id)
    if canonical is None or not path.is_file():
        return False
    conversation = re.compile(
        rb"(?:^|\s)conversationId="
        + re.escape(canonical.encode("ascii"))
        + rb"(?=\s|$)"
    )
    role: Optional[str] = None
    try:
        with path.open("rb") as handle:
            for line in handle:
                if b"[CodexMcpConnection] Spawning codex app-server" in line:
                    role = None
                    continue
                if conversation.search(line) is None:
                    continue
                if b"thread_stream_role_changed" in line:
                    role = None
                    match = re.search(rb"(?:^|\s)role=([^\s]+)(?=\s|$)", line)
                    if match is not None:
                        role = match.group(1).decode("ascii", errors="ignore")
                elif b"maybe_resume_success" in line:
                    role = None
                    match = re.search(
                        rb"(?:^|\s)assignedStreamRole=([^\s]+)(?=\s|$)", line
                    )
                    if match is not None:
                        role = match.group(1).decode("ascii", errors="ignore")
                elif b"maybe_resume_failed" in line:
                    role = None
    except OSError:
        return False
    return role == "owner"


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
    ) -> None:
        self.user_data_root = _canonical_local_path(user_data_root)
        self.status_runner = status_runner

    def snapshot(self) -> Optional[Dict[str, LiveVSCodeWindow]]:
        status = self.status_runner()
        if not isinstance(status, str) or not status:
            return None
        processes = self._parse_status(status)
        if not processes:
            return None
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
        for window_number, (extension_host_pid, codex_pid) in processes.items():
            match = self._latest_window_log(
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
            lowered = stripped.casefold()
            if (
                "\\.vscode\\extensions\\openai.chatgpt-" in lowered
                and "\\codex.exe" in lowered
                and " app-server" in lowered
            ):
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
                        storage_keys.update(
                            match.group(1).decode("ascii", errors="ignore")
                            for match in marker.finditer(line)
                        )
                        start = host_marker.search(line)
                        exited = exit_marker.search(line)
                        if start is not None:
                            if int(start.group(1)) == extension_host_pid:
                                target_started = True
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
    ) -> None:
        self.runtime = Path(runtime)
        self.codex_home = _canonical_local_path(Path(codex_home))
        self.lock_probe = lock_probe
        self.owner_probe = owner_probe

    def resolve(
        self,
        paths: Iterable[Path],
        *,
        codex_log: Optional[Path],
        window_state_database: Optional[Path],
    ) -> SessionResolution:
        if codex_log is None:
            return SessionResolution(
                "unresolved", None, None, "vscode_codex_log_unavailable"
            )
        if window_state_database is None:
            return SessionResolution(
                "unresolved", None, None, "vscode_session_cache_unavailable"
            )
        window_sessions = self.window_session_candidates(window_state_database)
        if window_sessions is None:
            return SessionResolution(
                "unresolved", None, None, "vscode_session_cache_unavailable"
            )
        if not window_sessions:
            return SessionResolution("unresolved", None, None, "no_window_codex_thread")
        identities = {_path_identity(path) for path in paths}
        database_candidates, database_status = self._database_candidates(
            identities, codex_log, window_sessions
        )
        if len(database_candidates) == 1:
            return SessionResolution(
                "resolved",
                next(iter(database_candidates)),
                "codex_state_vscode_window_cache_owner",
                None,
            )
        if len(database_candidates) > 1:
            return SessionResolution(
                "unresolved", None, None, "ambiguous_loaded_threads"
            )

        reason = (
            "codex_state_unavailable"
            if database_status == "unavailable"
            else "no_loaded_vscode_thread"
        )
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
        self, identities: set[str], codex_log: Path, window_sessions: set[str],
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
                    if session_id not in window_sessions:
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
                    if self.lock_probe(lock) and self.owner_probe(
                        codex_log, session_id
                    ):
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
        git_root_resolver: GitRootResolver = _default_git_root,
        exclude: Sequence[str] = (),
        sleep: Sleep = time.sleep,
    ) -> None:
        self.runtime = Path(runtime)
        selected_codex_home = (
            Path(codex_home)
            if codex_home is not None
            else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        )
        self.codex_home = _canonical_local_path(selected_codex_home)
        if user_data_root is None:
            appdata = os.environ.get("APPDATA")
            self.user_data_root = (
                _canonical_local_path(Path(appdata) / "Code" / "User")
                if appdata
                else None
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
            else VSCodeLiveWindowIndex(self.user_data_root)
            if self.user_data_root is not None
            else None
        )
        self.git_root_resolver = git_root_resolver
        self.exclude = tuple(item.casefold() for item in exclude if item)
        self.sleep = sleep

    def snapshot(self) -> DiscoverySnapshot:
        discovered_at = utc_now()
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
        storage_keys = self._workspace_storage_keys()
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
                ("vscode_live_status_unavailable",),
            )

        windows: List[DiscoveredWindow] = []
        candidates: List[Tuple[int, TrackedWorkspace]] = []
        issues: List[str] = []
        matched_live_keys = set()
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
                    if not matching_keys
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
        resolution = self.session_resolver.resolve(
            (local_path, repo_root),
            codex_log=codex_log,
            window_state_database=(
                self.user_data_root / "workspaceStorage" / storage_key / "state.vscdb"
            ),
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

    def _read_json_retry(self, path: Path) -> Optional[Any]:
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
