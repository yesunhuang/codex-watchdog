"""Optional bounded completion forensics without acquiring an executor.

Unknown historic outcomes never gate independent traffic or handoff. This
explicit path grants no capability and never imports, enrolls, dispatches,
polls a provider, or edits native ownership. Only existing receipt progress and
its original event's proven completion metadata may change.
"""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3

from .control_state import ControlError, control_native_repo, control_read_json
from .models import sha256_text
from .node_observation import NodeObservation, observation_lock
from .relay_native_completion import completion_progress, _object, _constant


_JSON_LIMIT = 131072
_BOUNDED_VALUE = "substr(CAST(value AS BLOB),1,131073)"


def _small_json(raw):
    if not isinstance(raw, bytes) or len(raw) > _JSON_LIMIT:
        raise ValueError("relay_completion_record_too_large")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except RecursionError:
        raise ValueError("relay_completion_record_invalid") from None


def _existing_file(path):
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ControlError("relay_completion_existing_state_required")


def _ready(store, observation):
    root = store.codex_home / "watchdog-relay-authority"
    runtime = root / "runtime"
    manifest = root / "sessions" / (store.thread_id + ".json")
    for path in (root, root / "sessions", runtime, runtime / "locks"):
        if path.is_symlink() or path.resolve() != path or not path.is_dir():
            raise ControlError("relay_completion_path_invalid")
    for path in (root / "cluster.json", manifest, store.path, store.codex_home / "state_5.sqlite",
                 observation.path, observation.lock_path):
        _existing_file(path)
    cluster = control_read_json(root / "cluster.json")
    selected = control_read_json(manifest)
    if (type(cluster.get("schema_version")) is not int or cluster["schema_version"] != 1
            or type(selected.get("schema_version")) is not int or selected["schema_version"] != 1
            or not isinstance(cluster.get("plan_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", cluster["plan_sha256"]) is None
            or selected.get("plan_sha256") != cluster["plan_sha256"]
            or cluster.get("state") != "ready"
            or cluster.get("codex_home") != str(store.codex_home)
            or cluster.get("storage") != "cluster_provider_journals_v1"
            or selected.get("thread_id") != store.thread_id
            or selected.get("repo_path") != store.repo_path
            or selected.get("state") != "ready"
            or selected.get("storage") != "cluster_provider_journals_v1"):
        raise ControlError("relay_completion_manifest_invalid")
    # An existing exact native registration is evidence of identity, not an
    # ownership claim. A reboot, paused owner or live writer need no mutation.
    store.read()
    if control_native_repo(store.codex_home, store.thread_id) != store.repo_path:
        raise ControlError("relay_completion_exact_session_unavailable")
    if observation._read() is None:
        raise ControlError("relay_completion_observation_missing")
    providers = cluster.get("providers", ())
    if not isinstance(providers, (list, tuple)):
        raise ControlError("relay_completion_manifest_invalid")
    databases = []
    for provider in ("slack", "lark", "onebot"):
        path = runtime / provider / "reply-tickets.sqlite3"
        if path.exists() or path.is_symlink() or provider in providers:
            databases.append((provider, path))
    return runtime, databases


@contextmanager
def _existing_transaction(database):
    # mode=rw refuses a missing DB. Do not use ReplyTickets.transaction here:
    # that normal ingress path may create/migrate schemas or namespace markers.
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=1)) as db:
        db.execute("PRAGMA synchronous=FULL")
        try:
            db.execute("BEGIN IMMEDIATE")
            _schema(db)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _schema(db):
    if db.execute("PRAGMA user_version").fetchone()[0] not in (2,3):
        raise ControlError("relay_completion_database_schema_invalid")
    fields = [row[2] for row in db.execute("PRAGMA index_info(session_relay_pending)")]
    definition = db.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                            "AND name='session_relay_pending'").fetchone()
    sql = re.sub(r"\s+", "", definition[0]).lower() if definition and isinstance(definition[0], str) else ""
    sql = sql.replace("ifnotexists", "")
    if (fields != ["thread_id", "namespace", "key"]
            or sql != "createindexsession_relay_pendingonrecords(thread_id,namespace,key)"
                      "whereactive=1andkind='events'"):
        raise ControlError("relay_completion_pending_index_invalid")


def _queue_absent(store, receipt):
    paths = list(store.codex_home.glob("queue_*.sqlite"))
    if len(paths) != 1:
        return False
    try:
        _existing_file(paths[0])
        with closing(sqlite3.connect(paths[0].as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            return db.execute("SELECT 1 FROM queued_items WHERE thread_id=? AND id=?",
                              (store.thread_id, receipt["queue_message_id"])).fetchone() is None
    except (ControlError, sqlite3.Error, KeyError):
        return False


def _settle(db, store, namespace, key, raw, budget):
    if (not isinstance(namespace, str) or len(namespace) > 1024
            or not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None):
        return False
    if db.execute("SELECT 1 FROM sources WHERE namespace=?", (namespace,)).fetchone() is None:
        raise ControlError("relay_completion_namespace_missing")
    row = db.execute("SELECT " + _BOUNDED_VALUE + ",thread_id FROM records WHERE namespace=? "
                     "AND kind='relay_native_receipts' AND key=?", (namespace, key)).fetchone()
    if row is None:
        return False
    try:
        entry, proof = _small_json(raw), _small_json(row[0])
    except (TypeError, ValueError):
        return False
    if (not isinstance(entry, dict) or not isinstance(proof, dict)
            or entry.get("relay_authority_quarantine") is True
            or proof.get("relay_authority_quarantine") is True):
        return False
    receipt = proof.get("receipt")
    if (row[1] != store.thread_id or proof.get("thread_id") != store.thread_id
            or not isinstance(receipt, dict) or receipt.get("thread_id") != store.thread_id
            or receipt.get("instruction_id") != entry.get("instruction_id")
            or receipt.get("prompt_sha256") != entry.get("text_sha256")
            or not isinstance(proof.get("progress"), dict)):
        return False
    if not isinstance(receipt.get("rollout_path"), str):
        return False
    path = Path(receipt["rollout_path"])
    try:
        _existing_file(path)
        path.relative_to(store.codex_home / "sessions")
    except (ControlError, ValueError, OSError):
        return False
    progress, completed = completion_progress(receipt, proof["progress"], budget=budget)
    proof["progress"] = progress
    db.execute("UPDATE records SET value=? WHERE namespace=? AND kind='relay_native_receipts' "
               "AND key=? AND thread_id=? AND value=?",
               (json.dumps(proof, sort_keys=True), namespace, key, store.thread_id, row[0].decode("utf-8")))
    if not completed:
        return False
    try:
        _existing_file(path)
        current = path.stat()
        if current.st_ino != progress["inode"] or current.st_size < progress["observed_size"]:
            return False
    except (ControlError, OSError):
        return False
    if not _queue_absent(store, receipt):
        return False
    entry.update(native_completed=True, native_completion_turn_id=progress["turn_id"],
                 native_completion_receipt_sha256=progress["receipt_sha256"])
    changed = db.execute("UPDATE records SET value=?,active=0 WHERE namespace=? AND kind='events' "
                         "AND key=? AND thread_id=? AND active=1 AND value=?",
                         (json.dumps(entry, sort_keys=True), namespace, key, store.thread_id, raw.decode("utf-8")))
    if changed.rowcount != 1:
        raise ControlError("relay_completion_event_changed")
    return True


def reconcile_existing_native_completions(store):
    """Bounded proof-only settlement for an existing registered exact session.

    No token, selected relay epoch or native writer is acquired. A completed
    receipt says nothing about current idle state or another pending Git wake.
    """
    from .relay_authority import _NATIVE_COMPLETION_BUDGET

    observation = NodeObservation(store, {})
    # Validate before locking too: an absent lock must not be manufactured as
    # part of a supposedly existing-state reconciliation.
    runtime, _ = _ready(store, observation)
    result = dict(thread_sha256=sha256_text(store.thread_id), examined=0, completed=0, refusals=[])
    with observation_lock(observation.lock_path):
        runtime, databases = _ready(store, observation)
        for provider, database in databases:
            try:
                _existing_file(database)
                lock = runtime / "locks" / (provider + "-authority.lock")
                # A never-used compatible provider may have no lock/cursor yet.
                # A missing lock cannot be manufactured by this existing lane.
                if not lock.exists() and not lock.is_symlink():
                    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                        _schema(db)
                        pending = db.execute("SELECT 1 FROM records INDEXED BY session_relay_pending "
                            "WHERE thread_id=? AND active=1 AND kind='events' LIMIT 1", (store.thread_id,)).fetchone()
                    if pending is None:
                        continue
                    raise ControlError("relay_completion_existing_state_required")
                _existing_file(lock)
                with observation_lock(lock), _existing_transaction(database) as db:
                    pending = db.execute("SELECT 1 FROM records INDEXED BY session_relay_pending "
                        "WHERE thread_id=? AND active=1 AND kind='events' LIMIT 1", (store.thread_id,)).fetchone()
                    if pending is None:
                        continue
                    namespace = provider + "/authority-cursors.json"
                    if db.execute("SELECT 1 FROM sources WHERE namespace=?", (namespace,)).fetchone() is None:
                        raise ControlError("relay_completion_namespace_missing")
                    cursor = db.execute("SELECT " + _BOUNDED_VALUE + " FROM records WHERE namespace=? "
                        "AND kind='relay_reconcile_cursor' AND key=?", (namespace, store.thread_id)).fetchone()
                    present = cursor is not None
                    cursor = _small_json(cursor[0]) if present else {}
                    if present and (not isinstance(cursor, dict) or cursor.get("thread_id") != store.thread_id):
                        raise ControlError("relay_completion_cursor_invalid")
                    after = cursor.get("after", ("", ""))
                    if (not isinstance(after, (list, tuple)) or len(after) != 2
                            or not all(isinstance(item, str) and len(item) <= 1024 for item in after)):
                        raise ControlError("relay_completion_cursor_invalid")
                    after = tuple(after)
                    query = ("SELECT namespace,key," + _BOUNDED_VALUE + " FROM records INDEXED BY session_relay_pending "
                             "WHERE thread_id=? AND active=1 AND kind='events' ")
                    rows = db.execute(query + "AND (namespace,key)>(?,?) ORDER BY namespace,key LIMIT 4",
                                      (store.thread_id, *after)).fetchall()
                    if len(rows) < 4:
                        rows += db.execute(query + "AND (namespace,key)<=(?,?) ORDER BY namespace,key LIMIT ?",
                                           (store.thread_id, *after, 4-len(rows))).fetchall()
                    completed = 0
                    refused = []
                    for old_namespace, key, raw in rows:
                        try:
                            completed += int(_settle(db, store, old_namespace, key, raw,
                                                   _NATIVE_COMPLETION_BUDGET))
                        except (ControlError, ValueError, OSError, RecursionError) as error:
                            # A malformed parent retains its original proof and
                            # pending event, while the completion cursor rotates.
                            # Schema/SQLite faults still roll back this provider.
                            reason = str(error) if isinstance(error, ControlError) else "relay_completion_record_invalid"
                            refused.append(dict(provider=provider, reason=reason))
                        advanced = dict(cursor, after=[old_namespace, key], thread_id=store.thread_id)
                        db.execute("INSERT INTO records(namespace,kind,key,value,thread_id) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(namespace,kind,key) DO UPDATE SET value=excluded.value",
                            (namespace, "relay_reconcile_cursor", store.thread_id,
                             json.dumps(advanced, sort_keys=True), store.thread_id))
                result["examined"] += len(rows)
                result["completed"] += completed
                result["refusals"] += refused
            except (ControlError, sqlite3.Error, OSError, ValueError, RecursionError) as error:
                # A refused journal never grants execution, and must not stop
                # healthy providers settling their own exact original proofs.
                reason = str(error) if isinstance(error, ControlError) else "relay_completion_database_unavailable"
                result["refusals"].append(dict(provider=provider, reason=reason))
    return result
