from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, Optional
from types import SimpleNamespace

from .models import MAX_PROMPT_CHARS, sha256_text
from .queue_wake import QueueWakeDispatcher
from .remote_ssh import RemoteSshAdapter, RemoteSshTarget
from .slack_mapping import (
    SlackThreadMapping,
    SlackThreadStore,
    valid_slack_channel_id,
    valid_slack_timestamp,
    valid_slack_user_id,
)
from .storage import FileLock, StoreBusyError
from .control_state import ControlError, control_read_json
from .remote_control import RemoteControlClient
from .notifications import NotificationEvent
from .slack_presentation import slack_message_with_host
from .relay import ExactThreadRelay, ReplyResult as SlackReplyResult


_DELIVERED_STATES = frozenset({"enqueued", "consumed_or_started", "started"})




class SlackReplyRelay(ExactThreadRelay):
    """Relay allowlisted replies from mapped Slack threads to exact Codex threads."""

    def __init__(
        self,
        runtime: Path,
        *,
        bot_token: str,
        app_token: Optional[str],
        channel_id: str,
        allowed_user_ids: tuple[str, ...],
        queue_dispatcher: QueueWakeDispatcher,
        remote_ssh_adapter: RemoteSshAdapter,
        thread_store: Optional[SlackThreadStore] = None,
        reply_mode: str = "socket",
        route_api: Optional[Any] = None,
    ) -> None:
        if not isinstance(bot_token, str) or not bot_token.startswith("xoxb-"):
            raise ValueError("Slack bot token is invalid")
        if reply_mode not in ("socket", "poll"):
            raise ValueError("Slack reply mode is invalid")
        if reply_mode == "socket" and (not isinstance(app_token, str) or not app_token.startswith("xapp-")):
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
        self.reply_mode = reply_mode
        self.channel_id = channel_id
        self.allowed_user_ids = frozenset(allowed_user_ids)
        self.queue_dispatcher = queue_dispatcher
        self.remote_ssh_adapter = remote_ssh_adapter
        self.thread_store = (
            thread_store if thread_store is not None else SlackThreadStore(runtime)
        )
        self._route_api = route_api
        self._app = None
        self._handler = None
        self._listener_lock: Optional[FileLock] = None
        self._poller = None
        if reply_mode == "poll":
            from .slack_poll import SlackPollingThreadStore
            self.thread_store = thread_store or SlackPollingThreadStore(runtime)

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
            reply_mode=getattr(config, "slack_reply_mode", "socket"),
        )

    def start(self) -> None:
        if self.reply_mode == "poll":
            if self._poller is None:
                from .slack_poll import SlackReplyPoller
                poller = SlackReplyPoller(self)
                poller.start()
                self._poller = poller
            return
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
        if self._poller is not None:
            self._poller.close()
            self._poller = None
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
        self.acknowledge(event, result, client)

    def acknowledge(self, event, result, client) -> None:
        response = self._response_text(result)
        channel = event.get("channel")
        thread_ts = event.get("thread_ts")
        if (
            response is not None
            and valid_slack_channel_id(channel)
            and valid_slack_timestamp(thread_ts)
        ):
            hello_ok = None
            if result.status == "route_bound":
                hello_ok = self._send_binding_hello(result.instruction_id, client)
            if result.status in ("route_bound", "route_unbound"):
                from .session_routes import SessionRoutes
                routes = SessionRoutes(self.thread_store, scope=self.channel_id)
                if not routes.claim_ack(result.instruction_id):
                    return
                if hello_ok is False:
                    response = self._response_text_hello_failed(result)
            response = slack_message_with_host(response)

            def send_ack(_event):
                client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=response,
                                        unfurl_links=False, unfurl_media=False)
                return SimpleNamespace(to_dict=lambda: {"status": "sent", "channel": "slack"})

            if result.control is None:
                send_ack(None)
            else:
                control, target, token = result.control
                acknowledgement = NotificationEvent(result.workspace_id, "slack_reply_ack",
                                                       result.instruction_id, "Reply queued", response)
                try:
                    control.notify(target, token, acknowledgement, SimpleNamespace(notify=send_ack))
                except ControlError:
                    # A stale or uncertain acknowledgement is never resent.
                    return

    def _send_binding_hello(self, event_key: str, client: Any) -> bool:
        """Post a top-level hello to the destination channel and record the mapping.

        Returns True only when the send succeeded and record_thread was called.
        Sets hello_status=uncertain on any failure; never re-sends on second call.
        """
        from .session_routes import SessionRoutes
        routes = SessionRoutes(self.thread_store, scope=self.channel_id)
        claim = routes.claim_binding_hello(event_key)
        if claim is None:
            return False
        target, destination, fingerprint = claim
        text = slack_message_with_host(
            f"Hello! Binding successful. "
            f"Workspace: {target.workspace_id}, Session: {target.thread_id}. "
            f"Reply in this thread to send messages to this exact Codex session."
        )
        try:
            resp = client.chat_postMessage(
                channel=destination,
                text=text,
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception:
            routes.finish_binding_hello(event_key, status="uncertain")
            return False
        if isinstance(resp, dict):
            resp_dict = resp
        elif hasattr(resp, "data") and isinstance(resp.data, dict):
            resp_dict = resp.data
        else:
            resp_dict = {}
        if (resp_dict.get("ok") is not True
                or resp_dict.get("channel") != destination
                or not valid_slack_timestamp(resp_dict.get("ts"))):
            routes.finish_binding_hello(event_key, status="uncertain")
            return False
        new_ts = resp_dict["ts"]
        try:
            self.thread_store.record_thread(destination, new_ts, target, fingerprint)
        except Exception:
            routes.finish_binding_hello(event_key, status="uncertain")
            return False
        routes.finish_binding_hello(event_key, status="sent")
        return True

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
        if not valid_slack_channel_id(channel_id):
            return SlackReplyResult("ignored_channel")
        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts")
        if not valid_slack_timestamp(thread_ts) or not valid_slack_timestamp(
            message_ts
        ):
            return SlackReplyResult("ignored_not_thread_reply")
        if message_ts == thread_ts:
            return SlackReplyResult("ignored_not_thread_reply")
        text = event.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_PROMPT_CHARS:
            return SlackReplyResult("ignored_empty")
        try:
            mapping = self.thread_store.lookup_thread(channel_id, thread_ts)
        except StoreBusyError:
            return SlackReplyResult("deferred")
        except Exception as exc:
            return SlackReplyResult("rejected_state_or_collision", error_sha256=self._error_digest(exc))
        if mapping is None:
            if channel_id != self.channel_id:
                return SlackReplyResult("ignored_channel")
            return SlackReplyResult("ignored_unknown_thread")

        stable_event_id = self._event_key(event, event_id)

        from .slack_route_commands import parse_route_command
        try:
            route_cmd = parse_route_command(text)
        except ValueError as exc:
            return SlackReplyResult(
                "rejected_route_command",
                workspace_id=mapping.target.workspace_id,
                delivery_status=str(exc),
            )
        if route_cmd is not None:
            return self._handle_route_command(
                event, stable_event_id, route_cmd, mapping, channel_id, thread_ts, user_id, text,
            )

        instruction_id = "slack:" + sha256_text(stable_event_id)[:40]
        try:
            claimed, previous_status = self.thread_store.claim_reply(
                event_key=stable_event_id, channel_id=channel_id, thread_ts=thread_ts,
                instruction_id=instruction_id, text=text)
        except StoreBusyError:
            return SlackReplyResult("deferred")  # No claim or admission occurred.
        except Exception as exc:
            return SlackReplyResult("rejected_state_or_collision", error_sha256=self._error_digest(exc))
        if not claimed:
            return SlackReplyResult(
                "duplicate",
                workspace_id=mapping.target.workspace_id,
                instruction_id=instruction_id,
                delivery_status=previous_status,
                duplicate=True,
            )

        try:
            controlled = self._controlled_reply(mapping, stable_event_id, instruction_id, text,
                                                check_legacy_receipt=False)
            if controlled is not None:
                # Transport failure may follow an admitted remote wake. Close the
                # ticket conservatively; never offer a second human reply/replay.
                self.thread_store.finish_reply(stable_event_id,
                    state_value="delivered" if controlled.delivery_status in _DELIVERED_STATES else "uncertain",
                    delivery_status=controlled.delivery_status or controlled.status)
                if controlled.status == "deferred":
                    from dataclasses import replace
                    controlled = replace(controlled, status="uncertain")
                return controlled
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



    def _handle_route_command(
        self,
        event: Any,
        event_key: str,
        route_cmd: tuple,
        mapping: Any,
        channel_id: str,
        thread_ts: str,
        user_id: str,
        text: str,
    ) -> "SlackReplyResult":
        from .session_routes import SessionRoutes
        from .slack_route_commands import resolve_destination
        operation, argument = route_cmd
        routes = SessionRoutes(self.thread_store, provider="slack", scope=self.channel_id)

        destination: Optional[str] = None
        try:
            if operation == "bind":
                destination = resolve_destination(argument, self._get_route_api())
            result = routes.apply(
                event_key, channel_id, thread_ts, user_id, text, destination,
                command_ts=event.get("ts"),
            )
        except StoreBusyError:
            return SlackReplyResult("deferred")
        except Exception as exc:
            from urllib.error import HTTPError
            if isinstance(exc, HTTPError) and exc.code == 429:
                raise  # Preserve the poller's provider Retry-After handling.
            if isinstance(exc, ValueError) and str(exc) == "route_destination_missing_scope":
                return SlackReplyResult(
                    "rejected_route_command",
                    workspace_id=mapping.target.workspace_id,
                    delivery_status="route_destination_missing_scope",
                )
            return SlackReplyResult(
                "rejected_route_command",
                workspace_id=mapping.target.workspace_id,
                delivery_status="route_validation_failed",
                error_sha256=self._error_digest(exc),
            )

        apply_status = result["status"]

        if apply_status == "stale":
            return SlackReplyResult(
                "route_stale",
                workspace_id=mapping.target.workspace_id,
                instruction_id=event_key,
                duplicate=True,
            )

        if apply_status == "duplicate":
            return SlackReplyResult(
                "duplicate",
                workspace_id=mapping.target.workspace_id,
                instruction_id=event_key,
                duplicate=True,
            )

        if apply_status in ("bound", "unbound"):
            status = "route_bound" if apply_status == "bound" else "route_unbound"
            return SlackReplyResult(
                status,
                workspace_id=mapping.target.workspace_id,
                instruction_id=event_key,
                delivery_status=destination,
            )

        return SlackReplyResult(
            "rejected_route_command",
            workspace_id=mapping.target.workspace_id,
            delivery_status="route_apply_unexpected_status",
        )

    def _get_route_api(self):
        if self._route_api is not None:
            return self._route_api
        from .slack_route_commands import slack_conversations_get
        bot_token = self.bot_token
        def _api(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            return slack_conversations_get(bot_token, method, params)
        return _api

    @staticmethod
    def _event_key(event: Dict[str, Any], event_id: Optional[str]) -> str:
        if isinstance(event_id, str) and event_id:
            return "event:" + event_id
        client_message_id = event.get("client_msg_id")
        if isinstance(client_message_id, str) and client_message_id:
            return "client:" + client_message_id
        return "message:" + str(event["channel"]) + ":" + str(event["ts"])

    @staticmethod
    def _response_text_hello_failed(result: SlackReplyResult) -> Optional[str]:
        dest = result.delivery_status
        mention = f"<#{dest}>" if dest else "the destination channel"
        return (
            f"Session route bound to {mention}. "
            f"Binding saved, but the destination hello could not be confirmed."
        )

    @staticmethod
    def _response_text(result: SlackReplyResult) -> Optional[str]:
        if result.status == "queued":
            return "Queued for the exact existing Codex thread."
        if result.status == "uncertain":
            return (
                "WatchDog could not confirm exact-thread delivery and will not "
                "blindly resend this reply."
            )
        if result.status == "route_bound":
            dest = result.delivery_status
            mention = f"<#{dest}>" if dest else "the destination channel"
            return f"Session route bound to {mention}."
        if result.status == "route_unbound":
            return "Session route unbound. Notifications return to the default channel."
        if result.status == "rejected_route_command":
            if result.delivery_status == "route_destination_missing_scope":
                return (
                    "WatchDog cannot verify this channel: the bot is missing a required Slack scope. "
                    "Add channels:read (public) or groups:read (private) under Bot Token Scopes "
                    "and reinstall the existing Slack app to authorize, then retry."
                )
            return ("WatchDog could not change this session's destination. "
                    "Use bind #channel or unbind, and check that the bot can access the channel.")
        return None
