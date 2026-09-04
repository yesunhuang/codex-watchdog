from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import pytest

from codex_watchdog.git_adapter import GitObservation
from codex_watchdog.models import sha256_text
from codex_watchdog.service import RunOnceService
from codex_watchdog.storage import InstructionStore, StoreBusyError
from codex_watchdog.workspace_registry import WorkspaceRegistry


SESSION_1 = "11111111-2222-4333-8444-555555555555"
SESSION_2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def git_observation(repo_root: Path, observed_at: str = "2026-08-31T00:00:00Z"):
    return GitObservation(
        repo_root=str(repo_root.resolve()),
        status="observed",
        topology="equal",
        branch="main",
        upstream="origin/main",
        head_oid="1" * 40,
        upstream_oid="1" * 40,
        dirty_tracked=False,
        untracked_present=False,
        blockers=(),
        error_sha256=None,
        error_chars=0,
        observed_at=observed_at,
    )


class FakeGitAdapter:
    def __init__(
        self, behavior: Optional[Callable[[Path], GitObservation]] = None
    ) -> None:
        self.behavior = behavior or git_observation
        self.calls = []

    def observe(self, repo_root: Path) -> GitObservation:
        self.calls.append(repo_root)
        return self.behavior(repo_root)


def add_workspaces(tmp_path: Path):
    runtime = tmp_path / "runtime"
    registry = WorkspaceRegistry(runtime)
    z_repo = tmp_path / "z-repo"
    a_repo = tmp_path / "a-repo"
    z_repo.mkdir()
    a_repo.mkdir()
    registry.add("z-workspace", z_repo, SESSION_1)
    registry.add("a:workspace", a_repo, SESSION_2)
    return runtime, registry, a_repo, z_repo


def read_observation(service: RunOnceService, workspace_id: str) -> dict:
    return json.loads(
        service.observation_path(workspace_id).read_text(encoding="utf-8")
    )


def test_run_once_is_sorted_and_persists_hashed_atomic_observations(
    tmp_path: Path,
) -> None:
    runtime, registry, a_repo, z_repo = add_workspaces(tmp_path)
    adapter = FakeGitAdapter()
    service = RunOnceService(runtime, registry=registry, git_adapter=adapter)

    result = service.run_once()

    assert result.ok is True
    assert [item.workspace_id for item in result.workspaces] == [
        "a:workspace",
        "z-workspace",
    ]
    assert adapter.calls == [a_repo.resolve(), z_repo.resolve()]
    for workspace_id, repo in (
        ("a:workspace", a_repo),
        ("z-workspace", z_repo),
    ):
        path = service.observation_path(workspace_id)
        assert path.name == f"{sha256_text(workspace_id)}.json"
        payload = read_observation(service, workspace_id)
        assert payload["schema_version"] == 1
        assert payload["workspace_id"] == workspace_id
        assert payload["repo_root"] == str(repo.resolve())
        assert payload["execution_locality"] == "process_local"
        assert payload["git"]["status"] == "observed"
        assert payload["service_error"] is None
        assert len(payload["transition_fingerprint"]) == 64
    assert not list(service.observations.glob("*.tmp"))


def test_total_automatic_discovery_failure_makes_empty_sensor_fail(
    tmp_path: Path,
) -> None:
    class ErrorCatalog:
        last_snapshot = SimpleNamespace(
            status="error",
            windows=(),
            effective_workspaces=(),
            issues=("vscode_live_status_unavailable",),
        )
        path = tmp_path / "runtime" / "service" / "workspace-discovery.json"

        @staticmethod
        def list_workspaces():
            return []

    result = RunOnceService(
        tmp_path / "runtime", registry=ErrorCatalog(), git_adapter=FakeGitAdapter()
    ).run_once()

    assert result.ok is False
    assert result.to_dict()["status"] == "failed"
    assert result.to_dict()["discovery"]["status"] == "error"


def test_adapter_exception_is_hash_only_and_does_not_stop_next_workspace(
    tmp_path: Path,
) -> None:
    runtime, registry, _a_repo, _z_repo = add_workspaces(tmp_path)
    secret = "sensitive-token-and-path"

    def behavior(repo_root: Path) -> GitObservation:
        if repo_root.name == "a-repo":
            raise RuntimeError(secret)
        return git_observation(repo_root)

    service = RunOnceService(
        runtime, registry=registry, git_adapter=FakeGitAdapter(behavior)
    )

    result = service.run_once()

    assert [item.status for item in result.workspaces] == [
        "adapter_error",
        "persisted",
    ]
    assert result.ok is False
    failed = read_observation(service, "a:workspace")
    succeeded = read_observation(service, "z-workspace")
    assert failed["git"] is None
    assert failed["service_error"]["code"] == "git_adapter_error"
    assert failed["service_error"]["detail_sha256"]
    assert succeeded["git"]["status"] == "observed"
    assert secret not in service.observation_path("a:workspace").read_text(
        encoding="utf-8"
    )
    assert secret not in json.dumps(result.to_dict())


def test_persistence_failure_is_hash_only_and_does_not_stop_next_workspace(
    tmp_path: Path,
) -> None:
    runtime, registry, _a_repo, _z_repo = add_workspaces(tmp_path)
    secret = "private-write-failure"

    def writer(path: Path, value: dict) -> None:
        if value["workspace_id"] == "a:workspace":
            raise OSError(secret)
        InstructionStore._atomic_json(path, value)

    service = RunOnceService(
        runtime, registry=registry, git_adapter=FakeGitAdapter(), atomic_writer=writer,
    )

    result = service.run_once()

    assert [item.status for item in result.workspaces] == [
        "persistence_error",
        "persisted",
    ]
    assert not service.observation_path("a:workspace").exists()
    assert service.observation_path("z-workspace").exists()
    assert result.workspaces[0].error_sha256
    assert result.workspaces[0].transition_fingerprint is None
    assert secret not in json.dumps(result.to_dict())


def test_registered_missing_repo_is_persisted_as_a_git_blocker(tmp_path: Path,) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = WorkspaceRegistry(runtime)
    registry.add("missing-workspace", repo, SESSION_1)
    repo.rmdir()
    service = RunOnceService(runtime, registry=registry)

    result = service.run_once()

    payload = read_observation(service, "missing-workspace")
    assert result.ok is True
    assert result.workspaces[0].status == "persisted"
    assert result.workspaces[0].git_status == "blocked"
    assert payload["git"]["status"] == "blocked"
    assert payload["git"]["blockers"] == ["repo_missing"]


def test_equivalent_git_state_keeps_transition_fingerprint(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = WorkspaceRegistry(runtime)
    registry.add("workspace", repo, SESSION_1)
    observed_times = iter(("2026-08-31T00:00:00Z", "2026-08-31T00:01:00Z"))

    def behavior(repo_root: Path) -> GitObservation:
        return git_observation(repo_root, next(observed_times))

    service = RunOnceService(
        runtime, registry=registry, git_adapter=FakeGitAdapter(behavior)
    )

    first = service.run_once()
    first_payload = read_observation(service, "workspace")
    second = service.run_once()
    second_payload = read_observation(service, "workspace")

    assert first_payload["git"]["observed_at"] != second_payload["git"]["observed_at"]
    assert (
        first.workspaces[0].transition_fingerprint
        == second.workspaces[0].transition_fingerprint
    )
    assert (
        first_payload["transition_fingerprint"]
        == second_payload["transition_fingerprint"]
    )


def test_service_lock_prevents_an_overlapping_run(tmp_path: Path) -> None:
    runtime, registry, _a_repo, _z_repo = add_workspaces(tmp_path)
    adapter = FakeGitAdapter()
    service = RunOnceService(runtime, registry=registry, git_adapter=adapter)

    with service.service_lock():
        with pytest.raises(StoreBusyError):
            service.run_once()

    assert adapter.calls == []
