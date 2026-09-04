from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_watchdog.workspace_registry import (
    PROCESS_LOCAL,
    WorkspaceCollisionError,
    WorkspaceRegistry,
    WorkspaceRegistryFormatError,
)


SESSION_1 = "11111111-2222-4333-8444-555555555555"
SESSION_2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    return repo


def test_add_writes_schema_one_registry_with_canonical_local_path(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    registry = WorkspaceRegistry(tmp_path / "runtime")

    result = registry.add("workspace-1", repo / ".", SESSION_1.upper())

    assert result.status == "created"
    assert result.path == registry.path
    assert result.workspace.repo_root == repo.resolve()
    assert result.workspace.session_id == SESSION_1
    assert result.workspace.execution_locality == PROCESS_LOCAL
    value = json.loads(registry.path.read_text(encoding="utf-8"))
    assert value["schema_version"] == 1
    assert value["workspaces"] == [result.workspace.to_dict()]


def test_exact_add_is_idempotent_without_rewriting_registration(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    registry = WorkspaceRegistry(tmp_path / "runtime")
    first = registry.add("workspace-1", repo, SESSION_1)
    original = registry.path.read_bytes()

    second = registry.add("workspace-1", repo / ".", SESSION_1.upper())

    assert second.status == "existing"
    assert second.workspace == first.workspace
    assert registry.path.read_bytes() == original


@pytest.mark.parametrize("changed_field", ["path", "session"])
def test_workspace_id_reuse_with_changed_metadata_is_a_collision(
    tmp_path: Path, changed_field: str
) -> None:
    first_repo = make_repo(tmp_path, "first")
    second_repo = make_repo(tmp_path, "second")
    registry = WorkspaceRegistry(tmp_path / "runtime")
    registry.add("workspace-1", first_repo, SESSION_1)

    with pytest.raises(WorkspaceCollisionError, match="different metadata"):
        registry.add(
            "workspace-1",
            second_repo if changed_field == "path" else first_repo,
            SESSION_2 if changed_field == "session" else SESSION_1,
        )

    assert [item.workspace_id for item in registry.list_workspaces()] == ["workspace-1"]


def test_same_canonical_repo_cannot_be_registered_under_two_ids(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    registry = WorkspaceRegistry(tmp_path / "runtime")
    registry.add("workspace-1", repo, SESSION_1)

    with pytest.raises(WorkspaceCollisionError, match="already registered"):
        registry.add("workspace-2", repo / ".", SESSION_2)


def test_list_and_durable_json_are_sorted_by_workspace_id(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "runtime")
    registry.add("z-workspace", make_repo(tmp_path, "z-repo"), SESSION_1)
    registry.add("a-workspace", make_repo(tmp_path, "a-repo"), SESSION_2)

    assert [item.workspace_id for item in registry.list_workspaces()] == [
        "a-workspace",
        "z-workspace",
    ]
    value = json.loads(registry.path.read_text(encoding="utf-8"))
    assert [item["workspace_id"] for item in value["workspaces"]] == [
        "a-workspace",
        "z-workspace",
    ]
    assert registry.get("a-workspace").repo_root == (tmp_path / "a-repo").resolve()


def test_remove_deletes_only_the_selected_manual_override(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "runtime")
    first = registry.add("first", make_repo(tmp_path, "first"), SESSION_1).workspace
    second = registry.add("second", make_repo(tmp_path, "second"), SESSION_2).workspace

    result = registry.remove("first")

    assert result.status == "removed"
    assert result.workspace == first
    assert registry.list_workspaces() == [second]
    durable = json.loads(registry.path.read_text(encoding="utf-8"))
    assert durable["workspaces"] == [second.to_dict()]


def test_remove_missing_override_is_idempotent_and_does_not_create_file(
    tmp_path: Path,
) -> None:
    registry = WorkspaceRegistry(tmp_path / "runtime")

    result = registry.remove("missing")

    assert result.status == "missing"
    assert result.workspace is None
    assert not registry.path.exists()


def test_only_process_local_execution_is_accepted(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "runtime")

    with pytest.raises(ValueError, match="process_local"):
        registry.add(
            "remote-workspace",
            make_repo(tmp_path),
            SESSION_1,
            execution_locality="remote_ssh:gpu_lab",
        )

    assert not registry.path.exists()


@pytest.mark.parametrize(
    "nonlocal_path",
    [
        "vscode-remote://ssh-remote+gpu_lab/home/user/repo",
        "ssh://gpu_lab/home/user/repo",
        "user@gpu_lab:/home/user/repo",
        r"\\server\share\repo",
        "//server/share/repo",
    ],
)
def test_uri_and_unc_repository_paths_are_rejected(
    tmp_path: Path, nonlocal_path: str
) -> None:
    registry = WorkspaceRegistry(tmp_path / "runtime")

    with pytest.raises(ValueError, match="process-local"):
        registry.add("workspace-1", nonlocal_path, SESSION_1)

    assert not registry.path.exists()


def test_nonexistent_local_path_is_rejected(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "runtime")

    with pytest.raises(ValueError, match="valid local path"):
        registry.add("workspace-1", tmp_path / "missing", SESSION_1)


def test_malformed_registry_fails_closed_and_is_not_overwritten(
    tmp_path: Path,
) -> None:
    registry = WorkspaceRegistry(tmp_path / "runtime")
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text("{not-json", encoding="utf-8")
    original = registry.path.read_bytes()

    with pytest.raises(WorkspaceRegistryFormatError, match="cannot be read"):
        registry.add("workspace-1", make_repo(tmp_path), SESSION_1)

    assert registry.path.read_bytes() == original


@pytest.mark.parametrize(
    "value",
    [
        {"schema_version": 2, "workspaces": []},
        {"schema_version": True, "workspaces": []},
        {"schema_version": 1, "workspaces": {}, "extra": True},
        {"schema_version": 1, "workspaces": [{"workspace_id": "incomplete"}]},
    ],
)
def test_invalid_schema_or_shape_fails_closed(tmp_path: Path, value: dict) -> None:
    registry = WorkspaceRegistry(tmp_path / "runtime")
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(WorkspaceRegistryFormatError):
        registry.list_workspaces()


def test_registered_missing_repo_remains_visible_for_blocked_reporting(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    registry = WorkspaceRegistry(tmp_path / "runtime")
    registry.add("workspace-1", repo, SESSION_1)
    repo.rmdir()

    workspaces = registry.list_workspaces()

    assert len(workspaces) == 1
    assert workspaces[0].repo_root == repo.resolve()
    assert not workspaces[0].repo_root.exists()
