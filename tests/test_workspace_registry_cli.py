from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_watchdog import cli
from codex_watchdog.workspace_registry import WorkspaceRegistry


SESSION_1 = "11111111-2222-4333-8444-555555555555"
SESSION_2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def run_cli(arguments, capsys: pytest.CaptureFixture[str]):
    assert cli.main(arguments) == 0
    output = capsys.readouterr()
    assert output.err == ""
    return output.out, json.loads(output.out)


def test_workspace_add_emits_stable_json_and_persists_registration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo with spaces"
    repo.mkdir()

    raw, value = run_cli(
        [
            "--runtime",
            str(runtime),
            "workspace-add",
            "--workspace",
            "workspace-1",
            "--repo",
            str(repo),
            "--thread",
            SESSION_1,
        ],
        capsys,
    )

    stored = WorkspaceRegistry(runtime).get("workspace-1")
    expected = {"status": "created", "workspace": stored.to_dict()}
    assert value == expected
    assert raw == json.dumps(expected, ensure_ascii=False, sort_keys=True) + "\n"


def test_workspace_add_exact_repeat_reports_existing_registration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    arguments = [
        "--runtime",
        str(runtime),
        "workspace-add",
        "--workspace",
        "workspace-1",
        "--repo",
        str(repo),
        "--thread",
        SESSION_1,
    ]
    run_cli(arguments, capsys)

    _, value = run_cli(arguments, capsys)

    assert value["status"] == "existing"
    assert value["workspace"] == WorkspaceRegistry(runtime).get("workspace-1").to_dict()


def test_workspace_list_emits_deterministically_sorted_schema_one_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime"
    registry = WorkspaceRegistry(runtime)
    z_repo = tmp_path / "z-repo"
    a_repo = tmp_path / "a-repo"
    z_repo.mkdir()
    a_repo.mkdir()
    registry.add("z-workspace", z_repo, SESSION_1)
    registry.add("a-workspace", a_repo, SESSION_2)

    raw, value = run_cli(["--runtime", str(runtime), "workspace-list"], capsys)

    expected = {
        "schema_version": 1,
        "workspaces": [workspace.to_dict() for workspace in registry.list_workspaces()],
    }
    assert value == expected
    assert [item["workspace_id"] for item in value["workspaces"]] == [
        "a-workspace",
        "z-workspace",
    ]
    assert raw == json.dumps(expected, ensure_ascii=False, sort_keys=True) + "\n"


def test_workspace_list_without_registry_emits_empty_schema_one_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw, value = run_cli(
        ["--runtime", str(tmp_path / "runtime"), "workspace-list"], capsys
    )

    expected = {"schema_version": 1, "workspaces": []}
    assert value == expected
    assert raw == json.dumps(expected, ensure_ascii=False, sort_keys=True) + "\n"


def test_workspace_add_rejects_remote_uri_before_writing_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime"

    with pytest.raises(ValueError, match="process-local"):
        cli.main(
            [
                "--runtime",
                str(runtime),
                "workspace-add",
                "--workspace",
                "remote-workspace",
                "--repo",
                "vscode-remote://ssh-remote+gpu_lab/home/user/repo",
                "--thread",
                SESSION_1,
            ]
        )

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""
    assert not WorkspaceRegistry(runtime).path.exists()


def test_workspace_commands_do_not_construct_queue_dispatcher(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_dispatcher(*_args, **_kwargs):
        raise AssertionError("workspace commands must not initialize queue state")

    monkeypatch.setattr(cli, "QueueWakeDispatcher", unexpected_dispatcher)

    _, value = run_cli(
        ["--runtime", str(tmp_path / "runtime"), "workspace-list"], capsys
    )

    assert value == {"schema_version": 1, "workspaces": []}
