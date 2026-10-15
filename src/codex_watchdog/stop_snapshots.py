"""Compact current Stop state; legacy audit history is an explicit import only.

One atomic record belongs to one exact session and repository.  It is a current
completion and a small ordering boundary, not a journal of completed turns.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Dict, Optional

from .models import utc_now
from .storage import FileLock, InstructionStore, StoreBusyError


STOP_SCHEMA_VERSION = 1
STOP_MAX_BYTES = 131_072
STOP_RETENTION = timedelta(hours=72)
STOP_CLEANUP_BUDGET = 16
PARKED_STOP_OUTCOMES = frozenset({
    "continuation_confirmed_then_parked", "duplicate_turn_parked",
    "grace_expired_parked", "handler_error_failed_open",
    "invalid_stop_context_parked", "lock_busy_failed_open",
    "lock_busy_grace_expired_parked", "loop_guard_parked",
})
_DIAGNOSTIC_TYPES = frozenset({"mvp_workspace_cycle", "remote_ssh_workspace_cycle",
                               "queue_wake"})


def timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def stop_scope(session_id: str, workspace: Path) -> tuple[str, str, str]:
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        raise ValueError("invalid Stop session")
    path = Path(workspace).expanduser()
    if not path.is_absolute():
        raise ValueError("invalid Stop workspace")
    root = str(path.resolve())
    key = hashlib.sha256((session_id + "\0" + root).encode("utf-8")).hexdigest()
    return key, session_id, root


def valid_stop(event: Any) -> bool:
    if not isinstance(event, dict) or type(event.get("schema_version")) is not int:
        return False
    if (event["schema_version"] != 1 or event.get("event_type") != "Stop"
            or event.get("outcome") not in PARKED_STOP_OUTCOMES):
        return False
    for key in ("audit_id", "invocation_id", "session_id", "turn_id", "workspace"):
        if not isinstance(event.get(key), str) or not event[key] or len(event[key]) > 4096:
            return False
    started, completed = timestamp(event.get("hook_started_at")), timestamp(event.get("hook_completed_at"))
    if started is None or completed is None or completed < started or timestamp(event.get("recorded_at")) is None:
        return False
    chars, digest = event.get("last_output_chars"), event.get("last_output_sha256")
    if (type(chars) is not int or chars < 0
            or type(event.get("last_assistant_message_available")) is not bool
            or (chars > 0 and event["last_assistant_message_available"] is not True)
            or (chars == 0 and digest is not None)
            or (chars > 0 and (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None))):
        return False
    try:
        stop_scope(event["session_id"], Path(event["workspace"]))
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def _identity(event: Dict[str, Any]) -> str:
    fields = {key: event.get(key) for key in (
        "session_id", "workspace", "turn_id", "hook_started_at",
        "last_assistant_message_available", "last_output_sha256", "last_output_chars")}
    fields["workspace"] = str(Path(fields["workspace"]).expanduser().resolve())
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _read(path: Path) -> Optional[Dict[str, Any]]:
    if path.is_symlink():
        return None
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return None
        with path.open("rb") as handle:
            raw = handle.read(STOP_MAX_BYTES + 1)
        if len(raw) > STOP_MAX_BYTES:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def _file_revision(info):
    # Reading may update access time. It is not a replacement/content change,
    # and comparing complete stat_result can suppress every retirement on
    # Windows when the read crosses an access-time tick.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class StopSnapshots:
    def __init__(self, runtime: Path, *, writer: Callable = InstructionStore._atomic_json):
        self.runtime = Path(runtime)
        self.directory = self.runtime / "stop-current"
        self.audit = self.runtime / "audit"
        self.retired = self.runtime / "audit-retired"
        self.writer = writer
        self._cleanup_entries = None
        self._cleanup_identity = None

    def path(self, session_id: str, workspace: Path) -> Path:
        return self.directory / (stop_scope(session_id, workspace)[0] + ".json")

    def _snapshot(self, session_id: str, workspace: Path) -> Optional[Dict[str, Any]]:
        key, session, root = stop_scope(session_id, workspace)
        if self.directory.is_symlink():
            return None
        value = _read(self.directory / (key + ".json"))
        if (value is None or type(value.get("schema_version")) is not int
                or value["schema_version"] != STOP_SCHEMA_VERSION
                or value.get("kind") != "current_stop"
                or value.get("session_id") != session or value.get("workspace") != root
                or type(value.get("revision")) is not int or value["revision"] <= 0
                or timestamp(value.get("published_at")) is None
                or not valid_stop(value.get("event"))):
            return None
        event = value["event"]
        instant = timestamp(utc_now())
        if (instant is None or timestamp(event["hook_completed_at"]) > instant + timedelta(seconds=5)
                or timestamp(value["published_at"]) > instant + timedelta(seconds=5)):
            return None
        if (stop_scope(event["session_id"], Path(event["workspace"])) != (key, session, root)
                or value.get("identity") != _identity(event)
                or value.get("floor_started_at") != event["hook_started_at"]
                or value.get("floor_completed_at") != event["hook_completed_at"]):
            return None
        name = value.get("legacy_name")
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".json"):
            return None
        return value

    def _boundary(self, session_id: str, workspace: Path) -> Optional[Dict[str, Any]]:
        key, session, root = stop_scope(session_id, workspace)
        if self.directory.is_symlink():
            return None
        value = _read(self.directory / (key + ".floor.json"))
        if (value is None or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1 or value.get("kind") != "stop_boundary"
                or value.get("session_id") != session or value.get("workspace") != root
                or not isinstance(value.get("turn_id"), str) or not value["turn_id"]
                or not isinstance(value.get("invocation_id"), str) or not value["invocation_id"]
                or not isinstance(value.get("identity"), str)
                or re.fullmatch(r"[0-9a-f]{64}", value["identity"]) is None
                or type(value.get("revision")) is not int or value["revision"] <= 0
                or timestamp(value.get("published_at")) is None
                or timestamp(value.get("floor_started_at")) is None
                or timestamp(value.get("floor_completed_at")) is None
                or timestamp(value["floor_completed_at"]) < timestamp(value["floor_started_at"])):
            return None
        instant = timestamp(utc_now())
        if (instant is None or timestamp(value["floor_completed_at"]) > instant + timedelta(seconds=5)
                or timestamp(value["published_at"]) > instant + timedelta(seconds=5)):
            return None
        return value

    def read(self, session_id: str, workspace: Path) -> Optional[Dict[str, Any]]:
        value = self._snapshot(session_id, workspace)
        if value is None:
            return None
        boundary = self._boundary(session_id, workspace)
        if (boundary is None or any(value.get(key) != boundary.get(key) for key in
                ("session_id", "workspace", "identity", "revision", "published_at",
                 "floor_started_at", "floor_completed_at"))
                or value["event"]["turn_id"] != boundary["turn_id"]
                or value["event"]["invocation_id"] != boundary["invocation_id"]):
            return None
        return value

    def has_claimed_turn(self, session_id: str, workspace: Path, turn_id: str) -> bool:
        """A reserved Stop is not completed, but cannot qualify for a retry."""
        boundary = self._boundary(session_id, workspace)
        return bool(boundary is not None and boundary["turn_id"] == turn_id)

    def read_boundary(self, session_id: str, workspace: Path) -> Optional[Dict[str, Any]]:
        """Current chronological reservation, not a completed-output assertion."""
        return self._boundary(session_id, workspace)

    def publish(self, event: Dict[str, Any], *, legacy_name: Optional[str] = None,
                imported: bool = False) -> bool:
        """Publish only a newer exact completion.  A bad old record is local.

        The producer writes the terminal spool first.  The entire association is
        then committed in this single atomic snapshot.  No audit-history scan is
        used to recover a missing or corrupt snapshot.
        """
        if not valid_stop(event):
            return False
        instant = timestamp(utc_now())
        if instant is None or timestamp(event["hook_completed_at"]) > instant + timedelta(seconds=5):
            return False
        if imported and timestamp(event["recorded_at"]) > instant + timedelta(seconds=5):
            return False
        encoded = json.dumps(event, ensure_ascii=False).encode("utf-8")
        if len(encoded) > STOP_MAX_BYTES // 2:
            return False
        key, session, root = stop_scope(event["session_id"], Path(event["workspace"]))
        path = self.directory / (key + ".json")
        if self.directory.is_symlink() or path.is_symlink():
            return False
        name = legacy_name or (str(event["recorded_at"]).replace(":", "-") + "_" + event["audit_id"] + ".json")
        if Path(name).name != name or not name.endswith(".json"):
            return False
        with FileLock(self.runtime / "locks" / ("stop-" + key + ".lock")):
            previous = self._snapshot(session, Path(root))
            boundary = self._boundary(session, Path(root))
            floor_path = self.directory / (key + ".floor.json")
            if floor_path.is_symlink():
                return False
            if boundary is None and floor_path.exists() and previous is None:
                # Lost forensic evidence cannot veto a new native completion.
                # It also cannot turn an old callback into a newly published
                # effect. Hook terminal records are written immediately after
                # completion; deferred legacy evidence uses explicit import.
                if imported or instant - timestamp(event["hook_completed_at"]) > timedelta(seconds=5):
                    return False
            if previous is not None and (boundary is None or previous["revision"] > boundary["revision"]):
                # A valid current association is sufficient to recover a bad
                # compact floor; no directory/history recovery is permitted.
                boundary = {key: previous[key] for key in ("revision", "identity", "published_at",
                    "floor_started_at", "floor_completed_at")}
                boundary["turn_id"] = previous["event"]["turn_id"]
                boundary["invocation_id"] = previous["event"]["invocation_id"]
            if boundary is not None:
                # Same-turn retries never create a new eligible completion,
                # including changed output and a second invocation's callback.
                if boundary["turn_id"] == event["turn_id"]:
                    return False
                order = (timestamp(event["hook_started_at"]), timestamp(event["hook_completed_at"]))
                old_order = (timestamp(boundary["floor_started_at"]), timestamp(boundary["floor_completed_at"]))
                if order <= old_order:
                    return False
            value = {**(previous or {}),
                "schema_version": STOP_SCHEMA_VERSION, "kind": "current_stop",
                "session_id": session, "workspace": root,
                "revision": boundary["revision"] + 1 if boundary else 1,
                "published_at": event.get("recorded_at") if imported else utc_now(),
                "identity": _identity(event), "legacy_name": name,
                "floor_started_at": event["hook_started_at"],
                "floor_completed_at": event["hook_completed_at"], "event": dict(event)}
            if timestamp(value["published_at"]) is None:
                return False
            claim = {**(boundary or {}), "schema_version": 1, "kind": "stop_boundary",
                "session_id": session, "workspace": root, "turn_id": event["turn_id"],
                "invocation_id": event["invocation_id"],
                **{key: value[key] for key in ("revision", "identity", "published_at",
                   "floor_started_at", "floor_completed_at")}}
            # Reserving the exact terminal before publishing prevents a missing
            # or corrupt payload and a retry from making old output fresh.
            self.writer(floor_path, claim)
            self.writer(path, value)
            return True

    def record(self, event: Dict[str, Any]) -> Path:
        """Replace one current diagnostic/phase, never append historical files."""
        if event.get("event_type") == "Stop":
            self.publish(event)
            phase = "terminal" if event.get("hook_completed_at") else "waiting"
        else:
            phase = "current"
        scope = {key: event.get(key) for key in ("session_id", "workspace", "workspace_id")}
        kind = event.get("event_type")
        if kind not in _DIAGNOSTIC_TYPES and kind not in ("Stop", "PermissionRequest"):
            kind = "diagnostic"
        key = hashlib.sha256(json.dumps([kind, phase, scope], sort_keys=True,
                         ensure_ascii=False).encode("utf-8")).hexdigest()
        path = self.audit / ("current-" + key + ".json")
        if self.audit.is_symlink() or path.is_symlink():
            raise ValueError("current audit path is a symlink")
        if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > STOP_MAX_BYTES:
            raise ValueError("current audit record is oversized")
        self.writer(path, event)
        return path

    def import_legacy(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Explicit one-time upgrade work; never called by normal observation.

        Legacy files are not rewritten.  Only known, expired, completed
        WatchDog-only diagnostics without a terminal spool are moved aside for
        later bounded deletion.  Waiting/return-intent/unknown files stay intact.
        """
        marker = self.directory / "legacy-import.json"
        if self.directory.is_symlink():
            raise ValueError("Stop import directory is a symlink")
        with FileLock(self.runtime / "locks" / "stop-import.lock"):
            prior = _read(marker)
            if prior is not None:
                if type(prior.get("schema_version")) is not int or prior["schema_version"] != 1 or prior.get("kind") != "stop_legacy_import" or prior.get("state") != "completed":
                    raise ValueError("Stop import marker is malformed")
                return {**prior, "already_completed": True}
            if marker.exists() or marker.is_symlink():
                raise ValueError("Stop import marker is unreadable")
            instant = now or datetime.now(timezone.utc)
            if instant.utcoffset() is None:
                raise ValueError("Stop import clock is invalid")
            counts = {"visited": 0, "published": 0, "retired": 0, "retained": 0}
            if self.audit.is_symlink() or self.retired.is_symlink():
                raise ValueError("legacy audit path is a symlink")
            if self.audit.is_dir():
                with os.scandir(self.audit) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".json") or entry.name.startswith("current-"):
                            continue
                        counts["visited"] += 1
                        path = Path(entry.path)
                        value = _read(path)
                        if value is None:
                            counts["retained"] += 1
                            continue
                        if valid_stop(value):
                            try:
                                counts["published"] += int(self.publish(value, legacy_name=entry.name, imported=True))
                            except (StoreBusyError, OSError, ValueError):
                                counts["retained"] += 1
                                continue
                        recorded = timestamp(value.get("recorded_at"))
                        completed = timestamp(value.get("hook_completed_at"))
                        if completed is not None and recorded is not None:
                            recorded = max(recorded, completed)
                        known = (valid_stop(value) or value.get("event_type") in _DIAGNOSTIC_TYPES)
                        invocation = value.get("invocation_id")
                        spool = self.runtime / "transient" / "stop-output" / (str(invocation) + ".json")
                        if (not known or type(value.get("schema_version")) is not int
                                or value["schema_version"] != 1 or recorded is None
                                or instant - recorded < STOP_RETENTION
                                or (invocation is not None and (not isinstance(invocation, str)
                                    or Path(invocation).name != invocation or spool.exists() or spool.is_symlink()))):
                            counts["retained"] += 1
                            continue
                        # Import is serialized with other importers.  Historical
                        # audit names are immutable producer records, not queues.
                        before = path.stat()
                        self.retired.mkdir(parents=True, exist_ok=True)
                        target = self.retired / (hashlib.sha256(entry.name.encode()).hexdigest() + ".json")
                        if target.exists() or target.is_symlink() or path.is_symlink():
                            counts["retained"] += 1
                            continue
                        if _file_revision(path.stat()) != _file_revision(before):
                            counts["retained"] += 1
                            continue
                        os.rename(path, target)
                        counts["retired"] += 1
            result = {"schema_version": 1, "kind": "stop_legacy_import", "state": "completed",
                      "completed_at": utc_now(), **counts}
            self.writer(marker, result)
            return result

    def cleanup(self, *, budget: int = STOP_CLEANUP_BUDGET) -> int:
        """Delete at most budget explicitly retired files, never scan live data."""
        if type(budget) is not int or not 0 <= budget <= STOP_CLEANUP_BUDGET:
            raise ValueError("Stop cleanup budget is invalid")
        if not budget or not self.retired.is_dir() or self.retired.is_symlink():
            self.close()
            return 0
        removed = 0
        with FileLock(self.runtime / "locks" / "stop-cleanup.lock"):
            info = self.retired.stat()
            identity = (info.st_dev, info.st_ino)
            if self._cleanup_entries is not None and self._cleanup_identity != identity:
                self.close()
            if self._cleanup_entries is None:
                self._cleanup_entries = os.scandir(self.retired)
                self._cleanup_identity = identity
            for _ in range(budget):
                entry = next(self._cleanup_entries, None)
                if entry is None:
                    self.close()
                    break
                try:
                    if re.fullmatch(r"[0-9a-f]{64}\.json", entry.name) is None or not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    before = path.stat()
                    value = _read(path)
                    recorded = timestamp(value.get("recorded_at")) if value else None
                    completed = timestamp(value.get("hook_completed_at")) if value else None
                    if completed is not None and recorded is not None:
                        recorded = max(recorded, completed)
                    if (value is None or type(value.get("schema_version")) is not int
                            or value["schema_version"] != 1 or recorded is None
                            or datetime.now(timezone.utc) - recorded < STOP_RETENTION
                            or not (valid_stop(value) or value.get("event_type") in _DIAGNOSTIC_TYPES)):
                        continue
                    invocation = value.get("invocation_id")
                    if invocation is not None:
                        if not isinstance(invocation, str) or Path(invocation).name != invocation:
                            continue
                        spool = self.runtime / "transient" / "stop-output" / (invocation + ".json")
                        if spool.exists() or spool.is_symlink():
                            continue
                    if _file_revision(path.stat()) == _file_revision(before) and not path.is_symlink():
                        path.unlink()
                        removed += 1
                except OSError:
                    continue  # another bounded collector may have retired it
        return removed

    def close(self) -> None:
        if self._cleanup_entries is not None:
            self._cleanup_entries.close()
            self._cleanup_entries = None
            self._cleanup_identity = None

    def expire_scope(self, session_id: str, workspace: Path, *, expected_identity: str,
                     now: Optional[datetime] = None) -> bool:
        """Lifecycle's exact-scope cleanup, only after the completion horizon.

        It never removes a waiting hook, terminal output, queue, owner, grant or
        provider reservation.  The caller retains its normal replay floor.
        """
        key, session, root = stop_scope(session_id, workspace)
        with FileLock(self.runtime / "locks" / ("stop-" + key + ".lock")):
            value = self.read(session, Path(root))
            boundary = self._boundary(session, Path(root))
            if value is None:
                value = boundary
            instant = now or datetime.now(timezone.utc)
            if (value is None or value["identity"] != expected_identity
                    or instant.utcoffset() is None
                    or instant - timestamp(value["floor_completed_at"]) < STOP_RETENTION):
                return False
            invocation = boundary["invocation_id"]
            if Path(invocation).name != invocation:
                return False
            spool = self.runtime / "transient" / "stop-output" / (invocation + ".json")
            if spool.exists() or spool.is_symlink():
                return False
            self.path(session, Path(root)).unlink(missing_ok=True)
            (self.directory / (key + ".floor.json")).unlink()
            return True
