"""Native package acceptance with isolated state and an explicit fixture App Server."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import zipfile

from package_rebind_acceptance import verify_manual_rebind


ROOT = Path(__file__).resolve().parents[1]
THREAD = "11111111-2222-4333-8444-555555555555"
OTHER = "21111111-2222-4333-8444-555555555555"

# An external test dependency, never shipped in the package. It does no AI work.
FIXTURE_CODEX = '''import fcntl, json, os, pathlib, sqlite3, sys, uuid
home = pathlib.Path(os.environ["CODEX_HOME"])
if "--version" in sys.argv:
    print("codex-cli package-protocol-fixture")
    raise SystemExit(0)
if "queue" in sys.argv:
    thread = sys.argv[sys.argv.index("--thread") + 1]
    message = sys.argv[sys.argv.index("--message") + 1]
    queued = str(uuid.uuid4())
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES (?, ?, ?)", (queued, thread, json.dumps({"message":message})))
        db.execute("UPDATE queued_thread_revisions SET revision=revision+1 WHERE thread_id=?", (thread,))
    print("Queued message " + queued + " for thread " + thread + ".")
    raise SystemExit(0)
lock = None
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method, params = request["method"], request.get("params", {})
    with (home / "fixture-methods.jsonl").open("a") as record:
        record.write(json.dumps({"method":method, "params":params}) + "\\n")
    result = {}
    if method in ("thread/read", "thread/resume"):
        thread = params["threadId"]
        with sqlite3.connect(home / "state_5.sqlite") as db:
            cwd = db.execute("SELECT cwd FROM threads WHERE id=?", (thread,)).fetchone()[0]
        if method == "thread/resume":
            path = home / "thread-writer-locks" / (thread + ".lock")
            path.parent.mkdir(exist_ok=True)
            lock = path.open("a+b")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (home / "fixture-writer.pid").write_text(str(os.getpid()))
        result = {"thread":{"id":thread, "cwd":cwd, "status":{"type":"idle"}}}
    elif method != "initialize":
        raise RuntimeError("unsupported fixture method")
    print(json.dumps({"id":request["id"], "result":result}), flush=True)
'''


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def wait_state(runtime: Path, expected: str, process=None, timeout: float = 35) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads((runtime / "linux/status.json").read_text())
            if value.get("owner_state") == expected:
                return value
            if value.get("owner_state") == "blocked":
                raise AssertionError("fixture owner blocked: " + str(value.get("reason")))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if process is not None and process.poll() is not None:
            raise AssertionError("fixture owner exited before " + expected)
        time.sleep(0.1)
    raise AssertionError("fixture owner did not reach " + expected)


def validate_archive(package: Path) -> dict:
    manifest = json.loads((package / "package-manifest.json").read_text())
    required_glibc = tuple(map(int, manifest["minimum_glibc"].split(".")))
    host_glibc = tuple(map(int, platform.libc_ver()[1].split(".")))
    assert required_glibc <= {"x64": (2, 28), "arm64": (2, 35)}[manifest["architecture"]]
    assert host_glibc >= required_glibc
    architecture, machine = {"aarch64": ("arm64", 183), "x86_64": ("x64", 62)}[platform.machine()]
    assert manifest["platform"] == "linux" and manifest["architecture"] == architecture
    assert manifest["schema_version"] == 1 and len(manifest["source_commit"]) == 40
    allowed = {"codex-watchdog", "LICENSE", "LINUX_PACKAGE.md", "THIRD_PARTY_NOTICES.md",
               "THIRD_PARTY_LICENSES/README.md", "THIRD_PARTY_LICENSES/inventory.json"}
    inventory = json.loads((package / "THIRD_PARTY_LICENSES/inventory.json").read_text())
    assert inventory["schema_version"] == 1
    packages = {item["name"] for item in inventory["packages"]}
    assert {"CPython runtime", "pyinstaller"} <= packages
    for item in inventory["packages"]:
        assert item["license_files"], "missing dependency license: " + item["name"]
        for license_file in item["license_files"]:
            relative = Path(license_file["path"])
            assert not relative.is_absolute() and ".." not in relative.parts
            name = "THIRD_PARTY_LICENSES/" + relative.as_posix()
            assert digest(package / name) == license_file["sha256"]
            allowed.add(name)
    assert set(manifest["files"]) == allowed
    assert {p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file()} == allowed | {"package-manifest.json"}
    assert not any(p.is_symlink() for p in package.rglob("*"))
    assert all(digest(package / name) == sha for name, sha in manifest["files"].items())
    archive = package.parent / (package.name + ".zip")
    assert digest(archive) == (archive.parent / (archive.name + ".sha256")).read_text().split()[0]
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        assert set(zipped.namelist()) == {package.name + "/" + name for name in allowed | {"package-manifest.json"}}
        assert all(hashlib.sha256(zipped.read(package.name + "/" + name)).hexdigest() == sha
                   for name, sha in manifest["files"].items())
    with (package / "codex-watchdog").open("rb") as handle:
        header = handle.read(20)
    assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == machine
    from PyInstaller.archive.readers import CArchiveReader
    reader = CArchiveReader(str(package / "codex-watchdog"))
    assert not any(name.endswith("direct_url.json") for name in reader.toc)
    native = {item["file"]: item for item in inventory["native_libraries"]}
    assert set(native) == {name for name, entry in reader.toc.items() if entry[-1] == "b"}
    for name, item in native.items():
        assert item["license_package"] in packages
        assert hashlib.sha256(reader.extract(name)).hexdigest() == item["sha256"]
    pyz = reader.open_embedded_archive(next(name for name in reader.toc if name.endswith(".pyz")))
    forbidden = (str(ROOT), str(Path.home()))
    def private_path(value):
        return any(value == marker or marker + os.sep in value for marker in forbidden)
    def inspect(value):
        if isinstance(value, types.CodeType):
            assert not Path(value.co_filename).is_absolute()
            assert not private_path(value.co_filename)
            for child in value.co_consts:
                inspect(child)
        elif isinstance(value, str):
            assert not private_path(value)
    modules = [name for name in pyz.toc if name == "codex_watchdog" or name.startswith("codex_watchdog.")]
    assert modules
    for name in modules:
        inspect(pyz.extract(name))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--fixture-python", type=Path, default=Path(sys.executable),
                        help="external interpreter for the protocol fixture (defaults to the harness Python)")
    parser.add_argument("--archive-python", type=Path,
                        help="optional matching Python minor version for bundled bytecode inspection")
    parser.add_argument("--archive-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    assert sys.platform == "linux", "native Linux acceptance required"
    package = args.package.resolve()
    if args.archive_python:
        archive_python = args.archive_python.absolute()
        assert archive_python.is_file()
        probe = subprocess.run([str(archive_python), str(Path(__file__).resolve()),
                                "--package", str(package), "--archive-only"], check=True,
                               capture_output=True, text=True, timeout=60)
        manifest = json.loads(probe.stdout)
    else:
        manifest = validate_archive(package)
    if args.archive_only:
        print(json.dumps(manifest, sort_keys=True))
        return
    import sqlite3
    output_log = []
    with tempfile.TemporaryDirectory(prefix="watchdog-linux-package-") as temporary:
        root = Path(temporary).resolve()
        home, work = root / "user with spaces", root / "empty working directory"
        home.mkdir(); work.mkdir()
        codex, config, runtime = home / ".codex", home / "saved package profile", home / "previous runtime"
        tools = root / "path without python"
        tools.mkdir()
        git = shutil.which("git")
        assert git
        (tools / "git").symlink_to(git)
        assert shutil.which("python", path=str(tools)) is None and shutil.which("python3", path=str(tools)) is None
        environment = {"HOME": str(home), "PATH": str(tools), "LANG": "C.UTF-8",
                       "CODEX_HOME": str(codex), "CODEX_WATCHDOG_LINUX_CONFIG_DIR": str(config),
                       "TMPDIR": str(root), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        copied = root / "download with spaces"
        shutil.copytree(package, copied)
        executable = copied / "codex-watchdog"
        executable.chmod(0o700)
        def run(arguments, accepted=(0,), input_text=None, timeout=45, env=None):
            result = subprocess.run([str(v) for v in arguments], cwd=work, env=env or environment,
                                    input=input_text, capture_output=True, text=True, timeout=timeout)
            output_log.append(result.stdout + result.stderr)
            assert result.returncode in accepted, (arguments[1:2], result.returncode, result.stdout, result.stderr)
            return result
        assert run([executable, "--version"]).stdout.strip() == "codex-watchdog " + manifest["version"]
        assert "linux-install" in run([executable, "--help"]).stdout
        assert "linux-bind" in run([executable]).stdout
        assert not config.exists() and not codex.exists()
        doctor = run([executable, "doctor"], accepted=(0, 1, 2))
        assert str(home) not in doctor.stdout and str(ROOT) not in doctor.stdout
        export = work / "diagnostics.json"
        run([executable, "doctor", "--export", export], accepted=(0, 1, 2))
        assert str(home) not in export.read_text() and str(ROOT) not in export.read_text()
        assert not config.exists() and not codex.exists()
        # api.test accepts no credentials and never posts a Slack message. Exercise
        # the actual frozen urllib/TLS stack, including its current-host CA roots.
        tls_environment = {**environment, "CODEX_WATCHDOG_SLACK_WEBHOOK_URL": "https://slack.com/api/api.test"}
        tls_command = [executable, "--runtime", root / "credential-free TLS probe",
                       "notify-test", "--id", "linux-package-tls-probe"]
        tls_result = run(tls_command, accepted=(0, 1), env=tls_environment)
        if tls_result.returncode:
            for host_ca in (Path("/etc/ssl/certs/ca-certificates.crt"), Path("/etc/pki/tls/cert.pem")):
                if host_ca.is_file():
                    retry = run([*tls_command[:-1], "explicit-host-ca-probe"], accepted=(0, 1),
                                env={**tls_environment, "SSL_CERT_FILE": str(host_ca)})
                    print(json.dumps({"default_ca_probe": "failed", "explicit_host_ca_probe_exit": retry.returncode}), flush=True)
                    break
        assert tls_result.returncode == 0, "credential-free HTTPS failed with default CA discovery"
        assert json.loads(tls_result.stdout)["status"] == "sent"
        fresh = home / "fresh installation"
        fresh_env = {**environment, "CODEX_WATCHDOG_LINUX_CONFIG_DIR": str(fresh)}
        assert not json.loads(run([executable, "linux-install"], env=fresh_env).stdout)["runtime_reused"]
        assert json.loads((fresh / "linux-launcher.json").read_text())["runtime"] == str(fresh / "runtime")
        assert not (codex / "hooks.json").exists()
        runtime.mkdir()
        write_json(runtime / "retained-journal.json", {"schema_version": 1, "future_key": "retain"})
        write_json(runtime / "workspace-mappings.json", {"schema_version": 1, "preserve": "existing mapping"})
        write_json(config / "notification-routing.json", {"schema_version": 1, "unchanged": "existing routing"})
        codex.mkdir(exist_ok=True)
        (codex / "config.toml").write_text('# preserve existing trust\n')
        (codex / "credential-fixture.bin").write_bytes(b"opaque-test-credential-bytes")
        source_command = shlex.join([str(home / "old Python"), str(home / "old source/tools/codex_watchdog_hook.py"),
                                     "--runtime", str(runtime), "hook", "--grace-seconds", "30", "--poll-seconds", "0.1"])
        hooks = {"future_key": "retain", "hooks": {
            event: [{"hooks": [{"type": "command", "command": source_command, "timeout": timeout}]}]
            for event, timeout in (("Stop", 60), ("PermissionRequest", 10))}}
        write_json(codex / "hooks.json", hooks)
        protected = [runtime / "retained-journal.json", runtime / "workspace-mappings.json",
                     config / "notification-routing.json", codex / "config.toml", codex / "credential-fixture.bin"]
        hashes = {p: digest(p) for p in protected}
        old_hooks = (codex / "hooks.json").read_bytes()
        assert json.loads(run([executable, "linux-install"]).stdout)["runtime_reused"]
        installed = config / "bin/codex-watchdog"
        profile_path = config / "linux-launcher.json"
        profile = json.loads(profile_path.read_text())
        assert profile["runtime"] == str(runtime) and profile["codex_home"] == str(codex)
        assert (codex / "hooks.json").read_bytes() == old_hooks
        assert json.loads(run([executable, "linux-install"]).stdout)["status"] == "unchanged"
        verify_manual_rebind(installed, root / "manual registration acceptance", environment)
        rendered = json.loads(run([installed, "linux-hooks"]).stdout)
        command = shlex.split(rendered["hooks"]["Stop"][0]["hooks"][0]["command"])
        assert command[:4] == [str(installed), "--runtime", str(runtime), "hook"]
        assert rendered["future_key"] == "retain"
        run([installed, "linux-hooks", "--install"])
        assert next(codex.glob("hooks.json.backup-*")).read_bytes() == old_hooks
        stable_hooks = (codex / "hooks.json").read_bytes()
        profile["future_setting"] = {"retain": True}
        profile["version"] = "prior-profile-fixture"
        write_json(profile_path, profile)
        before_inode = installed.stat().st_ino
        assert json.loads(run([executable, "linux-install"]).stdout)["status"] == "installed"
        assert installed.stat().st_ino != before_inode
        assert json.loads(profile_path.read_text())["future_setting"] == {"retain": True}
        assert next(installed.parent.glob("codex-watchdog.backup-*")).read_bytes() == installed.read_bytes()
        assert json.loads(run([installed, "linux-hooks", "--install"]).stdout)["status"] == "unchanged"
        assert (codex / "hooks.json").read_bytes() == stable_hooks
        payload = {"hook_event_name": "Stop", "session_id": THREAD, "turn_id": "fixture-park",
                   "cwd": str(work), "last_assistant_message": "PACKAGE_FIXTURE_STOP"}
        assert json.loads(run(command, input_text=json.dumps(payload), timeout=65).stdout) == {}
        audits = [json.loads(p.read_text()) for p in (runtime / "audit").glob("*.json")]
        terminal = [v for v in audits if v.get("turn_id") == "fixture-park" and v.get("outcome") == "grace_expired_parked"]
        assert len(terminal) == 1 and 30000 <= terminal[0]["hook_duration_ms"] < 60000
        submit = [installed, "submit", "--id", "fixture-stop", "--thread", THREAD, "--message", "PACKAGE_FIXTURE_CONTINUE"]
        run(submit); run(submit)
        payload["turn_id"] = "fixture-continuation"
        assert json.loads(run(command, input_text=json.dumps(payload)).stdout)["decision"] == "block"
        payload["stop_hook_active"] = True
        assert json.loads(run(command, input_text=json.dumps(payload)).stdout) == {}
        assert not list((runtime / "inbox").glob("*.json")) and not list((runtime / "inflight").glob("*.json"))
        assert len(list((runtime / "consumed").glob("*.json"))) == 1
        repo, upstream = root / "bound repository", root / "upstream.git"
        run([git, "init", "--bare", upstream]); run([git, "init", "--initial-branch=main", repo])
        (repo / "README.md").write_text("Package fixture repository\n")
        run([git, "-C", repo, "add", "README.md"])
        run([git, "-C", repo, "-c", "user.name=Package Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture"])
        run([git, "-C", repo, "remote", "add", "origin", upstream])
        run([git, "-C", repo, "push", "--set-upstream", "origin", "main"])
        def git_state():
            values = [run([git, "--no-optional-locks", "-C", repo, *arguments]).stdout
                      for arguments in (["rev-parse", "HEAD"], ["show-ref"], ["status", "--porcelain=v1"], ["ls-files", "--stage"])]
            return values + [digest(repo / ".git/index"), digest(repo / ".git/config")]
        before_git = git_state()
        rollout = codex / "sessions/fixture.jsonl"
        rollout.parent.mkdir()
        rollout.write_text('{}\n')
        with sqlite3.connect(codex / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads (id, cwd, source, thread_source, archived, rollout_path)")
            db.execute("INSERT INTO threads VALUES (?, ?, 'vscode', 'user', 0, ?)", (THREAD, str(repo), str(rollout)))
        with sqlite3.connect(codex / "queue_1.sqlite") as db:
            db.execute("CREATE TABLE queued_items (id, thread_id, payload_json)")
            db.execute("CREATE TABLE queued_thread_revisions (thread_id, revision)")
            db.execute("INSERT INTO queued_thread_revisions VALUES (?, 0)", (THREAD,))
        fixture_script = root / "external protocol fixture.py"
        fixture_script.write_text(FIXTURE_CODEX)
        fixture_stderr = root / "external protocol fixture.stderr"
        fake_codex = tools / "codex"
        fake_codex.write_text("#!/bin/sh\nexec " + shlex.join([str(args.fixture_python.resolve(strict=True)), str(fixture_script)])
                              + ' "$@" 2>> ' + shlex.quote(str(fixture_stderr)) + '\n')
        fake_codex.chmod(0o700)
        bind = [installed, "linux-bind", "--workspace", "package-fixture", "--repo", repo, "--thread", THREAD]
        run(bind)
        reservation = codex / "watchdog-linux" / (THREAD + ".json")
        value = json.loads(reservation.read_text())
        value["future_setting"] = "retain"
        write_json(reservation, value)
        reservation_bytes = reservation.read_bytes()
        run(bind)
        assert reservation.read_bytes() == reservation_bytes
        rejected = run([*bind[:-1], OTHER], accepted=(1,))
        assert "blocked" in rejected.stdout
        lock = codex / "thread-writer-locks" / (THREAD + ".lock")
        lock.parent.mkdir()
        # A busy observer must produce a bounded, actionable release result,
        # with no state changes while initial admission is unavailable.
        control_lock = codex / "watchdog-control" / THREAD / "owner.lock"
        with control_lock.open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before_release = reservation.read_bytes()
            started = time.monotonic()
            busy_release = run([installed, "linux-release"], accepted=(1,))
            assert json.loads(busy_release.stdout)["reason"] == "linux_release_busy"
            assert time.monotonic() - started >= 1.0
            assert reservation.read_bytes() == before_release
        with lock.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = run([installed, "linux-run", "--codex-executable", fake_codex], accepted=(1,))
            assert "linux_conflicting_writer" in blocked.stdout
        # Discard only the fixture's old observation before waiting for a new one.
        (runtime / "linux/status.json").unlink()
        def start_owner(env=None):
            return subprocess.Popen([str(installed), "linux-run", "--interval", "1", "--codex-executable", str(fake_codex)],
                                    cwd=work, env=env or environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
        process = start_owner()
        try:
            wait_state(runtime, "owned", process)
            blocked = run([executable, "linux-install"], accepted=(1,))
            assert "linux_install_busy" in blocked.stderr
            run([installed, "linux-run", "--codex-executable", fake_codex], accepted=(1,))
            queue = [installed, "queue", "--thread", THREAD, "--id", "fixture-queue", "--message", "PACKAGE_FIXTURE_QUEUE"]
            receipt = json.loads(run(queue).stdout)
            assert receipt["status"] == "enqueued"
            assert json.loads(run(queue).stdout)["deduplicated"]
            foreign = run([installed, "--runtime", root / "foreign runtime", *queue[1:]], accepted=(0, 1))
            assert json.loads(foreign.stdout)["status"] == "rejected"
            with sqlite3.connect(codex / "queue_1.sqlite") as db:
                assert db.execute("SELECT COUNT(*) FROM queued_items").fetchone()[0] == 1
            # A one-file executable has a bootstrap parent and a controller child.
            # Crash only this fixture's new process group, including its fake server.
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=15)
            deadline = time.monotonic() + 15
            while True:
                with lock.open("rb") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        assert time.monotonic() < deadline
                time.sleep(0.1)
            (runtime / "linux/status.json").unlink()
            process = start_owner()
            wait_state(runtime, "owned", process)
            assert json.loads(run(queue).stdout)["deduplicated"]
            run([installed, "linux-release"])
            wait_state(runtime, "releasing", process)
            with sqlite3.connect(codex / "queue_1.sqlite") as db:
                db.execute("DELETE FROM queued_items WHERE thread_id=?", (THREAD,))
                db.execute("UPDATE queued_thread_revisions SET revision=revision+1 WHERE thread_id=?", (THREAD,))
            wait_state(runtime, "released")
            process.communicate(timeout=15)
            assert process.returncode == 0
            # Exercise the frozen notification path against a loopback-only
            # Slack-compatible receiver, with no provider credentials/accounts.
            alerts = []

            class AlertReceiver(BaseHTTPRequestHandler):
                def do_POST(self):
                    assert self.path == "/fixture-alert"
                    alerts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")

                def log_message(self, *args):
                    pass

            with HTTPServer(("127.0.0.1", 0), AlertReceiver) as receiver:
                server = threading.Thread(target=receiver.serve_forever, daemon=True)
                server.start()
                alert_env = dict(environment, CODEX_WATCHDOG_SLACK_WEBHOOK_URL=
                    f"http://127.0.0.1:{receiver.server_port}/fixture-alert")
                try:
                    run(bind)
                    (runtime / "linux/status.json").unlink()
                    process = start_owner(alert_env)
                    wait_state(runtime, "owned", process)
                    fixture_pid = int((codex / "fixture-writer.pid").read_text())
                    assert os.getpgid(fixture_pid) == process.pid
                    os.kill(fixture_pid, signal.SIGKILL)
                    stdout, stderr = process.communicate(timeout=20)
                    assert process.returncode == 1, (stdout, stderr)
                    failure = json.loads(stdout.splitlines()[-1])
                    assert failure["owner_state"] == "blocked"
                    assert failure["notification"]["status"] == "sent"
                    assert len(alerts) == 1 and "can no longer watch or control" in alerts[0]["text"]
                    (runtime / "linux/status.json").unlink()
                    process = start_owner(alert_env)
                    wait_state(runtime, "owned", process)
                    deadline = time.monotonic() + 10
                    while len(alerts) < 2 and time.monotonic() < deadline:
                        time.sleep(0.1)
                    assert len(alerts) == 2 and "again" in alerts[1]["text"]
                    run([installed, "linux-release"])
                    wait_state(runtime, "released")
                    process.communicate(timeout=15)
                    assert process.returncode == 0 and len(alerts) == 2
                finally:
                    receiver.shutdown()
                    server.join(timeout=5)
        except BaseException:
            # Only this generated fixture's diagnostics; never real server output.
            if fixture_stderr.exists():
                print("External protocol fixture stderr:\n" + fixture_stderr.read_text(), flush=True)
            raise
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=10)
        methods = [json.loads(line) for line in (codex / "fixture-methods.jsonl").read_text().splitlines()]
        resumes = [item for item in methods if item["method"] == "thread/resume"]
        assert len(resumes) == 4 and all(item["params"] == {"threadId": THREAD, "excludeTurns": True} for item in resumes)
        assert json.loads(reservation.read_text())["future_setting"] == "retain"
        assert git_state() == before_git
        assert all(digest(path) == sha for path, sha in hashes.items())
        assert (codex / "hooks.json").read_bytes() == stable_hooks
        assert not any("opaque-test-credential-bytes" in value for value in output_log)
    result = {"schema_version": 1, "status": "passed", "version": manifest["version"],
              "architecture": manifest["architecture"], "source_commit": manifest["source_commit"],
              "minimum_glibc": manifest["minimum_glibc"], "host_glibc": platform.libc_ver()[1],
              "glibc_requirement_verified": True,
              "credential_free_default_ca_https": True,
              "python_hidden": True, "source_free_layout": True, "elf_architecture_verified": True,
              "dependency_and_native_license_inventory": True, "own_bytecode_privacy": True,
              "doctor_privacy": True, "manual_registration_migration": True,
              "fresh_install": True, "source_runtime_reused": True,
              "replacement_and_stable_hooks": True, "foreground_and_writer_locks": True,
              "fixture_exact_thread_resume_restart_release": True, "fixture_queue_deduplication": True,
              "bounded_release_lock_admission": True,
              "detached_owner_loss_and_recovery_notifications": True,
              "notification_fixture_transport": "loopback_http_no_provider_credentials",
              "watchdog_git_read_only": True, "production_fixture_stop_ms": terminal[0]["hook_duration_ms"],
              "real_codex_hook_acceptance": "separate_native_user_test",
              "desktop_e2e_acceptance": "pending"}
    (package.parent / ("linux-" + manifest["architecture"] + "-package-acceptance.json")).write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
