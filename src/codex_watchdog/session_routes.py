"""Runtime-local exact Codex session destination bindings for Slack.

Routes are scoped to (provider, scope) so two relay configurations never share
route state.  All mutations happen in a single FileLock-protected SQLite
transaction reusing the existing ReplyTickets journal; no new daemon, table, or
schema version bump is required.
"""
from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any, Optional

from .models import sha256_text, utc_now
from .relay import RelayTarget
from .slack_mapping import valid_slack_channel_id, valid_slack_timestamp, valid_slack_user_id

_SCHEMA_VERSION = 1
_KIND_ROUTES = "session_routes"
_KIND_COMMANDS = "route_commands"


def _canonical_uuid(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        return None
    return parsed if parsed == value.lower() else None


def _valid_slack_ts(value: Any) -> bool:
    return valid_slack_timestamp(value)


class SessionRoutes:
    """Exact Codex-session destination bindings backed by an existing ReplyTickets journal."""

    def __init__(self, thread_store: Any, provider: str = "slack",
                 scope: Optional[str] = None) -> None:
        if not isinstance(provider, str) or re.fullmatch(r"[a-z][a-z0-9_-]*", provider) is None:
            raise ValueError("session_routes_provider_invalid")
        if not isinstance(scope, str) or not scope or "\0" in scope:
            raise ValueError("session_routes_scope_invalid")
        if provider == "slack" and not valid_slack_channel_id(scope):
            raise ValueError("session_routes_scope_invalid")
        self.thread_store = thread_store
        self.provider = provider
        self.scope = scope

    def _route_key(self, canonical_thread_id: str) -> str:
        return sha256_text(f"{self.provider}\0{self.scope}\0{canonical_thread_id}")

    def _command_key(self, event_key: str) -> str:
        if not isinstance(event_key, str) or not event_key:
            raise ValueError("route_apply_event_key_invalid")
        return sha256_text(f"{self.provider}\0{self.scope}\0{event_key}")

    @staticmethod
    def _schema(entry, error):
        if (not isinstance(entry, dict) or type(entry.get("schema_version")) is not int
                or entry["schema_version"] != _SCHEMA_VERSION):
            raise ValueError(error)

    def _validate_route(self, entry, canonical):
        self._schema(entry, "session_route_schema_invalid")
        if (entry.get("provider") != self.provider or entry.get("scope") != self.scope
                or entry.get("thread_id") != canonical):
            raise ValueError("session_route_identity_mismatch")
        if ("destination" not in entry or (entry["destination"] is not None
                and not valid_slack_channel_id(entry["destination"]))):
            raise ValueError("session_route_destination_invalid")
        if (not _valid_slack_ts(entry.get("last_command_ts"))
                or not isinstance(entry.get("created_at"), str) or not entry["created_at"]):
            raise ValueError("session_route_state_invalid")

    def _validate_command(self, entry):
        self._schema(entry, "route_command_schema_invalid")
        if (not isinstance(entry.get("text_hash"), str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["text_hash"]) is None
                or not valid_slack_user_id(entry.get("user_id"))
                or not valid_slack_channel_id(entry.get("channel_id"))
                or not _valid_slack_ts(entry.get("parent_ts"))
                or not _valid_slack_ts(entry.get("command_ts"))
                or _canonical_uuid(entry.get("thread_id")) != entry.get("thread_id")
                or entry.get("thread_id") is None
                or "destination" not in entry
                or (entry["destination"] is not None and not valid_slack_channel_id(entry["destination"]))
                or type(entry.get("ack_claimed")) is not bool
                or type(entry.get("stale", False)) is not bool
                or not isinstance(entry.get("created_at"), str) or not entry["created_at"]):
            raise ValueError("route_command_state_invalid")
        if (Decimal(entry["command_ts"]) <= Decimal(entry["parent_ts"])
                or (entry.get("stale", False) and not entry["ack_claimed"])):
            raise ValueError("route_command_state_invalid")

    def destination(self, thread_id: str) -> Optional[str]:
        """Return the bound destination channel ID for a Codex thread, or None."""
        canonical = _canonical_uuid(thread_id)
        if canonical is None:
            raise ValueError("session_routes_thread_id_invalid")
        route_key = self._route_key(canonical)
        journal = self.thread_store.journal
        with journal.transaction() as db:
            entry = journal.get(db, _KIND_ROUTES, route_key)
        if entry is None:
            return None
        self._validate_route(entry, canonical)
        return entry["destination"]

    def apply(
        self,
        event_key: str,
        channel_id: str,
        parent_ts: str,
        user_id: str,
        text: str,
        destination: Optional[str],
        *,
        command_ts: str,
    ) -> dict:
        """Bind or unbind a route atomically.

        Returns a dict with keys: status ('bound'/'unbound'/'duplicate'/'stale'),
        thread_id, destination, ack_pending (bool).
        Raises ValueError for unknown source, malformed inputs, or metadata collision.
        """
        if not isinstance(event_key, str) or not event_key:
            raise ValueError("route_apply_event_key_invalid")
        if not valid_slack_user_id(user_id):
            raise ValueError("route_apply_user_id_invalid")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("route_apply_text_invalid")
        if not _valid_slack_ts(command_ts):
            raise ValueError("route_apply_command_ts_invalid")
        if destination is not None:
            if (isinstance(destination, bool) or not isinstance(destination, str)
                    or not valid_slack_channel_id(destination)):
                raise ValueError("route_apply_destination_invalid")

        journal = self.thread_store.journal
        source_key = self.thread_store.thread_key(channel_id, parent_ts)
        if Decimal(command_ts) <= Decimal(parent_ts):
            raise ValueError("route_apply_command_ts_invalid")
        command_key = self._command_key(event_key)

        with journal.transaction() as db:
            entry = journal.get(db, "threads", source_key)
            if entry is None:
                raise ValueError("route_source_unknown")

            if entry.get("channel_id") != channel_id or entry.get("thread_ts") != parent_ts:
                raise ValueError("route_source_identity_mismatch")

            target = RelayTarget.from_dict(entry["target"])
            thread_id = target.thread_id
            canonical_tid = _canonical_uuid(thread_id)
            if canonical_tid is None:
                raise ValueError("route_source_thread_id_invalid")

            route_key = self._route_key(canonical_tid)
            text_hash = sha256_text(text)

            # Duplicate / collision detection runs before stale check
            existing_cmd = journal.get(db, _KIND_COMMANDS, command_key)
            if existing_cmd is not None:
                self._validate_command(existing_cmd)
                if (
                    existing_cmd.get("text_hash") != text_hash
                    or existing_cmd.get("user_id") != user_id
                    or existing_cmd.get("channel_id") != channel_id
                    or existing_cmd.get("parent_ts") != parent_ts
                    or existing_cmd.get("thread_id") != canonical_tid
                    or existing_cmd.get("destination") != destination
                    or existing_cmd.get("command_ts") != command_ts
                ):
                    raise ValueError("route_command_collision")
                ack_claimed = existing_cmd.get("ack_claimed")
                return {
                    "status": "duplicate",
                    "thread_id": canonical_tid,
                    "destination": existing_cmd.get("destination"),
                    "ack_pending": ack_claimed is False,
                }

            # Validate existing route schema before overwriting; stale check
            existing_route = journal.get(db, _KIND_ROUTES, route_key)
            if existing_route is not None:
                self._validate_route(existing_route, canonical_tid)
                last_ts = existing_route["last_command_ts"]
                if Decimal(command_ts) <= Decimal(last_ts):
                    stale_entry = {
                        "schema_version": _SCHEMA_VERSION,
                        "text_hash": text_hash,
                        "user_id": user_id,
                        "channel_id": channel_id,
                        "parent_ts": parent_ts,
                        "thread_id": canonical_tid,
                        "destination": destination,
                        "command_ts": command_ts,
                        "ack_claimed": True,
                        "stale": True,
                        "created_at": utc_now(),
                    }
                    journal.put(db, _KIND_COMMANDS, command_key, stale_entry)
                    return {
                        "status": "stale",
                        "thread_id": canonical_tid,
                        "destination": existing_route.get("destination"),
                        "ack_pending": False,
                    }

            # Close only the exact source ticket; historical closed tickets are idempotent
            db.execute(
                "UPDATE records SET active=0 WHERE namespace=? AND kind='threads' AND key=?",
                (journal.namespace, source_key),
            )

            # Upsert route for bind or tombstone for unbind (preserves last_command_ts)
            route_entry = {
                "schema_version": _SCHEMA_VERSION,
                "provider": self.provider,
                "scope": self.scope,
                "thread_id": canonical_tid,
                "destination": destination,
                "last_command_ts": command_ts,
                "created_at": utc_now(),
            }
            journal.put(db, _KIND_ROUTES, route_key, route_entry)

            # Persist command receipt (no raw text)
            cmd_entry = {
                "schema_version": _SCHEMA_VERSION,
                "text_hash": text_hash,
                "user_id": user_id,
                "channel_id": channel_id,
                "parent_ts": parent_ts,
                "thread_id": canonical_tid,
                "destination": destination,
                "command_ts": command_ts,
                "ack_claimed": False,
                "created_at": utc_now(),
            }
            journal.put(db, _KIND_COMMANDS, command_key, cmd_entry)

        return {
            "status": "unbound" if destination is None else "bound",
            "thread_id": canonical_tid,
            "destination": destination,
            "ack_pending": True,
        }

    def claim_binding_hello(self, event_key: str):
        """Atomically claim the destination hello for a valid bound command; returns once.

        Returns (RelayTarget, destination, fingerprint) on first successful claim.
        Returns None if absent, already claimed, stale, unbind, or superseded.
        No lock is held across the network call that follows.
        """
        command_key = self._command_key(event_key)
        journal = self.thread_store.journal
        with journal.transaction() as db:
            entry = journal.get(db, _KIND_COMMANDS, command_key)
            if entry is None:
                return None
            self._validate_command(entry)
            # Must be a bind (not unbind) and not stale
            if entry.get("stale", False) or entry.get("destination") is None:
                return None
            # Fail closed on malformed hello_claimed; absent is treated as False
            hello_claimed = entry.get("hello_claimed", False)
            if not isinstance(hello_claimed, bool):
                return None
            hello_status = entry.get("hello_status")
            if (hello_status is not None and hello_status not in ("claimed", "sent", "uncertain")
                    or hello_status is not None and hello_claimed is not True):
                return None
            if hello_claimed is True:
                return None

            canonical_tid = entry["thread_id"]
            destination = entry["destination"]
            command_ts = entry["command_ts"]

            # Current route must exactly match this command
            route_key = self._route_key(canonical_tid)
            route = journal.get(db, _KIND_ROUTES, route_key)
            if route is None:
                return None
            self._validate_route(route, canonical_tid)
            if (route.get("last_command_ts") != command_ts
                    or route.get("destination") != destination
                    or route.get("thread_id") != canonical_tid):
                return None

            # Immutable source mapping must agree on thread identity
            source_key = self.thread_store.thread_key(entry["channel_id"], entry["parent_ts"])
            source_entry = journal.get(db, "threads", source_key)
            if source_entry is None:
                return None
            try:
                target = RelayTarget.from_dict(source_entry["target"])
            except (ValueError, KeyError):
                return None
            if target.thread_id != canonical_tid:
                return None

            # Stable fingerprint derived from scope + event key (namespace-safe)
            fingerprint = sha256_text(f"hello\0{self.scope}\0{event_key}")

            # Claim atomically before any network call
            entry["hello_claimed"] = True
            entry["hello_status"] = "claimed"
            journal.put(db, _KIND_COMMANDS, command_key, entry)

        return target, destination, fingerprint

    def finish_binding_hello(self, event_key: str, *, status: str) -> None:
        """Record sent or uncertain for an already-claimed hello. No-ops otherwise."""
        if status not in ("sent", "uncertain"):
            raise ValueError("hello_status_invalid")
        command_key = self._command_key(event_key)
        journal = self.thread_store.journal
        with journal.transaction() as db:
            entry = journal.get(db, _KIND_COMMANDS, command_key)
            if entry is None:
                return
            self._validate_command(entry)
            if (entry.get("hello_claimed", False) is not True
                    or entry.get("hello_status") != "claimed"):
                return
            entry["hello_status"] = status
            journal.put(db, _KIND_COMMANDS, command_key, entry)

    def claim_ack(self, event_key: str) -> bool:
        """Mark acknowledgment as claimed before the external send; returns True once.

        Crash-safe: after True is returned once, subsequent calls return False so
        the ack is never resent even if the process dies before the send completes.
        """
        command_key = self._command_key(event_key)
        journal = self.thread_store.journal
        with journal.transaction() as db:
            entry = journal.get(db, _KIND_COMMANDS, command_key)
            if entry is None:
                return False
            self._validate_command(entry)
            if entry.get("ack_claimed") is not False:
                return False
            entry["ack_claimed"] = True
            journal.put(db, _KIND_COMMANDS, command_key, entry)
        return True
