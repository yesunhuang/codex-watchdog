from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import copy
import json
from pathlib import Path
from typing import Any, Dict, Optional
from types import SimpleNamespace

from .models import MAX_PROMPT_CHARS, sha256_text
from .queue_wake import QueueWakeDispatcher
from .session_access_commands import parse_slack_access, reserved_control
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
from .relay_authority import relay_guarded, relay_ack_guarded


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
        access_api: Optional[Any] = None,
        access_sender: Optional[Any] = None,
        session_acl: Optional[Any] = None,
        bot_api: Optional[Any] = None,
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
        self._access_api = access_api
        self._access_sender = access_sender
        self._session_acl = session_acl
        self._bot_api = bot_api
        self._bot_verifier = None
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
    ) -> SlackReplyResult:
        result = self.handle_message(event, event_id=body.get("event_id"),
            authenticated_team_id=body.get("team_id"),
            authenticated_app_id=body.get("api_app_id"))
        self.acknowledge(event, result, client)
        return result

    @relay_guarded
    def handle_polled_message(self, event):
        # Only the poller calls this entry point, after validating its whole page.
        text = event.get("text") if isinstance(event, dict) else None
        if self._bot_marked(event) or (isinstance(text, str) and text.strip()
                                      and text.strip().split()[0].lower() == "bot"):
            return self.handle_message(event, _from_poll=True)
        return self.handle_message(event)

    @relay_ack_guarded
    def acknowledge(self, event, result, client) -> None:
        if self._bot_marked(event) or result.status.startswith("bot_"):
            self._acknowledge_bot(result, client)
            return
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

    def _acknowledge_bot(self, result, client) -> None:
        """Report admission once without changing dispatch or reply tickets."""
        from .slack_bot_receipts import BotCommandReceipts
        if result.bot_receipt is None:
            return
        context, outcome = result.bot_receipt
        receipts = BotCommandReceipts(self._get_bot_access())
        try:
            claim = receipts.claim(context, outcome)
        except Exception:
            # Exact saved authority could not be established. Receipt failure
            # never alters or retries the independently recorded task dispatch.
            return
        if claim is None:
            return
        response = slack_message_with_host(self._bot_receipt_text(claim))
        posted = dict(status="uncertain", message_ts=None)

        def send_ack(_event):
            try:
                # Bolt's WebClient retries connection failures by default. An
                # isolated copy gives an uncertain receipt one provider attempt
                # without changing human sends or the listener client.
                sender = copy(client)
                if hasattr(sender, "retry_handlers"):
                    sender.retry_handlers = []
                if hasattr(sender, "timeout"):
                    sender.timeout = min(sender.timeout, 10)
                reply = sender.chat_postMessage(
                    channel=claim.channel_id, thread_ts=claim.thread_ts,
                    text=response, unfurl_links=False, unfurl_media=False)
                if (reply.get("ok") is True and reply.get("channel") == claim.channel_id
                        and valid_slack_timestamp(reply.get("ts"))):
                    posted.update(status="sent", message_ts=reply["ts"])
            except Exception:
                pass  # Provider details stay private; uncertain sends never retry.
            return SimpleNamespace(to_dict=lambda: dict(status=posted["status"], channel="slack"))

        try:
            if result.control is None:
                send_ack(None)
            else:
                control, target, token = result.control
                acknowledgement = NotificationEvent(
                    context.target.workspace_id, "slack_bot_receipt",
                    "slack-bot-receipt-" + claim.key, "Bot request receipt", response)
                control.notify(target, token, acknowledgement, SimpleNamespace(notify=send_ack))
        except Exception:
            # Never refresh or reacquire an owner capability just for feedback.
            pass
        try:
            receipts.finish(claim, status=posted["status"], message_ts=posted["message_ts"])
        except Exception:
            pass  # The durable pre-send claim still prohibits another attempt.

    @staticmethod
    def _bot_receipt_text(claim) -> str:
        outcomes = {
            "queued": "accepted and queued. This receipt does not confirm execution or completion.",
            "uncertain": "delivery is uncertain. The request ID remains reserved; do not replay or resend it.",
            "duplicate": "request ID was already reserved. This message queued no additional task; prior execution is not confirmed.",
            "rejected": "not accepted: this reply ticket is closed. This message queued no task.",
        }
        return "WatchDog request " + claim.context.request_id + ": " + outcomes[claim.outcome]

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

    @relay_guarded
    def handle_message(
        self, event: Any, *, event_id: Optional[str] = None,
        authenticated_team_id: Optional[str] = None,
        authenticated_app_id: Optional[str] = None, _from_poll: bool = False,
    ) -> SlackReplyResult:
        if not isinstance(event, dict) or event.get("type") != "message":
            return SlackReplyResult("ignored_event_type")
        if self._bot_marked(event):
            return self._handle_bot_instruction(event, event_id, authenticated_team_id,
                                                authenticated_app_id, _from_poll)
        if (
            event.get("subtype") is not None
            or event.get("bot_id") is not None
            or event.get("bot_profile") is not None
        ):
            return SlackReplyResult("ignored_bot_or_subtype")
        user_id = event.get("user")
        if not valid_slack_user_id(user_id):
            return SlackReplyResult("ignored_unauthorized")
        try:
            if self._get_bot_access().known_user(user_id):
                return SlackReplyResult("bot_ignored", delivery_status="bot_identity_missing")
        except StoreBusyError:
            return SlackReplyResult("deferred")
        except Exception:
            return SlackReplyResult("rejected_state_or_collision")
        is_admin = user_id in self.allowed_user_ids
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
            if not is_admin:
                return SlackReplyResult("ignored_unauthorized")
            return SlackReplyResult("ignored_unknown_thread")

        stable_event_id = self._event_key(event, event_id)
        if text.strip().split(None, 1)[0].lower() == "bot":
            if not is_admin:
                return SlackReplyResult("ignored_unauthorized")
            return self._handle_bot_control(event, stable_event_id, mapping,
                authenticated_team_id, authenticated_app_id, _from_poll)

        # Reserved control check: deny reserved commands from non-admins entirely;
        # parse add/remove/access for admins before the ordinary relay path.
        if reserved_control(text) or text.strip().split(None, 1)[0].lower() == "!codex":
            if not is_admin:
                return SlackReplyResult("ignored_unauthorized")
            # Admins: bind/unbind fall through to the route-command path below.
            # add/remove/access are handled here as access control commands.
            first_word = text.strip().split(None, 1)[0].lower()
            if first_word not in ("bind", "unbind"):
                try:
                    parsed = parse_slack_access(text)
                except ValueError as exc:
                    return SlackReplyResult("rejected_route_command",
                                           workspace_id=mapping.target.workspace_id,
                                           delivery_status=str(exc))
                if parsed is not None:
                    operation, raw_delegate_id = parsed
                    return self._handle_access_command(
                        event, stable_event_id, mapping, channel_id,
                        thread_ts, user_id, text, operation, raw_delegate_id,
                    )
                # WD-BIND- or other reserved non-bind: reject
                return SlackReplyResult("rejected_route_command",
                                       workspace_id=mapping.target.workspace_id)

        from .slack_route_commands import parse_route_command
        previous = None
        if text.strip().split(None, 1)[0].lower() in ("bind", "unbind"):
            if not is_admin:
                return SlackReplyResult("ignored_unauthorized")
            from .session_routes import SessionRoutes
            try:
                previous = SessionRoutes(self.thread_store, scope=self.channel_id).command_source(
                    stable_event_id, channel_id, thread_ts)
            except StoreBusyError:
                return SlackReplyResult("deferred")
            except Exception as exc:
                if isinstance(exc, ValueError) and str(exc) == "route_source_closed":
                    return SlackReplyResult("ignored_closed_ticket", workspace_id=mapping.target.workspace_id)
                return SlackReplyResult("rejected_state_or_collision", error_sha256=self._error_digest(exc))
        if previous is not None:
            # Validate the retained receipt before parsing changed text or doing
            # provider lookup. Closed history can only deduplicate this event.
            return self._handle_route_command(
                event, stable_event_id, ("duplicate", None), mapping,
                channel_id, thread_ts, user_id, text, previous=previous)
        try:
            route_cmd = parse_route_command(text)
        except ValueError as exc:
            return SlackReplyResult(
                "rejected_route_command",
                workspace_id=mapping.target.workspace_id,
                delivery_status=str(exc),
            )
        if route_cmd is not None:
            if not is_admin:
                return SlackReplyResult("ignored_unauthorized")
            return self._handle_route_command(
                event, stable_event_id, route_cmd, mapping, channel_id, thread_ts, user_id, text,
                previous=previous,
            )

        # Ordinary message relay. Admission callback atomically verifies target and ticket for
        # both admins and delegates.
        instruction_id = "slack:" + sha256_text(stable_event_id)[:40]
        access = self._get_session_access()
        source_key = SlackThreadStore.thread_key(channel_id, thread_ts)
        message_key = channel_id + "\0" + message_ts
        identity = dict(channel=channel_id, thread_ts=thread_ts, message_ts=message_ts,
                        user=user_id, text_sha256=sha256_text(text), target=mapping.target.to_dict())
        fingerprint = sha256_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))
        admission = access.make_admission(source_key, user_id, mapping.target,
                                         event_key=stable_event_id, message_key=message_key)
        try:
            claimed, previous_status = self.thread_store.claim_reply(
                event_key=stable_event_id, channel_id=channel_id, thread_ts=thread_ts,
                instruction_id=instruction_id, text=text, admission=admission,
                message_key=message_key, payload_sha256=fingerprint)
        except StoreBusyError:
            return SlackReplyResult("deferred")  # No claim or admission occurred.
        except Exception as exc:
            return SlackReplyResult("rejected_state_or_collision", error_sha256=self._error_digest(exc))
        if not claimed:
            if previous_status == "unauthorized":
                return SlackReplyResult("ignored_unauthorized")
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



    @staticmethod
    def _bot_marked(event):
        return isinstance(event, dict) and (
            event.get("subtype") == "bot_message"
            or any(key in event for key in ("bot_id", "bot_profile", "app_id", "api_app_id")))

    def _get_bot_access(self):
        from .slack_bot_acl import BotSessionAccess
        return BotSessionAccess(self.thread_store, self.channel_id)

    def _get_bot_verifier(self):
        if self._bot_verifier is None:
            from .slack_bot_identity import BotVerifier, bot_api_call
            api = self._bot_api
            if api is None:
                api = lambda method, params: bot_api_call(self.bot_token, method, params)
            self._bot_verifier = BotVerifier(api)
        return self._bot_verifier

    def _bot_context(self, team_id, app_id, from_poll):
        context = self._get_bot_verifier().context()
        # Socket metadata describes the receiving installation. Poll responses
        # have no envelope; their context comes from the authenticated API token.
        if not from_poll and (team_id != context.team_id or app_id != context.app_id):
            raise ValueError("bot_transport_context_mismatch")
        return context

    def _handle_bot_control(self, event, event_key, mapping, team_id, app_id, from_poll):
        from .slack_bot_identity import BotVerificationDeferred, parse_bot_control_event
        user = event["user"]
        source_key = self.thread_store.thread_key(event["channel"], event["thread_ts"])
        access = self._get_bot_access()
        try:
            operation, mentioned = parse_bot_control_event(event)
            if self._get_session_access().preflight(source_key, user, mapping.target) is None:
                return SlackReplyResult("ignored_closed_ticket")
            context = self._bot_context(team_id, app_id, from_poll)
            if any(key in event and event[key] != context.team_id for key in ("team", "team_id")):
                raise ValueError("bot_transport_context_mismatch")
            verifier = self._get_bot_verifier()
            verifier.human_owner(user)
            principal = None
            if mentioned is not None:
                recorded = [value for value in access.principals(mapping.target.thread_id)
                            if value.user_id == mentioned and value.team_id == context.team_id]
                if operation == "remove" and len(recorded) == 1:
                    principal = recorded[0]
                else:
                    principal = verifier.bot(mentioned)
            fingerprint = sha256_text(json.dumps(dict(
                kind="slack_bot_control", source=source_key, user=user,
                target=mapping.target.to_dict(), operation=operation,
                principal=principal.to_dict() if principal else None,
                text_sha256=sha256_text(event["text"]),
                message_ts=event["ts"], receiving=context.to_dict()),
                sort_keys=True, separators=(",", ":")))
            result = access.control(event_key=event_key,
                message_key=event["channel"] + "\0" + event["ts"],
                source_key=source_key, user_id=user, operation=operation,
                principal=principal, expected_target=mapping.target,
                admin_ids=self.allowed_user_ids, payload_sha256=fingerprint)
        except (StoreBusyError, BotVerificationDeferred):
            return SlackReplyResult("deferred")
        except Exception as exc:
            return SlackReplyResult("bot_control_rejected", error_sha256=self._error_digest(exc))
        if result["status"] in ("duplicate", "closed", "unauthorized"):
            return SlackReplyResult("bot_control_" + result["status"])
        key = result["operation_key"]
        try:
            confirmation = access.claim_confirmation(key)
            if confirmation is None:
                return SlackReplyResult("bot_control_duplicate")
            delegates = confirmation["principals"]
            listing = ", ".join("<@" + value.user_id + ">" for value in delegates) or "none"
            text = slack_message_with_host(
                "Bot access " + result["status"] + ". Session bots: " + listing
                + ". Human administrators retain independent access. "
                + "Reply to this new notification for this exact session.")
            sent = self._get_access_sender()(text, key, event["channel"])
            if (sent.get("chat_id") != event["channel"]
                    or not valid_slack_timestamp(sent.get("message_id"))):
                raise ValueError("bot_confirmation_address_invalid")
            self.thread_store.record_thread(sent["chat_id"], sent["message_id"],
                confirmation["target"], confirmation["fingerprint"])
            access.finish_confirmation(key, "sent")
            self.thread_store.finish_reply(event_key, state_value="delivered",
                                           delivery_status="bot_" + result["status"])
        except Exception:
            try:
                access.finish_confirmation(key, "uncertain")
                self.thread_store.finish_reply(event_key, state_value="uncertain",
                                               delivery_status="bot_confirmation_uncertain")
            except Exception:
                pass
            return SlackReplyResult("bot_control_uncertain", mapping.target.workspace_id, key)
        return SlackReplyResult("bot_access_applied", mapping.target.workspace_id, key,
                                delivery_status=result["status"])

    def _handle_bot_instruction(self, event, event_id, team_id, app_id, from_poll):
        from .slack_bot_identity import BotVerificationDeferred, event_principal, parse_bot_instruction
        from .slack_bot_acl import BotRequestReused
        from .slack_bot_receipts import BotReceiptContext
        command = parse_bot_instruction(event)
        if command is None:
            return SlackReplyResult("ignored_bot_or_subtype", delivery_status="bot_not_instruction")
        channel, parent, timestamp = event.get("channel"), event.get("thread_ts"), event.get("ts")
        if (not valid_slack_channel_id(channel) or not valid_slack_timestamp(parent)
                or not valid_slack_timestamp(timestamp) or timestamp == parent):
            return SlackReplyResult("bot_ignored", delivery_status="bot_unmapped")
        request_id, prompt = command
        try:
            mapping = self.thread_store.lookup_thread(channel, parent)
            if mapping is None:
                return SlackReplyResult("bot_ignored", delivery_status="bot_unmapped")
            context = self._bot_context(team_id, app_id, from_poll)
            principal = self._get_bot_verifier().bot(event.get("user"))
            if event_principal(event, context.team_id, principal) is not True:
                return SlackReplyResult("bot_ignored", delivery_status="bot_identity_mismatch")
            event_key = self._event_key(event, event_id)
            message_key = channel + "\0" + timestamp
            fingerprint = sha256_text(json.dumps(dict(
                kind="slack_bot_instruction", principal=principal.to_dict(),
                receiving=context.to_dict(), request_id=request_id,
                channel=channel, parent=parent, timestamp=timestamp,
                target=mapping.target.to_dict(), text_sha256=sha256_text(event["text"])),
                sort_keys=True, separators=(",", ":")))
            source_key = self.thread_store.thread_key(channel, parent)
            receipt_context = BotReceiptContext(source_key, principal, mapping.target,
                request_id, event_key, message_key, fingerprint)
            admission = self._get_bot_access().make_admission(source_key, principal, mapping.target,
                request_id=request_id, event_key=event_key, message_key=message_key,
                payload_sha256=fingerprint)
            instruction_id = "slackbot:" + sha256_text(event_key)[:40]
            claimed, previous = self.thread_store.claim_reply(
                event_key=event_key, channel_id=channel, thread_ts=parent,
                instruction_id=instruction_id, text=prompt, admission=admission,
                message_key=message_key, payload_sha256=fingerprint)
        except (StoreBusyError, BotVerificationDeferred):
            return SlackReplyResult("deferred")
        except BotRequestReused:
            return SlackReplyResult("bot_duplicate", duplicate=True,
                delivery_status="request_reserved", bot_receipt=(receipt_context, "duplicate"))
        except Exception as exc:
            return SlackReplyResult("bot_rejected", error_sha256=self._error_digest(exc))
        if not claimed:
            if previous == "unauthorized":
                return SlackReplyResult("bot_ignored", delivery_status=previous)
            # Original event/physical retries have no captured current owner
            # capability. They do not start a new primary receipt after a crash.
            # A new message on a closed ticket can receive bounded metadata-only
            # rejection/duplicate feedback under its independently rechecked grant.
            return SlackReplyResult("bot_duplicate", delivery_status=previous, duplicate=True,
                bot_receipt=(receipt_context, "rejected") if previous == "ticket_closed" else None)
        result, legacy_dispatch = None, False
        try:
            result = self._controlled_reply(mapping, event_key, instruction_id, prompt,
                                             check_legacy_receipt=False)
            if result is None:
                legacy_dispatch = True
                delivery = self._dispatch(mapping, instruction_id, prompt)
                result = SlackReplyResult("queued" if delivery in _DELIVERED_STATES else "uncertain",
                                          mapping.target.workspace_id, instruction_id, delivery)
            delivered = result.delivery_status in _DELIVERED_STATES
            self.thread_store.finish_reply(event_key,
                state_value="delivered" if delivered else "uncertain",
                delivery_status=result.delivery_status or result.status)
            return replace(result, status="bot_queued" if delivered else "bot_uncertain",
                           bot_receipt=(receipt_context, "queued" if delivered else "uncertain")
                           if legacy_dispatch or result.control is not None else None)
        except Exception as exc:
            try:
                self.thread_store.finish_reply(event_key, state_value="uncertain",
                                               delivery_status="exception")
            except Exception:
                pass
            return SlackReplyResult("bot_uncertain", mapping.target.workspace_id, instruction_id,
                                    error_sha256=self._error_digest(exc),
                                    control=result.control if result is not None else None,
                                    bot_receipt=(receipt_context, "uncertain")
                                    if legacy_dispatch or (result is not None and result.control is not None) else None)

    def _get_session_access(self) -> Any:
        from .session_access import SessionAccess
        return SessionAccess(
            self.thread_store, "slack", self.channel_id,
            admin_ids=tuple(self.allowed_user_ids),
            acl=self._session_acl,
        )

    def _get_access_api(self) -> Any:
        if self._access_api is not None:
            return self._access_api
        from .session_access_commands import slack_user_get
        token = self.bot_token
        def _api(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            return slack_user_get(token, method, params)
        return _api

    def _get_access_sender(self) -> Any:
        if self._access_sender is not None:
            return self._access_sender
        token = self.bot_token
        def _sender(text: str, operation_id: str, destination: str) -> Dict[str, Any]:
            try:
                from slack_sdk import WebClient
                client = WebClient(token=token, retry_handlers=[])
                resp = client.chat_postMessage(
                    channel=destination, text=text,
                    unfurl_links=False, unfurl_media=False,
                )
                resp_dict = resp.data if hasattr(resp, "data") else (resp if isinstance(resp, dict) else {})
                if (resp_dict.get("ok") is not True
                        or resp_dict.get("channel") != destination
                        or not valid_slack_timestamp(resp_dict.get("ts"))):
                    raise ValueError("access_sender_bad_response")
                return {"chat_id": resp_dict["channel"], "message_id": resp_dict["ts"]}
            except ImportError:
                raise RuntimeError("slack_sdk required for access sender")
        return _sender

    def _handle_access_command(
        self,
        event: Any,
        stable_event_id: str,
        mapping: Any,
        channel_id: str,
        thread_ts: str,
        user_id: str,
        text: str,
        operation: str,
        raw_delegate_id: Optional[str],
    ) -> SlackReplyResult:
        """Handle admin add/remove/access with one-shot confirmation mapping."""
        message_ts = event.get("ts", "")
        source_key = SlackThreadStore.thread_key(channel_id, thread_ts)
        message_key = channel_id + "\0" + message_ts

        # Preflight: atomically verify source ticket is active and target unchanged.
        access = self._get_session_access()
        try:
            current_delegates = access.preflight(source_key, user_id, mapping.target)
            if current_delegates is None:
                return SlackReplyResult("ignored_closed_ticket",
                                       workspace_id=mapping.target.workspace_id)
        except StoreBusyError:
            return SlackReplyResult("deferred")
        except Exception as exc:
            return SlackReplyResult("rejected_state_or_collision",
                                   error_sha256=self._error_digest(exc))

        # Verify delegate identity for add/remove; skip for known admins and existing delegates.
        delegate_id = raw_delegate_id
        if operation in ("add", "remove") and raw_delegate_id is not None:
            if raw_delegate_id in self.allowed_user_ids:
                pass  # Admin target: apply() will return already_admin
            else:
                skip_verify = operation == "remove" and raw_delegate_id in current_delegates
                if not skip_verify:
                    try:
                        from .session_access_commands import verify_slack_user
                        delegate_id = verify_slack_user(raw_delegate_id, self._get_access_api())
                    except ValueError as exc:
                        return SlackReplyResult("rejected_route_command",
                                               workspace_id=mapping.target.workspace_id,
                                               delivery_status=str(exc))
                    except Exception as exc:
                        from urllib.error import HTTPError
                        if isinstance(exc, HTTPError) and exc.code == 429:
                            raise
                        return SlackReplyResult("rejected_route_command",
                                               workspace_id=mapping.target.workspace_id)

        # Build deterministic payload fingerprint including command text hash.
        identity: Dict[str, Any] = {
            "provider": "slack", "channel": channel_id, "thread_ts": thread_ts,
            "message_ts": message_ts, "user": user_id,
            "operation": operation, "delegate": delegate_id,
            "target": mapping.target.to_dict(),
            "text_sha256": sha256_text(text),
        }
        payload_sha256 = sha256_text(
            json.dumps(identity, sort_keys=True, separators=(",", ":")))
        try:
            result = access.apply(
                event_key=stable_event_id, message_key=message_key,
                source_key=source_key, user_id=user_id,
                operation=operation, delegate_id=delegate_id,
                payload_sha256=payload_sha256, expected_target=mapping.target,
            )
        except StoreBusyError:
            return SlackReplyResult("deferred")
        except ValueError as exc:
            return SlackReplyResult("rejected_state_or_collision",
                                   error_sha256=self._error_digest(exc))

        operation_key = result["operation_key"]
        status = result["status"]

        if status == "duplicate":
            return SlackReplyResult("duplicate", mapping.target.workspace_id,
                                   operation_key, duplicate=True)

        # Once-only claim before any external effect.
        try:
            claim = access.claim_confirmation(operation_key)
        except Exception:
            return SlackReplyResult("uncertain", mapping.target.workspace_id, operation_key)

        if claim is None:
            return SlackReplyResult("duplicate", mapping.target.workspace_id,
                                   operation_key, duplicate=True)

        target = claim["target"]
        fingerprint = claim["fingerprint"]
        delegates = claim.get("delegates", [])

        from .session_access import access_confirmation_text_slack
        text_out = slack_message_with_host(
            access_confirmation_text_slack(operation, status, delegate_id, delegates, target))

        # Send fresh top-level message (not a reply) and record the new mapping.
        try:
            sender = self._get_access_sender()
            sent = sender(text_out, operation_key, channel_id)
            new_channel = sent.get("chat_id")
            new_ts = sent.get("message_id")
            if (not valid_slack_channel_id(new_channel)
                    or new_channel != channel_id
                    or not valid_slack_timestamp(new_ts)):
                raise ValueError("access_confirmation_address_invalid")
            self.thread_store.record_thread(new_channel, new_ts, target, fingerprint)
            access.finish_confirmation(operation_key, "sent")
        except Exception:
            try:
                access.finish_confirmation(operation_key, "uncertain")
            except Exception:
                pass
            return SlackReplyResult("uncertain", mapping.target.workspace_id, operation_key,
                                   delivery_status="access_confirmation_uncertain")

        return SlackReplyResult(
            "access_applied", mapping.target.workspace_id, operation_key,
            delivery_status=status,
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
        previous: Optional[dict] = None,
    ) -> "SlackReplyResult":
        from .session_routes import SessionRoutes
        from .slack_route_commands import resolve_destination
        operation, argument = route_cmd
        routes = SessionRoutes(self.thread_store, provider="slack", scope=self.channel_id)

        destination: Optional[str] = None
        try:
            if previous is not None:
                destination = previous["destination"]
            elif operation == "bind":
                destination = resolve_destination(argument, self._get_route_api())
            result = routes.apply(
                event_key, channel_id, thread_ts, user_id, text, destination,
                command_ts=event.get("ts"),
            )
        except StoreBusyError:
            return SlackReplyResult("deferred")
        except Exception as exc:
            from urllib.error import HTTPError
            if previous is not None:
                return SlackReplyResult("rejected_state_or_collision", error_sha256=self._error_digest(exc))
            if isinstance(exc, ValueError) and str(exc) == "route_source_closed":
                return SlackReplyResult("ignored_closed_ticket", workspace_id=mapping.target.workspace_id)
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
            if result.delivery_status == "access_verify_missing_scope":
                return ("WatchDog needs the Slack users:read bot scope to verify a new delegate. "
                        "Authorize that scope for the existing app, then retry add @person.")
            if (isinstance(result.delivery_status, str)
                    and result.delivery_status.startswith("access_")):
                return ("WatchDog could not change session access. Reply with add @person, "
                        "remove @person or access, using one native user mention.")
            if result.delivery_status == "route_destination_missing_scope":
                return (
                    "WatchDog cannot verify this channel: the bot is missing a required Slack scope. "
                    "Add channels:read (public) or groups:read (private) under Bot Token Scopes "
                    "and reinstall the existing Slack app to authorize, then retry."
                )
            return ("WatchDog could not change this session's destination. "
                    "Use bind #channel or unbind, and check that the bot can access the channel.")
        return None
