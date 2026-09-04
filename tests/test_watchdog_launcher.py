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
