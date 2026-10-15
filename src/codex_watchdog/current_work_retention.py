"""Current native proof protects original provider PKs before ordinary GC.

Index creation is cold/fresh-only. Ordinary reads seek current native identities,
never enumerate historical pending events. The caller owns the real fence for
the entire provider transaction. No task, client or receipt is manufactured.
"""
from contextlib import nullcontext
import json
from pathlib import Path
import stat

from .native_inflight import (NativeInflightError, _live_work, _work,
                             validate_queue_inventory)
from .native_inflight_pins import CurrentNativePins

WORK_BUDGET = 16


def _runtime_identity(value):
    """Existing directory resource, independent of Windows Path case equality."""
    path = Path(value)
    try:
        if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
            raise NativeInflightError("native_retention_runtime_changed")
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
            raise NativeInflightError("native_retention_runtime_changed")
        return info.st_dev, info.st_ino
    except (OSError, ValueError, TypeError, RuntimeError):
        raise NativeInflightError("native_retention_runtime_changed") from None
# Only a complete equality seek bounds work. SEARCH on a session/namespace
# prefix still walks that target's whole history. These are the actual fixed
# helper queries, not a guessed catalogue/index-name certificate.
_SEEKS = {
    "native_event_work": "thread_id=? AND <expr>=? AND <expr>=?",
    "native_request_event": "namespace=? AND <expr>=?",
    "native_ack_event": "<expr>=? AND <expr>=?",
}
_INDEXES = (
    ("native_event_work", "thread_id,json_extract(value,'$.instruction_id'),"
     "json_extract(value,'$.text_sha256')", "kind='events'"),
    ("native_request_event", "namespace,json_extract(value,'$.event_key_digest')",
     "kind='slack_bot_requests'"),
    ("native_ack_event", "json_extract(value,'$.source_namespace'),"
     "json_extract(value,'$.event_key_digest')", "kind='slack_bot_receipts'"),
)


def unsupported_retention(journal, db):
    """No current producer is unknown, never proof that deletion is safe."""
    return False


def create_reference_indexes(db):
    """Only an explicit cold migration or empty new journal calls this."""
    for name, columns, condition in _INDEXES:
        db.execute("CREATE INDEX IF NOT EXISTS " + name + " ON records(" + columns +
                   ") WHERE " + condition + " AND json_valid(value)")


def _indexed_rows(db, query, arguments, index):
    # A familiar name can hide a replaced index with the wrong leading keys.
    # Refuse before executing it; LIMIT alone does not bound a historical scan.
    expected = _SEEKS.get(index)
    if expected is None:
        raise NativeInflightError("native_retention_index_unavailable")
    plan = db.execute("EXPLAIN QUERY PLAN " + query, arguments).fetchmany(5)
    detail = "SEARCH records USING INDEX " + index + " (" + expected + ")"
    # Refuse an unsupported plan locally. Index rebuilding belongs to cold
    # preparation; no history query or schema repair is attempted here.
    if len(plan) != 1 or plan[0][3] not in (detail, "SEARCH TABLE records USING INDEX " + index + " (" + expected + ")"):
        raise NativeInflightError("native_retention_index_unavailable")
    return db.execute(query, arguments).fetchall()


def _row(db, namespace, kind, key, *, required=True):
    row = db.execute("SELECT namespace,kind,key,CASE WHEN length(CAST(value AS BLOB))<=65536 "
                     "THEN value END,thread_id FROM records WHERE namespace=? AND kind=? AND key=?",
                     (namespace, kind, key)).fetchone()
    if row is None and not required:
        return None
    if row is None or not isinstance(row[3], str):
        raise NativeInflightError("native_retention_reference_unavailable")
    return dict(zip(("namespace", "kind", "key", "value", "thread_id"), row))


def current_associations(db, inventory, *, budget=WORK_BUDGET):
    """Seek known original keys using exact current native instruction/hash.

    The budget bounds this optional action, not native FIFO or session count.
    Exceeding it defers GC without consuming, dropping or redispatching work.
    """
    if type(budget) is not int or not 1 <= budget <= 64:
        raise NativeInflightError("native_retention_work_budget_invalid")
    live = sorted(_live_work(inventory))
    if len(live) > budget:
        raise NativeInflightError("native_retention_work_budget")
    associations = []
    for identifier, digest in live:
        found = _indexed_rows(db,"SELECT namespace,key FROM records INDEXED BY native_event_work "
            "WHERE kind='events' AND json_valid(value) AND thread_id=? "
            "AND json_extract(value,'$.instruction_id')=? AND json_extract(value,'$.text_sha256')=? LIMIT 2",
            (inventory["thread_id"], identifier, digest),"native_event_work")
        if len(found) > 1:
            raise NativeInflightError("native_retention_event_ambiguous")
        if not found:
            continue  # Native human/Git work need not have a provider reservation.
        namespace, event_key = found[0]
        event = _row(db, namespace, "events", event_key)
        body = json.loads(event["value"])
        rows = [event, _row(db, namespace, "threads", body.get("thread_key"))]
        if body.get("message_key") is not None:
            rows.append(_row(db, namespace, "messages", body["message_key"]))
        receipt = _row(db, namespace, "relay_native_receipts", event_key, required=False)
        if receipt is not None:
            rows.append(receipt)
        for kind, index, predicate, arguments in (
            ("slack_bot_requests", "native_request_event", "namespace=? AND json_extract(value,'$.event_key_digest')=?", (namespace, event_key)),
            ("slack_bot_receipts", "native_ack_event", "json_extract(value,'$.source_namespace')=? AND json_extract(value,'$.event_key_digest')=?", (namespace, event_key)),
        ):
            linked = _indexed_rows(db,"SELECT namespace,key FROM records INDEXED BY " + index +
                " WHERE kind='" + kind + "' AND json_valid(value) AND " + predicate + " LIMIT 3", arguments,index)
            if len(linked) > 2:
                raise NativeInflightError("native_retention_links_ambiguous")
            rows.extend(_row(db, ns, kind, key) for ns, key in linked)
        queued = [item["queue_id"] for item in inventory["queue"]["items"]
                  if _work(item) == (identifier, digest)]
        if len(queued) > 1:
            raise NativeInflightError("native_retention_queue_ambiguous")
        original_queue = json.loads(receipt["value"]).get("receipt", {}).get("queue_message_id") if receipt is not None else None
        if queued and original_queue is not None and queued[0] != original_queue:
            raise NativeInflightError("native_retention_queue_identity_mismatch")
        associations.append(dict(instruction_id=identifier, prompt_sha256=digest,
            queue_id=queued[0] if queued else original_queue,
            refs=[[row[k] for k in ("namespace", "kind", "key")] for row in rows]))
    return associations


class CurrentWorkRetention:
    """Reusable exact-runtime binding; re-create after a selector cutover.

    ``reader`` executes under the caller's held fence. ``fence`` must cover
    provider BEGIN IMMEDIATE, pin writes, prune, body and commit. Existing
    A shared multi-session runtime must expose this only through an exact
    lexical session binding: before_prune restricts deletion to that UUID.
    Never install it as a runtime-wide proof for unrelated ingress or sessions.
    """
    def __init__(self, runtime, home, thread, repo, *, reader, fence, clock=None):
        if not callable(reader) or not callable(fence):
            raise NativeInflightError("native_retention_producer_required")
        self.runtime = Path(runtime).resolve()
        self.runtime_identity = _runtime_identity(self.runtime)
        self.reader, self.fence = reader, fence
        self.pins = CurrentNativePins(self.runtime, home, thread, repo, clock=clock)
        self.last_result = None

    def before_prune(self, journal, db):
        if (journal.database != self.runtime / journal.provider / "reply-tickets.sqlite3"
                or _runtime_identity(self.runtime) != self.runtime_identity
                or _runtime_identity(journal.runtime) != self.runtime_identity
                or _runtime_identity(journal.database.parent.parent) != self.runtime_identity):
            raise NativeInflightError("native_retention_runtime_changed")
        inventory = self.reader()
        scope = self.pins.scope
        validate_queue_inventory(inventory, scope["codex_home"], scope["thread_id"],
                                 scope["repo_path"], clock=self.pins.clock)
        associations = current_associations(db, inventory)
        if _runtime_identity(self.runtime) != self.runtime_identity:
            raise NativeInflightError("native_retention_runtime_changed")
        journal._retention_session = scope["thread_id"]
        registered = 0
        for association in associations:
            self.pins.register(association, inventory, db=db, fence=nullcontext)
            registered += 1
        if inventory["status"] == "complete":
            reconciled = self.pins.reconcile(inventory, db=db, fence=nullcontext)
        else:
            # The additions are positively proved by Q. Their transaction may
            # commit; uncertain native activity still forbids deletion.
            journal.defer_retention()
            reconciled = dict(status="unknown", examined=0, retired=0)
        self.last_result = dict(registered=registered, **reconciled)
        return True
