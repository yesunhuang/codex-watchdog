"""Small package adapter; the accepted Bash launcher still owns Keychain loading."""

from __future__ import annotations

import argparse
import copy
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Optional, Sequence

from . import __version__
from .stop_hook import HookSettings
from .storage import FileLock, InstructionStore, StoreBusyError


PACKAGE_FILES = ("codex-watchdog", "watchdog-macos.sh", "setup-slack-relay-macos.sh")
PROFILE_NAME = "macos-launcher.json"


class PackageError(RuntimeError):
    """Bounded reason codes, without paths, provider values, or subprocess output."""


def config_directory() -> Path:
    return Path(os.environ.get(
        "CODEX_WATCHDOG_MACOS_CONFIG_DIR",
        str(Path.home() / "Library/Application Support/CodexWatchdog"),
    )).expanduser().resolve()


def codex_directory() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()


def read_json(path: Path) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise PackageError("macos_config_too_large")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise PackageError("macos_config_unreadable") from exc
    if not isinstance(value, dict):
        raise PackageError("macos_config_unreadable")
    return value


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_profile(directory: Path) -> Optional[dict]:
    path = directory / PROFILE_NAME
    if not path.exists():
        return None
    value = read_json(path)
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("runtime"), str)
            or not Path(value["runtime"]).is_absolute()
            or not isinstance(value.get("install_dir"), str)
            or not Path(value["install_dir"]).is_absolute()
            or not isinstance(value.get("files"), dict)
            or set(value["files"]) != set(PACKAGE_FILES)
            or any(not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
                   for v in value["files"].values())):
        raise PackageError("macos_profile_unsupported")
    return value


def own_hook_arguments(definition: dict) -> Optional[list]:
    """Recognize only the shipped source or packaged hook command grammar."""
    command = definition.get("command")
    if not isinstance(command, str):
        return None
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise PackageError("macos_hook_command_unreadable") from exc
    prefix = (1 if parts and Path(parts[0]).name == "codex-watchdog" else
              2 if len(parts) > 1 and Path(parts[1]).name == "codex_watchdog_hook.py" else 0)
    if not prefix:
        return None
    tail = parts[prefix:]
    if (definition.get("type") != "command" or len(tail) < 3
            or tail[0] != "--runtime" or not Path(tail[1]).is_absolute() or tail[2] != "hook"):
        raise PackageError("macos_hook_command_unsupported")
    options = tail[3:]
    if len(options) % 2 or any(v not in ("--grace-seconds", "--poll-seconds") for v in options[::2]):
        raise PackageError("macos_hook_command_unsupported")
    if len(set(options[::2])) != len(options[::2]):
        raise PackageError("macos_hook_command_unsupported")
    settings = dict(zip(options[::2], options[1::2]))
    try:
        HookSettings(Path(tail[1]), float(settings.get("--grace-seconds", 30)),
                     float(settings.get("--poll-seconds", 0.1))).validate()
    except ValueError as exc:
        raise PackageError("macos_hook_command_unsupported") from exc
    return tail


def own_hooks(document: dict) -> list:
    result = []
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        raise PackageError("macos_hooks_unsupported")
    for event in ("Stop", "PermissionRequest"):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise PackageError("macos_hooks_unsupported")
        matches = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise PackageError("macos_hooks_unsupported")
            for definition in group["hooks"]:
                if not isinstance(definition, dict):
                    raise PackageError("macos_hooks_unsupported")
                tail = own_hook_arguments(definition)
                if tail is not None:
                    matches.append((event, definition, tail))
        if len(matches) > 1:
            raise PackageError("macos_hooks_ambiguous")
        result.extend(matches)
    return result


def select_runtime(directory: Path, codex_home: Path, override: Optional[Path] = None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    profile = load_profile(directory)
    if profile is not None:
        return Path(profile["runtime"])
    hooks = codex_home / "hooks.json"
    if hooks.exists():
        matches = own_hooks(read_json(hooks))
        runtimes = {str(Path(tail[1]).resolve()) for _, _, tail in matches}
        if len(runtimes) > 1:
            raise PackageError("macos_legacy_runtime_ambiguous")
        if runtimes:
            return Path(runtimes.pop())
    return directory / "runtime"


def snapshot(path: Path) -> Optional[Path]:
    if path.is_symlink():
        raise PackageError("macos_symlink_write_refused")
    if not path.exists():
        return None
    digest = file_hash(path)
    backup = path.with_name(path.name + ".backup-" + digest)
    if backup.exists():
        if backup.is_symlink() or file_hash(backup) != digest:
            raise PackageError("macos_backup_collision")
        return backup
    fd, temporary = tempfile.mkstemp(prefix=".watchdog-backup-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output, path.open("rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if file_hash(temporary_path) != digest:
            raise PackageError("macos_backup_source_changed")
        try:
            # Publish a complete independent copy atomically, without replacing
            # another backup. Linking the temporary never links to live user state.
            os.link(temporary_path, backup)
        except FileExistsError:
            if backup.is_symlink() or file_hash(backup) != digest:
                raise PackageError("macos_backup_collision")
    finally:
        temporary_path.unlink(missing_ok=True)
    return backup


def replace_file(source: Path, destination: Path, mode: int) -> None:
    if destination.is_symlink():
        raise PackageError("macos_symlink_write_refused")
    fd, temporary = tempfile.mkstemp(prefix=".watchdog-", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def validate_executable(executable: Path) -> None:
    # No runtime/provider setup is entered by either command.
    for arguments in (["--version"], ["--help"]):
        result = subprocess.run([str(executable), *arguments], capture_output=True,
                                text=True, timeout=20, check=False)
        if result.returncode or (arguments == ["--version"]
                                 and result.stdout.strip() != "codex-watchdog " + __version__):
            raise PackageError("macos_candidate_validation_failed")


def install_package(bundle: Path, directory: Path, codex_home: Path, *,
                    runtime: Optional[Path] = None, install_dir: Optional[Path] = None) -> dict:
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(directory / "macos-install.lock"), ExitStack() as owner_lock:
        profile = load_profile(directory)
        selected_runtime = select_runtime(directory, codex_home, runtime)
        destination = (install_dir or (Path(profile["install_dir"]) if profile else
                                       directory / "bin")).expanduser().absolute()
        if destination.is_symlink() or destination.resolve() != destination:
            raise PackageError("macos_symlink_write_refused")
        expected = {name: file_hash(bundle / name) for name in PACKAGE_FILES}
        for name in PACKAGE_FILES:
            existing = destination / name
            if existing.exists() or existing.is_symlink():
                if (existing.is_symlink() or profile is None
                        or Path(profile["install_dir"]) != destination
                        or file_hash(existing) != profile["files"][name]):
                    raise PackageError("macos_existing_install_conflict")
        owner_lock.enter_context(FileLock(selected_runtime / "locks/foreground-run.lock"))
        validate_executable(bundle / "codex-watchdog")
        updated = {**(profile or {}), "schema_version": 1, "runtime": str(selected_runtime),
                   "install_dir": str(destination), "version": __version__, "files": expected}
        if updated == profile and all((destination / name).is_file() for name in PACKAGE_FILES):
            return {"status": "unchanged", "runtime_reused": True}
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile_path = directory / PROFILE_NAME
        snapshot(profile_path)
        backups = {name: snapshot(destination / name) for name in PACKAGE_FILES}
        replaced = []
        try:
            for name in PACKAGE_FILES:
                replace_file(bundle / name, destination / name, 0o700)
                replaced.append(name)
            validate_executable(destination / "codex-watchdog")
            InstructionStore._atomic_json(profile_path, updated)
        except Exception:
            # Roll back only exact files this invocation replaced; never clear a directory.
            for name in reversed(replaced):
                if backups[name] is None:
                    (destination / name).unlink()
                else:
                    replace_file(backups[name], destination / name, 0o700)
            raise
    return {"status": "installed", "runtime_reused": profile is not None or selected_runtime != directory / "runtime"}


def packaged_hooks(directory: Path, codex_home: Path) -> dict:
    profile = load_profile(directory)
    if profile is None:
        raise PackageError("macos_install_required")
    executable = Path(profile["install_dir"]) / "codex-watchdog"
    if not executable.is_file() or file_hash(executable) != profile["files"]["codex-watchdog"]:
        raise PackageError("macos_existing_install_conflict")
    path = codex_home / "hooks.json"
    document = copy.deepcopy(read_json(path) if path.exists() else {
        "description": "Codex WatchDog user hooks; trust each definition manually", "hooks": {},
    })
    matches = own_hooks(document)
    hooks = document.setdefault("hooks", {})
    for event in ("Stop", "PermissionRequest"):
        matching = [entry for entry in matches if entry[0] == event]
        if matching:
            _, definition, tail = matching[0]
            # Keep grace, polling, timeout, matcher, and all unrelated provider keys.
            tail[1] = profile["runtime"]
            definition["command"] = shlex.join([str(executable), *tail])
        else:
            command = shlex.join([str(executable), "--runtime", profile["runtime"],
                                  "hook", "--grace-seconds", "30", "--poll-seconds", "0.1"])
            definition = {"type": "command", "command": command,
                          "timeout": 60 if event == "Stop" else 10,
                          "statusMessage": "Waiting briefly for a watchdog instruction" if event == "Stop"
                          else "Recording pre-routing approval event"}
            group = {"hooks": [definition]}
            if event == "PermissionRequest":
                group["matcher"] = ""
            hooks.setdefault(event, []).append(group)
    return document


def install_packaged_hooks(directory: Path, codex_home: Path) -> dict:
    # Installation and upgrades share a lock, so the rendered stable path cannot race an install.
    with FileLock(directory / "macos-install.lock"):
        document = packaged_hooks(directory, codex_home)
        path = codex_home / "hooks.json"
        if path.exists() and read_json(path) == document:
            return {"status": "unchanged", "trust": "not_modified"}
        snapshot(path)
        InstructionStore._atomic_json(path, document)
    return {"status": "installed", "trust": "review_exact_definitions_in_codex"}


def relay_values(path: Path) -> str:
    value = read_json(path)
    channel, users = value.get("channel_id"), value.get("allowed_user_ids")
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(channel, str) or re.fullmatch(r"[CG][A-Z0-9]{8,}", channel) is None
            or not isinstance(users, list) or not users
            or any(not isinstance(user, str) or re.fullmatch(r"[UW][A-Z0-9]{8,}", user) is None for user in users)):
        raise PackageError("macos_slack_config_invalid")
    return channel + "\t" + ",".join(dict.fromkeys(users))


def main(argv: Sequence[str], executable: Path) -> int:
    from .cli import build_parser, main as core_main

    arguments = list(argv)
    try:
        if arguments == ["--help"] or arguments == ["-h"]:
            print(build_parser().format_help())
            print("macOS package commands:\n  macos-install [--runtime PATH] [--install-dir PATH]\n"
                  "  macos-hooks [--install]\n\n"
                  "Run watchdog-macos.sh for the saved Keychain Slack foreground workflow.")
            return 0
        if arguments == ["--version"]:
            return core_main(arguments)
        directory, codex_home = config_directory(), codex_directory()
        if arguments and arguments[0] == "macos-install":
            if sys.platform != "darwin":
                raise PackageError("macos_required")
            parser = argparse.ArgumentParser(prog="codex-watchdog macos-install")
            parser.add_argument("--runtime", type=Path)
            parser.add_argument("--install-dir", type=Path)
            args = parser.parse_args(arguments[1:])
            print(json.dumps(install_package(executable.parent, directory, codex_home,
                                             runtime=args.runtime, install_dir=args.install_dir), sort_keys=True))
            return 0
        if arguments and arguments[0] == "macos-hooks":
            if sys.platform != "darwin":
                raise PackageError("macos_required")
            parser = argparse.ArgumentParser(prog="codex-watchdog macos-hooks")
            parser.add_argument("--install", action="store_true")
            args = parser.parse_args(arguments[1:])
            result = (install_packaged_hooks(directory, codex_home) if args.install else
                      packaged_hooks(directory, codex_home))
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        # Bounded data bridges for the unchanged Keychain logic in the Bash launcher.
        if arguments == ["_macos-runtime"]:
            print(select_runtime(directory, codex_home))
            return 0
        if len(arguments) == 2 and arguments[0] == "_macos-relay-config":
            print(relay_values(Path(arguments[1])))
            return 0
        if len(arguments) == 5 and arguments[0] == "_macos-launcher-summary":
            print(json.dumps({"status": "ready", "runtime": arguments[1], "slack_reply": arguments[2],
                              "smtp_configured": arguments[3] == "true", "slack_only": arguments[4] == "1"}, sort_keys=True))
            return 0
        if not arguments:
            launcher = executable.with_name("watchdog-macos.sh")
            return subprocess.run([str(launcher)], check=False).returncode
        if "--version" not in arguments and not any(v == "--runtime" or v.startswith("--runtime=") for v in arguments):
            arguments = ["--runtime", str(select_runtime(directory, codex_home)), *arguments]
        return core_main(arguments)
    except (PackageError, StoreBusyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = (str(exc) if isinstance(exc, PackageError) else
                  "macos_install_busy" if isinstance(exc, StoreBusyError) else "macos_package_operation_failed")
        print(json.dumps({"status": "blocked", "reason": reason}), file=sys.stderr)
        return 1
