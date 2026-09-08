"""Current-user Linux package installation; the existing core owns every thread."""

from __future__ import annotations

import argparse
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
from typing import Optional, Sequence

from . import __version__
from .posix_package import (
    PackageError, file_hash, own_hooks, read_json, replace_file, snapshot,
)
from .storage import FileLock, InstructionStore, StoreBusyError


PROFILE_NAME = "linux-launcher.json"
PACKAGE_FILES = ("codex-watchdog",)


def architecture() -> str:
    try:
        return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "AMD64": "x64"}[platform.machine()]
    except KeyError as exc:
        raise PackageError("linux_architecture_unsupported") from exc


def config_directory() -> Path:
    override = os.environ.get("CODEX_WATCHDOG_LINUX_CONFIG_DIR")
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))).expanduser()
    selected = Path(override).expanduser() if override else data / "codex-watchdog"
    if not selected.is_absolute():
        raise PackageError("linux_config_path_not_absolute")
    return selected.resolve()


def load_profile(directory: Path) -> Optional[dict]:
    path = directory / PROFILE_NAME
    if not path.exists():
        return None
    value = read_json(path)
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or any(not isinstance(value.get(key), str) or not Path(value[key]).is_absolute()
                   for key in ("runtime", "install_dir", "codex_home"))
            or not isinstance(value.get("files"), dict)
            or set(value["files"]) != set(PACKAGE_FILES)
            or any(not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
                   for v in value["files"].values())):
        raise PackageError("linux_profile_unsupported")
    return value


def codex_directory(directory: Path, override: Optional[Path] = None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser().resolve()
    profile = load_profile(directory)
    return Path(profile["codex_home"]) if profile else (Path.home() / ".codex").resolve()


def select_runtime(directory: Path, codex_home: Path, override: Optional[Path] = None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    profile = load_profile(directory)
    if profile:
        return Path(profile["runtime"])
    path = codex_home / "hooks.json"
    if path.exists():
        matches = own_hooks(read_json(path))
        runtimes = {str(Path(tail[1]).resolve()) for _, _, tail in matches}
        if len(runtimes) > 1:
            raise PackageError("linux_legacy_runtime_ambiguous")
        if runtimes:
            return Path(runtimes.pop())
    return directory / "runtime"


def validate_bundle(bundle: Path) -> None:
    executable = bundle / "codex-watchdog"
    manifest = read_json(bundle / "package-manifest.json")
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or manifest.get("platform") != "linux"
            or manifest.get("architecture") != architecture()
            or manifest.get("version") != __version__
            or not isinstance(manifest.get("files"), dict)
            or executable.is_symlink() or not executable.is_file()
            or manifest["files"].get("codex-watchdog") != file_hash(executable)):
        raise PackageError("linux_candidate_manifest_invalid")
    with executable.open("rb") as handle:
        header = handle.read(20)
    machine = {"arm64": 183, "x64": 62}[architecture()]
    if (len(header) != 20 or header[:6] != b"\x7fELF\x02\x01"
            or int.from_bytes(header[18:20], "little") != machine):
        raise PackageError("linux_candidate_architecture_invalid")


def validate_executable(executable: Path) -> None:
    for arguments in (["--version"], ["--help"]):
        result = subprocess.run([str(executable), *arguments], capture_output=True,
                                text=True, timeout=30, check=False)
        if result.returncode or (arguments == ["--version"]
                                 and result.stdout.strip() != "codex-watchdog " + __version__):
            raise PackageError("linux_candidate_validation_failed")


def install_package(bundle: Path, directory: Path, codex_home: Path, *,
                    runtime: Optional[Path] = None, install_dir: Optional[Path] = None) -> dict:
    validate_bundle(bundle)
    directory = directory.expanduser().resolve()
    codex_home = codex_home.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(directory / "linux-install.lock"), ExitStack() as owner_locks:
        profile = load_profile(directory)
        selected_runtime = select_runtime(directory, codex_home, runtime)
        destination = (install_dir or (Path(profile["install_dir"]) if profile else
                                       directory / "bin")).expanduser().absolute()
        if destination.is_symlink() or destination.resolve() != destination:
            raise PackageError("linux_symlink_write_refused")
        expected = {name: file_hash(bundle / name) for name in PACKAGE_FILES}
        for name in PACKAGE_FILES:
            existing = destination / name
            if existing.exists() or existing.is_symlink():
                if (existing.is_symlink() or profile is None
                        or Path(profile["install_dir"]) != destination
                        or file_hash(existing) != profile["files"][name]):
                    raise PackageError("linux_existing_install_conflict")
        # An explicit runtime move cannot replace the executable of an old live owner.
        runtimes = {selected_runtime}
        if profile:
            runtimes.add(Path(profile["runtime"]))
        for owned_runtime in sorted(runtimes, key=str):
            owner_locks.enter_context(FileLock(owned_runtime / "locks/foreground-run.lock"))
        validate_executable(bundle / "codex-watchdog")
        updated = {**(profile or {}), "schema_version": 1, "runtime": str(selected_runtime),
                   "install_dir": str(destination), "codex_home": str(codex_home),
                   "version": __version__, "files": expected}
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
        raise PackageError("linux_install_required")
    executable = Path(profile["install_dir"]) / "codex-watchdog"
    if executable.is_symlink() or not executable.is_file() or file_hash(executable) != profile["files"]["codex-watchdog"]:
        raise PackageError("linux_existing_install_conflict")
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
    with FileLock(directory / "linux-install.lock"):
        document = packaged_hooks(directory, codex_home)
        path = codex_home / "hooks.json"
        if path.exists() and read_json(path) == document:
            return {"status": "unchanged", "trust": "not_modified"}
        snapshot(path)
        InstructionStore._atomic_json(path, document)
    return {"status": "installed", "trust": "review_exact_definitions_in_codex"}


def main(argv: Sequence[str], executable: Path) -> int:
    from .cli import build_parser, main as core_main

    arguments = list(argv)
    try:
        if not arguments or arguments in (["--help"], ["-h"]):
            print(build_parser().format_help())
            print("Linux package commands:\n  linux-install [--runtime PATH] [--install-dir PATH] [--codex-home PATH]\n"
                  "  linux-hooks [--install] [--codex-home PATH]\n\n"
                  "Install, review/trust hooks, bind the exact existing conversation, then use linux-run.\n"
                  "Use linux-release and wait for release before reopening that conversation in VS Code.")
            return 0
        if arguments == ["--version"]:
            return core_main(arguments)
        directory = config_directory()
        if arguments[0] in ("linux-install", "linux-hooks"):
            if sys.platform != "linux":
                raise PackageError("linux_required")
            parser = argparse.ArgumentParser(prog="codex-watchdog " + arguments[0])
            parser.add_argument("--codex-home", type=Path)
            if arguments[0] == "linux-install":
                parser.add_argument("--runtime", type=Path)
                parser.add_argument("--install-dir", type=Path)
            else:
                parser.add_argument("--install", action="store_true")
            args = parser.parse_args(arguments[1:])
            codex_home = codex_directory(directory, args.codex_home)
            if arguments[0] == "linux-install":
                result = install_package(executable.parent, directory, codex_home,
                                         runtime=args.runtime, install_dir=args.install_dir)
            else:
                result = install_packaged_hooks(directory, codex_home) if args.install else packaged_hooks(directory, codex_home)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        parsed = build_parser().parse_args(arguments)
        codex_home = codex_directory(directory, parsed.codex_home)
        if not any(v == "--runtime" or v.startswith("--runtime=") for v in arguments):
            arguments = ["--runtime", str(select_runtime(directory, codex_home)), *arguments]
        if not any(v == "--codex-home" or v.startswith("--codex-home=") for v in arguments):
            arguments = ["--codex-home", str(codex_home), *arguments]
        return core_main(arguments)
    except (PackageError, StoreBusyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = (str(exc).replace("posix_", "linux_", 1) if isinstance(exc, PackageError) else
                  "linux_install_busy" if isinstance(exc, StoreBusyError) else "linux_package_operation_failed")
        print(json.dumps({"status": "blocked", "reason": reason}), file=sys.stderr)
        return 1
