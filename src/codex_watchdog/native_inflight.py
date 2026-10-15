"""Bounded current native work and exact journal GC pins.

The caller owns native observation, owner and sender fences.  This module never
starts a client or writes a native database.  A missing/partial native read is
unknown, not completion.  Historical receipt status is never a live-work pin.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from .native_rollout import find_native_rollout


PROOF_SECONDS = 5
PAYLOAD_BYTES = 131072
MAX_QUEUE_LIMIT = 16384
MAX_REFERENCES = 32
_INSTRUCTION = re.compile(r"[A-Za-z0-9._:/+-]{1,512}")
_HEADER = re.compile(r"\[CODEX_WATCHDOG_WAKE id=([A-Za-z0-9._:/+-]{1,512}) sha256=([0-9a-f]{64})\]\n([\s\S]*)")


class NativeInflightError(ValueError):
    """Refuse only the current optional inventory/maintenance action."""


def _now():
    return datetime.now(timezone.utc).timestamp()


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise NativeInflightError("native_inflight_duplicate_key")
        value[key] = item
    return value


def _load(raw):
    def bad_constant(_):
        raise NativeInflightError("native_inflight_nonstandard_number")
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_constant=bad_constant)
    except (ValueError, TypeError, RecursionError) as error:
        if isinstance(error, NativeInflightError):
            raise
        raise NativeInflightError("native_inflight_json_invalid") from None


def _scope(home, thread, repo):
    home, repo = Path(home), Path(repo)
    if (not _uuid(thread) or not home.is_absolute() or not repo.is_absolute()
            or home.resolve() != home or repo.resolve() != repo
            or not home.is_dir() or not repo.is_dir()):
        raise NativeInflightError("native_inflight_scope_invalid")
    return dict(codex_home=str(home), thread_id=thread, repo_path=str(repo))


def _owner(value):
    if (not isinstance(value, dict) or set(value) != {"node", "boot_id", "epoch", "instance"}
            or any(not isinstance(value.get(k), str) or not 1 <= len(value[k]) <= 256
                   or any(c in value[k] for c in ("\0", "\n", "/", "\\"))
                   for k in ("node", "boot_id", "instance"))
            or type(value.get("epoch")) is not int or value["epoch"] < 0):
        raise NativeInflightError("native_inflight_owner_unverified")
    return dict(value)


def _input(text, *, terminal_lf=False):
    if not isinstance(text, str) or len(text.encode("utf-8")) > PAYLOAD_BYTES:
        raise NativeInflightError("native_inflight_input_unsupported")
    match = _HEADER.fullmatch(text)
    if match is None:
        if "[CODEX_WATCHDOG_WAKE" in text:
            raise NativeInflightError("native_inflight_header_invalid")
        return dict(instruction_id=None, prompt_sha256=None, input_sha256=_sha(text))
    if (_sha(match[3]) != match[2]
            and not (terminal_lf and match[3].endswith("\n") and _sha(match[3][:-1]) == match[2])):
        raise NativeInflightError("native_inflight_prompt_mismatch")
    return dict(instruction_id=match[1], prompt_sha256=match[2], input_sha256=_sha(text))


def _parts(parts, *, terminal_lf=False):
    if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
            or parts[0].get("type") != "text" or not isinstance(parts[0].get("text"), str)):
        raise NativeInflightError("native_inflight_input_unsupported")
    return _input(parts[0]["text"], terminal_lf=terminal_lf)


def _native(proof, scope, current):
    """Interpret truthful RPC or separately tagged observed stream evidence.

    ``current_input`` is a positively observed same-turn native UserMessage
    supplied by the already-owned native actor. A bounded, owner-continuous
    stream producer has its own explicit validator and never emits thread_read.
    An actor without supported input evidence returns unknown for active work.
    """
    if isinstance(proof,dict) and proof.get("kind")=="native_observed_writer_stream_work":
        from .native_observed_work import validate_observed_work
        return validate_observed_work(proof,scope,current)
    if isinstance(proof,dict) and proof.get("kind")=="native_rollout_stream_work":
        from .native_stream_work import validate_stream_work
        return validate_stream_work(proof,scope,current)
    if (not isinstance(proof, dict) or any(proof.get(k) != v for k, v in scope.items())
            or not _number(proof.get("observed_at"))
            or not 0 <= current - proof["observed_at"] <= PROOF_SECONDS):
        raise NativeInflightError("native_inflight_current_evidence_unverified")
    owner = _owner(proof.get("owner"))
    response = proof.get("thread_read")
    thread = response.get("thread") if isinstance(response, dict) else None
    if (not isinstance(thread, dict) or thread.get("id") != scope["thread_id"]
            or thread.get("cwd") != scope["repo_path"]):
        raise NativeInflightError("native_inflight_thread_mismatch")
    status = thread.get("status")
    status = status.get("type") if isinstance(status, dict) else None
    turn = proof.get("current_turn")
    event = proof.get("current_input")
    if status == "idle" and turn is None and event is None:
        return owner, dict(status="idle", turn_id=None, instruction_id=None,
                           prompt_sha256=None, input_sha256=None)
    if status != "active":
        raise NativeInflightError("native_inflight_current_turn_unknown")
    unknown = dict(status="active", turn_id=None, instruction_id=None,
                   prompt_sha256=None, input_sha256=None,
                   reason="native_inflight_current_turn_unknown")
    if not isinstance(turn, dict) or turn.get("status") != "inProgress" or not _uuid(turn.get("id")):
        return owner, unknown
    stream=proof.get("active_input_proof")
    if stream is not None:
        from .native_stream_work import validate_stream_work
        try:
            stream_owner,native=validate_stream_work(stream,scope,current)
        except NativeInflightError:
            stream_owner,native=None,{}
        if stream_owner==owner and native.get("status")=="active" and native.get("turn_id")==turn["id"]:
            return owner,dict(native,evidence_kind="app_server_rpc+native_rollout_stream")
        unknown["turn_id"]=turn["id"]
        return owner,unknown
    payload = event.get("payload") if isinstance(event, dict) and event.get("type") == "event_msg" else None
    item = payload.get("item") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("thread_id") != scope["thread_id"]
            or payload.get("turn_id") != turn["id"] or payload.get("type") != "item_completed"
            or not isinstance(item, dict) or item.get("type") != "UserMessage"):
        unknown["turn_id"] = turn["id"]
        return owner, unknown
    return owner, dict(status="active", turn_id=turn["id"], **_parts(item.get("content"), terminal_lf=True))


def _queue_path(home):
    # Inspect at most two names: no historical directory enumeration.
    from itertools import islice
    paths = list(islice(home.glob("queue_*.sqlite"), 2))
    if len(paths) != 1 or paths[0] != home / "queue_1.sqlite":
        raise NativeInflightError("native_inflight_queue_unavailable")
    path = paths[0]
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise NativeInflightError("native_inflight_queue_unavailable")
    return path


def _indexed(db, query, parameters, expected):
    plan = db.execute("EXPLAIN QUERY PLAN " + query, parameters).fetchall()
    if (not any("SEARCH" in row[3] and expected in row[3] for row in plan)
            or any("SCAN" in row[3] or "TEMP B-TREE" in row[3] for row in plan)):
        raise NativeInflightError("native_inflight_index_required")


def _revision(db, thread):
    query = "SELECT revision FROM queued_thread_revisions WHERE thread_id=?"
    _indexed(db, query, (thread,), "thread_id")
    row = db.execute(query, (thread,)).fetchone()
    if row is None:
        return None
    if type(row[0]) is not int or row[0] < 0:
        raise NativeInflightError("native_inflight_queue_revision_invalid")
    return row[0]


def _sign(value):
    value["proof_sha256"] = _sha(_json({k: v for k, v in value.items() if k != "proof_sha256"}))
    return value


def collect_native_work(home, thread, repo, *, current_native, fence, clock=None, queue_limit=1024):
    """Read a fixed, indexed current queue snapshot and fresh native turn.

    ``fence`` is the existing observation/owner/sender context, not a new lease.
    ``current_native`` reads the already-owned actor or the explicit current
    stream producer described in ``_native``. It must not start/resume a client.
    A missing stream may still supply independently verified positive Q; it
    cannot establish retirement. Q+1 overflow refuses retirement rather
    than imposing a work/session cap.  No returned item contains prompt text.
    """
    clock = _now if clock is None else clock
    value = dict(schema_version=1, kind="native_current_work", status="unknown",
                 codex_home=str(home), thread_id=thread, repo_path=str(repo), owner=None,
                 observed_at=clock(), queue=dict(complete=False, revision=None, items=[]),
                 native=None, reason="native_inflight_unknown")
    try:
        scope = _scope(home, thread, repo)
        value.update(scope)
        if type(queue_limit) is not int or not 1 <= queue_limit <= MAX_QUEUE_LIMIT or not callable(current_native) or not callable(fence):
            raise NativeInflightError("native_inflight_arguments_invalid")
        with fence():
            before = _native(current_native(), scope, clock())
            value["owner"], value["native"] = before
            rollout = find_native_rollout(Path(home), thread, Path(repo))
            stream_evidence=before[1].get("evidence_kind") in ("native_rollout_stream", "observed_native_writer_stream")
            if stream_evidence:
                from .native_stream_work import stream_exact_thread
                exact=stream_exact_thread(Path(home),thread,Path(repo))
            else:
                exact=rollout is not None
            if not exact:
                raise NativeInflightError("native_inflight_exact_thread_unavailable")
            path = _queue_path(Path(home))
            original = path.stat()
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
                db.execute("PRAGMA query_only=ON")
                # A VM ceiling also bounds unsupported/corrupt query behavior.
                steps = [0]
                def progress():
                    steps[0] += 1
                    return int(steps[0] > max(1000, queue_limit * 50))
                db.set_progress_handler(progress, 100)
                db.execute("BEGIN")
                columns = db.execute("PRAGMA table_info(queued_items)").fetchmany(65)
                names = {row[1] for row in columns}
                if len(columns) > 64 or not {"id", "thread_id", "payload_json"}.issubset(names):
                    raise NativeInflightError("native_inflight_queue_schema_unsupported")
                order = " ORDER BY queue_order" if "queue_order" in names else ""
                query = ("SELECT id,CASE WHEN length(CAST(payload_json AS BLOB))<=? THEN payload_json ELSE NULL END "
                         "FROM queued_items WHERE thread_id=?" + order + " LIMIT ?")
                args = (PAYLOAD_BYTES, thread, queue_limit + 1)
                _indexed(db, query, args, "thread_id")
                revision = _revision(db, thread)
                rows = db.execute(query, args).fetchall()
                value["queue"]["revision"] = revision
                if len(rows) > queue_limit:
                    raise NativeInflightError("native_inflight_queue_budget")
                if rows and revision is None:
                    raise NativeInflightError("native_inflight_queue_revision_invalid")
                seen, work_seen = set(), set()
                for queue_id, raw in rows:
                    if not _uuid(queue_id) or queue_id in seen or not isinstance(raw, str):
                        raise NativeInflightError("native_inflight_queue_payload_unsupported")
                    seen.add(queue_id)
                    payload = _load(raw)
                    user = payload.get("UserInput") if isinstance(payload, dict) else None
                    if not isinstance(user, dict):
                        raise NativeInflightError("native_inflight_queue_payload_unsupported")
                    identity = _parts(user.get("content"))
                    work = _work(identity)
                    if work is not None and work in work_seen:
                        raise NativeInflightError("native_inflight_queue_identity_ambiguous")
                    work_seen.add(work)
                    value["queue"]["items"].append(dict(queue_id=queue_id, payload_sha256=_sha(raw), **identity))
                db.commit()
                after = _native(current_native(), scope, clock())
                if before != after or _revision(db, thread) != revision:
                    raise NativeInflightError("native_inflight_work_changed")
                if stream_evidence and not stream_exact_thread(Path(home),thread,Path(repo)):
                    raise NativeInflightError("native_inflight_exact_thread_unavailable")
                latest = path.stat()
                if ((latest.st_dev, latest.st_ino) != (original.st_dev, original.st_ino)
                        or _queue_path(Path(home)) != path
                        or find_native_rollout(Path(home), thread, Path(repo)) != rollout):
                    raise NativeInflightError("native_inflight_work_changed")
            value.update(status="unknown" if value["native"].get("reason") else "complete",
                         observed_at=clock(), reason=value["native"].get("reason"))
            value["queue"]["complete"] = True
    except Exception as error:
        value["reason"] = str(error) if isinstance(error, NativeInflightError) else "native_inflight_evidence_unavailable"
    return _sign(value)


def _validate_inventory(inventory, home, thread, repo, *, clock=None, queue_only=False):
    scope = _scope(home, thread, repo)
    current = (_now if clock is None else clock)()
    value = inventory
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("kind") != "native_current_work"
            or value.get("status") not in (("complete", "unknown") if queue_only else ("complete",))
            or any(value.get(k) != v for k, v in scope.items())
            or not _number(value.get("observed_at")) or not _number(current)
            or not 0 <= current - value["observed_at"] <= PROOF_SECONDS
            or not queue_only and value.get("reason") is not None
            or not _digest(value.get("proof_sha256"))
            or value["proof_sha256"] != _sha(_json({k: v for k, v in value.items() if k != "proof_sha256"}))):
        raise NativeInflightError("native_inflight_complete_proof_required")
    queue, native = value.get("queue"), value.get("native")
    owner = value.get("owner")
    observed = isinstance(owner, dict) and owner.get("kind") == "observed_native_writer"
    if observed:
        from .native_observed_work import validate_writer
        validate_writer(owner, scope)
    else:
        _owner(owner)
    if observed != (isinstance(native, dict) and native.get("evidence_kind") == "observed_native_writer_stream"):
        raise NativeInflightError("native_inflight_owner_evidence_mismatch")
    if (not isinstance(queue, dict) or queue.get("complete") is not True
            or queue.get("revision") is not None and (type(queue["revision"]) is not int or queue["revision"] < 0)
            or not isinstance(queue.get("items"), list) or len(queue["items"]) > MAX_QUEUE_LIMIT
            or not isinstance(native, dict) or native.get("status") not in (("idle", "active","unknown") if queue_only else ("idle","active"))):
        raise NativeInflightError("native_inflight_inventory_invalid")
    if (queue["items"] and queue["revision"] is None
            or queue_only and value["status"] == "unknown" and (
                native.get("status") not in ("active","unknown")
                or native.get("reason") != "native_inflight_current_turn_unknown"
                or value.get("reason") != native.get("reason"))):
        raise NativeInflightError("native_inflight_inventory_invalid")
    seen = set()
    for item in queue["items"]:
        if (not isinstance(item, dict) or not _uuid(item.get("queue_id")) or item["queue_id"] in seen
                or not _digest(item.get("payload_sha256")) or not _digest(item.get("input_sha256"))):
            raise NativeInflightError("native_inflight_inventory_invalid")
        _work(item)
        seen.add(item["queue_id"])
    if native["status"] == "idle":
        if any(native.get(k) is not None for k in ("turn_id", "instruction_id", "prompt_sha256", "input_sha256")):
            raise NativeInflightError("native_inflight_inventory_invalid")
    elif queue_only and value["status"] == "unknown" and native.get("reason") == "native_inflight_current_turn_unknown":
        if (native.get("turn_id") is not None and not _uuid(native["turn_id"])
                or any(native.get(k) is not None for k in ("instruction_id", "prompt_sha256", "input_sha256"))):
            raise NativeInflightError("native_inflight_inventory_invalid")
        if native.get("status")=="unknown" and (native.get("turn_id") is not None or native.get("evidence_kind") not in ("native_rollout_stream", "observed_native_writer_stream")):
            raise NativeInflightError("native_inflight_inventory_invalid")
    elif not _uuid(native.get("turn_id")) or not _digest(native.get("input_sha256")):
        raise NativeInflightError("native_inflight_inventory_invalid")
    _work(native)
    return value


def validate_inventory(inventory, home, thread, repo, *, clock=None):
    """Validate a fresh COMPLETE proof, not an authorization or completion."""
    return _validate_inventory(inventory, home, thread, repo, clock=clock)


def validate_queue_inventory(inventory, home, thread, repo, *, clock=None):
    """Positive Q-only pin additions; never permission to retire any pin."""
    return _validate_inventory(inventory, home, thread, repo, clock=clock, queue_only=True)


def _work(item):
    identifier, digest = item.get("instruction_id"), item.get("prompt_sha256")
    if identifier is None and digest is None:
        return None
    if not isinstance(identifier, str) or _INSTRUCTION.fullmatch(identifier) is None or not _digest(digest):
        raise NativeInflightError("native_inflight_inventory_invalid")
    return identifier, digest


def _live_work(inventory):
    work = {_work(item) for item in inventory["queue"]["items"]}
    work.add(_work(inventory["native"]))
    work.discard(None)
    return work


def resolve_snapshot_pins(snapshot, inventory, *, queue_only=False, work=None):
    """Cold-migration ONLY: join exported rows once to actual current Q/turn.

    Ordinary callers register already-known PKs with CurrentNativePins instead.
    Receipt uncertainty/active flags by themselves never add a pin.  Raw source
    rows are not modified; only exact journal PK triples are returned.
    """
    validate = validate_queue_inventory if queue_only else validate_inventory
    validate(inventory, inventory["codex_home"], inventory["thread_id"], inventory["repo_path"],
             clock=lambda: inventory["observed_at"])
    rows = snapshot.get("records", snapshot.get("rows")) if isinstance(snapshot, dict) else snapshot
    if not isinstance(rows, (list, tuple)):
        raise NativeInflightError("native_inflight_snapshot_invalid")
    by_key, bodies = {}, {}
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in ("namespace", "kind", "key", "value")):
            raise NativeInflightError("native_inflight_snapshot_invalid")
        key = (row["namespace"], row["kind"], row["key"])
        if key in by_key:
            raise NativeInflightError("native_inflight_snapshot_invalid")
        by_key[key] = row
        try:
            body = _load(row["value"])
        except (ValueError, RecursionError):
            body = None
        bodies[key] = body if isinstance(body, dict) else {}
    thread, live, selected = inventory["thread_id"], _live_work(inventory), set()
    if work is not None:
        if not isinstance(work, tuple) or len(work) != 2 or work not in live:
            raise NativeInflightError("native_inflight_work_not_current")
        live = {work}
    events = set()
    for key, row in by_key.items():
        body = bodies[key]
        if key[1] != "events" or (body.get("instruction_id"), body.get("text_sha256")) not in live:
            continue
        parent_key = (key[0], "threads", body.get("thread_key"))
        parent = bodies.get(parent_key, {})
        target = parent.get("target")
        if (not isinstance(target, dict) or target.get("thread_id") != thread
                or row.get("thread_id") not in (None, thread)):
            raise NativeInflightError("native_inflight_event_scope_mismatch")
        selected.update((key, parent_key))
        events.add((key[0], key[2]))
        physical = body.get("message_key")
        if physical is not None:
            physical_key = (key[0], "messages", physical)
            message = bodies.get(physical_key)
            if not isinstance(message, dict) or message.get("event_key") != key[2]:
                raise NativeInflightError("native_inflight_reference_invalid")
            selected.add(physical_key)
        receipt_key = (key[0], "relay_native_receipts", key[2])
        if receipt_key in by_key:
            receipt = bodies[receipt_key].get("receipt")
            if (not isinstance(receipt, dict) or receipt.get("thread_id") != thread
                    or (receipt.get("instruction_id"), receipt.get("prompt_sha256")) not in live):
                raise NativeInflightError("native_inflight_reference_invalid")
            selected.add(receipt_key)
    for key, body in bodies.items():
        if key[1] == "slack_bot_requests" and (key[0], body.get("event_key_digest")) in events:
            target = body.get("target")
            event = bodies[(key[0], "events", body["event_key_digest"])]
            if (not isinstance(target, dict) or target.get("thread_id") != thread
                    or body.get("message_key_digest") != event.get("message_key")):
                raise NativeInflightError("native_inflight_reference_invalid")
            selected.add(key)
    for key, body in bodies.items():
        if key[1] == "slack_bot_receipts":
            proof = body.get("proof")
            target = body.get("target")
            digest = proof.get("event_key_digest") if isinstance(proof, dict) else None
            namespace = body.get("source_namespace")
            if (isinstance(target, dict) and target.get("thread_id") == thread
                    and (namespace, digest) in events):
                event = bodies[(namespace, "events", digest)]
                if (body.get("event_key_digest") != digest
                        or body.get("message_key_digest") != event.get("message_key")
                        or proof.get("message_key_digest") != event.get("message_key")
                        or (namespace, "slack_bot_requests", proof.get("request_key")) not in selected):
                    raise NativeInflightError("native_inflight_reference_invalid")
                selected.add(key)
    return selected


pins_for_snapshot = resolve_snapshot_pins
