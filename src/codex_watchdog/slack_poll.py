"""Read replies to this host's own mapped notifications without a shared socket."""

import json
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import sha256_text, utc_now
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
        with FileLock(self.lock_path):
            return self._read_state()["threads"]


class SlackReplyPoller:
    """One bounded API page per tick, durable cursors, existing exact-thread relay."""

    def __init__(self, relay, *, api=None):
        self.relay = relay
        self.api = api or self._api
        self.path = relay.runtime / "slack" / "poll-cursors.json"
        self.health_path = relay.runtime / "slack" / "poll-health.json"
        self.lock = FileLock(relay.runtime / "locks" / "slack-poll-listener.lock")
        self.stop = threading.Event()
        self.thread = None
        self.next_poll = 0.0

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
        return state

    def _health(self, status, **fields):
        InstructionStore._atomic_json(self.health_path,
            dict(schema_version=1, checked_at=utc_now(), status=status, **fields))

    def poll_once(self):
        """Caller holds the listener lock; errors never advance a reply cursor."""
        state = self._read()
        mappings = self.relay.thread_store.poll_mappings()
        keys = sorted(mappings)
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
        state["after"] = key
        InstructionStore._atomic_json(self.path, state)
        self._health("polling", mapped_threads=len(keys), results=results)
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
