"""Explicit Linux thread reservations; never infer a replacement target."""

from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, Iterator
import uuid

from .storage import FileLock, InstructionStore, StoreBusyError
from .workspace_registry import TrackedWorkspace
from .control_context import effect_guard


class LinuxBindingError(ValueError):
    """A bounded, privacy-safe reason for refusing detached control."""


def read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(65537)
        value = json.loads(raw) if len(raw) <= 65536 else None
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise LinuxBindingError("linux_binding_unreadable") from exc


def reservation_path(codex_home: Path, thread: str) -> Path:
    return codex_home / "watchdog-linux" / (str(uuid.UUID(thread)) + ".json")


def runtime_identity(runtime: Path) -> str:
    return hashlib.sha256(str(runtime.resolve()).encode("utf-8")).hexdigest()


@contextmanager
def sender_guard(codex_home: Path, runtime: Path, thread: str) -> Iterator[None]:
    """A reservation fences new sends; old journal observation remains allowed."""
    path = reservation_path(codex_home, thread)
    # No behavior/state change for hosts without an explicit Linux binding.
    if not path.exists():
        yield
        return
    with FileLock(path.with_suffix(".send.lock")):
        value = read_json(path)
        if (value.get("thread_id") != thread
                or value.get("state") not in ("armed", "release_requested", "released")):
            raise LinuxBindingError("linux_thread_reserved")
        if value.get("state") != "released":
            if (
                value.get("state") != "armed"
                or value.get("thread_id") != thread
                or value.get("runtime_sha256") != runtime_identity(runtime)
                or type(value.get("expires_at")) not in (int, float)
                or not math.isfinite(value["expires_at"])
                or value["expires_at"] <= time.time()
            ):
                raise LinuxBindingError("linux_thread_reserved")
        yield


def locality_identity() -> str:
    if sys.platform != "linux":
        raise LinuxBindingError("linux_required")
    try:
        machine = Path("/etc/machine-id").read_text().strip()
        if not machine:
            raise ValueError()
        return hashlib.sha256(f"{machine}:{os.getuid()}".encode()).hexdigest()
    except (OSError, ValueError) as exc:
        raise LinuxBindingError("linux_locality_unavailable") from exc


def exact_thread(codex_home: Path, workspace: TrackedWorkspace) -> Path:
    """Read only the operator-selected VS Code user thread, even with siblings."""
    try:
        with closing(sqlite3.connect(
            (codex_home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=1
        )) as db:
            rows = db.execute(
                "SELECT rollout_path FROM threads WHERE id=? AND cwd=? "
                "AND source='vscode' AND thread_source='user' AND archived=0",
                (workspace.session_id, str(workspace.repo_root)),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError()
        rollout = Path(rows[0][0]).resolve(strict=True)
        rollout.relative_to((codex_home / "sessions").resolve())
        if not rollout.is_file() or not workspace.repo_root.is_dir():
            raise ValueError()
        return rollout
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise LinuxBindingError("linux_exact_thread_unavailable") from exc


class LinuxBinding:
    def __init__(self, runtime: Path, codex_home: Path) -> None:
        self.runtime = runtime.resolve()
        self.codex_home = codex_home.resolve()
        self.pointer = self.runtime / "linux" / "binding.json"

    def load(self) -> Dict[str, Any]:
        pointer = read_json(self.pointer)
        try:
            thread = str(uuid.UUID(pointer["thread_id"]))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise LinuxBindingError("linux_binding_unreadable") from exc
        value = read_json(reservation_path(self.codex_home, thread))
        if (
            value.get("thread_id") != thread
            or value.get("runtime_sha256") != runtime_identity(self.runtime)
            or value.get("locality_sha256") != locality_identity()
            or pointer.get("codex_home") != str(self.codex_home)
            or value.get("state") not in ("armed", "release_requested", "released")
            or type(value.get("expires_at")) not in (int, float)
            or not math.isfinite(value["expires_at"])
        ):
            raise LinuxBindingError("linux_binding_mismatch")
        self.workspace(value)
        return value

    @staticmethod
    def workspace(value: Dict[str, Any]) -> TrackedWorkspace:
        try:
            workspace = TrackedWorkspace.from_dict(value["workspace"])
            if workspace.session_id != value["thread_id"]:
                raise ValueError()
            return workspace
        except (KeyError, ValueError, RuntimeError) as exc:
            raise LinuxBindingError("linux_binding_mismatch") from exc

    def bind(self, workspace: TrackedWorkspace, lease_seconds: float) -> Dict[str, Any]:
        with effect_guard(self.codex_home, workspace.session_id, "writer"):
            return self._bind(workspace, lease_seconds)

    def _bind(self, workspace: TrackedWorkspace, lease_seconds: float) -> Dict[str, Any]:
        locality = locality_identity()
        if not 60 <= lease_seconds <= 86400:
            raise LinuxBindingError("linux_lease_must_be_60_to_86400_seconds")
        exact_thread(self.codex_home, workspace)
        path = reservation_path(self.codex_home, workspace.session_id)
        with FileLock(self.runtime / "locks" / "foreground-run.lock"), FileLock(
            path.with_suffix(".owner.lock")
        ), FileLock(path.with_suffix(".send.lock")):
            if self.pointer.exists():
                old = self.load()
                if not self.workspace(old).has_same_registration(workspace):
                    raise LinuxBindingError("linux_binding_collision")
            value = read_json(path) if path.exists() else {}
            if value and (
                value.get("thread_id") != workspace.session_id
                or value.get("state") not in ("armed", "release_requested", "released")
                or type(value.get("expires_at")) not in (int, float)
                or not math.isfinite(value["expires_at"])
            ):
                raise LinuxBindingError("linux_binding_unreadable")
            if value and (
                value.get("runtime_sha256") != runtime_identity(self.runtime)
                or value.get("locality_sha256") != locality
                or not self.workspace(value).has_same_registration(workspace)
            ):
                raise LinuxBindingError("linux_thread_reserved")
            if value.get("state") != "armed" or value.get("expires_at", 0) <= time.time():
                value.update(
                    schema_version=1, thread_id=workspace.session_id,
                    runtime_sha256=runtime_identity(self.runtime),
                    runtime_path=str(self.runtime),
                    locality_sha256=locality, workspace=workspace.to_dict(),
                    state="armed", expires_at=time.time() + lease_seconds,
                )
                InstructionStore._atomic_json(path, value)
            # Publishing the reservation first makes an interrupted bind fail closed.
            # Repeating the same bind finishes the pointer without another target.
            if not self.pointer.exists():
                InstructionStore._atomic_json(self.pointer, {
                    "schema_version": 1, "thread_id": workspace.session_id,
                    "codex_home": str(self.codex_home),
                })
            return value

    def set_state(self, state: str) -> Dict[str, Any]:
        value = self.load()
        with effect_guard(self.codex_home, value["thread_id"], "writer"):
            return self._set_state(state)

    def _set_state(self, state: str) -> Dict[str, Any]:
        if state not in ("release_requested", "released"):
            raise LinuxBindingError("linux_invalid_state")
        value = self.load()
        path = reservation_path(self.codex_home, value["thread_id"])
        with FileLock(path.with_suffix(".send.lock")):
            value = self.load()
            if value["state"] != "released":
                value["state"] = state
                InstructionStore._atomic_json(path, value)
        return value

    def request_release(self, *, monotonic=time.monotonic, sleep=time.sleep) -> Dict[str, Any]:
        """Explicit operator request; never grants execution or interrupts a turn."""
        from .control_state import ControlBusy, ControlStore, control_file_lock, control_atomic_json
        value = self.load()
        thread = value["thread_id"]
        workspace = self.workspace(value)
        path = self.codex_home / "watchdog-control" / thread / "owner.json"
        coordinated = path.exists()
        reservation = reservation_path(self.codex_home, thread)
        deadline = monotonic() + 1.0
        with ExitStack() as admitted:
            while True:
                attempt = ExitStack()
                try:
                    attempt.enter_context(control_file_lock(path.with_name("owner.lock")))
                    attempt.enter_context(FileLock(reservation.with_suffix(".send.lock")))
                except (ControlBusy, StoreBusyError) as exc:
                    attempt.close()
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise LinuxBindingError("linux_release_busy") from exc
                    sleep(min(0.05, remaining))
                    if monotonic() >= deadline:
                        raise LinuxBindingError("linux_release_busy") from exc
                except BaseException:
                    attempt.close()
                    raise
                else:
                    admitted.callback(attempt.close)
                    break
            # Retry only admission, before either durable write. Never switch
            # to a rebound thread or adopt first activation during the wait.
            value = self.load()
            if not workspace.has_same_registration(self.workspace(value)):
                raise LinuxBindingError("linux_release_binding_changed")
            if not coordinated and path.exists():
                raise LinuxBindingError("control_activation_changed")
            if coordinated:
                store = ControlStore(self.codex_home, thread, workspace.repo_root)
                current = store.read()
                current["auto_paused"] = True
                control_atomic_json(store.path, current)
            if value["state"] != "released":
                value["state"] = "release_requested"
                InstructionStore._atomic_json(reservation, value)
            return value

    def status(self) -> Dict[str, Any]:
        value = self.load()
        return {
            "schema_version": 1, "state": value["state"],
            "thread_sha256": hashlib.sha256(value["thread_id"].encode()).hexdigest(),
            "lease_expired": value["expires_at"] <= time.time(),
        }
