"""Run source tests without inheriting the developer's WatchDog provider state."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="watchdog-source-tests-") as temporary:
        root = Path(temporary)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("CODEX_WATCHDOG_", "PYTHON", "XDG_"))
                       and key not in ("CODEX_HOME", "VIRTUAL_ENV")}
        if os.name == "nt":
            # Hosted pwsh injects its module paths. Native Windows PowerShell
            # fixtures must discover their own 5.1 security/DPAPI modules.
            environment = {key: value for key, value in environment.items()
                           if key.casefold() != "psmodulepath"}
        # Leave CODEX_HOME unset: code defaults to this disposable HOME, and
        # individual tests can explicitly select their own fixture Codex home.
        environment.update(HOME=temporary, USERPROFILE=temporary,
            APPDATA=str(root / "app"), LOCALAPPDATA=str(root / "local"),
            XDG_CONFIG_HOME=str(root / "config"), XDG_STATE_HOME=str(root / "state"),
            GIT_CONFIG_GLOBAL=str(root / "gitconfig"),
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        if sys.platform == "darwin":
            security = root / "empty-keychain"
            security.write_text("#!/bin/sh\nexit 44\n")
            security.chmod(0o700)
            environment["CODEX_WATCHDOG_MACOS_CONFIG_DIR"] = str(root / "mac-config")
            environment["CODEX_WATCHDOG_MACOS_SECURITY_BIN"] = str(security)
        # Native Keychain tests explicitly create their own disposable store;
        # ordinary CLI tests cannot read the login Keychain through this loader.
        return subprocess.run([sys.executable, "-m", "pytest"] + sys.argv[1:],
                              cwd=repository, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())
