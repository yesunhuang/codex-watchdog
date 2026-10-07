"""Platform-neutral CLI guards without native POSIX migration fixtures."""
import json
import sys

import pytest

from codex_watchdog import cli, linux_relay_migration as migration
from codex_watchdog.control_state import ControlError


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "saved-home"
    home.mkdir()
    (home / "saved-settings.json").write_text('{"existing":"preserve"}\n')
    # These tests exercise CLI dispatch/printing after locality admission;
    # actual native locality and offline migration are separate POSIX gates.
    monkeypatch.setattr("codex_watchdog.linux_binding.locality_identity", lambda: "native-fixture-host")
    return home


def files_bytes(home):
    return {str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()}


def run_cli(home, *actions):
    return cli.main(["--codex-home", str(home), "linux-relay-authority-migrate", *actions])


@pytest.mark.skipif(sys.platform == "linux", reason="checks real unsupported-platform refusal")
def test_real_non_linux_cli_refuses_before_creating_or_reading_migration_state(tmp_path, capsys):
    absent = tmp_path / "unused-home"
    assert run_cli(absent) == 1
    assert json.loads(capsys.readouterr().out) == dict(status="blocked", reason="linux_required")
    assert not absent.exists()


def test_cli_preview_prints_only_summary_without_applying_or_reconfiguring(home, monkeypatch, capsys):
    before = files_bytes(home)
    summary = dict(schema_version=1, records={"slack": 2}, cursor_count=1, plan_sha256="a" * 64)
    monkeypatch.setattr(migration, "plan_authority", lambda _: dict(
        summary=summary, merged={"private": "synthetic-secret-preserve"}))
    monkeypatch.setattr(migration, "apply_authority", lambda *_args, **_kwargs: pytest.fail("preview must not apply"))
    monkeypatch.setattr("codex_watchdog.messaging_setup.prepare_launch",
                        lambda *_args, **_kwargs: pytest.fail("migration must not prepare messaging"))
    assert run_cli(home) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == dict(status="preview", **summary)
    assert "synthetic-secret-preserve" not in output
    assert files_bytes(home) == before


@pytest.mark.parametrize("action", ["--apply", "--rollback"])
def test_cli_mutations_require_explicit_fresh_receipt_without_modifying_state(home, capsys, action):
    before = files_bytes(home)
    assert run_cli(home, action) == 1
    assert json.loads(capsys.readouterr().out) == dict(
        status="blocked", reason="relay_migration_quiescence_required")
    assert files_bytes(home) == before


def test_cli_apply_and_rollback_are_mutually_exclusive_and_never_start_migration(home, monkeypatch, capsys):
    before = files_bytes(home)
    monkeypatch.setattr(migration, "apply_authority", lambda *_args, **_kwargs: pytest.fail("ambiguous action must not apply"))
    monkeypatch.setattr("codex_watchdog.relay_authority_rollback.rollback_authority",
                        lambda *_args, **_kwargs: pytest.fail("ambiguous action must not roll back"))
    assert run_cli(home, "--apply", "--rollback", "--quiescence", str(home / "absent-receipt.json")) == 1
    assert json.loads(capsys.readouterr().out) == dict(
        status="blocked", reason="relay_migration_action_ambiguous")
    assert files_bytes(home) == before


@pytest.mark.parametrize("error,expected", [
    (ControlError("relay_migration_source_changed"), "relay_migration_source_changed"),
    (ValueError("relay_migration_secret=synthetic-secret-preserve"), "linux_setup_failed"),
    (OSError("private-path synthetic-secret-preserve"), "linux_setup_failed"),
])
def test_cli_reports_only_fixed_nonsecret_migration_reasons(home, monkeypatch, capsys, error, expected):
    def fail(_home):
        raise error
    monkeypatch.setattr(migration, "plan_authority", fail)
    before = files_bytes(home)
    assert run_cli(home) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == dict(status="blocked", reason=expected)
    assert "synthetic-secret-preserve" not in output
    assert files_bytes(home) == before
