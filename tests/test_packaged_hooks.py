from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_watchdog import cli
from codex_watchdog.hook_config import build_packaged_hooks_document


def _fake_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "codex-watchdog.exe"
    executable.write_bytes(b"test executable placeholder")
    return executable


def test_packaged_hook_invokes_only_the_executable(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    runtime = tmp_path / "runtime"

    document = build_packaged_hooks_document(executable, runtime, 30, 0.1, True)

    stop = document["hooks"]["Stop"][0]["hooks"][0]
    assert stop["commandWindows"].startswith(str(executable.resolve()))
    assert "python" not in stop["commandWindows"].casefold()
    assert "codex_watchdog_hook.py" not in stop["commandWindows"]
    assert f"--runtime {runtime.resolve()} hook" in stop["commandWindows"]
    assert stop["commandWindows"].endswith("--test-mode")


def test_packaged_hook_cli_renders_and_installs_idempotently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    executable = _fake_executable(tmp_path)
    runtime = tmp_path / "runtime"
    codex_home = tmp_path / "codex-home"
    arguments = [
        "--runtime",
        str(runtime),
        "--codex-home",
        str(codex_home),
        "install-user-hooks",
        "--executable",
        str(executable),
    ]

    assert cli.main(arguments) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["hooks"]["Stop"]

    assert cli.main([*arguments, "--install"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "installed"
    assert first["trust"] == "manual_review_required"

    assert cli.main([*arguments, "--install"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "unchanged"
    assert second["sha256"] == first["sha256"]
