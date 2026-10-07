"""One-time, read-only relay snapshots and conservative authority merging.

These helpers do not activate an authority, select an execution node, or import
on handoff. The caller must fence old listeners and hold the shared authority
lock before publishing a completed import. Native owners, queues and process
state are deliberately outside this module.
"""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import uuid

from .reply_tickets import ACTIVE_TICKET_LIMIT
from .lark_transport import valid_id
from .slack_bot_identity import BotPrincipal
from .slack_mapping import valid_slack_timestamp


_FIELDS = ("namespace", "kind", "key", "value", "fingerprint", "thread_id",
           "active", "created_at")
_IDENTITY_FIELDS = ("fingerprint", "thread_id", "created_at")
_INVALID = "relay_authority_migration_record_invalid"
_CONFLICT = "relay_authority_migration_record_conflict"
_HISTORIC_QUARANTINE = frozenset(("threads", "notifications"))


def _pending(value):
    return (value.get("state") in ("dispatching", "uncertain")
            or value.get("delivery_status") in ("enqueued", "consumed_or_started", "started")
            and value.get("native_completed") is not True)


def _quarantined(value):
    return value.get("relay_authority_quarantine") is True


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_INVALID)
        result[key] = value
    return result


def _load(value):
    def invalid_constant(_):
        raise ValueError(_INVALID)
    try:
        result = json.loads(value, object_pairs_hook=_object,
                            parse_constant=invalid_constant)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (TypeError, ValueError, UnicodeError):
        raise ValueError(_INVALID) from None


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(_INVALID) from None


def _uuid(value):
    try:
        result = str(uuid.UUID(value))
        if result != value.lower():
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError(_INVALID) from None


def _principals(value):
    if not isinstance(value, list):
        raise ValueError(_INVALID)
    try:
        keys = [_json(BotPrincipal.from_dict(entry).to_dict()) for entry in value]
    except (ValueError, TypeError):
        raise ValueError(_INVALID) from None
    if keys != sorted(set(keys)):
        raise ValueError(_INVALID)
    return {key: entry for key, entry in zip(keys, value)}


def _validate_registry(value):
    if (type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("provider") != "slack"
            or not isinstance(value.get("created_at"), str)):
        raise ValueError(_INVALID)
    _stamp(value["created_at"])
    principals = _principals(value.get("principals"))
    if not principals or any(entry["user_id"] != value.get("user_id")
                             for entry in principals.values()):
        raise ValueError(_INVALID)
    return principals


def _validate_binding_row(row, body):
    """Preserve the numeric epochs emitted by the existing binding journal."""
    from .binding_challenges import BindingChallenges

    try:
        stamp = row["created_at"]
        if (not isinstance(stamp, str)
                or re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", stamp) is None
                or not math.isfinite(float(stamp))):
            raise ValueError()
        provider, scope = body.get("provider"), body.get("scope")
        bindings = BindingChallenges(SimpleNamespace(provider=provider, scope=scope), provider, scope)
        if row["kind"] == "bind_challenges":
            bindings._validate_challenge(body)
            expected_key = bindings.routes._route_key(body["thread_id"])
        else:
            bindings._validate_operation(body)
            expected_key = body["op_key"]
        if (row["namespace"] != provider + "/" + scope + "/relay-state.json"
                or row["key"] != expected_key
                or _uuid(row["thread_id"]) != _uuid(body["thread_id"])):
            raise ValueError()
        # SQLite converts numeric epochs to TEXT with its own precision. Also,
        # an upsert retains the first column timestamp while later challenge
        # generations update the payload epoch. Neither is a new identity.
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError(_INVALID) from None


def _row(value):
    if not isinstance(value, dict) or set(value) != set(_FIELDS):
        raise ValueError(_INVALID)
    result = dict(value)
    for field in ("namespace", "kind", "key"):
        if (not isinstance(result[field], str) or not result[field]
                or "\0" in result[field]):
            raise ValueError(_INVALID)
    if not isinstance(result["value"], str):
        raise ValueError(_INVALID)
    body = _load(result["value"])
    for field in ("fingerprint", "thread_id", "created_at"):
        if result[field] is not None and not isinstance(result[field], str):
            raise ValueError(_INVALID)
    if (result["fingerprint"] is not None
            and (len(result["fingerprint"]) != 64
                 or any(c not in "0123456789abcdef" for c in result["fingerprint"]))):
        raise ValueError(_INVALID)
    if result["thread_id"] is not None:
        _uuid(result["thread_id"])
    if result["kind"] in ("bind_challenges", "bind_operations"):
        _validate_binding_row(result, body)
    elif result["created_at"] is not None:
        _stamp(result["created_at"])
    if type(result["active"]) is not int or result["active"] not in (0, 1):
        raise ValueError(_INVALID)
    if result["active"] and (result["kind"] not in ("threads", "events")
                             or result["thread_id"] is None
                             or result["created_at"] is None):
        raise ValueError(_INVALID)
    if result["kind"] == "events" and result["active"] and not _pending(body):
        raise ValueError(_INVALID)
    if _quarantined(body):
        variants = body.get("variant_sha256")
        if (result["kind"] not in _HISTORIC_QUARANTINE or result["active"] != 0
                or set(body) != {"schema_version", "relay_authority_quarantine", "variant_sha256"}
                or type(body.get("schema_version")) is not int or body["schema_version"] != 3
                or not isinstance(variants, list) or not variants
                or any(not _cursor_digest(value) for value in variants)
                or variants != sorted(set(variants))
                or result["kind"] == "threads" and result["thread_id"] is None):
            raise ValueError(_INVALID)
        result["value"] = _json(body)
        return result
    if result["kind"] == "slack_bot_users":
        _validate_registry(body)
    if result["kind"] == "threads":
        target = body.get("target")
        if (not isinstance(target, dict) or result["thread_id"] is None
                or _uuid(target.get("thread_id")) != _uuid(result["thread_id"])
                or body.get("event_fingerprint") != result["fingerprint"]
                or body.get("created_at") != result["created_at"]):
            raise ValueError(_INVALID)
    result["value"] = _json(body)
    return result


def _compatible(left, right):
    """Preserve extra fields; never select a winner for conflicting fields."""
    result = dict(left)
    for key, value in right.items():
        if key in result and result[key] != value:
            raise ValueError(_CONFLICT)
        result[key] = value
    return result


def _merge(left, right):
    identity_fields = _IDENTITY_FIELDS
    if left["kind"] == "events" and (left["thread_id"] is None or right["thread_id"] is None):
        identity_fields = tuple(name for name in identity_fields if name != "thread_id")
    if any(left[name] != right[name] for name in identity_fields):
        raise ValueError(_CONFLICT)
    a, b = _load(left["value"]), _load(right["value"])
    if left["kind"] == "slack_bot_users":
        principals = _validate_registry(a)
        principals.update(_validate_registry(b))
        a.pop("principals")
        b.pop("principals")
        body = _compatible(a, b)
        body["principals"] = [principals[key] for key in sorted(principals)]
    else:
        # In particular, grant/revocation differences and uncertain/terminal
        # receipt differences cannot be resolved using host clocks or ordering.
        body = _compatible(a, b)
    result = dict(left, value=_json(body), active=left["active"] & right["active"])
    if left["kind"] == "events" and result["thread_id"] is None:
        result["thread_id"] = right["thread_id"]
    return result


def merge_records(sources):
    """Merge ``(source_id, rows)`` snapshots without replay or grant widening.

    Duplicate permission or receipt conflicts fail closed. A missing row in
    another source is absence, not revocation. Present closed tickets dominate
    active copies, and merged active tickets retain the last four per provider
    and exact session. Import order and hostname never choose a winner.
    """
    result, source_ids = {}, set()
    for source_id, rows in sources:
        if not isinstance(source_id, str) or not source_id or source_id in source_ids:
            raise ValueError("relay_authority_migration_source_invalid")
        source_ids.add(source_id)
        seen = set()
        for raw in rows:
            row = _row(raw)
            key = tuple(row[name] for name in ("namespace", "kind", "key"))
            if key in seen:
                raise ValueError("relay_authority_migration_source_invalid")
            seen.add(key)
            result[key] = _merge(result[key], row) if key in result else row
    # The old journal stores event session identity only through its immutable
    # parent mapping. Recover that portable identity for the shared pending
    # index, and never let an old active copy resurrect a claimed parent.
    for entry in result.values():
        if entry["kind"] != "events":
            continue
        value = _load(entry["value"])
        parent = value.get("thread_key")
        if parent is None:
            continue
        ticket = result.get((entry["namespace"], "threads", parent))
        if ticket is None:
            raise ValueError("relay_authority_migration_reference_invalid")
        if (entry["thread_id"] is not None
                and _uuid(entry["thread_id"]) != _uuid(ticket["thread_id"])):
            raise ValueError(_CONFLICT)
        entry["thread_id"] = ticket["thread_id"]
        entry["active"] = int(_pending(value))
        ticket["active"] = 0
    active = {}
    for row in result.values():
        if row["kind"] == "threads" and row["active"]:
            scope = (row["namespace"].split("/", 1)[0], _uuid(row["thread_id"]))
            active.setdefault(scope, []).append(row)
    for tickets in active.values():
        tickets.sort(key=lambda row: (_stamp(row["created_at"]), row["key"], row["namespace"]),
                     reverse=True)
        for row in tickets[ACTIVE_TICKET_LIMIT:]:
            row["active"] = 0
    return [result[key] for key in sorted(result)]


def _route_metadata_merge(copies, groups):
    values = [_load(entry["value"]) for entry in copies]
    first = values[0]
    if (first.get("provider") != "slack" or first.get("schema_version") != 1
            or not valid_slack_timestamp(first.get("last_command_ts"))):
        raise ValueError(_CONFLICT)
    semantic = {key: value for key, value in first.items() if key not in ("created_at", "last_command_ts")}
    for row, value in zip(copies, values):
        if (not valid_slack_timestamp(value.get("last_command_ts"))
                or {key: item for key, item in value.items() if key not in ("created_at", "last_command_ts")} != semantic
                or row["fingerprint"] != copies[0]["fingerprint"]
                or row["thread_id"] != copies[0]["thread_id"]
                or row["active"] != copies[0]["active"]):
            raise ValueError(_CONFLICT)
        _stamp(value.get("created_at"))
    canonical = _uuid(first.get("thread_id"))
    highwater = max(Decimal(value["last_command_ts"]) for value in values)
    selected_ts = min(value["last_command_ts"] for value in values
                      if Decimal(value["last_command_ts"]) == highwater)
    # A stale-command floor requires a retained actual route-command receipt
    # plus its one-shot physical-message identity, not a local updated_at.
    from .session_routes import SessionRoutes
    from .models import sha256_text
    routes = SessionRoutes(None, scope=first.get("scope"))
    try:
        for value in values:
            routes._validate_route(value, canonical)
    except ValueError:
        raise ValueError(_CONFLICT) from None
    if copies[0]["key"] != routes._route_key(canonical):
        raise ValueError(_CONFLICT)
    proved = False
    namespace = copies[0]["namespace"]
    for (candidate_namespace, kind, key), commands in groups.items():
        if candidate_namespace != namespace or kind != "route_commands":
            continue
        for command_row in commands:
            command = _load(command_row["value"])
            try:
                routes._validate_command(command)
            except ValueError:
                continue
            if (command.get("stale", False) or command["thread_id"] != canonical
                    or command["destination"] != first.get("destination")
                    or Decimal(command["command_ts"]) != highwater):
                continue
            physical_key = sha256_text(command["channel_id"] + "\0" + command["command_ts"])
            physical = groups.get((namespace, "route_messages", physical_key), [])
            if any(_load(entry["value"]).get("command_key") == key
                   and _load(entry["value"]).get("schema_version") == 1
                   and _cursor_digest(_load(entry["value"]).get("payload_sha256"))
                   for entry in physical):
                proved = True
    if not proved:
        raise ValueError(_CONFLICT)
    merged = dict(first, created_at=min((value["created_at"] for value in values), key=_stamp),
                  last_command_ts=selected_ts)
    column_stamps = [entry["created_at"] for entry in copies if entry["created_at"] is not None]
    return dict(copies[0], value=_json(merged),
                created_at=min(column_stamps, key=_stamp) if column_stamps else None)


def _historic_quarantine(copies):
    identity = copies[0]
    digests, variants, sessions = set(), {}, set()
    for row in copies:
        value = _load(row["value"])
        if row["thread_id"] is not None:
            sessions.add(_uuid(row["thread_id"]))
        if _quarantined(value):
            digests.update(value["variant_sha256"])
        else:
            digest = hashlib.sha256(_json(row).encode("utf-8")).hexdigest()
            digests.add(digest)
            variants[digest] = row
            target = value.get("target")
            if isinstance(target, dict) and target.get("thread_id") is not None:
                sessions.add(_uuid(target["thread_id"]))
    if len(sessions) > 1 or identity["kind"] == "threads" and len(sessions) != 1:
        raise ValueError(_CONFLICT)
    marker = dict(schema_version=3, relay_authority_quarantine=True, variant_sha256=sorted(digests))
    projection = dict(identity, value=_json(marker), active=0, fingerprint=None,
                      thread_id=next(iter(sessions)) if sessions else None, created_at=None)
    audit = dict(namespace=identity["namespace"], kind=identity["kind"], key=identity["key"],
                 variant_sha256=sorted(digests), variants=[variants[digest] for digest in sorted(variants)])
    return projection, audit


def merge_records_with_quarantine(sources):
    """Merge once, retiring only ambiguous historic parents/notifications.

    Inert markers occupy the original identity so indexed presence checks never
    classify it as fresh. Original variants remain in the returned private audit
    and must be retained in immutable activation backups. Grants, UUIDs, physical
    claims and unknown kinds keep strict conflict behavior.
    """
    groups, source_ids = {}, set()
    for source_id, rows in sources:
        if not isinstance(source_id, str) or not source_id or source_id in source_ids:
            raise ValueError("relay_authority_migration_source_invalid")
        source_ids.add(source_id)
        seen = set()
        for raw in rows:
            row = _row(raw)
            identity = tuple(row[name] for name in ("namespace", "kind", "key"))
            if identity in seen:
                raise ValueError("relay_authority_migration_source_invalid")
            seen.add(identity)
            groups.setdefault(identity, []).append(row)
    rows, quarantines = [], []
    for identity in sorted(groups):
        copies = groups[identity]
        if any(_quarantined(_load(row["value"])) for row in copies):
            merged, audit = _historic_quarantine(copies)
            quarantines.append(audit)
        else:
            try:
                merged = copies[0]
                for row in copies[1:]:
                    merged = _merge(merged, row)
            except ValueError as error:
                if str(error) != _CONFLICT:
                    raise
                if identity[1] in _HISTORIC_QUARANTINE:
                    merged, audit = _historic_quarantine(copies)
                    quarantines.append(audit)
                elif identity[1] == "session_routes":
                    merged = _route_metadata_merge(copies, groups)
                else:
                    raise
        rows.append(merged)
    return merge_records([("completed-import", rows)]), quarantines


def read_snapshot(database):
    """Return ``(rows, sha256)`` from a verified read-only SQLite transaction.

    The digest identifies the complete logical snapshot, including committed
    WAL state. It is not a main-file byte hash. Snapshot/import belongs to the
    one-time activation gate, never the ingress or handoff hot path.
    """
    path = Path(database).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("relay_authority_migration_snapshot_invalid")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != 2 or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError()
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables != {"records", "sources"}:
                raise ValueError()
            columns = db.execute("PRAGMA table_info(records)").fetchall()
            if (tuple(row[1] for row in columns) != _FIELDS
                    or [row[2].upper() for row in columns] != [
                        "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "INTEGER", "TEXT"]
                    or [row[3] for row in columns] != [1, 1, 1, 1, 0, 0, 1, 0]
                    or [row[5] for row in columns] != [1, 2, 3, 0, 0, 0, 0, 0]):
                raise ValueError()
            source_columns = db.execute("PRAGMA table_info(sources)").fetchall()
            if (len(source_columns) != 1 or source_columns[0][1] != "namespace"
                    or source_columns[0][2].upper() != "TEXT" or source_columns[0][5] != 1):
                raise ValueError()
            namespaces = [row[0] for row in db.execute("SELECT namespace FROM sources ORDER BY namespace")]
            if any(not isinstance(value, str) or not value or "\0" in value for value in namespaces):
                raise ValueError()
            rows = [_row(dict(zip(_FIELDS, values))) for values in db.execute(
                "SELECT " + ",".join(_FIELDS) + " FROM records ORDER BY namespace,kind,key")]
            payload = _json(dict(schema_version=version, namespaces=namespaces, rows=rows))
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            return rows, digest
    except (OSError, sqlite3.Error, ValueError, TypeError, IndexError):
        raise ValueError("relay_authority_migration_snapshot_invalid") from None


_CURSOR_INVALID = "relay_authority_migration_cursor_invalid"
_CURSOR_CONFLICT = "relay_authority_migration_cursor_conflict"


def _cursor_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _cursor_header(value, fields, scheduling):
    if (not isinstance(value, dict) or not set(fields).issubset(value)
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or (value.get(scheduling) is not None and not _cursor_digest(value[scheduling]))):
        raise ValueError(_CURSOR_INVALID)


def _cursor_extra(copies, known):
    result = {}
    for value in copies:
        for key, item in value.items():
            if key in known:
                continue
            if key in result and result[key] != item:
                raise ValueError(_CURSOR_CONFLICT)
            result[key] = item
    return result


def _scheduling(copies, key):
    values = [value[key] for value in copies if value[key] is not None]
    return min(values) if values else None


def _lark_parent(value):
    fields = {"not_before_ms", "after_ms", "seen_ids", "thread_id", "pending"}
    if (not isinstance(value, dict) or not fields.issubset(value)
            or any(type(value.get(name)) is not int or value[name] < 0
                   for name in ("not_before_ms", "after_ms"))
            or value["after_ms"] < value["not_before_ms"]
            or (value["thread_id"] is not None and not (
                valid_id(value["thread_id"], "omt") or valid_id(value["thread_id"], "om")))
            or not isinstance(value["seen_ids"], list) or len(value["seen_ids"]) > 50
            or any(not valid_id(mid, "om") for mid in value["seen_ids"])
            or len(set(value["seen_ids"])) != len(value["seen_ids"])):
        raise ValueError(_CURSOR_INVALID)
    pending = value["pending"]
    if pending is not None and (not isinstance(pending, dict)
            or not valid_id(pending.get("message_id"), "om")
            or type(pending.get("create_time")) is not int
            or pending["create_time"] < value["after_ms"]
            or not _cursor_digest(pending.get("sha256"))
            or (pending["create_time"] == value["after_ms"]
                and pending["message_id"] in value["seen_ids"])):
        raise ValueError(_CURSOR_INVALID)


def _merge_lark_parent(copies):
    for value in copies:
        _lark_parent(value)
    thread_ids = {value["thread_id"] for value in copies if value["thread_id"] is not None}
    if len(thread_ids) > 1:
        raise ValueError(_CURSOR_CONFLICT)
    highwater = max(value["after_ms"] for value in copies)
    seen = sorted({mid for value in copies if value["after_ms"] == highwater
                   for mid in value["seen_ids"]})
    if len(seen) > 50:
        raise ValueError(_CURSOR_CONFLICT)
    pending_copies = [value["pending"] for value in copies if value["pending"] is not None]
    pending = None
    if pending_copies:
        identities = {(value["message_id"], value["create_time"], value["sha256"])
                      for value in pending_copies}
        if len(identities) != 1:
            raise ValueError(_CURSOR_CONFLICT)
        candidate = _cursor_extra(pending_copies, set())
        # A confirmed later high-water is never rewound to an older pending
        # observation. A pending item already covered by that high-water stays
        # retired; no replacement admission or backfill is synthesized here.
        if (candidate["create_time"] > highwater
                or candidate["create_time"] == highwater and candidate["message_id"] not in seen):
            pending = candidate
    result = _cursor_extra(copies, {"not_before_ms", "after_ms", "seen_ids", "thread_id", "pending"})
    result.update(not_before_ms=max(value["not_before_ms"] for value in copies),
                  after_ms=highwater, seen_ids=seen,
                  thread_id=next(iter(thread_ids)) if thread_ids else None, pending=pending)
    return result


def merge_poll_cursors(provider, copies, active_keys):
    """One-time monotonic cursor merge for one exact session/provider scope.

    Only active parent keys survive. Scheduling uses a deterministic marker,
    never hostname precedence. A higher observed cursor cannot be rewound by
    initial migration, including for a historical command lacking admission.
    """
    copies, active_keys = list(copies), set(active_keys)
    if any(not _cursor_digest(key) for key in active_keys):
        raise ValueError(_CURSOR_INVALID)
    if provider == "slack":
        for value in copies:
            _cursor_header(value, {"schema_version", "after", "threads"}, "after")
            if (not isinstance(value["threads"], dict)
                    or any(not _cursor_digest(key) or not valid_slack_timestamp(stamp)
                           for key, stamp in value["threads"].items())):
                raise ValueError(_CURSOR_INVALID)
        result = _cursor_extra(copies, {"schema_version", "after", "threads", "closed_cursor"})
        threads = {}
        for value in copies:
            for key, stamp in value["threads"].items():
                if key not in active_keys:
                    continue
                previous = threads.get(key)
                if (previous is None or Decimal(stamp) > Decimal(previous)
                        or Decimal(stamp) == Decimal(previous) and stamp < previous):
                    threads[key] = stamp
        result.update(schema_version=1, after=_scheduling(copies, "after"),
                      threads={key: threads[key] for key in sorted(threads)})
        return result
    if provider != "lark" or not copies:
        raise ValueError(_CURSOR_INVALID)
    scopes = set()
    parents = {}
    for value in copies:
        _cursor_header(value, {"schema_version", "scope", "after_key", "parents"}, "after_key")
        if not _cursor_digest(value["scope"]) or not isinstance(value["parents"], dict):
            raise ValueError(_CURSOR_INVALID)
        scopes.add(value["scope"])
        for key, cursor in value["parents"].items():
            if not _cursor_digest(key):
                raise ValueError(_CURSOR_INVALID)
            _lark_parent(cursor)
            if key in active_keys:
                parents.setdefault(key, []).append(cursor)
    if len(scopes) != 1:
        raise ValueError(_CURSOR_CONFLICT)
    result = _cursor_extra(copies, {"schema_version", "scope", "after_key", "parents"})
    result.update(schema_version=1, scope=next(iter(scopes)),
                  after_key=_scheduling(copies, "after_key"),
                  parents={key: _merge_lark_parent(parents[key]) for key in sorted(parents)})
    return result
