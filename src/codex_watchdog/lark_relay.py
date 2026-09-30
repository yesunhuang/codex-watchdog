"""Allowlisted native replies to exact, previously recorded bot messages."""
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

from .lark_mapping import LarkThreadStore
from .lark_transport import LarkApi, LarkConnection, LarkTransportError, valid_id
from .models import MAX_PROMPT_CHARS, sha256_text
from .relay import ExactThreadRelay, ReplyResult, _DELIVERED_STATES
from .storage import FileLock, StoreBusyError


class LarkReplyRelay(ExactThreadRelay):
    reply_source = "lark_reply"

    def __init__(self, runtime, config, *, queue_dispatcher, remote_ssh_adapter,
                 timeout=10.0, thread_store=None, api=None, connection_factory=LarkConnection,
                 clock=time.time, session_acl=None):
        if not config.relay_configured:
            raise ValueError("Lark relay requires a configured bot, chat and user allowlist")
        self.runtime = Path(runtime)
        self.config = config
        self.queue_dispatcher = queue_dispatcher
        self.remote_ssh_adapter = remote_ssh_adapter
        self.thread_store = thread_store or LarkThreadStore(runtime, config.scope)
        self.timeout = timeout
        self.api = api
        self.connection_factory = connection_factory
        self._connection = None
        self._listener_lock = None
        self._poller = None
        self._session_acl = session_acl
        from .destination_binding import DestinationBinding
        self.binding = DestinationBinding(self, "lark", self._send_control, clock=clock)

    def _send_control(self, text, operation_id, destination, *, reply_to=None):
        api = self.api or LarkApi(self.config, self.timeout)
        return api.send(text, operation_id, reply_to=reply_to, destination=destination)

    def start(self):
        if self._connection is not None or self._poller is not None:
            return
        lock = FileLock(self.runtime / "locks" / ("lark-listener-" + self.config.scope + ".lock"))
        lock.__enter__()
        self._listener_lock = lock
        try:
            if self.config.reply_mode == "poll":
                from .lark_poll import LarkReplyPoller
                self._poller = LarkReplyPoller(self)
                self._poller.start()
                return
            self._connection = self.connection_factory(self.config, self._received, self.timeout)
            self._connection.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._poller is not None:
            self._poller.close()
            self._poller = None
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._listener_lock is not None:
            self._listener_lock.__exit__(None, None, None)
            self._listener_lock = None

    def _received(self, payload):
        # This entry point is registered only on the SDK's authenticated socket.
        result = self.handle_event(payload)
        if result.status == "queued":
            message = payload["event"]["message"]
            self.acknowledge(message.get("message_id"), result, message.get("chat_id"))

    def acknowledge(self, message_id, result, chat_id=None):
        if result.status != "queued":
            return
        def send_ack(_event):
            api = self.api or LarkApi(self.config, self.timeout)
            # Replies can now originate in a mapped bound chat. The original
            # source message is the immutable reply address; carry its chat too.
            destination = chat_id
            kwargs = {} if destination is None or destination == self.config.chat_id else {"destination": destination}
            api.send("Queued for the exact existing Codex thread.", "ack:" + result.instruction_id,
                     reply_to=message_id, **kwargs)
            return SimpleNamespace(to_dict=lambda: {"status": "sent", "channel": "lark"})
        try:
            if result.control is None:
                send_ack(None)
            else:
                from .notifications import NotificationEvent
                control, target, token = result.control
                event = NotificationEvent(result.workspace_id, "lark_reply_ack", result.instruction_id,
                                          "Reply queued", "Queued for the exact existing Codex thread.")
                control.notify(target, token, event, SimpleNamespace(notify=send_ack))
        except Exception:
            # Acknowledgement uncertainty never causes a second queue admission.
            return

    def _get_session_access(self):
        from .session_access import SessionAccess
        return SessionAccess(
            self.thread_store, "lark", self.config.scope,
            admin_ids=tuple(self.config.allowed_user_ids),
            acl=self._session_acl,
        )

    def handle_event(self, payload):
        if not isinstance(payload, dict) or payload.get("schema") != "2.0":
            return ReplyResult("ignored_event_type")
        header, event = payload.get("header"), payload.get("event")
        if not isinstance(header, dict) or not isinstance(event, dict):
            return ReplyResult("ignored_event_type")
        if header.get("event_type") != "im.message.receive_v1" or header.get("app_id") != self.config.app_id:
            return ReplyResult("ignored_app_or_event")
        event_id = header.get("event_id")
        if not isinstance(event_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", event_id) is None:
            return ReplyResult("ignored_event_identity")
        sender, message = event.get("sender"), event.get("message")
        if not isinstance(sender, dict) or not isinstance(message, dict):
            return ReplyResult("ignored_event_type")
        sender_ids = sender.get("sender_id")
        open_id = sender_ids.get("open_id") if isinstance(sender_ids, dict) else None
        if sender.get("sender_type") != "user" or not valid_id(open_id, "ou"):
            return ReplyResult("ignored_unauthorized")
        is_admin = open_id in self.config.allowed_user_ids
        chat_id, message_id = message.get("chat_id"), message.get("message_id")
        if not valid_id(chat_id, "oc"):
            return ReplyResult("ignored_chat")
        if not valid_id(message_id, "om"):
            return ReplyResult("ignored_message_identity")
        if message.get("message_type") != "text":
            return ReplyResult("ignored_message_type")
        try:
            body = json.loads(message.get("content"))
        except (ValueError, TypeError):
            return ReplyResult("ignored_text_format")
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_PROMPT_CHARS:
            return ReplyResult("ignored_empty_or_oversize")

        from .destination_binding import binding_control
        operation = binding_control(text)
        if operation is not None:
            if not is_admin:
                return ReplyResult("ignored_unauthorized")
            if message.get("edited") or message.get("deleted"):
                return ReplyResult("rejected_route_command")
            try:
                raw_created = message.get("create_time", header.get("create_time"))
                if not isinstance(raw_created, (str, int)) or isinstance(raw_created, bool):
                    raise ValueError()
                created_at = int(raw_created) / 1000
            except (ValueError, TypeError):
                return ReplyResult("rejected_route_command")
            if operation == "challenge":
                if message.get("root_id") or message.get("parent_id") or message.get("mentions"):
                    return ReplyResult("rejected_route_challenge")
                return self.binding.confirm(text, open_id, chat_id,
                                            message_id=message_id, created_at=created_at)

        # Check for access control commands (add/remove/access) via native mentions.
        mentions = message.get("mentions")
        from .session_access_commands import parse_lark_access, reserved_control
        parsed_access = None
        is_reserved = reserved_control(text)
        if is_reserved:
            if not is_admin:
                return ReplyResult("ignored_unauthorized")
            # Only try access command parse for add/remove/access (not bind/unbind already caught)
            if text.strip().split(None, 1)[0].lower() not in ("bind", "unbind"):
                if message.get("edited") or message.get("deleted"):
                    return ReplyResult("rejected_route_command")
                try:
                    parsed = parse_lark_access(text, mentions)
                except ValueError as exc:
                    return ReplyResult("rejected_route_command", delivery_status=str(exc))
                if parsed is None:
                    return ReplyResult("rejected_route_command")
                parsed_access = parsed

        addresses = [message.get(key) for key in ("root_id", "parent_id") if message.get(key)]
        if (not addresses or any(not valid_id(address, "om") for address in addresses)
                or message_id in addresses):
            return ReplyResult("ignored_not_thread_reply")
        try:
            mappings = [self.thread_store.lookup_thread(chat_id, address) for address in set(addresses)]
            mappings = [mapping for mapping in mappings if mapping is not None]
            if not mappings:
                if not is_admin:
                    return ReplyResult("ignored_unauthorized")
                return ReplyResult("ignored_unknown_thread" if chat_id == self.config.chat_id else "ignored_chat")
            if len({mapping.target for mapping in mappings}) != 1:
                return ReplyResult("ignored_ambiguous_thread")
            mapping = mappings[0]
            identity = {"app_id": header["app_id"], "chat_id": chat_id, "message_id": message_id,
                        "root_id": message.get("root_id"), "parent_id": message.get("parent_id"),
                        "sender": open_id, "text_sha256": sha256_text(text),
                        "thread_id": mapping.target.thread_id}
            fingerprint = sha256_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))
            event_key = "lark:" + self.config.scope + ":" + event_id
            if parsed_access is not None:
                return self._handle_access_command(header, message, event_key, mapping,
                    chat_id, open_id, text, *parsed_access)
            if operation is not None:
                return self.binding.command(operation, event_key=event_key, chat_id=chat_id,
                    parent_id=mapping.message_id, user_id=open_id, text=text,
                    message_id=message_id, created_at=created_at)
            message_key = chat_id + "\0" + message_id
            instruction_id = "lark:" + sha256_text(self.config.scope + "\0" + message_key)[:40]
            # Always pass admission callback; it atomically verifies target and ticket for both
            # admins and delegates.
            access = self._get_session_access()
            source_key = LarkThreadStore._address(chat_id, mapping.message_id)
            admission = access.make_admission(source_key, open_id, mapping.target,
                                             event_key=event_key, message_key=message_key)
            claimed, previous = self.thread_store.claim_reply(
                event_key=event_key, message_key=message_key, payload_sha256=fingerprint,
                instruction_id=instruction_id, text=text,
                chat_id=chat_id, parent_id=mapping.message_id, admission=admission)
            if not claimed:
                if previous == "unauthorized":
                    return ReplyResult("ignored_unauthorized")
                return ReplyResult("duplicate", mapping.target.workspace_id, instruction_id, previous, duplicate=True)
        except StoreBusyError:
            # No admission occurred. A history page may safely retry later.
            return ReplyResult("deferred")
        except Exception as exc:
            return ReplyResult("rejected_state_or_collision", error_sha256=self._error_digest(exc))
        try:
            # Lark has already durably bound both provider identities to this payload.
            result = self._controlled_reply(mapping, event_key, instruction_id, text, check_legacy_receipt=False)
            if result is None:
                delivery = self._dispatch(mapping, instruction_id, text)
                result = ReplyResult("queued" if delivery in _DELIVERED_STATES else "uncertain",
                                     mapping.target.workspace_id, instruction_id, delivery)
        except Exception as exc:
            result = ReplyResult("uncertain", mapping.target.workspace_id, instruction_id,
                                 "exception", error_sha256=self._error_digest(exc))
        try:
            self.thread_store.finish_reply(
                event_key, state_value="delivered" if result.delivery_status in _DELIVERED_STATES else "uncertain",
                delivery_status=result.delivery_status or result.status, error_sha256=result.error_sha256)
        except Exception as exc:
            return ReplyResult("uncertain", mapping.target.workspace_id, instruction_id,
                               result.delivery_status, error_sha256=self._error_digest(exc))
        return result

    def _handle_access_command(self, header, message, event_key, mapping, chat_id,
                                open_id, text, operation, raw_delegate_id):
        """Handle admin add/remove/access with once-only confirmation mapping."""
        message_id = message.get("message_id", "")
        # Validate the message is a thread reply (root/parent must exist and be valid).
        parent_ids = [message.get(k) for k in ("root_id", "parent_id") if message.get(k)]
        if not parent_ids or any(not valid_id(p, "om") for p in parent_ids):
            return ReplyResult("ignored_not_thread_reply")
        # Derive source_key from the canonical recorded mapping, not untrusted event parent.
        source_key = LarkThreadStore._address(chat_id, mapping.message_id)
        message_key = chat_id + "\0" + message_id

        # Preflight: atomically verify source ticket is active and target unchanged.
        access = self._get_session_access()
        try:
            current_delegates = access.preflight(source_key, open_id, mapping.target)
            if current_delegates is None:
                return ReplyResult("ignored_closed_ticket",
                                   workspace_id=mapping.target.workspace_id)
        except StoreBusyError:
            return ReplyResult("deferred")
        except Exception as exc:
            return ReplyResult("rejected_state_or_collision",
                               error_sha256=self._error_digest(exc))

        # Verify delegate identity for add/remove; skip for known admins and existing delegates.
        delegate_id = raw_delegate_id
        if operation in ("add", "remove") and raw_delegate_id is not None:
            if raw_delegate_id in self.config.allowed_user_ids:
                pass  # Admin target: apply() will return already_admin
            else:
                skip_verify = operation == "remove" and raw_delegate_id in current_delegates
                if not skip_verify:
                    try:
                        api_obj = self.api or LarkApi(self.config, self.timeout)
                        delegate_id = api_obj.verify_user(raw_delegate_id)
                        if delegate_id != raw_delegate_id:
                            raise LarkTransportError("lark_user_id_mismatch")
                    except Exception as exc:
                        code = str(exc) if isinstance(exc, LarkTransportError) else ""
                        safe_codes = {"lark_user_verify_failed_check_permissions", "lark_user_rate_limited",
                                      "lark_user_inactive", "lark_user_id_mismatch", "lark_user_verify_malformed"}
                        return ReplyResult("rejected_route_command",
                                           workspace_id=mapping.target.workspace_id,
                                           delivery_status=code if code in safe_codes else "lark_user_verify_failed")

        mentions = message.get("mentions")
        mentions_sha256 = sha256_text(json.dumps(mentions, sort_keys=True, separators=(",", ":"))
                                      if isinstance(mentions, list) else "")
        identity = {
            "provider": "lark", "scope": self.config.scope,
            "chat": chat_id, "parent": mapping.message_id,
            "message": message_id, "sender": open_id,
            "operation": operation, "delegate": delegate_id,
            "target": mapping.target.to_dict(),
            "text_sha256": sha256_text(text),
            "mentions_sha256": mentions_sha256,
        }
        payload_sha256 = sha256_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))

        try:
            result = access.apply(
                event_key=event_key, message_key=message_key,
                source_key=source_key, user_id=open_id,
                operation=operation, delegate_id=delegate_id,
                payload_sha256=payload_sha256, expected_target=mapping.target,
            )
        except StoreBusyError:
            return ReplyResult("deferred")
        except ValueError as exc:
            return ReplyResult("rejected_state_or_collision",
                               error_sha256=self._error_digest(exc))

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

        from .session_access import access_confirmation_text_lark
        from .destination_binding import DestinationBinding
        text_out = DestinationBinding._host(
            access_confirmation_text_lark(operation, status, delegate_id, delegates, target))

        # Send fresh top-level message via _send_control (no reply_to).
        try:
            previous = self.thread_store.prepare_notification(fingerprint, payload_sha256_notif)
            if previous is not None:
                # Previously sent confirmation – treat as duplicate.
                access.finish_confirmation(operation_key, "sent")
                return ReplyResult("duplicate", mapping.target.workspace_id,
                                  operation_key, duplicate=True)
            sent = self._send_control(text_out, operation_key, chat_id)
            new_chat = sent.get("chat_id")
            new_message_id = sent.get("message_id")
            if not valid_id(new_chat, "oc") or new_chat != chat_id or not valid_id(new_message_id, "om"):
                raise ValueError("access_confirmation_address_invalid")
            self.thread_store.finish_notification(fingerprint, new_chat, new_message_id, target=target)
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

    @staticmethod
    def _error_digest(error):
        return sha256_text("lark_reply_failed\0" + type(error).__module__ + "." + type(error).__qualname__)
