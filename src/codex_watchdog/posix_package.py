"""Shared POSIX package file operations and conservative native-hook parsing."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
from typing import Optional

from .stop_hook import HookSettings


class PackageError(RuntimeError):
    """Bounded reason codes, without paths, provider values, or subprocess output."""


def read_json(path: Path) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise PackageError("posix_config_too_large")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise PackageError("posix_config_unreadable") from exc
    if not isinstance(value, dict):
        raise PackageError("posix_config_unreadable")
    return value


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def own_hook_arguments(definition: dict) -> Optional[list]:
    """Recognize only the shipped source or packaged hook command grammar."""
    command = definition.get("command")
    if not isinstance(command, str):
        return None
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise PackageError("posix_hook_command_unreadable") from exc
    prefix = (1 if parts and Path(parts[0]).name == "codex-watchdog" else
              2 if len(parts) > 1 and Path(parts[1]).name == "codex_watchdog_hook.py" else 0)
    if not prefix:
        return None
    tail = parts[prefix:]
    if (definition.get("type") != "command" or len(tail) < 3
            or tail[0] != "--runtime" or not Path(tail[1]).is_absolute() or tail[2] != "hook"):
        raise PackageError("posix_hook_command_unsupported")
    options = tail[3:]
    if len(options) % 2 or any(v not in ("--grace-seconds", "--poll-seconds") for v in options[::2]):
        raise PackageError("posix_hook_command_unsupported")
    if len(set(options[::2])) != len(options[::2]):
        raise PackageError("posix_hook_command_unsupported")
    settings = dict(zip(options[::2], options[1::2]))
    try:
        HookSettings(Path(tail[1]), float(settings.get("--grace-seconds", 30)),
                     float(settings.get("--poll-seconds", 0.1))).validate()
    except ValueError as exc:
        raise PackageError("posix_hook_command_unsupported") from exc
    return tail


def own_hooks(document: dict) -> list:
    result = []
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        raise PackageError("posix_hooks_unsupported")
    for event in ("Stop", "PermissionRequest"):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise PackageError("posix_hooks_unsupported")
        matches = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise PackageError("posix_hooks_unsupported")
            for definition in group["hooks"]:
                if not isinstance(definition, dict):
                    raise PackageError("posix_hooks_unsupported")
                tail = own_hook_arguments(definition)
                if tail is not None:
                    matches.append((event, definition, tail))
        if len(matches) > 1:
            raise PackageError("posix_hooks_ambiguous")
        result.extend(matches)
    return result


def snapshot(path: Path) -> Optional[Path]:
    if path.is_symlink():
        raise PackageError("posix_symlink_write_refused")
    if not path.exists():
        return None
    digest = file_hash(path)
    backup = path.with_name(path.name + ".backup-" + digest)
    if backup.exists():
        if backup.is_symlink() or file_hash(backup) != digest:
            raise PackageError("posix_backup_collision")
        return backup
    fd, temporary = tempfile.mkstemp(prefix=".watchdog-backup-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output, path.open("rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if file_hash(temporary_path) != digest:
            raise PackageError("posix_backup_source_changed")
        try:
            # Publish a complete independent copy atomically, without replacing
            # another backup. Linking the temporary never links to live user state.
            os.link(temporary_path, backup)
        except FileExistsError:
            if backup.is_symlink() or file_hash(backup) != digest:
                raise PackageError("posix_backup_collision")
    finally:
        temporary_path.unlink(missing_ok=True)
    return backup


def replace_file(source: Path, destination: Path, mode: int) -> None:
    if destination.is_symlink():
        raise PackageError("posix_symlink_write_refused")
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
