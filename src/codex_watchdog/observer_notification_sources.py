"""Local, bounded source clocks for observer notifications.

Each workspace scope stores at most one point per notification slot. The first
observation of a slot is an unknown baseline; only a strictly later, different
fingerprint records a birth time. The single nonblocking lock serializes this
local clock only. It is not authorization, native-admission proof or a provider
sender fence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from .storage import FileLock, InstructionStore

SCHEMA_VERSION = 1
SLOTS = frozenset({"discovery_attention", "resume_ambiguity"})
MAX_STATE_BYTES = 8192
MAX_WORKSPACE_ID_CHARS = 4096
MAX_GENERATION = 2**63 - 2
EXPIRY_SECONDS = 72 * 60 * 60
MAX_CLEANUP_BUDGET = 64

_FINGERPRINT = re.compile("[0-9a-f]{64}")
_STATE_NAME = re.compile("([0-9a-f]{64})[.]json")
_POINT_KEYS = frozenset({"fingerprint", "born", "checked_at", "generation"})
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)

Identity = Tuple[int, int]
Fence = Tuple[Identity, Identity, Identity]
Writer = Callable[[Path, Dict[str, Any]], None]


def _time(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be an int or float")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _scope_sha256(workspace_id: Any) -> str:
    if (
        not isinstance(workspace_id, str)
        or not workspace_id
        or len(workspace_id) > MAX_WORKSPACE_ID_CHARS
    ):
        raise ValueError("workspace_id must be a nonempty string of at most 4096 characters")
    try:
        encoded = workspace_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("workspace_id must be encodable as UTF-8") from exc
    return hashlib.sha256(encoded).hexdigest()


def _require_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError("fingerprint must be exactly 64 lowercase hex characters")
    return value


def _point_checked_at(point: Any) -> float:
    """Validate one stored point and return its checked_at clock."""
    if not isinstance(point, dict) or not _POINT_KEYS <= set(point):
        raise ValueError("observer source point is incomplete")
    _require_fingerprint(point["fingerprint"])
    checked_at = _time(point["checked_at"], "checked_at")
    generation = point["generation"]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or not 0 <= generation <= MAX_GENERATION
    ):
        raise ValueError("generation must be an int in 0..2**63-2")
    born = point["born"]
    if born is None:
        if generation != 0:
            raise ValueError("an unknown baseline point must have generation 0")
    elif generation == 0 or _time(born, "born") > checked_at:
        raise ValueError("a born point must follow a baseline and precede checked_at")
    return checked_at


def _document_points(document: Any, scope: str) -> Dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("observer source state must be an object")
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ValueError("unsupported observer source schema_version")
    if document.get("scope_sha256") != scope:
        raise ValueError("observer source scope_sha256 does not match its path")
    points = document.get("points")
    if not isinstance(points, dict) or not set(points) <= SLOTS:
        raise ValueError("observer source points must hold only the known slots")
    return points


def _unique_object(pairs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in observer source state")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number in observer source state: {name}")


def _parse(data: bytes) -> Any:
    if len(data) > MAX_STATE_BYTES:
        raise ValueError("observer source state exceeds 8192 bytes")
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise ValueError("observer source state is too deeply nested") from exc


def _encoded_size(value: Dict[str, Any]) -> int:
    # Matches InstructionStore._atomic_json so published state stays readable.
    text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    return len(text.encode("utf-8"))


def _identity(status: os.stat_result) -> Identity:
    if not status.st_dev or not status.st_ino:
        raise ValueError("observer source path has no stable physical identity")
    return status.st_dev, status.st_ino


def _redirected(status: os.stat_result) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(attributes & _REPARSE_POINT)


def _canonical(path: Path) -> bool:
    text = str(path)
    return os.path.normcase(os.path.realpath(text)) == os.path.normcase(text)


def _require_regular(path: Path, status: os.stat_result) -> None:
    if _redirected(status) or not stat.S_ISREG(status.st_mode):
        raise ValueError(f"refusing redirected or irregular file: {path}")


class ObserverNotificationSources:
    """Persist one bounded source clock per workspace scope and slot."""

    def __init__(
        self, runtime: Any, *, writer: Writer = InstructionStore._atomic_json
    ) -> None:
        path = Path(runtime)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("runtime must be an absolute path without parent components")
        self._runtime = path
        self._service = path / "service"
        self._sources = self._service / "observer-sources"
        self._locks = path / "locks"
        self._lock_path = self._locks / "observer-sources.lock"
        self._writer = writer
        self._scan: Any = None
        self._scan_identity: Optional[Identity] = None

    def observe(
        self, workspace_id: Any, slot: Any, fingerprint: Any, observed_at: Any
    ) -> Optional[Dict[str, Any]]:
        """Record one observation and return a copy of the current point.

        Returns None, without writing, for a backwards observation or for a
        different fingerprint at the already checked time.
        """
        scope = _scope_sha256(workspace_id)
        if not isinstance(slot, str) or slot not in SLOTS:
            raise ValueError("slot must be discovery_attention or resume_ambiguity")
        _require_fingerprint(fingerprint)
        now = _time(observed_at, "observed_at")
        with self._locked() as fence:
            dirs = self._state_dirs(create=True)
            path = self._sources / f"{scope}.json"
            loaded = self._read_bounded(path)
            if loaded is None:
                identity = None
                document: Dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION,
                    "scope_sha256": scope,
                    "points": {},
                }
            else:
                data, identity = loaded
                document = _parse(data)
            points = _document_points(document, scope)
            if slot not in points:
                # Missing state is an unknown baseline, never a new activation.
                point: Dict[str, Any] = {
                    "fingerprint": fingerprint,
                    "born": None,
                    "checked_at": now,
                    "generation": 0,
                }
            else:
                current = points[slot]
                checked_at = _point_checked_at(current)
                same = current["fingerprint"] == fingerprint
                if now < checked_at or (now == checked_at and not same):
                    return None
                if now == checked_at:
                    return copy.deepcopy(current)
                point = copy.deepcopy(current)
                point["checked_at"] = now
                if not same:
                    if current["generation"] >= MAX_GENERATION:
                        raise ValueError("observer source generation is exhausted")
                    point["fingerprint"] = fingerprint
                    point["born"] = now
                    point["generation"] = current["generation"] + 1
            points[slot] = point
            if _encoded_size(document) > MAX_STATE_BYTES:
                raise ValueError("observer source state would exceed 8192 bytes")
            self._verify(fence, dirs)
            if self._identity_of(path) != identity:
                raise ValueError("observer source state changed before publication")
            self._writer(path, document)
            self._verify(fence, dirs)
            return copy.deepcopy(point)

    def cleanup(self, observed_at: Any, *, budget: Any = 16) -> Dict[str, int]:
        """Visit at most budget entries and delete fully expired valid scopes."""
        now = _time(observed_at, "observed_at")
        if (
            isinstance(budget, bool)
            or not isinstance(budget, int)
            or not 1 <= budget <= MAX_CLEANUP_BUDGET
        ):
            raise ValueError("budget must be an int in 1..64")
        horizon = now - EXPIRY_SECONDS
        visited = removed = 0
        try:
            with self._locked() as fence:
                dirs = self._state_dirs(create=False)
                if dirs is None:
                    self.close()
                    return {"visited": 0, "removed": 0}
                if self._scan is not None and self._scan_identity != dirs[1]:
                    self.close()
                while visited < budget:
                    if self._scan is None:
                        self._scan = os.scandir(str(self._sources))
                        self._scan_identity = dirs[1]
                        self._verify(fence, dirs)
                    try:
                        entry = next(self._scan)
                    except StopIteration:
                        self.close()
                        break
                    visited += 1
                    if self._remove_if_expired(entry.name, horizon, fence, dirs):
                        removed += 1
        except BaseException:
            self.close()
            raise
        return {"visited": visited, "removed": removed}

    def close(self) -> None:
        scan, self._scan, self._scan_identity = self._scan, None, None
        if scan is not None:
            scan.close()

    def _remove_if_expired(
        self, name: str, horizon: float, fence: Fence, dirs: Tuple[Identity, Identity]
    ) -> bool:
        match = _STATE_NAME.fullmatch(name)
        if match is None:
            return False
        path = self._sources / name
        try:
            loaded = self._read_bounded(path)
            if loaded is None:
                return False
            data, identity = loaded
            points = _document_points(_parse(data), match.group(1))
            if not points:
                return False
            for point in points.values():
                if _point_checked_at(point) > horizon:
                    return False
        except (OSError, ValueError):
            return False
        self._verify(fence, dirs)
        if self._identity_of(path) != identity:
            raise ValueError("observer source state changed during cleanup")
        os.unlink(path)
        return True

    @contextmanager
    def _locked(self) -> Iterator[Fence]:
        base = self._base()
        # Refuse a redirected lock file before FileLock opens it.
        self._identity_of(self._lock_path)
        with FileLock(self._lock_path) as lock:
            fence = self._fence()
            held = _identity(os.fstat(lock._handle.fileno()))
            if fence[:2] != base or held != fence[2]:
                raise ValueError("observer source lock or runtime changed during acquisition")
            yield fence

    def _base(self) -> Tuple[Identity, Identity]:
        runtime = self._directory(self._runtime, create=False)
        if runtime is None:
            raise ValueError(f"runtime directory does not exist: {self._runtime}")
        locks = self._directory(self._locks, create=True)
        assert locks is not None
        return runtime, locks

    def _fence(self) -> Fence:
        runtime, locks = self._base()
        lock = self._identity_of(self._lock_path)
        if lock is None:
            raise ValueError("observer source lock file disappeared")
        return runtime, locks, lock

    def _state_dirs(self, create: bool) -> Optional[Tuple[Identity, Identity]]:
        service = self._directory(self._service, create)
        if service is None:
            return None
        sources = self._directory(self._sources, create)
        if sources is None:
            return None
        return service, sources

    def _verify(self, fence: Fence, dirs: Optional[Tuple[Identity, Identity]]) -> None:
        if self._fence() != fence or self._state_dirs(create=False) != dirs:
            raise ValueError("observer source directories changed")

    @staticmethod
    def _directory(path: Path, create: bool) -> Optional[Identity]:
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(path)
            except FileExistsError:
                pass
            status = os.lstat(path)
        if _redirected(status) or not stat.S_ISDIR(status.st_mode) or not _canonical(path):
            raise ValueError(f"refusing redirected or noncanonical directory: {path}")
        return _identity(status)

    @staticmethod
    def _identity_of(path: Path) -> Optional[Identity]:
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            return None
        _require_regular(path, status)
        return _identity(status)

    @staticmethod
    def _read_bounded(path: Path) -> Optional[Tuple[bytes, Identity]]:
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            return None
        _require_regular(path, status)
        identity = _identity(status)
        descriptor = os.open(str(path), _READ_FLAGS)
        try:
            if _identity(os.fstat(descriptor)) != identity:
                raise ValueError(f"observer source state changed while opening: {path}")
            chunks = []
            remaining = MAX_STATE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        return b"".join(chunks), identity
