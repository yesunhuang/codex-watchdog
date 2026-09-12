"""Native package acceptance in an isolated home, with Python absent from PATH."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import zipfile

from package_rebind_acceptance import verify_manual_rebind


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    args = parser.parse_args()
    assert sys.platform == "darwin" and platform.machine() == "arm64", "native Apple Silicon required"
    package = args.package.resolve()
    manifest = json.loads((package / "package-manifest.json").read_text())
    allowed = {"codex-watchdog", "watchdog-macos.sh", "setup-slack-relay-macos.sh", "LICENSE",
               "MACOS_PACKAGE.md", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES/README.md",
               "THIRD_PARTY_LICENSES/inventory.json"}
    inventory = json.loads((package / "THIRD_PARTY_LICENSES/inventory.json").read_text())
    assert inventory["schema_version"] == 1
    assert {"CPython runtime", "pyinstaller", "macholib"} <= {p["name"] for p in inventory["packages"]}
    for dependency in inventory["packages"]:
        assert dependency["license_files"], "dependency license text is missing"
        for license_file in dependency["license_files"]:
            relative = Path(license_file["path"])
            assert not relative.is_absolute() and ".." not in relative.parts
            name = "THIRD_PARTY_LICENSES/" + relative.as_posix()
            assert digest(package / name) == license_file["sha256"]
            allowed.add(name)
    assert set(manifest["files"]) == allowed
    assert {p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file()} == allowed | {"package-manifest.json"}
    assert all(digest(package / name) == sha for name, sha in manifest["files"].items())
    archive = package.parent / (package.name + ".zip")
    assert digest(archive) == (archive.parent / (archive.name + ".sha256")).read_text().split()[0]
    with zipfile.ZipFile(archive) as zipped:
        assert set(zipped.namelist()) == {package.name + "/" + name for name in allowed | {"package-manifest.json"}}
    assert subprocess.check_output(["/usr/bin/lipo", "-archs", str(package / "codex-watchdog")], text=True).strip() == "arm64"
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(package / "codex-watchdog")], check=True)

    # Inspect our bundled bytecode, including compressed constants and filenames.
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(package / "codex-watchdog"))
    assert not any(name.endswith("direct_url.json") for name in reader.toc), "build-location metadata was bundled"
    pyz = reader.open_embedded_archive(next(name for name in reader.toc if name.endswith(".pyz")))
    forbidden = (str(ROOT), str(Path.home()))
    for name in reader.toc:
        if "codex_watchdog-" in name and name.endswith("METADATA"):
            metadata = reader.extract(name).decode()
            assert not any(marker in metadata for marker in forbidden)

    def inspect_code(value):
        if isinstance(value, types.CodeType):
            assert not any(marker in value.co_filename for marker in forbidden), "private bytecode filename"
            for item in value.co_consts:
                inspect_code(item)
        elif isinstance(value, str):
            assert not any(marker in value for marker in forbidden), "private bytecode constant"

    own_modules = [name for name in pyz.toc if name == "codex_watchdog" or name.startswith("codex_watchdog.")]
    assert own_modules
    for name in own_modules:
        inspect_code(pyz.extract(name))

    output_log = []
    with tempfile.TemporaryDirectory(prefix="watchdog-macos-package-") as temporary:
        root = Path(temporary).resolve()
        home, work = root / "user with spaces", root / "empty working directory"
        home.mkdir(); work.mkdir()
        tools = root / "path without python"
        tools.mkdir()
        for command, path in {"bash": "/bin/bash", "cat": "/bin/cat", "uname": "/usr/bin/uname", "dirname": "/usr/bin/dirname",
                              "date": "/bin/date", "git": "/usr/bin/git"}.items():
            (tools / command).symlink_to(path)
        environment = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_WATCHDOG_")
                       and k not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CODEX_HOME",
                                     "SSL_CERT_FILE", "SSL_CERT_DIR")}
        config = home / "Library/Application Support/CodexWatchdog"
        codex = home / ".codex"
        environment.update(HOME=str(home), PATH=str(tools), CODEX_HOME=str(codex),
                           CODEX_WATCHDOG_MACOS_CONFIG_DIR=str(config))
        assert shutil.which("python", path=str(tools)) is None and shutil.which("python3", path=str(tools)) is None
        extracted = root / "download with spaces"
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(extracted)], check=True)
        copied = extracted / package.name
        executable = copied / "codex-watchdog"
        assert all(digest(copied / name) == sha for name, sha in manifest["files"].items())
        assert all(os.access(copied / name, os.X_OK) for name in ("codex-watchdog", "watchdog-macos.sh", "setup-slack-relay-macos.sh"))

        def run(command, *, env=None, accepted=(0,), input_text=None, timeout=30):
            result = subprocess.run([str(v) for v in command], cwd=work, env=environment if env is None else env,
                                    capture_output=True, text=True, input=input_text, timeout=timeout, check=False)
            output_log.append(result.stdout + result.stderr)
            assert result.returncode in accepted, "package command failed: " + str(command[1:2])
            return result

        assert run([executable, "--version"]).stdout.strip() == "codex-watchdog " + manifest["version"]
        run([executable, "--help"])
        doctor = run([executable, "doctor", "--export", "-"], accepted=(0, 1))
        report = json.loads(doctor.stdout)
        assert report["platform"]["name"] == "macos" and report["platform"]["architecture"] == "arm64"
        assert not any(value in doctor.stdout + doctor.stderr for value in (str(home), str(copied), str(ROOT)))
        assert not config.exists() and not codex.exists(), "doctor created user state"

        runtime = home / "existing source checkout/.codex-watchdog"
        runtime.mkdir(parents=True); codex.mkdir()
        (runtime / "retained-journal.json").write_bytes(b'{"old_user_state":"retain"}')
        hook_command = shlex.join([str(home / "old venv/python"), str(home / "old source/tools/codex_watchdog_hook.py"),
                                   "--runtime", str(runtime), "hook", "--grace-seconds", "30", "--poll-seconds", "0.25"])
        hooks = {"unknown_user_key": "retain", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": hook_command, "timeout": 60}]}],
            "PermissionRequest": [{"matcher": "", "hooks": [{"type": "command", "command": hook_command, "timeout": 10}]}],
        }}
        (codex / "hooks.json").write_text(json.dumps(hooks))
        (codex / "config.toml").write_text('# existing trust remains untouched\n')
        config.mkdir(parents=True)
        (config / "slack-relay.json").write_text(json.dumps({"schema_version": 1, "channel_id": "G12345678",
                                                            "allowed_user_ids": ["U12345678"], "future_key": "retain"}))
        protected = [runtime / "retained-journal.json", codex / "config.toml", config / "slack-relay.json"]
        protected_hashes = {path: digest(path) for path in protected}
        old_hooks = (codex / "hooks.json").read_bytes()
        assert json.loads(run([executable, "macos-install"]).stdout)["runtime_reused"]
        installed = config / "bin/codex-watchdog"
        tls = json.loads(run([installed, "macos-tls-check"]).stdout)
        assert tls == {"status": "passed", "ca_source": "macos_system_bundle", "tls_verification": True,
                       "endpoint": "slack_api_test", "authenticated": False}
        profile_path = config / "macos-launcher.json"
        profile = json.loads(profile_path.read_text())
        assert profile["runtime"] == str(runtime)
        assert (codex / "hooks.json").read_bytes() == old_hooks
        assert json.loads(run([executable, "macos-install"]).stdout)["status"] == "unchanged"
        verify_manual_rebind(installed, root / "manual registration acceptance", environment)
        rendered = json.loads(run([installed, "macos-hooks"]).stdout)
        assert rendered["unknown_user_key"] == "retain"
        command = shlex.split(rendered["hooks"]["Stop"][0]["hooks"][0]["command"])
        assert command[:4] == [str(installed), "--runtime", str(runtime), "hook"]
        run([installed, "macos-hooks", "--install"])
        assert next(codex.glob("hooks.json.backup-*")).read_bytes() == old_hooks
        stable_hooks = (codex / "hooks.json").read_bytes()

        # A native disposable Keychain, without replacing the user's search list or default.
        keychain = root / "fixture.keychain-db"
        password = "package-fixture-only"
        security = Path("/usr/bin/security")
        run([security, "create-keychain", "-p", password, keychain])
        try:
            run([security, "unlock-keychain", "-p", password, keychain])
            for account, token in [("slack-bot-token", "xoxb-package-fixture"), ("slack-app-token", "xapp-package-fixture")]:
                run([security, "add-generic-password", "-a", account, "-s", "org.localcodexwatchdog.slack", "-w", token, keychain])
            wrapper = root / "security fixture"
            wrapper.write_text('#!/bin/bash\nexec /usr/bin/security "$@" "$PACKAGE_TEST_KEYCHAIN"\n')
            wrapper.chmod(0o700)
            keychain_env = {**environment, "CODEX_WATCHDOG_MACOS_SECURITY_BIN": str(wrapper),
                            "PACKAGE_TEST_KEYCHAIN": str(keychain), "CODEX_WATCHDOG_SMTP_HOST": "smtp.invalid",
                            "CODEX_WATCHDOG_SMTP_FROM": "sender@example.invalid", "CODEX_WATCHDOG_SMTP_TO": "recipient@example.invalid",
                            "CODEX_WATCHDOG_SMTP_PASSWORD": "email-fixture-only"}
            launcher = installed.with_name("watchdog-macos.sh")
            summary = json.loads(run([launcher, "--slack-only", "--dry-run"], env=keychain_env).stdout)
            assert summary == {"status": "ready", "runtime": str(runtime), "slack_reply": "macos_keychain",
                               "slack_reply_mode": "socket", "smtp_configured": False, "slack_only": True}
            shared = json.loads(run([launcher, "--slack-only", "--shared-slack-app", "--dry-run"], env=keychain_env).stdout)
            assert shared == {**summary, "slack_reply_mode": "poll"}
            once = run([launcher, "--slack-only", "--", "--once", "--manual-only"], env=keychain_env)
            assert json.loads(once.stdout.splitlines()[-1])["workspace_count"] == 0
            run([installed.with_name("setup-slack-relay-macos.sh"), "--help"], env=keychain_env)

            # Replace a changed package payload, preserving the stable hooks/profile choices.
            profile["future_setting"] = {"retain": True}
            profile_path.write_text(json.dumps(profile))
            with (copied / "watchdog-macos.sh").open("a") as handle:
                handle.write("\n# package replacement acceptance fixture\n")
            run([executable, "macos-install"])
            assert json.loads(profile_path.read_text())["future_setting"] == {"retain": True}
            assert json.loads(run([installed, "macos-hooks", "--install"]).stdout)["status"] == "unchanged"
            assert (codex / "hooks.json").read_bytes() == stable_hooks
            assert all(digest(path) == value for path, value in protected_hashes.items())
            for account, expected in [("slack-bot-token", "xoxb-package-fixture"), ("slack-app-token", "xapp-package-fixture")]:
                result = subprocess.run([str(security), "find-generic-password", "-a", account, "-s",
                                         "org.localcodexwatchdog.slack", "-w", str(keychain)], env=environment,
                                        capture_output=True, text=True, timeout=10, check=True)
                assert result.stdout.strip() == expected
        finally:
            run([security, "delete-keychain", keychain])

        # Normal foreground CLI lifetime and busy-owner upgrade refusal; no provider configured.
        process = subprocess.Popen([str(installed), "run", "--manual-only", "--interval", "1"],
                                   cwd=work, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=20), "foreground did not emit a cycle"
                assert json.loads(process.stdout.readline())["workspace_count"] == 0
            blocked = run([executable, "macos-install"], accepted=(1,))
            assert json.loads(blocked.stderr)["reason"] == "macos_install_busy"
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=15)
            output_log.append(stdout + stderr)
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)

        # Exercise the rendered production command; this is fixture hook input, not a real Codex Stop.
        payload = {"hook_event_name": "Stop", "session_id": "package-smoke-session", "turn_id": "package-smoke-turn",
                   "cwd": str(work), "last_assistant_message": "PACKAGE_HOOK_PARK"}
        hook_result = run(command, input_text=json.dumps(payload), timeout=65)
        assert json.loads(hook_result.stdout) == {}
        audits = [json.loads(path.read_text()) for path in (runtime / "audit").glob("*.json")]
        terminal = [v for v in audits if v.get("turn_id") == "package-smoke-turn" and v.get("outcome") == "grace_expired_parked"]
        assert len(terminal) == 1 and 30000 <= terminal[0]["hook_duration_ms"] < 60000
        assert all(digest(path) == value for path, value in protected_hashes.items())
        assert not any(secret in output for output in output_log for secret in
                       ("xoxb-package-fixture", "xapp-package-fixture", "email-fixture-only"))

    result = {"schema_version": 1, "status": "passed", "version": manifest["version"], "architecture": "arm64",
              "python_hidden": True, "source_free_layout": True, "own_bytecode_privacy": True,
              "doctor_privacy": True, "default_ca_outbound_tls": True, "manual_registration_migration": True,
              "legacy_runtime_reused": True, "native_fixture_keychain_preserved": True,
              "upgrade_and_stable_hooks": True, "foreground_lifecycle": True, "busy_upgrade_refused": True,
              "production_fixture_stop_ms": terminal[0]["hook_duration_ms"], "real_codex_hook_acceptance": "pending"}
    (package.parent / "macos-package-acceptance.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
