"""Explicit cold upgrade of the old suppression cache; no runtime history scan.

Only keys proven to belong to current configured scopes are retained. This is
WatchDog suppression state, never credentials, grants, native or user history.
The operator owns foreground/migration and notification locks before calling.
"""
import hashlib
import json
from pathlib import Path
import re

from .messaging_profile import write_new
from .models import utc_now
from .notification_state import CurrentNotificationState
from .storage import InstructionStore

_DIGEST = re.compile(r"[0-9a-f]{64}")


def migrate_notification_state(runtime, *, current_keys):
    root = Path(runtime).expanduser().resolve()
    keys = sorted(set(current_keys))
    if any(not isinstance(key, str) or _DIGEST.fullmatch(key) is None for key in keys):
        raise ValueError("notification_migration_current_scope_invalid")
    directory = root / "notifications"
    source = directory / "last-events.json"
    backup = directory / "current-suppression-upgrade.json"
    marker = directory / "current-suppression-ready.json"
    if directory.resolve() != directory or any(path.is_symlink() for path in (source, backup, marker)):
        raise ValueError("notification_migration_path_redirected")
    store = CurrentNotificationState(root)
    if marker.exists():
        value = json.loads(marker.read_bytes())
        if value.get("schema_version") != 1:
            raise ValueError("notification_migration_scope_changed")
        for key in value.get("imported_keys", ()):
            store.read(key)  # Point verification; never restore an old claim.
        if source.exists():
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != value.get("source_sha256"):
                raise ValueError("notification_migration_legacy_changed")
            source.unlink()  # Complete only the already committed retirement.
        return dict(status="unchanged", current_count=len(value.get("imported_keys", ())))
    if not backup.exists():
        raw = source.read_bytes() if source.exists() else b""
        old = json.loads(raw) if raw else dict(schema_version=1, last_events={})
        if (not isinstance(old, dict) or old.get("schema_version") != 1
                or not isinstance(old.get("last_events"), dict)):
            raise ValueError("notification_migration_legacy_cache_invalid")
        selected = {key: old["last_events"][key] for key in keys if key in old["last_events"]}
        value = dict(schema_version=1, current_keys=keys, protected_current=selected,
                     source_sha256=hashlib.sha256(raw).hexdigest(), created_at=utc_now())
        # Compact rollback evidence contains current scopes only, not an archive
        # of obsolete event keys. Never replace a previous recovery snapshot.
        write_new(backup, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())
    else:
        value = json.loads(backup.read_bytes())
        if value.get("schema_version") != 1 or value.get("current_keys") != keys:
            raise ValueError("notification_migration_backup_scope_changed")
        raw = source.read_bytes() if source.exists() else b""
        if source.exists() and hashlib.sha256(raw).hexdigest() != value.get("source_sha256"):
            raise ValueError("notification_migration_legacy_changed")
    selected = value["protected_current"]
    for key, entry in selected.items():
        store.import_current(key, entry)
        saved = store.read(key)
        if saved["current"]["fingerprint"] != entry["fingerprint"]:
            raise ValueError("notification_migration_current_claim_changed")
    ready = dict(schema_version=1, current_keys=keys, imported_keys=sorted(selected),
                 source_sha256=value["source_sha256"], completed_at=utc_now(),
                 rollback_policy="forward_recovery_only_after_new_claims")
    InstructionStore._atomic_json(marker, ready)
    # Publication precedes deletion. A crash before deletion is idempotent; a
    # crash before publication keeps original source and compact backup usable.
    if source.exists():
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != value["source_sha256"]:
            raise ValueError("notification_migration_source_changed_before_retirement")
        source.unlink()
    return dict(status="migrated", current_count=len(selected), retired_legacy_cache=True)
