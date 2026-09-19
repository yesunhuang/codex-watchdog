from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Dict, Optional, Tuple
import uuid

from .models import sha256_text, utc_now
from .storage import FileLock, InstructionStore
from .relay import RelayTarget as SlackRelayTarget


SLACK_RELAY_STATE_SCHEMA_VERSION = 1

_SLACK_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{8,}$")
_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")
_SLACK_TIMESTAMP = re.compile(r"^[0-9]{10,}\.[0-9]+$")


def valid_slack_channel_id(value: Any) -> bool:
    return isinstance(value, str) and _SLACK_CHANNEL_ID.fullmatch(value) is not None


def valid_slack_user_id(value: Any) -> bool:
    return isinstance(value, str) and _SLACK_USER_ID.fullmatch(value) is not None


def valid_slack_timestamp(value: Any) -> bool:
    return isinstance(value, str) and _SLACK_TIMESTAMP.fullmatch(value) is not None






@dataclass(frozen=True)
class SlackThreadMapping:
    channel_id: str
    thread_ts: str
    target: SlackRelayTarget


class SlackThreadStore:
    """Persist only exact Slack-thread routing and hash-only reply receipts."""

    def __init__(self, runtime: Path) -> None:
        self.runtime = Path(runtime)
        self.path = self.runtime / "slack" / "relay-state.json"
        self.lock_path = self.runtime / "locks" / "slack-relay.lock"

    @staticmethod
    def thread_key(channel_id: str, thread_ts: str) -> str:
        if not valid_slack_channel_id(channel_id):
            raise ValueError("Slack channel id is invalid")
        if not valid_slack_timestamp(thread_ts):
            raise ValueError("Slack thread timestamp is invalid")
        return sha256_text(f"{channel_id}\0{thread_ts}")

    @property
    def journal(self):
        from .reply_tickets import ReplyTickets
        return ReplyTickets(self.path, self.lock_path, "slack", self.runtime, self._legacy_state)

    def record_thread(self, channel_id, thread_ts, target, event_fingerprint):
        key = self.thread_key(channel_id, thread_ts)
        if not isinstance(event_fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", event_fingerprint) is None:
            raise ValueError("notification event fingerprint is invalid")
        journal = self.journal
        with journal.transaction() as db:
            journal.record(db, key, dict(channel_id=channel_id, thread_ts=thread_ts,
                target=target.to_dict(), event_fingerprint=event_fingerprint, created_at=utc_now()))

    def lookup_thread(self, channel_id, thread_ts):
        key = self.thread_key(channel_id, thread_ts)
        journal = self.journal
        with journal.transaction() as db:
            entry = journal.get(db, "threads", key)
        return SlackThreadMapping(channel_id, thread_ts, SlackRelayTarget.from_dict(entry["target"])) if entry else None

    def has_notification_mapping(self, event_fingerprint):
        journal = self.journal
        with journal.transaction() as db:
            return bool(journal.mappings(db, fingerprint=event_fingerprint))

    def lookup_reply(self, event_key):
        journal = self.journal
        with journal.transaction() as db:
            return journal.get(db, "events", sha256_text(event_key))

    def notification_mappings(self, fingerprint):
        journal = self.journal
        with journal.transaction() as db:
            return [dict(entry, ticket_schema=1) for entry in journal.mappings(db, fingerprint=fingerprint, active=True)]

    def mappings_for_threads(self, thread_ids):
        journal = self.journal
        with journal.transaction() as db:
            return [dict(entry, ticket_schema=1, thread_id=entry["target"]["thread_id"])
                    for entry in journal.mappings(db, thread_ids=thread_ids, active=True)]

    def cache_mappings(self, entries, target):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("provider") in ("lark", "onebot"):
                continue
            from .reply_tickets import ticket_time
            created = ticket_time(entry)
            key = self.thread_key(entry["channel_id"], entry["thread_ts"])
            journal = self.journal
            with journal.transaction() as db:
                journal.record(db, key, dict(channel_id=entry["channel_id"], thread_ts=entry["thread_ts"],
                    target=target.to_dict(), event_fingerprint=entry["event_fingerprint"],
                    created_at=created or utc_now()), active=created is not None)

    def claim_reply(self, *, event_key, channel_id, thread_ts, instruction_id, text):
        event_digest = sha256_text(event_key)
        key = self.thread_key(channel_id, thread_ts)
        journal = self.journal
        with journal.transaction() as db:
            previous = journal.get(db, "events", event_digest)
            if previous is not None:
                if previous["thread_key"] != key or previous["text_sha256"] != sha256_text(text):
                    raise ValueError("reply_ticket_event_collision")
                return False, previous.get("delivery_status")
            if not journal.claim(db, key):
                return False, "ticket_closed"
            journal.put(db, "events", event_digest, dict(thread_key=key,
                instruction_id=instruction_id, text_sha256=sha256_text(text), text_chars=len(text),
                state="dispatching", delivery_status=None, created_at=utc_now(),
                updated_at=None, error_sha256=None))
        return True, None

    def finish_reply(self, event_key, *, state_value, delivery_status, error_sha256=None):
        if state_value not in ("delivered", "uncertain"):
            raise ValueError("Slack reply state is invalid")
        journal = self.journal
        with journal.transaction() as db:
            key = sha256_text(event_key)
            entry = journal.get(db, "events", key)
            if entry is None:
                raise ValueError("Slack reply event was not claimed")
            entry.update(state=state_value, delivery_status=delivery_status,
                         updated_at=utc_now(), error_sha256=error_sha256)
            journal.put(db, "events", key, entry)

    def _legacy_state(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": SLACK_RELAY_STATE_SCHEMA_VERSION,
                "threads": {},
                "events": {},
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or frozenset(value) != frozenset({"schema_version", "threads", "events"})
            or value.get("schema_version") != SLACK_RELAY_STATE_SCHEMA_VERSION
            or not isinstance(value.get("threads"), dict)
            or not isinstance(value.get("events"), dict)
        ):
            raise ValueError("Slack relay state is malformed")
        for key, entry in value["threads"].items():
            if (
                not isinstance(key, str)
                or len(key) != 64
                or not isinstance(entry, dict)
            ):
                raise ValueError("Slack relay thread state is malformed")
            if frozenset(entry) != frozenset(
                {"channel_id", "thread_ts", "target", "event_fingerprint", "created_at"}
            ):
                raise ValueError("Slack relay thread state is malformed")
            if self.thread_key(entry["channel_id"], entry["thread_ts"]) != key:
                raise ValueError("Slack relay thread identity is malformed")
            SlackRelayTarget.from_dict(entry["target"])
            if (
                not isinstance(entry["event_fingerprint"], str)
                or len(entry["event_fingerprint"]) != 64
                or not isinstance(entry["created_at"], str)
                or not entry["created_at"]
            ):
                raise ValueError("Slack relay thread state is malformed")
        for key, entry in value["events"].items():
            if (
                not isinstance(key, str)
                or len(key) != 64
                or not isinstance(entry, dict)
                or frozenset(entry)
                != frozenset(
                    {
                        "thread_key",
                        "instruction_id",
                        "text_sha256",
                        "text_chars",
                        "state",
                        "delivery_status",
                        "created_at",
                        "updated_at",
                        "error_sha256",
                    }
                )
                or entry["state"] not in ("dispatching", "delivered", "uncertain")
                or not isinstance(entry["thread_key"], str)
                or len(entry["thread_key"]) != 64
                or not isinstance(entry["instruction_id"], str)
                or not isinstance(entry["text_sha256"], str)
                or len(entry["text_sha256"]) != 64
                or type(entry["text_chars"]) is not int
                or entry["text_chars"] < 0
            ):
                raise ValueError("Slack relay event state is malformed")
        return value
