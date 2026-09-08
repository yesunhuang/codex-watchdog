"""PyInstaller entry point for the Apple Silicon foreground package."""

from pathlib import Path
import sys

from codex_watchdog.macos_package import main


raise SystemExit(main(sys.argv[1:], Path(sys.executable).absolute()))
