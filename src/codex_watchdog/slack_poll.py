"""Read replies to this host's own mapped notifications without a shared socket."""

import json
import re
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import sha256_text, utc_now
from .messaging_device import device_label
from .slack_mapping import SlackThreadStore, valid_slack_timestamp
from .storage import FileLock, InstructionStore


class SlackPollingThreadStore(SlackThreadStore):
    """Separate inbox: old Socket Mode receipts cannot identify polled events.

    Keep existing socket mappings intact. A notification belongs to one reply
    transport for its lifetime, including when an older desktop observes it.
    """

    def __init__(self, runtime):
        super().__init__(runtime)
        self.path = self.runtime / "slack" / "poll-relay-state.json"
        self.lock_path = self.runtime / "locks" / "slack-poll-relay.lock"

    def notification_mappings(self, fingerprint):
        return []  # Do not export these parents to a competing Socket listener.

    def mappings_for_threads(self, thread_ids):
        return []

    def cache_mappings(self, entries, target):
        pass  # Never import or replay another transport's historical replies.

    def poll_mappings(self):
        journal = self.journal
        with journal.transaction() as db:
            return journal.active(db)


class SlackReplyPoller:
    """One bounded API page per tick, durable cursors, existing exact-thread relay."""

    def __init__(self, relay, *, api=None):
        self.relay = relay
        self.device_label = device_label(relay.runtime)
        self.api = api or self._api
        self.path = relay.runtime / "slack" / "poll-cursors.json"
        self.health_path = relay.runtime / "slack" / "poll-health.json"
        self.lock = FileLock(relay.runtime / "locks" / "slack-poll-listener.lock")
        self.stop = threading.Event()
        self.thread = None
        self.next_poll = 0.0
        self._tick_count = 0

    def _api(self, method, params):
        request = Request("https://slack.com/api/" + method,
                          data=urlencode(params).encode(),
                          headers={"Authorization": "Bearer " + self.relay.bot_token})
        with urlopen(request, timeout=10) as response:
            value = json.load(response)
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise RuntimeError("slack_poll_api_failed:" + str(value.get("error", "invalid_response")))
        return value

    def _read(self):
        if not self.path.exists():
            return dict(schema_version=1, after=None, threads={})
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("schema_version") != 1
                or not isinstance(state.get("threads"), dict)
                or any(not valid_slack_timestamp(ts) for ts in state["threads"].values())):
            raise ValueError("slack_poll_cursor_invalid")
        closed_cursor = state.get("closed_cursor")
        if closed_cursor is not None and (not isinstance(closed_cursor, str)
                or re.fullmatch(r"[0-9a-f]{64}", closed_cursor) is None):
            raise ValueError("slack_poll_cursor_invalid")
        return state

    def _health(self, status, **fields):
        InstructionStore._atomic_json(self.health_path,
            dict(schema_version=1, checked_at=utc_now(), status=status, device_label=self.device_label, **fields))

    def poll_once(self):
        """Caller holds the listener lock; errors never advance a reply cursor."""
        self._tick_count += 1
        if self._tick_count % 5 == 0:
            historical_results = self._poll_closed_once()
            if historical_results is not None:
                return historical_results
        state = self._read()
        mappings = self.relay.thread_store.poll_mappings()
        keys = sorted(mappings)
        retained = {key: value for key, value in state["threads"].items() if key in mappings}
        if retained != state["threads"]:
            state["threads"] = retained
            InstructionStore._atomic_json(self.path, state)
        if not keys:
            self._health("waiting_for_mapped_notification")
            return []
        later = [key for key in keys if key > (state.get("after") or "")]
        key = (later or keys)[0]
        mapping = mappings[key]
        parent = mapping["thread_ts"]
        oldest = state["threads"].get(key, parent)
        channel = mapping["channel_id"]
        page = self.api("conversations.replies", dict(channel=channel, ts=parent,
                         oldest=oldest, inclusive="false", limit=100))
        messages = page.get("messages")
        if not isinstance(messages, list):
            raise ValueError("slack_poll_page_invalid")
        # Validate the entire page before any delivery. The parent may appear
        # despite oldest; it is never a reply, even if posted by an allowlisted user.
        for message in messages:
            if (not isinstance(message, dict) or not valid_slack_timestamp(message.get("ts"))
                    or (message["ts"] != parent and message.get("thread_ts") != parent)):
                raise ValueError("slack_poll_reply_identity_invalid")
        messages = sorted(messages, key=lambda message: Decimal(message["ts"]))
        results = []
        for message in messages:
            if Decimal(message["ts"]) <= Decimal(oldest) or message["ts"] == parent:
                continue
            # A channel is not supplied by conversations.replies; use only the
            # request's saved mapping, never text parsed from a Slack message.
            event = dict(message, channel=channel, thread_ts=parent)
            result = self.relay.handle_message(event)
            results.append(result.to_dict())
            if result.status == "deferred":
                break  # No dispatch occurred. Retry this exact message later.
            state["threads"][key] = message["ts"]
            InstructionStore._atomic_json(self.path, state)
            self.relay.acknowledge(event, result, SimpleNamespace(
                chat_postMessage=lambda **params: self.api("chat.postMessage", params)))
            if key not in self.relay.thread_store.poll_mappings():
                break  # The first claim closes this parent, including uncertain delivery.
        state["after"] = key
        InstructionStore._atomic_json(self.path, state)
        self._health("polling", mapped_threads=len(keys), results=results)
        return results

    def _poll_closed_once(self):
        """Poll ONE closed parent for historical bind/unbind commands."""
        state = self._read()
        closed_cursor = state.get("closed_cursor") or ""
        journal = self.relay.thread_store.journal

        mapping_key = None
        mapping_value = None
        cursor_entry = None
        with journal.transaction() as db:
            row = db.execute(
                "SELECT key, value FROM records WHERE namespace=? AND kind='threads'"
                " AND active=0 AND key > ? ORDER BY key LIMIT 1",
                (journal.namespace, closed_cursor),
            ).fetchone()
            if row is None and closed_cursor:
                row = db.execute(
                    "SELECT key, value FROM records WHERE namespace=? AND kind='threads'"
                    " AND active=0 ORDER BY key LIMIT 1",
                    (journal.namespace,),
                ).fetchone()
            if row is None:
                return
            mapping_key = row[0]
            mapping_value = json.loads(row[1])
            cursor_entry = journal.get(db, "route_poll_cursors", mapping_key)

        channel = mapping_value["channel_id"]
        parent = mapping_value["thread_ts"]
        if self.relay.thread_store.thread_key(channel, parent) != mapping_key:
            raise ValueError("route_poll_mapping_identity_invalid")
        from .relay import RelayTarget
        RelayTarget.from_dict(mapping_value["target"])
        if cursor_entry is not None:
            if (not isinstance(cursor_entry, dict)
                    or type(cursor_entry.get("schema_version")) is not int
                    or cursor_entry["schema_version"] != 1
                    or not valid_slack_timestamp(cursor_entry.get("cursor"))):
                raise ValueError("route_poll_cursor_schema_invalid")
            oldest = cursor_entry["cursor"]
            if Decimal(oldest) < Decimal(parent):
                raise ValueError("route_poll_cursor_identity_invalid")
        else:
            oldest = parent

        page = self.api("conversations.replies", dict(
            channel=channel, ts=parent, oldest=oldest, inclusive="false", limit=100))
        messages = page.get("messages")
        if not isinstance(messages, list):
            raise ValueError("slack_poll_page_invalid")
        for message in messages:
            if (not isinstance(message, dict) or not valid_slack_timestamp(message.get("ts"))
                    or (message["ts"] != parent and message.get("thread_ts") != parent)):
                raise ValueError("slack_poll_reply_identity_invalid")
        messages = sorted(messages, key=lambda m: Decimal(m["ts"]))
        results = []
        for message in messages:
            if Decimal(message["ts"]) <= Decimal(oldest) or message["ts"] == parent:
                continue
            if (message.get("subtype") is not None
                    or message.get("bot_id") is not None
                    or message.get("bot_profile") is not None):
                oldest = message["ts"]
                with journal.transaction() as db:
                    journal.put(db, "route_poll_cursors", mapping_key,
                                {"schema_version": 1, "cursor": oldest, "updated_at": utc_now()})
                continue
            if message.get("user") not in self.relay.allowed_user_ids:
                oldest = message["ts"]
                with journal.transaction() as db:
                    journal.put(db, "route_poll_cursors", mapping_key,
                                {"schema_version": 1, "cursor": oldest, "updated_at": utc_now()})
                continue
            text = message.get("text", "")
            first_word = text.strip().split()[0].lower() if isinstance(text, str) and text.strip() else ""
            if first_word not in ("bind", "unbind"):
                oldest = message["ts"]
                with journal.transaction() as db:
                    journal.put(db, "route_poll_cursors", mapping_key,
                                {"schema_version": 1, "cursor": oldest, "updated_at": utc_now()})
                continue
            event = dict(message, channel=channel, thread_ts=parent)
            result = self.relay.handle_message(event)
            results.append(result.to_dict())
            if result.status == "deferred":
                break
            oldest = message["ts"]
            with journal.transaction() as db:
                journal.put(db, "route_poll_cursors", mapping_key,
                            {"schema_version": 1, "cursor": oldest, "updated_at": utc_now()})
            self.relay.acknowledge(event, result, SimpleNamespace(
                chat_postMessage=lambda **params: self.api("chat.postMessage", params)))

        state["closed_cursor"] = mapping_key
        InstructionStore._atomic_json(self.path, state)
        self._health("polling_closed_parent", closed_key=mapping_key[:16], results=results)
        return results

    def _run(self):
        while not self.stop.is_set():
            if time.monotonic() >= self.next_poll:
                delay = 10.0
                try:
                    self.poll_once()
                except Exception as error:
                    if isinstance(error, HTTPError) and error.code == 429:
                        try:
                            delay = max(delay, float(error.headers.get("Retry-After", "60")))
                        except ValueError:
                            delay = 60.0
                    try:
                        self._health("retrying", error_sha256=sha256_text(type(error).__name__ + ":" + str(error)),
                                     retry_seconds=delay)
                    except OSError:
                        pass
                self.next_poll = time.monotonic() + delay
            self.stop.wait(1)

    def start(self):
        if self.thread is not None:
            return
        self.lock.__enter__()
        try:
            self.thread = threading.Thread(target=self._run, name="watchdog-slack-replies", daemon=True)
            self.thread.start()
        except BaseException:
            self.lock.__exit__(None, None, None)
            self.thread = None
            raise

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()  # API requests have a bounded timeout.
            self.thread = None
            self.lock.__exit__(None, None, None)
