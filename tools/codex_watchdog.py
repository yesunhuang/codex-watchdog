"""Repo-local CLI entry point that works before the package is installed."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from codex_watchdog.cli import main  # noqa: E402


raise SystemExit(main())
