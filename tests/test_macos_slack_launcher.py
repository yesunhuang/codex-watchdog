from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX launcher tests require native shebang execution and file modes",
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP = REPO_ROOT / "setup-slack-relay-macos.sh"
LAUNCHER = REPO_ROOT / "watchdog-macos.sh"


def _base_environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("CODEX_WATCHDOG_SLACK_") or name.startswith(
            "CODEX_WATCHDOG_SMTP_"
        ):
            environment.pop(name)
    environment.pop("CODEX_WATCHDOG_OUTLOOK_CLIENT_ID", None)
    environment.update(
        {
            "CODEX_WATCHDOG_MACOS_TEST": "1",
            "CODEX_WATCHDOG_MACOS_CONFIG_DIR": str(tmp_path / "config"),
            "CODEX_WATCHDOG_PYTHON": sys.executable,
        }
    )
    return environment


def _fake_security(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "security"
    log = tmp_path / "security-argv.log"
    executable.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$CODEX_WATCHDOG_SECURITY_LOG"
command_name=${1:-}
account=""
while (($#)); do
    if [[ $1 == "-a" && $# -ge 2 ]]; then
        account=$2
        shift 2
    else
        shift
    fi
done
if [[ $command_name == "find-generic-password" ]]; then
    case "$account" in
        slack-bot-token) printf '%s\\n' 'xoxb-test-secret' ;;
        slack-app-token) printf '%s\\n' 'xapp-test-secret' ;;
        *) exit 44 ;;
    esac
fi
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable, log


def _write_config(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channel_id": "G12345678",
                "allowed_user_ids": ["U12345678"],
            }
        ),
        encoding="utf-8",
    )


def test_macos_launcher_loads_keychain_and_disables_email(tmp_path: Path) -> None:
    security, log = _fake_security(tmp_path)
    environment = _base_environment(tmp_path)
    environment.update(
        {
            "CODEX_WATCHDOG_MACOS_SECURITY_BIN": str(security),
            "CODEX_WATCHDOG_SECURITY_LOG": str(log),
            "CODEX_WATCHDOG_SMTP_HOST": "smtp.invalid",
            "CODEX_WATCHDOG_SMTP_FROM": "sender@example.invalid",
            "CODEX_WATCHDOG_SMTP_TO": "recipient@example.invalid",
            "CODEX_WATCHDOG_SMTP_PASSWORD": "email-secret",
        }
    )
    _write_config(tmp_path / "config" / "slack-relay.json")

    result = subprocess.run(
        [
            str(LAUNCHER),
            "--runtime",
            str(tmp_path / "runtime"),
            "--slack-only",
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary == {
        "runtime": str(tmp_path / "runtime"),
        "slack_only": True,
        "slack_reply": "macos_keychain",
        "smtp_configured": False,
        "status": "ready",
    }
    combined = result.stdout + result.stderr + log.read_text(encoding="utf-8")
    assert "xoxb-test-secret" not in combined
    assert "xapp-test-secret" not in combined
    assert "email-secret" not in combined


def test_macos_launcher_rejects_partial_relay_configuration(tmp_path: Path) -> None:
    environment = _base_environment(tmp_path)
    environment.update(
        {
            "CODEX_WATCHDOG_MACOS_SECURITY_BIN": "/usr/bin/false",
            "CODEX_WATCHDOG_SLACK_CHANNEL_ID": "G12345678",
        }
    )

    result = subprocess.run(
        [str(LAUNCHER), "--dry-run"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "configuration is incomplete" in result.stderr


def test_macos_setup_uses_keychain_prompts_and_writes_nonsecret_ids(
    tmp_path: Path,
) -> None:
    security, log = _fake_security(tmp_path)
    environment = _base_environment(tmp_path)
    environment.update(
        {
            "CODEX_WATCHDOG_MACOS_SECURITY_BIN": str(security),
            "CODEX_WATCHDOG_SECURITY_LOG": str(log),
        }
    )

    result = subprocess.run(
        [
            str(SETUP),
            "--channel-id",
            "G12345678",
            "--allowed-user-id",
            "U12345678",
            "--allowed-user-id",
            "U12345678",
            "--force",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary == {
        "status": "saved",
        "allowed_user_count": 1,
        "secret_protection": "macos_keychain_current_user",
    }
    config_path = tmp_path / "config" / "slack-relay.json"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "channel_id": "G12345678",
        "allowed_user_ids": ["U12345678"],
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    combined = result.stdout + result.stderr + log.read_text(encoding="utf-8")
    assert "xoxb-test-secret" not in combined
    assert "xapp-test-secret" not in combined
    add_lines = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("add-generic-password ")
    ]
    assert len(add_lines) == 2
    assert all(line.endswith(" -w") for line in add_lines)


def test_macos_setup_rejects_direct_message_channel(tmp_path: Path) -> None:
    environment = _base_environment(tmp_path)
    environment["CODEX_WATCHDOG_MACOS_SECURITY_BIN"] = "/usr/bin/false"

    result = subprocess.run(
        [
            str(SETUP),
            "--channel-id",
            "D12345678",
            "--allowed-user-id",
            "U12345678",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "must begin with C or G" in result.stderr
