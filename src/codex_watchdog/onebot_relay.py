"""OneBot-specific validation using the existing routing and admission ledgers."""
from pathlib import Path
import json
import time

from .lark_mapping import LarkThreadStore
from .models import MAX_PROMPT_CHARS, sha256_text
from .onebot_transport import OneBotConnection, OneBotError, identifier
from .relay import ExactThreadRelay, ReplyResult, _DELIVERED_STATES
from .storage import FileLock, StoreBusyError


class OneBotThreadStore(LarkThreadStore):
    """Reuse the tested journal in a separate provider namespace and address space.

The shared journal enforces the same four-ticket one-shot bound.
"""
    provider = "onebot"

    def __init__(self, runtime, scope):
        super().__init__(runtime, scope)
        self.path = Path(runtime) / "onebot" / scope / "relay-state.json"
        self.lock_path = Path(runtime) / "locks" / ("onebot-relay-" + scope + ".lock")

    @staticmethod
    def _address(chat_id, message_id):
        parts = chat_id.split(":") if isinstance(chat_id, str) else []
        if (len(parts) != 2 or parts[0] not in ("group", "private")
                or identifier(parts[1]) is None or not isinstance(message_id, str)
                or identifier(message_id, message=True) is None):
            raise OneBotError("onebot_message_address_invalid")
        return sha256_text(chat_id + "\0" + message_id)



def human_message(payload, self_id):
    """Array-form OneBot human text; non-self AT segments trigger access-command parse."""
    if (not isinstance(payload, dict) or payload.get("post_type") != "message"
            or identifier(payload.get("self_id")) != self_id
            or payload.get("anonymous") is not None):
        return None
    kind, user = payload.get("message_type"), identifier(payload.get("user_id"))
    sender = payload.get("sender")
    if (kind not in ("private", "group") or user is None or user == self_id
            or not isinstance(sender, dict) or identifier(sender.get("user_id")) != user):
        return None
    destination = identifier(payload.get("group_id")) if kind == "group" else user
    message_id = identifier(payload.get("message_id"), message=True)
    segments = payload.get("message")
    if (destination is None or message_id is None or not isinstance(segments, list)
            or not 1 <= len(segments) <= 256):
        return None
    text_parts, replies = [], []
    has_nonself_at = False
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            return None
        data = segment["data"]
        if segment.get("type") == "text" and isinstance(data.get("text"), str):
            text_parts.append(data["text"])
        elif segment.get("type") == "reply" and identifier(data.get("id"), message=True) is not None:
            replies.append(identifier(data["id"], message=True))
        elif segment.get("type") == "at" and identifier(data.get("qq")) == self_id:
            pass  # bot-self envelope mention
        elif segment.get("type") == "at":
            has_nonself_at = True
        else:
            return None

    # Ordinary path: text + optional reply + optional bot-self at, no other at.
    if not has_nonself_at:
        text = "".join(text_parts)
        if not text.strip() or len(text) > MAX_PROMPT_CHARS or len(replies) > 1:
            return None
        # Plain access op keyword (e.g. "access") requires no non-self AT.
        if text.strip().split()[0].lower() in ("add", "remove", "access"):
            from .session_access_commands import parse_onebot_access
            try:
                parsed = parse_onebot_access(segments, self_id)
            except ValueError:
                return None
            if parsed is not None:
                return dict(chat_id=kind + ":" + destination, message_id=message_id, user_id=user,
                            reply_to=replies[0] if replies else None, segments=segments,
                            access_cmd=parsed)
        return dict(chat_id=kind + ":" + destination, message_id=message_id, user_id=user,
                    text=text, reply_to=replies[0] if replies else None, segments=segments)

    # Non-self AT present: attempt to parse as access control command.
    from .session_access_commands import parse_onebot_access
    try:
        parsed = parse_onebot_access(segments, self_id)
    except ValueError:
        # Malformed reserved command – reject; never forward to Codex.
        return None
    if parsed is None:
        return None
    operation, delegate_id = parsed
    return dict(chat_id=kind + ":" + destination, message_id=message_id, user_id=user,
                reply_to=replies[0] if replies else None, segments=segments,
                access_cmd=(operation, delegate_id))


class OneBotReplyRelay(ExactThreadRelay):
    reply_source = "onebot_reply"

    def __init__(self, runtime, config, *, queue_dispatcher, remote_ssh_adapter,
                 timeout=10.0, thread_store=None, connection_factory=OneBotConnection,
                 api=None, clock=time.time, session_acl=None):
        if not config.relay_configured:
            raise OneBotError("onebot_relay_configuration_incomplete")
        self.runtime, self.config = Path(runtime), config
        self.queue_dispatcher, self.remote_ssh_adapter = queue_dispatcher, remote_ssh_adapter
        self.thread_store = thread_store or OneBotThreadStore(runtime, config.scope)
        self.timeout, self.connection_factory = timeout, connection_factory
        self._connection = self._listener_lock = None
        self.api = api
        self._session_acl = session_acl
        from .destination_binding import DestinationBinding
        self.binding = DestinationBinding(self, "onebot", self._send_control, clock=clock)

    def _send_control(self, text, operation_id, destination, *, reply_to=None):
        from .onebot_transport import OneBotApi
        api = self.api or OneBotApi(self.config, self.timeout)
        return api.send(text, operation_id, destination=destination, reply_to=reply_to)

    def start(self):
        if self._connection is not None:
            return
        lock = FileLock(self.runtime / "locks" / ("onebot-listener-" + self.config.scope + ".lock"))
        lock.__enter__()
        self._listener_lock = lock
        try:
            self._connection = self.connection_factory(self.config, self.handle_event, self.timeout,
                                                       runtime=self.runtime)
            self._connection.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._listener_lock is not None:
            self._listener_lock.__exit__(None, None, None)
            self._listener_lock = None

    def _get_session_access(self):
        from .session_access import SessionAccess
        return SessionAccess(
            self.thread_store, "onebot", self.config.scope,
            admin_ids=tuple(self.config.allowed_user_ids),
            acl=self._session_acl,
        )

    def handle_event(self, payload):
        # Called only from the authenticated connection to the selected bot.
        value = human_message(payload, self.config.self_id)
        if value is None:
            return ReplyResult("ignored_event_type")

        user_id = value["user_id"]
        is_admin = user_id in self.config.allowed_user_ids

        # Access control command path (has non-self AT or plain "access" keyword).
        access_cmd = value.get("access_cmd")
        if access_cmd is not None:
            if not is_admin:
                return ReplyResult("ignored_unauthorized")
            if payload.get("edited") or payload.get("deleted"):
                return ReplyResult("rejected_route_command")
            operation, raw_delegate_id = access_cmd
            reply_to = value.get("reply_to")
            if reply_to is None or reply_to == value["message_id"]:
                return ReplyResult("ignored_not_thread_reply")
            try:
                mapping = self.thread_store.lookup_thread(value["chat_id"], reply_to)
                if mapping is None:
                    return ReplyResult("ignored_unknown_thread"
                                      if value["chat_id"] == self.config.destination
                                      else "ignored_chat")
            except StoreBusyError:
                return ReplyResult("deferred")
            except Exception:
                return ReplyResult("rejected_state_or_collision")
            return self._handle_access_command(
                payload, value, mapping, operation, raw_delegate_id)

        from .destination_binding import binding_control
        operation = binding_control(value["text"])
        if operation is not None:
            if not is_admin:
                return ReplyResult("ignored_unauthorized")
            if payload.get("edited") or payload.get("deleted"):
                return ReplyResult("rejected_route_command")
            created_at = payload.get("time")
            if type(created_at) is not int or created_at < 0:
                return ReplyResult("rejected_route_command")
            if operation == "challenge":
                if value["reply_to"] is not None or any(s.get("type") != "text" for s in payload["message"]):
                    return ReplyResult("rejected_route_challenge")
                return self.binding.confirm(value["text"], value["user_id"], value["chat_id"],
                                            message_id=value["message_id"], created_at=created_at)

        # Check for reserved control text that wasn't already claimed by binding_control.
        text = value["text"]
        if operation is None:
            from .session_access_commands import reserved_control as _rc
            if _rc(text):
                if not is_admin:
                    return ReplyResult("ignored_unauthorized")
                # Admin used reserved keyword without proper AT structure -> reject.
                return ReplyResult("rejected_route_command")

        if value["reply_to"] is None or value["reply_to"] == value["message_id"]:
            return ReplyResult("ignored_not_thread_reply")
        try:
            mapping = self.thread_store.lookup_thread(value["chat_id"], value["reply_to"])
            if mapping is None:
                if not is_admin:
                    return ReplyResult("ignored_unauthorized")
                return ReplyResult("ignored_unknown_thread" if value["chat_id"] == self.config.destination else "ignored_chat")
            if operation is not None:
                return self.binding.command(operation,
                    event_key="onebot-control:" + value["chat_id"] + ":" + value["message_id"],
                    chat_id=value["chat_id"], parent_id=value["reply_to"], user_id=value["user_id"],
                    text=value["text"], message_id=value["message_id"], created_at=created_at)
            identity = {key: value[key] for key in ("chat_id", "message_id", "reply_to", "user_id")}
            identity.update(text_sha256=sha256_text(value["text"]), target=mapping.target.to_dict())
            fingerprint = sha256_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))
            message_key = value["chat_id"] + "\0" + value["message_id"]
            instruction_id = "onebot:" + sha256_text(self.config.scope + "\0" + message_key)[:40]
            # Always pass admission callback; it atomically verifies target and ticket for both
            # admins and delegates.
            access = self._get_session_access()
            source_key = OneBotThreadStore._address(value["chat_id"], value["reply_to"])
            admission = access.make_admission(source_key, user_id, mapping.target,
                                             event_key=instruction_id, message_key=message_key)
            claimed, previous = self.thread_store.claim_reply(
                event_key=instruction_id, message_key=message_key,
                payload_sha256=fingerprint, instruction_id=instruction_id,
                text=value["text"], chat_id=value["chat_id"],
                parent_id=value["reply_to"], admission=admission)
            if not claimed:
                if previous == "unauthorized":
                    return ReplyResult("ignored_unauthorized")
                return ReplyResult("duplicate", mapping.target.workspace_id, instruction_id,
                                   previous, duplicate=True)
        except StoreBusyError:
            return ReplyResult("deferred")
        except Exception:
            return ReplyResult("rejected_state_or_collision")
        try:
            result = self._controlled_reply(mapping, instruction_id, instruction_id, value["text"],
                                            check_legacy_receipt=False)
            if result is None:
                delivery = self._dispatch(mapping, instruction_id, value["text"])
                result = ReplyResult("queued" if delivery in _DELIVERED_STATES else "uncertain",
                                     mapping.target.workspace_id, instruction_id, delivery)
        except Exception:
            result = ReplyResult("uncertain", mapping.target.workspace_id, instruction_id, "exception")
        try:
            self.thread_store.finish_reply(instruction_id,
                state_value="delivered" if result.delivery_status in _DELIVERED_STATES else "uncertain",
                delivery_status=result.delivery_status or result.status)
        except Exception:
            return ReplyResult("uncertain", mapping.target.workspace_id, instruction_id,
                               result.delivery_status)
        return result

    def _handle_access_command(self, payload, value, mapping, operation, raw_delegate_id):
        """Handle admin add/remove/access with once-only confirmation mapping."""
        chat_id = value["chat_id"]
        message_id = value["message_id"]
        reply_to = value["reply_to"]
        user_id = value["user_id"]

        source_key = OneBotThreadStore._address(chat_id, reply_to)
        message_key = chat_id + "\0" + message_id
        event_key = "onebot-access:" + chat_id + ":" + message_id

        # Preflight: atomically verify source ticket is active and target unchanged.
        access = self._get_session_access()
        try:
            current_delegates = access.preflight(source_key, user_id, mapping.target)
            if current_delegates is None:
                return ReplyResult("ignored_closed_ticket",
                                   workspace_id=mapping.target.workspace_id)
        except StoreBusyError:
            return ReplyResult("deferred")
        except Exception:
            return ReplyResult("rejected_state_or_collision")

        # For OneBot, trust authenticated structured integer IDs directly.
        delegate_id = raw_delegate_id

        segments_sha256 = sha256_text(json.dumps(value["segments"], sort_keys=True, separators=(",", ":")))
        identity = {
            "provider": "onebot", "scope": self.config.scope,
            "chat": chat_id, "reply_to": reply_to,
            "message": message_id, "sender": user_id,
            "operation": operation, "delegate": delegate_id,
            "target": mapping.target.to_dict(),
            "segments_sha256": segments_sha256,
        }
        payload_sha256 = sha256_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))

        try:
            result = access.apply(
                event_key=event_key, message_key=message_key,
                source_key=source_key, user_id=user_id,
                operation=operation, delegate_id=delegate_id,
                payload_sha256=payload_sha256, expected_target=mapping.target,
            )
        except StoreBusyError:
            return ReplyResult("deferred")
        except ValueError:
            return ReplyResult("rejected_state_or_collision")

        operation_key = result["operation_key"]
        status = result["status"]

        if status == "duplicate":
            return ReplyResult("duplicate", mapping.target.workspace_id,
                              operation_key, duplicate=True)

        try:
            claim = access.claim_confirmation(operation_key)
        except Exception:
            return ReplyResult("uncertain", mapping.target.workspace_id, operation_key)

        if claim is None:
            return ReplyResult("duplicate", mapping.target.workspace_id,
                              operation_key, duplicate=True)

        target = claim["target"]
        fingerprint = claim["fingerprint"]
        payload_sha256_notif = claim.get("payload_sha256", payload_sha256)
        delegates = claim.get("delegates", [])

        from .session_access import access_confirmation_text_onebot
        from .destination_binding import DestinationBinding
        text_out = DestinationBinding._host(
            access_confirmation_text_onebot(operation, status, delegate_id, delegates, target))

        # Send fresh top-level message via _send_control (no reply_to).
        try:
            previous = self.thread_store.prepare_notification(fingerprint, payload_sha256_notif)
            if previous is not None:
                access.finish_confirmation(operation_key, "sent")
                return ReplyResult("duplicate", mapping.target.workspace_id,
                                  operation_key, duplicate=True)
            sent = self._send_control(text_out, operation_key, chat_id)
            new_chat = sent.get("chat_id")
            new_msg_id = sent.get("message_id")
            if (new_chat != chat_id or not isinstance(new_msg_id, str)
                    or identifier(new_msg_id, message=True) is None):
                raise ValueError("access_confirmation_address_invalid")
            self.thread_store.finish_notification(fingerprint, new_chat, new_msg_id, target=target)
            access.finish_confirmation(operation_key, "sent")
        except Exception:
            try:
                access.finish_confirmation(operation_key, "uncertain")
            except Exception:
                pass
            return ReplyResult("uncertain", mapping.target.workspace_id, operation_key,
                               "access_confirmation_uncertain")

        return ReplyResult(
            "access_applied", mapping.target.workspace_id, operation_key,
            delivery_status=status,
        )
