"""Exercise manual registration migration through an actual packaged CLI."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def verify_manual_rebind(executable: Path, root: Path, environment: dict) -> None:
    root.mkdir()
    runtime, repo = root / "retained runtime", root / "same repository"
    repo.mkdir()
    env = {k: v for k, v in environment.items() if not k.startswith("CODEX_WATCHDOG_")}
    # Registration acceptance has no messaging provider. Keep it independent
    # of the parent installer test's deliberately partial legacy profile.
    env.update(LOCALAPPDATA=str(root / "messaging"),
               CODEX_WATCHDOG_LINUX_CONFIG_DIR=str(root / "messaging"),
               CODEX_WATCHDOG_MACOS_CONFIG_DIR=str(root / "messaging"))
    if sys.platform == "darwin":
        security = root / "empty-keychain-fixture"
        security.write_text("#!/bin/sh\nexit 44\n")
        security.chmod(0o700)
        env["CODEX_WATCHDOG_MACOS_SECURITY_BIN"] = str(security)
    git = shutil.which("git", path=env["PATH"])
    assert git
    def run(args, *, input=None, timeout=30):
        result = subprocess.run([str(v) for v in args], env=env, cwd=root, capture_output=True,
                                text=True, input=input, timeout=timeout)
        assert result.returncode == 0, (args[1:2], result.stdout, result.stderr)
        return result.stdout
    run([git, "init", "--initial-branch=main", repo])
    (repo / "README.md").write_text("Isolated package migration fixture\n")
    run([git, "-C", repo, "add", "README.md"])
    run([git, "-C", repo, "-c", "user.name=Package Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-m", "fixture"])
    before_git = [run([git, "--no-optional-locks", "-C", repo, *args]) for args in
                  (["rev-parse", "HEAD"], ["show-ref"], ["status", "--porcelain=v1"], ["ls-files", "--stage"])]
    prefix = [executable, "--runtime", runtime, "--codex-home", env["CODEX_HOME"]]
    old, new = "31111111-2222-4333-8444-555555555555", "41111111-2222-4333-8444-555555555555"
    run([*prefix, "workspace-add", "--workspace", "manual-rebind", "--repo", repo, "--thread", old])
    audit = runtime / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "zz-prior-cursor.json").write_text('{}\n')
    def cycle():
        result = json.loads(run([*prefix, "run", "--once", "--manual-only"]))
        assert result["status"] == "completed" and len(result["workspaces"]) == 1
        assert result["workspaces"][0]["status"] == "completed"
        return result["workspaces"][0]
    assert cycle()["stop_count"] == 0
    state_path = runtime / "service/state" / (hashlib.sha256(b"manual-rebind").hexdigest() + ".json")
    state = json.loads(state_path.read_text())
    state["future_compatible_setting"] = {"retain": True}
    state_path.write_text(json.dumps(state))
    original = state_path.read_bytes()
    run([*prefix, "workspace-remove", "--workspace", "manual-rebind"])
    run([*prefix, "workspace-add", "--workspace", "manual-rebind", "--repo", repo, "--thread", new])
    def publish_stop(identity):
        # Exercise the packaged native-hook producer. Legacy audit files alone
        # are intentionally not imported by the history-independent service.
        payload = {"hook_event_name": "Stop", "session_id": new, "cwd": str(repo),
                   "turn_id": identity, "last_assistant_message": "Package rebind fixture"}
        result = json.loads(run([*prefix, "hook", "--grace-seconds", "30"],
                                input=json.dumps(payload), timeout=45))
        assert result == {}
    publish_stop("first-new-stop")
    assert cycle()["stop_count"] == 1
    assert cycle()["stop_count"] == 0
    publish_stop("next-stop")
    assert cycle()["stop_count"] == 1
    assert cycle()["stop_count"] == 0
    state = json.loads(state_path.read_text())
    assert state["session_id"] == new and state["future_compatible_setting"] == {"retain": True}
    backups = list(state_path.parent.glob(state_path.name + ".backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    after_git = [run([git, "--no-optional-locks", "-C", repo, *args]) for args in
                 (["rev-parse", "HEAD"], ["show-ref"], ["status", "--porcelain=v1"], ["ls-files", "--stage"])]
    assert after_git == before_git
