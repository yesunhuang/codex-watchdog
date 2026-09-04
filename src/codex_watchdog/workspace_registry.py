from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union
import uuid

from .models import utc_now, validate_instruction_id
from .storage import FileLock, InstructionStore


REGISTRY_SCHEMA_VERSION = 1
PROCESS_LOCAL = "process_local"

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SCP_STYLE_PATH = re.compile(r"^[^/\\:]+:.+")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_ENTRY_KEYS = frozenset(
    {"workspace_id", "repo_root", "session_id", "execution_locality", "registered_at",}
)
_REGISTRY_KEYS = frozenset({"schema_version", "workspaces"})


class WorkspaceRegistryError(RuntimeError):
    """Base error for registry content that must not be acted upon."""


class WorkspaceRegistryFormatError(WorkspaceRegistryError):
    """The durable registry is malformed or uses an unsupported schema."""


class WorkspaceCollisionError(WorkspaceRegistryError):
    """A workspace identity or repository root was reused inconsistently."""


def _canonical_session_id(value: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ValueError("session id must be a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("session id must be a UUID") from exc


def _reject_nonlocal_path(value: str) -> None:
    normalized = value.replace("\\", "/")
    lowered = normalized.lower()
    if (
        "://" in lowered
        or lowered.startswith("vscode-remote:")
        or lowered.startswith("ssh:")
        or value.startswith("\\\\")
        or normalized.startswith("//")
        or (
            _SCP_STYLE_PATH.match(value) is not None
            and _WINDOWS_ABSOLUTE_PATH.match(value) is None
        )
    ):
        raise ValueError("repository root must be a process-local filesystem path")


def _canonical_repo_root(
    value: Union[str, Path], *, require_exists: bool, require_stored_form: bool
) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw:
        raise ValueError("repository root must be a non-empty filesystem path")
    _reject_nonlocal_path(raw)
    try:
        path = Path(raw).expanduser()
        if require_stored_form and not path.is_absolute():
            raise ValueError("stored repository root must be absolute")
        resolved = path.resolve(strict=require_exists)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("repository root is not a valid local path") from exc
    if require_exists and not resolved.is_dir():
        raise ValueError("repository root must be an existing directory")
    if require_stored_form and os.path.normcase(str(path)) != os.path.normcase(
        str(resolved)
    ):
        raise ValueError("stored repository root must be canonical")
    return resolved


def _repo_identity(path: Path) -> str:
    return os.path.normcase(str(path))


@dataclass(frozen=True)
class TrackedWorkspace:
    workspace_id: str
    repo_root: Path
    session_id: str
    execution_locality: str
    registered_at: str

    @classmethod
    def create(
        cls,
        workspace_id: str,
        repo_root: Union[str, Path],
        session_id: str,
        execution_locality: str = PROCESS_LOCAL,
    ) -> "TrackedWorkspace":
        if not isinstance(workspace_id, str):
            raise ValueError("workspace id must be a string")
        workspace_id = validate_instruction_id(workspace_id)
        if execution_locality != PROCESS_LOCAL:
            raise ValueError("execution locality must be 'process_local'")
        return cls(
            workspace_id=workspace_id,
            repo_root=_canonical_repo_root(
                repo_root, require_exists=True, require_stored_form=False
            ),
            session_id=_canonical_session_id(session_id),
            execution_locality=execution_locality,
            registered_at=utc_now(),
        )

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "TrackedWorkspace":
        if not isinstance(value, dict) or frozenset(value) != _ENTRY_KEYS:
            raise WorkspaceRegistryFormatError(
                "workspace registry entry has unexpected or missing fields"
            )
        workspace_id = value["workspace_id"]
        registered_at = value["registered_at"]
        execution_locality = value["execution_locality"]
        if not isinstance(workspace_id, str):
            raise WorkspaceRegistryFormatError("workspace id must be a string")
        try:
            validate_instruction_id(workspace_id)
        except ValueError as exc:
            raise WorkspaceRegistryFormatError("invalid workspace id") from exc
        if execution_locality != PROCESS_LOCAL:
            raise WorkspaceRegistryFormatError(
                "workspace execution locality is not process_local"
            )
        if not isinstance(registered_at, str) or not registered_at:
            raise WorkspaceRegistryFormatError("invalid workspace registration time")
        try:
            repo_root = _canonical_repo_root(
                value["repo_root"], require_exists=False, require_stored_form=True,
            )
            session_id = _canonical_session_id(value["session_id"])
        except (TypeError, ValueError) as exc:
            raise WorkspaceRegistryFormatError(
                "invalid workspace path or session id"
            ) from exc
        return cls(
            workspace_id=workspace_id,
            repo_root=repo_root,
            session_id=session_id,
            execution_locality=execution_locality,
            registered_at=registered_at,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "repo_root": str(self.repo_root),
            "session_id": self.session_id,
            "execution_locality": self.execution_locality,
            "registered_at": self.registered_at,
        }

    def has_same_registration(self, other: "TrackedWorkspace") -> bool:
        return (
            self.workspace_id == other.workspace_id
            and _repo_identity(self.repo_root) == _repo_identity(other.repo_root)
            and self.session_id == other.session_id
            and self.execution_locality == other.execution_locality
        )


@dataclass(frozen=True)
class WorkspaceAddResult:
    workspace: TrackedWorkspace
    status: str
    path: Path


@dataclass(frozen=True)
class WorkspaceRemoveResult:
    workspace: Optional[TrackedWorkspace]
    status: str
    path: Path


class WorkspaceRegistry:
    """Atomic registry for workspaces owned by this watchdog process locality."""

    def __init__(self, runtime: Path) -> None:
        self.runtime = Path(runtime)
        self.path = self.runtime / "workspaces.json"
        self.lock_path = self.runtime / "locks" / "workspace-registry.lock"

    def registry_lock(self) -> FileLock:
        return FileLock(self.lock_path)

    def add(
        self,
        workspace_id: str,
        repo_root: Union[str, Path],
        session_id: str,
        execution_locality: str = PROCESS_LOCAL,
    ) -> WorkspaceAddResult:
        proposed = TrackedWorkspace.create(
            workspace_id, repo_root, session_id, execution_locality=execution_locality,
        )
        with self.registry_lock():
            existing = self._read_unlocked()
            for workspace in existing:
                if workspace.workspace_id == proposed.workspace_id:
                    if not workspace.has_same_registration(proposed):
                        raise WorkspaceCollisionError(
                            f"workspace id {workspace_id!r} already has different metadata"
                        )
                    return WorkspaceAddResult(workspace, "existing", self.path)
                if _repo_identity(workspace.repo_root) == _repo_identity(
                    proposed.repo_root
                ):
                    raise WorkspaceCollisionError(
                        "repository root is already registered to another workspace"
                    )
            existing.append(proposed)
            self._write_unlocked(existing)
            return WorkspaceAddResult(proposed, "created", self.path)

    def list_workspaces(self) -> List[TrackedWorkspace]:
        with self.registry_lock():
            return self._read_unlocked()

    def remove(self, workspace_id: str) -> WorkspaceRemoveResult:
        validate_instruction_id(workspace_id)
        with self.registry_lock():
            existing = self._read_unlocked()
            removed = next(
                (
                    workspace
                    for workspace in existing
                    if workspace.workspace_id == workspace_id
                ),
                None,
            )
            if removed is None:
                return WorkspaceRemoveResult(None, "missing", self.path)
            self._write_unlocked(
                [
                    workspace
                    for workspace in existing
                    if workspace.workspace_id != workspace_id
                ]
            )
            return WorkspaceRemoveResult(removed, "removed", self.path)

    def get(self, workspace_id: str) -> TrackedWorkspace:
        validate_instruction_id(workspace_id)
        for workspace in self.list_workspaces():
            if workspace.workspace_id == workspace_id:
                return workspace
        raise KeyError(workspace_id)

    def _read_unlocked(self) -> List[TrackedWorkspace]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceRegistryFormatError(
                "workspace registry cannot be read as JSON"
            ) from exc
        if not isinstance(value, dict) or frozenset(value) != _REGISTRY_KEYS:
            raise WorkspaceRegistryFormatError(
                "workspace registry has unexpected or missing fields"
            )
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != REGISTRY_SCHEMA_VERSION
        ):
            raise WorkspaceRegistryFormatError(
                f"unsupported workspace registry schema {value['schema_version']!r}"
            )
        raw_workspaces = value["workspaces"]
        if not isinstance(raw_workspaces, list):
            raise WorkspaceRegistryFormatError("registry workspaces must be a list")
        workspaces = []
        workspace_ids = set()
        repo_roots = set()
        for raw_workspace in raw_workspaces:
            workspace = TrackedWorkspace.from_dict(raw_workspace)
            repo_identity = _repo_identity(workspace.repo_root)
            if workspace.workspace_id in workspace_ids:
                raise WorkspaceRegistryFormatError(
                    "workspace registry contains a duplicate workspace id"
                )
            if repo_identity in repo_roots:
                raise WorkspaceRegistryFormatError(
                    "workspace registry contains a duplicate repository root"
                )
            workspace_ids.add(workspace.workspace_id)
            repo_roots.add(repo_identity)
            workspaces.append(workspace)
        return sorted(workspaces, key=lambda item: item.workspace_id)

    def _write_unlocked(self, workspaces: List[TrackedWorkspace]) -> None:
        ordered = sorted(workspaces, key=lambda item: item.workspace_id)
        InstructionStore._atomic_json(
            self.path,
            {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "workspaces": [workspace.to_dict() for workspace in ordered],
            },
        )
