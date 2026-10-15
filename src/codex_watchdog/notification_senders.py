"""Common kernel exclusion fence for notification, control and ACK senders.

``sender_fence`` holds one nonblocking, crash-releasing kernel lock at
``runtime/locks/notification-senders.lock`` for the caller's whole body and
commit. It is only an exclusion fence: never a capability, permission,
receipt, completion or native-owner proof. Callers still acquire every
existing owner/provider lock they require.
"""

from __future__ import annotations

import os
import stat
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Tuple, Union

from .storage import FileLock

SENDER_LOCK_NAME = "notification-senders.lock"

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

ResourceId = Tuple[int, int]


@dataclass
class _Lease:
    """A live lexical hold, reusable only by the same PID, thread and resource."""

    root: Path
    root_id: ResourceId
    locks_id: ResourceId
    lock_path: Path
    lock_id: ResourceId
    pid: int
    thread: int
    lock: FileLock
    closed: bool = False


# Only the current lexical nesting; no permanent cache of runtimes or holders.
_LEASES: ContextVar[Tuple[_Lease, ...]] = ContextVar(
    "codex_watchdog_sender_fence_leases", default=()
)


def _absolute(runtime: Union[str, "os.PathLike[str]"]) -> Path:
    raw = os.fspath(runtime)
    if not isinstance(raw, str) or not raw:
        raise ValueError("sender fence runtime must be a non-empty text path")
    if ".." in Path(raw).parts:
        raise ValueError("sender fence runtime contains unresolved parent components")
    return Path(os.path.abspath(raw))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _require_canonical(path: Path) -> None:
    if not _same_path(Path(os.path.realpath(path)), path):
        raise ValueError(f"sender fence path is redirected or not canonical: {path}")


def _is_redirect(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE_POINT)


def _directory_id(path: Path) -> ResourceId:
    info = os.lstat(path)
    if _is_redirect(info) or not stat.S_ISDIR(info.st_mode) or not info.st_ino:
        raise ValueError(
            f"sender fence directory is redirected or not a directory: {path}"
        )
    _require_canonical(path)
    return (info.st_dev, info.st_ino)


def _file_id(path: Path) -> ResourceId:
    info = os.lstat(path)
    if _is_redirect(info) or not stat.S_ISREG(info.st_mode) or not info.st_ino:
        raise ValueError(
            f"sender fence lock is redirected or not a regular file: {path}"
        )
    _require_canonical(path)
    return (info.st_dev, info.st_ino)


def _handle_id(lock: FileLock) -> ResourceId:
    handle = lock._handle
    if handle is None or handle.closed:
        raise ValueError("sender fence lease is no longer open")
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode) or not info.st_ino:
        raise ValueError("sender fence lock handle is not a regular file")
    return (info.st_dev, info.st_ino)


def _ensure_directory(path: Path) -> ResourceId:
    # Creates only this one empty directory; a missing parent is refused.
    _require_canonical(path)
    if not os.path.lexists(path):
        try:
            os.mkdir(path)
        except FileExistsError:
            pass
    return _directory_id(path)


def _verify(lease: _Lease) -> None:
    """Refuse a hold whose handle, directories or lock path no longer match."""
    try:
        changed = (
            _handle_id(lease.lock) != lease.lock_id
            or _directory_id(lease.root) != lease.root_id
            or _directory_id(lease.lock_path.parent) != lease.locks_id
            or _file_id(lease.lock_path) != lease.lock_id
        )
    except OSError as exc:
        raise ValueError(f"sender fence resource changed: {lease.lock_path}") from exc
    if changed:
        raise ValueError(f"sender fence resource changed: {lease.lock_path}")


@contextmanager
def sender_fence(runtime: Union[str, "os.PathLike[str]"]) -> Iterator[None]:
    """Hold the common sender kernel lock through the caller's body and commit.

    Raises StoreBusyError without waiting when another holder has it. Only a
    live lease from the same PID, thread and unchanged resource is reentered;
    every other caller must obtain real kernel exclusion.
    """
    root = _absolute(runtime)
    pid = os.getpid()
    thread = threading.get_ident()
    stack = _LEASES.get()
    if any(lease.closed for lease in stack):
        # Escaped copies may outlive a scope. Do not chain closed holds into
        # every subsequent copied context or make entry grow with old calls.
        stack = tuple(lease for lease in stack if not lease.closed)
        _LEASES.set(stack)
    for lease in reversed(stack):
        if (
            not lease.closed
            and lease.pid == pid
            and lease.thread == thread
            and _same_path(lease.root, root)
        ):
            if (_directory_id(root) != lease.root_id
                    or _directory_id(root / "locks") != lease.locks_id
                    or _file_id(root / "locks" / SENDER_LOCK_NAME) != lease.lock_id):
                raise ValueError("sender fence incoming runtime resource differs")
            _verify(lease)
            yield
            return

    root_id = _ensure_directory(root)
    locks_id = _ensure_directory(root / "locks")
    lock_path = root / "locks" / SENDER_LOCK_NAME
    if os.path.lexists(lock_path):
        _file_id(lock_path)
    else:
        _require_canonical(lock_path)
    with FileLock(lock_path) as lock:
        lease = _Lease(
            root, root_id, locks_id, lock_path, _handle_id(lock), pid, thread, lock
        )
        _verify(lease)
        token = _LEASES.set(stack + (lease,))
        try:
            yield
        finally:
            lease.closed = True
            try:
                _LEASES.reset(token)
            except ValueError:
                # Exited from a different context; the closed flag still
                # prevents any escaped copy from borrowing this lease.
                pass
