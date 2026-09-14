"""Verify frozen Feishu/Lark imports and notices without provider credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def check(package: Path, version: str) -> dict:
    package = package.resolve()
    executable = package / ("codex-watchdog.exe" if os.name == "nt" else "codex-watchdog")
    assert executable.is_file(), "missing packaged executable"
    with tempfile.TemporaryDirectory(prefix="watchdog-lark-package-") as temp:
        home = Path(temp)
        # No live provider values, source checkout or installed user profile.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CODEX_WATCHDOG_", "PYTHON")) and key != "CODEX_HOME"}
        env.update(HOME=temp, USERPROFILE=temp, APPDATA=str(home / "roaming"),
                   LOCALAPPDATA=str(home / "local"), CODEX_HOME=str(home / "codex"))
        env["PATH"] = (str(Path(os.environ["SystemRoot"]) / "System32")
                       if os.name == "nt" else str(home / "empty-path"))
        command = [str(executable), "--runtime", str(home / "runtime")]
        reported = subprocess.run(command + ["--version"], cwd=home, env=env,
                                  capture_output=True, text=True, timeout=60, check=True)
        assert reported.stdout.strip() == "codex-watchdog " + version
        checks = []
        for domain in ("feishu", "lark"):
            env["CODEX_WATCHDOG_LARK_DOMAIN"] = domain
            result = subprocess.run(command + ["lark-check"], cwd=home, env=env,
                                    capture_output=True, text=True, timeout=60)
            assert result.returncode == 1, "unconfigured audit must not report readiness"
            data = json.loads(result.stdout)
            assert data == dict(schema_version=1, domain=domain, sdk_version="1.4.0",
                                sdk_available=True, notification_configured=False,
                                relay_configured=False, sdk_error=None), data
            checks.append(domain)
    licenses = package / "THIRD_PARTY_LICENSES"
    inventory = json.loads((licenses / "inventory.json").read_text(encoding="utf-8"))
    sdk = next(p for p in inventory["packages"] if p["name"] == "lark-channel-sdk")
    assert sdk["version"] == "1.4.0", "unexpected SDK inventory"
    assert "MIT" in sdk["license"] and "BSD-3-Clause" in sdk["license"]
    notices = []
    for record in sdk["license_files"]:
        path = (licenses / record["path"]).resolve()
        assert licenses.resolve() in path.parents, "license path escapes package"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        if path.name.upper().startswith("THIRD_PARTY_NOTICES"):
            notices.append(path.read_text(encoding="utf-8"))
    assert notices and any("protobuf" in text.lower() and "BSD" in text for text in notices), (
        "vendored protobuf license notice is missing")
    guide = package / ("docs/FEISHU_LARK.md" if os.name == "nt" else "FEISHU_LARK.md")
    assert guide.is_file(), "packaged Feishu/Lark setup guide is missing"
    return dict(schema_version=1, status="passed", version=version,
                sdk_version=sdk["version"], domains=checks,
                provider_contacted=False, source_python_available=False,
                sdk_notices_verified=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.package, args.expected_version), sort_keys=True))
