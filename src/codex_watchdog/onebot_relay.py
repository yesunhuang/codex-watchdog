"""OneBot-specific validation using the existing routing and admission ledgers."""
from pathlib import Path
import json

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
    """Only array-form OneBot human text; never interpret CQ markup as commands."""
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
    text, replies = [], []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            return None
        data = segment["data"]
        if segment.get("type") == "text" and isinstance(data.get("text"), str):
            text.append(data["text"])
        elif segment.get("type") == "reply" and identifier(data.get("id"), message=True) is not None:
            replies.append(identifier(data["id"], message=True))
        elif segment.get("type") == "at" and identifier(data.get("qq")) == self_id:
            pass
        else:
            return None
    text = "".join(text)
    if not text.strip() or len(text) > MAX_PROMPT_CHARS or len(replies) > 1:
        return None
    return dict(chat_id=kind + ":" + destination, message_id=message_id, user_id=user,
                text=text, reply_to=replies[0] if replies else None)


class OneBotReplyRelay(ExactThreadRelay):
    reply_source = "onebot_reply"

    def __init__(self, runtime, config, *, queue_dispatcher, remote_ssh_adapter,
                 timeout=10.0, thread_store=None, connection_factory=OneBotConnection):
        if not config.relay_configured:
            raise OneBotError("onebot_relay_configuration_incomplete")
        self.runtime, self.config = Path(runtime), config
        self.queue_dispatcher, self.remote_ssh_adapter = queue_dispatcher, remote_ssh_adapter
        self.thread_store = thread_store or OneBotThreadStore(runtime, config.scope)
        self.timeout, self.connection_factory = timeout, connection_factory
        self._connection = self._listener_lock = None

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

    def handle_event(self, payload):
        # Called only from the authenticated connection to the selected bot.
        value = human_message(payload, self.config.self_id)
        if value is None:
            return ReplyResult("ignored_event_type")
        if value["user_id"] not in self.config.allowed_user_ids:
            return ReplyResult("ignored_unauthorized")
        if value["chat_id"] != self.config.destination:
            return ReplyResult("ignored_chat")
        if value["reply_to"] is None or value["reply_to"] == value["message_id"]:
            return ReplyResult("ignored_not_thread_reply")
        try:
            mapping = self.thread_store.lookup_thread(value["chat_id"], value["reply_to"])
            if mapping is None:
                return ReplyResult("ignored_unknown_thread")
            identity = {key: value[key] for key in ("chat_id", "message_id", "reply_to", "user_id")}
            identity.update(text_sha256=sha256_text(value["text"]), target=mapping.target.to_dict())
            fingerprint = sha256_text(json.dumps(identity, sort_keys=True, separators=(",", ":")))
            message_key = value["chat_id"] + "\0" + value["message_id"]
            instruction_id = "onebot:" + sha256_text(self.config.scope + "\0" + message_key)[:40]
            claimed, previous = self.thread_store.claim_reply(event_key=instruction_id,
                message_key=message_key, payload_sha256=fingerprint,
                instruction_id=instruction_id, text=value["text"],
                chat_id=value["chat_id"], parent_id=value["reply_to"])
            if not claimed:
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
