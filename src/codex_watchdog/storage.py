from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, Iterator, Optional
import uuid

from .models import Instruction, utc_now, validate_instruction_id


_IS_WINDOWS = os.name == "nt"
_ATOMIC_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08)
_TRANSIENT_WINDOWS_REPLACE_ERRORS = frozenset({5, 32, 33})


def _replace_atomically(source: Path, destination: Path) -> None:
    """Bound retries to transient Windows sharing/lock failures only."""
    for delay in (*_ATOMIC_REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if (
                not _IS_WINDOWS
                or getattr(exc, "winerror", None)
                not in _TRANSIENT_WINDOWS_REPLACE_ERRORS
                or delay is None
            ):
                raise
            time.sleep(delay)


class StoreBusyError(RuntimeError):
    """Another watchdog process owns the relevant short-lived store lock."""


class InstructionCollisionError(RuntimeError):
    """An existing instruction id was reused with different content."""


class FileLock(AbstractContextManager["FileLock"]):
    """A crash-releasing, non-blocking one-byte advisory file lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._handle.close()
            self._handle = None
            if getattr(exc, "errno", None) in (
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
                None,
            ):
                raise StoreBusyError(
                    f"watchdog lock is already held: {self.path}"
                ) from exc
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


@dataclass(frozen=True)
class SubmitResult:
    instruction: Instruction
    status: str
    path: Path


class InstructionStore:
    """Durable filesystem inbox with logical at-most-once continuation intent."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.inbox = self.root / "inbox"
        self.inflight = self.root / "inflight"
        self.consumed = self.root / "consumed"
        self.guards = self.root / "guards"
        self.audit = self.root / "audit"
        self.locks = self.root / "locks"

    def ensure(self) -> None:
        for directory in (
            self.inbox,
            self.inflight,
            self.consumed,
            self.guards,
            self.audit,
            self.locks,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def store_lock(self) -> FileLock:
        self.ensure()
        return FileLock(self.locks / "store.lock")

    def session_lock(self, session_id: str) -> FileLock:
        self.ensure()
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return FileLock(self.locks / f"session-{digest}.lock")

    def submit(
        self,
        instruction_id: str,
        source: str,
        prompt: str,
        target_session_id: Optional[str] = None,
    ) -> SubmitResult:
        proposed = Instruction.create(
            instruction_id, source, prompt, target_session_id=target_session_id
        )
        filename = self._instruction_filename(instruction_id)
        with self.store_lock():
            for status, directory in (
                ("queued", self.inbox),
                ("inflight", self.inflight),
                ("consumed", self.consumed),
            ):
                path = directory / filename
                if path.exists():
                    existing = self._read_instruction(path)
                    if (
                        existing.prompt_sha256 != proposed.prompt_sha256
                        or existing.source != proposed.source
                        or existing.target_session_id != proposed.target_session_id
                    ):
                        raise InstructionCollisionError(
                            f"instruction id {instruction_id!r} already exists with different metadata or content"
                        )
                    return SubmitResult(existing, status, path)
            path = self.inbox / filename
            self._atomic_json(path, proposed.to_dict())
            return SubmitResult(proposed, "created", path)

    def claim_next(self, session_id: str, turn_id: str) -> Optional[Instruction]:
        with self.store_lock():
            candidates = []
            for path in self.inbox.glob("*.json"):
                try:
                    instruction = self._read_instruction(path)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    # Fail open. A partially published or malformed instruction remains
                    # visible for manual recovery and is never interpreted as a prompt.
                    continue
                if (
                    instruction.target_session_id is not None
                    and instruction.target_session_id != session_id
                ):
                    continue
                candidates.append(
                    (
                        instruction.created_at,
                        instruction.instruction_id,
                        path,
                        instruction,
                    )
                )
            for _, _, path, instruction in sorted(candidates):
                destination = self.inflight / path.name
                if destination.exists() or (self.consumed / path.name).exists():
                    continue
                os.replace(path, destination)
                intent = instruction.with_state("return_intent", session_id, turn_id)
                self._atomic_json(destination, intent.to_dict())
                self._write_turn_guard(session_id, turn_id, intent)
                return intent
        return None

    def turn_has_intent(self, session_id: str, turn_id: str) -> bool:
        return self._guard_path(session_id, turn_id).exists()

    def confirm_continuation(
        self, session_id: str, turn_id: str
    ) -> Optional[Instruction]:
        """Use Stop(stop_hook_active=true) as the native receipt for return intent."""
        with self.store_lock():
            for path in sorted(
                self.inflight.glob("*.json"), key=lambda item: item.name
            ):
                try:
                    instruction = self._read_instruction(path)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                if (
                    instruction.session_id == session_id
                    and instruction.turn_id == turn_id
                    and instruction.state == "return_intent"
                ):
                    confirmed = instruction.with_state(
                        "continued_confirmed", session_id, turn_id
                    )
                    destination = self.consumed / path.name
                    self._atomic_json(path, confirmed.to_dict())
                    os.replace(path, destination)
                    return confirmed
        return None

    def record_audit(self, event: Dict[str, Any]) -> Path:
        self.ensure()
        value = dict(event)
        value.setdefault("schema_version", 1)
        value.setdefault("recorded_at", utc_now())
        value.setdefault("audit_id", str(uuid.uuid4()))
        filename = f"{value['recorded_at'].replace(':', '-')}_{value['audit_id']}.json"
        path = self.audit / filename
        self._atomic_json(path, value)
        return path

    def list_state(self, directory: str) -> Iterator[Instruction]:
        selected = {
            "inbox": self.inbox,
            "inflight": self.inflight,
            "consumed": self.consumed,
        }[directory]
        for path in sorted(selected.glob("*.json"), key=lambda item: item.name):
            yield self._read_instruction(path)

    def _guard_path(self, session_id: str, turn_id: str) -> Path:
        digest = hashlib.sha256(f"{session_id}\0{turn_id}".encode("utf-8")).hexdigest()
        return self.guards / f"{digest}.json"

    @staticmethod
    def _instruction_filename(instruction_id: str) -> str:
        validate_instruction_id(instruction_id)
        return hashlib.sha256(instruction_id.encode("utf-8")).hexdigest() + ".json"

    def _write_turn_guard(
        self, session_id: str, turn_id: str, instruction: Instruction
    ) -> None:
        path = self._guard_path(session_id, turn_id)
        if path.exists():
            raise InstructionCollisionError(
                f"turn already has a continuation intent: {session_id}/{turn_id}"
            )
        self._atomic_json(
            path,
            {
                "schema_version": 1,
                "session_id": session_id,
                "turn_id": turn_id,
                "instruction_id": instruction.instruction_id,
                "prompt_sha256": instruction.prompt_sha256,
                "created_at": utc_now(),
            },
        )

    @staticmethod
    def _read_instruction(path: Path) -> Instruction:
        with path.open("r", encoding="utf-8") as handle:
            return Instruction.from_dict(json.load(handle))

    @staticmethod
    def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_atomically(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
