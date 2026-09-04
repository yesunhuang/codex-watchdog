"""Render or conservatively install source-checkout Codex WatchDog user hooks.

Trust is intentionally outside this tool. After installation, the user must
inspect and trust each exact definition through Codex's `/hooks` UI.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Dict, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from codex_watchdog.hook_config import (  # noqa: E402
    build_hooks_document_from_prefix,
    hook_command,
    install_hooks,
    installation_result,
    render_hooks_document,
)


def _command(parts: Sequence[str], windows: bool) -> str:
    return hook_command(parts, windows=windows)


def build_hooks_document(
    repo_root: Path,
    runtime: Path,
    python_executable: Path,
    grace_seconds: float,
    poll_seconds: float,
    test_mode: bool,
) -> Dict:
    repo_root = repo_root.expanduser().resolve()
    python_executable = python_executable.expanduser().resolve()
    script = repo_root / "tools" / "codex_watchdog_hook.py"
    if not script.is_file():
        raise FileNotFoundError(f"watchdog hook entry point not found: {script}")
    if not python_executable.is_file():
        raise FileNotFoundError(f"Python executable not found: {python_executable}")
    return build_hooks_document_from_prefix(
        [str(python_executable), str(script)],
        runtime,
        grace_seconds,
        poll_seconds,
        test_mode,
    )


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
    rendered = render_hooks_document(document)
    if not args.install:
        sys.stdout.write(rendered)
        return 0
    status, path = install_hooks(args.codex_home, document)
    print(json.dumps(installation_result(status, path, rendered), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
