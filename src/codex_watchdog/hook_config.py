"""Render and conservatively install packaged Codex WatchDog user hooks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
from typing import Dict, Sequence, Tuple

from .stop_hook import HookSettings
from .storage import InstructionStore


def hook_command(parts: Sequence[str], *, windows: bool) -> str:
    if not windows:
        return shlex.join(parts)
    if any(
        '"' in part or any(character.isspace() for character in part) for part in parts
    ):
        raise ValueError(
            "the installed Windows hook runner requires quote-free commands; "
            "place codex-watchdog.exe and its runtime in paths without whitespace"
        )
    return " ".join(parts)


def build_hooks_document_from_prefix(
    command_prefix: Sequence[str],
    runtime: Path,
    grace_seconds: float,
    poll_seconds: float,
    test_mode: bool,
) -> Dict:
    if not command_prefix:
        raise ValueError("hook command prefix must not be empty")
    runtime = runtime.expanduser().resolve()
    HookSettings(runtime, grace_seconds, poll_seconds, test_mode).validate()
    parts = [
        *command_prefix,
        "--runtime",
        str(runtime),
        "hook",
        "--grace-seconds",
        f"{grace_seconds:g}",
        "--poll-seconds",
        f"{poll_seconds:g}",
    ]
    if test_mode:
        parts.append("--test-mode")
    command = hook_command(parts, windows=False)
    command_windows = hook_command(parts, windows=True)
    stop_timeout = max(10, int(grace_seconds) + 30)
    return {
        "description": "Codex WatchDog user hooks; trust each definition manually",
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "commandWindows": command_windows,
                            "timeout": 10,
                            "statusMessage": "Recording pre-routing approval event",
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "commandWindows": command_windows,
                            "timeout": stop_timeout,
                            "statusMessage": "Waiting briefly for a watchdog instruction",
                        }
                    ]
                }
            ],
        },
    }


def build_packaged_hooks_document(
    executable: Path,
    runtime: Path,
    grace_seconds: float,
    poll_seconds: float,
    test_mode: bool,
) -> Dict:
    executable = executable.expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"WatchDog executable not found: {executable}")
    return build_hooks_document_from_prefix(
        [str(executable)], runtime, grace_seconds, poll_seconds, test_mode
    )


def render_hooks_document(document: Dict) -> str:
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def install_hooks(codex_home: Path, document: Dict) -> Tuple[str, Path]:
    codex_home = codex_home.expanduser().resolve()
    destination = codex_home / "hooks.json"
    if destination.is_symlink():
        raise RuntimeError(f"refusing to replace symlink: {destination}")
    if destination.exists():
        try:
            current = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"refusing to replace unreadable existing hook file: {destination}"
            ) from exc
        if current != document:
            raise RuntimeError(
                f"refusing to overwrite existing hook configuration: {destination}"
            )
        return "unchanged", destination
    InstructionStore._atomic_json(destination, document)
    return "installed", destination


def installation_result(status: str, path: Path, rendered: str) -> Dict:
    return {
        "status": status,
        "path": str(path),
        "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "trust": "manual_review_required",
    }
