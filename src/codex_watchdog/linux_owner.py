"""One foreground Linux owner composed with the existing deterministic service."""

from __future__ import annotations

from contextlib import closing, ExitStack
from pathlib import Path
import signal
import sqlite3
import time
from typing import Any, Callable, Dict, Optional

from .app_server import AppServerError, StdioAppServer
from .linux_binding import LinuxBinding, LinuxBindingError, exact_thread, reservation_path
from .mvp_service import MvpWatchdogService
from .queue_wake import QueueWakeDispatcher, _resolve_codex_executable
from .storage import FileLock, InstructionStore, StoreBusyError
from .control_context import effect_guard, record_writer_pid
from .control_state import ControlBusy, ControlError
from .linux_health import LinuxOwnerHealth, owner_failure_reason


def writer_pid(codex_home: Path, thread: str) -> Optional[int]:
    return kernel_lock_owner(codex_home / "thread-writer-locks" / (thread + ".lock"))


def kernel_lock_owner(lock: Path) -> Optional[int]:
    from .control_state import ControlError, control_kernel_owner
    try:
        return control_kernel_owner(lock)
    except ControlError as exc:
        raise LinuxBindingError(str(exc)) from exc


def vscode_writer(pid: int) -> bool:
    from .control_state import control_vscode_writer
    return control_vscode_writer(pid)


def pending_count(codex_home: Path, thread: str) -> int:
    counts = []
    for path in codex_home.glob("queue_*.sqlite"):
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                counts.append(db.execute(
                    "SELECT COUNT(*) FROM queued_items WHERE thread_id=?", (thread,)
                ).fetchone()[0])
        except sqlite3.Error:
            continue
    if len(counts) != 1:
        raise LinuxBindingError("linux_queue_unavailable_or_ambiguous")
    return counts[0]


class _BoundCatalog:
    def __init__(self, binding: LinuxBinding) -> None:
        self.binding = binding

    def list_workspaces(self):
        value = self.binding.load()
        workspace = self.binding.workspace(value)
        exact_thread(self.binding.codex_home, workspace)
        return (workspace,) if value["state"] == "armed" else ()


class LinuxThreadOwner:
    def __init__(self, binding: LinuxBinding, *, executable: Optional[str] = None,
                 service: Optional[MvpWatchdogService] = None,
                 renew_lease: bool = False,
                 client_factory: Callable[..., StdioAppServer] = StdioAppServer) -> None:
        self.binding = binding
        self.renew_lease = renew_lease
        self.executable = executable or _resolve_codex_executable()
        self.service = service or MvpWatchdogService(
            binding.runtime, registry=_BoundCatalog(binding), codex_home=binding.codex_home,
            queue_dispatcher=QueueWakeDispatcher(
                binding.runtime, codex_home=binding.codex_home,
                codex_executable=self.executable,
            ),
        )
        self.client_factory = client_factory
        self.client: Optional[StdioAppServer] = None
        self.thread: Optional[str] = None
        self.thread_status = "unknown"
        self.approval_required = False
        self.release_requested = False
        self.status_path = binding.runtime / "linux" / "status.json"
        self._last_status: Optional[dict] = None
        self.health = LinuxOwnerHealth(
            binding.runtime, binding.codex_home, binding.workspace(binding.load()), self.service.notifier,
        )

    def _event(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        if method == "watchdog/approvalRequired":
            self.approval_required = True
            return
        params = message.get("params", {})
        if not isinstance(params, dict) or params.get("threadId") != self.thread:
            return
        if method == "thread/status/changed":
            status = params.get("status", {})
            self.thread_status = status.get("type", "unknown") if isinstance(status, dict) else "unknown"
        elif method == "turn/started":
            self.thread_status = "active"
        # turn/completed alone is not idle proof: Stop continuation/queued turns
        # may already be starting. Wait for the explicit thread status.

    def _status(self, state: str, reason: Optional[str] = None) -> Dict[str, Any]:
        result = {**self.binding.status(), "owner_state": state,
                  "thread_status": self.thread_status,
                  "approval_required": self.approval_required, "reason": reason}
        if result != self._last_status:
            InstructionStore._atomic_json(self.status_path, result)
            self._last_status = result
        return result

    def _resume(self, workspace) -> None:
        self.client = self.client_factory(
            self.executable, self.binding.codex_home, workspace.repo_root, self._event
        )
        self.client.initialize()
        before = self.client.request("thread/read", {"threadId": self.thread, "includeTurns": False})
        self._check_thread(before, workspace)
        if writer_pid(self.binding.codex_home, self.thread) is not None:
            raise LinuxBindingError("linux_writer_changed_before_resume")
        record_writer_pid(self.client.process.pid)
        # No thread/start, fork, history/path, cwd, model, or permission override.
        resumed = self.client.request("thread/resume", {"threadId": self.thread, "excludeTurns": True})
        self._check_thread(resumed, workspace)
        if writer_pid(self.binding.codex_home, self.thread) != self.client.process.pid:
            raise LinuxBindingError("linux_resume_did_not_own_writer")
        status = resumed["thread"].get("status", {})
        self.thread_status = status.get("type", "unknown") if isinstance(status, dict) else "unknown"

    def _check_thread(self, response: Dict[str, Any], workspace) -> None:
        thread = response.get("thread", {})
        if (not isinstance(thread, dict) or thread.get("id") != self.thread
                or thread.get("cwd") != str(workspace.repo_root)):
            raise LinuxBindingError("linux_app_server_thread_mismatch")

    def step(self, *, observe: bool) -> Dict[str, Any]:
        workspace = self.binding.workspace(self.binding.load())
        with ExitStack() as admitted:
            try:
                admitted.enter_context(effect_guard(self.binding.codex_home, workspace.session_id, "writer"))
            except ControlBusy:
                # Queue/release admission can briefly hold this lock. No writer
                # operation has begun; retain the client and recheck next cycle.
                return self._status("standby", "control_operation_in_progress")
            result = self._owned_step(observe=False)
            if (self.renew_lease and not self.release_requested
                    and result["owner_state"] in ("owned", "waiting_for_detach")):
                try:
                    self.binding.renew_lease(self.thread)
                except StoreBusyError:
                    pass  # Retry next owner check; a released/expired lease stays fenced.
        if observe and result["owner_state"] == "owned":
            cycle = self.service.run_once()
            if cycle.reason != "service_cycle_lock_held":
                if (cycle.status == "completed" and len(cycle.workspaces) == 1
                        and cycle.workspaces[0].workspace_id == workspace.workspace_id
                        and cycle.workspaces[0].status == "standby"
                        and cycle.workspaces[0].reason == "control_operation_in_progress"):
                    # A queue/release command can briefly hold the control lock
                    # after our writer check. No observation ran, so report
                    # neither an outage nor recovery; the next cycle rechecks.
                    return result
                if (cycle.status != "completed" or len(cycle.workspaces) != 1
                        or cycle.workspaces[0].workspace_id != workspace.workspace_id
                        or cycle.workspaces[0].status != "completed"):
                    if self.release_requested or self.binding.load()["state"] != "armed":
                        return result  # An idle release can race the catalog read.
                    raise LinuxBindingError("linux_observation_failed")
                self.health.report()
        return result

    def _owned_step(self, *, observe: bool) -> Dict[str, Any]:
        value = self.binding.load()
        workspace = self.binding.workspace(value)
        self.thread = workspace.session_id
        exact_thread(self.binding.codex_home, workspace)
        if self.release_requested or value["expires_at"] <= time.time():
            value = self.binding.set_state("release_requested")
        releasing = value["state"] != "armed"
        pid = writer_pid(self.binding.codex_home, self.thread)
        if self.client is None:
            if releasing:
                self.binding.set_state("released")
                return self._status("released")
            if pid is not None:
                if not vscode_writer(pid):
                    raise LinuxBindingError("linux_conflicting_writer")
                # The explicit binding authorizes this target, but the VS Code
                # process still owns execution. Never resume or send from here.
                return self._status("waiting_for_detach")
            self._resume(workspace)
        else:
            self.client.pump(timeout=0.01)
            if pid != self.client.process.pid:
                raise LinuxBindingError("linux_writer_changed")
        if releasing:
            if self.thread_status == "idle" and pending_count(self.binding.codex_home, self.thread) == 0:
                # A cached idle event alone can precede a consumed queued turn.
                # Re-read the live first-party status while the client handles
                # earlier events, then recheck the queue before closing stdio.
                current = self.client.request("thread/read", {
                    "threadId": self.thread, "includeTurns": False,
                })
                self._check_thread(current, workspace)
                status = current["thread"].get("status", {})
                if (self.thread_status == "idle" and isinstance(status, dict)
                        and status.get("type") == "idle"
                        and pending_count(self.binding.codex_home, self.thread) == 0):
                    self.client.close()
                    self.client = None
                    record_writer_pid(None)
                    self.binding.set_state("released")
                    return self._status("released")
            return self._status("releasing")
        if observe:
            self.service.run_once()
        return self._status("owned")

    def run(self, interval_seconds: float = 5,
            emit: Optional[Callable[[dict], None]] = None) -> int:
        if not 1 <= interval_seconds <= 300:
            raise LinuxBindingError("linux_interval_must_be_1_to_300_seconds")
        value = self.binding.load()
        lock = reservation_path(self.binding.codex_home, value["thread_id"]).with_suffix(".owner.lock")
        previous_handlers = {}
        next_observation = 0.0
        last = None
        locks = ExitStack()
        try:
            locks.enter_context(FileLock(self.binding.runtime / "locks" / "foreground-run.lock"))
            locks.enter_context(FileLock(lock))
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, self._signal_release)
            while True:
                now = time.monotonic()
                result = self.step(observe=now >= next_observation)
                if now >= next_observation:
                    next_observation = now + interval_seconds
                if emit is not None and result != last:
                    emit(result)
                    last = result
                if result["owner_state"] == "released":
                    return 0
                if self.client is not None:
                    self.client.pump(timeout=1)
                else:
                    time.sleep(0.5)
        except (LinuxBindingError, AppServerError, ControlError, OSError) as exc:
            reason = owner_failure_reason(exc)
            # No retry after an uncertain resume. Journals and reservation survive.
            notification = self.report_failure(reason)
            result = self._status("blocked", reason)
            result["notification"] = notification
            if emit is not None:
                emit(result)
            return 1
        finally:
            if self.client is not None:
                self.client.close()
                self.client = None
            locks.close()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

    def report_failure(self, reason):
        try:
            return self.health.report(reason)
        except (ControlError, OSError, ValueError, RuntimeError) as exc:
            return {"status": "blocked", "reason": owner_failure_reason(exc)}

    def _signal_release(self, signum, frame) -> None:
        self.release_requested = True
