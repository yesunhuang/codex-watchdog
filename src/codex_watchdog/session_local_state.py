"""Cold, exact-session preservation of two current runtime points.

Callers hold the admitted observation/owner and exact-session sender/producer
fences for capture, staging and final revalidation. This helper never discovers
sessions, changes old locks, reads native history, or manufactures missing state.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .control_state import ControlError
from .storage import StoreBusyError


LIMITS = {"lifecycle": 65536, "activity-cursors": 16384}


def _safe(path):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path:
        raise ControlError("session_local_state_path_invalid")
    return path


def _thread(thread):
    from .session_activity import canonical_session_id
    try:
        return canonical_session_id(thread)
    except ValueError:
        raise ControlError("session_local_state_identity_invalid") from None


def _read(runtime, thread, kind):
    path = _safe(_safe(runtime)/kind/(thread+".json"))
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise ControlError("session_local_state_file_invalid")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(LIMITS[kind]+1)
    after = path.lstat()
    if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            or len(raw) > LIMITS[kind]):
        raise ControlError("session_local_state_changed_or_oversized")
    try:
        value = json.loads(raw)
        if kind == "lifecycle":
            from .session_activity import _validate
            _validate(value, thread)
        else:
            from .activity_signals import _valid_cursor
            if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                    or value["schema_version"] != 1 or value.get("thread_id") != thread
                    or not _valid_cursor(value.get("cursor"))):
                raise ValueError()
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ControlError("session_local_state_record_invalid") from None
    return raw


def describe(thread, records):
    return dict(schema_version=1, thread_id=thread, files={kind: None if raw is None else
        dict(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        for kind, raw in records.items()})


def validate_descriptor(value, thread):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "thread_id", "files"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["thread_id"] != _thread(thread) or not isinstance(value["files"], dict)
            or set(value["files"]) != set(LIMITS)):
        raise ControlError("session_local_state_metadata_invalid")
    for kind, item in value["files"].items():
        if item is not None and (not isinstance(item, dict) or set(item) != {"sha256", "bytes"}
                or re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256"))) is None
                or type(item.get("bytes")) is not int or not 0 < item["bytes"] <= LIMITS[kind]):
            raise ControlError("session_local_state_metadata_invalid")
    return value


def validate_staged(runtime, thread, descriptor):
    validate_descriptor(descriptor, thread)
    actual = describe(thread, {kind: _read(runtime, thread, kind) for kind in LIMITS})
    if actual != descriptor:
        raise ControlError("session_local_state_snapshot_changed")


class SessionLocalState:
    def __init__(self, runtime, thread, locks):
        self.runtime, self.thread = _safe(runtime), _thread(thread)
        self.locks = locks
        self.records = {kind: _read(runtime, thread, kind) for kind in LIMITS}
        self.descriptor = describe(thread, self.records)

    def revalidate(self):
        for path, identity in self.locks.items():
            info = _safe(path).lstat()
            if (info.st_dev, info.st_ino, info.st_size) != identity:
                raise ControlError("session_local_state_source_lock_changed")
        validate_staged(self.runtime, self.thread, self.descriptor)

    def stage(self, runtime):
        from .messaging_profile import write_new
        runtime = _safe(runtime)
        for kind, raw in self.records.items():
            path = _safe(runtime/kind/(self.thread+".json"))
            if raw is not None and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_new(path, raw)
        validate_staged(runtime, self.thread, self.descriptor)


@contextmanager
def _existing_lock(path):
    # Unlike FileLock's creating open, a disappearing old lock remains absent.
    # These are the same producer byte/flock locks, with no directory/NUL writes.
    with path.open("r+b") as stream:
        info = os.fstat(stream.fileno())
        current = _safe(path).lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size < 1
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise ControlError("session_local_state_source_lock_invalid")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise StoreBusyError("session local state producer lock unavailable") from error
        try:
            yield (info.st_dev, info.st_ino, info.st_size)
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def capture_session_local_state(runtime, thread):
    """Hold existing point locks inside the caller's current producer fence.

    Missing point+lock is retained as absence. A present point without its
    existing producer lock is an explicit migration gap, not a reset. No old
    directory or lock is created; absence is rechecked immediately pre-selector.
    """
    runtime, thread = _safe(runtime), _thread(thread)
    with ExitStack() as held:
        locks = {}
        for kind, prefix in (("lifecycle", "lifecycle-"), ("activity-cursors", "activity-cursor-")):
            lock = _safe(runtime/"locks"/(prefix+thread+".lock"))
            point = _safe(runtime/kind/(thread+".json"))
            if not lock.exists():
                if point.exists():
                    raise ControlError("session_local_state_source_lock_missing")
                continue
            info = lock.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size < 1
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise ControlError("session_local_state_source_lock_invalid")
            locks[lock] = held.enter_context(_existing_lock(lock))
        yield SessionLocalState(runtime, thread, locks)
