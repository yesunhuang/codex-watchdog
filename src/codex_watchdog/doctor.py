from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from .models import utc_now
from .platform_adapters import (
    HostPlatformAdapter,
    VSCodeStatusProbe,
    detect_platform_adapter,
    run_vscode_status,
)
from .queue_wake import QueueWakeDispatcher, _resolve_codex_executable
from .storage import _replace_atomically
from .workspace_discovery import VSCodeLiveWindowIndex, VSCodeWorkspaceDiscovery
from .workspace_registry import WorkspaceRegistry


DOCTOR_SCHEMA_VERSION = 1
PASS = "PASS"
PARTIAL = "PARTIAL"
FAIL = "FAIL"
_SAFE_REASON = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    reason: str
    details: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class DoctorReport:
    status: str
    platform: str
    architecture: str
    support_tier: str
    checks: Tuple[DoctorCheck, ...]
    generated_at: str
    schema_version: int = DOCTOR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "generated_at": self.generated_at,
            "platform": {
                "name": self.platform,
                "architecture": self.architecture,
                "support_tier": self.support_tier,
            },
            "checks": [check.to_dict() for check in self.checks],
        }


StatusProbe = Callable[[HostPlatformAdapter], VSCodeStatusProbe]


class WatchdogDoctor:
    """Read-only capability audit that emits only bounded, privacy-safe facts."""

    def __init__(
        self,
        runtime: Path,
        *,
        codex_home: Optional[Path] = None,
        user_data_root: Optional[Path] = None,
        adapter: Optional[HostPlatformAdapter] = None,
        status_probe: StatusProbe = run_vscode_status,
        linux_bound: bool = False,
    ) -> None:
        if type(linux_bound) is not bool:
            raise ValueError("linux_bound must be a boolean")
        self.linux_bound = linux_bound
        self.runtime = Path(runtime).expanduser().resolve()
        self.adapter = detect_platform_adapter() if adapter is None else adapter
        self.codex_home = (
            Path(codex_home).expanduser().resolve()
            if codex_home is not None
            else self.adapter.default_codex_home()
        )
        self.user_data_root = (
            Path(user_data_root).expanduser().resolve()
            if user_data_root is not None
            else self.adapter.primary_vscode_user_data_root()
        )
        self.status_probe = status_probe

    def run(self) -> DoctorReport:
        checks = []
        supported = self.adapter.system in ("windows", "linux", "macos")
        checks.append(
            DoctorCheck(
                "platform",
                PASS if supported else FAIL,
                "platform_supported" if supported else "platform_unsupported",
                {
                    "host_kind": self.adapter.system,
                    "architecture": self.adapter.architecture,
                    "support_tier": self.adapter.support_tier,
                },
            )
        )

        if not self.linux_bound:
            status_commands = self.adapter.vscode_status_commands()
            checks.append(
                DoctorCheck(
                    "vscode_cli",
                    PASS if status_commands else FAIL,
                    "vscode_cli_available" if status_commands else "vscode_cli_unavailable",
                    {
                        "candidate_count": len(status_commands),
                        "candidate_sources": sorted({item.source for item in status_commands}),
                    },
                )
            )

            checks.append(self._user_data_check())
            checks.append(self._workspace_storage_check())

        extensions = self._extension_count()
        executable, executable_available, executable_source = self._codex_executable()
        checks.append(
            DoctorCheck(
                "codex_extension",
                PASS if extensions else PARTIAL,
                "codex_extension_available" if extensions else "codex_extension_not_found",
                {"installation_count": extensions},
            )
        )
        checks.append(
            DoctorCheck(
                "codex_executable",
                PASS if executable_available else FAIL,
                "codex_executable_available"
                if executable_available
                else "codex_executable_unavailable",
                {"source": executable_source},
            )
        )
        checks.append(self._codex_home_check())

        if self.linux_bound:
            checks.extend(self._linux_binding_checks())
        else:
            probe = self.status_probe(self.adapter)
            checks.append(
                DoctorCheck(
                    "vscode_live_status",
                    PASS if probe.status == "available" else PARTIAL,
                    probe.reason if _SAFE_REASON.fullmatch(probe.reason) else "vscode_status_unknown",
                    {"source": probe.source or "none"},
                )
            )
            live_index, live_count = self._live_window_index(probe)
            checks.append(
                DoctorCheck(
                    "live_window_discovery",
                    PASS if live_count is not None else PARTIAL,
                    "live_windows_resolved"
                    if live_count is not None
                    else "live_windows_unavailable",
                    {"resolved_window_count": live_count or 0},
                )
            )
            checks.append(self._thread_resolution_check(probe, live_index))
        checks.append(self._queue_check(executable, executable_available))
        checks.append(self._hook_check())

        credential_status = (
            PASS if self.adapter.system in ("windows", "macos") else PARTIAL
        )
        checks.append(
            DoctorCheck(
                "credential_storage",
                credential_status,
                self.adapter.credential_backend,
                {"encrypted_store_required": True},
            )
        )
        launcher_status = (
            PASS if self.adapter.system in ("windows", "macos") else PARTIAL
        )
        checks.append(
            DoctorCheck(
                "launcher",
                launcher_status,
                self.adapter.launcher_mode,
                {"background_service_installed": False},
            )
        )
        checks.append(
            DoctorCheck(
                "filesystem_primitives",
                PASS if supported else FAIL,
                "windows_lock_and_atomic_replace"
                if self.adapter.system == "windows"
                else "posix_flock_and_atomic_replace"
                if self.adapter.system in ("linux", "macos")
                else "filesystem_primitives_unverified",
                {"read_only_probe": True},
            )
        )

        overall = self._overall_status(tuple(checks))
        return DoctorReport(
            overall,
            self.adapter.system,
            self.adapter.architecture,
            self.adapter.support_tier,
            tuple(checks),
            utc_now(),
        )

    def _linux_binding_checks(self) -> Tuple[DoctorCheck, ...]:
        from .linux_binding import LinuxBinding, LinuxBindingError, exact_thread, reservation_path
        from .linux_owner import kernel_lock_owner, vscode_writer, writer_pid

        if self.adapter.system != "linux":
            return (DoctorCheck("linux_binding", FAIL, "linux_required", {}),)
        try:
            binding = LinuxBinding(self.runtime, self.codex_home)
            value = binding.load()
            workspace = binding.workspace(value)
            exact_thread(self.codex_home, workspace)
            expired = binding.status()["lease_expired"]
            binding_check = DoctorCheck(
                "linux_binding",
                PASS if value["state"] == "armed" and not expired else PARTIAL,
                "linux_binding_valid",
                {"binding_state": value["state"], "lease_expired": expired,
                 "explicit_operator_binding": True},
            )
            controller = kernel_lock_owner(
                reservation_path(self.codex_home, workspace.session_id).with_suffix(".owner.lock")
            )
            controller_check = DoctorCheck(
                "linux_controller", PASS if controller is not None else PARTIAL,
                "linux_controller_present" if controller is not None else "linux_controller_not_running",
                {"live_kernel_lock": controller is not None},
            )
            writer = writer_pid(self.codex_home, workspace.session_id)
            if writer is None:
                writer_check = DoctorCheck("linux_writer", PARTIAL, "linux_writer_vacant", {})
            elif vscode_writer(writer):
                writer_check = DoctorCheck("linux_writer", PASS, "linux_vscode_writer_verified", {})
            else:
                process = Path("/proc") / str(writer)
                before = (process / "stat").read_text().rsplit(")", 1)[1].split()
                arguments = (process / "cmdline").read_bytes().split(b"\0")
                after = (process / "stat").read_text().rsplit(")", 1)[1].split()
                owned = (controller is not None and int(before[1]) == controller
                         and before[19] == after[19] and b"app-server" in arguments)
                writer_check = DoctorCheck(
                    "linux_writer", PASS if owned else FAIL,
                    "linux_source_writer_verified" if owned else "linux_conflicting_writer", {},
                )
            return binding_check, controller_check, writer_check
        except (LinuxBindingError, OSError, ValueError, IndexError) as exc:
            reason = str(exc) if isinstance(exc, LinuxBindingError) else "linux_owner_probe_unavailable"
            return (DoctorCheck("linux_binding", FAIL, reason, {}),)

    def _user_data_check(self) -> DoctorCheck:
        if self.user_data_root is None:
            return DoctorCheck(
                "vscode_user_data",
                FAIL,
                "vscode_user_data_unavailable",
                {"source": "none", "window_state_readable": False},
            )
        state = self.user_data_root / "globalStorage" / "storage.json"
        root_readable = _directory_readable(self.user_data_root)
        state_readable = _file_readable(state)
        status = PASS if root_readable and state_readable else FAIL
        reason = (
            "vscode_user_data_readable"
            if status == PASS
            else "vscode_window_state_unavailable"
        )
        return DoctorCheck(
            "vscode_user_data",
            status,
            reason,
            {
                "source": "configured_or_platform_default",
                "window_state_readable": state_readable,
            },
        )

    def _workspace_storage_check(self) -> DoctorCheck:
        if self.user_data_root is None:
            return DoctorCheck(
                "workspace_storage",
                FAIL,
                "workspace_storage_unavailable",
                {"entry_count": 0, "database_count": 0},
            )
        root = self.user_data_root / "workspaceStorage"
        try:
            entries = tuple(path for path in root.iterdir() if path.is_dir())
        except OSError:
            return DoctorCheck(
                "workspace_storage",
                FAIL,
                "workspace_storage_unreadable",
                {"entry_count": 0, "database_count": 0},
            )
        databases = sum(_file_readable(path / "state.vscdb") for path in entries)
        return DoctorCheck(
            "workspace_storage",
            PASS,
            "workspace_storage_readable",
            {"entry_count": len(entries), "database_count": databases},
        )

    def _extension_count(self) -> int:
        count = 0
        for root in self.adapter.codex_extension_roots():
            try:
                count += sum(path.is_dir() for path in root.glob("openai.chatgpt-*"))
            except OSError:
                continue
        return count

    def _codex_executable(self) -> Tuple[str, bool, str]:
        resolved = _resolve_codex_executable(
            home=self.adapter.home,
            which=self.adapter.which,
            platform_name=self.adapter.system,
        )
        path = Path(resolved)
        if path.is_file():
            source = "path" if self.adapter.which("codex") else "extension"
            executable = os.access(path, os.X_OK) if self.adapter.system != "windows" else True
            return resolved, executable, source
        return resolved, False, "none"

    def _codex_home_check(self) -> DoctorCheck:
        root_readable = _directory_readable(self.codex_home)
        state_readable = _file_readable(self.codex_home / "state_5.sqlite")
        sessions_readable = _directory_readable(self.codex_home / "sessions")
        status = PASS if root_readable and (state_readable or sessions_readable) else FAIL
        return DoctorCheck(
            "codex_home",
            status,
            "codex_state_available" if status == PASS else "codex_state_unavailable",
            {
                "state_database_readable": state_readable,
                "sessions_readable": sessions_readable,
            },
        )

    def _live_window_index(
        self, probe: VSCodeStatusProbe
    ) -> Tuple[Optional[VSCodeLiveWindowIndex], Optional[int]]:
        if self.user_data_root is None or probe.output is None:
            return None, None
        index = VSCodeLiveWindowIndex(
            self.user_data_root, status_runner=lambda: probe.output
        )
        windows = index.snapshot()
        return index, len(windows) if windows is not None else None

    def _thread_resolution_check(
        self,
        probe: VSCodeStatusProbe,
        live_index: Optional[VSCodeLiveWindowIndex],
    ) -> DoctorCheck:
        if self.user_data_root is None or probe.output is None or live_index is None:
            return DoctorCheck(
                "current_thread_resolution",
                PARTIAL,
                "current_thread_probe_unavailable",
                {
                    "window_count": 0,
                    "tracked_count": 0,
                    "remote_adapter_count": 0,
                    "issue_codes": [],
                },
            )
        try:
            snapshot = VSCodeWorkspaceDiscovery(
                self.runtime,
                codex_home=self.codex_home,
                user_data_root=self.user_data_root,
                registry=_ReadOnlyWorkspaceRegistry(self.runtime),
                live_window_index=live_index,
                platform_adapter=self.adapter,
            ).snapshot()
        except Exception:
            return DoctorCheck(
                "current_thread_resolution",
                FAIL,
                "current_thread_probe_failed",
                {
                    "window_count": 0,
                    "tracked_count": 0,
                    "remote_adapter_count": 0,
                    "issue_codes": ["current_thread_probe_failed"],
                },
            )
        remote_count = sum(
            window.tracking_status == "remote_adapter" for window in snapshot.windows
        )
        tracked_count = len(snapshot.effective_workspaces)
        status = PASS if tracked_count or remote_count else PARTIAL
        issues = sorted(
            {
                issue if _SAFE_REASON.fullmatch(issue) else "noncanonical_issue"
                for issue in snapshot.issues
            }
        )
        return DoctorCheck(
            "current_thread_resolution",
            status,
            "current_thread_resolved" if status == PASS else "no_current_thread_resolved",
            {
                "discovery_status": snapshot.status,
                "window_count": len(snapshot.windows),
                "tracked_count": tracked_count,
                "remote_adapter_count": remote_count,
                "issue_codes": issues,
            },
        )

    def _queue_check(
        self, executable: str, executable_available: bool
    ) -> DoctorCheck:
        compatible = 0
        selected = None
        try:
            databases = tuple(self.codex_home.glob("queue_*.sqlite"))
            compatible = sum(
                QueueWakeDispatcher._compatible_queue_database(path)
                for path in databases
            )
            if executable_available:
                selected = QueueWakeDispatcher(
                    self.runtime,
                    codex_executable=executable,
                    codex_home=self.codex_home,
                )._select_queue_database()
        except (OSError, ValueError):
            selected = None
        if not executable_available:
            status = FAIL
            reason = "queue_cli_unavailable"
        elif selected is None:
            status = PARTIAL
            reason = "queue_database_unavailable_or_ambiguous"
        else:
            status = PASS
            reason = "queue_wake_ready"
        return DoctorCheck(
            "queue_wake",
            status,
            reason,
            {
                "compatible_database_count": compatible,
                "selected_database": selected is not None,
                "dispatch_attempted": False,
            },
        )

    def _hook_check(self) -> DoctorCheck:
        path = self.codex_home / "hooks.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            strings = tuple(_iter_strings(document))
        except (OSError, UnicodeError, json.JSONDecodeError):
            strings = ()
        count = sum("codex-watchdog" in value.casefold() for value in strings)
        return DoctorCheck(
            "codex_hooks",
            PASS if count else PARTIAL,
            "watchdog_hooks_present" if count else "watchdog_hooks_not_detected",
            {"matching_definition_count": count},
        )

    @staticmethod
    def _overall_status(checks: Tuple[DoctorCheck, ...]) -> str:
        if any(check.status == FAIL for check in checks):
            return FAIL
        if any(check.status == PARTIAL for check in checks):
            return PARTIAL
        return PASS


def _directory_readable(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
        next(path.iterdir(), None)
        return True
    except OSError:
        return False


def _file_readable(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        with path.open("rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _iter_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_strings(nested)


class _ReadOnlyWorkspaceRegistry:
    """Read an atomic registry snapshot without creating a diagnostic lock."""

    def __init__(self, runtime: Path) -> None:
        self._registry = WorkspaceRegistry(runtime)

    def list_workspaces(self):
        return self._registry._read_unlocked()


def write_doctor_export(path: Path, report: DoctorReport) -> None:
    """Atomically write the already-redacted report with user-only permissions."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                report.to_dict(), stream, ensure_ascii=False, sort_keys=True, indent=2
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _replace_atomically(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
