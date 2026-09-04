"""Render or conservatively install Codex watchdog user hooks.

Trust is intentionally outside this tool. After installation, the user must
inspect and trust each exact definition through Codex's `/hooks` UI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Dict, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from codex_watchdog.stop_hook import HookSettings  # noqa: E402
from codex_watchdog.storage import InstructionStore  # noqa: E402


def _command(parts: Sequence[str], windows: bool) -> str:
    if not windows:
        return shlex.join(parts)
    if any(
        '"' in part or any(character.isspace() for character in part) for part in parts
    ):
        raise ValueError(
            "the installed Windows hook runner requires quote-free commands; "
            "use absolute paths without whitespace"
        )
    return " ".join(parts)


def build_hooks_document(
    repo_root: Path,
    runtime: Path,
    python_executable: Path,
    grace_seconds: float,
    poll_seconds: float,
    test_mode: bool,
) -> Dict:
    repo_root = repo_root.expanduser().resolve()
    runtime = runtime.expanduser().resolve()
    python_executable = python_executable.expanduser().resolve()
    script = repo_root / "tools" / "codex_watchdog_hook.py"
    if not script.is_file():
        raise FileNotFoundError(f"watchdog hook entry point not found: {script}")
    if not python_executable.is_file():
        raise FileNotFoundError(f"Python executable not found: {python_executable}")
    HookSettings(runtime, grace_seconds, poll_seconds, test_mode).validate()

    parts = [
        str(python_executable),
        str(script),
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
    command = _command(parts, windows=False)
    command_windows = _command(parts, windows=True)
    stop_timeout = max(10, int(grace_seconds) + 30)
    return {
        "description": "Codex watchdog user hooks; trust each exact definition manually",
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, default=Path("~/.codex"))
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--grace-seconds", type=float, default=600.0)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--test-mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--install",
        action="store_true",
        help="write hooks.json only when it is missing or already equivalent",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.expanduser().resolve()
    runtime = (
        args.runtime.expanduser().resolve()
        if args.runtime is not None
        else repo_root / ".codex-watchdog"
    )
    document = build_hooks_document(
        repo_root,
        runtime,
        args.python,
        args.grace_seconds,
        args.poll_seconds,
        args.test_mode,
    )
    rendered = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if not args.install:
        sys.stdout.write(rendered)
        return 0
    status, path = install_hooks(args.codex_home, document)
    print(
        json.dumps(
            {
                "status": status,
                "path": str(path),
                "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                "trust": "manual_review_required",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
