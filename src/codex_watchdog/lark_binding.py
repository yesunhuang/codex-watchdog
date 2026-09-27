"""Short-lived destination discovery inside the existing authenticated poller."""
import json
import time

from .lark_transport import LarkTransportError, valid_id
from .models import sha256_text


class LarkBindingDiscovery:
    # Hard bounds apply only while the user has requested a binding. A scan
    # finishes its fixed time window before admitting any destination candidate.
    MAX_CHATS = 32
    PAGES_PER_TICK = 4
    MAX_PAGES_PER_CHAT = 4

    def __init__(self, relay, api, *, clock=time.time):
        self.relay, self.api, self.clock = relay, api, clock
        self.scan = None

    def poll_once(self):
        pending = self.relay.binding.store.pending()
        if not pending:
            self.scan = None
            return []
        signature = sorted(p["key"] for p in pending)
        if self.scan is None or self.scan["signature"] != signature:
            conversations = self.api.conversations()
            chats = {self.relay.config.chat_id}
            for entry in conversations:
                chat = entry[0]
                if not valid_id(chat, "oc"):
                    raise LarkTransportError("binding_conversation_invalid")
                chats.add(chat)
            if len(chats) > self.MAX_CHATS:
                raise LarkTransportError("binding_discovery_too_many_chats")
            self.scan = dict(signature=signature, chats=sorted(chats), index=0,
                             start=int(min(p["created_at"] for p in pending)),
                             end=int(self.clock()) - 2, token=None, pages=0,
                             tokens=set(), matches={})
        scan = self.scan
        if scan["end"] < scan["start"]:
            self.scan = None
            return []
        for _ in range(self.PAGES_PER_TICK):
            chat = scan["chats"][scan["index"]]
            page = self.api.history(scan["start"], scan["end"], scan["token"], destination=chat)
            items = page.get("items") if isinstance(page, dict) else None
            more = page.get("has_more") if isinstance(page, dict) else None
            token = page.get("page_token") if isinstance(page, dict) else None
            if not isinstance(items, list) or len(items) > 50 or type(more) is not bool:
                raise LarkTransportError("binding_history_invalid")
            # Whole-page identity validation precedes any candidate handling.
            for message in items:
                if (not isinstance(message, dict) or message.get("chat_id") != chat
                        or not valid_id(message.get("message_id"), "om")
                        or not str(message.get("create_time", "")).isdigit()
                        or not scan["start"] * 1000 <= int(message["create_time"]) < (scan["end"] + 1) * 1000
                        or type(message.get("deleted")) is not bool
                        or not isinstance(message.get("sender"), dict)
                        or not isinstance(message.get("body"), dict)):
                    raise LarkTransportError("binding_history_invalid")
            for message in items:
                self._offer(message, scan["matches"])
            scan["pages"] += 1
            if more:
                if (not isinstance(token, str) or not 0 < len(token) <= 4096
                        or token in scan["tokens"] or not items
                        or scan["pages"] >= self.MAX_PAGES_PER_CHAT):
                    raise LarkTransportError("binding_history_limit_or_cursor_invalid")
                scan["tokens"].add(token)
                scan["token"] = token
                continue
            scan.update(index=scan["index"] + 1, pages=0, token=None, tokens=set())
            if scan["index"] == len(scan["chats"]):
                self.scan = None
                return self._finish(scan["matches"])
        return []

    def _offer(self, message, matches):
        sender, body = message["sender"], message["body"]
        if sender.get("sender_type") != "user" or sender.get("id_type") != "open_id":
            return
        if sender.get("id") not in self.relay.config.allowed_user_ids:
            return
        try:
            content = json.loads(body.get("content", "{}"))
        except (ValueError, TypeError):
            return
        text = content.get("text") if isinstance(content, dict) else None
        if not isinstance(text, str):
            return
        journal = self.relay.thread_store.journal
        identity = sha256_text(message["chat_id"] + "\0" + message["message_id"])
        semantic = sha256_text(json.dumps([sender.get("id"), message["create_time"], text,
            message.get("deleted"), message.get("edited"), message.get("root_id"),
            message.get("parent_id"), message.get("msg_type")], sort_keys=True))
        with journal.transaction() as db:
            old = journal.get(db, "binding_seen", identity)
            if old is None:
                journal.put(db, "binding_seen", identity, dict(schema_version=1, digest=semantic))
            elif old != dict(schema_version=1, digest=semantic):
                # Never authorize an edited first-observed message, including
                # after process restart. Store only semantic hashes.
                journal.put(db, "binding_seen", identity, dict(schema_version=1, invalidated=True))
                for candidates in matches.values():
                    candidates.pop(identity, None)
                return
        if (message["deleted"] or message.get("edited") or message.get("parent_id")
                or message.get("root_id") or message.get("mentions") or message.get("msg_type") != "text"):
            return
        candidate = self.relay.binding.store.lookup(text)
        if candidate is None or sender["id"] != candidate["user_id"]:
            return
        created = int(message["create_time"]) / 1000
        if not candidate["created_at"] <= created <= min(candidate["expires_at"], self.clock() + 2):
            return
        matches.setdefault(text, {})[identity] = (message, created)

    def _finish(self, matches):
        results = []
        journal = self.relay.thread_store.journal
        for text, candidates in matches.items():
            if not candidates:
                continue
            key = sha256_text(text)
            with journal.transaction() as db:
                if len(candidates) != 1:
                    journal.put(db, "binding_rejected", key, dict(schema_version=1, reason="ambiguous"))
                if journal.get(db, "binding_rejected", key) is not None:
                    results.append({"status": "rejected_ambiguous_route_challenge"})
                    continue
            message, created = next(iter(candidates.values()))
            result = self.relay.binding.confirm(text, message["sender"]["id"], message["chat_id"],
                message_id=message["message_id"], created_at=created)
            results.append(result.to_dict())
        return results
