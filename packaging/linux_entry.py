"""PyInstaller entry point for either native Linux architecture."""

from pathlib import Path
import sys

from codex_watchdog.linux_package import main


raise SystemExit(main(sys.argv[1:], Path(sys.executable).absolute()))
