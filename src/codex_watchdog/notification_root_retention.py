"""Explicit notification-only cold cutover, never a polling-time migration.

The caller positively excludes every old notification sender for this selected
runtime through the entire call, including SMTP/OAuth and desktop subprocesses.
The common cooperative lock alone cannot fence an old binary. Native queues,
provider journals/pins, authority selectors, grants and credentials are outside
this operation and are neither read nor modified.
"""
from contextlib import closing, nullcontext
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

from .notification_receipt_retention import (FiniteReceipts, NAMES, RetentionError,
    FRESH_ORIGIN, _digest, _initial_floor_key, _safe, _sha, _time, context,
    prepare_notification_receipts)
from .notification_senders import sender_fence as common_sender_fence
from .notification_state_migration import migrate_notification_state
from .storage import FileLock, InstructionStore

READY = "finite-notification-root.json"
INTENT = "finite-notification-root-intent.json"
RETIRED = ("relay-migration.json", "relay-migration-history.sqlite3")
MAX_BYTES = 16384


def _read(path):
    _safe(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_BYTES:
        raise RetentionError("notification_root_metadata_oversized")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise RetentionError("notification_root_metadata_invalid")
            value[key] = item
        return value
    def number(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError()
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_float=number,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise RetentionError("notification_root_metadata_invalid") from None
    if not isinstance(value, dict):
        raise RetentionError("notification_root_metadata_invalid")
    return value


def _summary(value):
    if value is None:
        return None
    keys = ("plan_sha256", "source_count", "history_count", "dual_count")
    if (not isinstance(value, dict) or not _digest(value.get("plan_sha256"))
            or any(type(value.get(k)) is not int or value[k] < 0 for k in keys[1:])):
        raise RetentionError("notification_root_relay_metadata_invalid")
    return {key: value[key] for key in keys}


def _write(path, value):
    if len((json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)+"\n").encode()) > MAX_BYTES:
        raise RetentionError("notification_root_metadata_oversized")
    InstructionStore._atomic_json(path, value)


def read_notification_root(runtime):
    """One bounded point read; missing means unprepared, invalid means refuse."""
    directory = _safe(Path(runtime).absolute() / "notifications")
    value = _read(directory / READY)
    if value is None:
        return None
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("storage") != "finite_notification_root_v1"
            or value.get("state") != "ready" or not _digest(value.get("plan_sha256"))
            or value.get("rollback_policy") != "forward_recovery_only_no_old_sender"
            or value.get("ready_sha256") != _sha({k:v for k,v in value.items() if k != "ready_sha256"})):
        raise RetentionError("notification_root_metadata_invalid")
    _time(value.get("floor"))
    _initial_floor_key(value)
    _summary(value.get("relay"))
    return value


def validate_notification_root(runtime, expected_relay_plan=None):
    """Cold/read-only family verification; no historical payload reads.

    Authority's ordinary guard deliberately uses only the compact root identity,
    keeping unavailable receipt families out of native/control admission.
    """
    value = read_notification_root(runtime)
    if value is None:
        return None
    relay = _summary(value.get("relay"))
    if expected_relay_plan is not None and (relay is None or relay["plan_sha256"] != expected_relay_plan):
        raise RetentionError("notification_root_relay_plan_mismatch")
    directory = _safe(Path(runtime).absolute() / "notifications")
    marker = _read(directory / "dual-deliveries.json")
    if (marker is None or type(marker.get("schema_version")) is not int
            or marker["schema_version"] != 3 or marker.get("retention") != "finite-v1"
            or marker.get("storage") != "dual-deliveries.sqlite3"
            or marker.get("plan_sha256") != value["plan_sha256"]):
        raise RetentionError("notification_root_family_mismatch")
    for family, name in NAMES.items():
        path = _safe(directory / (name + ".sqlite3"))
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            if FiniteReceipts(db, family).plan != value["plan_sha256"]:
                raise RetentionError("notification_root_family_mismatch")
    return value


def source_eligible(runtime, born):
    """None: legacy; False: pre-cutover/unknown source; True: later source.

    No retry clock or ticket creation can replace the producer's original birth.
    Normal provider/context/TTL/claim guards still apply to a later source.
    """
    value = read_notification_root(runtime)
    if value is None:
        return None
    try:
        born = _time(born)
    except RetentionError:
        return False
    return born > value["floor"] or (born == 0 and _initial_floor_key(value) == '')


def _identity(path):
    """Cold-only streaming hash, bounded memory; never a history copy."""
    _safe(path)
    if not path.exists():
        return None
    if not path.is_file() or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise RetentionError("notification_root_legacy_source_busy")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return dict(sha256=digest.hexdigest(), size=size)


def _intent(value):
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("storage") != "notification_root_intent_v1"
            or not isinstance(value.get("retired"), dict) or set(value["retired"]) != set(RETIRED)):
        raise RetentionError("notification_root_intent_invalid")
    _summary(value.get("relay"))
    if "legacy_metadata" in value and _summary(value["legacy_metadata"]) != value.get("relay"):
        raise RetentionError("notification_root_intent_invalid")
    for entry in value["retired"].values():
        if entry is not None and (not isinstance(entry, dict) or not _digest(entry.get("sha256"))
                or type(entry.get("size")) is not int or entry["size"] < 0):
            raise RetentionError("notification_root_intent_invalid")
    return value


def _legacy_history(directory, relay):
    """Cold-only recognition of the disposable store, never its payloads."""
    path = _safe(directory / RETIRED[1])
    if (relay is None) != (not path.exists()):
        raise RetentionError("notification_root_orphaned_relay_history")
    if relay is None:
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchmany(4)
        fields = db.execute("PRAGMA table_info(history)").fetchmany(4)
        if (db.execute("PRAGMA user_version").fetchone()[0] != 1
                or set(objects) != {("table", "history"), ("index", "notification_history_lookup")}
                or tuple(row[1] for row in fields) != ("fingerprint", "evidence_sha256", "value")
                or tuple(row[1] for row in fields if row[5]) != ("evidence_sha256",)
                or tuple(row[2] for row in db.execute("PRAGMA index_info(notification_history_lookup)")) != ("fingerprint",)):
            raise RetentionError("notification_root_legacy_history_unrecognized")


def _protect(directory, contexts, pins, clock):
    """Install only explicitly verified HTTP-receipt pins; never infer removals."""
    values = {value["fingerprint"]: value for value in map(context, contexts)}
    for family, providers in (("dual", ("slack", "lark")), ("fallback", ("smtp", "windows_msg"))):
        selected = [(fp, provider) for fp, provider in pins if provider in providers]
        if not selected:
            continue
        path = _safe(directory / (NAMES[family] + ".sqlite3"))
        with closing(sqlite3.connect(path.as_uri()+"?mode=rw", uri=True, timeout=1)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            for fp, provider in selected:
                store = FiniteReceipts(db, family, receipt_context=values.get(fp), clock=clock)
                row = store._row(fp, provider)
                if row is None:
                    raise RetentionError("notification_root_current_pin_gap")
                store._check(fp, provider, row)
                db.execute("UPDATE retention SET protected=1 WHERE fingerprint=? AND provider=? AND protected=0", (fp,provider))
            db.commit()


def prepare_notification_root(runtime, *, scopes, current_keys, contexts=(), current_pins,
                              sender_fence, clock=time.time):
    """Compose existing cold APIs under genuine caller old-sender exclusion.

    scopes/contexts/pins are the complete verified notification-receipt projection for
    this runtime, not a guessed foreign/shared-home inventory. This does not
    assert native idle or make provider-journal migration safe. A crash resumes
    forward only; retained old binaries must not be restarted after activation.
    Current pins must be explicitly supplied, never inferred empty merely from
    their different tuple format. Fence ALL actual caller roots after authority
    selection; the selected receipt root may differ from the authority root.
    Do not hold the nonreentrant notifications.lock inside sender_fence.
    """
    if not callable(sender_fence):
        raise RetentionError("notification_root_sender_fence_required")
    scopes, current_keys, contexts, current_pins = list(scopes), list(current_keys), list(contexts), list(current_pins)
    runtime = _safe(Path(runtime).absolute())
    directory = _safe(runtime / "notifications")
    with sender_fence(), common_sender_fence(runtime):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        ready = validate_notification_root(runtime)
        with FileLock(runtime / "locks/notifications.lock"):
            pending = _read(directory / INTENT)
            if pending is None:
                if ready is not None:
                    raise RetentionError("notification_root_intent_missing")
                relay = _read(directory / RETIRED[0])
                if relay is not None and (type(relay.get("schema_version")) is not int or relay["schema_version"] != 1
                        or relay.get("state") != "ready"
                        or relay.get("storage") != "relay_notification_history_v1"):
                    raise RetentionError("notification_root_relay_metadata_invalid")
                _legacy_history(directory, relay)
                pending = dict(schema_version=1, storage="notification_root_intent_v1",
                    relay=_summary(relay), legacy_metadata=relay,
                    retired={name: _identity(directory / name) for name in RETIRED})
                _intent(pending)
                _write(directory / INTENT, pending)
            else:
                _intent(pending)
            if ready is not None and ready.get("relay") != pending["relay"]:
                raise RetentionError("notification_root_intent_collision")
            if ready is None:
                for name in RETIRED:
                    if _identity(directory / name) != pending["retired"][name]:
                        raise RetentionError("notification_root_retired_source_changed")
        prepared = prepare_notification_receipts(runtime, scopes=scopes, contexts=contexts,
            current_pins=current_pins, sender_fence=nullcontext, clock=clock)
        if prepared.get("pin_gaps"):
            raise RetentionError("notification_root_current_pin_gap")
        with FileLock(runtime / "locks/notifications.lock"):
            _protect(directory, contexts, current_pins, clock)
            if ready is None:
                migrate_notification_state(runtime, current_keys=current_keys)
                # The existing compact recovery plan fixes the ORIGINAL cutoff.
                # Read it only here, never during ordinary send/authority checks.
                plan = json.loads(_safe(directory / "finite-receipts-preparation.json").read_bytes())
                if plan.get("plan_sha256") != prepared["plan_sha256"]:
                    raise RetentionError("notification_root_preparation_mismatch")
                ready = dict(schema_version=1, storage="finite_notification_root_v1", state="ready",
                    floor=_time(plan.get("born")), plan_sha256=prepared["plan_sha256"],
                    relay=pending["relay"], rollback_policy="forward_recovery_only_no_old_sender")
                if _initial_floor_key(plan, floor_field='born') == '':
                    ready['origin'] = FRESH_ORIGIN
                ready["ready_sha256"] = _sha(ready)
                _write(directory / READY, ready)
            elif prepared["plan_sha256"] != ready["plan_sha256"]:
                raise RetentionError("notification_root_preparation_mismatch")
        validate_notification_root(runtime)
        # Publish readiness before physical retirement. Interrupted cleanup is
        # harmless to hot paths; cold reuse deletes only unchanged known files.
        with FileLock(runtime / "locks/notifications.lock"):
            for name in RETIRED:
                path = directory / name
                actual = _identity(path)
                if actual is None:
                    continue
                if actual != pending["retired"][name]:
                    raise RetentionError("notification_root_retired_source_changed")
                path.unlink()
            from .notification_legacy_cleanup import _retire_source_evidence
            copied_history = _retire_source_evidence(runtime, expected_root=ready)
        return dict(status="ready", floor=ready["floor"], plan_sha256=ready["plan_sha256"],
            legacy_history_retired=True, native_or_provider_state_changed=False,
            copied_history=copied_history,
            maintenance_required=prepared["maintenance_required"],
            rollback_policy=ready["rollback_policy"])
