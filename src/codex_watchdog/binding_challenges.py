"""Durable one-time binding challenges for exact-session Feishu/Lark and OneBot routes.

An authorized user replies ``bind``/``unbind`` to an active mapped notification.
That control surface is consumed atomically (never queued to Codex) and, for
``bind``, produces a short-lived unpredictable challenge string tied to the
exact provider/session/source/user. Sending that exact string as a new plain
message in the intended destination chat completes the bind: the observed
chat becomes the immutable destination authority. All writes for a single
call happen inside one existing-journal transaction; only the token's hash is
ever persisted.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .lark_transport import valid_id as _valid_lark_id
from .models import sha256_text, utc_now
from .onebot_transport import identifier as _onebot_identifier
from .relay import RelayTarget
from .session_routes import SessionRoutes, _KIND_ROUTES

_SCHEMA_VERSION = 1
_KIND_CHALLENGES = "bind_challenges"
_KIND_OPERATIONS = "bind_operations"
_KIND_OP_MESSAGES = "bind_operation_messages"
_KIND_COMPLETIONS = "bind_completions"

_MAX_PENDING = 32
_PREFIX = {"lark": "WD-BIND-LARK-", "onebot": "WD-BIND-ONEBOT-"}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _normalize_command(text: Any) -> Optional[str]:
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    return stripped.lower() if stripped else None


def _parent_epoch_seconds(source_entry: dict) -> float:
    try:
        value = source_entry["created_at"]
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError("bind_source_state_invalid") from None


class BindingChallenges:
    """Exact-session bind/unbind challenges reusing the provider's existing journal."""

    def __init__(self, thread_store: Any, provider: str, scope: str, *,
                 clock=time.time, lifetime: float = 300) -> None:
        if provider not in ("lark", "onebot"):
            raise ValueError("bind_provider_invalid")
        if (getattr(thread_store, "provider", None) != provider
                or getattr(thread_store, "scope", None) != scope
                or not _finite(lifetime) or not 0 < lifetime <= 300):
            raise ValueError("bind_scope_or_lifetime_invalid")
        self.thread_store = thread_store
        self.provider = provider
        self.scope = scope
        self.clock = clock
        self.lifetime = lifetime
        self.routes = SessionRoutes(thread_store, provider, scope)

    # ── provider-specific identity/address validation ──────────────────────

    def _valid_user_id(self, value: Any) -> bool:
        if self.provider == "lark":
            return _valid_lark_id(value, "ou")
        return isinstance(value, str) and _onebot_identifier(value) == value

    def _valid_message_id(self, value: Any) -> bool:
        if self.provider == "lark":
            return _valid_lark_id(value, "om")
        return isinstance(value, str) and _onebot_identifier(value, message=True) == value

    def _valid_destination(self, value: Any) -> bool:
        if self.provider == "lark":
            return _valid_lark_id(value, "oc")
        if not isinstance(value, str):
            return False
        parts = value.split(":")
        return (len(parts) == 2 and parts[0] in ("private", "group")
                and _onebot_identifier(parts[1]) == parts[1])

    def _valid_chat_id(self, value: Any) -> bool:
        if self.provider == "lark":
            return _valid_lark_id(value, "oc")
        return self._valid_destination(value)

    # ── key derivation (defense-in-depth: kinds partition storage) ─────────

    def _op_key(self, event_key: str) -> str:
        if not isinstance(event_key, str) or not event_key:
            raise ValueError("bind_event_key_invalid")
        return sha256_text(f"{self.provider}\0{self.scope}\0op\0{event_key}")

    def _msg_key(self, chat_id: str, message_id: str) -> str:
        return sha256_text(f"{self.provider}\0{self.scope}\0msg\0{chat_id}\0{message_id}")

    def _source_key(self, chat_id: str, parent_id: str) -> str:
        return sha256_text(chat_id + "\0" + parent_id)

    def _completion_key(self, message_id: str) -> str:
        return sha256_text(f"{self.provider}\0{self.scope}\0complete\0{message_id}")

    # ── schema guards: any malformed persisted record fails closed ─────────

    def _schema(self, entry: Any, error: str) -> None:
        if (not isinstance(entry, dict) or type(entry.get("schema_version")) is not int
                or entry["schema_version"] != _SCHEMA_VERSION):
            raise ValueError(error)

    def _validate_identity(self, entry, error):
        target = RelayTarget.from_dict(entry.get("target"))
        if (entry.get("thread_id") != target.thread_id
                or not self._valid_user_id(entry.get("user_id"))
                or not self._valid_chat_id(entry.get("chat_id"))
                or not self._valid_message_id(entry.get("parent_id"))):
            raise ValueError(error)

    def _validate_challenge(self, entry: dict) -> None:
        self._schema(entry, "bind_challenge_state_invalid")
        if (entry.get("provider") != self.provider or entry.get("scope") != self.scope
                or entry.get("status") not in ("pending", "cleared", "consumed")
                or type(entry.get("generation")) is not int or entry["generation"] < 1
                or not isinstance(entry.get("thread_id"), str)
                or not isinstance(entry.get("target"), dict)
                or not isinstance(entry.get("user_id"), str)
                or not isinstance(entry.get("chat_id"), str)
                or not isinstance(entry.get("parent_id"), str)
                or not _finite(entry.get("created_at"))):
            raise ValueError("bind_challenge_state_invalid")
        if entry["status"] == "pending":
            if (not isinstance(entry.get("token_hash"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", entry["token_hash"]) is None
                    or not _finite(entry.get("expires_at"))):
                raise ValueError("bind_challenge_state_invalid")
        elif entry.get("token_hash") is not None or entry.get("expires_at") is not None:
            raise ValueError("bind_challenge_state_invalid")
        self._validate_identity(entry, "bind_challenge_state_invalid")
        if entry["status"] == "pending" and not 0 < entry["expires_at"] - entry["created_at"] <= 300:
            raise ValueError("bind_challenge_state_invalid")

    def _validate_operation(self, entry: dict) -> None:
        self._schema(entry, "bind_operation_state_invalid")
        if (entry.get("provider") != self.provider or entry.get("scope") != self.scope
                or entry.get("op") not in ("bind", "unbind")
                or entry.get("status") not in ("pending", "unbound")
                or not isinstance(entry.get("thread_id"), str)
                or not isinstance(entry.get("target"), dict)
                or not isinstance(entry.get("fingerprint"), str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["fingerprint"]) is None
                or not isinstance(entry.get("op_key"), str) or not entry["op_key"]
                or not _finite(entry.get("created_at"))
                or not isinstance(entry.get("recorded_at"), str) or not entry["recorded_at"]):
            raise ValueError("bind_operation_state_invalid")
        if RelayTarget.from_dict(entry["target"]).thread_id != entry["thread_id"]:
            raise ValueError("bind_operation_state_invalid")

    def _validate_op_message(self, entry: dict) -> None:
        if (not isinstance(entry, dict) or not isinstance(entry.get("fingerprint"), str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["fingerprint"]) is None
                or not isinstance(entry.get("op_key"), str) or not entry["op_key"]):
            raise ValueError("bind_message_state_invalid")

    def _validate_completion(self, entry: dict) -> None:
        self._schema(entry, "bind_completion_state_invalid")
        hello_status = entry.get("hello_status")
        if (entry.get("provider") != self.provider or entry.get("scope") != self.scope
                or entry.get("status") != "bound"
                or not isinstance(entry.get("thread_id"), str)
                or not isinstance(entry.get("target"), dict)
                or not self._valid_destination(entry.get("destination"))
                or not isinstance(entry.get("user_id"), str)
                or not isinstance(entry.get("chat_id"), str)
                or not isinstance(entry.get("parent_id"), str)
                or not isinstance(entry.get("fingerprint"), str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["fingerprint"]) is None
                or type(entry.get("hello_claimed")) is not bool
                or (hello_status is not None and hello_status not in ("claimed", "sent", "uncertain"))
                or (hello_status is not None and entry.get("hello_claimed") is not True)
                or not isinstance(entry.get("created_at"), str) or not entry["created_at"]):
            raise ValueError("bind_completion_state_invalid")
        self._validate_identity(entry, "bind_completion_state_invalid")
        if type(entry.get("generation")) is not int or entry["generation"] < 1:
            raise ValueError("bind_completion_state_invalid")

    # ── fingerprints for immutable duplicate/collision comparison ──────────

    def _op_fingerprint(self, op, canonical_tid, target, chat_id, parent_id, message_id,
                        user_id, text_hash, created_at) -> str:
        payload = {"op": op, "provider": self.provider, "scope": self.scope, "thread_id": canonical_tid,
                   "target": target.to_dict(), "chat_id": chat_id, "parent_id": parent_id,
                   "message_id": message_id, "user_id": user_id, "text_hash": text_hash,
                   "created_at": created_at}
        return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def _completion_fingerprint(self, destination, user_id, text_hash, created_at, message_id) -> str:
        payload = {"provider": self.provider, "scope": self.scope, "destination": destination,
                   "user_id": user_id, "text_hash": text_hash, "created_at": created_at,
                   "message_id": message_id}
        return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def _count_pending(self, db, now: float) -> int:
        journal = self.thread_store.journal
        rows = db.execute("SELECT value FROM records WHERE namespace=? AND kind=?",
                          (journal.namespace, _KIND_CHALLENGES)).fetchall()
        count = 0
        for (raw,) in rows:
            entry = json.loads(raw)
            self._validate_challenge(entry)
            if entry["status"] == "pending" and entry["expires_at"] > now:
                count += 1
        return count

    def _duplicate_result(self, op_entry: dict, target: RelayTarget) -> dict:
        return {"status": "duplicate", "target": target, "operation_key": op_entry["op_key"]}

    # ── begin / unbind: consume the active notification, admit once ────────

    def _admit(self, op: str, event_key: str, chat_id: str, parent_id: str, user_id: str, text: str,
               *, message_id: str, created_at: float) -> dict:
        if not isinstance(event_key, str) or not event_key:
            raise ValueError("bind_event_key_invalid")
        expected = "bind" if op == "bind" else "unbind"
        if _normalize_command(text) != expected:
            raise ValueError("bind_text_invalid")
        if not self._valid_user_id(user_id):
            raise ValueError("bind_user_id_invalid")
        if not self._valid_message_id(message_id):
            raise ValueError("bind_message_id_invalid")
        if not self._valid_chat_id(chat_id):
            raise ValueError("bind_chat_id_invalid")
        if not self._valid_message_id(parent_id):
            raise ValueError("bind_parent_id_invalid")
        if not _finite(created_at):
            raise ValueError("bind_created_at_invalid")

        text_hash = sha256_text(expected)
        source_key = self._source_key(chat_id, parent_id)
        op_key = self._op_key(event_key)
        msg_key = self._msg_key(chat_id, message_id)

        journal = self.thread_store.journal
        with journal.transaction() as db:
            source_entry = journal.get(db, "threads", source_key)
            if source_entry is None:
                raise ValueError("bind_source_unknown")
            if source_entry.get("chat_id") != chat_id or source_entry.get("message_id") != parent_id:
                raise ValueError("bind_source_identity_mismatch")
            target = RelayTarget.from_dict(source_entry["target"])
            canonical_tid = target.thread_id

            parent_time = _parent_epoch_seconds(source_entry)
            if self.provider == "onebot":
                parent_time = math.floor(parent_time)  # Provider events have whole-second precision.
            if created_at < parent_time:
                raise ValueError("bind_created_at_invalid")

            fingerprint = self._op_fingerprint(op, canonical_tid, target, chat_id, parent_id,
                                               message_id, user_id, text_hash, created_at)

            existing_op = journal.get(db, _KIND_OPERATIONS, op_key)
            if existing_op is not None:
                self._validate_operation(existing_op)
                if existing_op["fingerprint"] != fingerprint:
                    raise ValueError("bind_operation_collision")
                return self._duplicate_result(existing_op, target)

            existing_msg = journal.get(db, _KIND_OP_MESSAGES, msg_key)
            if existing_msg is not None:
                self._validate_op_message(existing_msg)
                if existing_msg["fingerprint"] != fingerprint:
                    raise ValueError("bind_message_collision")
                ref = journal.get(db, _KIND_OPERATIONS, existing_msg["op_key"])
                self._validate_operation(ref)
                return self._duplicate_result(ref, target)

            if not db.execute("SELECT 1 FROM records WHERE namespace=? AND kind='threads' "
                              "AND key=? AND active=1", (journal.namespace, source_key)).fetchone():
                raise ValueError("bind_source_closed")

            now = self.clock()
            session_key = self.routes._route_key(canonical_tid)
            route = journal.get(db, _KIND_ROUTES, session_key)
            if route is not None:
                self.routes._validate_route(route, canonical_tid)
            if op == "bind" and self._count_pending(db, now) >= _MAX_PENDING:
                raise ValueError("bind_pending_limit_reached")

            if not journal.claim(db, source_key):
                raise ValueError("bind_source_closed")

            session_key = self.routes._route_key(canonical_tid)
            existing_challenge = journal.get(db, _KIND_CHALLENGES, session_key)
            if existing_challenge is not None:
                self._validate_challenge(existing_challenge)
            generation = (existing_challenge["generation"] + 1) if existing_challenge else 1

            if op == "bind":
                token = _PREFIX[self.provider] + secrets.token_urlsafe(24)
                token_hash = sha256_text(token)
                challenge_start = math.floor(now) if self.provider == "onebot" else now
                expires_at = challenge_start + self.lifetime
                journal.put(db, _KIND_CHALLENGES, session_key, dict(
                    schema_version=_SCHEMA_VERSION, provider=self.provider, scope=self.scope,
                    thread_id=canonical_tid, target=target.to_dict(), generation=generation,
                    status="pending", token_hash=token_hash, user_id=user_id, chat_id=chat_id,
                    parent_id=parent_id, created_at=challenge_start, expires_at=expires_at))
                result = {"status": "pending", "target": target, "operation_key": op_key,
                          "challenge": token, "expires_at": expires_at}
            else:
                journal.put(db, _KIND_CHALLENGES, session_key, dict(
                    schema_version=_SCHEMA_VERSION, provider=self.provider, scope=self.scope,
                    thread_id=canonical_tid, target=target.to_dict(), generation=generation,
                    status="cleared", token_hash=None, user_id=user_id, chat_id=chat_id,
                    parent_id=parent_id, created_at=now, expires_at=None))
                route_key = self.routes._route_key(canonical_tid)
                route_entry = dict(schema_version=_SCHEMA_VERSION, provider=self.provider,
                    scope=self.scope, thread_id=canonical_tid, destination=None,
                    last_command_ts=str(int(created_at * 1000)), created_at=utc_now())
                self.routes._validate_route(route_entry, canonical_tid)
                journal.put(db, _KIND_ROUTES, route_key, route_entry)
                result = {"status": "unbound", "target": target, "operation_key": op_key, "destination": None}

            journal.put(db, _KIND_OPERATIONS, op_key, dict(
                schema_version=_SCHEMA_VERSION, provider=self.provider, scope=self.scope, op=op,
                thread_id=canonical_tid, target=target.to_dict(), fingerprint=fingerprint,
                status=result["status"], op_key=op_key, created_at=created_at, recorded_at=utc_now()))
            journal.put(db, _KIND_OP_MESSAGES, msg_key, dict(fingerprint=fingerprint, op_key=op_key))

        return result

    def begin(self, event_key: str, chat_id: str, parent_id: str, user_id: str, text: str,
              *, message_id: str, created_at: float) -> dict:
        return self._admit("bind", event_key, chat_id, parent_id, user_id, text,
                           message_id=message_id, created_at=created_at)

    def unbind(self, event_key: str, chat_id: str, parent_id: str, user_id: str, text: str,
               *, message_id: str, created_at: float) -> dict:
        return self._admit("unbind", event_key, chat_id, parent_id, user_id, text,
                           message_id=message_id, created_at=created_at)

    # ── pending / lookup: read-only, no raw token ever returned ────────────

    def _current_challenges(self, db):
        journal = self.thread_store.journal
        rows = db.execute("SELECT key, value FROM records WHERE namespace=? AND kind=?",
                          (journal.namespace, _KIND_CHALLENGES)).fetchall()
        for key, raw in rows:
            entry = json.loads(raw)
            self._validate_challenge(entry)
            if key != self.routes._route_key(entry["thread_id"]):
                raise ValueError("bind_challenge_identity_mismatch")
            yield key, entry

    def pending(self) -> list:
        now = self.clock()
        journal = self.thread_store.journal
        result = []
        with journal.transaction() as db:
            for key, entry in self._current_challenges(db):
                if entry["status"] != "pending" or entry["expires_at"] <= now:
                    continue
                result.append({"key": key, "target": entry["target"], "user_id": entry["user_id"],
                               "created_at": entry["created_at"], "expires_at": entry["expires_at"]})
        return result

    def lookup(self, text: str) -> Optional[dict]:
        if not isinstance(text, str):
            return None
        token = text
        if not token or token != token.strip():
            return None
        token_hash = sha256_text(token)
        now = self.clock()
        journal = self.thread_store.journal
        with journal.transaction() as db:
            for key, entry in self._current_challenges(db):
                if entry.get("token_hash") != token_hash:
                    continue
                if entry["status"] != "pending" or entry["expires_at"] <= now:
                    return None
                return {"key": key, "target": entry["target"], "user_id": entry["user_id"],
                        "created_at": entry["created_at"], "expires_at": entry["expires_at"]}
        return None

    # ── complete: consume the exact challenge, persist the route atomically ─

    def complete(self, text: str, user_id: str, destination: str, *,
                 message_id: str, created_at: float) -> dict:
        if not isinstance(text, str):
            raise ValueError("bind_confirm_text_invalid")
        token = text
        if not token or token != token.strip():
            raise ValueError("bind_confirm_text_invalid")
        if not self._valid_user_id(user_id):
            raise ValueError("bind_user_id_invalid")
        if not self._valid_message_id(message_id):
            raise ValueError("bind_message_id_invalid")
        if not self._valid_destination(destination):
            raise ValueError("bind_destination_invalid")
        if not _finite(created_at):
            raise ValueError("bind_created_at_invalid")

        text_hash = sha256_text(token)
        token_hash = text_hash
        completion_key = self._completion_key(message_id)
        now = self.clock()

        journal = self.thread_store.journal
        with journal.transaction() as db:
            existing = journal.get(db, _KIND_COMPLETIONS, completion_key)
            if existing is not None:
                self._validate_completion(existing)
                fingerprint = self._completion_fingerprint(destination, user_id, text_hash,
                                                           created_at, message_id)
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("bind_completion_collision")
                return {"status": "duplicate", "target": RelayTarget.from_dict(existing["target"]),
                        "operation_key": completion_key, "destination": existing["destination"]}

            session_key = match_entry = None
            for key, entry in self._current_challenges(db):
                if entry.get("token_hash") == token_hash:
                    session_key, match_entry = key, entry
                    break
            if match_entry is None:
                raise ValueError("bind_challenge_not_found")
            if match_entry["status"] != "pending":
                raise ValueError("bind_challenge_not_pending")
            if match_entry["user_id"] != user_id:
                raise ValueError("bind_challenge_wrong_user")
            if match_entry["expires_at"] <= now:
                raise ValueError("bind_challenge_expired")
            if (created_at < match_entry["created_at"] or created_at > match_entry["expires_at"]
                    or created_at > now + 2):
                raise ValueError("bind_created_at_invalid")

            canonical_tid = match_entry["thread_id"]
            target = RelayTarget.from_dict(match_entry["target"])
            source = journal.get(db, "threads", self._source_key(match_entry["chat_id"], match_entry["parent_id"]))
            if (source is None or source.get("chat_id") != match_entry["chat_id"]
                    or source.get("message_id") != match_entry["parent_id"]
                    or source.get("target") != target.to_dict()):
                raise ValueError("bind_source_identity_mismatch")
            route = journal.get(db, _KIND_ROUTES, self.routes._route_key(canonical_tid))
            if route is not None:
                self.routes._validate_route(route, canonical_tid)

            # Consume before any other effect: this exact token can never bind again.
            match_entry.update(status="consumed", token_hash=None, expires_at=None)
            journal.put(db, _KIND_CHALLENGES, session_key, match_entry)

            route_key = self.routes._route_key(canonical_tid)
            route_entry = dict(schema_version=_SCHEMA_VERSION, provider=self.provider, scope=self.scope,
                thread_id=canonical_tid, destination=destination,
                last_command_ts=str(int(created_at * 1000)), created_at=utc_now())
            self.routes._validate_route(route_entry, canonical_tid)
            journal.put(db, _KIND_ROUTES, route_key, route_entry)

            fingerprint = self._completion_fingerprint(destination, user_id, text_hash, created_at, message_id)
            journal.put(db, _KIND_COMPLETIONS, completion_key, dict(
                schema_version=_SCHEMA_VERSION, provider=self.provider, scope=self.scope,
                thread_id=canonical_tid, target=target.to_dict(), destination=destination,
                user_id=user_id, chat_id=match_entry["chat_id"], parent_id=match_entry["parent_id"],
                generation=match_entry["generation"],
                fingerprint=fingerprint, status="bound", hello_claimed=False, hello_status=None,
                created_at=utc_now()))

        return {"status": "bound", "target": target, "operation_key": completion_key, "destination": destination}

    # ── post-bind destination hello: claim once before any external send ───

    def claim_hello(self, operation_key: str):
        journal = self.thread_store.journal
        with journal.transaction() as db:
            entry = journal.get(db, _KIND_COMPLETIONS, operation_key)
            if entry is None:
                return None
            self._validate_completion(entry)
            hello_claimed = entry.get("hello_claimed", False)
            if not isinstance(hello_claimed, bool) or hello_claimed is True or entry.get("hello_status") is not None:
                return None

            canonical_tid = entry["thread_id"]
            current = journal.get(db, _KIND_CHALLENGES, self.routes._route_key(canonical_tid))
            if current is None:
                return None
            self._validate_challenge(current)
            if (current["status"] != "consumed" or current["generation"] != entry["generation"]
                    or current["target"] != entry["target"]):
                return None
            route = journal.get(db, _KIND_ROUTES, self.routes._route_key(canonical_tid))
            if route is None:
                return None
            self.routes._validate_route(route, canonical_tid)
            if route.get("destination") != entry["destination"] or route.get("thread_id") != canonical_tid:
                return None

            source_entry = journal.get(db, "threads", self._source_key(entry["chat_id"], entry["parent_id"]))
            if source_entry is None:
                return None
            try:
                target = RelayTarget.from_dict(source_entry["target"])
            except (ValueError, KeyError):
                return None
            if target.to_dict() != entry["target"]:
                return None

            fingerprint = sha256_text(f"hello\0{self.provider}\0{self.scope}\0{operation_key}")
            entry["hello_claimed"] = True
            entry["hello_status"] = "claimed"
            journal.put(db, _KIND_COMPLETIONS, operation_key, entry)

        return target, entry["destination"], fingerprint

    def finish_hello(self, operation_key: str, status: str) -> None:
        if status not in ("sent", "uncertain"):
            raise ValueError("bind_hello_status_invalid")
        journal = self.thread_store.journal
        with journal.transaction() as db:
            entry = journal.get(db, _KIND_COMPLETIONS, operation_key)
            if entry is None:
                return
            self._validate_completion(entry)
            if entry.get("hello_claimed") is not True or entry.get("hello_status") != "claimed":
                return
            entry["hello_status"] = status
            journal.put(db, _KIND_COMPLETIONS, operation_key, entry)
