"""Bounded provider controls; no control text is admitted to the Codex queue."""
import socket

from .binding_challenges import BindingChallenges
from .models import sha256_text
from .relay import ReplyResult
from .storage import StoreBusyError


def binding_control(text):
    """Reserve malformed commands and challenges too, preventing prompt fallback."""
    value = text.strip()
    if value.upper().startswith("WD-BIND-"):
        return "challenge"
    if value and value.split()[0].lower() in ("bind", "unbind"):
        return value.lower() if value.lower() in ("bind", "unbind") else "invalid"
    return None


class DestinationBinding:
    def __init__(self, relay, provider, send, *, clock=None):
        self.relay, self.provider, self.send = relay, provider, send
        kwargs = {} if clock is None else {"clock": clock}
        self.store = BindingChallenges(relay.thread_store, provider, relay.config.scope, **kwargs)

    def command(self, operation, *, event_key, chat_id, parent_id, user_id, text,
                message_id, created_at):
        if operation not in ("bind", "unbind"):
            return ReplyResult("rejected_route_command")
        try:
            method = self.store.begin if operation == "bind" else self.store.unbind
            result = method(event_key, chat_id, parent_id, user_id, text,
                            message_id=message_id, created_at=created_at)
        except StoreBusyError:
            return ReplyResult("deferred")
        except Exception:
            return ReplyResult("rejected_route_command")
        try:
            if result["status"] == "duplicate":
                return ReplyResult("duplicate", duplicate=True)
            target = result["target"]
            if operation == "bind":
                response = ("To move this exact Codex session, send the following code as a new "
                            "plain-text message in the destination group containing this bot. "
                            "Use the same account within five minutes:\n" + result["challenge"])
            else:
                response = "Unbound. Future notifications for this session use the configured default destination."
            # Admission is durable before the send. Duplicate events cannot retry it.
            self.send(self._host(response), result["operation_key"], chat_id, reply_to=message_id)
            return ReplyResult("route_pending" if operation == "bind" else "route_unbound",
                               target.workspace_id, result["operation_key"])
        except Exception:
            return ReplyResult("rejected_or_uncertain_route_command")

    def confirm(self, text, user_id, destination, *, message_id, created_at):
        if text != text.strip():
            return ReplyResult("rejected_route_challenge")
        try:
            journal = self.relay.thread_store.journal
            with journal.transaction() as db:
                if journal.get(db, "binding_rejected", sha256_text(text)) is not None:
                    return ReplyResult("rejected_route_challenge")
            result = self.store.complete(text, user_id, destination,
                                         message_id=message_id, created_at=created_at)
        except StoreBusyError:
            return ReplyResult("deferred")
        except Exception:
            return ReplyResult("rejected_route_challenge")
        try:
            if result["status"] != "bound":
                return ReplyResult("duplicate", duplicate=True)
            operation = result["operation_key"]
            claim = self.store.claim_hello(operation)
            if claim is None:
                return ReplyResult("route_hello_uncertain")
            target, chat_id, fingerprint = claim
            text = self._host(
                f"Hello! Binding successful. Workspace: {target.workspace_id}, "
                f"Session: {target.thread_id}. Reply to this message to reach this exact Codex session.")
            journal = self.relay.thread_store
            try:
                digest = sha256_text(text + "\0" + chat_id)
                if journal.prepare_notification(fingerprint, digest) is not None:
                    raise ValueError("binding_hello_already_recorded")
                response = self.send(text, fingerprint, chat_id)
                if response.get("chat_id") != chat_id:
                    raise ValueError("binding_hello_destination_mismatch")
                journal.finish_notification(fingerprint, chat_id, response["message_id"], target)
                self.store.finish_hello(operation, "sent")
            except Exception:
                self.store.finish_hello(operation, "uncertain")
                return ReplyResult("route_hello_uncertain", target.workspace_id, operation)
            return ReplyResult("route_bound", target.workspace_id, operation)
        except Exception:
            return ReplyResult("rejected_route_challenge")

    @staticmethod
    def _host(text):
        return "Machine: " + socket.gethostname() + "\n" + text
