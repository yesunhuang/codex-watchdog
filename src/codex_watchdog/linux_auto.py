"""Persistent Linux mode composed from the existing exact-thread owner/service."""

from contextlib import ExitStack
import json
from pathlib import Path
import signal
import time
import uuid

from .control_context import acting_as
from .app_server import AppServerError
from .control_state import ControlBusy, ControlError, ControlStore, control_atomic_json, control_read_json
from .linux_binding import LinuxBinding, exact_thread, locality_identity, reservation_path
from .linux_owner import LinuxThreadOwner, writer_pid
from .linux_health import owner_failure_reason
from .models import sha256_text
from .mvp_service import MvpWatchdogService
from .remote_ssh import RemoteSshTarget, _REMOTE_SCRIPT
from .storage import FileLock, StoreBusyError, InstructionStore
from .workspace_registry import TrackedWorkspace, WorkspaceRegistry
from .slack_mapping import SlackRelayTarget


def bind_for_operator(binding, workspace, lease_seconds):
    """Keep manual recovery on the same fence once automatic mode was activated."""
    path = binding.codex_home / "watchdog-control" / workspace.session_id / "owner.json"
    if not path.exists():
        return binding.bind(workspace, lease_seconds)
    store = ControlStore(binding.codex_home, workspace.session_id, workspace.repo_root)
    pid = writer_pid(binding.codex_home, workspace.session_id)
    token = store.claim_remote(str(uuid.uuid4()), locality_identity(),
                               "absent" if pid is None else "present", manual=True)
    if token is None:
        raise ControlError("control_existing_owner_not_released")
    try:
        with acting_as(store, token):
            result = binding.bind(workspace, lease_seconds)
            with store.guard(token) as value:
                value["auto_paused"] = False
                control_atomic_json(store.path, value)
            return result
    finally:
        store.release_remote(token, "absent")


class HostRemoteAdapter:
    """The same helper and journals, called locally after detached ownership."""
    supports_control = True

    def __init__(self, codex_home):
        self.namespace = {}
        exec(compile(_REMOTE_SCRIPT, "<watchdog-host-helper>", "exec"), self.namespace)
        self.namespace["remote_codex_home"] = lambda: Path(codex_home)

    def probe(self, target, *, control=None, pending_instruction_id=None, wake=None):
        request = dict(repo_path=target.repo_path, storage_key=target.storage_key)
        if target.expected_session_ids:
            request["expected_session_ids"] = list(target.expected_session_ids)
        if control is not None:
            request["control"] = control
        if pending_instruction_id is not None:
            request["pending_instruction_id"] = pending_instruction_id
        if wake is not None:
            request.update(action="wake", **wake)
        results = []
        self.namespace["emit"] = results.append
        self.namespace["run"](request)
        if len(results) != 1 or not isinstance(results[0], dict):
            raise ControlError("control_helper_result_invalid")
        return results[0]


class LinuxAutoWatchdog:
    def __init__(self, runtime, codex_home, *, executable=None, exclude=(), threads=(), stop_on_release=False,
                 owner_factory=LinuxThreadOwner, store_factory=ControlStore,
                 service_factory=MvpWatchdogService):
        self.runtime = Path(runtime).resolve()
        self.codex_home = Path(codex_home).resolve()
        self.executable = executable
        self.exclude = tuple(str(value).casefold() for value in exclude)
        self.threads = frozenset(threads)
        self.stop_on_release = stop_on_release
        self.instance = str(uuid.uuid4())
        self.locality = locality_identity()
        self.owner_factory = owner_factory
        self.store_factory = store_factory
        self.service_factory = service_factory
        self.controllers = {}
        self.stopping = False

    def _excluded(self, repo):
        return str(repo).casefold() in self.exclude or Path(repo).name.casefold() in self.exclude

    def _start(self, store, token, value):
        runtime = Path(value["runtime_path"])
        canonical_id = "control-" + sha256_text(store.thread_id + "\0" + value["repo_path"])[:32]
        workspace = TrackedWorkspace.create(canonical_id, Path(value["repo_path"]), store.thread_id)
        exact_thread(self.codex_home, workspace)
        binding = LinuxBinding(runtime, self.codex_home)
        if binding.pointer.exists():
            previous = binding.workspace(binding.load())
            if previous.repo_root != workspace.repo_root or previous.session_id != workspace.session_id:
                raise ControlError("control_previous_binding_mismatch")
            workspace = previous
        binding.bind(workspace, 86400)
        locks = ExitStack()
        try:
            locks.enter_context(FileLock(runtime / "locks" / "foreground-run.lock"))
            locks.enter_context(FileLock(reservation_path(self.codex_home, store.thread_id).with_suffix(".owner.lock")))
            target = value["remote_target"]
            target = RemoteSshTarget(target["authority"], target["repo_path"], target["storage_key"],
                                     (store.thread_id,), canonical_id)
            adapter = HostRemoteAdapter(self.codex_home)
            service = self.service_factory(runtime, codex_home=self.codex_home,
                                            registry=WorkspaceRegistry(runtime), remote_ssh_adapter=adapter,
                                            auto_discovery=False)
            relay = getattr(service, "slack_reply_relay", None)
            if relay is not None:
                relay.thread_store.cache_mappings(
                    store.slack_mappings(),
                    SlackRelayTarget(canonical_id, store.thread_id, "process_local"),
                )
                relay.start()
                locks.callback(relay.close)
            owner = self.owner_factory(binding, executable=self.executable, service=service)
            item = dict(store=store, token=token, owner=owner, service=service, target=target, locks=locks)
            self.controllers[store.thread_id] = item
            return item
        except Exception:
            locks.close()
            raise

    def _cycle(self, item):
        service, target, token = item["service"], item["target"], item["token"]
        probe = service.remote_control.probe(target, token)
        if probe.get("status") != "ok":
            raise ControlError(probe.get("reason", "control_probe_failed"))
        state = probe["control"].get("state")
        if not isinstance(state, dict):
            state = service._read_remote_state(service.state_path(target.workspace_id), target)
        state = dict(state, workspace_id=target.workspace_id, session_id=token["thread_id"])
        service._remote_control_scope = (target, token)
        try:
            return service._run_remote_owned(target, str(uuid.uuid4()), initial_state=state, initial_probe=probe)
        finally:
            service._remote_control_scope = None

    def step(self, *, observe=True):
        results = []
        paths = sorted((self.codex_home / "watchdog-control").glob("*/owner.json"))
        for path in paths:
            thread = path.parent.name
            if self.threads and thread not in self.threads:
                continue
            item = self.controllers.get(thread)
            try:
                value = control_read_json(path)
                repo = value.get("repo_path")
                if not isinstance(repo, str) or self._excluded(repo):
                    continue
                store = item["store"] if item else self.store_factory(self.codex_home, thread, repo)
                value = store.read()
                if item is None:
                    if self.stopping:
                        continue
                    pid = writer_pid(self.codex_home, thread)
                    token = store.claim_remote(self.instance, self.locality,
                                               "absent" if pid is None else "present", ttl=120)
                    if token is None:
                        results.append(dict(thread_sha256=sha256_text(thread), epoch=value["epoch"], state="standby"))
                        continue
                    try:
                        with acting_as(store, token):
                            item = self._start(store, token, store.read())
                    except Exception:
                        if writer_pid(self.codex_home, thread) is None:
                            store.release_remote(token, "absent")
                        raise
                token, owner = item["token"], item["owner"]
                store.renew(token, ttl=120)
                value = store.read()
                if self.stopping or value["state"] == "HANDOFF" or value.get("auto_paused") is True:
                    owner.release_requested = True
                with acting_as(store, token):
                    result = owner.step(observe=False)
                    # Publish the exact current writer PID under the same epoch;
                    # trusted hooks additionally verify their actual ancestry.
                    with store.guard(token) as current:
                        current["writer_pid"] = owner.client.process.pid if owner.client else None
                        control_atomic_json(store.path, current)
                    if result["owner_state"] == "owned" and observe:
                        self._cycle(item)
                        owner.health.report()
                if result["owner_state"] == "released":
                    if writer_pid(self.codex_home, thread) is not None:
                        raise ControlError("control_writer_not_released")
                    store.release_remote(token, "absent")
                    item["locks"].close()
                    self.controllers.pop(thread)
                    if self.stop_on_release:
                        self.stopping = True
                results.append(dict(thread_sha256=sha256_text(thread), epoch=token["epoch"],
                                    state=result["owner_state"], thread_status=result["thread_status"]))
            except (ControlError, AppServerError, StoreBusyError, ValueError, OSError) as exc:
                reason = owner_failure_reason(exc)
                blocked = dict(thread_sha256=sha256_text(thread), state="blocked", reason=reason)
                if item is not None and not isinstance(exc, (ControlBusy, StoreBusyError)):
                    try:
                        with acting_as(item["store"], item["token"]):
                            blocked["notification"] = item["owner"].report_failure(reason)
                    except (ControlError, OSError, ValueError) as error:
                        blocked["notification"] = dict(status="blocked", reason=owner_failure_reason(error))
                results.append(blocked)
        return results

    def run(self, interval_seconds=5, emit=None):
        if not 1 <= interval_seconds <= 60:
            raise ControlError("control_interval_must_be_1_to_60_seconds")
        root = self.codex_home / "watchdog-control"
        handlers = {}
        try:
            with FileLock(root / "agent.lock"):
                # This advertisement contains no secrets and is not liveness
                # proof; only the kernel lock, writer, epoch and lease are proof.
                control_atomic_json(root / "agent.json", dict(schema_version=1, instance=self.instance,
                                                               runtime_path=str(self.runtime)))
                for signum in (signal.SIGINT, signal.SIGTERM):
                    handlers[signum] = signal.signal(signum, self._stop)
                while True:
                    result = self.step()
                    if emit is not None:
                        emit(dict(schema_version=1, owners=result))
                    if self.stopping and not self.controllers:
                        return 0
                    time.sleep(interval_seconds)
        finally:
            for item in self.controllers.values():
                if item["owner"].client is not None:
                    item["owner"].client.close()
                item["locks"].close()
            for signum, handler in handlers.items():
                signal.signal(signum, handler)

    def _stop(self, signum, frame):
        self.stopping = True
