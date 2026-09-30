"""Bounded exact-session delegation storage backed by the existing ReplyTickets journal."""
from __future__ import annotations

import re
import uuid
from typing import Any, List, Optional

from .models import sha256_text, utc_now
from .relay import RelayTarget

_SCHEMA_VERSION = 1
_KIND_ACL = "session_acl"
_KIND_ACL_COMMANDS = "acl_commands"
_KIND_ACL_MESSAGES = "acl_messages"
_SUPPORTED_PROVIDERS = frozenset({"slack", "lark", "onebot"})
_STATUSES_BY_OPERATION = {
    "access": ("access",),
    "add": ("already_admin", "already_present", "added"),
    "remove": ("already_admin", "not_present", "removed"),
}


def _canonical_uuid(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        return None
    return parsed if parsed == value.lower() else None


def _digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _valid_user_id(provider: str, value: Any) -> bool:
    if provider == "slack":
        from .slack_mapping import valid_slack_user_id
        return valid_slack_user_id(value)
    if provider == "lark":
        from .lark_transport import valid_id
        return valid_id(value, "ou")
    if provider == "onebot":
        from .onebot_transport import identifier
        return isinstance(value, str) and identifier(value) is not None
    return False


def _recompute_source_key(provider: str, entry: dict) -> Optional[str]:
    try:
        if provider == "slack":
            from .slack_mapping import valid_slack_channel_id, valid_slack_timestamp
            ch = entry.get("channel_id")
            ts = entry.get("thread_ts")
            if not valid_slack_channel_id(ch) or not valid_slack_timestamp(ts):
                return None
            return sha256_text(ch + "\0" + ts)
        elif provider == "lark":
            from .lark_mapping import LarkThreadStore as _LTS
            return _LTS._address(entry.get("chat_id"), entry.get("message_id"))
        else:
            from .onebot_relay import OneBotThreadStore as _OTS
            return _OTS._address(entry.get("chat_id"), entry.get("message_id"))
    except (ValueError, RuntimeError, KeyError, TypeError):
        return None


class SessionACL:
    """Versioned session-level delegate list backed by an existing ReplyTickets journal."""

    def __init__(self, thread_store: Any, provider: str = "slack",
                 scope: Optional[str] = None) -> None:
        if provider not in _SUPPORTED_PROVIDERS:
            raise ValueError("session_acl_provider_invalid")
        if not isinstance(scope, str) or not scope or "\0" in scope:
            raise ValueError("session_acl_scope_invalid")
        from .slack_mapping import valid_slack_channel_id
        if provider == "slack" and not valid_slack_channel_id(scope):
            raise ValueError("session_acl_scope_invalid")
        if provider in ("lark", "onebot") and re.fullmatch(r"[0-9a-f]{64}", scope) is None:
            raise ValueError("session_acl_scope_invalid")
        if hasattr(thread_store, 'provider') and thread_store.provider != provider:
            raise ValueError("session_acl_store_provider_mismatch")
        if hasattr(thread_store, 'scope') and thread_store.scope != scope:
            raise ValueError("session_acl_store_scope_mismatch")
        self.thread_store = thread_store
        self.provider = provider
        self.scope = scope

    def _acl_key(self, canonical_thread_id: str) -> str:
        return sha256_text(f"{self.provider}\0{self.scope}\0acl\0{canonical_thread_id}")

    def _command_key(self, event_key: str) -> str:
        return sha256_text(f"{self.provider}\0{self.scope}\0acl_cmd\0{event_key}")

    def _msg_dedup_key(self, message_key: str) -> str:
        return sha256_text(f"{self.provider}\0{self.scope}\0acl_msg\0{message_key}")

    @staticmethod
    def _schema(entry: Any, error: str) -> None:
        if (not isinstance(entry, dict) or type(entry.get("schema_version")) is not int
                or entry["schema_version"] != _SCHEMA_VERSION):
            raise ValueError(error)

    @staticmethod
    def _strict_get_own(db: Any, journal: Any, kind: str, key: str,
                        null_error: str) -> "Optional[dict]":
        """Distinguish absent (None) from stored null/non-dict (raises)."""
        exists = db.execute(
            "SELECT 1 FROM records WHERE namespace=? AND kind=? AND key=?",
            (journal.namespace, kind, key)).fetchone()
        if exists is None:
            return None
        value = journal.get(db, kind, key)
        if not isinstance(value, dict):
            raise ValueError(null_error)
        return value

    def _validate_acl(self, entry: dict, canonical_thread_id: str) -> None:
        self._schema(entry, "session_acl_schema_invalid")
        if (entry.get("provider") != self.provider or entry.get("scope") != self.scope
                or entry.get("thread_id") != canonical_thread_id):
            raise ValueError("session_acl_identity_mismatch")
        delegates = entry.get("delegates")
        if not isinstance(delegates, list):
            raise ValueError("session_acl_delegates_invalid")
        for d in delegates:
            if not isinstance(d, str):
                raise ValueError("session_acl_delegates_invalid")
        if delegates != sorted(delegates) or len(set(delegates)) != len(delegates):
            raise ValueError("session_acl_delegates_invalid")
        for d in delegates:
            if not _valid_user_id(self.provider, d):
                raise ValueError("session_acl_delegate_id_invalid")
        if not isinstance(entry.get("created_at"), str) or not entry["created_at"]:
            raise ValueError("session_acl_state_invalid")

    def _validate_command(self, entry: dict) -> None:
        self._schema(entry, "acl_command_schema_invalid")
        cc = entry.get("confirmation_claimed")
        cs = entry.get("confirmation_status")
        if (not _digest(entry.get("event_key_digest"))
                or not _digest(entry.get("message_key_digest"))
                or not _digest(entry.get("source_key"))
                or not _valid_user_id(self.provider, entry.get("user_id"))
                or entry.get("operation") not in ("add", "remove", "access")
                or not _digest(entry.get("payload_sha256"))
                or _canonical_uuid(entry.get("thread_id")) != entry.get("thread_id")
                or entry.get("thread_id") is None
                or not isinstance(entry.get("target"), dict)
                or not isinstance(entry.get("status"), str)
                or type(cc) is not bool
                or not isinstance(entry.get("created_at"), str) or not entry["created_at"]
                or (cs is not None and cs not in ("sent", "uncertain", "claimed"))
                or (cs is not None and cc is not True)
                or (cc is True and cs is None)
                or entry.get("provider") not in _SUPPORTED_PROVIDERS
                or entry.get("provider") != self.provider
                or not isinstance(entry.get("scope"), str) or not entry.get("scope")
                or entry.get("scope") != self.scope):
            raise ValueError("acl_command_state_invalid")
        try:
            stored_target = RelayTarget.from_dict(entry.get("target", {}))
        except (ValueError, KeyError, TypeError):
            raise ValueError("acl_command_state_invalid")
        if _canonical_uuid(stored_target.thread_id) != entry.get("thread_id"):
            raise ValueError("acl_command_state_invalid")
        operation = entry.get("operation")
        if entry["status"] not in _STATUSES_BY_OPERATION[operation]:
            raise ValueError("acl_command_state_invalid")
        delegate_id = entry.get("delegate_id")
        if operation == "access" and delegate_id is not None:
            raise ValueError("acl_command_state_invalid")
        if operation in ("add", "remove") and not _valid_user_id(self.provider, delegate_id):
            raise ValueError("acl_command_state_invalid")

    def _validate_message(self, entry: dict) -> None:
        self._schema(entry, "acl_message_schema_invalid")
        if (not _digest(entry.get("event_key_digest"))
                or not _digest(entry.get("payload_sha256"))
                or not _digest(entry.get("command_key"))):
            raise ValueError("acl_message_state_invalid")

    def admit(self, db: Any, source_key: str, user_id: str, admin_ids: Any,
              expected_target: RelayTarget, *, event_key: Optional[str] = None,
              message_key: Optional[str] = None) -> bool:
        """Check ACL admission. Called WITHIN an existing journal transaction."""
        journal = self.thread_store.journal
        entry = journal.get(db, "threads", source_key)
        if entry is None:
            return False
        recomputed = _recompute_source_key(self.provider, entry)
        if recomputed is None or recomputed != source_key:
            raise ValueError("session_acl_source_identity_mismatch")
        try:
            stored_target = RelayTarget.from_dict(entry["target"])
        except (ValueError, KeyError):
            raise ValueError("session_acl_source_target_malformed")
        if stored_target != expected_target:
            return False
        active = db.execute(
            "SELECT 1 FROM records WHERE namespace=? AND kind='threads' AND key=? AND active=1",
            (journal.namespace, source_key)
        ).fetchone()
        if not active:
            return False
        # Access commands and ordinary replies must not admit the same provider
        # event or physical message through separate receipt kinds.
        for kind, key in (
            (_KIND_ACL_COMMANDS, self._command_key(event_key) if event_key is not None else None),
            (_KIND_ACL_MESSAGES, self._msg_dedup_key(message_key) if message_key is not None else None),
        ):
            if key is not None and db.execute(
                    "SELECT 1 FROM records WHERE namespace=? AND kind=? AND key=?",
                    (journal.namespace, kind, key)).fetchone() is not None:
                raise ValueError("acl_reply_identity_collision")
        self._check_route_identity(db, journal, event_key, message_key)
        if user_id in admin_ids:
            return True
        canonical_tid = _canonical_uuid(stored_target.thread_id)
        if canonical_tid is None:
            raise ValueError("session_acl_source_target_malformed")
        acl_key = self._acl_key(canonical_tid)
        acl_entry = self._strict_get_own(db, journal, _KIND_ACL, acl_key,
                                         "session_acl_null_record")
        if acl_entry is None:
            return False
        self._validate_acl(acl_entry, canonical_tid)
        return user_id in acl_entry.get("delegates", [])

    def _check_route_identity(self, db, journal, event_key, message_key):
        if self.provider != "slack":
            return
        from .session_routes import SessionRoutes
        routes = SessionRoutes(self.thread_store, scope=self.scope)
        for kind, key in (("route_commands", routes._command_key(event_key) if event_key else None),
                          ("route_messages", sha256_text(message_key) if message_key else None)):
            if key is not None and db.execute(
                    "SELECT 1 FROM records WHERE namespace=? AND kind=? AND key=?",
                    (journal.namespace, kind, key)).fetchone() is not None:
                raise ValueError("acl_route_identity_collision")

    def apply(self, *, event_key: str, message_key: str, source_key: str,
              user_id: str, operation: str, delegate_id: Optional[str],
              payload_sha256: str, admin_ids: Any, expected_target: RelayTarget) -> dict:
        """Apply an ACL operation atomically."""
        if not isinstance(event_key, str) or not event_key:
            raise ValueError("acl_apply_event_key_invalid")
        if not isinstance(message_key, str) or not message_key:
            raise ValueError("acl_apply_message_key_invalid")
        if not isinstance(source_key, str) or not _digest(source_key):
            raise ValueError("acl_apply_source_key_invalid")
        if not _valid_user_id(self.provider, user_id):
            raise ValueError("acl_apply_user_id_invalid")
        if operation not in ("add", "remove", "access"):
            raise ValueError("acl_apply_operation_invalid")
        if operation != "access" and delegate_id is None:
            raise ValueError("acl_apply_delegate_id_required")
        if operation == "access" and delegate_id is not None:
            raise ValueError("acl_apply_delegate_id_forbidden")
        if delegate_id is not None and not _valid_user_id(self.provider, delegate_id):
            raise ValueError("acl_apply_delegate_id_invalid")
        if not _digest(payload_sha256):
            raise ValueError("acl_apply_payload_sha256_invalid")
        if user_id not in admin_ids:
            raise ValueError("acl_apply_not_admin")

        journal = self.thread_store.journal
        cmd_key = self._command_key(event_key)
        msg_dedup_key = self._msg_dedup_key(message_key)
        event_key_digest = sha256_text(event_key)
        message_key_digest = sha256_text(message_key)

        with journal.transaction() as db:
            self._check_route_identity(db, journal, event_key, message_key)
            # A receipt of any shape reserves its identity. A malformed prior
            # ordinary receipt must not look like permission to overwrite it.
            for kind, key in (("events", event_key_digest), ("messages", message_key_digest)):
                if db.execute(
                        "SELECT 1 FROM records WHERE namespace=? AND kind=? AND key=?",
                        (journal.namespace, kind, key)).fetchone() is not None:
                    raise ValueError("acl_reply_identity_collision")
            existing_cmd = self._strict_get_own(db, journal, _KIND_ACL_COMMANDS,
                                                cmd_key, "acl_command_null_record")
            if existing_cmd is not None:
                self._validate_command(existing_cmd)
                self._check_command_collision(existing_cmd, event_key_digest,
                    message_key_digest, source_key, user_id, operation,
                    delegate_id, payload_sha256, expected_target)
                try:
                    target_obj = RelayTarget.from_dict(existing_cmd["target"])
                except (ValueError, KeyError):
                    raise ValueError("acl_command_target_malformed")
                acl_key = self._acl_key(existing_cmd["thread_id"])
                acl_entry = self._strict_get_own(db, journal, _KIND_ACL, acl_key,
                                                 "session_acl_null_record")
                current_dels = self._read_delegates(acl_entry, existing_cmd["thread_id"])
                return {"status": "duplicate", "target": target_obj,
                        "delegates": current_dels, "operation_key": cmd_key}

            existing_msg = self._strict_get_own(db, journal, _KIND_ACL_MESSAGES,
                                                msg_dedup_key, "acl_message_null_record")
            if existing_msg is not None:
                self._validate_message(existing_msg)
                if existing_msg.get("payload_sha256") != payload_sha256:
                    raise ValueError("acl_message_id_collision")
                orig_cmd_key = existing_msg["command_key"]
                orig_cmd = self._strict_get_own(db, journal, _KIND_ACL_COMMANDS,
                                                orig_cmd_key, "acl_command_null_record")
                if orig_cmd is None:
                    raise ValueError("acl_message_reference_missing")
                self._validate_command(orig_cmd)
                if (existing_msg.get("event_key_digest") != orig_cmd.get("event_key_digest")
                        or orig_cmd.get("message_key_digest") != message_key_digest):
                    raise ValueError("acl_message_receipt_mismatch")
                self._check_message_collision(orig_cmd, source_key, user_id, operation,
                    delegate_id, payload_sha256, expected_target)
                try:
                    target_obj = RelayTarget.from_dict(orig_cmd["target"])
                except (ValueError, KeyError):
                    raise ValueError("acl_command_target_malformed")
                acl_key = self._acl_key(orig_cmd["thread_id"])
                acl_entry = self._strict_get_own(db, journal, _KIND_ACL, acl_key,
                                                 "session_acl_null_record")
                current_dels = self._read_delegates(acl_entry, orig_cmd["thread_id"])
                return {"status": "duplicate", "target": target_obj,
                        "delegates": current_dels, "operation_key": orig_cmd_key}

            source_entry = journal.get(db, "threads", source_key)
            if source_entry is None:
                raise ValueError("acl_source_unknown")
            recomputed = _recompute_source_key(self.provider, source_entry)
            if recomputed is None or recomputed != source_key:
                raise ValueError("acl_source_identity_mismatch")
            try:
                stored_target = RelayTarget.from_dict(source_entry["target"])
            except (ValueError, KeyError):
                raise ValueError("acl_source_target_malformed")
            if stored_target != expected_target:
                raise ValueError("acl_target_mismatch")

            active = db.execute(
                "SELECT 1 FROM records WHERE namespace=? AND kind='threads' AND key=? AND active=1",
                (journal.namespace, source_key)
            ).fetchone()
            if not active:
                raise ValueError("acl_source_closed")

            canonical_tid = _canonical_uuid(stored_target.thread_id)
            if canonical_tid is None:
                raise ValueError("acl_source_target_malformed")
            acl_key = self._acl_key(canonical_tid)
            acl_entry = self._strict_get_own(db, journal, _KIND_ACL, acl_key,
                                             "session_acl_null_record")
            if acl_entry is not None:
                self._validate_acl(acl_entry, canonical_tid)
                current_delegates = list(acl_entry["delegates"])
            else:
                current_delegates = []

            if operation == "access":
                status = "access"
                new_delegates = current_delegates
            elif operation == "add":
                if delegate_id in admin_ids:
                    status = "already_admin"
                    new_delegates = current_delegates
                elif delegate_id in current_delegates:
                    status = "already_present"
                    new_delegates = current_delegates
                else:
                    status = "added"
                    new_delegates = sorted(current_delegates + [delegate_id])
            else:
                if delegate_id in admin_ids:
                    status = "already_admin"
                    new_delegates = current_delegates
                elif delegate_id not in current_delegates:
                    status = "not_present"
                    new_delegates = current_delegates
                else:
                    status = "removed"
                    new_delegates = sorted(d for d in current_delegates if d != delegate_id)

            if new_delegates != current_delegates:
                new_acl = dict(acl_entry) if acl_entry is not None else {}
                new_acl.update({
                    "schema_version": _SCHEMA_VERSION,
                    "provider": self.provider,
                    "scope": self.scope,
                    "thread_id": canonical_tid,
                    "delegates": new_delegates,
                    "updated_at": utc_now(),
                })
                if "created_at" not in new_acl:
                    new_acl["created_at"] = utc_now()
                journal.put(db, _KIND_ACL, acl_key, new_acl)

            if not journal.claim(db, source_key):
                raise ValueError("acl_source_closed")

            cmd_entry = {
                "schema_version": _SCHEMA_VERSION,
                "provider": self.provider,
                "scope": self.scope,
                "event_key_digest": event_key_digest,
                "message_key_digest": message_key_digest,
                "source_key": source_key,
                "user_id": user_id,
                "operation": operation,
                "delegate_id": delegate_id,
                "payload_sha256": payload_sha256,
                "thread_id": canonical_tid,
                "target": stored_target.to_dict(),
                "status": status,
                "confirmation_claimed": False,
                "confirmation_status": None,
                "created_at": utc_now(),
            }
            journal.put(db, _KIND_ACL_COMMANDS, cmd_key, cmd_entry)

            msg_entry = {
                "schema_version": _SCHEMA_VERSION,
                "event_key_digest": event_key_digest,
                "payload_sha256": payload_sha256,
                "command_key": cmd_key,
            }
            journal.put(db, _KIND_ACL_MESSAGES, msg_dedup_key, msg_entry)

        return {
            "status": status,
            "target": stored_target,
            "delegates": new_delegates,
            "operation_key": cmd_key,
        }

    def _check_command_collision(self, existing: dict, event_key_digest: str,
                                  message_key_digest: str, source_key: str, user_id: str,
                                  operation: str, delegate_id: Optional[str],
                                  payload_sha256: str, expected_target: RelayTarget) -> None:
        try:
            stored_target = RelayTarget.from_dict(existing["target"])
        except (ValueError, KeyError):
            raise ValueError("acl_command_target_malformed")
        if (existing.get("event_key_digest") != event_key_digest
                or existing.get("message_key_digest") != message_key_digest
                or existing.get("source_key") != source_key
                or existing.get("user_id") != user_id
                or existing.get("operation") != operation
                or existing.get("delegate_id") != delegate_id
                or existing.get("payload_sha256") != payload_sha256
                or stored_target != expected_target):
            raise ValueError("acl_command_collision")

    def _check_message_collision(self, orig_cmd: dict, source_key: str, user_id: str,
                                  operation: str, delegate_id: Optional[str],
                                  payload_sha256: str, expected_target: RelayTarget) -> None:
        try:
            stored_target = RelayTarget.from_dict(orig_cmd["target"])
        except (ValueError, KeyError):
            raise ValueError("acl_command_target_malformed")
        if (orig_cmd.get("source_key") != source_key
                or orig_cmd.get("user_id") != user_id
                or orig_cmd.get("operation") != operation
                or orig_cmd.get("delegate_id") != delegate_id
                or orig_cmd.get("payload_sha256") != payload_sha256
                or stored_target != expected_target):
            raise ValueError("acl_message_binding_collision")

    def _read_delegates(self, acl_entry: Optional[dict], canonical_tid: str) -> List[str]:
        if acl_entry is None:
            return []
        self._validate_acl(acl_entry, canonical_tid)
        return list(acl_entry["delegates"])

    def claim_confirmation(self, operation_key: str) -> Optional[dict]:
        """Claim the one-shot confirmation notification. Returns once; None thereafter."""
        journal = self.thread_store.journal
        with journal.transaction() as db:
            entry = self._strict_get_own(db, journal, _KIND_ACL_COMMANDS,
                                         operation_key, "acl_command_null_record")
            if entry is None:
                return None
            self._validate_command(entry)
            if type(entry.get("confirmation_claimed")) is not bool:
                return None
            if entry["confirmation_claimed"] is True:
                return None

            source_key = entry["source_key"]
            source_entry = journal.get(db, "threads", source_key)
            if source_entry is None:
                return None
            recomputed = _recompute_source_key(self.provider, source_entry)
            if recomputed is None or recomputed != source_key:
                raise ValueError("acl_confirmation_source_mismatch")
            try:
                source_target = RelayTarget.from_dict(source_entry["target"])
            except (ValueError, KeyError):
                return None
            try:
                cmd_target = RelayTarget.from_dict(entry["target"])
            except (ValueError, KeyError):
                return None
            if source_target != cmd_target:
                return None
            canonical_tid = entry["thread_id"]
            if _canonical_uuid(source_target.thread_id) != canonical_tid:
                raise ValueError("acl_confirmation_target_mismatch")

            acl_key = self._acl_key(canonical_tid)
            acl_entry = self._strict_get_own(db, journal, _KIND_ACL, acl_key,
                                             "session_acl_null_record")
            current_delegates = self._read_delegates(acl_entry, canonical_tid)

            fingerprint = sha256_text(
                f"acl_confirm\0{self.scope}\0{entry['event_key_digest']}")

            op_status = entry["status"]
            entry["confirmation_claimed"] = True
            entry["confirmation_status"] = "claimed"
            journal.put(db, _KIND_ACL_COMMANDS, operation_key, entry)

        if self.provider == "slack":
            address = (source_entry.get("channel_id", "")
                       + "\0" + source_entry.get("thread_ts", ""))
        else:
            address = (source_entry.get("chat_id", "")
                       + "\0" + source_entry.get("message_id", ""))

        return {
            "target": cmd_target,
            "source_key": source_key,
            "source_entry": source_entry,
            "address": address,
            "delegates": current_delegates,
            "status": op_status,
            "fingerprint": fingerprint,
        }

    def finish_confirmation(self, operation_key: str, status: str) -> None:
        """Record sent/uncertain after a claimed confirmation. No retry after uncertain."""
        if status not in ("sent", "uncertain"):
            raise ValueError("acl_confirmation_status_invalid")
        journal = self.thread_store.journal
        with journal.transaction() as db:
            entry = self._strict_get_own(db, journal, _KIND_ACL_COMMANDS,
                                         operation_key, "acl_command_null_record")
            if entry is None:
                return
            self._validate_command(entry)
            if (entry.get("confirmation_claimed") is not True
                    or entry.get("confirmation_status") != "claimed"):
                return
            entry["confirmation_status"] = status
            journal.put(db, _KIND_ACL_COMMANDS, operation_key, entry)

    def delegates(self, db: Any, thread_id: str) -> List[str]:
        """Return sorted immutable delegates from strictly validated current state."""
        canonical = _canonical_uuid(thread_id)
        if canonical is None:
            raise ValueError("session_acl_thread_id_invalid")
        journal = self.thread_store.journal
        acl_key = self._acl_key(canonical)
        acl_entry = self._strict_get_own(db, journal, _KIND_ACL, acl_key,
                                         "session_acl_null_record")
        return self._read_delegates(acl_entry, canonical)
