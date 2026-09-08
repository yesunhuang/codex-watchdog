import json
from pathlib import Path
import sqlite3

from codex_watchdog import cli
from codex_watchdog.doctor import DoctorCheck, DoctorReport, WatchdogDoctor
from codex_watchdog.platform_adapters import VSCodeStatusProbe, detect_platform_adapter


def _queue_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE queued_items "
            "(id TEXT, thread_id TEXT, payload_json TEXT)"
        )
        connection.execute(
            "CREATE TABLE queued_thread_revisions (thread_id TEXT, revision INTEGER)"
        )


def _windows_fixture(tmp_path: Path):
    home = tmp_path / "private-user-home"
    appdata = tmp_path / "Roaming"
    local = tmp_path / "Local"
    user_data = appdata / "Code" / "User"
    (user_data / "globalStorage").mkdir(parents=True)
    (user_data / "globalStorage" / "storage.json").write_text(
        json.dumps({"windowsState": {"openedWindows": []}}), encoding="utf-8"
    )
    workspace = user_data / "workspaceStorage" / "opaque-entry"
    workspace.mkdir(parents=True)
    (workspace / "state.vscdb").write_bytes(b"diagnostic")

    code = local / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
    code.parent.mkdir(parents=True)
    code.write_text("@echo off\n", encoding="utf-8")
    codex = (
        home
        / ".vscode"
        / "extensions"
        / "openai.chatgpt-1-win32-x64"
        / "bin"
        / "windows-x86_64"
        / "codex.exe"
    )
    codex.parent.mkdir(parents=True)
    codex.write_bytes(b"binary")

    codex_home = home / ".codex"
    (codex_home / "sessions").mkdir(parents=True)
    (codex_home / "state_5.sqlite").write_bytes(b"state")
    _queue_database(codex_home / "queue_1.sqlite")
    (codex_home / "hooks.json").write_text(
        json.dumps({"command": "codex-watchdog hook"}), encoding="utf-8"
    )
    adapter = detect_platform_adapter(
        system_name="Windows",
        machine="AMD64",
        home=home,
        environment={
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(local),
            "COMSPEC": str(tmp_path / "cmd.exe"),
        },
        which=lambda _command: None,
    )
    return adapter, codex_home, user_data


def test_doctor_report_is_bounded_privacy_safe_and_read_only(tmp_path: Path) -> None:
    adapter, codex_home, user_data = _windows_fixture(tmp_path)
    runtime = tmp_path / "runtime-must-not-be-created"
    report = WatchdogDoctor(
        runtime,
        codex_home=codex_home,
        user_data_root=user_data,
        adapter=adapter,
        status_probe=lambda _adapter: VSCodeStatusProbe(
            "available", "vscode_status_available", "test", "no-live-windows"
        ),
    ).run()

    assert report.status == "PARTIAL"
    assert not runtime.exists()
    names = {check.name for check in report.checks}
    assert {
        "platform",
        "vscode_cli",
        "vscode_user_data",
        "workspace_storage",
        "codex_extension",
        "codex_executable",
        "codex_home",
        "vscode_live_status",
        "live_window_discovery",
        "current_thread_resolution",
        "queue_wake",
        "codex_hooks",
        "credential_storage",
        "launcher",
        "filesystem_primitives",
    } == names
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert str(tmp_path) not in encoded
    assert "private-user-home" not in encoded
    assert "opaque-entry" not in encoded
    assert "codex-watchdog hook" not in encoded


def test_doctor_export_round_trips_only_redacted_report(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    report = DoctorReport(
        "PARTIAL",
        "macos",
        "arm64",
        "preview_ci",
        (DoctorCheck("platform", "PASS", "platform_supported", {}),),
        "2026-09-06T00:00:00Z",
    )

    class FakeDoctor:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            return report

    monkeypatch.setattr(cli, "WatchdogDoctor", FakeDoctor)
    export = tmp_path / "safe-doctor.json"

    assert cli.main(["doctor", "--export", str(export)]) == 0
    assert json.loads(export.read_text(encoding="utf-8")) == report.to_dict()
    assert json.loads(capsys.readouterr().out) == report.to_dict()


def test_linux_doctor_identifies_preview_security_and_launcher_boundaries(
    tmp_path: Path,
) -> None:
    adapter = detect_platform_adapter(
        system_name="Linux",
        machine="aarch64",
        home=tmp_path,
        environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
        which=lambda _command: None,
    )
    report = WatchdogDoctor(
        tmp_path / "runtime",
        adapter=adapter,
        status_probe=lambda _adapter: VSCodeStatusProbe(
            "unavailable", "vscode_cli_unavailable"
        ),
    ).run()
    checks = {check.name: check for check in report.checks}

    assert report.platform == "linux"
    assert report.architecture == "arm64"
    assert report.support_tier == "preview_ci"
    assert checks["credential_storage"].status == "PARTIAL"
    assert checks["credential_storage"].reason == "linux_libsecret_required"
    assert checks["launcher"].status == "PARTIAL"
    assert checks["launcher"].reason == "foreground_cli_only"


def test_macos_doctor_reports_native_keychain_launcher_capability(
    tmp_path: Path,
) -> None:
    adapter = detect_platform_adapter(
        system_name="Darwin",
        machine="arm64",
        home=tmp_path,
        environment={},
        which=lambda _command: None,
    )
    report = WatchdogDoctor(
        tmp_path / "runtime",
        adapter=adapter,
        status_probe=lambda _adapter: VSCodeStatusProbe(
            "unavailable", "vscode_cli_unavailable"
        ),
    ).run()
    checks = {check.name: check for check in report.checks}

    assert report.platform == "macos"
    assert report.architecture == "arm64"
    assert report.support_tier == "preview_native_e2e"
    assert checks["credential_storage"].status == "PASS"
    assert checks["credential_storage"].reason == "macos_keychain_slack"
    assert checks["launcher"].status == "PASS"
    assert checks["launcher"].reason == "macos_keychain_foreground"
