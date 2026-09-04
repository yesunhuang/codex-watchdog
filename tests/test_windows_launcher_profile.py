from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from codex_watchdog.windows_launcher import (
    WindowsLauncherError,
    launch_one_click,
    packaged_cli_arguments,
    prepare_one_click_launch,
)


def _environment(tmp_path: Path) -> dict[str, str]:
    system_root = tmp_path / "Windows"
    powershell = (
        system_root
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"placeholder")
    return {
        "LOCALAPPDATA": str(tmp_path / "local-app-data"),
        "USERPROFILE": str(tmp_path / "user"),
        "SystemRoot": str(system_root),
    }


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "codex-watchdog-v0.2.0-windows-x64"
    package.mkdir()
    executable = package / "codex-watchdog.exe"
    executable.write_bytes(b"placeholder")
    (package / "watchdog.ps1").write_text("# placeholder\n", encoding="utf-8")
    return executable


def test_upgrade_recovers_v010_hook_runtime_and_persists_versioned_profile(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    executable = _package(tmp_path)
    runtime = tmp_path / "existing-runtime"
    runtime.mkdir()
    codex_home = Path(environment["USERPROFILE"]) / ".codex"
    codex_home.mkdir(parents=True)
    hooks = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "commandWindows": (
                                'D:\\old-release\\codex-watchdog.exe '
                                f'--runtime "{runtime}" hook --grace-seconds 30'
                            )
                        }
                    ]
                }
            ]
        }
    }
    (codex_home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    resolution = prepare_one_click_launch(executable, environment)

    assert resolution.runtime == runtime.resolve()
    assert resolution.source == "codex_hooks"
    profile = json.loads(resolution.profile_path.read_text(encoding="utf-8"))
    assert profile["schema_version"] == 1
    assert Path(profile["runtime_path"]) == runtime.resolve()
    assert profile["discovered_from"] == "codex_hooks"


def test_existing_profile_is_reused_without_normalizing_unknown_keys(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    executable = _package(tmp_path)
    runtime = tmp_path / "existing-runtime"
    runtime.mkdir()
    profile_path = Path(environment["LOCALAPPDATA"]) / "CodexWatchdog" / "launcher-profile.json"
    profile_path.parent.mkdir(parents=True)
    original = {
        "schema_version": 1,
        "runtime_path": str(runtime.resolve()),
        "future_nonconflicting_key": {"preserve": True},
    }
    profile_path.write_text(json.dumps(original, indent=2), encoding="utf-8")
    before = profile_path.read_bytes()

    resolution = prepare_one_click_launch(executable, environment)

    assert resolution.source == "saved_profile"
    assert resolution.runtime == runtime.resolve()
    assert profile_path.read_bytes() == before


def test_missing_saved_runtime_fails_closed_without_replacing_profile(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    executable = _package(tmp_path)
    profile_path = Path(environment["LOCALAPPDATA"]) / "CodexWatchdog" / "launcher-profile.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        json.dumps(
            {"schema_version": 1, "runtime_path": str(tmp_path / "missing-runtime")}
        ),
        encoding="utf-8",
    )
    before = profile_path.read_bytes()

    with pytest.raises(WindowsLauncherError, match="no longer exists"):
        prepare_one_click_launch(executable, environment)

    assert profile_path.read_bytes() == before
    assert not (tmp_path / "missing-runtime").exists()


def test_fresh_install_uses_stable_current_user_runtime(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    executable = _package(tmp_path)

    resolution = prepare_one_click_launch(executable, environment)

    assert resolution.runtime == (
        Path(environment["LOCALAPPDATA"]) / "CodexWatchdog" / "runtime"
    ).resolve()
    assert resolution.source == "new_current_user_runtime"


def test_one_click_invokes_bundled_bootstrap_with_resolved_runtime(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    executable = _package(tmp_path)
    runtime = tmp_path / "existing-runtime"
    runtime.mkdir()
    environment["CODEX_WATCHDOG_RUNTIME"] = str(runtime)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return 0

    stdout = io.StringIO()
    assert (
        launch_one_click(
            executable, environment=environment, runner=runner, stdout=stdout
        )
        == 0
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[-2:] == ["-Runtime", str(runtime.resolve())]
    assert command[command.index("-File") + 1] == str(executable.parent / "watchdog.ps1")
    assert kwargs["cwd"] == str(executable.parent.resolve())
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "existing-runtime" in stdout.getvalue()


def test_packaged_cli_automatically_reuses_saved_runtime(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    executable = _package(tmp_path)
    runtime = tmp_path / "existing-runtime"
    runtime.mkdir()
    environment["CODEX_WATCHDOG_RUNTIME"] = str(runtime)

    arguments = packaged_cli_arguments(
        ("install-user-hooks",), executable, environment
    )

    assert arguments == (
        "--runtime",
        str(runtime.resolve()),
        "install-user-hooks",
    )


def test_packaged_cli_preserves_explicit_runtime_and_help(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    executable = _package(tmp_path)

    assert packaged_cli_arguments(
        ("--runtime", "D:\\explicit", "run"), executable, environment
    ) == ("--runtime", "D:\\explicit", "run")
    assert packaged_cli_arguments(("--help",), executable, environment) == ("--help",)
    assert not (
        Path(environment["LOCALAPPDATA"]) / "CodexWatchdog" / "launcher-profile.json"
    ).exists()
