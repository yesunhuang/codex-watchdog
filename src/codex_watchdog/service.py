from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .git_adapter import GitObservation, LocalGitAdapter
from .models import sha256_text, utc_now
from .storage import FileLock, InstructionStore
from .workspace_discovery import EffectiveWorkspaceCatalog
from .workspace_registry import TrackedWorkspace, WorkspaceRegistry


SERVICE_OBSERVATION_SCHEMA_VERSION = 1

AtomicWriter = Callable[[Path, Dict[str, Any]], None]


@dataclass(frozen=True)
class WorkspaceRunResult:
    workspace_id: str
    status: str
    observation_path: Path
    transition_fingerprint: Optional[str]
    git_status: Optional[str]
    blockers: Tuple[str, ...]
    error_sha256: Optional[str]
    error_chars: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "status": self.status,
            "observation_path": str(self.observation_path),
            "transition_fingerprint": self.transition_fingerprint,
            "git_status": self.git_status,
            "blockers": list(self.blockers),
            "error_sha256": self.error_sha256,
            "error_chars": self.error_chars,
        }


@dataclass(frozen=True)
class RunOnceResult:
    workspaces: Tuple[WorkspaceRunResult, ...]
    discovery: Optional[Dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return (
            self.discovery is None or self.discovery.get("status") != "error"
        ) and all(workspace.status == "persisted" for workspace in self.workspaces)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": "ok" if self.ok else "failed",
            "workspace_count": len(self.workspaces),
            "workspaces": [workspace.to_dict() for workspace in self.workspaces],
            "discovery": self.discovery,
        }


class RunOnceService:
    """Take one deterministic, locality-bound Git sensor snapshot."""

    def __init__(
        self,
        runtime: Path,
        registry: Optional[WorkspaceRegistry] = None,
        git_adapter: Optional[LocalGitAdapter] = None,
        atomic_writer: Optional[AtomicWriter] = None,
    ) -> None:
        self.runtime = Path(runtime)
        self.registry = (
            registry
            if registry is not None
            else EffectiveWorkspaceCatalog(self.runtime)
        )
        self.git_adapter = git_adapter if git_adapter is not None else LocalGitAdapter()
        self.atomic_writer = (
            atomic_writer
            if atomic_writer is not None
            else InstructionStore._atomic_json
        )
        self.observations = self.runtime / "service" / "observations"
        self.lock_path = self.runtime / "locks" / "service-once.lock"

    def service_lock(self) -> FileLock:
        return FileLock(self.lock_path)

    def observation_path(self, workspace_id: str) -> Path:
        digest = sha256_text(workspace_id)
        return self.observations / f"{digest}.json"

    def run_once(self) -> RunOnceResult:
        with self.service_lock():
            workspaces = sorted(
                self.registry.list_workspaces(), key=lambda item: item.workspace_id
            )
            results = tuple(
                self._observe_workspace(workspace) for workspace in workspaces
            )
            snapshot = getattr(self.registry, "last_snapshot", None)
            discovery = (
                {
                    "status": snapshot.status,
                    "window_count": len(snapshot.windows),
                    "tracked_workspace_count": len(snapshot.effective_workspaces),
                    "issues": list(snapshot.issues),
                    "path": str(getattr(self.registry, "path", "")) or None,
                }
                if snapshot is not None
                else None
            )
        return RunOnceResult(results, discovery)

    def _observe_workspace(self, workspace: TrackedWorkspace) -> WorkspaceRunResult:
        observation: Optional[GitObservation]
        service_error: Optional[Dict[str, Any]]
        status: str
        try:
            observation = self.git_adapter.observe(workspace.repo_root)
            if not isinstance(observation, GitObservation):
                raise TypeError("Git adapter returned an invalid observation")
            service_error = None
            status = "persisted"
        except Exception as exc:
            observation = None
            service_error = self._error_summary("git_adapter_error", exc)
            status = "adapter_error"

        transition_fingerprint = self._transition_fingerprint(
            workspace, observation, service_error
        )
        path = self.observation_path(workspace.workspace_id)
        payload = {
            "schema_version": SERVICE_OBSERVATION_SCHEMA_VERSION,
            "workspace_id": workspace.workspace_id,
            "repo_root": str(workspace.repo_root),
            "session_id": workspace.session_id,
            "execution_locality": workspace.execution_locality,
            "git": observation.to_dict() if observation is not None else None,
            "service_error": service_error,
            "transition_fingerprint": transition_fingerprint,
            "recorded_at": utc_now(),
        }
        try:
            self.atomic_writer(path, payload)
        except Exception as exc:
            write_error = self._error_summary("observation_write_failed", exc)
            return WorkspaceRunResult(
                workspace_id=workspace.workspace_id,
                status="persistence_error",
                observation_path=path,
                transition_fingerprint=None,
                git_status=observation.status if observation is not None else None,
                blockers=("observation_write_failed",),
                error_sha256=write_error["detail_sha256"],
                error_chars=write_error["detail_chars"],
            )

        if observation is not None:
            blockers = observation.blockers
            git_status = observation.status
            error_sha256 = observation.error_sha256
            error_chars = observation.error_chars
        else:
            assert service_error is not None
            blockers = (service_error["code"],)
            git_status = None
            error_sha256 = service_error["detail_sha256"]
            error_chars = service_error["detail_chars"]
        return WorkspaceRunResult(
            workspace_id=workspace.workspace_id,
            status=status,
            observation_path=path,
            transition_fingerprint=transition_fingerprint,
            git_status=git_status,
            blockers=blockers,
            error_sha256=error_sha256,
            error_chars=error_chars,
        )

    @staticmethod
    def _transition_fingerprint(
        workspace: TrackedWorkspace,
        observation: Optional[GitObservation],
        service_error: Optional[Dict[str, Any]],
    ) -> str:
        stable = {
            "schema_version": SERVICE_OBSERVATION_SCHEMA_VERSION,
            "workspace_id": workspace.workspace_id,
            "repo_root": str(workspace.repo_root),
            "session_id": workspace.session_id,
            "execution_locality": workspace.execution_locality,
            "git_transition_fingerprint": (
                observation.transition_fingerprint()
                if observation is not None
                else None
            ),
            "service_error": service_error,
        }
        return sha256_text(json.dumps(stable, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _error_summary(code: str, error: Exception) -> Dict[str, Any]:
        error_type = f"{type(error).__module__}.{type(error).__qualname__}"
        try:
            message = str(error)
        except Exception:
            message = ""
        detail = f"{error_type}\0{message}"
        return {
            "code": code,
            "detail_sha256": sha256_text(detail),
            "detail_chars": len(detail),
        }
