"""Provider-scoped immutable routing and hash-only admission receipts."""
from dataclasses import dataclass
import json
from pathlib import Path
import re

from .lark_transport import LarkTransportError, valid_id
from .models import sha256_text, utc_now
from .relay import RelayTarget
from .storage import FileLock, InstructionStore


@dataclass(frozen=True)
class LarkThreadMapping:
    chat_id: str
    message_id: str
    target: RelayTarget


class LarkThreadStore:
    def __init__(self, runtime, scope):
        if not isinstance(scope, str) or re.fullmatch(r"[0-9a-f]{64}", scope) is None:
            raise ValueError("Lark scope is invalid")
        self.scope = scope
        self.path = Path(runtime) / "lark" / scope / "relay-state.json"
        self.lock_path = Path(runtime) / "locks" / ("lark-relay-" + scope + ".lock")

    @staticmethod
    def _address(chat_id, message_id):
        if not valid_id(chat_id, "oc") or not valid_id(message_id, "om"):
            raise ValueError("Lark message address is invalid")
        return sha256_text(chat_id + "\0" + message_id)

    def _read(self):
        if not self.path.exists():
            return {"schema_version": 1, "scope": self.scope, "threads": {},
                    "notifications": {}, "events": {}, "messages": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("scope") != self.scope
                or any(not isinstance(value.get(key), dict)
                       for key in ("threads", "notifications", "events", "messages"))):
            raise ValueError("Lark relay state is malformed")
        for key, entry in value["threads"].items():
            if (not isinstance(entry, dict)
                    or self._address(entry.get("chat_id"), entry.get("message_id")) != key
                    or not self._digest(entry.get("event_fingerprint"))):
                raise ValueError("Lark routing state is malformed")
            RelayTarget.from_dict(entry.get("target"))
        for key, entry in value["notifications"].items():
            if (not self._digest(key) or not isinstance(entry, dict)
                    or not self._digest(entry.get("payload_sha256"))
                    or entry.get("state") not in ("uncertain", "sent")):
                raise ValueError("Lark notification state is malformed")
            if entry["state"] == "sent":
                self._address(entry.get("chat_id"), entry.get("message_id"))
        for key, entry in value["events"].items():
            if (not self._digest(key) or not isinstance(entry, dict)
                    or not self._digest(entry.get("payload_sha256"))
                    or not self._digest(entry.get("message_key"))
                    or not self._digest(entry.get("text_sha256"))
                    or entry.get("state") not in ("dispatching", "delivered", "uncertain")
                    or not isinstance(entry.get("instruction_id"), str)):
                raise ValueError("Lark event state is malformed")
        for key, entry in value["messages"].items():
            if (not self._digest(key) or not isinstance(entry, dict)
                    or not self._digest(entry.get("payload_sha256"))
                    or entry.get("event_key") not in value["events"]):
                raise ValueError("Lark message receipt is malformed")
        return value

    @staticmethod
    def _digest(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    def _write(self, state):
        InstructionStore._atomic_json(self.path, state)

    def prepare_notification(self, fingerprint, payload_sha256):
        if not self._digest(fingerprint) or not self._digest(payload_sha256):
            raise ValueError("Lark notification fingerprint is invalid")
        with FileLock(self.lock_path):
            state = self._read()
            previous = state["notifications"].get(fingerprint)
            if previous is not None:
                if previous["payload_sha256"] != payload_sha256:
                    raise LarkTransportError("lark_notification_id_collision")
                if previous["state"] != "sent":
                    raise LarkTransportError("lark_notification_outcome_uncertain")
                return dict(previous)
            state["notifications"][fingerprint] = {
                "payload_sha256": payload_sha256, "state": "uncertain", "created_at": utc_now(),
            }
            self._write(state)
        return None

    def finish_notification(self, fingerprint, chat_id, message_id, target=None):
        key = self._address(chat_id, message_id)
        with FileLock(self.lock_path):
            state = self._read()
            receipt = state["notifications"][fingerprint]
            if target is not None:
                self._put_mapping(state, key, chat_id, message_id, target, fingerprint)
            receipt.update(state="sent", chat_id=chat_id, message_id=message_id)
            self._write(state)

    def _put_mapping(self, state, key, chat_id, message_id, target, fingerprint):
        previous = state["threads"].get(key)
        if previous is not None:
            if (previous["target"]["thread_id"] != target.thread_id
                    or previous["event_fingerprint"] != fingerprint):
                raise LarkTransportError("lark_thread_mapping_collision")
            return
        state["threads"][key] = {"chat_id": chat_id, "message_id": message_id,
                                 "target": target.to_dict(), "event_fingerprint": fingerprint,
                                 "created_at": utc_now()}

    def lookup_thread(self, chat_id, message_id):
        key = self._address(chat_id, message_id)
        with FileLock(self.lock_path):
            entry = self._read()["threads"].get(key)
        if entry is None:
            return None
        return LarkThreadMapping(chat_id, message_id, RelayTarget.from_dict(entry["target"]))

    def notification_mappings(self, fingerprint):
        with FileLock(self.lock_path):
            return [self._envelope(entry) for entry in self._read()["threads"].values()
                    if entry["event_fingerprint"] == fingerprint]

    def has_notification_mapping(self, fingerprint):
        return bool(self.notification_mappings(fingerprint))

    def _envelope(self, entry):
        return {"provider": "lark", "scope": self.scope, **{
            key: entry[key] for key in ("chat_id", "message_id", "event_fingerprint")}}

    def mappings_for_threads(self, thread_ids):
        with FileLock(self.lock_path):
            return [dict(self._envelope(entry), thread_id=entry["target"]["thread_id"])
                    for entry in self._read()["threads"].values() if entry["target"]["thread_id"] in thread_ids]

    def cache_mappings(self, entries, target):
        with FileLock(self.lock_path):
            state = self._read()
            changed = False
            for entry in entries:
                if entry.get("provider") != "lark" or entry.get("scope") != self.scope:
                    continue
                if not self._digest(entry.get("event_fingerprint")):
                    raise ValueError("Lark mapping fingerprint is invalid")
                key = self._address(entry.get("chat_id"), entry.get("message_id"))
                self._put_mapping(state, key, entry["chat_id"], entry["message_id"],
                                  target, entry["event_fingerprint"])
                changed = True
            if changed:
                self._write(state)

    def claim_reply(self, *, event_key, message_key, payload_sha256, instruction_id, text):
        event_digest = sha256_text(event_key)
        message_digest = sha256_text(message_key)
        with FileLock(self.lock_path):
            state = self._read()
            previous = state["events"].get(event_digest)
            message = state["messages"].get(message_digest)
            if previous is not None:
                if previous["payload_sha256"] != payload_sha256 or previous["message_key"] != message_digest:
                    raise LarkTransportError("lark_event_id_collision")
                return False, previous.get("delivery_status")
            if message is not None:
                if message["payload_sha256"] != payload_sha256:
                    raise LarkTransportError("lark_message_id_collision")
                return False, state["events"][message["event_key"]].get("delivery_status")
            state["events"][event_digest] = {
                "payload_sha256": payload_sha256, "message_key": message_digest,
                "instruction_id": instruction_id, "text_sha256": sha256_text(text),
                "text_chars": len(text), "state": "dispatching", "delivery_status": None,
                "created_at": utc_now(), "error_sha256": None,
            }
            state["messages"][message_digest] = {"event_key": event_digest, "payload_sha256": payload_sha256}
            self._write(state)
        return True, None

    def finish_reply(self, event_key, *, state_value, delivery_status, error_sha256=None):
        if state_value not in ("delivered", "uncertain"):
            raise ValueError("Lark reply state is invalid")
        with FileLock(self.lock_path):
            state = self._read()
            entry = state["events"][sha256_text(event_key)]
            entry.update(state=state_value, delivery_status=delivery_status,
                         error_sha256=error_sha256, updated_at=utc_now())
            self._write(state)
