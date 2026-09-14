"""No-provider frozen setup acceptance, using isolated profiles and a real POSIX TTY."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time


def check(package):
    executable = package.resolve() / ("codex-watchdog.exe" if os.name == "nt" else "codex-watchdog")
    with tempfile.TemporaryDirectory(prefix="watchdog-setup-package-") as directory:
        home = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_WATCHDOG_", "PYTHON")) and k != "CODEX_HOME"}
        config = home / "config"
        env.update(HOME=directory, USERPROFILE=directory, LOCALAPPDATA=directory,
                   CODEX_WATCHDOG_MACOS_CONFIG_DIR=str(config), CODEX_WATCHDOG_LINUX_CONFIG_DIR=str(config))
        if os.name == "nt":
            config = home / "CodexWatchdog"
        elif sys.platform == "darwin":
            # Inspecting even an isolated profile must see current-user Keychain
            # traces. This existing launcher fixture supplies an empty store,
            # never modifying or treating the actual user Keychain as pristine.
            security = home / "fixture-security"
            security.write_text("#!/bin/sh\nexit 44\n")
            security.chmod(0o700)
            env["CODEX_WATCHDOG_MACOS_SECURITY_BIN"] = str(security)
        command = [str(executable), "--runtime", str(home / "runtime"), "setup-messaging"]
        def run(*extra):
            result = subprocess.run(command + list(extra), env=env, cwd=home, input="", text=True,
                                    capture_output=True, timeout=45)
            assert result.returncode == 0, (result.stdout, result.stderr)
            return result.stdout
        assert json.loads(run("--check"))["state"] == "PRISTINE_UNCONFIGURED"
        assert "interactive" in run("--auto")
        assert not (config / "messaging-setup.json").exists()
        tty_checked = False
        if os.name != "nt":
            import pty
            child, master = pty.fork()
            if child == 0:
                os.chdir(home)
                os.execve(executable, command + ["--auto"], env)
            output, status, sent = b"", None, False
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(master, 4096)
                        except OSError:
                            chunk = b""
                        output += chunk
                        if b"Choose 1-4:" in output and not sent:
                            os.write(master, b"4\n")
                            sent = True
                    pid, status = os.waitpid(child, os.WNOHANG)
                    if pid:
                        break
                    status = None
                assert status == 0 and sent, output.decode(errors="replace")
            finally:
                if status is None:
                    os.kill(child, 15)
                    os.waitpid(child, 0)
                os.close(master)
            assert json.loads((config / "messaging-setup.json").read_text())["status"] == "skipped"
            before = (config / "messaging-setup.json").read_bytes()
            assert "Choose 1-4" not in run("--auto")
            assert (config / "messaging-setup.json").read_bytes() == before
            tty_checked = True
        config.mkdir(exist_ok=True)
        legacy = config / "lark-relay.json"
        legacy.write_bytes(b"malformed legacy profile; preserve exactly")
        before = legacy.read_bytes()
        assert json.loads(run("--check"))["state"] == "EXISTING_OR_PARTIAL"
        assert "Choose 1-4" not in run("--auto") and legacy.read_bytes() == before
    return dict(schema_version=1, status="passed", pristine_headless_never_prompts=True,
                partial_legacy_preserved=True, interactive_skip_and_restart=tty_checked,
                provider_contacted=False, production_profile_modified=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    print(json.dumps(check(parser.parse_args().package), sort_keys=True))
