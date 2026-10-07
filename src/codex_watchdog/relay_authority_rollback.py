"""Offline rollback before the first canonical relay-state mutation.

The canonical authority and its evidence remain recoverable. This never exports
current claims into an old journal or restores stale deduplication after use.
Native sessions, queues, owners, settings and credentials are untouched.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .control_state import ControlError, control_atomic_json, control_read_json
from .linux_relay_migration import (
    _digest, _quiescence, _safe_json, _safe_path, _validate_runtime, plan_authority,
)
from .messaging_profile import write_new
from .node_observation import observation_lock
from .relay_authority_migration import read_snapshot


_NAMES = frozenset(("relay-state.json", "poll-relay-state.json", "poll-cursors.json", "poll-active-parents.json"))
_NOTIFICATION_NAMES = frozenset(("last-events.json", "dual-deliveries.json"))
_STORAGE = "cluster_provider_journals_v1"


def _bytes_sha(value):
    return hashlib.sha256(value).hexdigest()


def _bound_path(home, value):
    if not isinstance(value, str):
        raise ControlError("relay_rollback_path_invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ControlError("relay_rollback_path_invalid")
    _safe_path(path)
    try:
        relative = path.relative_to(home / "watchdog-nodes")
    except ValueError:
        raise ControlError("relay_rollback_path_invalid") from None
    if len(relative.parts) < 3:
        raise ControlError("relay_rollback_path_invalid")
    return path


def _validate_restorations(home, manifest, digest):
    entries = manifest.get("json_sources")
    if not isinstance(entries, list):
        raise ControlError("relay_rollback_original_json_index_missing")
    restorations, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ControlError("relay_rollback_original_json_index_invalid")
        path = _bound_path(home, entry.get("path"))
        known = (path.name in _NAMES or
                 (path.name in _NOTIFICATION_NAMES and path.parent.name == "notifications"))
        if not known or str(path) in seen:
            raise ControlError("relay_rollback_original_json_index_invalid")
        seen.add(str(path))
        expected = entry.get("sha256")
        if (not isinstance(expected, str) or len(expected) != 64
                or any(c not in "0123456789abcdef" for c in expected)):
            raise ControlError("relay_rollback_original_json_index_invalid")
        backup = path.with_name(path.name + ".relay-authority-v1-backup")
        _safe_path(backup)
        try:
            original = backup.read_bytes()
            current = path.read_bytes()
            original_value = json.loads(original)
        except (OSError, ValueError, UnicodeError):
            raise ControlError("relay_rollback_original_json_unavailable") from None
        if (_bytes_sha(original) != expected or not isinstance(original_value, dict)
                or original_value.get("schema_version") not in (1, 2)):
            raise ControlError("relay_rollback_original_json_changed")
        if _bytes_sha(current) != expected:
            try:
                marker = json.loads(current)
            except (ValueError, UnicodeError):
                raise ControlError("relay_rollback_source_not_fenced") from None
            if (not isinstance(marker, dict) or marker.get("schema_version") != 3
                    or marker.get("storage") != _STORAGE or marker.get("plan_sha256") != digest):
                raise ControlError("relay_rollback_source_not_fenced")
        restorations.append(dict(path=str(path), backup=str(backup), sha256=expected))
    return sorted(restorations, key=lambda item: item["path"])


def _restore_bytes(path, data):
    """Publish only already-verified original bytes on the same filesystem."""
    fd, name = tempfile.mkstemp(prefix=".relay-rollback-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _safe_path(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def rollback_authority(home, quiescence, clock=None):
    """Restore an unused previous profile; refuse after canonical admission."""
    import time
    home = Path(home).expanduser().resolve()
    root = home / "watchdog-relay-authority"
    _safe_path(root)
    now = clock or time.time
    with observation_lock(root / "migration.lock"):
        cluster_path = root / "cluster.json"
        _safe_path(cluster_path)
        cluster = control_read_json(cluster_path)
        digest = cluster.get("plan_sha256")
        if (cluster.get("codex_home") != str(home) or cluster.get("storage") != _STORAGE
                or cluster.get("state") not in ("ready", "rolled_back")):
            raise ControlError("relay_rollback_authority_invalid")
        complete_path = root / "rollback-complete.json"
        if cluster.get("state") == "rolled_back" and complete_path.exists():
            complete = _safe_json(complete_path)
            if (complete.get("schema_version") != 1 or complete.get("plan_sha256") != digest
                    or complete.get("state") != "complete"):
                raise ControlError("relay_rollback_completion_invalid")
            if cluster.get("rollback_state") != "complete":
                control_atomic_json(cluster_path, dict(cluster, rollback_state="complete"))
            return dict(status="already_rolled_back", plan_sha256=digest)
        # Pristine canonical rows are mandatory: a post-admission rollback must
        # not revive an older UUID reservation, one-shot ticket or uncertainty.
        _validate_runtime(root, digest, pristine=True)
        manifest = _safe_json(root / "migration-manifest.json")
        if manifest.get("plan_sha256") != digest:
            raise ControlError("relay_rollback_manifest_mismatch")
        summary = dict(manifest)
        summary.pop("plan_sha256", None)
        quarantines = summary.get("quarantines")
        if (not isinstance(quarantines, dict)
                or any(not isinstance(values, list) for values in quarantines.values())):
            raise ControlError("relay_rollback_manifest_mismatch")
        summary["quarantines"] = {provider: len(values) for provider, values in quarantines.items()}
        if _digest(summary) != digest:
            raise ControlError("relay_rollback_manifest_mismatch")
        plan = plan_authority(home)
        if plan["summary"]["nodes"] != manifest.get("nodes"):
            raise ControlError("relay_rollback_nodes_changed")
        _quiescence(plan, quiescence, now())
        for source in manifest.get("sources", ()):
            path = _bound_path(home, source.get("path"))
            backup = path.with_name(path.name + ".relay-authority-v1-backup")
            _safe_path(backup)
            if (read_snapshot(path)[1] != source.get("snapshot_sha256")
                    or read_snapshot(backup)[1] != source.get("snapshot_sha256")):
                raise ControlError("relay_rollback_source_database_changed")
        restorations = _validate_restorations(home, manifest, digest)
        intent_path = root / "rollback-intent.json"
        _safe_path(intent_path)
        intent = dict(schema_version=1, purpose="initial-relay-authority-rollback",
                      plan_sha256=digest, restorations=restorations)
        if intent_path.exists():
            if _safe_json(intent_path) != intent:
                raise ControlError("relay_rollback_recovery_intent_changed")
        else:
            control_atomic_json(intent_path, intent)
        cluster_backup = root / "cluster.json.pre-rollback"
        _safe_path(cluster_backup)
        if cluster.get("state") == "ready":
            original_cluster = cluster_path.read_bytes()
            if cluster_backup.exists():
                if cluster_backup.read_bytes() != original_cluster:
                    raise ControlError("relay_rollback_cluster_backup_conflict")
            else:
                write_new(cluster_backup, original_cluster)
        elif not cluster_backup.exists():
            raise ControlError("relay_rollback_cluster_backup_missing")
        # Candidate readers stop before the first old refusal marker is removed.
        retired = dict(cluster, state="rolled_back", rollback_state="restoring")
        control_atomic_json(cluster_path, retired)
        for entry in restorations:
            path, backup = Path(entry["path"]), Path(entry["backup"])
            _safe_path(path)
            _safe_path(backup)
            data = backup.read_bytes()
            if _bytes_sha(data) != entry["sha256"]:
                raise ControlError("relay_rollback_original_json_changed")
            current = path.read_bytes()
            if _bytes_sha(current) != entry["sha256"]:
                value = json.loads(current)
                if (value.get("schema_version") != 3 or value.get("storage") != _STORAGE
                        or value.get("plan_sha256") != digest):
                    raise ControlError("relay_rollback_source_not_fenced")
                _restore_bytes(path, data)
        complete = dict(schema_version=1, state="complete", plan_sha256=digest,
                        restored_json_count=len(restorations), canonical_runtime_retained=True)
        control_atomic_json(complete_path, complete)
        control_atomic_json(cluster_path, dict(retired, rollback_state="complete"))
        return dict(status="rolled_back", plan_sha256=digest,
                    restored_json_count=len(restorations), canonical_runtime_retained=True)
