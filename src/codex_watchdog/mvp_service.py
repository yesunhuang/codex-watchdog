from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote
import uuid

from .git_adapter import GitObservation, LocalGitAdapter
from .models import sha256_text, utc_now
from .notifications import (
    EnvironmentNotifier,
    NotificationEvent,
    NotificationResult,
    notification_workspace_label,
)
from .queue_wake import REMOTE_UPDATE_PROMPT, QueueReceipt, QueueWakeDispatcher
from .remote_ssh import (
    RemoteSshAdapter,
    RemoteSshTarget,
    remote_ssh_adapter_from_environment,
)
from .service import (
    SERVICE_OBSERVATION_SCHEMA_VERSION,
    RunOnceService,
)
from .slack_mapping import SlackRelayTarget
from .slack_relay import SlackReplyRelay
from .storage import InstructionStore
from .workspace_discovery import EffectiveWorkspaceCatalog
from .workspace_registry import TrackedWorkspace, WorkspaceRegistry


MVP_STATE_SCHEMA_VERSION = 2
_NON_ERROR_WAKE_STATES = frozenset({"enqueued", "consumed_or_started", "started"})
_COMPLETED_WAKE_STATES = frozenset({"consumed_or_started", "started"})
_TRANSIENT_GIT_BLOCKER = "state_changed_during_observation"
_STOP_OUTPUT_MAX_CHARS = 32_000
_ROLLOUT_TAIL_MAX_BYTES = 1_048_576
_ROLLOUT_STOP_FALLBACK_DELAY_SECONDS = 45
_REMOTE_FAILURE_DEBOUNCE_CYCLES = 2
_ROLLOUT_STOP_NAMESPACE = uuid.UUID("d09e48db-cf18-46f0-893c-5e76227854cc")
_PARKED_STOP_OUTCOMES = frozenset(
    {
        "continuation_confirmed_then_parked",
        "duplicate_turn_parked",
        "grace_expired_parked",
        "handler_error_failed_open",
        "invalid_stop_context_parked",
        "lock_busy_failed_open",
        "lock_busy_grace_expired_parked",
        "loop_guard_parked",
    }
)

AtomicWriter = Callable[[Path, Dict[str, Any]], None]
Sleep = Callable[[float], None]
CycleSink = Callable[[Dict[str, Any]], None]
UtcClock = Callable[[], datetime]


def _utc_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MvpWorkspaceResult:
    workspace_id: str
    status: str
    stop_count: int
    stop_audit_id: Optional[str]
    initial_git: Optional[Dict[str, Any]]
    final_git: Optional[Dict[str, Any]]
    wake: Optional[Dict[str, Any]]
    notifications: Tuple[Dict[str, Any], ...]
    observation_path: str
    state_path: str
    audit_path: Optional[str]
    error_sha256: Optional[str] = None
    error_chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "status": self.status,
            "stop_count": self.stop_count,
            "stop_audit_id": self.stop_audit_id,
            "initial_git": self.initial_git,
            "final_git": self.final_git,
            "wake": self.wake,
            "notifications": list(self.notifications),
            "observation_path": self.observation_path,
            "state_path": self.state_path,
            "audit_path": self.audit_path,
            "error_sha256": self.error_sha256,
            "error_chars": self.error_chars,
        }


@dataclass(frozen=True)
class MvpCycleResult:
    cycle_id: str
    status: str
    started_at: str
    completed_at: str
    workspaces: Tuple[MvpWorkspaceResult, ...]
    discovery: Optional[Dict[str, Any]] = None
    error_sha256: Optional[str] = None
    error_chars: int = 0

    @property
    def ok(self) -> bool:
        return (
            self.status == "completed"
            and (self.discovery is None or self.discovery.get("status") != "error")
            and all(workspace.status == "completed" for workspace in self.workspaces)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "workspace_count": len(self.workspaces),
            "workspaces": [workspace.to_dict() for workspace in self.workspaces],
            "discovery": self.discovery,
            "error_sha256": self.error_sha256,
            "error_chars": self.error_chars,
        }


class MvpWatchdogService:
    """Run the watchdog workflow in one foreground process."""

    def __init__(
        self,
        runtime: Path,
        *,
        registry: Optional[WorkspaceRegistry] = None,
        git_adapter: Optional[LocalGitAdapter] = None,
        notifier: Optional[EnvironmentNotifier] = None,
        queue_dispatcher: Optional[QueueWakeDispatcher] = None,
        remote_ssh_adapter: Optional[RemoteSshAdapter] = None,
        slack_reply_relay: Optional[SlackReplyRelay] = None,
        codex_home: Optional[Path] = None,
        atomic_writer: Optional[AtomicWriter] = None,
        sleep: Sleep = time.sleep,
        clock: UtcClock = _utc_clock,
        replay_latest_stop: bool = False,
        auto_discovery: bool = True,
        discovery_exclude: Sequence[str] = (),
    ) -> None:
        if type(replay_latest_stop) is not bool:
            raise ValueError("replay_latest_stop must be a boolean")
        if type(auto_discovery) is not bool:
            raise ValueError("auto_discovery must be a boolean")
        self.runtime = Path(runtime)
        self.git_adapter = git_adapter if git_adapter is not None else LocalGitAdapter()
        self.notifier = (
            notifier if notifier is not None else EnvironmentNotifier(runtime)
        )
        self.queue_dispatcher = (
            queue_dispatcher
            if queue_dispatcher is not None
            else QueueWakeDispatcher(runtime, codex_home=codex_home)
        )
        self.remote_ssh_adapter = (
            remote_ssh_adapter
            if remote_ssh_adapter is not None
            else remote_ssh_adapter_from_environment()
        )
        notifier_config = getattr(self.notifier, "config", None)
        self.slack_reply_relay = (
            slack_reply_relay
            if slack_reply_relay is not None
            else SlackReplyRelay.from_notification_config(
                runtime,
                notifier_config,
                queue_dispatcher=self.queue_dispatcher,
                remote_ssh_adapter=self.remote_ssh_adapter,
            )
            if notifier_config is not None
            else None
        )
        queue_codex_home = getattr(self.queue_dispatcher, "codex_home", None)
        selected_codex_home = (
            codex_home
            if codex_home is not None
            else queue_codex_home
            if queue_codex_home is not None
            else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        )
        self.codex_home = Path(selected_codex_home).expanduser().resolve()
        if registry is not None:
            self.registry = registry
        elif auto_discovery:
            self.registry = EffectiveWorkspaceCatalog(
                runtime, codex_home=self.codex_home, exclude=discovery_exclude,
            )
        else:
            self.registry = WorkspaceRegistry(runtime)
        self.atomic_writer = (
            atomic_writer
            if atomic_writer is not None
            else InstructionStore._atomic_json
        )
        self.sleep = sleep
        self.clock = clock
        self.replay_latest_stop = replay_latest_stop
        self.discovery_exclude = tuple(
            value.strip().lower()
            for value in discovery_exclude
            if isinstance(value, str) and value.strip()
        )
        self.store = InstructionStore(runtime)
        self.observation_service = RunOnceService(
            runtime,
            registry=self.registry,
            git_adapter=self.git_adapter,
            atomic_writer=self.atomic_writer,
        )
        self.states = self.runtime / "service" / "state"
        self._rollout_paths: Dict[str, Path] = {}
        self._rollout_snapshots: Dict[
            str, Tuple[Path, int, int, Optional[Dict[str, Any]]]
        ] = {}
        self._observed_rollout_completions = set()

    def state_path(self, workspace_id: str) -> Path:
        return self.states / f"{sha256_text(workspace_id)}.json"

    def run_once(self, *, replay_latest_stop: Optional[bool] = None) -> MvpCycleResult:
        replay = self._effective_replay(replay_latest_stop)
        cycle_id = str(uuid.uuid4())
        started_at = utc_now()
        discovery_summary = None
        try:
            with self.observation_service.service_lock():
                workspaces = tuple(
                    sorted(
                        self.registry.list_workspaces(),
                        key=lambda workspace: workspace.workspace_id,
                    )
                )
                snapshot = getattr(self.registry, "last_snapshot", None)
                if snapshot is not None:
                    remote_workspace_count = sum(
                        getattr(window, "locality", None) == "remote_ssh"
                        and getattr(window, "tracking_status", "remote_adapter")
                        == "remote_adapter"
                        for window in snapshot.windows
                    )
                    discovery_summary = {
                        "status": snapshot.status,
                        "window_count": len(snapshot.windows),
                        "tracked_workspace_count": len(snapshot.effective_workspaces),
                        "remote_workspace_count": remote_workspace_count,
                        "issues": list(snapshot.issues),
                        "path": str(getattr(self.registry, "path", "")) or None,
                    }
                local_results = tuple(
                    self._run_workspace(
                        workspace,
                        cycle_id,
                        replay_latest_stop=replay,
                        resume_is_ambiguous=(
                            len(workspaces) != 1
                            and (self.runtime / "resume_prompt.md").exists()
                        ),
                        notify_resume_ambiguity=index == 0,
                    )
                    for index, workspace in enumerate(workspaces)
                )
                remote_targets = self._remote_targets(snapshot)
                remote_results = tuple(
                    self._run_remote_workspace(target, cycle_id)
                    for target in remote_targets
                )
                missing_remote_results = tuple(
                    self._run_missing_remote_workspace(target, cycle_id)
                    for target in self._missing_remote_targets(snapshot, remote_targets)
                )
                results = (
                    *local_results,
                    *remote_results,
                    *missing_remote_results,
                )
        except Exception as exc:
            digest, chars = self._error_summary(exc)
            return MvpCycleResult(
                cycle_id=cycle_id,
                status="error",
                started_at=started_at,
                completed_at=utc_now(),
                workspaces=(),
                error_sha256=digest,
                error_chars=chars,
            )
        return MvpCycleResult(
            cycle_id=cycle_id,
            status="completed",
            started_at=started_at,
            completed_at=utc_now(),
            workspaces=results,
            discovery=discovery_summary,
        )

    @staticmethod
    def _remote_targets(snapshot: Any) -> Tuple[RemoteSshTarget, ...]:
        if snapshot is None:
            return ()
        targets = []
        for window in getattr(snapshot, "windows", ()):
            if getattr(window, "locality", None) != "remote_ssh":
                continue
            if getattr(window, "tracking_status", "remote_adapter") != "remote_adapter":
                continue
            authority = getattr(window, "remote_authority", None)
            repo_path = getattr(window, "workspace_path", None)
            storage_key = getattr(window, "workspace_storage_key", None)
            if all(
                isinstance(value, str) and value
                for value in (authority, repo_path, storage_key,)
            ):
                session_candidates = getattr(window, "session_candidates", ())
                if not isinstance(session_candidates, tuple):
                    session_candidates = ()
                targets.append(
                    RemoteSshTarget(
                        authority,
                        repo_path,
                        storage_key,
                        expected_session_ids=session_candidates,
                    )
                )
        return tuple(sorted(targets, key=lambda target: target.workspace_id))

    def _run_remote_workspace(
        self, target: RemoteSshTarget, cycle_id: str
    ) -> MvpWorkspaceResult:
        workspace_id = target.workspace_id
        state_path = self.state_path(workspace_id)
        observation_path = (
            self.runtime
            / "service"
            / "observations"
            / f"{sha256_text(workspace_id)}.json"
        )
        state = self._read_remote_state(state_path, target)
        state["presence_tracking"] = True
        state["last_seen_at"] = utc_now()
        pending_instruction = state.get("pending_instruction_id")
        probe = self.remote_ssh_adapter.probe(
            target,
            pending_instruction_id=(
                pending_instruction if isinstance(pending_instruction, str) else None
            ),
        )
        notifications: List[Dict[str, Any]] = []
        if probe.get("status") != "ok":
            reason = str(probe.get("reason", "remote_adapter_unavailable"))
            self._record_remote_failure(target, state, cycle_id, reason, notifications)
            return self._finish_remote_cycle(
                target,
                cycle_id,
                state,
                probe,
                notifications,
                observation_path,
                state_path,
                status="error",
            )

        session_id = probe.get("session_id")
        if not isinstance(session_id, str):
            probe = {"status": "unavailable", "reason": "remote_thread_unresolved"}
            self._record_remote_failure(
                target, state, cycle_id, "remote_thread_unresolved", notifications,
            )
            return self._finish_remote_cycle(
                target,
                cycle_id,
                state,
                probe,
                notifications,
                observation_path,
                state_path,
                status="error",
            )
        self._record_remote_success(target, state, notifications, session_id)
        if state.get("session_id") not in (None, session_id):
            state.update(
                last_completion_turn=None,
                last_remote_oid=None,
                pending_remote_oid=None,
                pending_instruction_id=None,
            )
        state["session_id"] = session_id

        completion = probe.get("completion")
        stop_count = 0
        stop_audit_id = None
        if self._remote_completion_is_ready(completion) and completion.get(
            "turn_id"
        ) != state.get("last_completion_turn"):
            turn_id = str(completion["turn_id"])
            output = str(completion["final_output"])
            notifications.append(
                self._safe_notify(
                    NotificationEvent(
                        workspace_id=workspace_id,
                        event_type="codex_parked",
                        transition_fingerprint=self._fingerprint(
                            {
                                "session_id": session_id,
                                "turn_id": turn_id,
                                "workspace_id": workspace_id,
                            }
                        ),
                        subject=f"[Codex Watchdog] {target.label} stopped",
                        message=(
                            f"Repository: {target.label}\n"
                            f"Workspace ID: {workspace_id}\n"
                            f"Session: {session_id}\n"
                            "Outcome: remote_rollout_task_complete_fallback\n\n"
                            + output
                        ),
                        relay_target=self._remote_relay_target(target, session_id),
                    )
                )
            )
            state["last_completion_turn"] = turn_id
            stop_count = 1
            stop_audit_id = f"rollout:{turn_id}"

        git = probe.get("git") if isinstance(probe.get("git"), dict) else {}
        head_oid = git.get("head_oid")
        remote_oid = git.get("upstream_oid")
        wake = probe.get("wake") if isinstance(probe.get("wake"), dict) else None
        raw_git_blockers = git.get("blockers")
        if not isinstance(raw_git_blockers, (list, tuple)):
            raw_git_blockers = ()
        git_blockers = list(
            dict.fromkeys(
                blocker
                for blocker in raw_git_blockers
                if isinstance(blocker, str) and blocker != _TRANSIENT_GIT_BLOCKER
            )
        )
        if git_blockers:
            notifications.append(
                self._safe_notify(
                    NotificationEvent(
                        workspace_id=workspace_id,
                        event_type="git_attention",
                        transition_fingerprint=self._fingerprint(
                            {
                                "blockers": git_blockers,
                                "head_oid": head_oid,
                                "topology": git.get("topology"),
                                "workspace_id": workspace_id,
                            }
                        ),
                        subject=f"[Codex Watchdog] {target.label} needs Git attention",
                        message=(
                            "The read-only Remote-SSH workflow is blocked. Blockers: "
                            + ", ".join(git_blockers)
                        ),
                        relay_target=self._remote_relay_target(target, session_id),
                    )
                )
            )
        if wake is not None and wake.get("state") in _COMPLETED_WAKE_STATES:
            state["last_remote_oid"] = state.get("pending_remote_oid")
            state["pending_remote_oid"] = None
            state["pending_instruction_id"] = None
        if isinstance(remote_oid, str) and remote_oid == head_oid:
            state["last_remote_oid"] = remote_oid
            state["pending_remote_oid"] = None
            state["pending_instruction_id"] = None
        elif isinstance(remote_oid, str) and state.get("pending_remote_oid") is None:
            if (
                state.get("last_remote_oid") is None
                and git.get("topology")
                in ("remote_ahead", "remote_changed", "diverged")
            ) or (
                state.get("last_remote_oid") is not None
                and remote_oid != state.get("last_remote_oid")
            ):
                instruction_id = (
                    f"git:{sha256_text(workspace_id)[:16]}:{remote_oid.lower()}"
                )
                probe = self.remote_ssh_adapter.probe(
                    target,
                    wake={
                        "instruction_id": instruction_id,
                        "prompt": REMOTE_UPDATE_PROMPT,
                    },
                )
                wake = (
                    probe.get("wake")
                    if isinstance(probe.get("wake"), dict)
                    else {"state": "uncertain"}
                )
                state["pending_remote_oid"] = remote_oid
                state["pending_instruction_id"] = instruction_id
        elif isinstance(remote_oid, str) and state.get("last_remote_oid") is None:
            state["last_remote_oid"] = remote_oid

        if wake is not None and wake.get("state") == "uncertain":
            notifications.append(
                self._safe_notify(
                    NotificationEvent(
                        workspace_id=workspace_id,
                        event_type="wake_uncertain",
                        transition_fingerprint=self._fingerprint(
                            {
                                "instruction_id": state.get("pending_instruction_id"),
                                "workspace_id": workspace_id,
                            }
                        ),
                        subject=f"[Codex Watchdog] {target.label} wake is uncertain",
                        message=(
                            "The exact-thread Remote-SSH wake could not be confirmed. "
                            "WatchDog will not blindly resend it."
                        ),
                        relay_target=self._remote_relay_target(target, session_id),
                    )
                )
            )

        return self._finish_remote_cycle(
            target,
            cycle_id,
            state,
            {**probe, "git": git, "wake": wake},
            notifications,
            observation_path,
            state_path,
            status="completed",
            stop_count=stop_count,
            stop_audit_id=stop_audit_id,
        )

    def _read_remote_state(self, path: Path, target: RemoteSshTarget) -> Dict[str, Any]:
        base = {
            "schema_version": 1,
            "workspace_id": target.workspace_id,
            "authority": target.authority,
            "repo_path": target.repo_path,
            "session_id": None,
            "last_completion_turn": None,
            "last_remote_oid": None,
            "pending_remote_oid": None,
            "pending_instruction_id": None,
            "presence_tracking": False,
            "last_seen_at": None,
            "connection_status": "unknown",
            "consecutive_failures": 0,
            "last_failure_reason": None,
            "outage_id": None,
            "connection_alert_pending": False,
            "recovery_alert_pending": False,
        }
        if not path.is_file():
            return base
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return base
        if (
            not isinstance(value, dict)
            or value.get("workspace_id") != target.workspace_id
            or value.get("authority") != target.authority
            or value.get("repo_path") != target.repo_path
        ):
            return base
        return {**base, **value}

    def _missing_remote_targets(
        self, snapshot: Any, active_targets: Sequence[RemoteSshTarget],
    ) -> Tuple[RemoteSshTarget, ...]:
        if snapshot is None or getattr(snapshot, "status", None) == "error":
            return ()
        visible_ids = set()
        for window in getattr(snapshot, "windows", ()):
            if getattr(window, "locality", None) != "remote_ssh":
                continue
            authority = getattr(window, "remote_authority", None)
            repo_path = getattr(window, "workspace_path", None)
            if not all(
                isinstance(value, str) and value for value in (authority, repo_path)
            ):
                continue
            visible_ids.add(
                RemoteSshTarget(authority, repo_path, "0" * 32).workspace_id
            )
        visible_ids.update(target.workspace_id for target in active_targets)

        missing = []
        try:
            paths = tuple(self.states.glob("*.json"))
        except OSError:
            return ()
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(value, dict)
                or value.get("presence_tracking") is not True
            ):
                continue
            authority = value.get("authority")
            repo_path = value.get("repo_path")
            workspace_id = value.get("workspace_id")
            if not all(
                isinstance(item, str) and item
                for item in (authority, repo_path, workspace_id)
            ):
                continue
            target = RemoteSshTarget(authority, repo_path, "0" * 32)
            if target.workspace_id != workspace_id or workspace_id in visible_ids:
                continue
            if self._remote_target_is_excluded(target):
                continue
            try:
                target.host
            except ValueError:
                continue
            missing.append(target)
        return tuple(sorted(missing, key=lambda target: target.workspace_id))

    def _remote_target_is_excluded(self, target: RemoteSshTarget) -> bool:
        if not self.discovery_exclude:
            return False
        path = target.repo_path.lower()
        name = path.rstrip("/").rsplit("/", 1)[-1]
        uri = (
            f"vscode-remote://{quote(target.authority, safe='')}"
            f"{quote(target.repo_path, safe='/')}"
        ).lower()
        return any(value in self.discovery_exclude for value in (path, name, uri))

    def _run_missing_remote_workspace(
        self, target: RemoteSshTarget, cycle_id: str
    ) -> MvpWorkspaceResult:
        workspace_id = target.workspace_id
        state_path = self.state_path(workspace_id)
        observation_path = (
            self.runtime
            / "service"
            / "observations"
            / f"{sha256_text(workspace_id)}.json"
        )
        state = self._read_remote_state(state_path, target)
        notifications: List[Dict[str, Any]] = []
        reason = "remote_vscode_window_missing"
        self._record_remote_failure(target, state, cycle_id, reason, notifications)
        return self._finish_remote_cycle(
            target,
            cycle_id,
            state,
            {"status": "unavailable", "reason": reason},
            notifications,
            observation_path,
            state_path,
            status="error",
        )

    def _record_remote_failure(
        self,
        target: RemoteSshTarget,
        state: Dict[str, Any],
        cycle_id: str,
        reason: str,
        notifications: List[Dict[str, Any]],
    ) -> None:
        previous = state.get("consecutive_failures")
        failures = previous if type(previous) is int and previous >= 0 else 0
        failures += 1
        state["consecutive_failures"] = failures
        state["last_failure_reason"] = reason
        if (
            failures >= _REMOTE_FAILURE_DEBOUNCE_CYCLES
            and state.get("connection_status") != "disconnected"
        ):
            state["connection_status"] = "disconnected"
            state["outage_id"] = cycle_id
            state["connection_alert_pending"] = True
            state["recovery_alert_pending"] = False
        if state.get("connection_alert_pending") is not True:
            return
        outage_id = state.get("outage_id")
        if not isinstance(outage_id, str) or not outage_id:
            outage_id = cycle_id
            state["outage_id"] = outage_id
        notification = self._safe_notify(
            NotificationEvent(
                workspace_id=target.workspace_id,
                event_type="remote_adapter_attention",
                transition_fingerprint=self._fingerprint(
                    {"outage_id": outage_id, "workspace_id": target.workspace_id}
                ),
                subject=f"[Codex Watchdog] {target.label} monitoring lost",
                message=self._remote_failure_message(target, reason),
            )
        )
        notifications.append(notification)
        if self._notification_was_handled(notification):
            state["connection_alert_pending"] = False

    def _record_remote_success(
        self,
        target: RemoteSshTarget,
        state: Dict[str, Any],
        notifications: List[Dict[str, Any]],
        session_id: str,
    ) -> None:
        was_disconnected = state.get("connection_status") == "disconnected"
        state["connection_status"] = "connected"
        state["consecutive_failures"] = 0
        state["last_failure_reason"] = None
        state["connection_alert_pending"] = False
        if was_disconnected:
            state["recovery_alert_pending"] = True
        if state.get("recovery_alert_pending") is not True:
            if not was_disconnected:
                state["outage_id"] = None
            return
        outage_id = state.get("outage_id")
        if not isinstance(outage_id, str) or not outage_id:
            outage_id = str(uuid.uuid4())
            state["outage_id"] = outage_id
        notification = self._safe_notify(
            NotificationEvent(
                workspace_id=target.workspace_id,
                event_type="remote_adapter_recovered",
                transition_fingerprint=self._fingerprint(
                    {"outage_id": outage_id, "workspace_id": target.workspace_id}
                ),
                subject=f"[Codex Watchdog] {target.label} monitoring restored",
                message=(
                    f"WatchDog can monitor {target.label} again. The Remote-SSH "
                    "workspace and its exact Codex thread are reachable."
                ),
                relay_target=self._remote_relay_target(target, session_id),
            )
        )
        notifications.append(notification)
        if self._notification_was_handled(notification):
            state["recovery_alert_pending"] = False
            state["outage_id"] = None

    @staticmethod
    def _notification_was_handled(notification: Dict[str, Any]) -> bool:
        return notification.get("status") in {
            "sent",
            "sent_fallback",
            "audit_only",
            "suppressed",
        }

    @staticmethod
    def _remote_failure_message(target: RemoteSshTarget, reason: str) -> str:
        if reason == "remote_vscode_window_missing":
            return (
                f"WatchDog no longer sees the {target.label} Remote-SSH VS Code "
                "window. It may have been closed, reloaded, or disconnected. "
                "Monitoring will resume automatically if the window returns."
            )
        if reason == "remote_duo_upstream_unavailable":
            return (
                f"WatchDog found {target.label}, but its operator-authenticated "
                "MFA HPC Plink connection is no longer available. A fresh password "
                "and Duo approval are required; restart the watchdog launcher to "
                "open a new shared connection."
            )
        return (
            f"WatchDog found {target.label}, but could not monitor its exact "
            f"Remote-SSH Codex thread for two consecutive checks. Reason: {reason}. "
            "Check the VS Code remote connection and SSH authentication."
        )

    def _remote_completion_is_ready(self, completion: Any) -> bool:
        if not isinstance(completion, dict):
            return False
        if not all(
            isinstance(completion.get(key), str) and completion.get(key)
            for key in ("turn_id", "completed_at", "final_output")
        ):
            return False
        completed_at = self._parse_timestamp(completion["completed_at"])
        now = self.clock()
        if completed_at is None or not self._aware(now):
            return False
        return (
            now.astimezone(timezone.utc) - completed_at
        ).total_seconds() >= _ROLLOUT_STOP_FALLBACK_DELAY_SECONDS

    def _finish_remote_cycle(
        self,
        target: RemoteSshTarget,
        cycle_id: str,
        state: Dict[str, Any],
        probe: Dict[str, Any],
        notifications: List[Dict[str, Any]],
        observation_path: Path,
        state_path: Path,
        *,
        status: str,
        stop_count: int = 0,
        stop_audit_id: Optional[str] = None,
    ) -> MvpWorkspaceResult:
        completion = probe.get("completion")
        safe_completion = None
        if isinstance(completion, dict):
            safe_completion = {
                key: completion.get(key)
                for key in (
                    "turn_id",
                    "completed_at",
                    "final_output_sha256",
                    "final_output_chars",
                )
            }
        safe_probe = {
            "status": probe.get("status"),
            "reason": probe.get("reason"),
            "transport": probe.get("transport"),
            "session_id": probe.get("session_id"),
            "repo_path": target.repo_path,
            "authority": target.authority,
            "git": probe.get("git"),
            "completion": safe_completion,
            "wake": probe.get("wake"),
            "error_sha256": probe.get("error_sha256"),
            "error_chars": probe.get("error_chars", 0),
        }
        self.atomic_writer(observation_path, safe_probe)
        self.atomic_writer(state_path, state)
        git = probe.get("git") if isinstance(probe.get("git"), dict) else None
        result = MvpWorkspaceResult(
            workspace_id=target.workspace_id,
            status=status,
            stop_count=stop_count,
            stop_audit_id=stop_audit_id,
            initial_git=git,
            final_git=git,
            wake=probe.get("wake") if isinstance(probe.get("wake"), dict) else None,
            notifications=tuple(notifications),
            observation_path=str(observation_path),
            state_path=str(state_path),
            audit_path=None,
            error_sha256=probe.get("error_sha256"),
            error_chars=int(probe.get("error_chars", 0)),
        )
        audit_path = self.store.record_audit(
            {
                "event_type": "remote_ssh_workspace_cycle",
                "outcome": status,
                "cycle_id": cycle_id,
                "workspace_id": target.workspace_id,
                "session_id": probe.get("session_id"),
                "execution_locality": "remote_ssh",
                "remote_authority": target.authority,
                "transport": probe.get("transport"),
                "stop_count": stop_count,
                "stop_audit_id": stop_audit_id,
                "head_oid": git.get("head_oid") if git else None,
                "upstream_oid": git.get("upstream_oid") if git else None,
                "adapter_reason": probe.get("reason"),
            }
        )
        return MvpWorkspaceResult(**{**result.__dict__, "audit_path": str(audit_path)})

    def run(
        self,
        interval_seconds: float = 300.0,
        *,
        replay_latest_stop: Optional[bool] = None,
        emit: Optional[CycleSink] = None,
        max_cycles: Optional[int] = None,
    ) -> int:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles <= 0):
            raise ValueError("max_cycles must be a positive integer")
        replay = self._effective_replay(replay_latest_stop)
        completed = 0
        try:
            if self.slack_reply_relay is not None:
                self.slack_reply_relay.start()
            while max_cycles is None or completed < max_cycles:
                result = self.run_once(replay_latest_stop=replay)
                replay = False
                completed += 1
                if emit is not None:
                    emit(result.to_dict())
                if max_cycles is not None and completed >= max_cycles:
                    break
                self.sleep(interval_seconds)
        except KeyboardInterrupt:
            return 0
        finally:
            if self.slack_reply_relay is not None:
                self.slack_reply_relay.close()
        return 0

    def _run_workspace(
        self,
        workspace: TrackedWorkspace,
        cycle_id: str,
        *,
        replay_latest_stop: bool,
        resume_is_ambiguous: bool,
        notify_resume_ambiguity: bool,
    ) -> MvpWorkspaceResult:
        state_path = self.state_path(workspace.workspace_id)
        observation_path = self.observation_service.observation_path(
            workspace.workspace_id
        )
        try:
            state, stops = self._load_state_and_stops(
                workspace, replay_latest_stop=replay_latest_stop
            )
            latest_stop = stops[-1] if stops else None
            notifications: List[Dict[str, Any]] = []

            initial = self.git_adapter.observe(workspace.repo_root)
            if self._is_transient_observation(initial):
                initial = self.git_adapter.observe(workspace.repo_root)
            current = initial

            if self._is_transient_observation(initial):
                return self._finish_transient_cycle(
                    workspace,
                    cycle_id,
                    initial=initial,
                    current=current,
                    state_path=state_path,
                    observation_path=observation_path,
                )

            rollout_stop = self._rollout_stop_fallback(workspace)
            if rollout_stop is not None:
                stops = (*stops, rollout_stop)
                latest_stop = rollout_stop

            if latest_stop is not None:
                notifications.append(self._notify_stop(workspace, latest_stop, initial))

            # Inbound Git is detection plus transport only. The watcher never
            # fetches, fast-forwards, or otherwise synchronizes the worktree.
            # Codex receives the exact remote OID and owns synchronization in
            # the queued turn, where task context is available.
            remote_trigger: Optional[str] = None
            remote_oid = current.upstream_oid
            if remote_oid is not None:
                pending_oid = state["pending_remote_oid"]
                last_remote_oid = state["last_remote_oid"]
                changed = False
                if remote_oid == current.head_oid:
                    if pending_oid is not None or last_remote_oid != remote_oid:
                        state["last_remote_oid"] = remote_oid
                        state["pending_remote_oid"] = None
                        state["pending_remote_detected_at"] = None
                        changed = True
                elif pending_oid is None:
                    if last_remote_oid is None and current.topology in (
                        "remote_ahead",
                        "remote_changed",
                        "diverged",
                    ):
                        state["pending_remote_oid"] = remote_oid
                        state["pending_remote_detected_at"] = utc_now()
                        changed = True
                    elif last_remote_oid is None:
                        state["last_remote_oid"] = remote_oid
                        changed = True
                    elif remote_oid != last_remote_oid:
                        state["pending_remote_oid"] = remote_oid
                        state["pending_remote_detected_at"] = utc_now()
                        changed = True
                if changed:
                    self._write_state(state_path, state)
                if isinstance(state["pending_remote_oid"], str):
                    remote_trigger = state["pending_remote_oid"]

            wake: Optional[Dict[str, Any]] = None
            resume_path = self.runtime / "resume_prompt.md"
            if resume_is_ambiguous:
                if notify_resume_ambiguity:
                    notifications.append(self._notify_resume_ambiguity(workspace))
            wake = self._dispatch_one_wake(
                workspace,
                resume_path,
                remote_trigger,
                allow_resume=not resume_is_ambiguous,
            )
            if wake is not None and wake.get("kind") == "remote_update":
                if wake.get("status") in _COMPLETED_WAKE_STATES:
                    state["last_remote_oid"] = remote_trigger
                    state["pending_remote_oid"] = None
                    state["pending_remote_detected_at"] = None
                    self._write_state(state_path, state)

            notifications.extend(
                self._attention_notifications(workspace, current, wake)
            )
            self._persist_observation(workspace, current)

            self._write_state(state_path, state)
            result = MvpWorkspaceResult(
                workspace_id=workspace.workspace_id,
                status="completed",
                stop_count=len(stops),
                stop_audit_id=(
                    str(latest_stop.get("audit_id"))
                    if latest_stop is not None
                    else None
                ),
                initial_git=initial.to_dict(),
                final_git=current.to_dict(),
                wake=wake,
                notifications=tuple(notifications),
                observation_path=str(observation_path),
                state_path=str(state_path),
                audit_path=None,
            )
            audit_path = self._record_workspace_audit(
                cycle_id, workspace, result, latest_stop
            )
            return MvpWorkspaceResult(
                **{
                    **result.__dict__,
                    "audit_path": str(audit_path) if audit_path is not None else None,
                }
            )
        except Exception as exc:
            digest, chars = self._error_summary(exc)
            return MvpWorkspaceResult(
                workspace_id=workspace.workspace_id,
                status="error",
                stop_count=0,
                stop_audit_id=None,
                initial_git=None,
                final_git=None,
                wake=None,
                notifications=(),
                observation_path=str(observation_path),
                state_path=str(state_path),
                audit_path=None,
                error_sha256=digest,
                error_chars=chars,
            )

    def _load_state_and_stops(
        self, workspace: TrackedWorkspace, *, replay_latest_stop: bool
    ) -> Tuple[Dict[str, Any], Tuple[Dict[str, Any], ...]]:
        path = self.state_path(workspace.workspace_id)
        audit_files = self._audit_files()
        if not path.exists():
            state = {
                "schema_version": MVP_STATE_SCHEMA_VERSION,
                "workspace_id": workspace.workspace_id,
                "repo_root": str(workspace.repo_root),
                "session_id": workspace.session_id,
                "audit_cursor": audit_files[-1].name if audit_files else None,
                "last_remote_oid": None,
                "pending_remote_oid": None,
                "pending_remote_detected_at": None,
            }
            matches = self._matching_stops(workspace, audit_files)
            if replay_latest_stop and matches:
                return state, (matches[-1],)
            return state, ()

        state = self._read_state(path, workspace)
        cursor = state["audit_cursor"]
        candidates = tuple(
            audit_path
            for audit_path in audit_files
            if cursor is None or audit_path.name > cursor
        )
        if audit_files:
            state["audit_cursor"] = audit_files[-1].name
        return state, self._matching_stops(workspace, candidates)

    @staticmethod
    def _has_only_transient_blocker(blockers: Sequence[str]) -> bool:
        return tuple(blockers) == (_TRANSIENT_GIT_BLOCKER,)

    @classmethod
    def _is_transient_observation(cls, observation: GitObservation) -> bool:
        return cls._has_only_transient_blocker(observation.blockers)

    def _finish_transient_cycle(
        self,
        workspace: TrackedWorkspace,
        cycle_id: str,
        *,
        initial: GitObservation,
        current: GitObservation,
        state_path: Path,
        observation_path: Path,
        notifications: Tuple[Dict[str, Any], ...] = (),
    ) -> MvpWorkspaceResult:
        """Record a busy snapshot without consuming any durable pending work."""
        self._persist_observation(workspace, current)
        result = MvpWorkspaceResult(
            workspace_id=workspace.workspace_id,
            status="completed",
            stop_count=0,
            stop_audit_id=None,
            initial_git=initial.to_dict(),
            final_git=current.to_dict(),
            wake=None,
            notifications=notifications,
            observation_path=str(observation_path),
            state_path=str(state_path),
            audit_path=None,
        )
        audit_path = self._record_workspace_audit(
            cycle_id, workspace, result, latest_stop=None
        )
        return MvpWorkspaceResult(
            **{
                **result.__dict__,
                "audit_path": str(audit_path) if audit_path is not None else None,
            }
        )

    def _read_state(self, path: Path, workspace: TrackedWorkspace) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        legacy_keys = frozenset(
            {
                "schema_version",
                "workspace_id",
                "repo_root",
                "session_id",
                "audit_cursor",
                "pending_remote_oid",
                "pending_remote_detected_at",
            }
        )
        if (
            isinstance(state, dict)
            and state.get("schema_version") == 1
            and frozenset(state) == legacy_keys
        ):
            state = {
                **state,
                "schema_version": MVP_STATE_SCHEMA_VERSION,
                "last_remote_oid": None,
            }
        expected_keys = frozenset(
            {
                "schema_version",
                "workspace_id",
                "repo_root",
                "session_id",
                "audit_cursor",
                "last_remote_oid",
                "pending_remote_oid",
                "pending_remote_detected_at",
            }
        )
        if not isinstance(state, dict) or frozenset(state) != expected_keys:
            raise ValueError("MVP workspace state is malformed")
        if (
            state["schema_version"] != MVP_STATE_SCHEMA_VERSION
            or state["workspace_id"] != workspace.workspace_id
            or state["repo_root"] != str(workspace.repo_root)
            or state["session_id"] != workspace.session_id
        ):
            raise ValueError("MVP workspace state does not match registration")
        cursor = state["audit_cursor"]
        if cursor is not None and (
            not isinstance(cursor, str)
            or Path(cursor).name != cursor
            or not cursor.endswith(".json")
        ):
            raise ValueError("MVP audit cursor is malformed")
        pending = state["pending_remote_oid"]
        last_remote = state["last_remote_oid"]
        detected_at = state["pending_remote_detected_at"]
        if (pending is None) != (detected_at is None):
            raise ValueError("MVP pending remote state is malformed")
        if pending is not None and (
            not isinstance(pending, str)
            or len(pending) not in (40, 64)
            or any(character not in "0123456789abcdefABCDEF" for character in pending)
            or not isinstance(detected_at, str)
            or not detected_at
        ):
            raise ValueError("MVP pending remote state is malformed")
        if last_remote is not None and (
            not isinstance(last_remote, str)
            or len(last_remote) not in (40, 64)
            or any(
                character not in "0123456789abcdefABCDEF" for character in last_remote
            )
        ):
            raise ValueError("MVP last remote state is malformed")
        return state

    def _write_state(self, path: Path, state: Dict[str, Any]) -> None:
        self.atomic_writer(path, state)

    def _audit_files(self) -> Tuple[Path, ...]:
        audit = self.runtime / "audit"
        if not audit.is_dir():
            return ()
        return tuple(sorted(audit.glob("*.json"), key=lambda path: path.name))

    @classmethod
    def _matching_stops(
        cls, workspace: TrackedWorkspace, audit_files: Sequence[Path]
    ) -> Tuple[Dict[str, Any], ...]:
        matches: List[Dict[str, Any]] = []
        for audit_path in audit_files:
            try:
                with audit_path.open("r", encoding="utf-8") as handle:
                    event = json.load(handle)
                if not cls._is_matching_stop(workspace, event):
                    continue
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
            matches.append(event)
        return tuple(matches)

    @staticmethod
    def _is_matching_stop(workspace: TrackedWorkspace, event: Any) -> bool:
        if (
            not isinstance(event, dict)
            or event.get("schema_version") != 1
            or event.get("event_type") != "Stop"
            or not isinstance(event.get("hook_completed_at"), str)
            or not event["hook_completed_at"]
            or event.get("session_id") != workspace.session_id
            or not isinstance(event.get("audit_id"), str)
            or not event["audit_id"]
            or not isinstance(event.get("invocation_id"), str)
            or not event["invocation_id"]
            or event.get("outcome") not in _PARKED_STOP_OUTCOMES
            or not isinstance(event.get("workspace"), str)
        ):
            return False
        try:
            event_root = Path(event["workspace"]).expanduser().resolve()
        except (OSError, ValueError):
            return False
        return event_root == workspace.repo_root

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value:
            return None
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            return parsed.astimezone(timezone.utc)
        except (OverflowError, TypeError, ValueError):
            return None

    @staticmethod
    def _aware(value: Any) -> bool:
        return (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )

    def _dispatch_one_wake(
        self,
        workspace: TrackedWorkspace,
        resume_path: Path,
        remote_oid: Optional[str],
        *,
        allow_resume: bool = True,
    ) -> Optional[Dict[str, Any]]:
        attempted_resume = allow_resume and resume_path.exists()
        if not attempted_resume and remote_oid is None:
            return None
        wake_kind = "remote_update" if remote_oid is not None else "resume_prompt"
        try:
            with self.store.session_lock(workspace.session_id):
                if not self._workspace_is_current(workspace):
                    return {
                        "kind": wake_kind,
                        "status": "deferred_workspace_changed",
                        "instruction_id": None,
                        "thread_id": workspace.session_id,
                        "queue_message_id": None,
                        "returncode": -1,
                        "deduplicated": False,
                        "error_sha256": None,
                        "error_chars": 0,
                    }
                if remote_oid is not None:
                    receipt = self.queue_dispatcher.dispatch_remote_update(
                        workspace.session_id,
                        remote_oid,
                        workspace_id=workspace.workspace_id,
                    )
                    return self._wake_summary("remote_update", receipt)
                if attempted_resume:
                    receipt = self.queue_dispatcher.claim_and_dispatch_resume_prompt(
                        workspace.session_id
                    )
                    if receipt is not None:
                        return self._wake_summary("resume_prompt", receipt)
        except Exception as exc:
            digest, chars = self._error_summary(exc)
            return {
                "kind": wake_kind,
                "status": "exception",
                "instruction_id": None,
                "thread_id": workspace.session_id,
                "queue_message_id": None,
                "returncode": -1,
                "deduplicated": False,
                "error_sha256": digest,
                "error_chars": chars,
            }
        return None

    def _workspace_is_current(self, workspace: TrackedWorkspace) -> bool:
        validator = getattr(self.registry, "is_current", None)
        if validator is None:
            return True
        try:
            return bool(validator(workspace))
        except Exception:
            return False

    @staticmethod
    def _wake_summary(kind: str, receipt: QueueReceipt) -> Dict[str, Any]:
        return {
            "kind": kind,
            "status": receipt.status,
            "instruction_id": receipt.instruction_id,
            "thread_id": receipt.thread_id,
            "queue_message_id": receipt.queue_message_id,
            "returncode": receipt.returncode,
            "deduplicated": receipt.deduplicated,
            "error_sha256": None,
            "error_chars": 0,
        }

    def _notify_stop(
        self,
        workspace: TrackedWorkspace,
        stop: Dict[str, Any],
        observation: GitObservation,
    ) -> Dict[str, Any]:
        label = notification_workspace_label(
            workspace.workspace_id, workspace.repo_root
        )
        stable = {
            "session_id": workspace.session_id,
            "turn_id": stop.get("turn_id"),
            "workspace_id": workspace.workspace_id,
        }
        output_available = stop.get("last_assistant_message_available") is True
        output_chars = stop.get("last_output_chars")
        output, spool_path = self._load_stop_output(workspace, stop)
        message = (
            f"Repository: {label}\n"
            f"Workspace ID: {workspace.workspace_id}\n"
            f"Hook outcome: {stop['outcome']}\n"
            f"Git topology: {observation.topology or 'unknown'}\n"
            f"Tracked changes: {'yes' if observation.dirty_tracked else 'no'}\n"
            f"Last output available: {'yes' if output_available else 'no'}\n"
            f"Last output characters: {output_chars if isinstance(output_chars, int) else 0}"
        )
        if output is not None:
            rendered = output
            if len(rendered) > _STOP_OUTPUT_MAX_CHARS:
                rendered = (
                    rendered[:_STOP_OUTPUT_MAX_CHARS]
                    + "\n[Codex Watchdog truncated this notification output; "
                    f"original characters: {len(output)}]"
                )
            message += (
                "\n\nCodex final output:\n"
                "------------------------------------------------\n"
                f"{rendered}\n"
                "------------------------------------------------"
            )
        notification = self._safe_notify(
            NotificationEvent(
                workspace_id=workspace.workspace_id,
                event_type="codex_parked",
                transition_fingerprint=self._fingerprint(stable),
                subject=f"[Codex Watchdog] {label} stopped",
                message=message,
                relay_target=self._local_relay_target(workspace),
            )
        )
        if spool_path is not None and (
            (
                notification.get("status") in ("sent", "sent_fallback")
                and notification.get("channel") not in (None, "local_audit")
            )
            or (
                notification.get("status") == "suppressed"
                and notification.get("duplicate") is True
            )
        ):
            try:
                spool_path.unlink()
            except OSError:
                pass
        return notification

    def _load_stop_output(
        self, workspace: TrackedWorkspace, stop: Dict[str, Any]
    ) -> Tuple[Optional[str], Optional[Path]]:
        invocation_id = stop.get("invocation_id")
        try:
            canonical_invocation = str(uuid.UUID(invocation_id))
        except (AttributeError, ValueError):
            return None, None
        if canonical_invocation != str(invocation_id).lower():
            return None, None
        path = (
            self.runtime / "transient" / "stop-output" / f"{canonical_invocation}.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None, None
        expected_keys = frozenset(
            {
                "schema_version",
                "invocation_id",
                "session_id",
                "turn_id",
                "workspace",
                "last_assistant_message",
                "last_output_sha256",
                "last_output_chars",
            }
        )
        if (
            not isinstance(value, dict)
            or frozenset(value) != expected_keys
            or value.get("schema_version") != 1
            or value.get("invocation_id") != canonical_invocation
            or value.get("session_id") != workspace.session_id
            or value.get("turn_id") != stop.get("turn_id")
            or not isinstance(value.get("workspace"), str)
            or not isinstance(value.get("last_assistant_message"), str)
            or type(value.get("last_output_chars")) is not int
        ):
            return None, None
        try:
            output_workspace = Path(value["workspace"]).expanduser().resolve()
        except (OSError, ValueError):
            return None, None
        output = value["last_assistant_message"]
        expected_hash = sha256_text(output) if output else None
        if (
            output_workspace != workspace.repo_root
            or len(output) != value["last_output_chars"]
            or expected_hash != value.get("last_output_sha256")
            or value["last_output_chars"] != stop.get("last_output_chars")
            or expected_hash != stop.get("last_output_sha256")
        ):
            return None, None
        return output, path

    def _rollout_stop_fallback(
        self, workspace: TrackedWorkspace
    ) -> Optional[Dict[str, Any]]:
        completion = self._latest_rollout_completion(workspace)
        if completion is None:
            return None
        turn_id = completion["turn_id"]
        key = (workspace.session_id, turn_id)
        if key in self._observed_rollout_completions:
            return None
        completed_at = self._parse_timestamp(completion["completed_at"])
        now = self.clock()
        if completed_at is None or not self._aware(now):
            return None
        age_seconds = (now.astimezone(timezone.utc) - completed_at).total_seconds()
        if age_seconds < _ROLLOUT_STOP_FALLBACK_DELAY_SECONDS:
            return None
        if self._has_matching_stop_turn(workspace, turn_id):
            self._observed_rollout_completions.add(key)
            return None

        self._observed_rollout_completions.add(key)
        invocation_id = str(
            uuid.uuid5(_ROLLOUT_STOP_NAMESPACE, f"{workspace.session_id}\0{turn_id}",)
        )
        output = completion["last_agent_message"]
        output_hash = sha256_text(output) if output else None
        spool = self.runtime / "transient" / "stop-output" / f"{invocation_id}.json"
        self.atomic_writer(
            spool,
            {
                "schema_version": 1,
                "invocation_id": invocation_id,
                "session_id": workspace.session_id,
                "turn_id": turn_id,
                "workspace": str(workspace.repo_root),
                "last_assistant_message": output,
                "last_output_sha256": output_hash,
                "last_output_chars": len(output),
            },
        )
        return {
            "schema_version": 1,
            "event_type": "Stop",
            "audit_id": f"rollout:{turn_id}",
            "invocation_id": invocation_id,
            "session_id": workspace.session_id,
            "turn_id": turn_id,
            "workspace": str(workspace.repo_root),
            "outcome": "rollout_task_complete_fallback",
            "hook_started_at": completion["completed_at"],
            "hook_completed_at": completion["completed_at"],
            "recorded_at": completion["completed_at"],
            "stop_hook_active": False,
            "last_assistant_message_available": bool(output),
            "last_output_sha256": output_hash,
            "last_output_chars": len(output),
        }

    def _latest_rollout_completion(
        self, workspace: TrackedWorkspace
    ) -> Optional[Dict[str, Any]]:
        path = self._rollout_paths.get(workspace.session_id)
        if path is None or not path.is_file():
            sessions = self.codex_home / "sessions"
            matches = (
                list(sessions.rglob(f"rollout-*{workspace.session_id}.jsonl"))
                if sessions.is_dir()
                else []
            )
            if len(matches) != 1:
                return None
            path = matches[0]
            self._rollout_paths[workspace.session_id] = path
        try:
            stat = path.stat()
        except OSError:
            return None
        cached = self._rollout_snapshots.get(workspace.session_id)
        if (
            cached is not None
            and cached[0] == path
            and cached[1] == stat.st_size
            and cached[2] == stat.st_mtime_ns
        ):
            return cached[3]
        try:
            with path.open("rb") as handle:
                start = max(0, stat.st_size - _ROLLOUT_TAIL_MAX_BYTES)
                handle.seek(start)
                raw = handle.read()
        except OSError:
            return None
        if start:
            separator = raw.find(b"\n")
            raw = raw[separator + 1 :] if separator >= 0 else b""
        if raw and not raw.endswith(b"\n"):
            separator = raw.rfind(b"\n")
            raw = raw[: separator + 1] if separator >= 0 else b""

        latest_lifecycle: Optional[Dict[str, Any]] = None
        for raw_line in raw.splitlines():
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "event_msg":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")
            if payload_type == "task_started":
                latest_lifecycle = {"type": "task_started"}
                continue
            if payload_type != "task_complete":
                continue
            turn_id = payload.get("turn_id")
            completed_at = event.get("timestamp")
            output = payload.get("last_agent_message", "")
            if (
                not isinstance(turn_id, str)
                or not turn_id
                or self._parse_timestamp(completed_at) is None
                or not isinstance(output, str)
            ):
                latest_lifecycle = None
                continue
            latest_lifecycle = {
                "type": "task_complete",
                "turn_id": turn_id,
                "completed_at": completed_at,
                "last_agent_message": output,
            }
        completion = (
            latest_lifecycle
            if latest_lifecycle is not None
            and latest_lifecycle.get("type") == "task_complete"
            else None
        )
        self._rollout_snapshots[workspace.session_id] = (
            path,
            stat.st_size,
            stat.st_mtime_ns,
            completion,
        )
        return completion

    def _has_matching_stop_turn(
        self, workspace: TrackedWorkspace, turn_id: str
    ) -> bool:
        for audit_path in reversed(self._audit_files()):
            try:
                with audit_path.open("r", encoding="utf-8") as handle:
                    event = json.load(handle)
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if event.get("turn_id") == turn_id and self._is_matching_stop(
                workspace, event
            ):
                return True
        return False

    def _notify_resume_ambiguity(self, workspace: TrackedWorkspace) -> Dict[str, Any]:
        return self._safe_notify(
            NotificationEvent(
                workspace_id=workspace.workspace_id,
                event_type="resume_prompt_ambiguous",
                transition_fingerprint=self._fingerprint(
                    {"workspace_id": workspace.workspace_id, "ambiguous": True}
                ),
                subject="[Codex Watchdog] resume prompt needs a target",
                message=(
                    "resume_prompt.md was retained because more than one workspace "
                    "is registered. No wake was dispatched."
                ),
            )
        )

    def _attention_notifications(
        self,
        workspace: TrackedWorkspace,
        observation: GitObservation,
        wake: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        notifications: List[Dict[str, Any]] = []
        label = notification_workspace_label(
            workspace.workspace_id, workspace.repo_root
        )
        observation_issues: List[str] = [
            blocker
            for blocker in observation.blockers
            if blocker != _TRANSIENT_GIT_BLOCKER
        ]
        observation_issues = list(dict.fromkeys(observation_issues))
        if observation_issues:
            notifications.append(
                self._safe_notify(
                    NotificationEvent(
                        workspace_id=workspace.workspace_id,
                        event_type="git_attention",
                        transition_fingerprint=self._fingerprint(
                            {
                                "blockers": observation_issues,
                                "head_oid": observation.head_oid,
                                "topology": observation.topology,
                                "workspace_id": workspace.workspace_id,
                            }
                        ),
                        subject=(f"[Codex Watchdog] {label} needs Git attention"),
                        message=(
                            "No destructive recovery was attempted. Blockers: "
                            + ", ".join(observation_issues)
                        ),
                        relay_target=self._local_relay_target(workspace),
                    )
                )
            )

        if wake is not None and wake.get("status") == "deferred_workspace_changed":
            notifications.append(
                self._safe_notify(
                    NotificationEvent(
                        workspace_id=workspace.workspace_id,
                        event_type="wake_deferred",
                        transition_fingerprint=self._fingerprint(
                            {
                                "kind": wake.get("kind"),
                                "status": wake.get("status"),
                                "workspace_id": workspace.workspace_id,
                            }
                        ),
                        subject=f"[Codex Watchdog] {label} wake deferred",
                        message=(
                            "The live VS Code workspace/session mapping changed. "
                            "The pending wake was retained for a later safe cycle."
                        ),
                        relay_target=self._local_relay_target(workspace),
                    )
                )
            )
        elif wake is not None and wake.get("status") not in _NON_ERROR_WAKE_STATES:
            notifications.append(
                self._safe_notify(
                    NotificationEvent(
                        workspace_id=workspace.workspace_id,
                        event_type="wake_uncertain",
                        transition_fingerprint=self._fingerprint(
                            {
                                "instruction_id": wake.get("instruction_id"),
                                "kind": wake.get("kind"),
                                "status": wake.get("status"),
                                "workspace_id": workspace.workspace_id,
                            }
                        ),
                        subject=(f"[Codex Watchdog] {label} wake is uncertain"),
                        message=(
                            f"Wake kind: {wake.get('kind')}\n"
                            f"Status: {wake.get('status')}\n"
                            "The watchdog will not blindly resend an uncertain wake."
                        ),
                        relay_target=self._local_relay_target(workspace),
                    )
                )
            )
        return notifications

    @staticmethod
    def _local_relay_target(workspace: TrackedWorkspace) -> SlackRelayTarget:
        return SlackRelayTarget(
            workspace_id=workspace.workspace_id,
            thread_id=workspace.session_id,
            execution_locality="process_local",
        )

    @staticmethod
    def _remote_relay_target(
        target: RemoteSshTarget, thread_id: str
    ) -> SlackRelayTarget:
        return SlackRelayTarget(
            workspace_id=target.workspace_id,
            thread_id=thread_id,
            execution_locality="remote_ssh",
            remote_authority=target.authority,
            remote_repo_path=target.repo_path,
            remote_storage_key=target.storage_key,
        )

    def _safe_notify(self, event: NotificationEvent) -> Dict[str, Any]:
        try:
            result: NotificationResult = self.notifier.notify(event)
            return {"event_type": event.event_type, **result.to_dict()}
        except Exception as exc:
            digest, chars = self._error_summary(exc)
            return {
                "event_type": event.event_type,
                "status": "exception",
                "channel": None,
                "event_fingerprint": event.event_fingerprint(),
                "duplicate": False,
                "attempted_channels": [],
                "configuration_issues": [],
                "state_path": None,
                "state_persisted": False,
                "error_sha256": digest,
                "error_chars": chars,
            }

    def _persist_observation(
        self, workspace: TrackedWorkspace, observation: GitObservation
    ) -> None:
        transition = RunOnceService._transition_fingerprint(
            workspace, observation, None
        )
        self.atomic_writer(
            self.observation_service.observation_path(workspace.workspace_id),
            {
                "schema_version": SERVICE_OBSERVATION_SCHEMA_VERSION,
                "workspace_id": workspace.workspace_id,
                "repo_root": str(workspace.repo_root),
                "session_id": workspace.session_id,
                "execution_locality": workspace.execution_locality,
                "git": observation.to_dict(),
                "service_error": None,
                "transition_fingerprint": transition,
                "recorded_at": utc_now(),
            },
        )

    def _record_workspace_audit(
        self,
        cycle_id: str,
        workspace: TrackedWorkspace,
        result: MvpWorkspaceResult,
        latest_stop: Optional[Dict[str, Any]],
    ) -> Optional[Path]:
        initial = result.initial_git or {}
        final = result.final_git or {}
        try:
            return self.store.record_audit(
                {
                    "event_type": "mvp_workspace_cycle",
                    "outcome": result.status,
                    "cycle_id": cycle_id,
                    "workspace_id": workspace.workspace_id,
                    "session_id": workspace.session_id,
                    "stop_count": result.stop_count,
                    "stop_audit_id": result.stop_audit_id,
                    "stop_invocation_id": (
                        latest_stop.get("invocation_id")
                        if latest_stop is not None
                        else None
                    ),
                    "stop_outcome": (
                        latest_stop.get("outcome") if latest_stop is not None else None
                    ),
                    "head_before": initial.get("head_oid"),
                    "head_after": final.get("head_oid"),
                    "topology_before": initial.get("topology"),
                    "topology_after": final.get("topology"),
                    "dirty_tracked_before": initial.get("dirty_tracked"),
                    "dirty_tracked_after": final.get("dirty_tracked"),
                    "untracked_present": final.get("untracked_present"),
                    "wake_kind": result.wake.get("kind") if result.wake else None,
                    "wake_status": (result.wake.get("status") if result.wake else None),
                    "wake_instruction_id": (
                        result.wake.get("instruction_id") if result.wake else None
                    ),
                    "notification_statuses": [
                        notification.get("status")
                        for notification in result.notifications
                    ],
                }
            )
        except Exception:
            return None

    def _effective_replay(self, value: Optional[bool]) -> bool:
        if value is None:
            return self.replay_latest_stop
        if type(value) is not bool:
            raise ValueError("replay_latest_stop must be a boolean")
        return value

    @staticmethod
    def _fingerprint(value: Dict[str, Any]) -> str:
        return sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _error_summary(error: Exception) -> Tuple[str, int]:
        error_type = f"{type(error).__module__}.{type(error).__qualname__}"
        try:
            message = str(error)
        except Exception:
            message = ""
        detail = f"{error_type}\0{message}"
        return sha256_text(detail), len(detail)
