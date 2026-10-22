from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform as platform_module
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Mapping, Optional, Tuple


Which = Callable[[str], Optional[str]]
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class VSCodeStatusCommand:
    argv: Tuple[str, ...]
    source: str


@dataclass(frozen=True)
class VSCodeStatusProbe:
    status: str
    reason: str
    source: Optional[str] = None
    output: Optional[str] = None


def _canonical_architecture(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_")
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    return aliases.get(normalized, normalized or "unknown")


def _canonical_system(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized in ("nt", "windows", "win32"):
        return "windows"
    if normalized in ("darwin", "mac", "macos", "osx"):
        return "macos"
    if normalized in ("linux", "posix"):
        return "linux"
    return normalized or "unknown"


@dataclass(frozen=True)
class HostPlatformAdapter:
    """Small host adapter for paths and process invocation, not service policy."""

    system: str
    architecture: str
    home: Path
    environment: Mapping[str, str]
    which: Which

    @property
    def support_tier(self) -> str:
        if self.system == "windows":
            return "stable_full_e2e"
        if self.system == "macos":
            return "preview_native_e2e"
        if self.system == "linux":
            return "preview_ci"
        return "unsupported"

    @property
    def codex_executable_name(self) -> str:
        return "codex.exe" if self.system == "windows" else "codex"

    @property
    def credential_backend(self) -> str:
        if self.system == "windows":
            return "windows_dpapi"
        if self.system == "macos":
            return "macos_keychain_slack"
        if self.system == "linux":
            return "linux_libsecret_required"
        return "encrypted_persistence_unverified"

    @property
    def launcher_mode(self) -> str:
        if self.system == "windows":
            return "windows_one_click_foreground"
        if self.system == "macos":
            return "macos_keychain_foreground"
        return "foreground_cli_only"

    def default_codex_home(self) -> Path:
        configured = self.environment.get("CODEX_HOME", "").strip()
        return _absolute(Path(configured) if configured else self.home / ".codex")

    def application_data_root(self) -> Optional[Path]:
        if self.system == "windows":
            configured = self.environment.get("LOCALAPPDATA", "").strip()
            return _absolute(Path(configured) / "CodexWatchdog") if configured else None
        if self.system == "macos":
            return _absolute(
                self.home / "Library" / "Application Support" / "CodexWatchdog"
            )
        if self.system == "linux":
            configured = self.environment.get("XDG_DATA_HOME", "").strip()
            root = Path(configured) if configured else self.home / ".local" / "share"
            return _absolute(root / "codex-watchdog")
        return None

    def vscode_user_data_roots(self) -> Tuple[Path, ...]:
        if self.system == "windows":
            appdata = self.environment.get("APPDATA", "").strip()
            if not appdata:
                return ()
            root = Path(appdata)
        elif self.system == "macos":
            root = self.home / "Library" / "Application Support"
        elif self.system == "linux":
            configured = self.environment.get("XDG_CONFIG_HOME", "").strip()
            root = Path(configured) if configured else self.home / ".config"
        else:
            return ()
        return (
            _absolute(root / "Code" / "User"),
            _absolute(root / "Code - Insiders" / "User"),
        )

    def primary_vscode_user_data_root(self) -> Optional[Path]:
        roots = self.vscode_user_data_roots()
        return roots[0] if roots else None

    def codex_extension_roots(self) -> Tuple[Path, ...]:
        return tuple(
            _absolute(self.home / name / "extensions")
            for name in (
                ".vscode",
                ".vscode-insiders",
                ".vscode-server",
                ".vscode-server-insiders",
            )
        )

    def vscode_status_commands(self) -> Tuple[VSCodeStatusCommand, ...]:
        candidates = []
        if self.system == "windows":
            local_app_data = self.environment.get("LOCALAPPDATA", "").strip()
            program_files = self.environment.get("ProgramFiles", "").strip()
            program_files_x86 = self.environment.get("ProgramFiles(x86)", "").strip()
            if local_app_data:
                candidates.append(
                    (
                        Path(local_app_data)
                        / "Programs"
                        / "Microsoft VS Code"
                        / "bin"
                        / "code.cmd",
                        "windows_user_install",
                    )
                )
            if program_files:
                candidates.append(
                    (
                        Path(program_files)
                        / "Microsoft VS Code"
                        / "bin"
                        / "code.cmd",
                        "windows_machine_install",
                    )
                )
            if program_files_x86:
                candidates.append(
                    (
                        Path(program_files_x86)
                        / "Microsoft VS Code"
                        / "bin"
                        / "code.cmd",
                        "windows_machine_x86_install",
                    )
                )
        elif self.system == "macos":
            candidates.extend(
                (
                    (Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"), "macos_application"),
                    (self.home / "Applications/Visual Studio Code.app/Contents/Resources/app/bin/code", "macos_user_application"),
                )
            )
        elif self.system == "linux":
            candidates.extend(
                (
                    (Path("/usr/bin/code"), "linux_usr_bin"),
                    (Path("/usr/local/bin/code"), "linux_usr_local"),
                    (Path("/snap/bin/code"), "linux_snap"),
                )
            )

        located = self.which("code")
        if located:
            candidates.append((Path(located), "path"))

        commands = []
        seen = set()
        for candidate, source in candidates:
            path = _absolute(candidate)
            identity = os.path.normcase(str(path))
            if identity in seen or not path.is_file():
                continue
            seen.add(identity)
            if self.system == "windows" and path.suffix.casefold() in (".cmd", ".bat"):
                command_processor = self.environment.get("COMSPEC", "").strip()
                if not command_processor:
                    continue
                argv = (
                    str(_absolute(Path(command_processor))),
                    "/d",
                    "/c",
                    "call",
                    str(path),
                    "--status",
                )
            else:
                argv = (str(path), "--status")
            commands.append(VSCodeStatusCommand(argv, source))
        return tuple(commands)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def detect_platform_adapter(
    *,
    system_name: Optional[str] = None,
    machine: Optional[str] = None,
    home: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
    which: Optional[Which] = None,
) -> HostPlatformAdapter:
    source = dict(os.environ if environment is None else environment)
    system = _canonical_system(
        platform_module.system() if system_name is None else system_name
    )
    architecture = _canonical_architecture(
        platform_module.machine() if machine is None else machine
    )
    if home is None:
        configured_home = (
            source.get("USERPROFILE", "").strip()
            if system == "windows"
            else source.get("HOME", "").strip()
        )
        selected_home = Path(configured_home) if configured_home else Path.home()
    else:
        selected_home = Path(home)
    return HostPlatformAdapter(
        system=system,
        architecture=architecture,
        home=_absolute(selected_home),
        environment=source,
        which=shutil.which if which is None else which,
    )


def run_vscode_status(
    adapter: Optional[HostPlatformAdapter] = None,
    *,
    runner: Runner = subprocess.run,
    timeout_seconds: float = 30.0,
) -> VSCodeStatusProbe:
    selected = detect_platform_adapter() if adapter is None else adapter
    commands = selected.vscode_status_commands()
    if not commands:
        return VSCodeStatusProbe("unavailable", "vscode_cli_unavailable")
    environment = {
        key: value
        for key, value in selected.environment.items()
        if not key.upper().startswith("CODEX_WATCHDOG_")
    }
    if selected.system == "windows":
        return _run_windows_vscode_status(commands, environment, runner, timeout_seconds)
    for candidate in commands:
        try:
            completed = runner(
                list(candidate.argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0 and completed.stdout:
            return VSCodeStatusProbe(
                "available",
                "vscode_status_available",
                source=candidate.source,
                output=completed.stdout,
            )
    return VSCodeStatusProbe("error", "vscode_status_failed")


_WINDOWS_STATUS_BYTES = 4 * 1024 * 1024
_WINDOWS_STDERR_BYTES = 65536
_WINDOWS_PIPE_CHUNK = 65536
_WINDOWS_STATUS_HEADER = ("CPU %", "Mem MB", "PID", "Process")
_WINDOWS_FORMATTER_FAILURES = (
    "maximum call stack size exceeded", "too much recursion", "rangeerror",
)
_WINDOWS_STATUS_HOST = re.compile(r"extension-host \[[0-9]+\]")


def _classify_windows_status(
    completed: subprocess.CompletedProcess[str], source: str,
) -> VSCodeStatusProbe:
    """Return compact safe reasons; never retain raw stderr or environment."""
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    stderr = completed.stderr if isinstance(completed.stderr, str) else ""
    if len(stdout) > _WINDOWS_STATUS_BYTES:
        return VSCodeStatusProbe("error", "vscode_status_oversized", source)
    diagnostic = (stdout + "\n" + stderr[:65536]).casefold()
    if any(marker in diagnostic for marker in _WINDOWS_FORMATTER_FAILURES):
        return VSCodeStatusProbe("error", "vscode_status_formatter_failed", source)
    if completed.returncode != 0:
        return VSCodeStatusProbe("error", "vscode_status_failed", source)
    if not stdout.strip():
        return VSCodeStatusProbe("error", "vscode_status_empty", source)
    header = rows = hosts = False
    for line in stdout.splitlines():
        fields = line.split("\t", 3)
        if tuple(field.strip() for field in fields) == _WINDOWS_STATUS_HEADER:
            header = True
        elif len(fields) == 4 and fields[2].strip().isdigit():
            rows = True
            hosts = hosts or _WINDOWS_STATUS_HOST.fullmatch(fields[3].rstrip()) is not None
    if not header and not rows:
        return VSCodeStatusProbe("error", "vscode_status_malformed", source)
    return VSCodeStatusProbe(
        "available",
        "vscode_status_available" if hosts else "vscode_status_no_extension_hosts",
        source=source,
        output=stdout,
    )


_WINDOWS_PROBE_LOCK = threading.Lock()
_WINDOWS_PROBE_READERS = ()


class _StatusProbePending(subprocess.SubprocessError):
    """One previous owned probe still has inherited pipe readers draining."""


class _StatusOutputExceeded(subprocess.SubprocessError):
    """The diagnostic exceeded its byte budget while it was being read."""


def _drain_pipe(stream, keep: int, hard: int, state: dict) -> None:
    """Retain at most ``keep`` bytes; stop reading once ``hard`` is exceeded."""
    kept = []
    size = total = 0
    try:
        while True:
            chunk = stream.read(_WINDOWS_PIPE_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if size < keep:
                kept.append(chunk[:keep - size])
                size += len(kept[-1])
            if total > hard:
                state["exceeded"] = True
                break
    except (OSError, ValueError):
        pass
    state["data"] = b"".join(kept)
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _decode_status(data: bytes) -> str:
    # Match text=True universal-newline decoding of the previous runner.
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _stop_owned_child(process) -> None:
    """Terminate only the exact child handle this probe created; never a tree."""
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass
    try:
        process.wait(0.5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _run_owned_windows_probe(
    argv: Tuple[str, ...],
    *,
    env: Mapping[str, str],
    timeout: float,
    popen=None,
    clock: Callable[[], float] = time.monotonic,
) -> subprocess.CompletedProcess[str]:
    """Collect ``--status`` with byte and time budgets enforced while reading.

    Output is never buffered beyond its budget. On overflow or timeout only the
    owned child handle is terminated. A grandchild that inherited the pipe is
    not signalled: an idle pipe is closed (broken pipe for its writer), while a
    pipe with a still-blocked reader is left to its daemon thread.
    """
    global _WINDOWS_PROBE_READERS
    deadline = clock() + timeout
    process = (subprocess.Popen if popen is None else popen)(
        list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0, env=dict(env),
    )
    states: Tuple[dict, dict] = ({}, {})
    readers = (
        threading.Thread(target=_drain_pipe, daemon=True, args=(
            process.stdout, _WINDOWS_STATUS_BYTES, _WINDOWS_STATUS_BYTES, states[0])),
        threading.Thread(target=_drain_pipe, daemon=True, args=(
            process.stderr, _WINDOWS_STDERR_BYTES, _WINDOWS_STATUS_BYTES, states[1])),
    )
    _WINDOWS_PROBE_READERS = readers
    for reader in readers:
        reader.start()
    failure: Optional[subprocess.SubprocessError] = None
    returncode = None
    try:
        while True:
            if any(state.get("exceeded") for state in states):
                failure = _StatusOutputExceeded()
                break
            alive = [reader for reader in readers if reader.is_alive()]
            if not alive:
                break
            remaining = deadline - clock()
            if remaining <= 0:
                failure = subprocess.TimeoutExpired(list(argv), timeout)
                break
            alive[0].join(min(remaining, 0.05))
        if failure is None:
            try:
                returncode = process.wait(max(0.0, deadline - clock()))
            except subprocess.TimeoutExpired:
                failure = subprocess.TimeoutExpired(list(argv), timeout)
    finally:
        if failure is not None:
            _stop_owned_child(process)
        for stream, reader in zip((process.stdout, process.stderr), readers):
            if not reader.is_alive():
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
    if failure is not None:
        raise failure
    return subprocess.CompletedProcess(
        list(argv), returncode, _decode_status(states[0].get("data", b"")),
        _decode_status(states[1].get("data", b"")),
    )


def _bounded_windows_run(argv, *, env, timeout, popen=None, clock=time.monotonic):
    # At most one owned diagnostic and two pipe readers per process, even if a
    # descendant retains a pipe after its exact owned wrapper has been killed.
    # Never kill an ancestry tree to recover a diagnostic: PID edges may be stale.
    if not _WINDOWS_PROBE_LOCK.acquire(blocking=False):
        raise _StatusProbePending()
    try:
        if any(reader.is_alive() for reader in _WINDOWS_PROBE_READERS):
            raise _StatusProbePending()
        return _run_owned_windows_probe(argv, env=env, timeout=timeout, popen=popen, clock=clock)
    finally:
        _WINDOWS_PROBE_LOCK.release()


def _run_windows_vscode_status(
    commands: Tuple[VSCodeStatusCommand, ...],
    environment: Mapping[str, str],
    runner: Runner,
    timeout_seconds: float,
) -> VSCodeStatusProbe:
    failure = VSCodeStatusProbe("error", "vscode_status_failed")
    # The default runner would buffer unbounded output before any size check.
    bounded = runner is subprocess.run
    for candidate in commands:
        try:
            if bounded:
                completed = _bounded_windows_run(
                    candidate.argv, env=environment, timeout=timeout_seconds,
                )
            else:
                completed = runner(
                    list(candidate.argv),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                    env=environment,
                )
        except _StatusProbePending:
            return VSCodeStatusProbe("error", "vscode_status_probe_pending", candidate.source)
        except subprocess.TimeoutExpired:
            # A hung diagnostic is not retried or re-run verbosely in the hot path.
            return VSCodeStatusProbe("error", "vscode_status_timeout", candidate.source)
        except _StatusOutputExceeded:
            if failure.reason == "vscode_status_failed":
                failure = VSCodeStatusProbe("error", "vscode_status_oversized", candidate.source)
            continue
        except (OSError, subprocess.SubprocessError):
            continue
        probe = _classify_windows_status(completed, candidate.source)
        if probe.status == "available":
            return probe
        if failure.reason == "vscode_status_failed":
            failure = probe
    return failure


_CODEX_BINARY = re.compile(r"(?:^|/)codex(?:\.exe)?(?:\s|$)", re.IGNORECASE)
_APP_SERVER = re.compile(r"(?:^|\s)app-server(?:\s|$)", re.IGNORECASE)


def is_codex_app_server_description(description: str) -> bool:
    """Recognize VS Code's Codex child on Windows and POSIX status output."""

    normalized = description.strip().replace("\\", "/")
    lowered = normalized.casefold()
    return (
        "/extensions/openai.chatgpt-" in lowered
        and _CODEX_BINARY.search(normalized) is not None
        and _APP_SERVER.search(normalized) is not None
    )
