"""One bounded sender-side notification intent per exact control target.

The kernel lease spans prepare, send and receipt commit. A subsequent holder
can finish its own crash-left intent conservatively, never call a provider.
Remote-host ownership leases alone cannot prove a desktop send has ended.
"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import uuid

from .control_state import ControlError, control_atomic_json, control_file_lock, control_read_json


class NotificationAttempt:
    def __init__(self, runtime, target):
        self.runtime = Path(runtime).resolve()
        self.target = dict(target)
        key = hashlib.sha256(json.dumps(self.target, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        directory = self.runtime / "notifications" / "control-attempts"
        self.path = directory / (key + ".json")
        self.lock_path = directory / (key + ".lock")
        self.sender = hashlib.sha256((socket.gethostname() + "\0" + str(self.path)).encode()).hexdigest()

    def lease(self):
        return control_file_lock(self.lock_path)

    def read(self):
        if not self.path.exists():
            return None
        value = control_read_json(self.path)
        if (value.get("target") != self.target or value.get("sender") != self.sender
                or value.get("phase") not in ("active", "terminal", "finalized")
                or not isinstance(value.get("token"), dict)
                or not isinstance(value.get("fingerprint"), str)
                or not isinstance(value.get("operation_id"), str)
                or value.get("phase") == "terminal" and (not isinstance(value.get("result"), dict)
                                                          or not isinstance(value.get("relay_mappings"), list))):
            raise ControlError("control_notification_attempt_invalid")
        return value

    def start(self, token, fingerprint, mapping_sources=()):
        value = dict(schema_version=1, phase="active", target=self.target, sender=self.sender,
                     token=dict(token), fingerprint=fingerprint, operation_id=str(uuid.uuid4()),
                     mapping_sources=list(mapping_sources))
        control_atomic_json(self.path, value)  # Before the possibly disconnected prepare RPC.
        return value

    def terminal(self, value, result, mappings):
        value = dict(value, phase="terminal", result=result, relay_mappings=list(mappings))
        control_atomic_json(self.path, value)  # All sends ended; finish-RPC failure cannot erase this.
        return value

    def finalized(self, value):
        control_atomic_json(self.path, dict(value, phase="finalized"))

    def reconcile(self, finish):
        """Caller holds the lease; no scan, provider call, TTL/PID guess or replay."""
        value = self.read()
        if value is None or value["phase"] == "finalized":
            return
        result = value.get("result") if value["phase"] == "terminal" else None
        if not isinstance(result, dict):
            result = dict(status="uncertain", terminal=True, reason="notification_sender_ended",
                          event_fingerprint=value["fingerprint"], duplicate=True)
        mappings, outcomes, errors = self.durable_evidence(value)
        result = dict(result)
        if outcomes and "provider_outcomes" not in result:
            result["provider_outcomes"] = outcomes
        if errors:
            result["evidence_errors"] = errors
        # Existing journals can contain a sent route even when the sender
        # crashed before copying it into this intent. Never reopen closed rows.
        if not value.get("mapping_sources"):
            for entry in value.get("relay_mappings", ()):
                if entry not in mappings:
                    mappings.append(entry)
        value = self.terminal(value, result, mappings)
        finish(value, result, mappings)
        self.finalized(value)

    def durable_evidence(self, value):
        mappings, outcomes, errors = [], {}, []
        sources = value.get("mapping_sources", ())
        if not isinstance(sources, (list, tuple)) or len(sources) > 3:
            raise ControlError("control_notification_attempt_invalid")
        fingerprint = value["fingerprint"]
        for source in sources:
            provider = source.get("provider") if isinstance(source, dict) else None
            if provider not in ("slack", "lark", "onebot"):
                raise ControlError("control_notification_attempt_invalid")
            namespace = source.get("namespace")
            if not isinstance(namespace, str) or not namespace.startswith(provider + "/"):
                raise ControlError("control_notification_attempt_invalid")
            outcomes[provider] = "uncertain"
            database = self.runtime / provider / "reply-tickets.sqlite3"
            try:
                if database.exists():
                    self._journal_evidence(database, source, value, mappings, outcomes)
                if provider in ("slack", "lark"):
                    dual = self.runtime / "notifications" / "dual-deliveries.sqlite3"
                    if dual.exists():
                        with closing(sqlite3.connect(dual.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                            row = db.execute("SELECT " + provider + " FROM receipts WHERE fingerprint=?", (fingerprint,)).fetchone()
                            if row is not None and row[0] is not None:
                                outcomes[provider] = row[0]
            except Exception:
                errors.append(provider + "_receipt_unavailable")
        return mappings, outcomes, errors

    @staticmethod
    def _journal_evidence(database, source, value, mappings, outcomes):
        provider = source["provider"]
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("journal schema")
            rows = db.execute("SELECT value FROM records INDEXED BY session_active_tickets "
                "WHERE namespace=? AND kind='threads' AND fingerprint=? "
                "AND thread_id COLLATE NOCASE=? AND active=1 LIMIT 4",
                (source["namespace"], value["fingerprint"], value["token"]["thread_id"])).fetchall()
            for row in rows:
                entry = json.loads(row[0])
                if provider == "slack":
                    mappings.append(dict(entry, ticket_schema=1))
                else:
                    mappings.append(dict(provider=provider, scope=source["scope"], ticket_schema=1,
                        **{k: entry[k] for k in ("chat_id", "message_id", "event_fingerprint", "created_at")}))
            receipt = db.execute("SELECT value FROM records WHERE namespace=? "
                "AND kind='notifications' AND key=?", (source["namespace"], value["fingerprint"])).fetchone()
            if receipt is not None:
                state = json.loads(receipt[0])["state"]
                if state not in ("sent", "uncertain"):
                    raise ValueError("journal receipt")
                outcomes[provider] = state


def remote_attempt(runtime, target, thread_id):
    # These are the established exact window/host coordinates, not a new route.
    return NotificationAttempt(runtime, dict(authority=target.authority, repo_path=target.repo_path,
                                             storage_key=target.storage_key, thread_id=thread_id))


def local_attempt(runtime, store):
    return NotificationAttempt(runtime, dict(control_directory=str(store.directory.resolve()),
                                             thread_id=store.thread_id, repo_path=store.repo_path))
