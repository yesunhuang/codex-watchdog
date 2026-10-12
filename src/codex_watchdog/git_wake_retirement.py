"""Explicitly retire one obsolete uncertain Git wake without replay or handoff.

The original courier, owner and observer records are evidence and stay intact.
Only the ordinary observation guard can subsequently transfer observation.
"""
from contextlib import closing, ExitStack
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from . import control_state as cs


def _existing_file(path):
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise cs.ControlError("git_retirement_original_unavailable")
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise cs.ControlError("git_retirement_original_unavailable")


def _read(path):
    _existing_file(path)
    with path.open("rb") as handle:
        raw = handle.read(131073)
    try:
        if len(raw) > 131072:
            raise ValueError()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=cs._git_retirement_object,
                           parse_constant=cs._git_retirement_constant)
        if not isinstance(value, dict):
            raise ValueError()
    except (ValueError, RecursionError):
        raise cs.ControlError("git_retirement_original_invalid") from None
    return value, hashlib.sha256(raw).hexdigest()


def _queue_empty(store):
    path = store.codex_home / "queue_1.sqlite"
    if (list(islice(store.codex_home.glob("queue_*.sqlite"), 2)) != [path]
            or path.is_symlink() or path.resolve() != path or not path.is_file()):
        raise cs.ControlError("git_retirement_queue_unavailable")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            if db.execute("SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1",
                          (store.thread_id,)).fetchone() is not None:
                raise cs.ControlError("git_retirement_queue_pending")
    except sqlite3.Error:
        raise cs.ControlError("git_retirement_queue_unavailable") from None


def _original_paths(store, value):
    node = store.codex_home / "watchdog-nodes" / value["original_node"]
    return (node / "watchdog-control" / store.thread_id / "owner.json",
            node / "remote-wake" / (hashlib.sha256(value["instruction_id"].encode()).hexdigest() + ".json"),
            store.codex_home / "watchdog-observers" / (store.thread_id + ".json"))


def _validate_original(store, value, *, observer_required=True):
    owner_path, wake_path, observer_path = _original_paths(store, value)
    owner, owner_sha = _read(owner_path)
    wake, wake_sha = _read(wake_path)
    observer, observer_sha = _read(observer_path) if observer_required else ({}, None)
    state = owner.get("remote_state")
    identity = owner.get("owner")
    relay_owner = observer.get("relay_owner")
    if (owner_sha != value["original_owner_sha256"] or wake_sha != value["original_wake_sha256"]
            or type(owner.get("schema_version")) is not int or owner["schema_version"] != 1
            or owner.get("thread_id") != store.thread_id or owner.get("repo_path") != store.repo_path
            or type(owner.get("epoch")) is not int or owner["epoch"] != value["original_owner_epoch"]
            or owner.get("external_effect") is not None or owner.get("writer_pid") is not None
            or owner.get("state") not in ("DETACHED_REMOTE", "HANDOFF")
            or owner.get("attached_request") is not None or not isinstance(identity, dict)
            or identity.get("role") != "remote" or not cs.ControlStore._valid_identity(identity.get("instance"))
            or not cs.ControlStore._valid_identity(identity.get("locality"))
            or not cs.ControlStore._valid_deadline(identity.get("expires"))
            or not isinstance(owner.get("native_boot_id"), str) or not owner["native_boot_id"]
            or identity.get("boot_id") != owner["native_boot_id"]
            or not isinstance(state, dict) or state.get("pending_instruction_id") != value["instruction_id"]
            or wake.get("instruction_id") != value["instruction_id"] or wake.get("thread_id") != store.thread_id
            or wake.get("prompt_sha256") != value["prompt_sha256"] or wake.get("state") != "uncertain"
            or wake.get("queue_message_id") is not None or wake.get("started_turn_id") is not None):
        raise cs.ControlError("git_retirement_original_mismatch")
    if observer_required and (observer_sha != value["original_observer_sha256"]
            or type(observer.get("schema_version")) is not int or observer["schema_version"] != 1
            or observer.get("node") != value["original_node"] or observer.get("thread_id") != store.thread_id
            or observer.get("repo_path") != store.repo_path or observer.get("writer_live") is not False
            or observer.get("wake_pending") is not True or not isinstance(relay_owner, dict)
            or observer.get("boot_id") != owner.get("native_boot_id")
            or relay_owner.get("boot_id") != observer.get("boot_id")
            or relay_owner.get("node") != value["original_node"]
            or relay_owner.get("instance") != identity["instance"]
            or type(relay_owner.get("local_epoch")) is not int
            or relay_owner["local_epoch"] != value["original_owner_epoch"]):
        raise cs.ControlError("git_retirement_original_mismatch")
    _queue_empty(store)


def _native(store):
    current = store.read()
    pid = cs.control_kernel_owner(store.codex_home / "thread-writer-locks" / (store.thread_id + ".lock"))
    if (current.get("external_effect") is not None or pid is None or not cs.control_vscode_writer(pid)
            or cs.control_native_repo(store.codex_home, store.thread_id) != store.repo_path):
        raise cs.ControlError("git_retirement_native_session_unverified")


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _publish(path, value, validate):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    _sync_directory(path.parent.parent)  # Persist a newly created reservation directory too.
    fd, temporary = tempfile.mkstemp(prefix=".retirement-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        validate()
        os.link(temporary, path)  # Atomic exclusive publication; never replace a reservation.
        _sync_directory(path.parent)
    finally:
        os.unlink(temporary)
        _sync_directory(path.parent)
    validate()


def retire_git_wake(store, *, instruction_id, prompt_sha256, original_node,
                    original_owner_epoch, original_owner_sha256, original_wake_sha256,
                    original_observer_sha256, authorization_sha256):
    """Owner-authorized native API; this function never infers authorization."""
    from .node_observation import observation_lock

    if cs.sys.platform != "linux":
        raise cs.ControlError("git_retirement_native_linux_required")
    path = cs.control_git_retirement_path(store.codex_home, instruction_id)
    if (not isinstance(original_node, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", original_node) is None
            or type(original_owner_epoch) is not int or original_owner_epoch < 0):
        raise cs.ControlError("git_retirement_request_invalid")
    value = dict(schema_version=1, kind="git_wake_retirement", state="retired",
                 instruction_id=instruction_id, thread_id=store.thread_id, repo_path=store.repo_path,
                 prompt_sha256=prompt_sha256, original_node=original_node,
                 original_owner_epoch=original_owner_epoch, original_owner_sha256=original_owner_sha256,
                 original_wake_sha256=original_wake_sha256, original_observer_sha256=original_observer_sha256,
                 authorization_sha256=authorization_sha256)
    for key in ("prompt_sha256", "original_owner_sha256", "original_wake_sha256",
                "original_observer_sha256", "authorization_sha256"):
        if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise cs.ControlError("git_retirement_request_invalid")
    previous = cs.control_git_retirement_read(store.codex_home, instruction_id)
    if previous is not None:
        if any(previous.get(key) != item for key, item in value.items()):
            raise cs.ControlError("git_retirement_id_collision")
        return dict(previous, duplicate=True)
    owner_path, _, observer_path = _original_paths(store, value)
    _native(store)
    _validate_original(store, value)
    for lock in {store.lock_path, owner_path.with_suffix(".lock"), observer_path.with_suffix(".lock")}:
        _existing_file(lock)
    # Observation first, then sorted unique current/original controller locks,
    # then courier ID. Never wait for a controller while holding the ID lock.
    with observation_lock(observer_path.with_suffix(".lock")), ExitStack() as owners:
        for lock in sorted({store.lock_path, owner_path.with_suffix(".lock")}):
            owners.enter_context(cs.control_file_lock(lock))
        with cs.control_git_retirement_lock(store.codex_home, instruction_id):
            previous = cs.control_git_retirement_read(store.codex_home, instruction_id)
            if previous is not None:
                if any(previous.get(key) != item for key, item in value.items()):
                    raise cs.ControlError("git_retirement_id_collision")
                return dict(previous, duplicate=True)

            def validate():
                _native(store)
                _validate_original(store, value)

            validate()
            _publish(path, value, validate)
            return dict(value, duplicate=False)


def retired_observer_pending(store, observer):
    """Discount only the frozen old pending stamp, never other work or writers."""
    if observer.get("writer_live") is not False or observer.get("wake_pending") is not True:
        return False
    original_node = observer.get("node")
    if (not isinstance(original_node, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", original_node) is None):
        raise cs.ControlError("git_retirement_original_mismatch")
    path = store.codex_home / "watchdog-nodes" / original_node / "watchdog-control" / store.thread_id / "owner.json"
    owner, _ = _read(path)
    state = owner.get("remote_state")
    pending = state.get("pending_instruction_id") if isinstance(state, dict) else None
    retirement = cs.control_git_retirement_read(store.codex_home, pending, store.thread_id, store.repo_path)
    if retirement is None:
        return False
    _validate_original(store, retirement)
    return True


def retired_local_pending(store, local):
    state = local.get("remote_state")
    pending = state.get("pending_instruction_id") if isinstance(state, dict) else None
    retirement = cs.control_git_retirement_read(store.codex_home, pending, store.thread_id, store.repo_path)
    if retirement is None:
        return False
    if (retirement["original_node"] != cs.control_node_name()
            or retirement["original_owner_epoch"] != local.get("epoch")):
        return False
    _validate_original(store, retirement, observer_required=False)
    return True
