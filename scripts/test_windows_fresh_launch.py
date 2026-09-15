"""Exercise the Windows no-argument EXE through a real pseudoconsole.

Requires pywinpty on the test host only. Uses disposable HOME/APPDATA/Codex
directories, no real credentials, no providers and no production conversations.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from winpty import PtyProcess


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


class Console:
    def __init__(self, executable, environment, cwd, log):
        # No CLI flags: this is the executable's double-click entry path.
        self.process = PtyProcess.spawn([str(executable)], cwd=str(cwd), env=environment)
        self.pending = queue.Queue()
        self.output = ""
        self.log = log

        def reader():
            try:
                while True:
                    self.pending.put(self.process.read(4096))
            except (EOFError, OSError):
                self.pending.put(None)

        self.reader = threading.Thread(target=reader, daemon=True)
        self.reader.start()

    def until(self, phrase, timeout=55):
        deadline = time.monotonic() + timeout
        while phrase not in ANSI.sub("", self.output):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("Console did not reach " + phrase)
            try:
                part = self.pending.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if part is None:
                raise AssertionError("Console exited before " + phrase)
            self.output += part

    def interrupt(self):
        self.process.sendcontrol("c")
        deadline = time.monotonic() + 20
        while self.process.isalive() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not self.process.isalive(), "Own fixture did not exit after Ctrl-C"

    def close(self):
        try:
            if self.process.isalive():
                self.process.terminate(force=True)
            self.process.close(force=True)
        finally:
            self.log.write_text(self.output, encoding="utf-8")


def check(executable, evidence):
    evidence.mkdir(parents=True, exist_ok=False)
    results = []
    for case, diagnostic_first in (("direct-first-launch", False), ("check-then-launch", True)):
        home = evidence / case
        home.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CODEX_WATCHDOG_", "PYTHON", "SLACK_", "LARK_", "FEISHU_", "SMTP_"))
               and k != "CODEX_HOME"}
        env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "local"),
                   APPDATA=str(home / "roaming"), CODEX_HOME=str(home / "codex"),
                   PATH=os.environ["SystemRoot"] + "\\System32;" + os.environ["SystemRoot"])
        for name in ("local", "roaming", "codex"):
            (home / name).mkdir()
        config = home / "local" / "CodexWatchdog"
        if diagnostic_first:
            result = subprocess.run([str(executable), "setup-messaging", "--check"],
                                    env=env, cwd=home, input="", capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["state"] == "PRISTINE_UNCONFIGURED"
            assert not config.exists(), "Read-only check changed first-run state"
        assert not config.exists()
        console = Console(executable, env, home, home / "first-console.log")
        try:
            console.until("Choose 1-4:")
            profile = json.loads((config / "launcher-profile.json").read_text(encoding="utf-8"))
            runtime = Path(profile["runtime_path"])
            assert runtime == config / "runtime" and runtime.is_dir(), "First-run profile points to absent runtime"
            before = (config / "launcher-profile.json").read_bytes()
            if diagnostic_first:
                console.process.write("4\r\n")
                console.until('"cycle_id"')
            # Case one exits at the first setup prompt, before service startup.
            console.interrupt()
        finally:
            console.close()
        assert runtime.is_dir()
        console = Console(executable, env, home, home / "reopened-console.log")
        try:
            console.until('"cycle_id"')
            assert "no longer exists" not in console.output
            assert (config / "launcher-profile.json").read_bytes() == before
            console.interrupt()
        finally:
            console.close()
        results.append(dict(case=case, status="passed", no_argument_launch=True,
                            real_console=True, runtime_ready_before_user_input=True,
                            reopened_without_missing_runtime=True))
    return dict(schema_version=1, status="passed", cases=results,
                production_profile_modified=False, provider_contacted=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.executable.resolve(), args.evidence.resolve()), sort_keys=True))
