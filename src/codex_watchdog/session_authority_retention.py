"""Explicit cold retention upgrade of one verified shared-home session.

Only this operation reads historical provider snapshots. Publication selects a
new exact-session runtime; old shared journals, other manifests and native
state are never rewritten. Native/sender evidence is supplied inside the
existing admitted observation and owner fences, never inferred from history.
"""
from contextlib import ExitStack
from contextlib import closing, nullcontext
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import uuid

from .control_state import ControlBusy, ControlError, control_atomic_json, control_read_json
from .node_observation import observation_lock
from .recent_state import CURRENT_KINDS, RECENT_KINDS, RECENT_LIMIT, TTL_SECONDS, epoch


STORAGE = "session_provider_journals_v3"
PROVIDERS = ("slack", "lark", "onebot")
_GLOBAL_IDENTITY = frozenset(("slack_bot_users", "binding_rejected", "binding_seen", "notifications"))
_REFERENCES = ("thread_key", "source_key", "event_key", "event_key_digest", "command_key", "op_key")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _safe(path):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path:
        raise ControlError("session_authority_path_invalid")
    return path


def _inside(root, relative):
    path=_safe(root/relative)
    try:
        path.relative_to(root)
    except ValueError:
        raise ControlError("session_authority_unknown_prepared_artifact") from None
    return path


def _metadata(path, value):
    _safe(path)
    if path.exists():
        if control_read_json(path) != value:
            raise ControlError("session_authority_recovery_metadata_changed")
    else:
        control_atomic_json(path, value)


def _scope(row):
    value = json.loads(row["value"])
    target = value.get("target")
    values = [row.get("thread_id"), value.get("thread_id"),
              target.get("thread_id") if isinstance(target, dict) else None]
    sessions = set()
    for item in values:
        if item is None:
            continue
        try:
            if not isinstance(item, str) or str(uuid.UUID(item)) != item:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ControlError("session_authority_record_identity_invalid") from None
        sessions.add(item)
    if len(sessions) > 1:
        raise ControlError("session_authority_record_identity_conflict")
    return next(iter(sessions)) if sessions else None


def compact_session_snapshot(snapshot, thread_id, *, now, pins=()):
    """Cold-only exact-session projection preserving schema3 clocks/metadata.

    Recognized provider-global identities/denials/suppression are conservative
    evidence, not authorization grants. All grants/routes require the exact session. Recent
    physical receipts inherit ownership only through an exact stored reference.
    Unknown unscoped records refuse instead of being transferred as permission.
    """
    from .relay_authority_migration import snapshot_digest
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ControlError("session_authority_clock_invalid")
    if snapshot.get("source_schema") not in (2, 3):
        raise ControlError("session_authority_source_schema_invalid")
    from .legacy_ticket_cutoff import apply_legacy_ticket_cutoffs
    rows = apply_legacy_ticket_cutoffs(snapshot["records"], snapshot.get("record_order"),
        source_schema=snapshot["source_schema"])
    by_id = {tuple(row[name] for name in ("namespace", "kind", "key")): row for row in rows}
    order = [tuple(item) for item in snapshot.get("record_order", sorted(by_id))]
    rank = {identity: index for index, identity in enumerate(order)}
    owners = {key: _scope(row) for key, row in by_id.items()}
    bodies = {key: json.loads(row["value"]) for key, row in by_id.items()}
    # Explicit cold import only. Build reverse references once; no ordinary
    # tick or handoff consults this historical graph.
    by_key, dependents = {}, {}
    for identity in by_id:
        by_key.setdefault((identity[0], identity[2]), []).append(identity)
    for identity, body in bodies.items():
        for name in _REFERENCES:
            reference = body.get(name)
            if not isinstance(reference, str):
                continue
            for candidate in (reference, hashlib.sha256(reference.encode()).hexdigest()):
                for parent in by_key.get((identity[0], candidate), ()):
                    dependents.setdefault(parent, set()).add(identity)
    frontier = [key for key, owner in owners.items() if owner == thread_id]
    selected_ids = set(frontier)
    while frontier:
        parent = frontier.pop()
        for child in dependents.get(parent, ()):
            if owners[child] not in (None, thread_id):
                continue
            if child not in selected_ids:
                selected_ids.add(child)
                frontier.append(child)
    selected_ids.update(key for key in by_id if key[1] in _GLOBAL_IDENTITY and owners[key] is None)
    if any(key[1] not in CURRENT_KINDS | RECENT_KINDS for key in selected_ids):
        raise ControlError("session_authority_record_kind_unsupported")
    pins = set(tuple(item) for item in pins)
    if not pins.issubset(selected_ids):
        raise ControlError("session_authority_current_pin_unresolved")
    # Sessionless evidence that cannot be associated is not imported. Current
    # authorization kinds must never silently disappear under ambiguity.
    if any(owner is None and key[1] in CURRENT_KINDS - _GLOBAL_IDENTITY
           and key not in selected_ids for key, owner in owners.items()):
        raise ControlError("session_authority_current_scope_unresolved")
    namespaces = sorted({key[0] for key in selected_ids} | set(snapshot["namespaces"]))
    boundaries = [deepcopy(row) for row in snapshot.get("current_boundaries", ())
                  if row["session"] in ("", thread_id) and row["namespace"] in namespaces]
    current = max([now] + [row["checked_at"] for row in boundaries])
    recent = {tuple(row[name] for name in ("namespace", "kind", "key")): deepcopy(row)
              for row in snapshot.get("recent_records", ())}
    kept, pools, retired = {}, {}, []
    for identity in selected_ids:
        row = by_id[identity]
        if row["kind"] not in RECENT_KINDS:
            kept[identity] = row
            continue
        metadata = recent.get(identity)
        if snapshot["source_schema"] == 3 and metadata is None:
            raise ControlError("session_authority_recent_metadata_missing")
        if metadata is None:
            born = epoch(row["created_at"])
            born = epoch(bodies[identity].get("created_at")) if born is None else born
            born = current if born is None else born
            metadata = dict(namespace=identity[0], kind=identity[1], key=identity[2],
                            born=born, expires=born+TTL_SECONDS, protected=0, session=thread_id)
        if metadata["session"] not in ("", thread_id):
            raise ControlError("session_authority_recent_identity_conflict")
        metadata["protected"] = int(identity in pins)
        if identity in pins:
            kept[identity] = row
            recent[identity] = metadata
        elif metadata["born"] > current or metadata["expires"] <= current:
            retired.append(row)
        else:
            pools.setdefault((identity[0], metadata["session"], identity[1]), []).append((metadata, row))
    for values in pools.values():
        values.sort(key=lambda item: (item[0]["born"], item[1]["key"]), reverse=True)
        for metadata, row in values[:RECENT_LIMIT]:
            identity = tuple(row[name] for name in ("namespace", "kind", "key"))
            kept[identity], recent[identity] = row, metadata
        retired.extend(row for _, row in values[RECENT_LIMIT:])
    active = {}
    for row in kept.values():
        if row["kind"] == "threads" and row["active"]:
            active.setdefault(row["namespace"].split("/",1)[0], []).append(row)
    for tickets in active.values():
        tickets.sort(key=lambda row: (row["created_at"] or "",
                    rank[tuple(row[name] for name in ("namespace", "kind", "key"))]), reverse=True)
        for row in tickets[4:]:
            if tuple(row[name] for name in ("namespace", "kind", "key")) in pins:
                raise ControlError("session_authority_current_ticket_cap_conflict")
            row["active"] = 0
    # Import cutoff closes absent/retired callbacks. Existing exact older active
    # entries remain exceptions; immutable timestamps/generations are unchanged.
    indexed = {(row["namespace"], row["session"]): row for row in boundaries}
    for namespace in namespaces:
        prior = indexed.setdefault((namespace, ""), dict(namespace=namespace, session="", floor=0, checked_at=current))
        prior["checked_at"] = max(prior["checked_at"], current)
        prior = indexed.setdefault((namespace, thread_id), dict(namespace=namespace, session=thread_id,
                                                               floor=current, checked_at=current))
        prior["floor"], prior["checked_at"] = max(prior["floor"], current), max(prior["checked_at"], current)
    result = dict(schema_version=1, source_schema=3, namespaces=namespaces,
                  records=[kept[key] for key in sorted(kept)],
                  recent_records=[recent[key] for key in sorted(kept) if key[1] in RECENT_KINDS],
                  current_boundaries=[indexed[key] for key in sorted(indexed)],
                  record_order=[list(identity) for identity in order if identity in kept])
    snapshot_digest(result)  # The shared strict structured validator owns schema compatibility.
    return result


def selected_session_runtime(authority, manifest):
    """Small existing-file validation; no history scan, migration or creation."""
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or manifest.get("state") != "ready" or manifest.get("storage") != STORAGE
            or manifest.get("thread_id") != authority.thread_id
            or manifest.get("repo_path") != authority.repo_path
            or manifest.get("workspace_id") != authority.workspace_id
            or not isinstance(manifest.get("providers"), list)
            or manifest["providers"] != sorted(set(manifest["providers"]))
            or not set(manifest["providers"]).issubset(PROVIDERS)
            or re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("plan_sha256", ""))) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("native_pins_sha256", ""))) is None):
        raise ControlError("session_authority_manifest_invalid")
    relative = "session-authorities/%s/%s/runtime" % (authority.thread_id, manifest["plan_sha256"])
    if manifest.get("runtime_relative") != relative:
        raise ControlError("session_authority_manifest_invalid")
    runtime = _safe(authority.root / relative)
    if not runtime.is_dir():
        raise ControlError("session_authority_runtime_missing")
    imported = control_read_json(_safe(runtime / "session-import.json"))
    if (type(imported.get("schema_version")) is not int or imported["schema_version"] != 1
            or imported.get("storage") != STORAGE or imported.get("plan_sha256") != manifest["plan_sha256"]
            or imported.get("thread_id") != authority.thread_id or imported.get("repo_path") != authority.repo_path
            or not isinstance(imported.get("provider_sha256"),dict)
            or sorted(imported["provider_sha256"]) != manifest["providers"]
            or not isinstance(imported.get("native_pins"),dict)
            or imported.get("native_pins", {}).get("sha256") != manifest.get("native_pins_sha256")):
        raise ControlError("session_authority_runtime_invalid")
    if "session_local_state" in imported:
        from .session_local_state import validate_descriptor
        validate_descriptor(imported["session_local_state"], authority.thread_id)
        if manifest.get("session_local_state_sha256") != _digest(imported["session_local_state"]):
            raise ControlError("session_authority_runtime_invalid")
    elif "session_local_state_sha256" in manifest:
        raise ControlError("session_authority_runtime_invalid")
    return runtime


def _inventory(authority, reader, clock):
    from .native_inflight import validate_inventory
    inventory = reader()
    validate_inventory(inventory, authority.store.codex_home, authority.thread_id,
                       authority.repo_path, clock=clock)
    if inventory.get("status") != "complete":
        raise ControlBusy("session_authority_native_work_unverified")
    expected = dict(node=authority.observation.node, boot_id=authority.store.boot_id,
                    epoch=authority.token["epoch"], instance=authority.token["instance"])
    if inventory.get("owner") != expected:
        raise ControlBusy("session_authority_native_owner_mismatch")
    return inventory


def _work(inventory):
    return _digest({key: inventory[key] for key in ("owner", "queue", "native")})


def _selected(authority):
    # Caller holds the exact owner and shared observation locks. Do not open
    # another owner fd (flock is not reentrant across file descriptions).
    value = authority.observation._read()
    if (value is None or any(value.get("relay_owner", {}).get(key) != field
                            for key, field in authority.observation._relay_owner().items())
            or type(value.get("relay_epoch")) is not int):
        raise ControlBusy("control_node_relay_not_selected")
    return {key: deepcopy(value[key]) for key in ("relay_epoch", "relay_owner")}


def _intent(value, authority):
    fields={"schema_version","storage","thread_id","repo_path","workspace_id","source_sha256",
            "migration_clock","work_sha256","previous_manifest_sha256","provider_sha256","plan_sha256"}
    if isinstance(value,dict) and "session_local_state" in value:
        from .session_local_state import validate_descriptor
        validate_descriptor(value["session_local_state"], authority.thread_id)
        fields.add("session_local_state")
    if (not isinstance(value,dict) or set(value)!=fields or type(value.get("schema_version")) is not int
            or value["schema_version"]!=1 or value.get("storage")!=STORAGE
            or value.get("thread_id")!=authority.thread_id or value.get("repo_path")!=authority.repo_path
            or value.get("workspace_id")!=authority.workspace_id
            or type(value.get("migration_clock")) not in (int,float) or not math.isfinite(value["migration_clock"])
            or value["migration_clock"]<0
            or any(re.fullmatch(r"[0-9a-f]{64}",str(value.get(name,""))) is None
                   for name in ("work_sha256","previous_manifest_sha256","plan_sha256"))
            or not isinstance(value.get("provider_sha256"),dict) or not value["provider_sha256"]
            or not set(value["provider_sha256"]).issubset(PROVIDERS)
            or not isinstance(value.get("source_sha256"),dict)
            or set(value["source_sha256"])!=set(value["provider_sha256"])
            or any(re.fullmatch(r"[0-9a-f]{64}",str(item)) is None
                   for name in ("provider_sha256","source_sha256") for item in value[name].values())
            or _digest({key:field for key,field in value.items() if key!="plan_sha256"})!=value["plan_sha256"]):
        raise ControlError("session_authority_recovery_metadata_changed")
    return value


def _prepare_pins(runtime, recovery_root, snapshots, inventory, clock):
    """Stage current associations plus one compact same-boundary recovery DB."""
    from .native_inflight_pins import CurrentNativePins, prepare_migration_pins
    from .storage import FileLock
    for provider, snapshot in snapshots.items():
        prepare_migration_pins(runtime, provider, snapshot, inventory, fence=nullcontext, clock=clock)
    store = CurrentNativePins(runtime, inventory["codex_home"], inventory["thread_id"],
                              inventory["repo_path"], clock=clock)
    exported = store.export()
    recovered = CurrentNativePins(recovery_root, inventory["codex_home"], inventory["thread_id"],
                                  inventory["repo_path"], clock=clock)
    if store.path.exists():
        _safe(store.path)
        _safe(recovered.path)
        recovered.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if recovered.path.exists():
            if recovered.export()["sha256"] != exported["sha256"]:
                raise ControlError("session_authority_pin_recovery_changed")
        else:
            fd, name = tempfile.mkstemp(prefix=".pin-recovery-", suffix=".sqlite3", dir=recovered.path.parent)
            os.close(fd)
            temporary = Path(name)
            try:
                with FileLock(store.lock), closing(sqlite3.connect(store.path.as_uri()+"?mode=ro",uri=True)) as source:
                    with closing(sqlite3.connect(temporary)) as target:
                        source.backup(target)
                with temporary.open("r+b") as stream:
                    os.fsync(stream.fileno())
                os.link(temporary, recovered.path)
            finally:
                temporary.unlink(missing_ok=True)
            if recovered.export()["sha256"] != exported["sha256"]:
                raise ControlError("session_authority_pin_recovery_changed")
    elif recovered.path.exists():
        raise ControlError("session_authority_pin_recovery_changed")
    return dict(sha256=exported["sha256"], bindings=len(exported["bindings"]),
                snapshot_present=store.path.exists())


def _validate_pins(runtime, recovery_root, authority, header, clock):
    from .native_inflight_pins import CurrentNativePins
    pins = header.get("native_pins")
    if (not isinstance(pins, dict) or set(pins) != {"sha256", "bindings", "snapshot_present"}
            or re.fullmatch(r"[0-9a-f]{64}", str(pins["sha256"])) is None
            or type(pins["bindings"]) is not int or pins["bindings"] < 0
            or type(pins["snapshot_present"]) is not bool):
        raise ControlError("session_authority_pin_recovery_changed")
    current = CurrentNativePins(runtime, authority.store.codex_home, authority.thread_id,
                                authority.repo_path, clock=clock)
    exported = current.export()
    if (exported["sha256"] != pins["sha256"] or len(exported["bindings"]) != pins["bindings"]
            or current.path.exists() != pins["snapshot_present"]):
        raise ControlError("session_authority_pin_recovery_changed")
    if pins["snapshot_present"]:
        backup = CurrentNativePins(recovery_root, authority.store.codex_home, authority.thread_id,
                                   authority.repo_path, clock=clock)
        if not backup.path.is_file() or backup.export()["sha256"] != pins["sha256"]:
            raise ControlError("session_authority_pin_recovery_changed")
    return pins


def _artifact_files(directory, intent):
    """Verify a bounded helper-owned unactivated tree before retaining it."""
    from .relay_authority_migration import read_authority_snapshot, snapshot_digest
    thread = intent["thread_id"]
    allowed = {"recovery.json", "previous-manifest.json", "abandoned.json", "runtime/session-import.json"}
    if "session_local_state" in intent:
        allowed.update("runtime/%s/%s.json" % (kind, thread)
                       for kind, item in intent["session_local_state"]["files"].items() if item is not None)
    allowed.update("runtime/%s/reply-tickets.sqlite3" % provider for provider in intent["provider_sha256"])
    for prefix in ("runtime/", "pin-recovery/"):
        allowed.update(prefix+"native-inflight/"+thread+suffix for suffix in (".sqlite3", ".lock"))
    directories = set()
    for name in allowed:
        directories.update(str(parent).replace("\\", "/") for parent in Path(name).parents if str(parent)!=".")
    files, seen, pending = {}, 0, [directory]
    while pending:
        current = _safe(pending.pop())
        if current != directory:
            relative = str(current.relative_to(directory)).replace("\\", "/")
            if relative not in directories:
                raise ControlError("session_authority_unknown_prepared_artifact")
        with os.scandir(current) as entries:
            for entry in entries:
                seen += 1
                if seen > 48:
                    raise ControlError("session_authority_unknown_prepared_artifact")
                path = _safe(Path(entry.path))
                info = path.lstat()
                if hasattr(os,"getuid") and info.st_uid != os.getuid():
                    raise ControlError("session_authority_unknown_prepared_artifact")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                relative = str(path.relative_to(directory)).replace("\\", "/")
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or relative not in allowed:
                    raise ControlError("session_authority_unknown_prepared_artifact")
                if relative.startswith("runtime/") and relative.endswith("/reply-tickets.sqlite3"):
                    provider = relative.split("/")[1]
                    if snapshot_digest(read_authority_snapshot(path)) != intent["provider_sha256"][provider]:
                        raise ControlError("session_authority_recovery_runtime_changed")
                elif relative in ("recovery.json", "runtime/session-import.json"):
                    value = control_read_json(path)
                    if {key:value.get(key) for key in intent} != intent:
                        raise ControlError("session_authority_recovery_metadata_changed")
                elif relative == "previous-manifest.json":
                    if hashlib.sha256(path.read_bytes()).hexdigest() != intent["previous_manifest_sha256"]:
                        raise ControlError("session_authority_recovery_metadata_changed")
                elif relative.startswith(("runtime/lifecycle/", "runtime/activity-cursors/")):
                    from .session_local_state import _read
                    kind = relative.split("/")[1]
                    raw = _read(directory/"runtime", thread, kind)
                    expected = intent["session_local_state"]["files"][kind]
                    if raw is None or len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
                        raise ControlError("session_authority_recovery_runtime_changed")
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(files=files, directories=sorted(directories, key=lambda name: (-len(Path(name).parts),name)))


def _retire_old_abandoned(root, slot, scope, authority):
    """One managed backup slot; restart-safe retirement never touches unknowns."""
    disposition = _safe(root/"retiring-abandoned.json")
    if disposition.exists():
        value = control_read_json(disposition)
    elif slot.exists():
        previous = control_read_json(_safe(slot/"abandoned.json"))
        if (previous.get("kind") != "unactivated_session_authority_recovery"
                or any(previous.get(key)!=field for key,field in scope.items())):
            raise ControlError("session_authority_unknown_prepared_artifact")
        old_intent=_intent(previous.get("intent"),authority)
        inventory = _artifact_files(slot, old_intent)
        value = dict(schema_version=1,kind="retire_unactivated_session_authority_recovery",
                     intent=old_intent,**scope,**inventory)
        control_atomic_json(disposition,value)
    else:
        return
    if (value.get("kind")!="retire_unactivated_session_authority_recovery"
            or any(value.get(key)!=field for key,field in scope.items())
            or not isinstance(value.get("files"),dict) or len(value["files"])>32
            or not isinstance(value.get("directories"),list) or len(value["directories"])>24):
        raise ControlError("session_authority_unknown_prepared_artifact")
    _intent(value.get("intent"),authority)
    for relative in list(value["files"])+value["directories"]:
        if (not isinstance(relative,str) or not relative or len(relative)>512
                or Path(relative).is_absolute() or any(part in (".","..") for part in Path(relative).parts)):
            raise ControlError("session_authority_unknown_prepared_artifact")
    if any(re.fullmatch(r"[0-9a-f]{64}",str(item)) is None for item in value["files"].values()):
        raise ControlError("session_authority_unknown_prepared_artifact")
    # The disposition was written only after a complete known-tree validation.
    # A crash midway may leave a subset; every remaining byte must still match.
    if slot.exists():
        seen,pending=0,[slot]
        while pending:
            current=_safe(pending.pop())
            with os.scandir(current) as entries:
                for entry in entries:
                    seen+=1
                    if seen>48:raise ControlError("session_authority_unknown_prepared_artifact")
                    path=_safe(Path(entry.path))
                    relative=str(path.relative_to(slot)).replace("\\","/")
                    info=path.lstat()
                    if hasattr(os,"getuid") and info.st_uid!=os.getuid():
                        raise ControlError("session_authority_unknown_prepared_artifact")
                    if stat.S_ISDIR(info.st_mode):
                        if relative not in value["directories"]:
                            raise ControlError("session_authority_unknown_prepared_artifact")
                        pending.append(path)
                    elif (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                            or relative not in value["files"]
                            or hashlib.sha256(path.read_bytes()).hexdigest()!=value["files"][relative]):
                        raise ControlError("session_authority_unknown_prepared_artifact")
        for relative in value["files"]:
            path=_inside(slot,relative)
            if path.exists():path.unlink()
        for relative in value["directories"]:
            path=_inside(slot,relative)
            if path.exists():path.rmdir()
        slot.rmdir()
    disposition.unlink()


def abandon_prepared_session_authority(authority, expected_plan_sha256, *, inflight_reader,
                                       sender_fence, clock=time.time):
    """Explicitly reprepare only a verified, never-activated cold candidate.

    Keep one compact managed recovery copy. Repeated cold retries rotate only
    that proven older copy; malformed/unknown artifacts remain untouched.
    No source/native record, grant, claim, receipt or selected runtime is reset.
    """
    if re.fullmatch(r"[0-9a-f]{64}",str(expected_plan_sha256)) is None or not callable(sender_fence):
        raise ControlError("session_authority_abandon_arguments_invalid")
    observation=authority.observation
    with observation_lock(observation.lock_path):
        observation.validate_snapshot()
        with authority.store.guard(authority.token), observation_lock(_safe(authority.root/"migration.lock")):
            observation.validate_snapshot(active_control=(authority.store,authority.token))
            with sender_fence():
                _inventory(authority,inflight_reader,clock)
                root=_safe(authority.root/"session-authorities"/authority.thread_id)
                with observation_lock(root/"migration.lock"):
                    intent_path=_safe(root/"migration-intent.json")
                    if not intent_path.exists():return dict(status="nothing_prepared")
                    intent=_intent(control_read_json(intent_path),authority)
                    scope=dict(thread_id=authority.thread_id,repo_path=authority.repo_path)
                    if (intent.get("plan_sha256")!=expected_plan_sha256
                            or any(intent.get(key)!=field for key,field in scope.items())
                            or intent.get("storage")!=STORAGE
                            or _digest({key:value for key,value in intent.items() if key!="plan_sha256"})!=expected_plan_sha256):
                        raise ControlError("session_authority_abandon_identity_mismatch")
                    manifest=_safe(authority.manifest).read_bytes()
                    if hashlib.sha256(manifest).hexdigest()!=intent["previous_manifest_sha256"]:
                        raise ControlError("session_authority_abandon_already_selected_or_changed")
                    paths=[_safe(root/name) for name in (expected_plan_sha256,"stage-"+expected_plan_sha256)]
                    candidates=[path for path in paths if path.exists()]
                    slot=_safe(root/"abandoned")
                    if not candidates and slot.exists():
                        prior=control_read_json(_safe(slot/"abandoned.json"))
                        if prior.get("intent")==intent:
                            _artifact_files(slot,intent)
                            intent_path.unlink()
                            return dict(status="abandoned",plan_sha256=expected_plan_sha256)
                    if len(candidates)>1:
                        raise ControlError("session_authority_unknown_prepared_artifact")
                    if candidates:
                        candidate=candidates[0]
                        _artifact_files(candidate,intent)
                        _metadata(candidate/"abandoned.json",dict(schema_version=1,
                            kind="unactivated_session_authority_recovery",intent=intent,**scope))
                        _retire_old_abandoned(root,slot,scope,authority)
                        if slot.exists():raise ControlError("session_authority_unknown_prepared_artifact")
                        os.rename(candidate,slot)
                    if authority.manifest.read_bytes()!=manifest:
                        raise ControlError("session_authority_selection_changed")
                    intent_path.unlink()
                    return dict(status="abandoned",plan_sha256=expected_plan_sha256,
                                recovery_retained=bool(candidates))


def migrate_session_authority(authority, *, sources, inflight_reader, sender_fence, clock=time.time):
    """Publish one compact schema3 authority under existing native fences.

    ``inflight_reader`` obtains genuinely current native evidence and
    ``sender_fence`` holds the existing exact-session current sender leases.
    Both are explicit trusted integration callbacks, not proof-free defaults.
    Cold reads may scale with source H; installed readiness never does.
    """
    from .native_inflight import pins_for_snapshot
    from .control_context import acting_as, current_effect
    from .session_local_state import capture_session_local_state, validate_staged
    from .relay_authority_migration import (read_authority_snapshot, snapshot_digest,
                                             write_authority_snapshot)
    if (not callable(inflight_reader) or not callable(sender_fence) or not isinstance(sources, dict)
            or not set(sources).issubset(PROVIDERS)):
        raise ControlError("session_authority_migration_arguments_invalid")
    observation = authority.observation
    with observation_lock(observation.lock_path):
        observation.validate_snapshot()
        # Share the actual lexical owner lock with the existing native actor.
        # Its current-effect validation must not open a competing descriptor.
        with acting_as(authority.store, authority.token), current_effect(), observation_lock(_safe(authority.root/"migration.lock")):
            owner = authority.store.read()
            if owner.get("auto_paused") is True:
                raise ControlBusy("relay_authority_paused")
            observation.validate_snapshot(active_control=(authority.store, authority.token))
            selected = _selected(authority)
            # Ready exact-session recovery is indexed metadata, never a source re-import.
            if authority.manifest.exists():
                existing = control_read_json(_safe(authority.manifest))
                if existing.get("storage") == STORAGE:
                    authority.runtime = selected_session_runtime(authority, existing)
                    return dict(status="already_migrated", plan_sha256=existing["plan_sha256"])
            authority._ready(enroll=False)
            old_runtime = authority.runtime
            old_manifest = authority.manifest.read_bytes()
            previous=json.loads(old_manifest)
            if (previous.get("workspace_id",authority.workspace_id)!=authority.workspace_id
                    or previous.get("notifications_relative","runtime/session-notifications/"+authority.thread_id)
                        != "runtime/session-notifications/"+authority.thread_id):
                raise ControlError("session_authority_manifest_invalid")
            cluster=control_read_json(_safe(authority.root/"cluster.json"))
            configured=cluster.get("providers",())
            if (not isinstance(configured,(list,tuple)) or len(set(configured))!=len(configured)
                    or not set(configured).issubset(PROVIDERS)):
                raise ControlError("relay_authority_cluster_invalid")
            expected=set(configured)|{provider for provider in PROVIDERS
                if (old_runtime/provider/"reply-tickets.sqlite3").exists()}
            if not expected or set(sources)!=expected:
                raise ControlError("session_authority_complete_providers_required")
            migration_root = _safe(authority.root / "session-authorities" / authority.thread_id)
            with ExitStack() as held:
                held.enter_context(sender_fence())
                from .notification_senders import sender_fence as common_sender_fence
                held.enter_context(common_sender_fence(old_runtime))
                local_state = held.enter_context(capture_session_local_state(old_runtime, authority.thread_id))
                for provider in sorted(sources):
                    expected = _safe(old_runtime / provider / "reply-tickets.sqlite3")
                    if Path(sources[provider]).resolve() != expected or Path(sources[provider]).is_symlink():
                        raise ControlError("session_authority_source_path_mismatch")
                    lock = _safe(old_runtime / "locks" / (provider+"-authority.lock"))
                    if not lock.is_file():
                        raise ControlError("session_authority_source_lock_missing")
                    held.enter_context(observation_lock(lock))
                inventory = _inventory(authority, inflight_reader, clock)
                migration_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                held.enter_context(observation_lock(migration_root / "migration.lock"))
                intent_path = _safe(migration_root / "migration-intent.json")
                prior = _intent(control_read_json(intent_path),authority) if intent_path.exists() else None
                instant = prior["migration_clock"] if prior is not None else clock()
                original = {provider: read_authority_snapshot(sources[provider]) for provider in sorted(sources)}
                if any(not name.startswith(provider+"/") for provider,value in original.items()
                       for name in value["namespaces"]):
                    raise ControlError("session_authority_provider_namespace_mismatch")
                digests = {provider: snapshot_digest(value) for provider, value in original.items()}
                pins = {provider: pins_for_snapshot(value, inventory) for provider, value in original.items()}
                compact = {provider: compact_session_snapshot(value, authority.thread_id, now=instant,
                          pins=pins[provider]) for provider, value in original.items()}
                plan = dict(schema_version=1, storage=STORAGE, thread_id=authority.thread_id,
                            repo_path=authority.repo_path, workspace_id=authority.workspace_id,
                            source_sha256=digests, migration_clock=instant, work_sha256=_work(inventory),
                            previous_manifest_sha256=hashlib.sha256(old_manifest).hexdigest(),
                            session_local_state=local_state.descriptor,
                            provider_sha256={p:snapshot_digest(v) for p,v in compact.items()})
                digest = _digest(plan)
                intent = dict(plan, plan_sha256=digest)
                if prior is not None and prior != intent:
                    raise ControlError("session_authority_recovery_state_changed")
                destination = _safe(migration_root / digest)
                stage = _safe(migration_root / ("stage-"+digest))
                if prior is None:
                    if destination.exists() or stage.exists():
                        raise ControlError("session_authority_unknown_prepared_artifact")
                    control_atomic_json(intent_path, intent)
                elif stage.exists():
                    # Interrupted preparation may lack known files, but it
                    # must not absorb unrelated evidence before resuming.
                    _artifact_files(stage, intent)
                    if (stage/"abandoned.json").exists():
                        raise ControlError("session_authority_abandon_in_progress")
                runtime = destination / "runtime"
                if not destination.exists():
                    stage.mkdir(exist_ok=True, mode=0o700)
                    staged_runtime = _safe(stage / "runtime")
                    staged_runtime.mkdir(exist_ok=True, mode=0o700)
                    local_state.stage(staged_runtime)
                    for provider, snapshot in compact.items():
                        path = _safe(staged_runtime / provider / "reply-tickets.sqlite3")
                        if path.exists():
                            if snapshot_digest(read_authority_snapshot(path)) != snapshot_digest(snapshot):
                                raise ControlError("session_authority_recovery_runtime_changed")
                        else:
                            write_authority_snapshot(path, snapshot)
                    pin_recovery = _prepare_pins(staged_runtime, stage/"pin-recovery", compact, inventory, clock)
                    # Compact recovery consists of staged provider DBs plus
                    # bounded exact old-manifest metadata, never full H backup.
                    from .messaging_profile import write_new
                    previous_path = _safe(stage / "previous-manifest.json")
                    if previous_path.exists():
                        if previous_path.read_bytes() != old_manifest:
                            raise ControlError("session_authority_recovery_metadata_changed")
                    else:
                        write_new(previous_path, old_manifest)
                    _metadata(stage / "recovery.json", dict(intent, native_pins=pin_recovery))
                    _metadata(staged_runtime / "session-import.json", dict(intent, native_pins=pin_recovery))
                    _artifact_files(stage, intent)
                    os.replace(stage, destination)
                for provider, snapshot in compact.items():
                    if snapshot_digest(read_authority_snapshot(runtime/provider/"reply-tickets.sqlite3")) != snapshot_digest(snapshot):
                        raise ControlError("session_authority_recovery_runtime_changed")
                imported = control_read_json(_safe(runtime / "session-import.json"))
                if {key:imported.get(key) for key in intent} != intent:
                    raise ControlError("session_authority_recovery_metadata_changed")
                pin_recovery = _validate_pins(runtime,destination/"pin-recovery",authority,imported,clock)
                if control_read_json(_safe(destination/"recovery.json")) != imported:
                    raise ControlError("session_authority_recovery_metadata_changed")
                if _safe(destination/"previous-manifest.json").read_bytes() != old_manifest:
                    raise ControlError("session_authority_recovery_metadata_changed")
                if (destination/"abandoned.json").exists():
                    raise ControlError("session_authority_abandon_in_progress")
                _artifact_files(destination,intent)
                # Revalidate both source and genuinely current work at the
                # publication boundary. No native action or receipt is replayed.
                if any(snapshot_digest(read_authority_snapshot(sources[p])) != digests[p] for p in sources):
                    raise ControlError("session_authority_source_changed")
                if _work(_inventory(authority, inflight_reader, clock)) != plan["work_sha256"]:
                    raise ControlBusy("session_authority_native_work_changed")
                observation.validate_snapshot(active_control=(authority.store, authority.token))
                if _selected(authority) != selected or authority.manifest.read_bytes() != old_manifest:
                    raise ControlError("session_authority_selection_changed")
                local_state.revalidate()
                validate_staged(runtime, authority.thread_id, local_state.descriptor)
                previous = json.loads(old_manifest)
                notification = previous.get("notifications_relative",
                                            "runtime/session-notifications/"+authority.thread_id)
                manifest = dict(previous, schema_version=1, state="ready", storage=STORAGE,
                    thread_id=authority.thread_id, repo_path=authority.repo_path,
                    workspace_id=authority.workspace_id, plan_sha256=digest,
                    runtime_relative=str(runtime.relative_to(authority.root)).replace("\\", "/"),
                    providers=sorted(compact), notifications_relative=notification)
                manifest["native_pins_sha256"] = pin_recovery["sha256"]
                manifest["session_local_state_sha256"] = _digest(local_state.descriptor)
                control_atomic_json(authority.manifest, manifest)
                authority.runtime = selected_session_runtime(authority, manifest)
                return dict(status="migrated", plan_sha256=digest, providers=sorted(compact),
                            retained_records={p:len(v["records"]) for p,v in compact.items()})
