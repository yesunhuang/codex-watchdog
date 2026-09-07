from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform as platform_module
import re
import shutil
import subprocess
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
        if self.system in ("linux", "macos"):
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
            return "macos_keychain_required"
        if self.system == "linux":
            return "linux_libsecret_required"
        return "encrypted_persistence_unverified"

    @property
    def launcher_mode(self) -> str:
        return (
            "windows_one_click_foreground"
            if self.system == "windows"
            else "foreground_cli_only"
        )

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
