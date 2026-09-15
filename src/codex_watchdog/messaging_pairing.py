"""Per-device, one-time pairing through independent authenticated history reads."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time

from .lark_transport import valid_id
from .messaging_device import device_label
from .messaging_profile import MessagingError, PREFIX
from .pairing_api import PairingApi, select_conversation
from .storage import FileLock
from .slack_mapping import valid_slack_timestamp


class NoncePairing:
    def __init__(self, provider, chat, label, *, bot_user=None, lifetime=180,
                 clock=time.time, monotonic=time.monotonic):
        self.provider, self.chat, self.label, self.bot_user = provider, chat, label, bot_user
        self.clock, self.monotonic = clock, monotonic
        self.started, self.deadline = clock(), monotonic() + lifetime
        self.lifetime = lifetime
        self.nonce = "WATCHDOG-PAIR-" + label + "-" + secrets.token_hex(16)
        self.candidate = None
        self.ambiguous = self.consumed = False
        self.observed = {}
        self.poll_count = 0
        self.exact_code_seen = False

    def diagnostics(self):
        # No message text, credentials or raw pairing code enters durable state.
        return dict(provider=self.provider, conversation_id=self.chat, device_label=self.label,
                    code_sha256=hashlib.sha256(self.nonce.encode()).hexdigest(),
                    started_at=self.started, poll_count=self.poll_count,
                    observed_message_count=len(self.observed), exact_code_seen=self.exact_code_seen)

    def check_deadline(self):
        if self.monotonic() >= self.deadline:
            raise MessagingError("messaging_pairing_expired")
        if self.consumed:
            raise MessagingError("messaging_pairing_expired_or_ambiguous")

    def offer(self, message):
        self.check_deadline()
        if not isinstance(message, dict):
            raise MessagingError("messaging_history_page_invalid")
        if self.provider == "slack":
            user, stamp, text = message.get("user"), message.get("ts"), message.get("text")
            valid = (isinstance(user, str) and re.fullmatch(r"[UW][A-Z0-9]{8,}", user) and
                     valid_slack_timestamp(stamp) and message.get("channel", self.chat) == self.chat and
                     user != self.bot_user and message.get("type") == "message" and
                     not any(message.get(k) for k in ("subtype", "bot_id", "app_id", "edited", "hidden", "deleted", "thread_ts")))
            identity = stamp
            created = stamp
            semantic = (user, stamp, text, bool(valid))
        else:
            sender, body = message.get("sender", {}), message.get("body", {})
            if not isinstance(sender, dict) or not isinstance(body, dict):
                raise MessagingError("messaging_history_page_invalid")
            user, identity = sender.get("id"), message.get("message_id")
            try:
                content = json.loads(body.get("content", "{}"))
                text = content.get("text") if isinstance(content, dict) else None
                created = int(message.get("create_time", "")) / 1000
            except (TypeError, ValueError):
                text, created = None, None
            valid = (valid_id(user, "ou") and valid_id(identity, "om") and
                     sender.get("sender_type") == "user" and sender.get("id_type") == "open_id" and
                     message.get("chat_id") == self.chat and message.get("msg_type") == "text" and
                     message.get("deleted") is False and
                     not any(message.get(k) for k in ("edited", "parent_id", "root_id")))
            # Feishu's updated/update_time also change on ordinary newly sent
            # messages. They are not proof of a text edit. Creation must be after
            # the unpredictable nonce; a changed first-observed message is refused.
            semantic = (user, identity, created, text, bool(valid))
        if not isinstance(identity, str):
            return False
        self.exact_code_seen |= text == self.nonce
        digest = hashlib.sha256(json.dumps(semantic, ensure_ascii=True).encode()).hexdigest()
        previous = self.observed.setdefault(identity, digest)
        if previous != digest:
            if self.candidate and identity == self.candidate[1]:
                self.ambiguous = True
            return False
        try:
            fresh = self.started <= float(created) <= min(self.clock() + 2, self.started + self.lifetime)
        except (ValueError, TypeError, OverflowError):
            fresh = False
        if not valid or not fresh or text != self.nonce:
            return False
        candidate = (user, identity)
        if self.candidate and self.candidate != candidate:
            self.ambiguous = True
        else:
            self.candidate = candidate
        return True

    def poll(self, api):
        self.check_deadline()
        self.poll_count += 1
        cursor, seen, messages = None, set(), []
        end = self.clock()
        for _ in range(20):
            self.check_deadline()
            items, next_cursor = api.history(self.chat, self.started, end, cursor)
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise MessagingError("messaging_history_page_invalid")
            messages.extend(items)
            if not next_cursor:
                break
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise MessagingError("messaging_history_pagination_invalid")
            seen.add(next_cursor)
            cursor = next_cursor
        else:
            raise MessagingError("messaging_pairing_history_too_large")
        # Read the complete bounded interval before accepting a candidate, so a
        # second matching identity on a later page cannot be hidden by success.
        matched = [self.offer(message) for message in messages]
        self.check_deadline()
        if self.ambiguous:
            raise MessagingError("messaging_pairing_expired_or_ambiguous")
        return any(matched)

    def finish(self, values):
        self.check_deadline()
        if not self.candidate or self.ambiguous:
            raise MessagingError("messaging_pairing_expired_or_ambiguous")
        self.consumed = True
        route = dict(schema_version=1, device_label=self.label, reply_mode="poll",
                     allowed_user_ids=[self.candidate[0]])
        if self.provider == "slack":
            route["channel_id"] = self.chat
        else:
            route.update(chat_id=self.chat, domain=values[PREFIX + "LARK_DOMAIN"], app_id=values[PREFIX + "LARK_APP_ID"])
        return route


def pair_provider(provider, values, runtime, *, read=input, output=print, api_factory=PairingApi):
    # This lock serializes setup only. Existing reply listeners can keep running;
    # no socket is opened and no provider event is consumed or acknowledged.
    with FileLock(runtime / "locks" / "messaging-pairing.lock"):
        api = api_factory(provider, values)
        chat = select_conversation(api, read=read, output=output)
        api.check_history(chat)
        pairing = NoncePairing(provider, chat, device_label(runtime), bot_user=api.bot_user)
        output("Device " + pairing.label + ": pairing with " + chat + ".")
        output("Post only the following code as a NEW plain-text message in that conversation, not a thread reply or code block (expires in 3 minutes):")
        output(pairing.nonce)
        output("Waiting for your message; checking every 5 seconds...")
        try:
            while not pairing.poll(api):
                time.sleep(min(5, max(0, pairing.deadline - pairing.monotonic())))
            user, identity = pairing.candidate
            api.check_replies(chat, identity)
            output("Received confirmation from " + user + " in " + chat + ".")
            if read("Is this the conversation and account you intended? Type yes: ").strip().lower() != "yes":
                raise MessagingError("messaging_pairing_cancelled")
            # Re-read before saving; a deleted/changed code cannot survive the
            # local confirmation, and simultaneous attempts stay independent.
            if not pairing.poll(api):
                raise MessagingError("messaging_pairing_confirmation_changed")
            return pairing.finish(values)
        except MessagingError as exc:
            exc.pairing_diagnostics = pairing.diagnostics()
            if str(exc) == "messaging_pairing_expired":
                output("Pairing timed out: no valid exact confirmation was found in " + chat +
                       ". Check the conversation and post the new code when you retry setup.")
            raise
