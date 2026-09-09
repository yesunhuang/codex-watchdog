"""Process-local capability context; the remote file record remains authoritative."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import os
import sys
import uuid

from .control_state import ControlError, ControlStore, control_file_lock, control_atomic_json


_capability = ContextVar("watchdog_control_capability", default=None)
_held = ContextVar("watchdog_control_held", default=None)


@contextmanager
def acting_as(store, token):
    with store.guard(token):
        pass
    selected = _capability.set((store, dict(token)))
    try:
        yield
    finally:
        _capability.reset(selected)


def current_control():
    return _capability.get()


def record_writer_pid(pid):
    selected = current_control()
    if selected is not None:
        with current_effect("writer"):
            store, token = selected
            value = store.read()
            value["writer_pid"] = pid
            control_atomic_json(store.path, value)


@contextmanager
def effect_guard(codex_home, thread_id, purpose="state"):
    """No epoch is inferred from a saved runtime or a revived process's PID."""
    thread_id = str(uuid.UUID(thread_id))
    directory = Path(codex_home).resolve() / "watchdog-control" / thread_id
    selected = _capability.get()
    # A caller already inside this exact capability's kernel lock may compose
    # existing queue/notifier code without reacquiring the same advisory lock.
    if _held.get() == (str(directory), selected[1] if selected is not None else None):
        if selected is not None and purpose == "queue" and selected[0].read()["state"] == "HANDOFF":
            raise ControlError("control_handback_pending")
        yield
        return
    if not (directory / "owner.json").exists():
        # Synchronize even the uncoordinated path with first activation. This
        # lock is not an owner record and does not opt a thread into handoff.
        with control_file_lock(directory / "owner.lock"):
            if (directory / "owner.json").exists():
                raise ControlError("control_activation_changed")
            held = _held.set((str(directory), None))
            try:
                yield
            finally:
                _held.reset(held)
        return
    if selected is None or selected[0].directory.resolve() != directory:
        raise ControlError("control_owner_capability_required")
    store, token = selected
    with store.guard(token, purpose=purpose):
        held = _held.set((str(directory), token))
        try:
            yield
        finally:
            _held.reset(held)


@contextmanager
def current_effect(purpose="state"):
    selected = _capability.get()
    if selected is None:
        yield
    else:
        store, token = selected
        with effect_guard(store.directory.parent.parent, store.thread_id, purpose):
            yield


def _hook_descends_from(pid):
    parent = os.getppid()
    for _ in range(8):
        if parent == pid:
            return True
        if parent <= 1:
            return False
        try:
            proc = Path("/proc") / str(parent)
            if proc.stat().st_uid != os.getuid():
                return False
            parent = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


@contextmanager
def hook_owner(runtime, codex_home, payload):
    """A hook is delegated by its live first-party writer, never by a saved PID."""
    thread = payload.get("session_id")
    try:
        thread = str(uuid.UUID(thread))
    except (ValueError, TypeError, AttributeError):
        yield runtime
        return
    path = Path(codex_home) / "watchdog-control" / thread / "owner.json"
    if not path.exists():
        yield runtime
        return
    if sys.platform != "linux" or not isinstance(payload.get("cwd"), str):
        raise ControlError("control_hook_context_unverified")
    from .linux_owner import writer_pid, vscode_writer
    store = ControlStore(codex_home, thread, payload["cwd"])
    value = store.read()
    owner = value["owner"]
    pid = writer_pid(Path(codex_home), thread)
    if (owner is None or pid is None or not _hook_descends_from(pid)
            or owner["role"] == "local" and not vscode_writer(pid)
            or owner["role"] == "remote" and value.get("writer_pid") != pid):
        raise ControlError("control_hook_writer_unverified")
    selected_runtime = value.get("runtime_path")
    if not isinstance(selected_runtime, str) or not Path(selected_runtime).is_absolute():
        raise ControlError("control_hook_runtime_unavailable")
    with acting_as(store, store._token(value)):
        yield Path(selected_runtime)
