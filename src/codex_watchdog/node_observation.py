"""Keep one Git/completion observer when Linux nodes share a Codex home.

This selects observation, not a new Codex session. A verified native writer
can take over from a parked observer. Active or uncertain work stays fenced;
shared history alone never selects a different node.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path

from . import control_state as cs


@contextmanager
def observation_lock(path):
    # GPFS's flock is local to a host. POSIX record locks exclude other native
    # nodes and release on process exit; this was verified on the target GPFS.
    if os.name == "nt":  # Enables isolated node-mode fixtures on Windows.
        with cs.control_file_lock(path):
            yield
        return
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise cs.ControlBusy("control_node_observation_busy")
            raise
        try:
            yield
        finally:
            fcntl.lockf(handle, fcntl.LOCK_UN)


class NodeObservation:
    def __init__(self, store, token):
        self.store, self.token = store, token
        self.node = cs.control_node_name()
        self.path = store.codex_home / "watchdog-observers" / (store.thread_id + ".json")
        self.lock_path = self.path.with_suffix(".lock")
        self.reason = None
        self._admitted = False
        self._parked_snapshot = None

    def _read(self):
        if not self.path.exists():
            return None
        value = cs.control_read_json(self.path)
        if (value.get("thread_id") != self.store.thread_id
                or value.get("repo_path") != self.store.repo_path
                or not self.store._valid_identity(value.get("node"))
                or not isinstance(value.get("boot_id"), str)
                or type(value.get("writer_live")) is not bool
                or type(value.get("wake_pending")) is not bool):
            raise cs.ControlError("control_node_observation_mismatch")
        return value

    def _native_writer(self, local):
        pid = cs.control_kernel_owner(
            self.store.codex_home / "thread-writer-locks" / (self.store.thread_id + ".lock")
        )
        if pid is None:
            return False
        if pid == local.get("writer_pid") or cs.control_vscode_writer(pid):
            return True
        raise cs.ControlError("control_writer_unverified")

    def _unique_legacy_node(self, local):
        # Reuse an unambiguous same-boot registration during upgrade. Multiple
        # old node records require fresh native ownership, never an mtime guess.
        if local.get("native_boot_id") != self.store.boot_id:
            return False
        pattern = "*/watchdog-control/" + self.store.thread_id + "/owner.json"
        return list((self.store.codex_home / "watchdog-nodes").glob(pattern)) == [self.store.path]

    def validate_snapshot(self):
        if not self._admitted:
            raise cs.ControlBusy("control_node_observation_required")
        if self._parked_snapshot is not None:
            path = Path(self._parked_snapshot[0])
            info = path.stat()
            current = [str(path), info.st_ino, info.st_size, info.st_mtime_ns]
            if current != self._parked_snapshot:
                raise cs.ControlBusy("control_node_observation_history_changed")

    @contextmanager
    def guard(self, *, rollout_stamp, foreign_queue=False):
        self.reason = None
        with observation_lock(self.lock_path):
            with self.store.guard(self.token) as local:
                writer_live = self._native_writer(local)
                value = self._read()
                ours = value is not None and value["node"] == self.node
                if not ours:
                    if value is None:
                        if not writer_live and not self._unique_legacy_node(local):
                            self.reason = "control_node_observation_needs_native_writer"
                    elif not writer_live:
                        self.reason = "control_node_observed_elsewhere"
                    elif value["writer_live"] or value["wake_pending"]:
                        self.reason = "control_node_observation_other_work_pending"
                    if self.reason is None:
                        value = dict(value or {}, schema_version=1, node=self.node,
                                     thread_id=self.store.thread_id, repo_path=self.store.repo_path,
                                     boot_id=self.store.boot_id,
                                     writer_live=writer_live, wake_pending=False)
                elif value["boot_id"] != self.store.boot_id and not writer_live:
                    self.reason = "control_node_observation_needs_native_writer"
                if self.reason is None:
                    stamp = list(rollout_stamp) if rollout_stamp is not None else None
                    if writer_live:
                        value["rollout_stamp"] = None
                    elif value["writer_live"] or value.get("rollout_stamp") is None:
                        value["rollout_stamp"] = stamp
                    value.update(boot_id=self.store.boot_id, writer_live=writer_live)
                    if value != self._read():
                        cs.control_atomic_json(self.path, value)
                    if not writer_live:
                        if stamp is None or value.get("rollout_stamp") != stamp:
                            self.reason = "control_node_observation_history_changed"
                        elif foreign_queue:
                            self.reason = "control_node_observation_foreign_queue"
            if self.reason is not None:
                yield False
                return
            self._admitted = True
            self._parked_snapshot = None if writer_live else value["rollout_stamp"]
            try:
                yield True
            finally:
                self._admitted = False
                self._parked_snapshot = None
                # Retain a just-enqueued or uncertain wake until its original
                # courier receipt is reconciled. An absent writer alone does
                # not make this interval safe for another node to take over.
                with self.store.guard(self.token) as local:
                    state = local.get("remote_state") or {}
                    pending = isinstance(state.get("pending_instruction_id"), str)
                    if value["wake_pending"] != pending:
                        value["wake_pending"] = pending
                        cs.control_atomic_json(self.path, value)
