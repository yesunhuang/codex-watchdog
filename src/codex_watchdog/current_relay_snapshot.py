"""Small current reply-address snapshot for the dependency-free host protocol.

This is current state, not an archive. Explicit upgrade import is the only
operation allowed to enumerate legacy mapping files. Source stays Python 3.6.
"""

CURRENT_RELAY_SOURCE = r'''
import base64
import datetime

CONTROL_MAPPING_TTL = 72 * 60 * 60
CONTROL_MAPPING_LIMIT = 4


def control_mapping_time(value):
    if (not isinstance(value, str) or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value) is None):
        raise ControlError("control_reply_ticket_metadata_invalid")
    try:
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
        return datetime.datetime.strptime(value, fmt).replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, OverflowError):
        raise ControlError("control_reply_ticket_metadata_invalid")


def control_mapping_path(store):
    path = store.directory / "current-relay-mappings.json"
    if path.is_symlink() or path.resolve() != path:
        raise ControlError("control_current_mappings_path_invalid")
    return path


def control_mapping_empty(store):
    return dict(schema_version=1, kind="current_relay_mappings", thread_id=store.thread_id,
                repo_path=store.repo_path, providers={})


def control_mapping_identity(store, entry, event_id=None):
    if (not isinstance(entry, dict) or entry.get("thread_id", store.thread_id) != store.thread_id
            or "target" in entry and (not isinstance(entry["target"], dict)
            or entry["target"].get("thread_id") != store.thread_id)):
        raise ControlError("control_reply_ticket_thread_mismatch")
    provider = entry.get("provider", "slack")
    if provider == "slack":
        if (not all(isinstance(entry.get(name), str) for name in
                    ("channel_id", "thread_ts", "event_fingerprint"))
                or re.fullmatch(r"[CG][A-Z0-9]{8,}", str(entry.get("channel_id", ""))) is None
                or re.fullmatch(r"[0-9]{10,}\.[0-9]+", str(entry.get("thread_ts", ""))) is None
                or re.fullmatch(r"[0-9a-f]{64}", str(entry.get("event_fingerprint", ""))) is None
                or event_id is not None and entry.get("event_fingerprint") != event_id):
            raise ControlError("control_slack_mapping_invalid")
        key = hashlib.sha256((entry["channel_id"] + "\0" + entry["thread_ts"]).encode()).hexdigest()
        mapping = dict(schema_version=1, **{name: entry[name] for name in (
            "channel_id", "thread_ts", "event_fingerprint")})
        mapping.update(store._ticket_metadata(entry))
    elif provider in ("lark", "onebot"):
        validate = store._lark_mapping if provider == "lark" else store._onebot_mapping
        key, mapping = validate(entry, event_id)
    else:
        raise ControlError("control_relay_provider_invalid")
    # These existing optional boundaries are carried, never manufactured.
    for name in ("activation_generation", "route_generation"):
        if name in entry:
            if type(entry[name]) is not int or entry[name] < 1:
                raise ControlError("control_reply_ticket_generation_invalid")
            mapping[name] = entry[name]
    return provider, key, mapping


def control_mapping_load(store):
    path = control_mapping_path(store)
    if not path.exists():
        return control_mapping_empty(store)
    value = control_read_json(path)
    providers = value.get("providers")
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("kind") != "current_relay_mappings" or value.get("thread_id") != store.thread_id
            or value.get("repo_path") != store.repo_path or not isinstance(providers, dict)
            or not set(providers).issubset({"slack", "lark", "onebot"})):
        raise ControlError("control_current_mappings_invalid")
    for provider, state in providers.items():
        if not isinstance(state, dict):
            raise ControlError("control_current_mappings_invalid")
        floor, entries = state.get("floor"), state.get("entries")
        if (not isinstance(floor, list) or len(floor) != 2
                or type(floor[0]) not in (int, float) or not math.isfinite(floor[0])
                or not isinstance(floor[1], str)
                or floor[1] not in ("", "~") and re.fullmatch(r"[0-9a-f]{64}", floor[1]) is None
                or not isinstance(entries, list)
                or len(entries) > CONTROL_MAPPING_LIMIT):
            raise ControlError("control_current_mappings_invalid")
        seen = set()
        for entry in entries:
            kind, key, mapped = control_mapping_identity(store, entry)
            if kind != provider or key in seen or mapped.get("ticket_schema") != 1:
                raise ControlError("control_current_mappings_invalid")
            control_mapping_time(mapped["created_at"])
            seen.add(key)
        expired = state.get("expired_before", floor[0])
        if type(expired) not in (int, float) or not math.isfinite(expired):
            raise ControlError("control_current_mappings_invalid")
    return value


def control_mapping_prune(store, value, now):
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ControlError("control_mapping_clock_invalid")
    for provider, state in value["providers"].items():
        cutoff = max(state.get("expired_before", now - CONTROL_MAPPING_TTL), now - CONTROL_MAPPING_TTL)
        state["expired_before"] = cutoff
        floor = max(tuple(state["floor"]), (cutoff, "~"))
        state["floor"] = list(floor)
        state["entries"] = [entry for entry in state["entries"]
            if control_mapping_time(entry["created_at"]) > cutoff]
    return value


def control_mapping_merge(store, entries, event_id=None, now=None, write=True):
    now = time.time() if now is None else now
    # Validate the whole bounded callback before changing durable state.
    if not isinstance(entries, (list, tuple)) or len(entries) > 12:
        raise ControlError("control_mapping_callback_too_large")
    normalized = [control_mapping_identity(store, entry, event_id) for entry in entries]
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ControlError("control_mapping_clock_invalid")
    for provider, key, mapping in normalized:
        if mapping.get("ticket_schema") == 1 and control_mapping_time(mapping["created_at"]) > now:
            raise ControlError("control_reply_ticket_future_timestamp")
    if not normalized:
        return dict(status="no_op")  # No optional projection read or creation.
    try:
        value = control_mapping_load(store)
    except (ControlError, OSError, ValueError, TypeError, RecursionError) as exc:
        if not write:
            raise
        return dict(status="projection_unavailable", reason=(str(exc) if isinstance(exc, ControlError)
                    else "control_current_mappings_unavailable"))
    control_mapping_prune(store, value, now)
    for provider, key, mapping in normalized:
        if mapping.get("ticket_schema") != 1:
            continue  # Old immutable evidence is never promoted to a live ticket.
        created = control_mapping_time(mapping["created_at"])
        if created > now:
            raise ControlError("control_reply_ticket_future_timestamp")
        state = value["providers"].setdefault(provider, dict(
            floor=[now - CONTROL_MAPPING_TTL, "~"], expired_before=now - CONTROL_MAPPING_TTL, entries=[]))
        current = {control_mapping_identity(store, entry)[1]: entry for entry in state["entries"]}
        if key in current:
            if any(current[key].get(name) != field for name, field in mapping.items()):
                raise ControlError("control_mapping_immutable_collision")
            continue  # Preserve original creation and any unknown current fields.
        if (created, key) <= tuple(state["floor"]):
            continue  # Absolute expiry/eviction is permanent across late callbacks.
        state["entries"].append(mapping)
        state["entries"].sort(key=lambda entry: (
            control_mapping_time(entry["created_at"]), control_mapping_identity(store, entry)[1]))
        if len(state["entries"]) > CONTROL_MAPPING_LIMIT:
            removed = state["entries"].pop(0)
            state["floor"] = list(max(tuple(state["floor"]), (
                control_mapping_time(removed["created_at"]), control_mapping_identity(store, removed)[1])))
    if write:
        try:
            control_atomic_json(control_mapping_path(store), value)
        except (ControlError, OSError, ValueError, TypeError, RecursionError):
            return dict(status="projection_unavailable", reason="control_current_mappings_unavailable")
    return value


def control_retire_current_mappings(store, provider, keys, now=None, created_at_by_key=None):
    """Called by the exact ticket-close/eviction lifecycle, not provider polling.

    Advancing the admission floor does not remove older still-active exceptions.
    A removed address cannot be restored by an old duplicate notification callback.
    """
    if (provider not in ("slack", "lark", "onebot") or not isinstance(keys, (list, tuple))
            or len(keys) > CONTROL_MAPPING_LIMIT
            or any(re.fullmatch(r"[0-9a-f]{64}", str(key)) is None for key in keys)):
        raise ControlError("control_mapping_retirement_invalid")
    with control_file_lock(store.lock_path):
        value = control_mapping_load(store)
        now = time.time() if now is None else now
        control_mapping_prune(store, value, now)
        if (created_at_by_key is not None and (not isinstance(created_at_by_key, dict)
                or not set(created_at_by_key).issubset(set(keys)))):
            raise ControlError("control_mapping_retirement_invalid")
        state = value["providers"].setdefault(provider, dict(
            floor=[now - CONTROL_MAPPING_TTL, "~"], expired_before=now - CONTROL_MAPPING_TTL, entries=[]))
        current = {control_mapping_identity(store, entry)[1]: entry for entry in state["entries"]}
        for key in keys:
            created = (current[key]["created_at"] if key in current
                       else (created_at_by_key or {}).get(key))
            if created is None:
                raise ControlError("control_mapping_retirement_timestamp_required")
            timestamp = control_mapping_time(created)
            if timestamp > now:
                raise ControlError("control_reply_ticket_future_timestamp")
            state["floor"] = list(max(tuple(state["floor"]), (timestamp, key)))
        state["entries"] = [entry for key, entry in current.items() if key not in keys]
        control_atomic_json(control_mapping_path(store), value)
        return dict(status="retired_current_addresses")


def control_current_mappings(store, now=None):
    with control_file_lock(store.lock_path):
        value = control_mapping_load(store)
        before = json.dumps(value, sort_keys=True)
        control_mapping_prune(store, value, time.time() if now is None else now)
        if json.dumps(value, sort_keys=True) != before:
            control_atomic_json(control_mapping_path(store), value)
        return [dict(entry) for state in value["providers"].values() for entry in state["entries"]]


def control_import_current_mappings(store, now=None, active_mappings=None):
    """Explicit one-time compatible upgrade; never called by a normal probe.

    The upgrade caller supplies the authoritative active N4/TTL/floor view;
    legacy envelopes alone never establish ticket eligibility or closure.
    Old sources are unchanged until subsequent bounded explicit cleanup.
    """
    with control_file_lock(store.lock_path):
        if control_mapping_path(store).exists():
            control_mapping_load(store)  # A corrupt existing snapshot is not initialization.
            return dict(status="already_initialized")
        if (not isinstance(active_mappings, (list, tuple)) or len(active_mappings) > 12):
            raise ControlError("control_mapping_active_import_required")
        now = time.time() if now is None else now
        selected = []
        counts = {}
        for entry in active_mappings:
            provider, key, mapped = control_mapping_identity(store, entry)
            created = control_mapping_time(mapped.get("created_at"))
            if mapped.get("ticket_schema") != 1 or not now - CONTROL_MAPPING_TTL < created <= now:
                raise ControlError("control_mapping_active_import_required")
            counts[provider] = counts.get(provider, 0) + 1
            if counts[provider] > CONTROL_MAPPING_LIMIT:
                raise ControlError("control_mapping_active_import_required")
            path = store.directory / (provider + "-threads") / (key + ".json")
            if path.is_symlink() or path.resolve() != path:
                raise ControlError("control_current_mappings_path_invalid")
            if path.exists():
                _, _, previous = control_mapping_identity(store, control_read_json(path))
                if previous != mapped:
                    raise ControlError("control_mapping_immutable_collision")
            selected.append(mapped)
        value = (control_mapping_merge(store, selected, now=now, write=False)
                 if selected else control_mapping_empty(store))
        # Exact active exceptions survive the upgrade. Unknown pre-upgrade
        # callbacks cannot repopulate the snapshot, even if their ticket was
        # closed only moments before import or no recent tickets remain.
        for provider in ("slack", "lark", "onebot"):
            state = value["providers"].setdefault(provider, dict(
                floor=[now, "~"], expired_before=now - CONTROL_MAPPING_TTL, entries=[]))
            state["floor"] = list(max(tuple(state["floor"]), (now, "~")))
        value["legacy_import_complete"] = True
        value["import_source"] = "authoritative_active_tickets"
        control_atomic_json(control_mapping_path(store), value)
        return dict(status="imported", active=len(selected))


def control_mapping_backup(store, path, raw, now):
    """One managed bounded recovery artifact, rotated before replacement.

    The 256KiB backup bound covers base64 of the full compatible 128KiB source
    plus scope/digest metadata. Unknown backup files are never overwritten.
    Atomic visibility/temp-file fsync follows the existing platform primitive;
    no new cross-platform power-loss durability guarantee is claimed.
    """
    backup = store.directory / "current-relay-mappings.reconstruction-backup"
    if backup.is_symlink() or backup.resolve() != backup:
        raise ControlError("control_mapping_recovery_backup_invalid")
    value = {}
    if backup.exists():
        try:
            info = backup.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or hasattr(os, "getuid") and info.st_uid != os.getuid()):
                raise ValueError()
            with backup.open("rb") as handle:
                old = handle.read(262145)
            value = json.loads(old) if len(old) <= 262144 else None
            previous = base64.b64decode(value["raw_base64"], validate=True)
            if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                    or value["schema_version"] != 1 or value.get("kind") != "current_mapping_projection_backup"
                    or value.get("thread_id") != store.thread_id or value.get("repo_path") != store.repo_path
                    or len(previous) > 131072 or hashlib.sha256(previous).hexdigest() != value.get("raw_sha256")):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ControlError("control_mapping_recovery_backup_invalid")
    value.update(schema_version=1, kind="current_mapping_projection_backup", thread_id=store.thread_id,
                 repo_path=store.repo_path, raw_base64=base64.b64encode(raw).decode("ascii"),
                 raw_sha256=hashlib.sha256(raw).hexdigest(), backed_up_at=now)
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)+"\n").encode()
    if len(encoded) > 262144:
        raise ControlError("control_mapping_recovery_backup_too_large")
    fd, temporary = tempfile.mkstemp(prefix=".mapping-backup-", dir=str(store.directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(backup))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def control_reconstruct_current_mappings(store, active_mappings, trusted_floors, now=None):
    """Explicit trusted caller repair of an optional secondary projection.

    A must obtain the complete CURRENT active N4/TTL set and these monotonic
    creation/address floors from its authoritative ticket/ACL/route state under
    its existing fences. This is not a provider callback or a generic RPC action.
    Normal callbacks never reconstruct unknown floors from historical envelopes.
    Optional existing generation fields remain immutable and are validated.

    One fixed managed backup retains exact prior malformed/unknown bytes within
    the compatible 128KiB source bound; later repairs atomically rotate only
    that recognized backup, without growing an archive. Unknown backup files or
    aliased/oversized sources refuse untouched. A failed publish is safely
    retryable as metadata repair from a fresh authoritative view, never a send.
    """
    now = time.time() if now is None else now
    if (type(now) not in (int, float) or not math.isfinite(now)
            or not isinstance(active_mappings, (list, tuple)) or len(active_mappings) > 12
            or not isinstance(trusted_floors, dict)
            or set(trusted_floors) != {"slack", "lark", "onebot"}):
        raise ControlError("control_mapping_trusted_reconstruction_invalid")
    floors = {}
    for provider, floor in trusted_floors.items():
        if (not isinstance(floor, (list, tuple)) or len(floor) != 2
                or type(floor[0]) not in (int, float) or not math.isfinite(floor[0])
                or not isinstance(floor[1], str)
                or floor[1] not in ("", "~") and re.fullmatch(r"[0-9a-f]{64}", floor[1]) is None):
            raise ControlError("control_mapping_trusted_reconstruction_invalid")
        floors[provider] = list(max(tuple(floor), (now, "~")))
    value = control_mapping_empty(store)
    value["providers"] = {provider: dict(floor=floor, expired_before=now-CONTROL_MAPPING_TTL,
                                        entries=[]) for provider, floor in floors.items()}
    seen = set()
    for entry in active_mappings:
        provider, key, mapped = control_mapping_identity(store, entry)
        created = control_mapping_time(mapped.get("created_at"))
        if (mapped.get("ticket_schema") != 1 or not now-CONTROL_MAPPING_TTL < created <= now
                or (provider, key) in seen
                or len(value["providers"][provider]["entries"]) >= CONTROL_MAPPING_LIMIT):
            raise ControlError("control_mapping_trusted_reconstruction_invalid")
        seen.add((provider, key))
        value["providers"][provider]["entries"].append(mapped)
    value.update(legacy_import_complete=True, import_source="trusted_current_reconstruction",
                 reconstruction_complete=True)
    with control_file_lock(store.lock_path):
        path = control_mapping_path(store)
        if path.exists():
            try:
                control_mapping_load(store)
            except ControlError:
                pass
            else:
                return dict(status="already_available")
        backup = None
        if path.exists():
            current = path.stat()
            if (not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or hasattr(os, "getuid") and current.st_uid != os.getuid()):
                raise ControlError("control_mapping_recovery_source_invalid")
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                raw = handle.read(131073)
                ended = os.fstat(handle.fileno())
            if (identity(current) != identity(opened) or identity(opened) != identity(ended)):
                raise ControlError("control_mapping_recovery_source_changed")
            if len(raw) > 131072:
                raise ControlError("control_mapping_recovery_source_too_large")
            if len(raw) != current.st_size:
                raise ControlError("control_mapping_recovery_source_changed")
            backup = control_mapping_backup(store, path, raw, now)
            if identity(path.stat()) != identity(current):
                raise ControlError("control_mapping_recovery_source_changed")
        control_atomic_json(path, value)
        return dict(status="reconstructed", active=len(active_mappings), backup_retained=backup is not None)


def control_cleanup_legacy_mappings(store, budget=16, now=None):
    """Bounded upgrade cleanup of expired recognized WatchDog mapping files.

    Unknown/corrupt files and the exact current external effect are retained.
    Cleanup never scans until it finds a deletable record or touches native data.
    """
    if type(budget) is not int or not 1 <= budget <= 64:
        raise ControlError("control_cleanup_budget_invalid")
    with control_file_lock(store.lock_path):
        snapshot = control_mapping_load(store)
        if snapshot.get("legacy_import_complete") is not True:
            return dict(status="explicit_import_required", visited=0, removed=0)
        now = time.time() if now is None else now
        active = {control_mapping_identity(store, entry)[1]
                  for state in snapshot["providers"].values() for entry in state["entries"]}
        owner = store.read() if store.path.exists() else {}
        effect = owner.get("external_effect")
        protected = effect.get("event_id") if isinstance(effect, dict) else None
        visited = removed = 0
        # Foreground-only transient cleanup work, not a persistent archive.
        # A recreated store/short-lived SSH RPC restarts enumeration; no
        # cross-restart cleanup progress is claimed. A owns scheduler retention.
        state = getattr(store, "_mapping_cleanup", None)
        if state is None:
            state = dict(provider=0, iterator=None)
            store._mapping_cleanup = state
        exhausted = 0
        while visited < budget and exhausted < 3:
            provider = ("slack", "lark", "onebot")[state["provider"]]
            directory = store.directory / (provider + "-threads")
            if directory.is_symlink() or directory.resolve() != directory:
                raise ControlError("control_current_mappings_path_invalid")
            if state["iterator"] is None:
                try:
                    state["iterator"] = os.scandir(str(directory))
                except FileNotFoundError:
                    state["provider"] = (state["provider"] + 1) % 3
                    exhausted += 1
                    continue
            try:
                item = next(state["iterator"])
            except StopIteration:
                state["iterator"].close()
                state["iterator"] = None
                state["provider"] = (state["provider"] + 1) % 3
                exhausted += 1
                continue
            visited += 1
            if not item.name.endswith(".json") or not item.is_file(follow_symlinks=False):
                continue
            path = directory / item.name
            try:
                entry = control_read_json(path)
                kind, key, mapped = control_mapping_identity(store, entry)
                if (kind != provider or path.stem != key or key in active
                        or mapped.get("event_fingerprint") == protected
                        or mapped.get("ticket_schema") == 1
                        and control_mapping_time(mapped["created_at"]) > now - CONTROL_MAPPING_TTL):
                    continue
                path.unlink()
                removed += 1
            except (ControlError, OSError):
                continue
        return dict(status="cleaned_bounded", visited=visited, removed=removed)
'''
