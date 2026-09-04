from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Dict, Optional, Tuple
import uuid

from .models import sha256_text, utc_now
from .storage import FileLock, InstructionStore


SLACK_RELAY_STATE_SCHEMA_VERSION = 1

_SLACK_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{8,}$")
_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")
_SLACK_TIMESTAMP = re.compile(r"^[0-9]{10,}\.[0-9]+$")
_REMOTE_AUTHORITY = re.compile(
    r"^ssh-remote\+[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$"
)
_STORAGE_KEY = re.compile(r"^[0-9a-f]{32}$")


def valid_slack_channel_id(value: Any) -> bool:
    return isinstance(value, str) and _SLACK_CHANNEL_ID.fullmatch(value) is not None


def valid_slack_user_id(value: Any) -> bool:
    return isinstance(value, str) and _SLACK_USER_ID.fullmatch(value) is not None


def valid_slack_timestamp(value: Any) -> bool:
    return isinstance(value, str) and _SLACK_TIMESTAMP.fullmatch(value) is not None


def _canonical_uuid(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        return None
    return parsed if parsed == value.lower() else None


@dataclass(frozen=True)
class SlackRelayTarget:
    workspace_id: str
    thread_id: str
    execution_locality: str
    remote_authority: Optional[str] = None
    remote_repo_path: Optional[str] = None
    remote_storage_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("relay workspace id must be a non-empty string")
        if _canonical_uuid(self.thread_id) is None:
            raise ValueError("relay thread id must be a canonical UUID")
        if self.execution_locality not in ("process_local", "remote_ssh"):
            raise ValueError("relay execution locality is invalid")
        remote_values = (
            self.remote_authority,
            self.remote_repo_path,
            self.remote_storage_key,
        )
        if self.execution_locality == "process_local":
            if any(value is not None for value in remote_values):
                raise ValueError("local relay target cannot contain remote routing")
            return
        if (
            not isinstance(self.remote_authority, str)
            or _REMOTE_AUTHORITY.fullmatch(self.remote_authority) is None
            or not isinstance(self.remote_repo_path, str)
            or not self.remote_repo_path.startswith("/")
            or not isinstance(self.remote_storage_key, str)
            or _STORAGE_KEY.fullmatch(self.remote_storage_key) is None
        ):
            raise ValueError("remote relay target routing is invalid")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "thread_id": self.thread_id,
            "execution_locality": self.execution_locality,
            "remote_authority": self.remote_authority,
            "remote_repo_path": self.remote_repo_path,
            "remote_storage_key": self.remote_storage_key,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SlackRelayTarget":
        if not isinstance(value, dict) or frozenset(value) != frozenset(
            {
                "workspace_id",
                "thread_id",
                "execution_locality",
                "remote_authority",
                "remote_repo_path",
                "remote_storage_key",
            }
        ):
            raise ValueError("Slack relay target is malformed")
        return cls(**value)


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

    def record_thread(
        self,
        channel_id: str,
        thread_ts: str,
        target: SlackRelayTarget,
        event_fingerprint: str,
    ) -> None:
        key = self.thread_key(channel_id, thread_ts)
        if not isinstance(event_fingerprint, str) or len(event_fingerprint) != 64:
            raise ValueError("notification event fingerprint is invalid")
        with FileLock(self.lock_path):
            state = self._read_state()
            state["threads"][key] = {
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "target": target.to_dict(),
                "event_fingerprint": event_fingerprint,
                "created_at": utc_now(),
            }
            InstructionStore._atomic_json(self.path, state)

    def lookup_thread(
        self, channel_id: str, thread_ts: str
    ) -> Optional[SlackThreadMapping]:
        key = self.thread_key(channel_id, thread_ts)
        with FileLock(self.lock_path):
            entry = self._read_state()["threads"].get(key)
        if entry is None:
            return None
        return SlackThreadMapping(
            channel_id=entry["channel_id"],
            thread_ts=entry["thread_ts"],
            target=SlackRelayTarget.from_dict(entry["target"]),
        )

    def has_notification_mapping(self, event_fingerprint: str) -> bool:
        if (
            not isinstance(event_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", event_fingerprint) is None
        ):
            raise ValueError("notification event fingerprint is invalid")
        with FileLock(self.lock_path):
            threads = self._read_state()["threads"].values()
            return any(
                entry["event_fingerprint"] == event_fingerprint for entry in threads
            )

    def claim_reply(
        self,
        *,
        event_key: str,
        channel_id: str,
        thread_ts: str,
        instruction_id: str,
        text: str,
    ) -> Tuple[bool, Optional[str]]:
        event_digest = sha256_text(event_key)
        thread_key = self.thread_key(channel_id, thread_ts)
        with FileLock(self.lock_path):
            state = self._read_state()
            previous = state["events"].get(event_digest)
            if previous is not None:
                return False, previous.get("delivery_status")
            state["events"][event_digest] = {
                "thread_key": thread_key,
                "instruction_id": instruction_id,
                "text_sha256": sha256_text(text),
                "text_chars": len(text),
                "state": "dispatching",
                "delivery_status": None,
                "created_at": utc_now(),
                "updated_at": None,
                "error_sha256": None,
            }
            InstructionStore._atomic_json(self.path, state)
        return True, None

    def finish_reply(
        self,
        event_key: str,
        *,
        state_value: str,
        delivery_status: str,
        error_sha256: Optional[str] = None,
    ) -> None:
        if state_value not in ("delivered", "uncertain"):
            raise ValueError("Slack reply state is invalid")
        event_digest = sha256_text(event_key)
        with FileLock(self.lock_path):
            state = self._read_state()
            entry = state["events"].get(event_digest)
            if entry is None:
                raise ValueError("Slack reply event was not claimed")
            entry.update(
                state=state_value,
                delivery_status=delivery_status,
                updated_at=utc_now(),
                error_sha256=error_sha256,
            )
            InstructionStore._atomic_json(self.path, state)

    def _read_state(self) -> Dict[str, Any]:
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
