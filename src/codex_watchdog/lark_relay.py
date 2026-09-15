"""Allowlisted native replies to exact, previously recorded bot messages."""
import json
from pathlib import Path
import re
from types import SimpleNamespace

from .lark_mapping import LarkThreadStore
from .lark_transport import LarkApi, LarkConnection, valid_id
from .models import MAX_PROMPT_CHARS, sha256_text
from .relay import ExactThreadRelay, ReplyResult, _DELIVERED_STATES
from .storage import FileLock, StoreBusyError


class LarkReplyRelay(ExactThreadRelay):
    reply_source = "lark_reply"

    def __init__(self, runtime, config, *, queue_dispatcher, remote_ssh_adapter,
                 timeout=10.0, thread_store=None, api=None, connection_factory=LarkConnection):
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
            self.acknowledge(payload["event"]["message"].get("message_id"), result)

    def acknowledge(self, message_id, result):
        if result.status != "queued":
            return
        def send_ack(_event):
            api = self.api or LarkApi(self.config, self.timeout)
            api.send("Queued for the exact existing Codex thread.", "ack:" + result.instruction_id,
                     reply_to=message_id)
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
        if (sender.get("sender_type") != "user" or not isinstance(sender_ids, dict)
                or sender_ids.get("open_id") not in self.config.allowed_user_ids):
            return ReplyResult("ignored_unauthorized")
        chat_id, message_id = message.get("chat_id"), message.get("message_id")
        if chat_id != self.config.chat_id:
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
        addresses = [message.get(key) for key in ("root_id", "parent_id") if message.get(key)]
        if (not addresses or any(not valid_id(address, "om") for address in addresses)
                or message_id in addresses):
            return ReplyResult("ignored_not_thread_reply")
        try:
            mappings = [self.thread_store.lookup_thread(chat_id, address) for address in set(addresses)]
            mappings = [mapping for mapping in mappings if mapping is not None]
            if not mappings:
                return ReplyResult("ignored_unknown_thread")
            if len({mapping.target for mapping in mappings}) != 1:
                return ReplyResult("ignored_ambiguous_thread")
            mapping = mappings[0]
            identity = {"app_id": header["app_id"], "chat_id": chat_id, "message_id": message_id,
                        "root_id": message.get("root_id"), "parent_id": message.get("parent_id"),
                        "sender": sender_ids["open_id"], "text_sha256": sha256_text(text),
                        "thread_id": mapping.target.thread_id}
            fingerprint = sha256_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))
            event_key = "lark:" + self.config.scope + ":" + event_id
            message_key = chat_id + "\0" + message_id
            instruction_id = "lark:" + sha256_text(self.config.scope + "\0" + message_key)[:40]
            claimed, previous = self.thread_store.claim_reply(
                event_key=event_key, message_key=message_key, payload_sha256=fingerprint,
                instruction_id=instruction_id, text=text)
            if not claimed:
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

    @staticmethod
    def _error_digest(error):
        return sha256_text("lark_reply_failed\0" + type(error).__module__ + "." + type(error).__qualname__)
