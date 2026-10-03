"""Bounded metadata-only Slack bot receipts; never admit or replay a task."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Optional

from .models import sha256_text, utc_now
from .relay import RelayTarget
from .slack_bot_acl import BotSessionAccess, _digest, _principal, _uuid
from .slack_bot_identity import BotPrincipal
from .slack_mapping import SlackThreadStore, valid_slack_channel_id, valid_slack_timestamp


_NAMESPACE = "slack/bot-command-receipts-v1"
_KIND = "slack_bot_receipts"
_CATEGORIES = {"queued": "primary", "uncertain": "primary",
               "rejected": "rejection", "duplicate": "duplicate"}
_DELIVERED = frozenset(("enqueued", "consumed_or_started", "started"))


@dataclass(frozen=True)
class BotReceiptContext:
    """Created only after the relay verifies the provider's complete identity."""

    source_key: str
    principal: BotPrincipal
    target: RelayTarget
    request_id: str
    event_key: str
    message_key: str
    payload_sha256: str

    def __post_init__(self):
        _principal(self.principal)
        if (not isinstance(self.target, RelayTarget) or not _digest(self.source_key)
                or _uuid(self.request_id) != self.request_id):
            raise ValueError("slack_bot_receipt_context_invalid")
        RelayTarget.from_dict(self.target.to_dict())
        BotSessionAccess._input(self.event_key, self.message_key, self.payload_sha256)
        address = self.message_key.split("\0")
        if (len(address) != 2 or not valid_slack_channel_id(address[0])
                or not valid_slack_timestamp(address[1])):
            raise ValueError("slack_bot_receipt_message_invalid")


@dataclass(frozen=True)
class BotReceiptClaim:
    key: str
    outcome: str
    context: BotReceiptContext
    channel_id: str
    thread_ts: str
    fingerprint: str


class BotCommandReceipts:
    def __init__(self, access: BotSessionAccess):
        if not isinstance(access, BotSessionAccess):
            raise ValueError("slack_bot_receipt_access_invalid")
        self.access = access

    @staticmethod
    def _key(context, category):
        request = BotSessionAccess._request_key(context.principal, context.request_id)
        return sha256_text("slack_bot_receipt\0" + request + "\0" + category)

    @staticmethod
    def _fingerprint(key):
        return sha256_text("slack_bot_receipt_send\0" + key)

    def _get_receipt(self, db, key):
        row = db.execute("SELECT value FROM records WHERE namespace=? AND kind=? AND key=?",
                         (_NAMESPACE, _KIND, key)).fetchone()
        if row is None:
            return None
        try:
            entry = json.loads(row[0])
        except (ValueError, TypeError):
            raise ValueError("slack_bot_receipt_invalid") from None
        self._validate_receipt(entry, key)
        return entry

    def _validate_receipt(self, entry, key):
        if not isinstance(entry, dict):
            raise ValueError("slack_bot_receipt_invalid")
        self.access._schema(entry)
        principal = BotPrincipal.from_dict(entry.get("principal"))
        request_id = entry.get("request_id")
        target = RelayTarget.from_dict(entry.get("target"))
        outcome = entry.get("outcome")
        category = _CATEGORIES.get(outcome) if isinstance(outcome, str) else None
        request_key = BotSessionAccess._request_key(principal, request_id) if _uuid(request_id) == request_id else None
        expected = sha256_text("slack_bot_receipt\0" + str(request_key) + "\0" + str(category))
        if (category is None or entry.get("category") != category or key != expected
                or entry.get("fingerprint") != self._fingerprint(key)
                or not valid_slack_channel_id(entry.get("scope"))
                or not isinstance(entry.get("source_namespace"), str) or not entry["source_namespace"]
                or entry.get("source_key") != SlackThreadStore.thread_key(
                    entry.get("channel_id"), entry.get("thread_ts"))
                or any(not _digest(entry.get(name)) for name in (
                    "event_key_digest", "message_key_digest", "payload_sha256"))
                or entry.get("state") not in ("claimed", "sent", "uncertain")
                or (entry["state"] == "sent" and not valid_slack_timestamp(entry.get("message_ts")))
                or (entry["state"] != "sent" and entry.get("message_ts") is not None)):
            raise ValueError("slack_bot_receipt_invalid")
        proof = entry.get("proof")
        if (not isinstance(proof, dict) or set(proof) != {"request_key", "event_key_digest", "message_key_digest"}
                or proof.get("request_key") not in (None, request_key)
                or (outcome != "rejected" and proof.get("request_key") is None)
                or any(value is not None and not _digest(value) for value in proof.values())
                or (proof["request_key"] is None) != (proof["event_key_digest"] is None)
                or (proof["request_key"] is None) != (proof["message_key_digest"] is None)):
            raise ValueError("slack_bot_receipt_proof_invalid")
        return target

    def _event(self, db, key):
        entry = self.access._get(db, "events", key)
        if entry is None:
            return None
        required = {"admission_schema", "thread_key", "message_key", "payload_sha256", "text_sha256",
                    "instruction_id", "text_chars", "state", "delivery_status", "created_at", "updated_at", "error_sha256"}
        if (not required.issubset(entry)
                or type(entry.get("admission_schema")) is not int or entry["admission_schema"] != 1
                or any(not _digest(entry.get(name)) for name in (
                    "thread_key", "message_key", "payload_sha256", "text_sha256"))
                or entry.get("instruction_id") != "slackbot:" + key[:40]
                or type(entry.get("text_chars")) is not int or entry["text_chars"] < 1
                or entry.get("state") not in ("dispatching", "delivered", "uncertain")
                or not isinstance(entry.get("created_at"), str) or not entry["created_at"]
                or (entry["error_sha256"] is not None and not _digest(entry["error_sha256"]))
                or (entry["state"] == "dispatching"
                    and (entry["delivery_status"] is not None or entry["updated_at"] is not None))
                or (entry["state"] != "dispatching" and (
                    not isinstance(entry["delivery_status"], str) or not entry["delivery_status"]
                    or not isinstance(entry["updated_at"], str) or not entry["updated_at"]))
                or (entry["state"] == "delivered" and entry["delivery_status"] not in _DELIVERED)):
            raise ValueError("slack_bot_receipt_event_invalid")
        return entry

    def _physical(self, db, key):
        entry = self.access._get(db, "messages", key)
        if entry is not None and (type(entry.get("schema_version")) is not int
                or entry["schema_version"] != 1 or not _digest(entry.get("event_key"))
                or not _digest(entry.get("payload_sha256"))):
            raise ValueError("slack_bot_receipt_message_invalid")
        return entry

    def _request(self, db, context):
        key = self.access._request_key(context.principal, context.request_id)
        entry = self.access._get(db, "slack_bot_requests", key)
        if entry is None:
            return None
        self.access._schema(entry)
        target = RelayTarget.from_dict(entry.get("target"))
        if (BotPrincipal.from_dict(entry.get("principal")) != context.principal
                or entry.get("request_id") != context.request_id
                or not valid_slack_channel_id(entry.get("scope"))
                or any(not _digest(entry.get(name)) for name in (
                    "source_key", "event_key_digest", "message_key_digest", "payload_sha256"))):
            raise ValueError("slack_bot_receipt_request_invalid")
        source = self.access._source(db, entry["source_key"], target, active=False)
        if (source is None or entry.get("channel_id") != source["channel_id"]
                or entry.get("thread_ts") != source["thread_ts"]):
            raise ValueError("slack_bot_receipt_request_invalid")
        event = self._event(db, entry["event_key_digest"])
        physical = self._physical(db, entry["message_key_digest"])
        if (event is None or physical is None
                or event["thread_key"] != entry["source_key"]
                or event["message_key"] != entry["message_key_digest"]
                or event["payload_sha256"] != entry["payload_sha256"]
                or physical["event_key"] != entry["event_key_digest"]
                or physical["payload_sha256"] != entry["payload_sha256"]):
            raise ValueError("slack_bot_receipt_request_invalid")
        return entry, event

    def _current_identity(self, db, context):
        event_key, message_key = sha256_text(context.event_key), sha256_text(context.message_key)
        event = self._event(db, event_key)
        physical = self._physical(db, message_key)
        if physical is not None:
            if physical["payload_sha256"] != context.payload_sha256:
                raise ValueError("slack_bot_receipt_identity_collision")
            original = self._event(db, physical["event_key"])
            if original is None:
                raise ValueError("slack_bot_receipt_identity_collision")
            if event is not None and physical["event_key"] != event_key:
                raise ValueError("slack_bot_receipt_identity_collision")
            event = original
        if event is not None and (physical is None or event["thread_key"] != context.source_key
                or event["message_key"] != message_key or event["payload_sha256"] != context.payload_sha256):
            raise ValueError("slack_bot_receipt_identity_collision")
        self.access._human_control_collision(db, context.event_key, context.message_key)
        return event

    def _identity(self, context, outcome, source):
        return dict(provider="slack", scope=self.access.scope,
            source_namespace=self.access.thread_store.journal.namespace,
            principal=context.principal.to_dict(), request_id=context.request_id,
            target=context.target.to_dict(), source_key=context.source_key,
            channel_id=source["channel_id"], thread_ts=source["thread_ts"],
            event_key_digest=sha256_text(context.event_key),
            message_key_digest=sha256_text(context.message_key), payload_sha256=context.payload_sha256,
            outcome=outcome, category=_CATEGORIES[outcome])

    def claim(self, context: BotReceiptContext, outcome: str) -> Optional[BotReceiptClaim]:
        if (not isinstance(context, BotReceiptContext) or not isinstance(outcome, str)
                or outcome not in _CATEGORIES):
            raise ValueError("slack_bot_receipt_context_invalid")
        context.__post_init__()
        journal = self.access.thread_store.journal
        with journal.transaction() as db:
            source = self.access._source(db, context.source_key, context.target, active=False)
            if source is None:
                return None
            channel, timestamp = context.message_key.split("\0")
            if channel != source["channel_id"] or timestamp == source["thread_ts"]:
                raise ValueError("slack_bot_receipt_identity_collision")
            if context.principal not in self.access._grants(db, context.target.thread_id)[1]:
                return None
            registry = self.access._get(db, "slack_bot_users", self.access._user_key(context.principal.user_id))
            if not self.access._known(db, context.principal.user_id):
                return None
            if context.principal not in self.access._principal_list(registry):
                return None
            current = self._current_identity(db, context)
            request = self._request(db, context)
            if outcome in ("queued", "uncertain"):
                if request is None:
                    return None
                original, event = request
                if (original["scope"] != self.access.scope or original["target"] != context.target.to_dict()
                        or original["source_key"] != context.source_key
                        or original["message_key_digest"] != sha256_text(context.message_key)
                        or original["payload_sha256"] != context.payload_sha256 or current != event):
                    raise ValueError("slack_bot_receipt_identity_collision")
                delivered = event["state"] == "delivered" and event["delivery_status"] in _DELIVERED
                if (outcome == "queued") != delivered:
                    raise ValueError("slack_bot_receipt_outcome_invalid")
            elif outcome == "duplicate":
                if request is None:
                    return None
                if current is not None and current != request[1]:
                    raise ValueError("slack_bot_receipt_identity_collision")
            else:
                active = db.execute("SELECT active FROM records WHERE namespace=? AND kind='threads' AND key=?",
                                    (journal.namespace, context.source_key)).fetchone()
                if current is not None or active is None or active[0] != 0:
                    return None
                if request is not None:
                    # A new physical message can encounter the closed source
                    # before admission checks its already reserved logical UUID.
                    outcome = "duplicate"
            key = self._key(context, _CATEGORIES[outcome])
            if self._get_receipt(db, key) is not None:
                return None
            proof = dict(request_key=self.access._request_key(context.principal, context.request_id) if request else None,
                         event_key_digest=request[0]["event_key_digest"] if request else None,
                         message_key_digest=request[0]["message_key_digest"] if request else None)
            entry = dict(self._identity(context, outcome, source), schema_version=1, created_at=utc_now(),
                         state="claimed", message_ts=None, fingerprint=self._fingerprint(key), proof=proof)
            db.execute("INSERT INTO records(namespace,kind,key,value) VALUES(?,?,?,?)",
                       (_NAMESPACE, _KIND, key, json.dumps(entry, sort_keys=True)))
            return BotReceiptClaim(key, outcome, context, source["channel_id"], source["thread_ts"], entry["fingerprint"])

    def finish(self, claim: BotReceiptClaim, status: str = "sent", message_ts: Optional[str] = None) -> None:
        if (not isinstance(claim, BotReceiptClaim) or status not in ("sent", "uncertain")
                or (status == "sent" and not valid_slack_timestamp(message_ts))
                or (status == "uncertain" and message_ts is not None)):
            raise ValueError("slack_bot_receipt_finish_invalid")
        if (claim.outcome not in _CATEGORIES or claim.key != self._key(claim.context, _CATEGORIES[claim.outcome])
                or claim.fingerprint != self._fingerprint(claim.key)):
            raise ValueError("slack_bot_receipt_identity_collision")
        journal = self.access.thread_store.journal
        with journal.transaction() as db:
            entry = self._get_receipt(db, claim.key)
            expected = self._identity(claim.context, claim.outcome,
                                      dict(channel_id=claim.channel_id, thread_ts=claim.thread_ts))
            if entry is None or any(entry.get(key) != value for key, value in expected.items()):
                raise ValueError("slack_bot_receipt_identity_collision")
            if entry["state"] != "claimed":
                return
            entry.update(state=status, message_ts=message_ts)
            db.execute("UPDATE records SET value=? WHERE namespace=? AND kind=? AND key=?",
                       (json.dumps(entry, sort_keys=True), _NAMESPACE, _KIND, claim.key))
