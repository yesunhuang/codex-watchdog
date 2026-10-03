"""Exact-session Slack bot grants and hash-only receipts in ReplyTickets.

Provider identity verification belongs to the caller. All authorization and
logical request reservations are rechecked within the existing ticket claim.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

from .models import sha256_text, utc_now
from .relay import RelayTarget
from .slack_bot_identity import BotPrincipal
from .slack_mapping import SlackThreadStore, valid_slack_channel_id, valid_slack_user_id

_GRANTS = "slack_bot_grants"
_USERS = "slack_bot_users"
_CONTROLS = "slack_bot_controls"
_REQUESTS = "slack_bot_requests"
_OUTCOMES = {"add": ("added", "already_present"),
             "remove": ("removed", "not_present"), "access": ("access",)}


class BotRequestReused(ValueError):
    """A reserved logical UUID must not admit another physical message."""


def _digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _uuid(value: Any) -> str:
    try:
        canonical = str(uuid.UUID(value))
        if canonical != value.lower():
            raise ValueError()
        return canonical
    except (ValueError, TypeError, AttributeError):
        raise ValueError("slack_bot_uuid_invalid") from None


def _principal(value: Any) -> BotPrincipal:
    if not isinstance(value, BotPrincipal):
        raise ValueError("slack_bot_principal_invalid")
    return BotPrincipal.from_dict(value.to_dict())


def _principal_key(value: BotPrincipal) -> str:
    return json.dumps(value.to_dict(), sort_keys=True, separators=(",", ":"))


class BotSessionAccess:
    """Bot authority is separate from human ACLs and scoped to an exact session."""

    def __init__(self, store: Any, scope: str) -> None:
        if getattr(store, "provider", None) != "slack":
            raise ValueError("slack_bot_store_provider_invalid")
        if not valid_slack_channel_id(scope):
            raise ValueError("slack_bot_scope_invalid")
        self.thread_store, self.scope = store, scope

    def _grant_key(self, thread_id: str) -> str:
        return sha256_text(f"slack\0{self.scope}\0bot_grant\0{_uuid(thread_id)}")

    @staticmethod
    def _user_key(user_id: str) -> str:
        return sha256_text(f"slack\0bot_user\0{user_id}")

    def _operation_key(self, event_digest: str) -> str:
        return sha256_text(f"slack\0{self.scope}\0bot_control\0{event_digest}")

    @staticmethod
    def _request_key(principal: BotPrincipal, request_id: str) -> str:
        # No session or destination: a logical request cannot be moved to a
        # different active surface. The journal keeps transport namespaces apart.
        return sha256_text(f"slack\0bot_request\0{_principal_key(principal)}\0{request_id}")

    def _get(self, db: Any, kind: str, key: str) -> Optional[dict]:
        journal = self.thread_store.journal
        row = db.execute("SELECT value FROM records WHERE namespace=? AND kind=? AND key=?",
                         (journal.namespace, kind, key)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError):
            raise ValueError("slack_bot_record_invalid") from None
        if not isinstance(value, dict):
            raise ValueError("slack_bot_record_invalid")
        return value

    @staticmethod
    def _schema(entry: dict) -> None:
        if (type(entry.get("schema_version")) is not int or entry["schema_version"] != 1
                or entry.get("provider") != "slack"
                or not isinstance(entry.get("created_at"), str) or not entry["created_at"]):
            raise ValueError("slack_bot_record_invalid")

    @staticmethod
    def _principal_list(entry: dict) -> list:
        raw = entry.get("principals")
        if not isinstance(raw, list):
            raise ValueError("slack_bot_record_invalid")
        values = [BotPrincipal.from_dict(value) for value in raw]
        keys = [_principal_key(value) for value in values]
        if keys != sorted(set(keys)):
            raise ValueError("slack_bot_record_invalid")
        return values

    def _known(self, db: Any, user_id: str) -> bool:
        entry = self._get(db, _USERS, self._user_key(user_id))
        if entry is None:
            return False
        self._schema(entry)
        principals = self._principal_list(entry)
        if (entry.get("user_id") != user_id or not principals
                or any(p.user_id != user_id for p in principals)):
            raise ValueError("slack_bot_record_invalid")
        return True

    def known_user(self, user_id: str) -> bool:
        if not valid_slack_user_id(user_id):
            raise ValueError("slack_bot_user_invalid")
        with self.thread_store.journal.transaction() as db:
            return self._known(db, user_id)

    def _grants(self, db: Any, thread_id: str) -> tuple:
        canonical = _uuid(thread_id)
        entry = self._get(db, _GRANTS, self._grant_key(canonical))
        if entry is None:
            return None, []
        self._schema(entry)
        if entry.get("scope") != self.scope or entry.get("thread_id") != canonical:
            raise ValueError("slack_bot_grant_identity_invalid")
        return entry, self._principal_list(entry)

    def principals(self, thread_id: str) -> list:
        with self.thread_store.journal.transaction() as db:
            return self._grants(db, thread_id)[1]

    def _source(self, db: Any, source_key: str, target: RelayTarget, *, active: bool) -> Optional[dict]:
        if not _digest(source_key) or not isinstance(target, RelayTarget):
            raise ValueError("slack_bot_source_invalid")
        entry = self._get(db, "threads", source_key)
        if entry is None:
            return None
        try:
            key = SlackThreadStore.thread_key(entry.get("channel_id"), entry.get("thread_ts"))
            actual = RelayTarget.from_dict(entry.get("target"))
        except (ValueError, TypeError, KeyError):
            raise ValueError("slack_bot_source_invalid") from None
        if key != source_key or not _digest(entry.get("event_fingerprint")):
            raise ValueError("slack_bot_source_invalid")
        if actual != target:
            return None
        if active and db.execute(
                "SELECT 1 FROM records WHERE namespace=? AND kind='threads' AND key=? AND active=1",
                (self.thread_store.journal.namespace, source_key)).fetchone() is None:
            return None
        return entry

    def _human_control_collision(self, db: Any, event_key: str, message_key: str) -> None:
        # CP190 controls predate shared ordinary receipts. Reserve their event
        # and physical identities too, without modifying human authorization.
        from .session_acl import SessionACL
        from .session_routes import SessionRoutes
        acl = SessionACL(self.thread_store, "slack", self.scope)
        routes = SessionRoutes(self.thread_store, "slack", self.scope)
        for kind, key in (("acl_commands", acl._command_key(event_key)),
                          ("acl_messages", acl._msg_dedup_key(message_key)),
                          ("route_commands", routes._command_key(event_key)),
                          ("route_messages", sha256_text(message_key))):
            if db.execute("SELECT 1 FROM records WHERE namespace=? AND kind=? AND key=?",
                          (self.thread_store.journal.namespace, kind, key)).fetchone() is not None:
                raise ValueError("slack_bot_control_identity_collision")

    @staticmethod
    def _input(event_key: str, message_key: str, payload_sha256: str) -> None:
        if (not isinstance(event_key, str) or not event_key
                or not isinstance(message_key, str) or not message_key
                or not _digest(payload_sha256)):
            raise ValueError("slack_bot_input_invalid")

    def _validate_control(self, entry: dict, key: str) -> RelayTarget:
        self._schema(entry)
        operation = entry.get("operation")
        claimed, status = entry.get("confirmation_claimed"), entry.get("confirmation_status")
        if (entry.get("scope") != self.scope
                or not _digest(entry.get("event_key_digest"))
                or key != self._operation_key(entry["event_key_digest"])
                or not _digest(entry.get("message_key_digest"))
                or not _digest(entry.get("payload_sha256"))
                or not _digest(entry.get("source_key"))
                or not valid_slack_user_id(entry.get("user_id"))
                or not isinstance(operation, str) or operation not in _OUTCOMES
                or entry.get("status") not in _OUTCOMES[operation]
                or type(claimed) is not bool
                or (claimed is False and status is not None)
                or (claimed is True and status not in ("claimed", "sent", "uncertain"))):
            raise ValueError("slack_bot_control_invalid")
        principal = entry.get("principal")
        if operation == "access":
            if principal is not None:
                raise ValueError("slack_bot_control_invalid")
        else:
            BotPrincipal.from_dict(principal)
        target = RelayTarget.from_dict(entry.get("target"))
        if (entry.get("thread_id") != _uuid(target.thread_id)
                or SlackThreadStore.thread_key(entry.get("channel_id"), entry.get("thread_ts"))
                != entry["source_key"]):
            raise ValueError("slack_bot_control_invalid")
        return target

    def control(self, *, event_key: str, message_key: str, source_key: str,
                user_id: str, operation: str, principal: Optional[BotPrincipal],
                expected_target: RelayTarget, admin_ids: Any, payload_sha256: str) -> dict:
        self._input(event_key, message_key, payload_sha256)
        if (not valid_slack_user_id(user_id) or user_id not in admin_ids
                or not isinstance(operation, str) or operation not in _OUTCOMES):
            raise ValueError("slack_bot_control_unauthorized")
        if operation == "access":
            if principal is not None:
                raise ValueError("slack_bot_control_principal_invalid")
        else:
            principal = _principal(principal)
            if principal.user_id == user_id:
                raise ValueError("slack_bot_control_unauthorized")
        journal = self.thread_store.journal
        operation_key = self._operation_key(sha256_text(event_key))
        result = dict(status="unauthorized", operation_key=None,
                      target=expected_target, principals=[])
        with journal.transaction() as db:
            source = self._source(db, source_key, expected_target, active=False)
            if self._known(db, user_id):
                raise ValueError("slack_bot_control_unauthorized")
            if source is None:
                return result

        def admission(db: Any) -> bool:
            current_source = self._source(db, source_key, expected_target, active=True)
            if current_source is None or self._known(db, user_id):
                return False
            self._human_control_collision(db, event_key, message_key)
            if self._get(db, _CONTROLS, operation_key) is not None:
                raise ValueError("slack_bot_control_identity_collision")
            old, values = self._grants(db, expected_target.thread_id)
            if operation == "access":
                status = "access"
            elif operation == "add":
                status = "already_present" if principal in values else "added"
                if principal not in values:
                    values.append(principal)
            else:
                status = "removed" if principal in values else "not_present"
                values = [value for value in values if value != principal]
            values.sort(key=_principal_key)
            if status in ("added", "removed"):
                grant = dict(old or {})
                grant.update(schema_version=1, provider="slack", scope=self.scope,
                             thread_id=_uuid(expected_target.thread_id),
                             principals=[value.to_dict() for value in values],
                             created_at=grant.get("created_at", utc_now()), updated_at=utc_now())
                journal.put(db, _GRANTS, self._grant_key(expected_target.thread_id), grant)
            if principal is not None:
                known = self._known(db, principal.user_id)
                registry = self._get(db, _USERS, self._user_key(principal.user_id)) if known else {}
                registered = self._principal_list(registry) if known else []
                if principal not in registered:
                    registered.append(principal)
                    registered.sort(key=_principal_key)
                    registry.update(schema_version=1, provider="slack", user_id=principal.user_id,
                                    principals=[value.to_dict() for value in registered],
                                    created_at=registry.get("created_at", utc_now()))
                    journal.put(db, _USERS, self._user_key(principal.user_id), registry)
            receipt = dict(schema_version=1, provider="slack", scope=self.scope,
                           thread_id=_uuid(expected_target.thread_id), target=expected_target.to_dict(),
                           source_key=source_key, channel_id=current_source["channel_id"],
                           thread_ts=current_source["thread_ts"], user_id=user_id,
                           principal=principal.to_dict() if principal is not None else None,
                           operation=operation, status=status, event_key_digest=sha256_text(event_key),
                           message_key_digest=sha256_text(message_key), payload_sha256=payload_sha256,
                           created_at=utc_now(), confirmation_claimed=False, confirmation_status=None)
            journal.put(db, _CONTROLS, operation_key, receipt)
            result.update(status=status, operation_key=operation_key, principals=values)
            return True

        claimed, state = self.thread_store.claim_reply(
            event_key=event_key, channel_id=source["channel_id"], thread_ts=source["thread_ts"],
            instruction_id="slack-bot-control-" + operation_key, text=payload_sha256,
            admission=admission, message_key=message_key, payload_sha256=payload_sha256)
        if claimed:
            return result
        if state in ("ticket_closed", "unauthorized"):
            result["status"] = "closed" if state == "ticket_closed" else "unauthorized"
            return result
        with journal.transaction() as db:
            event_digest = sha256_text(event_key)
            if self._get(db, "events", event_digest) is None:
                message = self._get(db, "messages", sha256_text(message_key))
                event_digest = message["event_key"] if message is not None else event_digest
            key = self._operation_key(event_digest)
            receipt = self._get(db, _CONTROLS, key)
            if receipt is not None:
                actual = self._validate_control(receipt, key)
                if (actual != expected_target or receipt["source_key"] != source_key
                        or receipt["user_id"] != user_id or receipt["operation"] != operation
                        or receipt["principal"] != (principal.to_dict() if principal is not None else None)
                        or receipt["message_key_digest"] != sha256_text(message_key)
                        or receipt["payload_sha256"] != payload_sha256):
                    raise ValueError("slack_bot_control_identity_collision")
                result.update(operation_key=key, principals=self._grants(db, expected_target.thread_id)[1])
        result["status"] = "duplicate"
        return result

    def claim_confirmation(self, operation_key: str) -> Optional[dict]:
        if not _digest(operation_key):
            raise ValueError("slack_bot_operation_key_invalid")
        journal = self.thread_store.journal
        with journal.transaction() as db:
            receipt = self._get(db, _CONTROLS, operation_key)
            if receipt is None:
                return None
            target = self._validate_control(receipt, operation_key)
            if receipt["confirmation_claimed"]:
                return None
            source = self._source(db, receipt["source_key"], target, active=False)
            if source is None:
                return None
            values = self._grants(db, target.thread_id)[1]
            receipt.update(confirmation_claimed=True, confirmation_status="claimed")
            journal.put(db, _CONTROLS, operation_key, receipt)
            return dict(target=target, source_key=receipt["source_key"], source_entry=source,
                        address=source["channel_id"] + "\0" + source["thread_ts"],
                        principals=values, status=receipt["status"],
                        fingerprint=sha256_text("slack_bot_confirmation\0" + operation_key))

    def finish_confirmation(self, operation_key: str, status: str) -> None:
        if not _digest(operation_key) or status not in ("sent", "uncertain"):
            raise ValueError("slack_bot_confirmation_invalid")
        journal = self.thread_store.journal
        with journal.transaction() as db:
            receipt = self._get(db, _CONTROLS, operation_key)
            if receipt is None:
                return
            self._validate_control(receipt, operation_key)
            if receipt["confirmation_status"] == "claimed":
                receipt["confirmation_status"] = status
                journal.put(db, _CONTROLS, operation_key, receipt)

    def make_admission(self, source_key: str, principal: BotPrincipal,
                       expected_target: RelayTarget, *, request_id: str, event_key: str,
                       message_key: str, payload_sha256: str):
        principal = _principal(principal)
        self._input(event_key, message_key, payload_sha256)
        if _uuid(request_id) != request_id:
            raise ValueError("slack_bot_request_id_invalid")
        request_key = self._request_key(principal, request_id)
        journal = self.thread_store.journal

        def admission(db: Any) -> bool:
            source = self._source(db, source_key, expected_target, active=True)
            if source is None:
                return False
            if principal not in self._grants(db, expected_target.thread_id)[1]:
                return False
            if not self._known(db, principal.user_id):
                return False
            self._human_control_collision(db, event_key, message_key)
            existing = self._get(db, _REQUESTS, request_key)
            if existing is not None:
                # Any prior logical reservation is final, including an uncertain
                # dispatch. Ordinary event/message retries were handled first.
                self._schema(existing)
                raise BotRequestReused("slack_bot_request_reused")
            journal.put(db, _REQUESTS, request_key, dict(
                schema_version=1, provider="slack", scope=self.scope,
                request_id=request_id, principal=principal.to_dict(),
                target=expected_target.to_dict(), source_key=source_key,
                channel_id=source["channel_id"], thread_ts=source["thread_ts"],
                event_key_digest=sha256_text(event_key), message_key_digest=sha256_text(message_key),
                payload_sha256=payload_sha256, created_at=utc_now()))
            return True

        return admission
