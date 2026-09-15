"""Shared-app replies: each runtime reads history and admits only its mappings.

Feishu long connections load-balance events instead of broadcasting them. A
socket on another host can acknowledge an event with no local route. Polling
does not consume that event and reuses the existing message-id admission fence.
"""
import json
import threading
import time

from .lark_transport import LarkApi, LarkTransportError, valid_id
from .models import sha256_text, utc_now
from .messaging_device import device_label
from .storage import InstructionStore


class LarkReplyPoller:
    def __init__(self, relay, *, api=None, clock=time.time):
        self.relay = relay
        self.device_label = device_label(relay.runtime)
        self.api = api or relay.api or LarkApi(relay.config, relay.timeout)
        self.clock = clock
        self.started_ms = int(clock() * 1000)
        self.chat_hash = sha256_text(relay.config.chat_id)
        base = relay.runtime / "lark" / relay.config.scope
        self.path = base / ("poll-cursor-" + self.chat_hash + ".json")
        self.health_path = base / ("poll-health-" + self.chat_hash + ".json")
        self.stop = threading.Event()
        self.thread = None

    def _read(self):
        if not self.path.exists():
            state = dict(schema_version=1, scope=self.relay.config.scope, chat_sha256=self.chat_hash,
                         not_before_ms=self.started_ms, after=self.started_ms // 1000,
                         window_end=None, page_token=None)
            # A first switch from socket mode does not replay historical commands.
            # Subsequent restarts resume the retained cursor, including downtime.
            InstructionStore._atomic_json(self.path, state)
            return state
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("schema_version") != 1 or state.get("scope") != self.relay.config.scope
                or state.get("chat_sha256") != self.chat_hash
                or any(type(state.get(k)) is not int or state[k] < 0 for k in ("not_before_ms", "after"))
                or (state.get("window_end") is not None and
                    (type(state["window_end"]) is not int or state["window_end"] < state["after"]))
                or (state.get("page_token") is not None and
                    (not isinstance(state["page_token"], str) or not 0 < len(state["page_token"]) <= 4096))
                or (state.get("page_token") is not None and state.get("window_end") is None)):
            raise LarkTransportError("lark_poll_cursor_invalid")
        return state

    def _health(self, status, **fields):
        InstructionStore._atomic_json(self.health_path,
            dict(schema_version=1, checked_at=utc_now(), status=status, device_label=self.device_label, **fields))

    def poll_once(self):
        """One page per tick; the relay owns the runtime's listener lock."""
        state = self._read()
        end = state["window_end"]
        if end is None:
            end = min(int(self.clock()) - 2, state["after"] + 300)
        if end <= state["after"]:
            self._health("waiting_for_new_replies")
            return []
        # A short overlap catches messages becoming visible near a page boundary.
        start = max(state["not_before_ms"] // 1000, state["after"] - 5)
        page = self.api.history(start, end, state["page_token"])
        items = page.get("items") if isinstance(page, dict) else None
        more = page.get("has_more") if isinstance(page, dict) else None
        token = page.get("page_token") if isinstance(page, dict) else None
        if (not isinstance(items, list) or len(items) > 50 or type(more) is not bool
                or (more and (not items or not isinstance(token, str) or not 0 < len(token) <= 4096
                              or token == state["page_token"]))):
            raise LarkTransportError("lark_history_page_invalid")
        for message in items:
            if (not isinstance(message, dict) or not valid_id(message.get("message_id"), "om")
                    or message.get("chat_id") != self.relay.config.chat_id
                    or not str(message.get("create_time", "")).isdigit()
                    or not start * 1000 <= int(message["create_time"]) < (end + 1) * 1000
                    or type(message.get("deleted")) is not bool
                    or not isinstance(message.get("sender"), dict)
                    or not isinstance(message.get("body"), dict)):
                raise LarkTransportError("lark_history_message_invalid")
        results = []
        for message in sorted(items, key=lambda m: (int(m["create_time"]), m["message_id"])):
            if int(message["create_time"]) < state["not_before_ms"] or message["deleted"]:
                continue
            sender = message["sender"]
            if sender.get("id_type") != "open_id":
                continue
            # This envelope is an internal adapter from the authenticated SDK
            # history response, not a fabricated inbound provider event. The
            # shared relay validates sender/chat/root/parent and existing owner.
            payload = {"schema": "2.0", "header": {
                "app_id": self.relay.config.app_id, "event_type": "im.message.receive_v1",
                "event_id": "poll-" + sha256_text(message["message_id"])}, "event": {
                "sender": {"sender_type": sender.get("sender_type"), "sender_id": {"open_id": sender.get("id")}},
                "message": {"message_id": message["message_id"], "chat_id": message["chat_id"],
                    "message_type": message.get("msg_type"), "content": message["body"].get("content"),
                    "root_id": message.get("root_id"), "parent_id": message.get("parent_id")}}}
            result = self.relay.handle_event(payload)
            results.append(result.to_dict())
            if result.status == "deferred":
                self._health("deferred", results=results)
                return results  # Do not advance an unadmitted reply past the cursor.
            self.relay.acknowledge(message["message_id"], result)
        if more:
            state.update(window_end=end, page_token=token)
        else:
            state.update(after=end, window_end=None, page_token=None)
        InstructionStore._atomic_json(self.path, state)
        self._health("polling", results=results)
        return results

    def _run(self):
        while not self.stop.is_set():
            delay = 10
            try:
                self.poll_once()
            except Exception as error:
                code = str(error) if isinstance(error, LarkTransportError) else "lark_poll_failed"
                if code == "lark_history_rate_limited":
                    delay = 60
                try:
                    self._health("retrying", reason=code, retry_seconds=delay)
                except OSError:
                    pass
            self.stop.wait(delay)

    def start(self):
        if self.thread is None:
            self._read()  # Validate/persist the baseline before the background loop.
            self.stop.clear()
            self.thread = threading.Thread(target=self._run, name="watchdog-lark-replies", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()  # Each SDK request has the configured HTTP timeout.
            self.thread = None
