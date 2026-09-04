"""One-click Windows bootstrap with non-destructive upgrade discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, TextIO, Tuple

from . import __version__


LAUNCHER_PROFILE_SCHEMA_VERSION = 1
LAUNCHER_PROFILE_NAME = "launcher-profile.json"
_RUNTIME_ARGUMENT = re.compile(
    r"(?:^|\s)--runtime\s+(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
    re.IGNORECASE,
)
_RELEASE_DIRECTORY = re.compile(
    r"^codex-watchdog-v(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?-windows-x64$",
    re.IGNORECASE,
)
_WATCHDOG_HOOK_MARKERS = (
    "codex-watchdog.exe",
    "codex_watchdog_hook.py",
    "codex-watchdog hook",
)


class WindowsLauncherError(RuntimeError):
    """The one-click bootstrap cannot make a safe deterministic choice."""


@dataclass(frozen=True)
class LauncherResolution:
    runtime: Path
    profile_path: Path
    source: str


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _config_root(environment: Mapping[str, str]) -> Path:
    local_app_data = environment.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise WindowsLauncherError(
            "LOCALAPPDATA is unavailable; the current-user WatchDog profile "
            "cannot be resolved."
        )
    return _absolute(Path(local_app_data) / "CodexWatchdog")


def _codex_home(environment: Mapping[str, str]) -> Optional[Path]:
    configured = environment.get("CODEX_HOME", "").strip()
    if configured:
        return _absolute(Path(configured))
    user_profile = environment.get("USERPROFILE", "").strip()
    if user_profile:
        return _absolute(Path(user_profile) / ".codex")
    return None


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _iter_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_strings(nested)


def runtimes_from_hooks(path: Path) -> Tuple[Path, ...]:
    """Return unique absolute runtimes from recognizable WatchDog hook commands."""
    if not path.is_file():
        return ()
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WindowsLauncherError(
            f"The existing Codex hook file cannot be read safely: {path}"
        ) from exc
    discovered: Dict[str, Path] = {}
    for value in _iter_strings(document):
        folded = value.casefold()
        if not any(marker in folded for marker in _WATCHDOG_HOOK_MARKERS):
            continue
        match = _RUNTIME_ARGUMENT.search(value)
        if match is None:
            continue
        raw = next(group for group in match.groups() if group is not None)
        runtime = _absolute(Path(raw))
        discovered[os.path.normcase(str(runtime))] = runtime
    return tuple(discovered[key] for key in sorted(discovered))


def _read_profile(path: Path) -> Path:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WindowsLauncherError(
            f"The saved launcher profile is unreadable; it was left unchanged: {path}"
        ) from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise WindowsLauncherError(
            f"The saved launcher profile has an unsupported schema and was left unchanged: {path}"
        )
    configured = value.get("runtime_path")
    if not isinstance(configured, str) or not configured.strip():
        raise WindowsLauncherError(
            f"The saved launcher profile has no valid runtime path and was left unchanged: {path}"
        )
    runtime = _absolute(Path(configured))
    if not runtime.is_dir():
        raise WindowsLauncherError(
            "The saved WatchDog runtime no longer exists. Restore it or edit the "
            f"profile explicitly; no replacement runtime was created: {runtime}"
        )
    return runtime


def _write_profile(path: Path, runtime: Path, source: str) -> None:
    """Create the first versioned profile atomically; never overwrite one implicitly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = _read_profile(path)
        if os.path.normcase(str(existing)) != os.path.normcase(str(runtime)):
            raise WindowsLauncherError(
                "The saved launcher profile changed during startup and was left unchanged."
            )
        return
    payload = {
        "schema_version": LAUNCHER_PROFILE_SCHEMA_VERSION,
        "runtime_path": str(runtime),
        "discovered_from": source,
        "created_by_version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _previous_release_runtime(executable: Path) -> Optional[Path]:
    candidates = []
    parent = executable.parent.parent
    if not parent.is_dir():
        return None
    try:
        children = tuple(parent.iterdir())
    except OSError:
        return None
    for child in children:
        match = _RELEASE_DIRECTORY.fullmatch(child.name)
        runtime = child / ".codex-watchdog"
        if match is not None and child != executable.parent and runtime.is_dir():
            candidates.append((tuple(int(part) for part in match.groups()), runtime))
    if not candidates:
        return None
    highest = max(version for version, _ in candidates)
    matching = sorted(
        (_absolute(runtime) for version, runtime in candidates if version == highest),
        key=lambda item: os.path.normcase(str(item)),
    )
    if len(matching) != 1:
        raise WindowsLauncherError(
            "Multiple previous release runtimes have the same newest version; "
            "WatchDog will not guess which state to use."
        )
    return matching[0]


def prepare_one_click_launch(
    executable: Path, environment: Optional[Mapping[str, str]] = None
) -> LauncherResolution:
    """Resolve and persist one compatible current-user runtime without secrets."""
    source_environment = dict(os.environ if environment is None else environment)
    executable = _absolute(executable)
    root = _config_root(source_environment)
    profile_path = root / LAUNCHER_PROFILE_NAME
    if profile_path.exists():
        return LauncherResolution(_read_profile(profile_path), profile_path, "saved_profile")

    configured_runtime = source_environment.get("CODEX_WATCHDOG_RUNTIME", "").strip()
    if configured_runtime:
        runtime = _absolute(Path(configured_runtime))
        source = "environment"
    else:
        codex_home = _codex_home(source_environment)
        hook_runtimes = (
            runtimes_from_hooks(codex_home / "hooks.json")
            if codex_home is not None
            else ()
        )
        if len(hook_runtimes) > 1:
            raise WindowsLauncherError(
                "The installed WatchDog hooks reference multiple runtimes; no "
                "launcher profile was written."
            )
        if hook_runtimes:
            runtime = hook_runtimes[0]
            if not runtime.is_dir():
                raise WindowsLauncherError(
                    "The runtime referenced by the installed Codex hooks is missing; "
                    f"no new state was created: {runtime}"
                )
            source = "codex_hooks"
        else:
            adjacent = executable.parent / ".codex-watchdog"
            previous = _previous_release_runtime(executable)
            if adjacent.is_dir():
                runtime = _absolute(adjacent)
                source = "adjacent_runtime"
            elif previous is not None:
                runtime = previous
                source = "previous_release"
            else:
                runtime = root / "runtime"
                source = "new_current_user_runtime"

    _write_profile(profile_path, runtime, source)
    return LauncherResolution(runtime, profile_path, source)


def packaged_cli_arguments(
    arguments: Sequence[str],
    executable: Path,
    environment: Optional[Mapping[str, str]] = None,
) -> Tuple[str, ...]:
    """Apply the saved runtime to packaged CLI commands unless explicitly set."""
    values = tuple(arguments)
    if any(value == "--runtime" or value.startswith("--runtime=") for value in values):
        return values
    if any(value in ("--help", "-h", "--version") for value in values):
        return values
    resolution = prepare_one_click_launch(executable, environment)
    return ("--runtime", str(resolution.runtime), *values)


def _powershell_path(environment: Mapping[str, str]) -> Path:
    system_root = environment.get("SystemRoot", environment.get("SYSTEMROOT", "")).strip()
    if system_root:
        candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return _absolute(candidate)
    discovered = shutil.which("powershell.exe", path=environment.get("PATH"))
    if discovered:
        return _absolute(Path(discovered))
    raise WindowsLauncherError("Windows PowerShell is unavailable; WatchDog cannot start.")


def launch_one_click(
    executable: Path,
    *,
    environment: Optional[Mapping[str, str]] = None,
    runner: Optional[Callable[..., int]] = None,
    stdout: TextIO = sys.stdout,
) -> int:
    source_environment = dict(os.environ if environment is None else environment)
    executable = _absolute(executable)
    launcher = executable.parent / "watchdog.ps1"
    if not launcher.is_file():
        raise WindowsLauncherError(
            f"The packaged bootstrap is missing beside the executable: {launcher}"
        )
    resolution = prepare_one_click_launch(executable, source_environment)
    command = [
        str(_powershell_path(source_environment)),
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(launcher),
        "-Runtime",
        str(resolution.runtime),
    ]
    if source_environment.get("CODEX_WATCHDOG_PACKAGE_TEST_ONCE") == "1":
        command.extend(("-Once", "-ManualOnly"))
    print(
        f"Starting Codex WatchDog with runtime: {resolution.runtime}",
        file=stdout,
        flush=True,
    )
    if resolution.source != "saved_profile":
        print(
            f"Saved launcher profile ({resolution.source}): {resolution.profile_path}",
            file=stdout,
            flush=True,
        )
    invoke = subprocess.call if runner is None else runner
    child_environment = dict(source_environment)
    # The one-file parent launches PowerShell, which launches the same packaged
    # executable with explicit CLI arguments. Tell PyInstaller that this is a
    # new top-level application instance instead of a bundle worker process.
    child_environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return int(invoke(command, cwd=str(executable.parent), env=child_environment))


def _record_startup_error(environment: Mapping[str, str], message: str) -> Optional[Path]:
    try:
        path = _config_root(environment) / "launcher-last-error.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            + " "
            + message
            + "\n",
            encoding="utf-8",
        )
        return path
    except OSError:
        return None


def one_click_main(
    executable: Optional[Path] = None,
    *,
    environment: Optional[Mapping[str, str]] = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    source_environment = dict(os.environ if environment is None else environment)
    selected_executable = Path(sys.executable) if executable is None else executable
    try:
        exit_code = launch_one_click(
            selected_executable, environment=source_environment, stdout=stdout
        )
        if exit_code in (0, 130):
            return 0
        message = f"Codex WatchDog exited with code {exit_code}."
    except (OSError, ValueError, WindowsLauncherError) as exc:
        message = f"Codex WatchDog could not start: {exc}"
    error_path = _record_startup_error(source_environment, message)
    print(message, file=stderr, flush=True)
    if error_path is not None:
        print(f"Startup error record: {error_path}", file=stderr, flush=True)
    if stdin.isatty() and source_environment.get("CODEX_WATCHDOG_PACKAGE_TEST_ONCE") != "1":
        try:
            input("Press Enter to close... ")
        except (EOFError, KeyboardInterrupt):
            pass
    return 1
