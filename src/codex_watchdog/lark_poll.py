"""Shared-app replies through bounded pages of still-active native threads.

Feishu long connections load-balance events instead of broadcasting them. A
socket on another host can acknowledge an event with no local route. Polling
does not consume that event and reuses the existing message-id admission fence.
"""
import json
import re
import threading
import time
from datetime import datetime

from .lark_transport import LarkApi, LarkTransportError, valid_id
from .models import sha256_text, utc_now
from .messaging_device import device_label
from .storage import InstructionStore


class LarkReplyPoller:
    PARENTS_PER_TICK = 4  # Work budget, not a cap on sessions or valid tickets.

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
        self.active_path = base / "poll-active-parents.json"
        self.stop = threading.Event()
        self.thread = None
        self._binding_discovery = None

    def _read(self, chat_id=None, baseline_ms=None, *, create=True):
        chat_hash = self.chat_hash if chat_id is None else sha256_text(chat_id)
        path = self.path.with_name("poll-cursor-" + chat_hash + ".json")
        baseline = self.started_ms if baseline_ms is None else baseline_ms
        if not path.exists():
            if not create:
                raise LarkTransportError("lark_poll_cursor_invalid")
            state = dict(schema_version=1, scope=self.relay.config.scope, chat_sha256=chat_hash,
                         not_before_ms=baseline, after=baseline // 1000,
                         window_end=None, page_token=None)
            # A first switch from socket mode does not replay historical commands.
            # Subsequent restarts resume the retained cursor, including downtime.
            InstructionStore._atomic_json(path, state)
            return state
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise LarkTransportError("lark_poll_cursor_invalid") from None
        if (not isinstance(state, dict) or state.get("schema_version") != 1 or state.get("scope") != self.relay.config.scope
                or state.get("chat_sha256") != chat_hash
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
        """Rotate valid tickets before attempts; one parent's fault is local."""
        journal = self.relay.thread_store.journal
        with journal.transaction() as db:
            mappings = journal.active(db)  # Existing last-four/provider/session policy.
        state = self._read_active()
        state["parents"] = {key: value for key, value in state["parents"].items() if key in mappings}
        seed_errors = {}
        for key, mapping in mappings.items():
            if key not in state["parents"]:
                try:
                    state["parents"][key] = self._new_cursor(mapping)
                except Exception as error:
                    seed_errors[key] = self._error_code(error)
        # Retain every known active root's first-adoption floor before a restart
        # can move it, even when its provider visit is beyond this tick's budget.
        self._save_active(state)
        keys = sorted(mappings)
        after = state["after_key"]
        if after is not None:
            keys = [key for key in keys if key > after] + [key for key in keys if key <= after]
        results, failures = [], []
        for key in keys[:self.PARENTS_PER_TICK]:
            # Persist rotation before provider/handler work, including restart.
            state["after_key"] = key
            self._save_active(state)
            try:
                if key in seed_errors:
                    raise LarkTransportError(seed_errors[key])
                if self._active(key):
                    results.extend(self._poll_root(key, mappings[key], state))
                if not self._active(key):
                    state["parents"].pop(key, None)
                    self._save_active(state)
            except Exception as error:
                code = self._error_code(error)
                if code == "lark_history_rate_limited":
                    raise LarkTransportError(code) from None  # Provider-wide backoff.
                failures.append(dict(parent_key=key, reason=code))
        if self._binding_discovery is None:
            from .lark_binding import LarkBindingDiscovery
            self._binding_discovery = LarkBindingDiscovery(self.relay, self.api, clock=self.clock)
        try:
            results.extend(self._binding_discovery.poll_once())
        except Exception as error:
            code = self._error_code(error)
            if code == "lark_history_rate_limited":
                raise LarkTransportError(code) from None
            failures.append(dict(reason=code))
        self._save_active(state)
        self._health("retrying" if failures else "polling" if mappings else "waiting_for_active_tickets",
                     results=results, **(dict(reason=failures[0]["reason"], failures=failures,
                                             retry_seconds=10) if failures else {}))
        return results

    @staticmethod
    def _digest(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    @staticmethod
    def _thread_id(value):
        return valid_id(value, "omt") or valid_id(value, "om")

    @staticmethod
    def _error_code(error):
        known = {"lark_history_rate_limited", "lark_history_unavailable_check_permissions",
                 "lark_history_failed_or_timed_out", "lark_history_page_invalid",
                 "lark_history_message_invalid", "lark_poll_cursor_invalid",
                 "lark_mapping_chat_invalid", "lark_mapping_time_invalid",
                 "lark_poll_root_invalid", "lark_poll_message_invalid",
                 "lark_poll_message_id_invalid", "lark_poll_thread_id_invalid",
                 "lark_poll_pending_changed", "lark_poll_reply_window_saturated"}
        return str(error) if isinstance(error, LarkTransportError) and str(error) in known else "lark_poll_failed"

    def _read_active(self):
        if not self.active_path.exists():
            return dict(schema_version=1, scope=self.relay.config.scope, after_key=None, parents={})
        state = json.loads(self.active_path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict)
                or not {"schema_version", "scope", "after_key", "parents"}.issubset(state)
                or type(state.get("schema_version")) is not int or state["schema_version"] != 1
                or state.get("scope") != self.relay.config.scope
                or (state.get("after_key") is not None and not self._digest(state["after_key"]))
                or not isinstance(state.get("parents"), dict)):
            raise LarkTransportError("lark_poll_cursor_invalid")
        for key, value in state["parents"].items():
            if (not self._digest(key) or not isinstance(value, dict)
                    or not {"not_before_ms", "after_ms", "seen_ids", "thread_id", "pending"}.issubset(value)
                    or any(type(value.get(k)) is not int or value[k] < 0 for k in ("not_before_ms", "after_ms"))
                    or value["after_ms"] < value["not_before_ms"]
                    or (value.get("thread_id") is not None and not self._thread_id(value["thread_id"]))
                    or not isinstance(value.get("seen_ids"), list) or len(value["seen_ids"]) > 50
                    or any(not valid_id(mid, "om") for mid in value["seen_ids"])
                    or len(set(value["seen_ids"])) != len(value["seen_ids"])):
                raise LarkTransportError("lark_poll_cursor_invalid")
            pending = value.get("pending")
            if pending is not None and (not isinstance(pending, dict)
                    or not valid_id(pending.get("message_id"), "om")
                    or type(pending.get("create_time")) is not int
                    or pending["create_time"] < value["after_ms"]
                    or not self._digest(pending.get("sha256"))
                    or (pending["create_time"] == value["after_ms"] and pending["message_id"] in value["seen_ids"])):
                raise LarkTransportError("lark_poll_cursor_invalid")
        return state

    def _save_active(self, state):
        InstructionStore._atomic_json(self.active_path, state)

    def _active(self, key):
        journal = self.relay.thread_store.journal
        with journal.transaction() as db:
            return db.execute("SELECT 1 FROM records WHERE namespace=? AND kind='threads' AND key=? AND active=1",
                              (journal.namespace, key)).fetchone() is not None

    def _baseline(self, mapping):
        if not valid_id(mapping.get("chat_id"), "oc") or not valid_id(mapping.get("message_id"), "om"):
            raise LarkTransportError("lark_mapping_chat_invalid")
        try:
            timestamp = datetime.fromisoformat(mapping["created_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError()
            baseline = int(timestamp.timestamp() * 1000)
            if baseline < 0:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise LarkTransportError("lark_mapping_time_invalid") from None
        # Only a completed, exactly scoped old cursor proves a later floor.
        # Retain its bytes for rollback; never resume its chronological backlog.
        path = self.path.with_name("poll-cursor-" + sha256_text(mapping["chat_id"]) + ".json")
        if path.exists():
            legacy = self._read(mapping["chat_id"], create=False)
            baseline = max(baseline, legacy["not_before_ms"])
            if legacy["window_end"] is None and legacy["page_token"] is None:
                baseline = max(baseline, legacy["after"] * 1000)
        else:
            baseline = max(baseline, self.started_ms)
        return baseline

    def _validate_message(self, message, mapping, thread_id):
        root = mapping["message_id"]
        if (not isinstance(message, dict) or not valid_id(message.get("message_id"), "om")
                or message.get("chat_id") != mapping["chat_id"]
                or not str(message.get("create_time", "")).isdigit()
                or not 0 <= int(message["create_time"]) <= int(self.clock() * 1000) + 2000
                or type(message.get("deleted")) is not bool
                or any(key in message and type(message[key]) is not bool for key in ("updated", "edited"))
                or not isinstance(message.get("sender"), dict)
                or not isinstance(message.get("body"), dict)
                or (message.get("thread_id") and message["thread_id"] != thread_id)):
            raise LarkTransportError("lark_history_message_invalid")
        if message["message_id"] == root:
            if message.get("root_id") or message.get("parent_id"):
                raise LarkTransportError("lark_history_message_invalid")
        elif (message.get("root_id") != root
                or (message.get("parent_id") and not valid_id(message["parent_id"], "om"))):
            raise LarkTransportError("lark_history_message_invalid")

    @staticmethod
    def _message_digest(message):
        fields = ("message_id", "chat_id", "root_id", "parent_id", "thread_id", "create_time",
                  "deleted", "edited", "updated", "update_time", "msg_type", "sender", "body", "mentions")
        return sha256_text(json.dumps({key: message.get(key) for key in fields}, sort_keys=True, separators=(",", ":")))

    def _handle_message(self, message, mapping, cursor, state):
        created, mid = int(message["create_time"]), message["message_id"]
        if created < cursor["after_ms"] or (created == cursor["after_ms"] and mid in cursor["seen_ids"]):
            return None
        if created == cursor["after_ms"] and len(cursor["seen_ids"]) == 50:
            raise LarkTransportError("lark_poll_reply_window_saturated")
        # The API may echo update_time == create_time for an unedited message.
        # Only actual edit flags make a first-read reply ineligible as a command.
        edited = message.get("updated") is True or message.get("edited") is True
        result = ({"status": "ignored_edited"}
                  if edited and not message["deleted"] and mid != mapping["message_id"] else None)
        sender = message["sender"]
        if (not edited and not message["deleted"] and mid != mapping["message_id"]
                and sender.get("id_type") == "open_id"):
            # Hash-only pending identity survives provider/handler/deferred faults.
            # Re-fetch this exact ID even if it leaves the bounded newest page.
            cursor["pending"] = dict(message_id=mid, create_time=created, sha256=self._message_digest(message))
            self._save_active(state)
            payload = {"schema": "2.0", "header": {
                "app_id": self.relay.config.app_id, "event_type": "im.message.receive_v1",
                "event_id": "poll-" + sha256_text(mid)}, "event": {
                "sender": {"sender_type": sender.get("sender_type"), "sender_id": {"open_id": sender.get("id")}},
                "message": {"message_id": mid, "chat_id": message["chat_id"],
                    "message_type": message.get("msg_type"), "content": message["body"].get("content"),
                    "root_id": message.get("root_id"), "parent_id": message.get("parent_id"),
                    "create_time": message.get("create_time"), "edited": edited,
                    "updated": message.get("updated"), "update_time": message.get("update_time"),
                    "deleted": message.get("deleted"), "mentions": message.get("mentions")}}}
            reply = self.relay.handle_event(payload)
            result = reply.to_dict()
            if reply.status == "deferred":
                return result  # Keep the failed unread position; no new admission.
            self.relay.acknowledge(mid, reply, mapping["chat_id"])
        if created > cursor["after_ms"]:
            cursor.update(after_ms=created, seen_ids=[])
        cursor["seen_ids"].append(mid)
        cursor["pending"] = None
        self._save_active(state)
        return result

    def _poll_root(self, key, mapping, state):
        cursor = state["parents"].get(key)
        if cursor is None:
            cursor = self._new_cursor(mapping)
            state["parents"][key] = cursor
            self._save_active(state)
        if cursor["thread_id"] is None:
            thread_id = self.api.thread_for_root(mapping["message_id"], destination=mapping["chat_id"])
            if thread_id is None:
                return [{"status": "waiting_for_native_thread"}]
            if not self._thread_id(thread_id):
                raise LarkTransportError("lark_poll_thread_id_invalid")
            cursor["thread_id"] = thread_id
            self._save_active(state)
        results = []
        if cursor["pending"] is not None:
            pending = cursor["pending"]
            message = self.api.get_message(pending["message_id"])
            self._validate_message(message, mapping, cursor["thread_id"])
            if message["message_id"] != pending["message_id"] or self._message_digest(message) != pending["sha256"]:
                raise LarkTransportError("lark_poll_pending_changed")
            result = self._handle_message(message, mapping, cursor, state)
            if result is not None:
                results.append(result)
                if result.get("status") == "deferred":
                    return results
            if not self._active(key):
                return results
        if not self._active(key):
            return results
        page = self.api.thread_history(cursor["thread_id"])
        items = page.get("items") if isinstance(page, dict) else None
        more = page.get("has_more") if isinstance(page, dict) else None
        token = page.get("page_token") if isinstance(page, dict) else None
        if (not isinstance(items, list) or len(items) > 50 or type(more) is not bool
                or (more and (not items or not isinstance(token, str) or not 0 < len(token) <= 4096))):
            raise LarkTransportError("lark_history_page_invalid")
        for message in items:
            self._validate_message(message, mapping, cursor["thread_id"])
        times = [int(message["create_time"]) for message in items]
        if times != sorted(times, reverse=True):
            raise LarkTransportError("lark_history_page_invalid")
        if more and times[-1] >= cursor["after_ms"]:
            # No historical walk or unknown-tail skip. A saturated unread burst
            # is refused, while other active parents continue their bounded work.
            raise LarkTransportError("lark_poll_reply_window_saturated")
        for message in sorted(items, key=lambda m: (int(m["create_time"]), m["message_id"])):
            if not self._active(key):
                break
            result = self._handle_message(message, mapping, cursor, state)
            if result is not None:
                results.append(result)
                if result.get("status") == "deferred":
                    break
        return results

    def _new_cursor(self, mapping):
        baseline = self._baseline(mapping)
        return dict(not_before_ms=baseline, after_ms=baseline, seen_ids=[], thread_id=None, pending=None)

    def _run(self):
        while not self.stop.is_set():
            delay = 10
            try:
                self.poll_once()
            except Exception as error:
                code = self._error_code(error)
                if code == "lark_history_rate_limited":
                    delay = 60
                try:
                    self._health("retrying", reason=code, retry_seconds=delay)
                except OSError:
                    pass
            self.stop.wait(delay)

    def start(self):
        if self.thread is None:
            self._save_active(self._read_active())
            self.stop.clear()
            self.thread = threading.Thread(target=self._run, name="watchdog-lark-replies", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()  # Each SDK request has the configured HTTP timeout.
            self.thread = None
