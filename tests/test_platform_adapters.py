from pathlib import Path
import subprocess

from codex_watchdog.platform_adapters import (
    detect_platform_adapter,
    is_codex_app_server_description,
    run_vscode_status,
)
from codex_watchdog.workspace_discovery import VSCodeWorkspaceDiscovery


def test_platform_adapters_resolve_standard_user_data_roots(tmp_path: Path) -> None:
    windows = detect_platform_adapter(
        system_name="Windows",
        machine="AMD64",
        home=tmp_path / "win-home",
        environment={
            "APPDATA": str(tmp_path / "Roaming"),
            "LOCALAPPDATA": str(tmp_path / "Local"),
        },
        which=lambda _command: None,
    )
    macos = detect_platform_adapter(
        system_name="Darwin",
        machine="arm64",
        home=tmp_path / "mac-home",
        environment={},
        which=lambda _command: None,
    )
    linux = detect_platform_adapter(
        system_name="Linux",
        machine="aarch64",
        home=tmp_path / "linux-home",
        environment={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        which=lambda _command: None,
    )

    assert windows.system == "windows"
    assert windows.architecture == "x86_64"
    assert windows.primary_vscode_user_data_root() == (
        tmp_path / "Roaming" / "Code" / "User"
    ).resolve()
    assert windows.application_data_root() == (
        tmp_path / "Local" / "CodexWatchdog"
    ).resolve()
    assert macos.system == "macos"
    assert macos.architecture == "arm64"
    assert macos.primary_vscode_user_data_root() == (
        tmp_path
        / "mac-home"
        / "Library"
        / "Application Support"
        / "Code"
        / "User"
    ).resolve()
    assert macos.application_data_root() == (
        tmp_path
        / "mac-home"
        / "Library"
        / "Application Support"
        / "CodexWatchdog"
    ).resolve()
    assert linux.system == "linux"
    assert linux.architecture == "arm64"
    assert linux.primary_vscode_user_data_root() == (
        tmp_path / "xdg" / "Code" / "User"
    ).resolve()
    assert linux.application_data_root() == (
        tmp_path / "linux-home" / ".local" / "share" / "codex-watchdog"
    ).resolve()


def test_windows_code_status_uses_cmd_and_scrubs_watchdog_environment(
    tmp_path: Path,
) -> None:
    local = tmp_path / "Local"
    code = local / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
    code.parent.mkdir(parents=True)
    code.write_text("@echo off\n", encoding="utf-8")
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "status-output", "")

    adapter = detect_platform_adapter(
        system_name="nt",
        machine="x64",
        home=tmp_path,
        environment={
            "LOCALAPPDATA": str(local),
            "COMSPEC": str(tmp_path / "cmd.exe"),
            "SAFE_VALUE": "retained",
            "CODEX_WATCHDOG_SECRET": "removed",
        },
        which=lambda _command: None,
    )
    result = run_vscode_status(adapter, runner=runner)

    assert result.status == "available"
    assert result.source == "windows_user_install"
    assert result.output == "status-output"
    assert captured["argv"][-2:] == [str(code.resolve()), "--status"]
    assert captured["argv"][1:4] == ["/d", "/c", "call"]
    assert captured["env"]["SAFE_VALUE"] == "retained"
    assert "CODEX_WATCHDOG_SECRET" not in captured["env"]


def test_posix_code_status_invokes_path_binary_directly(tmp_path: Path) -> None:
    code = tmp_path / "bin" / "code"
    code.parent.mkdir()
    code.write_text("#!/bin/sh\n", encoding="utf-8")
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    adapter = detect_platform_adapter(
        system_name="Linux",
        machine="x86_64",
        home=tmp_path,
        environment={},
        which=lambda command: str(code) if command == "code" else None,
    )
    result = run_vscode_status(adapter, runner=runner)

    assert result.status == "available"
    assert result.source == "path"
    assert captured["argv"] == [str(code.resolve()), "--status"]


def test_code_status_parser_recognizes_windows_linux_and_macos_codex() -> None:
    assert is_codex_app_server_description(
        r"C:\Users\u\.vscode\extensions\openai.chatgpt-1-win32-x64\bin\windows-x86_64\codex.exe -c x app-server"
    )
    assert is_codex_app_server_description(
        "/home/u/.vscode/extensions/openai.chatgpt-1-linux-x64/bin/linux-x86_64/codex app-server"
    )
    assert is_codex_app_server_description(
        "/Users/u/.vscode/extensions/openai.chatgpt-1-darwin-arm64/bin/darwin-arm64/codex -c x app-server"
    )
    assert not is_codex_app_server_description(
        "/home/u/.vscode/extensions/other.extension/bin/codex app-server"
    )


def test_discovery_uses_injected_posix_platform_data_root(tmp_path: Path) -> None:
    linux = detect_platform_adapter(
        system_name="Linux",
        home=tmp_path / "linux-home",
        environment={"XDG_CONFIG_HOME": str(tmp_path / "linux-config")},
        which=lambda _command: None,
    )
    macos = detect_platform_adapter(
        system_name="Darwin",
        home=tmp_path / "mac-home",
        environment={},
        which=lambda _command: None,
    )

    linux_discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "linux-runtime",
        codex_home=tmp_path / "linux-codex",
        platform_adapter=linux,
    )
    macos_discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "mac-runtime",
        codex_home=tmp_path / "mac-codex",
        platform_adapter=macos,
    )

    assert linux_discovery.user_data_root == (
        tmp_path / "linux-config" / "Code" / "User"
    ).resolve()
    assert macos_discovery.user_data_root == (
        tmp_path
        / "mac-home"
        / "Library"
        / "Application Support"
        / "Code"
        / "User"
    ).resolve()
