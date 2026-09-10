"""Durable loss/recovery alerts for one explicitly owned Linux thread."""

import re
import uuid

from .app_server import AppServerError
from .control_context import current_control, effect_guard
from .control_state import ControlError, control_read_json
from .linux_binding import LinuxBindingError
from .models import sha256_text
from .notifications import NotificationEvent, notification_workspace_label
from .remote_control import notify_local_control
from .storage import FileLock, InstructionStore


def owner_failure_reason(exc):
    if isinstance(exc, (LinuxBindingError, AppServerError, ControlError)):
        reason = str(exc)
        if re.fullmatch(r"[a-z][a-z0-9_]{1,95}", reason):
            return reason
    return "linux_owner_monitoring_failed"


class LinuxOwnerHealth:
    def __init__(self, runtime, codex_home, workspace, notifier):
        self.codex_home = codex_home
        self.workspace = workspace
        self.notifier = notifier
        self.identity = sha256_text(workspace.session_id + "\0" + str(workspace.repo_root))
        self.path = runtime / "linux" / "health" / (self.identity + ".json")
        self.lock = runtime / "locks" / ("linux-health-" + self.identity + ".lock")

    def report(self, reason=None):
        thread = self.workspace.session_id
        with FileLock(self.lock):
            with effect_guard(self.codex_home, thread, "state"):
                state = control_read_json(self.path) if self.path.exists() else {
                    "schema_version": 1, "identity": self.identity, "health": "healthy",
                }
                if (state.get("schema_version") != 1 or state.get("identity") != self.identity
                        or state.get("health") not in ("healthy", "lost")
                        or state.get("pending") is True and not isinstance(state.get("outage_id"), str)):
                    raise ControlError("linux_health_state_invalid")
                health = "lost" if reason is not None else "healthy"
                if state["health"] != health:
                    if health == "lost":
                        state["outage_id"] = str(uuid.uuid4())
                    state.update(health=health, pending=True)
                if not state.get("pending"):
                    return state.get("notification") if health == "lost" else None
                state["reason"] = reason
                # Persist the event identity before sending. Restart/recovery
                # cannot silently create another alert for the same outage.
                InstructionStore._atomic_json(self.path, state)
            label = notification_workspace_label(self.workspace.workspace_id, self.workspace.repo_root)
            event = NotificationEvent(
                workspace_id=self.workspace.workspace_id,
                event_type="linux_owner_attention" if reason is not None else "linux_owner_recovered",
                transition_fingerprint=state["outage_id"],
                subject=f"[Codex Watchdog] {label} detached monitoring {health if reason else 'restored'}",
                message=(
                    f"The Linux WatchDog can no longer watch or control the existing Codex thread "
                    f"{thread[:8]} in {label}. Reason: {reason}. Check the Linux WatchDog service "
                    "and reopen the existing thread in VS Code if intervention is needed."
                    if reason is not None else
                    f"The Linux WatchDog can watch and control the existing Codex thread "
                    f"{thread[:8]} in {label} again."
                ),
            )
            selected = current_control()
            if selected is not None:
                # Use the local canonical ledger, not an SSH/database probe
                # that could itself be the reason monitoring was lost.
                notification = notify_local_control(*selected, event, self.notifier)
            else:
                # Legacy explicit bindings must also fence first activation.
                with effect_guard(self.codex_home, thread, "notification"):
                    notification = self.notifier.notify(event).to_dict()
            notification = dict(notification, event_type=event.event_type)
            with effect_guard(self.codex_home, thread, "state"):
                state["notification"] = notification
                state["pending"] = notification.get("status") not in (
                    "sent", "sent_fallback", "audit_only", "suppressed",
                )
                InstructionStore._atomic_json(self.path, state)
            return notification
