"""Offline, one-time import of opted-in cluster messaging state.

Plans and command output contain hashes/counts, not journal payloads. Backups,
staging, and the canonical authority stay on the existing same-user shared home.
An operator supplies fresh native receipts proving every old listener stopped;
a filesystem lock cannot fence an older GPFS flock-only binary by itself.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .control_state import ControlError, control_atomic_json, control_read_json
from .models import utc_now
from .node_observation import observation_lock
from .relay_authority_migration import read_snapshot, merge_poll_cursors, merge_records_with_quarantine
from .reply_tickets import ReplyTickets
from .storage import InstructionStore

_REFUSAL_NAMES = frozenset(("relay-state.json", "poll-relay-state.json", "poll-cursors.json",
                            "poll-active-parents.json"))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _safe_json(path):
    _safe_path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ControlError("relay_migration_source_unavailable") from None
    if not isinstance(value, dict):
        raise ControlError("relay_migration_source_invalid")
    if value.get("schema_version") == 3 and value.get("storage") == "cluster_provider_journals_v1":
        path = path.with_name(path.name + ".relay-authority-v1-backup")
        _safe_path(path)
        value = json.loads(path.read_text(encoding="utf-8"))
    return value


def _safe_path(path):
    if path.is_symlink() or path.resolve() != path:
        raise ControlError("relay_migration_source_path_invalid")


def _original_json_bytes(path):
    _safe_path(path)
    original = path.read_bytes()
    value = json.loads(original)
    if value.get("schema_version") == 3 and value.get("storage") == "cluster_provider_journals_v1":
        backup = path.with_name(path.name + ".relay-authority-v1-backup")
        _safe_path(backup)
        original = backup.read_bytes()
    return original


def _native_proofs(home, nodes, merged):
    """Import only identical immutable dispatch boundaries, never queue work."""
    from .relay_native_completion import completion_progress
    fields = ("thread_id", "instruction_id", "prompt_sha256", "rollout_path",
              "rollout_baseline_offset", "queue_message_id")
    for rows in merged.values():
        proofs = []
        for row in rows:
            if row["kind"] != "events" or not row["active"]:
                continue
            event = json.loads(row["value"])
            instruction = event.get("instruction_id")
            if not isinstance(instruction, str):
                continue
            key = hashlib.sha256(instruction.encode()).hexdigest()
            found = []
            for node in nodes:
                path = node / "remote-wake" / (key + ".json")
                if not path.exists():
                    continue
                value = _safe_json(path)
                if (value.get("thread_id") != row["thread_id"]
                        or value.get("instruction_id") != instruction
                        or value.get("prompt_sha256") != event.get("text_sha256")):
                    continue
                receipt = {field: value.get(field) for field in fields}
                found.append(receipt)
            if not found or any(value != found[0] for value in found[1:]):
                continue  # No invented completion or node-selected boundary.
            progress, _ = completion_progress(found[0], budget=32)
            if progress.get("invalid"):
                continue
            value = dict(thread_id=row["thread_id"], receipt=found[0], progress=progress)
            proofs.append(dict(namespace=row["namespace"], kind="relay_native_receipts", key=row["key"],
                value=json.dumps(value, sort_keys=True), fingerprint=None,
                thread_id=row["thread_id"], active=0, created_at=None))
        rows.extend(proofs)


def _nodes(home):
    result = []
    for node in sorted((home / "watchdog-nodes").glob("*")):
        marker = node / "node.json"
        if not marker.exists():
            continue
        value = control_read_json(marker)
        if (node.is_symlink() or node.resolve() != node or value.get("node") != node.name
                or value.get("codex_home") != str(home)):
            raise ControlError("relay_migration_node_mismatch")
        result.append(node)
    if not result:
        raise ControlError("relay_migration_no_configured_nodes")
    return result


def plan_authority(codex_home):
    home = Path(codex_home).expanduser().resolve()
    nodes = _nodes(home)
    sources, providers, cursors, sessions, json_sources, notification_sources = [], {}, {}, {}, {}, {}
    node_notification_sources = []
    for node in nodes:
        runtimes = [node / "runtime"] + sorted(node.glob("watchdog-control/*/runtime"))
        for runtime in runtimes:
            for name in ("last-events.json", "dual-deliveries.json"):
                path = runtime / "notifications" / name
                if path.exists():
                    json_sources[str(path)] = hashlib.sha256(_original_json_bytes(path)).hexdigest()
            if (runtime == node / "runtime" and any((runtime / "notifications" / name).exists()
                    for name in ("last-events.json", "dual-deliveries.json", "dual-deliveries.sqlite3"))):
                node_notification_sources.append(str(runtime))
            for provider in ("slack", "lark", "onebot"):
                database = runtime / provider / "reply-tickets.sqlite3"
                provider_json = [path for path in (runtime / provider).rglob("*.json")
                                 if path.name in _REFUSAL_NAMES]
                if not database.exists():
                    if provider_json:
                        raise ControlError("relay_migration_legacy_or_missing_journal")
                    continue
                _safe_path(database)
                rows, digest = read_snapshot(database)
                sources.append(dict(path=str(database), node=node.name, runtime=str(runtime),
                                    provider=provider, snapshot_sha256=digest, records=len(rows)))
                providers.setdefault(provider, []).append((str(database), rows))
                for path in provider_json:
                    json_sources[str(path)] = hashlib.sha256(_original_json_bytes(path)).hexdigest()
                for row in rows:
                    body = json.loads(row["value"])
                    target = body.get("target")
                    if row["kind"] == "threads" and isinstance(target, dict):
                        sessions.setdefault(target["thread_id"], None)
            # Polling cursors are exact controller/session state. A node-wide
            # old cursor must not be guessed into a particular session.
            if runtime.parent.parent.name != "watchdog-control":
                continue
            thread = runtime.parent.name
            sessions.setdefault(thread, None)
            _safe_path(runtime)
            notification_sources.setdefault(thread, []).append(str(runtime))
            for name in ("last-events.json", "dual-deliveries.json"):
                path = runtime / "notifications" / name
                if path.exists():
                    json_sources[str(path)] = hashlib.sha256(_original_json_bytes(path)).hexdigest()
            path = runtime / "slack" / "poll-cursors.json"
            if path.exists():
                cursors.setdefault((thread, "slack", "slack-active"), []).append(_safe_json(path))
            for path in sorted((runtime / "lark").glob("*/poll-active-parents.json")):
                cursors.setdefault((thread, "lark", "lark-active:" + path.parent.name), []).append(_safe_json(path))
    merged, quarantines = {}, {}
    for provider, records in providers.items():
        merged[provider], quarantines[provider] = merge_records_with_quarantine(records)
    _native_proofs(home, nodes, merged)
    database = home / "state_5.sqlite"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        for thread in list(sessions):
            roots = db.execute("SELECT cwd FROM threads WHERE id=? AND archived=0 "
                "AND source='vscode' AND thread_source='user'", (thread,)).fetchall()
            if len(roots) == 1 and isinstance(roots[0][0], str) and Path(roots[0][0]).is_absolute():
                sessions[thread] = str(Path(roots[0][0]).resolve())
            else:
                # Historical records remain dedup evidence without enrolling
                # an archived, ambiguous or replacement native conversation.
                sessions.pop(thread)
    portable_cursors = []
    for (thread, provider, key), copies in sorted(cursors.items()):
        active = {row["key"] for row in merged.get(provider, ())
                  if row["kind"] == "threads" and row["active"] and row["thread_id"] == thread}
        state = merge_poll_cursors(provider, copies, active)
        portable_cursors.append(dict(thread_id=thread, provider=provider, key=key, value=state))
    from .relay_notification_migration import plan_notification_migration
    for thread in sessions:
        notification_sources.setdefault(thread, [])
    for paths in notification_sources.values():
        # These indexed fingerprints include the exact session/workspace.
        # Import suppression only; no global notification row grants a ticket.
        paths.extend(node_notification_sources)
    notifications = {thread: plan_notification_migration(paths)
                     for thread, paths in sorted(notification_sources.items())}
    summary = dict(schema_version=1, nodes=[node.name for node in nodes], sources=sources,
                   sessions=sessions, records={p: len(rows) for p, rows in merged.items()},
                   quarantines={p: len(rows) for p, rows in quarantines.items()},
                   cursor_count=len(portable_cursors),
                   json_sources=[dict(path=path, sha256=digest) for path, digest in sorted(json_sources.items())])
    summary["records_sha256"] = _digest(merged)
    summary["cursors_sha256"] = _digest(portable_cursors)
    summary["notifications"] = {thread: value["summary"] for thread, value in notifications.items()}
    summary["plan_sha256"] = _digest(summary)
    return dict(home=home, nodes=nodes, summary=summary, merged=merged,
                quarantines=quarantines, cursors=portable_cursors, notifications=notifications,
                notification_sources=notification_sources)


def _quiescence(plan, path, now):
    path = Path(path)
    info = path.stat()
    if (path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ControlError("relay_migration_quiescence_receipt_not_private")
    value = _safe_json(path)
    if (value.get("schema_version") != 1 or value.get("purpose") != "offline-relay-migration"
            or value.get("codex_home") != str(plan["home"]) or not isinstance(value.get("nodes"), list)):
        raise ControlError("relay_migration_quiescence_receipt_invalid")
    checked = set()
    for item in value["nodes"]:
        try:
            stamp = datetime.fromisoformat(item["verified_at"].replace("Z", "+00:00"))
            age = now - stamp.timestamp()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ControlError("relay_migration_quiescence_receipt_invalid") from None
        if (stamp.tzinfo is None or not 0 <= age <= 300 or item.get("MainPID") != 0
                or type(item.get("MainPID")) is not int or item.get("ActiveState") != "inactive"
                or item.get("no_watchdog_processes") is not True
                or not isinstance(item.get("boot_id"), str) or not item["boot_id"]
                or item.get("node") in checked):
            raise ControlError("relay_migration_old_listener_not_quiescent")
        checked.add(item["node"])
    if checked != set(plan["summary"]["nodes"]):
        raise ControlError("relay_migration_quiescence_incomplete")
    for node in plan["nodes"]:
        for path in node.glob("watchdog-control/*/owner.json"):
            owner = control_read_json(path)
            state = owner.get("remote_state") or {}
            if owner.get("external_effect") is not None or state.get("pending_instruction_id") is not None:
                raise ControlError("relay_migration_native_effect_pending")
    return _digest(value)


def _backup_database(path, expected):
    _safe_path(path)
    backup = path.with_name(path.name + ".relay-authority-v1-backup")
    _safe_path(backup)
    if backup.exists():
        if read_snapshot(backup)[1] != expected:
            raise ControlError("relay_migration_backup_conflict")
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as source:
        fd, name = tempfile.mkstemp(prefix=".relay-backup-", dir=path.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(str(temporary))) as destination:
                source.backup(destination)
            if read_snapshot(temporary)[1] != expected:
                raise ControlError("relay_migration_source_changed")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.link(temporary, backup)
        finally:
            temporary.unlink(missing_ok=True)


def _fence(path, plan_digest):
    _safe_path(path)
    if not path.exists():
        return
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") == 3:
        if value.get("plan_sha256") != plan_digest:
            raise ControlError("relay_migration_fence_conflict")
        return
    from .messaging_profile import write_new
    original = path.read_bytes()
    backup = path.with_name(path.name + ".relay-authority-v1-backup")
    _safe_path(backup)
    if backup.exists():
        if backup.read_bytes() != original:
            raise ControlError("relay_migration_backup_conflict")
    else:
        write_new(backup, original)
    InstructionStore._atomic_json(path, dict(schema_version=3, storage="cluster_provider_journals_v1",
                                            plan_sha256=plan_digest))


def _validate_runtime(root, digest, *, pristine=False):
    runtime = root / "runtime"
    _safe_path(runtime)
    manifest = _safe_json(runtime / "migration-import.json")
    if manifest.get("plan_sha256") != digest or manifest.get("schema_version") != 1:
        raise ControlError("relay_migration_installed_runtime_invalid")
    for provider, expected in manifest["provider_sha256"].items():
        path = runtime / provider / "reply-tickets.sqlite3"
        _safe_path(path)
        if not path.is_file():
            raise ControlError("relay_migration_installed_database_missing")
        actual = read_snapshot(path)[1]
        if pristine and actual != expected:
            raise ControlError("relay_migration_recovery_runtime_changed")
    from .relay_notification_migration import validate_notification_migration
    for thread, expected in manifest.get("notifications", {}).items():
        path = runtime / "session-notifications" / thread
        validate_notification_migration(path, expected["plan_sha256"])
        if pristine and _notification_digest(path) != manifest["notification_sha256"][thread]:
            raise ControlError("relay_migration_recovery_runtime_changed")
    return manifest


def _notification_digest(path):
    _safe_path(path)
    hashes = {}
    for item in sorted(path.rglob("*")):
        _safe_path(item)
        if item.is_file():
            hashes[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    return _digest(hashes)


def _validate_ready(root, home, value):
    if (value.get("state") != "ready" or value.get("codex_home") != str(home)
            or value.get("storage") != "cluster_provider_journals_v1"):
        raise ControlError("relay_migration_existing_authority_invalid")
    manifest = _validate_runtime(root, value["plan_sha256"])
    for thread, repo in manifest["sessions"].items():
        session = _safe_json(root / "sessions" / (thread + ".json"))
        if (session.get("state") != "ready" or session.get("thread_id") != thread
                or session.get("repo_path") != repo
                or session.get("storage") != "cluster_provider_journals_v1"):
            raise ControlError("relay_migration_installed_session_invalid")


def initialize_empty_authority(codex_home):
    """Fresh node installs need no migration of nonexistent messaging state."""
    home = Path(codex_home).resolve()
    root = home / "watchdog-relay-authority"
    _safe_path(root)
    with observation_lock(root / "migration.lock"):
        if (root / "cluster.json").exists():
            return
        if (root / "migration-intent.json").exists():
            raise ControlError("relay_authority_migration_required")
        nodes = _nodes(home)
        # This probe may run again while an upgrade awaits offline migration.
        # Reject by existence before opening any journal/history, so failed
        # activation never becomes a repeated full-history migration scan.
        for node in nodes:
            for runtime in [node / "runtime"] + sorted(node.glob("watchdog-control/*/runtime")):
                _safe_path(runtime)
                if any((runtime / "notifications" / name).exists() for name in
                       ("last-events.json", "dual-deliveries.json", "dual-deliveries.sqlite3")):
                    raise ControlError("relay_authority_migration_required")
                for provider in ("slack", "lark", "onebot"):
                    if (runtime / provider / "reply-tickets.sqlite3").exists():
                        raise ControlError("relay_authority_migration_required")
                    if any(path.name in _REFUSAL_NAMES for path in (runtime / provider).rglob("*.json")):
                        raise ControlError("relay_migration_legacy_or_missing_journal")
        digest = _digest(dict(schema_version=1, fresh_install=True, nodes=[node.name for node in nodes]))
        control_atomic_json(root / "runtime" / "migration-import.json",
                            dict(schema_version=1, plan_sha256=digest, provider_sha256={}, sessions={}))
        control_atomic_json(root / "cluster.json", dict(schema_version=1, codex_home=str(home),
            state="ready", storage="cluster_provider_journals_v1", plan_sha256=digest,
            nodes=[node.name for node in nodes], activated_at=utc_now(), fresh_install=True))


def apply_authority(codex_home, quiescence, *, clock=None):
    import time
    now = clock or time.time
    home = Path(codex_home).expanduser().resolve()
    root = home / "watchdog-relay-authority"
    _safe_path(root)
    with observation_lock(root / "migration.lock"):
        cluster = root / "cluster.json"
        if cluster.exists():
            value = control_read_json(cluster)
            _validate_ready(root, home, value)
            return dict(status="already_migrated", plan_sha256=value["plan_sha256"])
        plan = plan_authority(home)
        evidence = _quiescence(plan, quiescence, now())
        digest = plan["summary"]["plan_sha256"]
        intent = root / "migration-intent.json"
        value = dict(schema_version=1, plan_sha256=digest, state="prepared", codex_home=str(home))
        if intent.exists():
            if control_read_json(intent) != value:
                raise ControlError("relay_migration_recovery_plan_changed")
        else:
            control_atomic_json(intent, value)
        control_atomic_json(root / "migration-manifest.json",
            dict(plan["summary"], quarantines=plan["quarantines"]))
        for source in plan["summary"]["sources"]:
            _backup_database(Path(source["path"]), source["snapshot_sha256"])
        stage = root / ("stage-" + digest)
        _safe_path(stage)
        stage.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime = root / "runtime"
        for provider, rows in (plan["merged"].items() if not runtime.exists() else ()):
            expected = {(row["namespace"], row["kind"], row["key"]): row for row in rows}
            for cursor in plan["cursors"]:
                if cursor["provider"] == provider:
                    namespace = provider + "/authority-cursors.json"
                    key = cursor["thread_id"] + ":" + cursor["key"]
                    expected[(namespace, "relay_cursors", key)] = dict(namespace=namespace, kind="relay_cursors",
                        key=key, value=json.dumps(dict(thread_id=cursor["thread_id"], value=cursor["value"]), sort_keys=True),
                        fingerprint=None, thread_id=cursor["thread_id"], active=0, created_at=None)
            for row in expected.values():
                row["value"] = json.dumps(json.loads(row["value"]), sort_keys=True,
                                         separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            staged = stage / provider / "reply-tickets.sqlite3"
            if staged.exists():
                previous = read_snapshot(staged)[0]
                if any(expected.get((row["namespace"], row["kind"], row["key"])) != row for row in previous):
                    raise ControlError("relay_migration_unplanned_staged_state")
            journal = ReplyTickets(stage / provider / "migration-state.json",
                stage / "locks" / (provider + "-migration.lock"), provider, stage, lambda: {})
            with journal.transaction() as db:
                db.execute("CREATE INDEX IF NOT EXISTS session_relay_pending "
                           "ON records(thread_id,namespace,key) WHERE active=1 AND kind='events'")
                for row in rows:
                    values = [row[field] for field in ("namespace", "kind", "key", "value", "fingerprint",
                                                       "thread_id", "active", "created_at")]
                    db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?,?)", values)
                    db.execute("INSERT OR IGNORE INTO sources VALUES (?)", (row["namespace"],))
                for cursor in plan["cursors"]:
                    if cursor["provider"] != provider:
                        continue
                    namespace = provider + "/authority-cursors.json"
                    db.execute("INSERT OR IGNORE INTO sources VALUES (?)", (namespace,))
                    db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?, ?,NULL,?,0,NULL)",
                        (namespace, "relay_cursors", cursor["thread_id"] + ":" + cursor["key"],
                         json.dumps(dict(thread_id=cursor["thread_id"], value=cursor["value"]), sort_keys=True),
                         cursor["thread_id"]))
        if not runtime.exists():
            from .relay_notification_migration import apply_notification_migration
            for thread, value in plan["notifications"].items():
                apply_notification_migration(stage / "session-notifications" / thread,
                    plan["notification_sources"][thread], expected_plan_sha256=value["plan_sha256"])
            hashes = {provider: read_snapshot(stage / provider / "reply-tickets.sqlite3")[1]
                      for provider in plan["merged"]}
            control_atomic_json(stage / "migration-import.json", dict(schema_version=1,
                plan_sha256=digest, provider_sha256=hashes, sessions=plan["summary"]["sessions"],
                notifications={thread: value["summary"] for thread, value in plan["notifications"].items()},
                notification_sha256={thread: _notification_digest(stage / "session-notifications" / thread)
                                     for thread in plan["notifications"]}))
        else:
            _validate_runtime(root, digest, pristine=True)
        # Confirm source consistency and native quiescence once more before
        # installing any refusal marker or publishing portable readiness.
        if plan_authority(home)["summary"]["plan_sha256"] != digest:
            raise ControlError("relay_migration_source_changed")
        _quiescence(plan, quiescence, now())
        for source in plan["summary"]["json_sources"]:
            _fence(Path(source["path"]), digest)
        if not runtime.exists():
            os.replace(stage, runtime)
        else:
            stage.rmdir()  # Our empty recovery stage, never unknown content.
        for thread, repo in plan["summary"]["sessions"].items():
            control_atomic_json(root / "sessions" / (thread + ".json"), dict(schema_version=1,
                thread_id=thread, repo_path=repo, state="ready", storage="cluster_provider_journals_v1",
                plan_sha256=digest, quiescence_sha256=evidence,
                notification_plan_sha256=plan["notifications"][thread]["plan_sha256"]))
        control_atomic_json(cluster, dict(schema_version=1, codex_home=str(home), state="ready",
            storage="cluster_provider_journals_v1", plan_sha256=digest,
            providers=sorted(plan["merged"]),
            initial_sessions=plan["summary"]["sessions"],
            nodes=plan["summary"]["nodes"], activated_at=utc_now(), quiescence_sha256=evidence))
        return dict(status="migrated", **plan["summary"])
