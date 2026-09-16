from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher")


def _run_launcher(tmp_path: Path, **environment_updates: str):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith(("CODEX_WATCHDOG_LARK_", "CODEX_WATCHDOG_ONEBOT_")) or name == "CODEX_WATCHDOG_INTERACTIVE_TRANSPORT":
            environment.pop(name)
    environment["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    for name in (
        "CODEX_WATCHDOG_SLACK_WEBHOOK_URL",
        "CODEX_WATCHDOG_SLACK_BOT_TOKEN",
        "CODEX_WATCHDOG_SLACK_APP_TOKEN",
        "CODEX_WATCHDOG_SLACK_CHANNEL_ID",
        "CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS",
        "CODEX_WATCHDOG_DUO_PLINK_TARGET",
        "CODEX_WATCHDOG_PLINK_EXE",
    ):
        environment.pop(name, None)
    environment.update(environment_updates)
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(root / "watchdog.ps1"),
            "-DryRun",
            "-NoDuo",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def test_dry_run_reports_relay_environment_without_exposing_tokens(
    tmp_path: Path,
) -> None:
    bot_token = "xoxb-test-launcher-secret"
    app_token = "xapp-test-launcher-secret"
    result = _run_launcher(
        tmp_path,
        CODEX_WATCHDOG_SLACK_BOT_TOKEN=bot_token,
        CODEX_WATCHDOG_SLACK_APP_TOKEN=app_token,
        CODEX_WATCHDOG_SLACK_CHANNEL_ID="C12345678",
        CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS="U12345678,U87654321",
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["slack_reply"] == "environment"
    assert summary["slack"] == "not_configured"
    assert bot_token not in result.stdout + result.stderr
    assert app_token not in result.stdout + result.stderr


def test_dry_run_rejects_partial_relay_configuration_without_echoing_secret(
    tmp_path: Path,
) -> None:
    bot_token = "xoxb-test-partial-secret"
    result = _run_launcher(
        tmp_path,
        CODEX_WATCHDOG_SLACK_BOT_TOKEN=bot_token,
    )

    assert result.returncode != 0
    assert "incomplete or invalid" in result.stderr
    assert bot_token not in result.stdout + result.stderr


def test_packaged_dry_run_does_not_require_python(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    package = tmp_path / "package"
    package.mkdir()
    shutil.copy2(root / "watchdog.ps1", package / "watchdog.ps1")
    (package / "codex-watchdog.exe").write_bytes(b"test executable placeholder")
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    environment["PATH"] = os.pathsep.join(
        [str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"]]
    )
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "CODEX_WATCHDOG_SLACK_WEBHOOK_URL",
        "CODEX_WATCHDOG_SLACK_BOT_TOKEN",
        "CODEX_WATCHDOG_SLACK_APP_TOKEN",
        "CODEX_WATCHDOG_SLACK_CHANNEL_ID",
        "CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS",
    ):
        environment.pop(name, None)
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )

    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-File",
            str(package / "watchdog.ps1"),
            "-DryRun",
            "-NoDuo",
        ],
        cwd=package,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["runner"] == "packaged_executable"
    assert "python" not in result.stdout.casefold()


def test_saved_feishu_dpapi_profile_is_reused_for_both_without_secret_output(tmp_path):
    config = tmp_path/"local-app-data/CodexWatchdog"
    config.mkdir(parents=True)
    credential = config/"existing-feishu-credential.clixml"
    fixture = tmp_path/"create-fixture.ps1"
    fixture.write_text("[pscredential]::new('cli_fixture000001', (ConvertTo-SecureString 'fixture-feishu-secret' -AsPlainText -Force)) | Export-Clixml -LiteralPath $args[0]\n")
    subprocess.run(["powershell.exe","-NoProfile","-File",str(fixture),str(credential)],check=True)
    profile = config/"lark-relay.json"
    profile.write_text(json.dumps(dict(schema_version=1, domain="feishu", app_id="cli_fixture000001",
        chat_id="oc_fixture000001", allowed_user_ids=["ou_fixture000001"],
        credential_path=credential.name, interactive_transport="both", future_key="preserve")))
    before = (profile.read_bytes(),credential.read_bytes())
    result = _run_launcher(tmp_path, CODEX_WATCHDOG_SLACK_BOT_TOKEN="xoxb-launcher-fixture",
        CODEX_WATCHDOG_SLACK_APP_TOKEN="xapp-launcher-fixture", CODEX_WATCHDOG_SLACK_CHANNEL_ID="C12345678",
        CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS="U12345678")
    assert result.returncode == 0,result.stderr
    summary=json.loads(result.stdout)
    assert summary["lark"]=="encrypted_store" and summary["lark_domain"]=="feishu"
    assert summary["interactive_transport"]=="both" and summary["slack_reply"]=="environment"
    assert "fixture-feishu-secret" not in result.stdout+result.stderr
    assert (profile.read_bytes(),credential.read_bytes())==before


def test_partial_feishu_environment_fails_without_loading_other_credentials(tmp_path):
    result=_run_launcher(tmp_path, CODEX_WATCHDOG_LARK_APP_SECRET="private-fixture-value")
    assert result.returncode != 0 and "incomplete" in result.stderr
    assert "private-fixture-value" not in result.stdout+result.stderr


@pytest.mark.parametrize("selection", ["onebot", "slack+onebot", "lark+onebot", "all"])
def test_onebot_selection_is_accepted_by_launcher_without_secret_output(tmp_path, selection):
    result = _run_launcher(tmp_path, CODEX_WATCHDOG_INTERACTIVE_TRANSPORT=selection,
        CODEX_WATCHDOG_ONEBOT_ACCESS_TOKEN="onebot-launcher-private-fixture")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["interactive_transport"] == selection
    assert "onebot-launcher-private-fixture" not in result.stdout + result.stderr
