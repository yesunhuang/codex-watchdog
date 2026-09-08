"""Exercise manual registration migration through an actual packaged CLI."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def verify_manual_rebind(executable: Path, root: Path, environment: dict) -> None:
    root.mkdir()
    runtime, repo = root / "retained runtime", root / "same repository"
    repo.mkdir()
    env = {k: v for k, v in environment.items() if not k.startswith("CODEX_WATCHDOG_")}
    git = shutil.which("git", path=env["PATH"])
    assert git
    def run(args):
        result = subprocess.run([str(v) for v in args], env=env, cwd=root, capture_output=True,
                                text=True, timeout=30)
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
    def write_stop(name, identity):
        event = {"schema_version": 1, "event_type": "Stop", "outcome": "grace_expired_parked",
                 "session_id": new, "workspace": str(repo), "audit_id": identity,
                 "invocation_id": identity, "turn_id": identity,
                 "hook_completed_at": datetime.now(timezone.utc).isoformat()}
        (audit / name).write_text(json.dumps(event))
    write_stop("aa-first-new-stop.json", "first-new-stop")
    assert cycle()["stop_count"] == 1
    assert cycle()["stop_count"] == 0
    write_stop("zzz-next-stop.json", "next-stop")
    assert cycle()["stop_count"] == 1
    assert cycle()["stop_count"] == 0
    state = json.loads(state_path.read_text())
    assert state["session_id"] == new and state["future_compatible_setting"] == {"retain": True}
    backups = list(state_path.parent.glob(state_path.name + ".backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    after_git = [run([git, "--no-optional-locks", "-C", repo, *args]) for args in
                 (["rev-parse", "HEAD"], ["show-ref"], ["status", "--porcelain=v1"], ["ls-files", "--stage"])]
    assert after_git == before_git
