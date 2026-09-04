from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, Optional

from .models import sha256_text
from .queue_wake import QueueWakeDispatcher
from .remote_ssh import RemoteSshAdapter, RemoteSshTarget
from .slack_mapping import (
    SlackThreadMapping,
    SlackThreadStore,
    valid_slack_channel_id,
    valid_slack_timestamp,
    valid_slack_user_id,
)
from .storage import FileLock


_DELIVERED_STATES = frozenset({"enqueued", "consumed_or_started", "started"})


@dataclass(frozen=True)
class SlackReplyResult:
    status: str
    workspace_id: Optional[str] = None
    instruction_id: Optional[str] = None
    delivery_status: Optional[str] = None
    duplicate: bool = False
    error_sha256: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "workspace_id": self.workspace_id,
            "instruction_id": self.instruction_id,
            "delivery_status": self.delivery_status,
            "duplicate": self.duplicate,
            "error_sha256": self.error_sha256,
        }


class SlackReplyRelay:
    """Relay allowlisted replies from mapped Slack threads to exact Codex threads."""

    def __init__(
        self,
        runtime: Path,
        *,
        bot_token: str,
        app_token: str,
        channel_id: str,
        allowed_user_ids: tuple[str, ...],
        queue_dispatcher: QueueWakeDispatcher,
        remote_ssh_adapter: RemoteSshAdapter,
        thread_store: Optional[SlackThreadStore] = None,
    ) -> None:
        if not isinstance(bot_token, str) or not bot_token.startswith("xoxb-"):
            raise ValueError("Slack bot token is invalid")
        if not isinstance(app_token, str) or not app_token.startswith("xapp-"):
            raise ValueError("Slack app token is invalid")
        if not valid_slack_channel_id(channel_id):
            raise ValueError("Slack relay channel id is invalid")
        if not allowed_user_ids or any(
            not valid_slack_user_id(value) for value in allowed_user_ids
        ):
            raise ValueError("Slack relay requires valid allowlisted user ids")
        self.runtime = Path(runtime)
        self.bot_token = bot_token
        self.app_token = app_token
        self.channel_id = channel_id
        self.allowed_user_ids = frozenset(allowed_user_ids)
        self.queue_dispatcher = queue_dispatcher
        self.remote_ssh_adapter = remote_ssh_adapter
        self.thread_store = (
            thread_store if thread_store is not None else SlackThreadStore(runtime)
        )
        self._app = None
        self._handler = None
        self._listener_lock: Optional[FileLock] = None

    @classmethod
    def from_notification_config(
        cls,
        runtime: Path,
        config: Any,
        *,
        queue_dispatcher: QueueWakeDispatcher,
        remote_ssh_adapter: RemoteSshAdapter,
    ) -> Optional["SlackReplyRelay"]:
        if getattr(config, "slack_relay_configured", False) is not True:
            return None
        return cls(
            runtime,
            bot_token=config.slack_bot_token,
            app_token=config.slack_app_token,
            channel_id=config.slack_channel_id,
            allowed_user_ids=config.slack_allowed_user_ids,
            queue_dispatcher=queue_dispatcher,
            remote_ssh_adapter=remote_ssh_adapter,
        )

    def start(self) -> None:
        if self._handler is not None:
            return
        listener_lock = FileLock(self.runtime / "locks" / "slack-socket-mode.lock")
        listener_lock.__enter__()
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler

            app = App(token=self.bot_token)
            app.event("message")(self._handle_bolt_message)
            handler = SocketModeHandler(app, self.app_token)
            handler.connect()
        except ImportError as exc:
            listener_lock.__exit__(type(exc), exc, exc.__traceback__)
            raise RuntimeError("Slack relay requires the slack-bolt package") from exc
        except BaseException as exc:
            listener_lock.__exit__(type(exc), exc, exc.__traceback__)
            raise
        self._app = app
        self._handler = handler
        self._listener_lock = listener_lock

    def close(self) -> None:
        handler = self._handler
        listener_lock = self._listener_lock
        self._handler = None
        self._app = None
        self._listener_lock = None
        try:
            if handler is not None:
                handler.close()
        finally:
            if listener_lock is not None:
                listener_lock.__exit__(None, None, None)

    def _handle_bolt_message(
        self, event: Dict[str, Any], body: Dict[str, Any], client: Any
    ) -> None:
        result = self.handle_message(event, event_id=body.get("event_id"))
        response = self._response_text(result)
        channel = event.get("channel")
        thread_ts = event.get("thread_ts")
        if (
            response is not None
            and valid_slack_channel_id(channel)
            and valid_slack_timestamp(thread_ts)
        ):
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=response,
                unfurl_links=False,
                unfurl_media=False,
            )

    def handle_message(
        self, event: Any, *, event_id: Optional[str] = None
    ) -> SlackReplyResult:
        if not isinstance(event, dict) or event.get("type") != "message":
            return SlackReplyResult("ignored_event_type")
        if (
            event.get("subtype") is not None
            or event.get("bot_id") is not None
            or event.get("bot_profile") is not None
        ):
            return SlackReplyResult("ignored_bot_or_subtype")
        user_id = event.get("user")
        if user_id not in self.allowed_user_ids:
            return SlackReplyResult("ignored_unauthorized")
        channel_id = event.get("channel")
        if channel_id != self.channel_id:
            return SlackReplyResult("ignored_channel")
        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts")
        if not valid_slack_timestamp(thread_ts) or not valid_slack_timestamp(
            message_ts
        ):
            return SlackReplyResult("ignored_not_thread_reply")
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            return SlackReplyResult("ignored_empty")
        mapping = self.thread_store.lookup_thread(channel_id, thread_ts)
        if mapping is None:
            return SlackReplyResult("ignored_unknown_thread")

        stable_event_id = self._event_key(event, event_id)
        instruction_id = "slack:" + sha256_text(stable_event_id)[:40]
        claimed, previous_status = self.thread_store.claim_reply(
            event_key=stable_event_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            instruction_id=instruction_id,
            text=text,
        )
        if not claimed:
            return SlackReplyResult(
                "duplicate",
                workspace_id=mapping.target.workspace_id,
                instruction_id=instruction_id,
                delivery_status=previous_status,
                duplicate=True,
            )

        try:
            delivery_status = self._dispatch(mapping, instruction_id, text)
        except Exception as exc:
            digest = self._error_digest(exc)
            self.thread_store.finish_reply(
                stable_event_id,
                state_value="uncertain",
                delivery_status="exception",
                error_sha256=digest,
            )
            return SlackReplyResult(
                "uncertain",
                workspace_id=mapping.target.workspace_id,
                instruction_id=instruction_id,
                delivery_status="exception",
                error_sha256=digest,
            )

        delivered = delivery_status in _DELIVERED_STATES
        self.thread_store.finish_reply(
            stable_event_id,
            state_value="delivered" if delivered else "uncertain",
            delivery_status=delivery_status,
        )
        return SlackReplyResult(
            "queued" if delivered else "uncertain",
            workspace_id=mapping.target.workspace_id,
            instruction_id=instruction_id,
            delivery_status=delivery_status,
        )

    def _dispatch(
        self, mapping: SlackThreadMapping, instruction_id: str, text: str
    ) -> str:
        target = mapping.target
        if target.execution_locality == "process_local":
            return self.queue_dispatcher.dispatch(
                target.thread_id, instruction_id, text, "slack_reply",
            ).status
        assert target.remote_authority is not None
        assert target.remote_repo_path is not None
        assert target.remote_storage_key is not None
        remote_target = RemoteSshTarget(
            target.remote_authority,
            target.remote_repo_path,
            target.remote_storage_key,
            expected_session_ids=(target.thread_id,),
        )
        probe = self.remote_ssh_adapter.probe(
            remote_target, wake={"instruction_id": instruction_id, "prompt": text},
        )
        if probe.get("status") != "ok":
            return str(probe.get("reason", "remote_adapter_unavailable"))
        wake = probe.get("wake")
        if not isinstance(wake, dict):
            return "remote_wake_missing"
        return str(wake.get("state", "uncertain"))

    @staticmethod
    def _event_key(event: Dict[str, Any], event_id: Optional[str]) -> str:
        if isinstance(event_id, str) and event_id:
            return "event:" + event_id
        client_message_id = event.get("client_msg_id")
        if isinstance(client_message_id, str) and client_message_id:
            return "client:" + client_message_id
        return "message:" + str(event["channel"]) + ":" + str(event["ts"])

    @staticmethod
    def _response_text(result: SlackReplyResult) -> Optional[str]:
        if result.status == "queued":
            return "Queued for the exact existing Codex thread."
        if result.status == "uncertain":
            return (
                "WatchDog could not confirm exact-thread delivery and will not "
                "blindly resend this reply."
            )
        return None

    @staticmethod
    def _error_digest(error: Exception) -> str:
        error_type = f"{type(error).__module__}.{type(error).__qualname__}"
        try:
            detail = str(error)
        except Exception:
            detail = ""
        return sha256_text(
            json.dumps(["slack_reply_dispatch_failed", error_type, detail])
        )
