"""Keep external Codex processes in the user's original loader environment."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Dict


def codex_process_environment(codex_home: Path) -> Dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    if sys.platform == "linux" and getattr(sys, "frozen", False):
        # PyInstaller prepends its own libraries for the frozen parent. External
        # Codex executables must load their host libraries instead. Never change
        # the parent's environment, which its later extension imports still need.
        if "LD_LIBRARY_PATH_ORIG" in environment:
            environment["LD_LIBRARY_PATH"] = environment["LD_LIBRARY_PATH_ORIG"]
        else:
            environment.pop("LD_LIBRARY_PATH", None)
    return environment
