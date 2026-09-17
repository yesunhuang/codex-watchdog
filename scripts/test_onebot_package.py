"""Frozen OneBot imports, license provenance and loopback-only send acceptance."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def check(package: Path, version: str) -> dict:
    from PyInstaller.archive.readers import CArchiveReader
    from websockets.asyncio.server import serve
    package = package.resolve()
    executable = package / ("codex-watchdog.exe" if os.name == "nt" else "codex-watchdog")
    archive = CArchiveReader(str(executable))
    vendor = "codex_watchdog/_vendor/napcat_sdk/"
    source = Path(__file__).resolve().parents[1] / "src" / vendor
    for name in ("LICENSE", "UPSTREAM.md"):
        entry = next(key for key in archive.toc if key.replace("\\", "/") == vendor + name)
        assert archive.extract(entry) == (source / name).read_bytes()
    licenses = package / "THIRD_PARTY_LICENSES"
    inventory = json.loads((licenses / "inventory.json").read_text())
    component = next(p for p in inventory["packages"] if p["name"] == "napcat-sdk connection (vendored)")
    assert component["version"] == "4d2f72a7e11ff749e1b0d7d8962198db31fc74a8" and component["license"] == "MIT"
    client = next(p for p in inventory["packages"] if p["name"] == "websockets")
    assert client["version"] == "15.0.1" and "BSD" in client["license"]
    for record in component["license_files"] + client["license_files"]:
        path = (licenses / record["path"]).resolve()
        assert licenses in path.parents
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
    guide = package / ("docs/ONEBOT_QQ.md" if os.name == "nt" else "ONEBOT_QQ.md")
    assert guide.is_file()
    with tempfile.TemporaryDirectory(prefix="watchdog-onebot-frozen-") as temp:
        home = Path(temp)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CODEX_WATCHDOG_", "PYTHON", "XDG_")) and key != "CODEX_HOME"}
        env.update(HOME=temp, USERPROFILE=temp, APPDATA=str(home / "app"),
                   LOCALAPPDATA=str(home / "local"), CODEX_HOME=str(home / "codex"))
        env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") if os.name == "nt" else str(home / "empty-path")
        command = [str(executable), "--runtime", str(home / "runtime")]
        def run(*args, code=0):
            result = subprocess.run(command + list(args), cwd=home, env=env,
                                    capture_output=True, text=True, timeout=30)
            assert result.returncode == code, "frozen OneBot command failed"
            assert "onebot-frozen-secret" not in result.stdout + result.stderr
            return result.stdout
        assert run("--version").strip() == "codex-watchdog " + version
        offline = json.loads(run("onebot-check", code=1))
        assert offline["client_version"] == "15.0.1" and offline["error"] is None
        assert not offline["notification_configured"] and not offline["backend_verified"]
        calls = []
        async def exercise():
            async def backend(ws):
                assert ws.request.headers["Authorization"] == "Bearer onebot-frozen-secret"
                async for raw in ws:
                    request = json.loads(raw)
                    calls.append(request)
                    data = {"user_id": 12345} if request["action"] == "get_login_info" else {"message_id": -100}
                    await ws.send(json.dumps(dict(status="ok", retcode=0, data=data, echo=request["echo"])))
            async with serve(backend, "127.0.0.1", 0) as server:
                values = dict(WS_URL="ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1]),
                              ACCESS_TOKEN="onebot-frozen-secret", SELF_ID="12345", CHAT_TYPE="group", CHAT_ID="67890")
                env.update({"CODEX_WATCHDOG_ONEBOT_" + key: value for key, value in values.items()})
                env["CODEX_WATCHDOG_INTERACTIVE_TRANSPORT"] = "onebot"
                live = json.loads(await asyncio.to_thread(run, "onebot-check", "--connect"))
                assert live["backend_verified"] and live["error"] is None
                first = json.loads(await asyncio.to_thread(run, "notify-test", "--id", "onebot-frozen"))
                duplicate = json.loads(await asyncio.to_thread(run, "notify-test", "--id", "onebot-frozen", code=1))
                assert first["status"] == "sent" and first["channel"] == "onebot"
                assert duplicate["status"] == "suppressed"
        asyncio.run(exercise())
        assert [call["action"] for call in calls] == ["get_login_info", "get_login_info", "send_msg"]
        assert "Machine:" in calls[-1]["params"]["message"][0]["data"]["text"]
    return dict(schema_version=1, status="passed", version=version, protocol_version=11,
                client_version="15.0.1", embedded_vendor_notices=True, loopback_send=True,
                no_duplicate_send=True, provider_contacted=False, real_qq_acceptance=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.package, args.expected_version), sort_keys=True))
