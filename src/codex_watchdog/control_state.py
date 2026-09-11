"""Remote-host control records shared with the dependency-free SSH helper.

The source below deliberately uses Python 3.6 syntax: an existing remote helper
must enforce the same fence even when no packaged Linux WatchDog is installed.
Keeping one implementation also avoids a second, subtly different protocol.
"""

CONTROL_SOURCE = r'''
import contextlib
import errno
import hashlib
import json
import math
import os
import re
import socket
import sys
from pathlib import Path
import tempfile
import time
import uuid


class ControlError(ValueError):
    pass


class ControlBusy(ControlError):
    pass


@contextlib.contextmanager
def control_file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise ControlBusy("control_operation_in_progress")
            raise
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def control_atomic_json(path, value):
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    if len(serialized.encode("utf-8")) > 131072:
        raise ControlError("control_record_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".control-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def control_read_json(path):
    try:
        with path.open("rb") as handle:
            raw = handle.read(131073)
        value = json.loads(raw) if len(raw) <= 131072 else None
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError):
        raise ControlError("control_record_unreadable")


def control_node_name():
    name = socket.gethostname()
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", name) is None:
        raise ControlError("control_node_name_invalid")
    return name


def control_node_directory(codex_home):
    return Path(codex_home).resolve() / "watchdog-nodes" / control_node_name()


def control_state_home(codex_home):
    """Opt-in host-local state; native Codex databases/locks never move."""
    home = Path(codex_home).resolve()
    if sys.platform != "linux":
        return home
    node = control_node_directory(home)
    marker = node / "node.json"
    if not marker.exists() and not marker.is_symlink():
        return home
    value = control_read_json(marker)
    if (value.get("node") != control_node_name() or value.get("codex_home") != str(home)
            or node.is_symlink() or marker.is_symlink() or node.resolve() != node):
        raise ControlError("control_node_configuration_mismatch")
    return node


def control_root(codex_home):
    return control_state_home(codex_home) / "watchdog-control"


def control_binding_root(codex_home):
    return control_state_home(codex_home) / "watchdog-linux"


def control_native_repo(codex_home, thread):
    import sqlite3
    try:
        path = Path(codex_home).resolve() / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            rows = db.execute("SELECT cwd FROM threads WHERE id=? AND archived=0 "
                              "AND source='vscode' AND thread_source='user'", (thread,)).fetchall()
        if len(rows) == 1 and isinstance(rows[0][0], str) and Path(rows[0][0]).is_absolute():
            return str(Path(rows[0][0]).resolve())
    except (OSError, sqlite3.Error):
        pass
    raise ControlError("control_exact_thread_unavailable")


def control_kernel_owner(lock):
    import fcntl
    try:
        with lock.open("rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
                return None
        identity = lock.stat()
        with Path("/proc/locks").open("rb") as handle:
            raw = handle.read(1048577)
        if len(raw) > 1048576:
            raise ControlError("linux_writer_unavailable")
        owners = set()
        for line in raw.splitlines():
            fields = line.split()
            if len(fields) < 6 or fields[1:4] != [b"FLOCK", b"ADVISORY", b"WRITE"]:
                continue
            try:
                major, minor, inode = fields[5].split(b":")
                if (int(major, 16), int(minor, 16), int(inode)) == (
                        os.major(identity.st_dev), os.minor(identity.st_dev), identity.st_ino):
                    owners.add(int(fields[4]))
            except ValueError:
                raise ControlError("linux_writer_unavailable")
        if len(owners) == 1:
            pid = owners.pop()
            after = lock.stat()
            if ((identity.st_dev, identity.st_ino) == (after.st_dev, after.st_ino)
                    and pid > 0 and (Path("/proc") / str(pid)).stat().st_uid == os.getuid()):
                return pid
    except FileNotFoundError:
        return None
    except OSError:
        raise ControlError("linux_writer_unavailable")
    raise ControlError("linux_writer_ambiguous")


def control_vscode_writer(pid):
    try:
        proc = Path("/proc") / str(pid)
        args = (proc / "cmdline").read_bytes().split(b"\0")
        stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        parent = (Path("/proc") / stat[1] / "cmdline").read_bytes().split(b"\0")
        return b"app-server" in args and b"--type=extensionHost" in parent
    except (OSError, ValueError, IndexError):
        return False


class ControlStore:
    def __init__(self, codex_home, thread_id, repo_path, clock=time.monotonic, boot_id=None):
        if str(uuid.UUID(thread_id)) != thread_id:
            raise ControlError("control_thread_invalid")
        self.thread_id = thread_id
        self.repo_path = str(Path(repo_path).resolve())
        self.codex_home = Path(codex_home).resolve()
        self.directory = control_root(self.codex_home) / thread_id
        self.path = self.directory / "owner.json"
        self.lock_path = self.directory / "owner.lock"
        self.clock = clock
        self.boot_id = boot_id
        if self.boot_id is None:
            try:
                self.boot_id = str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
            except (OSError, ValueError):
                raise ControlError("control_boot_identity_unavailable")

    def enroll_node(self):
        """Enroll only a proven local native writer, never shared-home history."""
        if control_state_home(self.codex_home) == self.codex_home:
            raise ControlError("control_node_not_configured")
        with control_file_lock(self.lock_path):
            if self.path.exists():
                return False
            pid = control_kernel_owner(self.codex_home / "thread-writer-locks" / (self.thread_id + ".lock"))
            if (pid is None or not control_vscode_writer(pid)
                    or control_native_repo(self.codex_home, self.thread_id) != self.repo_path):
                raise ControlError("control_initial_attachment_unverified")
            control_atomic_json(self.path, dict(
                schema_version=1, thread_id=self.thread_id, repo_path=self.repo_path,
                epoch=0, owner=None, state="HANDOFF", attached_request=None, external_effect=None,
                runtime_path=str(self.directory / "runtime"), remote_state=None,
                native_boot_id=self.boot_id,
                remote_target=dict(authority="ssh-remote+" + control_node_name(),
                                   repo_path=self.repo_path,
                                   storage_key=hashlib.sha256(self.repo_path.encode("utf-8")).hexdigest()[:32]),
            ))
            return True

    def read(self):
        value = control_read_json(self.path)
        if (value.get("thread_id") != self.thread_id or value.get("repo_path") != self.repo_path
                or type(value.get("epoch")) is not int or value["epoch"] < 0
                or value.get("state") not in ("ATTACHED_LOCAL", "HANDOFF", "DETACHED_REMOTE")
                or "owner" not in value or "external_effect" not in value):
            raise ControlError("control_record_mismatch")
        if (control_state_home(self.codex_home) != self.codex_home
                and value.get("runtime_path") not in (None, str(self.directory / "runtime"))):
            raise ControlError("control_node_runtime_mismatch")
        owner = value["owner"]
        if owner is not None:
            if (not isinstance(owner, dict) or owner.get("role") not in ("local", "remote")
                    or not self._valid_identity(owner.get("instance"))
                    or not self._valid_identity(owner.get("locality"))
                    or not self._valid_deadline(owner.get("expires"))
                    or not isinstance(owner.get("boot_id"), str)):
                raise ControlError("control_record_mismatch")
        request = value.get("attached_request")
        if request is not None and (not isinstance(request, dict)
                or not self._valid_identity(request.get("instance"))
                or not self._valid_identity(request.get("locality"))
                or not self._valid_deadline(request.get("expires"))
                or not isinstance(request.get("boot_id"), str)):
            raise ControlError("control_record_mismatch")
        barrier = value["external_effect"]
        if barrier is not None and (not isinstance(barrier, dict) or owner is None
                or not self._valid_identity(barrier.get("id"))
                or barrier.get("epoch") != value["epoch"]
                or barrier.get("instance") != owner["instance"]):
            raise ControlError("control_record_mismatch")
        if (value["state"] == "ATTACHED_LOCAL" and (owner is None or owner["role"] != "local")
                or value["state"] == "DETACHED_REMOTE" and (owner is None or owner["role"] != "remote")):
            raise ControlError("control_record_mismatch")
        return value

    @staticmethod
    def _valid_identity(value):
        return isinstance(value, str) and 0 < len(value) <= 128 and all(
            c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:" for c in value)

    @staticmethod
    def _valid_deadline(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    def _live(self, value):
        return (isinstance(value, dict) and value.get("boot_id") == self.boot_id
                and self._valid_deadline(value.get("expires")) and value["expires"] > self.clock())

    def _identity(self, instance, locality, ttl):
        if not self._valid_identity(instance) or not self._valid_identity(locality):
            raise ControlError("control_owner_identity_invalid")
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 5 <= ttl <= 3600:
            raise ControlError("control_lease_invalid")
        return dict(instance=instance, locality=locality, expires=self.clock() + ttl, boot_id=self.boot_id)

    def _token(self, value):
        owner = value["owner"]
        return dict(thread_id=self.thread_id, repo_path=self.repo_path, epoch=value["epoch"],
                    instance=owner["instance"], locality=owner["locality"], role=owner["role"])

    def _check(self, value, token):
        if not isinstance(token, dict) or value["owner"] is None or token != self._token(value):
            raise ControlError("control_stale_epoch")
        # An issued external action cannot be revoked merely by a timeout. Its
        # durable barrier survives disconnects/restarts until an exact receipt.
        if not self._live(value["owner"]) and value["external_effect"] is None:
            raise ControlError("control_lease_expired")

    def _grant(self, value, identity, role):
        value.update(epoch=value["epoch"] + 1, owner=dict(identity, role=role),
                     state="ATTACHED_LOCAL" if role == "local" else "DETACHED_REMOTE")
        if role == "local":
            value["attached_request"] = None
        elif identity.get("host_observer") is True and self._live(value.get("attached_request")):
            value["state"] = "HANDOFF"
        control_atomic_json(self.path, value)
        return self._token(value)

    def _recover_completed_external(self, value):
        barrier = value["external_effect"]
        if isinstance(barrier, dict) and isinstance(barrier.get("event_id"), str):
            path = self.effect_path("notification", barrier["event_id"])
            if path.exists():
                receipt = control_read_json(path)
                if (receipt.get("operation_id") == barrier["id"]
                        and receipt.get("epoch") == value["epoch"]
                        and receipt.get("state") == "completed"):
                    value["external_effect"] = None
                    control_atomic_json(self.path, value)

    def attach(self, instance, locality, writer, ttl=900):
        identity = self._identity(instance, locality, ttl)
        if writer not in ("vscode", "remote", "absent"):
            raise ControlError("control_writer_unverified")
        with control_file_lock(self.lock_path):
            if not self.path.exists():
                if writer != "vscode":
                    raise ControlError("control_initial_attachment_unverified")
                value = dict(schema_version=1, thread_id=self.thread_id, repo_path=self.repo_path,
                             epoch=0, owner=None, state="HANDOFF", attached_request=None,
                             external_effect=None, native_boot_id=self.boot_id)
            else:
                value = self.read()
            self._recover_completed_external(value)
            owner = value["owner"]
            if (owner is not None and owner["role"] == "local"
                    and owner["instance"] == instance and owner["locality"] == locality
                    and self._live(owner)):
                owner.update(identity)
                control_atomic_json(self.path, value)
                return self._token(value)
            if value["external_effect"] is not None:
                raise ControlError("control_external_effect_unresolved")
            if owner is not None and (self._live(owner) or writer == "remote"):
                if owner["role"] == "remote":
                    # A host observer keeps notification authority while VS Code
                    # owns execution. Only its detached writer needs handback.
                    if owner.get("host_observer") is True and writer != "remote":
                        return None
                    request = value.get("attached_request")
                    if self._live(request) and request["instance"] != instance:
                        raise ControlError("control_competing_attached_client")
                    value.update(state="HANDOFF", attached_request=identity)
                    control_atomic_json(self.path, value)
                return None
            if writer == "remote":
                raise ControlError("control_writer_not_released")
            return self._grant(value, identity, "local")

    def claim_remote(self, instance, locality, writer, ttl=30, manual=False, host_observer=False):
        identity = self._identity(instance, locality, ttl)
        with control_file_lock(self.lock_path):
            value = self.read()  # Remote never invents an unobserved target.
            if control_state_home(self.codex_home) != self.codex_home:
                if writer == "vscode":
                    pid = control_kernel_owner(self.codex_home / "thread-writer-locks" / (self.thread_id + ".lock"))
                    if pid is None or not control_vscode_writer(pid):
                        return None
                    value["native_boot_id"] = self.boot_id
                elif value.get("native_boot_id") != self.boot_id:
                    # A reboot is not proof that this shared-home thread still
                    # belongs to this node. Re-enroll through its native writer.
                    return None
            if value.get("auto_paused") is True and not manual:
                return None
            self._recover_completed_external(value)
            if value["external_effect"] is not None:
                raise ControlError("control_external_effect_unresolved")
            if host_observer:
                # The running host controller may replace a desktop observer,
                # never a live host controller or an unverified native writer.
                # The lock and external-effect barrier fence in-flight sends.
                if writer not in ("absent", "vscode"):
                    return None
                owner = value["owner"]
                if self._live(owner) and owner["role"] != "local":
                    return None
                identity["host_observer"] = True
                if writer == "vscode":
                    value["attached_request"] = None
            else:
                if self._live(value.get("attached_request")) or self._live(value["owner"]):
                    return None
                if writer != "absent":
                    return None
            return self._grant(value, identity, "remote")

    def observe_writer(self, token, writer):
        """Separate native writer handback from host observation ownership."""
        if writer not in ("absent", "vscode", "remote"):
            raise ControlError("control_writer_unverified")
        with self.guard(token) as value:
            if value["owner"].get("host_observer") is not True:
                raise ControlError("control_host_observer_required")
            # Older desktop helpers still write HANDOFF for a VS Code writer.
            # Retain our epoch, receipts and cursor; that writer needs no release.
            if writer == "vscode" or not self._live(value.get("attached_request")):
                value.update(state="DETACHED_REMOTE", attached_request=None)
                control_atomic_json(self.path, value)
            return value["state"] == "HANDOFF"

    def handback_pending(self, value):
        if value["state"] != "HANDOFF":
            return False
        if value["owner"].get("host_observer") is True:
            lock = self.codex_home / "thread-writer-locks" / (self.thread_id + ".lock")
            pid = control_kernel_owner(lock)
            if pid is not None and control_vscode_writer(pid):
                return False
        return True

    def renew(self, token, ttl=30):
        identity = self._identity(token["instance"], token["locality"], ttl)
        with control_file_lock(self.lock_path):
            value = self.read()
            try:
                self._check(value, token)
            except ControlError as exc:
                # A delayed controller may recover only its unchanged epoch and
                # still-held writer on this boot. A surviving writer already
                # prevents any replacement from claiming detached execution.
                pid = value.get("writer_pid")
                lock = self.codex_home / "thread-writer-locks" / (self.thread_id + ".lock")
                if (str(exc) != "control_lease_expired" or token["role"] != "remote"
                        or value["owner"]["boot_id"] != self.boot_id
                        or type(pid) is not int or pid <= 0 or control_kernel_owner(lock) != pid):
                    raise
            value["owner"].update(identity)
            control_atomic_json(self.path, value)

    def detach(self, token):
        with control_file_lock(self.lock_path):
            value = self.read()
            self._check(value, token)
            if token["role"] != "local" or value["external_effect"] is not None:
                raise ControlError("control_release_not_safe")
            value.update(owner=None, state="HANDOFF", attached_request=None)
            control_atomic_json(self.path, value)

    def release_remote(self, token, writer):
        with control_file_lock(self.lock_path):
            value = self.read()
            self._check(value, token)
            attached_observer = (writer == "vscode" and value["owner"].get("host_observer") is True
                                 and value.get("writer_pid") is None)
            if (token["role"] != "remote" or writer != "absent" and not attached_observer
                    or value["external_effect"] is not None):
                raise ControlError("control_release_not_safe")
            value.update(owner=None, state="HANDOFF")
            control_atomic_json(self.path, value)

    @contextlib.contextmanager
    def guard(self, token, purpose="state", external_id=None):
        with control_file_lock(self.lock_path):
            value = self.read()
            self._check(value, token)
            barrier = value["external_effect"]
            if external_id is not None and (not isinstance(barrier, dict)
                    or barrier.get("id") != external_id):
                raise ControlError("control_external_effect_mismatch")
            if purpose == "queue" and self.handback_pending(value):
                raise ControlError("control_handback_pending")
            yield value

    def begin_external(self, token, operation_id):
        if not self._valid_identity(operation_id):
            raise ControlError("control_operation_id_invalid")
        with self.guard(token) as value:
            if value["external_effect"] is not None:
                raise ControlError("control_external_effect_unresolved")
            value["external_effect"] = dict(id=operation_id, epoch=token["epoch"],
                                            instance=token["instance"], started=self.clock())
            control_atomic_json(self.path, value)

    def effect_path(self, kind, event_id):
        key = hashlib.sha256((kind + "\0" + event_id).encode("utf-8")).hexdigest()
        return self.directory / "effects" / (key + ".json")

    def prepare_notification(self, token, event_id, fingerprint):
        path = self.effect_path("notification", event_id)
        with self.guard(token) as value:
            if path.exists():
                receipt = control_read_json(path)
                if receipt.get("fingerprint") != fingerprint:
                    raise ControlError("control_effect_id_collision")
                return dict(receipt, duplicate=True)
            if value["external_effect"] is not None:
                raise ControlError("control_external_effect_unresolved")
            operation = str(uuid.uuid4())
            value["external_effect"] = dict(id=operation, event_id=event_id, epoch=token["epoch"],
                                            instance=token["instance"], started=self.clock())
            control_atomic_json(self.path, value)
            receipt = dict(schema_version=1, state="uncertain", fingerprint=fingerprint,
                           epoch=token["epoch"], instance=token["instance"], operation_id=operation)
            control_atomic_json(path, receipt)
            return dict(receipt, duplicate=False)

    def merge_slack_mappings(self, value, entries, event_id=None):
        for entry in entries:
            if (not isinstance(entry, dict)
                    or re.fullmatch(r"[CG][A-Z0-9]{8,}", str(entry.get("channel_id", ""))) is None
                    or re.fullmatch(r"[0-9]{10,}\.[0-9]+", str(entry.get("thread_ts", ""))) is None
                    or re.fullmatch(r"[0-9a-f]{64}", str(entry.get("event_fingerprint", ""))) is None
                    or event_id is not None and entry.get("event_fingerprint") != event_id):
                raise ControlError("control_slack_mapping_invalid")
            key = hashlib.sha256((entry["channel_id"] + "\0" + entry["thread_ts"]).encode()).hexdigest()
            path = self.directory / "slack-threads" / (key + ".json")
            if path.exists():
                continue  # The exact Slack address is immutable routing evidence.
            control_atomic_json(path, dict(schema_version=1, **{
                name: entry[name] for name in ("channel_id", "thread_ts", "event_fingerprint")}))

    def slack_mappings(self):
        return [control_read_json(path) for path in sorted((self.directory / "slack-threads").glob("*.json"))]

    def finish_notification(self, token, event_id, operation_id, result, relay_mappings=()):
        path = self.effect_path("notification", event_id)
        with self.guard(token, external_id=operation_id) as value:
            receipt = control_read_json(path)
            if receipt.get("operation_id") != operation_id:
                raise ControlError("control_external_effect_mismatch")
            self.merge_slack_mappings(value, relay_mappings, event_id)
            # Retain routing before clearing the external-send barrier. A crash
            # cannot publish a completed send while losing its reply address.
            control_atomic_json(self.path, value)
            receipt.update(state="completed", result=result)
            control_atomic_json(path, receipt)
            value["external_effect"] = None
            control_atomic_json(self.path, value)
            return receipt

    def finish_external(self, token, operation_id):
        with self.guard(token, external_id=operation_id) as value:
            value["external_effect"] = None
            control_atomic_json(self.path, value)

    def once(self, token, kind, event_id, fingerprint, action, external_id=None):
        with self.guard(token, purpose=kind, external_id=external_id):
            return self._once_locked(token, kind, event_id, fingerprint, action)

    def _once_locked(self, token, kind, event_id, fingerprint, action):
        path = self.effect_path(kind, event_id)
        if path.exists():
            receipt = control_read_json(path)
            if receipt.get("fingerprint") != fingerprint:
                raise ControlError("control_effect_id_collision")
            return dict(receipt, duplicate=True)
        receipt = dict(schema_version=1, state="uncertain", fingerprint=fingerprint,
                       epoch=token["epoch"], instance=token["instance"])
        control_atomic_json(path, receipt)
        result = action()
        receipt.update(state="completed", result=result)
        control_atomic_json(path, receipt)
        return dict(receipt, duplicate=False)
'''

exec(compile(CONTROL_SOURCE, "<watchdog-control-state>", "exec"))
