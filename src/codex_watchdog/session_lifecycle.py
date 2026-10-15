"""Current-session lifecycle and claim-before-send Hello; never wake Codex."""
import json
import time

from .models import sha256_text
from .notifications import NotificationEvent, notification_workspace_label
from .relay import RelayTarget
from .session_activity import SessionActivity, hello_claim_key
from .session_routes import SessionRoutes


class SessionLifecycle:
    def __init__(self, runtime, notifier, *, clock=None):
        self.runtime, self.notifier, self.clock = runtime, notifier, clock

    def _store(self):
        # Linux handoff must carry this exact session's generation in the same
        # portable authority as its current route/ACL, never a node-local cache.
        authority = getattr(self.notifier, "relay_authority", None)
        return SessionActivity(authority.runtime if authority is not None else self.runtime,
                               clock=self.clock)

    def observe(self, workspace, signals, *, verified_owner=False,
                enrollment_at=None, legacy=False):
        if verified_owner is not True or not signals.get("exact"):
            return dict(status="UNKNOWN", reason="native_owner_or_activity_unverified",
                        notifications=())
        from .activation_request import requested_activity
        activity_store=self._store()
        try:
            request=requested_activity(self.runtime,workspace.session_id,now=activity_store._now())
            if request is not None:enrollment_at=max(enrollment_at or 0,request)
        except (OSError,ValueError):
            pass  # Optional intent uncertainty leaves independent work alone.
        state = activity_store.observe(workspace.session_id,
            genuine_at=signals.get("genuine_at"), enrollment_at=enrollment_at,
            turn_active=signals.get("turn_active"), queue_active=signals.get("queue_active"),
            legacy=legacy)
        state['monitor_current_work'] = not (signals.get('turn_active') is False and signals.get('queue_active') is False)
        notifications = []
        if state["status"] == "ACTIVE" and state["hello_eligible"]:
            try:
                hello = self._hello(workspace, state)
                if hello is not None:
                    notifications.append(hello)
            except Exception:
                # Activity/tracking was already published. Hello uncertainty is
                # not a gate for observation or new independently eligible work.
                state["hello_error"] = "activation_hello_unavailable"
        return dict(state, notifications=tuple(notifications))

    def is_active(self, session_id):
        try:
            return self._store().is_active(session_id)
        except (ValueError, OSError):
            return True  # Corrupt history cannot terminate native monitoring.

    def _hello(self, workspace, state):
        config = self.notifier.config
        providers = []
        original_births = []
        for provider in config.interactive_providers:
            if provider == "slack":
                if not config.slack_relay_configured:
                    continue
                scope, fallback = config.slack_channel_id, config.slack_channel_id
                thread_store = self.notifier.slack_thread_store
            else:
                options = getattr(config, provider)
                if not options.relay_configured:
                    continue
                scope = options.scope
                fallback = options.chat_id if provider == "lark" else options.destination
                thread_store = getattr(self.notifier, provider + "_thread_store")
            try:
                destination = SessionRoutes(thread_store, provider=provider, scope=scope).destination(workspace.session_id) or fallback
                route = sha256_text(json.dumps(dict(session_id=workspace.session_id,
                    provider=provider, scope=scope, destination=destination), sort_keys=True))
                if self._store().claim_hello(workspace.session_id, provider, scope, route):
                    saved = self._store().snapshot(workspace.session_id)
                    if saved is None or saved["generation"] != state["generation"]:
                        continue
                    original_births.append(saved["hello_claims"][hello_claim_key(provider, scope)]["claimed_at"])
                    providers.append(provider)
            except Exception:
                continue  # Refuse only the provider whose exact state is unknown.
        if not providers:
            return None
        authority=getattr(self.notifier,'relay_authority',None)
        if authority is not None:
            remote=authority.store.read()['remote_target']
            target=RelayTarget(workspace.workspace_id,workspace.session_id,'remote_ssh',
                remote['authority'],remote['repo_path'],remote['storage_key'])
            authority._resolve_target(target) if hasattr(authority,'_resolve_target') else None
        else:
            target=RelayTarget(workspace.workspace_id,workspace.session_id,workspace.execution_locality)
        label = notification_workspace_label(workspace.workspace_id, str(workspace.repo_root))
        event = NotificationEvent(workspace_id=workspace.workspace_id, event_type="session_hello",
            transition_fingerprint=sha256_text(json.dumps(dict(session_id=workspace.session_id,
                generation=state["generation"], providers=providers), sort_keys=True)),
            subject=f"[Codex Watchdog] {label} monitoring active",
            message="Hello. Monitoring is active for this existing Codex session. Reply here to send a new command; previous expired threads remain closed.",
            relay_target=target,
            provider_filter=tuple(providers), source_born=min(original_births))
        return self.notifier.notify(event).to_dict()
