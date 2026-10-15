"""Explicit finite wake activation and bounded, current-proof maintenance.

Activation is whole-CODEX_HOME, because legacy courier IDs were home-global.
The caller must hold an existing positively verified fence over ALL old senders,
including foreign sessions, through preparation/publication. Unknown login5 or
an unobservable old sender refuses this family only; no shared files are moved.
The yielded checker must freshly prove that fence, not acquire a new credential
or infer absence from an old receipt. This module never starts native work.

After activation callers retain original source birth on every retry and supply
the exact eligibility context to the embedded claim API. A removed recent ID
is covered by a compact monotone global birth floor, not an eternal tombstone.
Consequently an old delayed independent event may be refused; newer valid births
and actual current Q/turn pins continue. Backward old-sender startup is unsafe.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import uuid

from . import control_state as control
from .native_inflight import validate_inventory


TTL = control._WAKE_TTL
LIMIT = control._WAKE_CAP
BUDGET = 32


def _clock():
    return control._wake_time()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now(clock):
    value = clock()
    if not control._wake_number(value):
        raise control.ControlError("wake_retention_clock_invalid")
    return value


def _scope(home, thread, repo):
    home, repo = Path(home).resolve(), Path(repo).resolve()
    if not control._wake_uuid(thread) or not home.is_dir() or not repo.is_dir():
        raise control.ControlError("wake_retention_scope_invalid")
    return home, repo


def _live(inventory):
    values = inventory["queue"]["items"] + [inventory["native"]]
    result = {}
    for item in values:
        identifier, digest = item.get("instruction_id"), item.get("prompt_sha256")
        if identifier is None:
            continue
        if identifier in result and result[identifier] != digest:
            raise control.ControlError("wake_retention_current_collision")
        result[identifier] = digest
    return result


def _inventory(home, thread, repo, reader, clock):
    if not callable(reader):
        raise control.ControlError("wake_retention_inventory_required")
    value = reader()
    validate_inventory(value, home, thread, repo, clock=clock)
    return value


def _home_fence(check, home, threads, clock):
    if not callable(check):
        raise control.ControlError("wake_retention_all_senders_fence_required")
    value = check()
    now = _now(clock)
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("kind") != "wake_home_sender_fence"
            or value.get("codex_home") != str(home) or value.get("discovery_complete") is not True
            or value.get("all_old_senders_fenced") is not True or value.get("unknown_senders") != []
            or not control._wake_number(value.get("observed_at"))
            or not 0 <= now - value["observed_at"] <= 5
            or not isinstance(value.get("senders"), list)
            or not isinstance(value.get("threads"), list) or sorted(value["threads"]) != sorted(threads)):
        raise control.ControlError("wake_retention_all_senders_fence_required")
    seen = set()
    for sender in value["senders"]:
        if (not isinstance(sender, dict) or sender.get("fenced") is not True
                or not control._wake_uuid(sender.get("thread_id"))
                or any(not isinstance(sender.get(key), str) or not sender[key] or len(sender[key]) > 256
                       for key in ("node", "boot_id", "instance"))):
            raise control.ControlError("wake_retention_all_senders_fence_required")
        seen.add(sender["thread_id"])
    if not set(threads).issubset(seen):
        raise control.ControlError("wake_retention_all_senders_fence_required")
    return value


def _private_dir(path):
    path.mkdir(mode=0o700, exist_ok=True)
    control._wake_private(path, directory=True)


def _atomic(path, value):
    control.control_atomic_json(path, value)
    control._wake_private(path)
    control._wake_attempt_sync(path.parent)


def _selector(path):
    control._wake_private(path, directory=True)
    info = path.stat()
    return dict(device=info.st_dev, inode=info.st_ino)


def _schema(db, home, generation, floor):
    db.executescript("""
    CREATE TABLE metadata(slot INTEGER PRIMARY KEY,generation TEXT NOT NULL,codex_home TEXT NOT NULL,floor REAL NOT NULL);
    CREATE TABLE sessions(thread_id TEXT PRIMARY KEY NOT NULL,repo_path TEXT NOT NULL,generation TEXT NOT NULL,floor REAL NOT NULL,checked_at REAL NOT NULL,pin_cursor TEXT NOT NULL);
    CREATE TABLE claims(instruction_id TEXT PRIMARY KEY NOT NULL,thread_id TEXT NOT NULL,prompt_sha256 TEXT,born REAL,expires REAL,protected INTEGER NOT NULL,value TEXT NOT NULL);
    CREATE INDEX wake_current ON claims(thread_id,protected,instruction_id);
    CREATE INDEX wake_expiry ON claims(thread_id,protected,expires,instruction_id);
    CREATE INDEX wake_pool ON claims(thread_id,protected,born,instruction_id);
    PRAGMA user_version=1;
    """)
    db.execute("INSERT INTO metadata VALUES(1,?,?,?)", (generation, str(home), floor))


def _legacy_record(root, identifier, thread, digest):
    path = root / (hashlib.sha256(identifier.encode()).hexdigest() + ".json")
    if not path.exists() and not path.is_symlink():
        return dict(schema_version=1, kind="wake_attempt", state="claimed", instruction_id=identifier,
                    thread_id=thread, prompt_sha256=digest, origin="legacy",
                    evidence=dict(current_native_reservation=True))
    value = control._wake_json(path)
    if (value.get("schema_version") != 1 or type(value.get("schema_version")) is not int
            or value.get("kind") != "wake_attempt" or value.get("state") != "claimed"
            or value.get("instruction_id") != identifier or value.get("thread_id") != thread
            or value.get("origin") not in ("dispatch", "legacy") or "prompt_sha256" not in value
            or value["prompt_sha256"] is not None and value["prompt_sha256"] != digest
            or value["prompt_sha256"] is None and value["origin"] != "legacy"):
        raise control.ControlError("wake_retention_current_legacy_collision")
    return value


def _publish_sentinel(path, root):
    # One fixed private recovery file, not a growing chain of publication
    # backups. If a process dies after link and before unlink, the two known
    # paths must name precisely the same expected bytes/inode before recovery.
    temporary = root / "sentinel.tmp"
    if temporary.exists() or temporary.is_symlink():
        info = temporary.lstat()
        if (temporary.resolve()!=temporary or not stat.S_ISREG(info.st_mode)
                or info.st_nlink not in (1,2) or hasattr(os,"getuid") and info.st_uid!=os.getuid()
                or os.name!="nt" and stat.S_IMODE(info.st_mode)!=0o600):
            raise control.ControlError("wake_retention_sentinel_recovery_invalid")
        with temporary.open("rb") as handle:
            raw=handle.read(len(control._WAKE_SENTINEL)+1)
        if not control._WAKE_SENTINEL.startswith(raw):
            raise control.ControlError("wake_retention_sentinel_recovery_invalid")
        if info.st_nlink==2:
            peer=path.lstat()
            if ((peer.st_dev,peer.st_ino)!=(info.st_dev,info.st_ino)
                    or raw!=control._WAKE_SENTINEL or path.is_symlink()):
                raise control.ControlError("wake_retention_sentinel_recovery_invalid")
            temporary.unlink()
            control._wake_attempt_sync(root)
        elif raw!=control._WAKE_SENTINEL:
            if path.exists():
                raise control.ControlError("wake_retention_sentinel_recovery_invalid")
            with temporary.open("r+b") as handle:
                handle.seek(0)
                handle.write(control._WAKE_SENTINEL)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
    if path.exists() or path.is_symlink():
        control._wake_private(path)
        with path.open("rb") as handle:
            if handle.read(len(control._WAKE_SENTINEL)+1) != control._WAKE_SENTINEL:
                raise control.ControlError("wake_retention_original_selector_changed")
        if temporary.exists():
            temporary.unlink()
            control._wake_attempt_sync(root)
        return
    if not temporary.exists():
        fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(control._WAKE_SENTINEL)
            handle.flush()
            os.fsync(handle.fileno())
        control._wake_attempt_sync(root)
    os.link(temporary, path)
    control._wake_attempt_sync(path.parent)
    temporary.unlink()
    control._wake_attempt_sync(root)


def prepare_wake_retention(home, sessions, *, all_sender_fence, clock=None, checkpoint=None):
    """Cold batch activation; no native history read or provider effect.

    ``sessions`` maps each exact UUID to ``repo_path`` and ``inventory_reader``.
    ``all_sender_fence()`` holds the caller's existing whole-home sender fence
    and yields a fresh checker satisfying ``_home_fence``. The fence must cover
    every old global consumer, not just these currently selected sessions. An
    unknown foreign/login5 sender makes that positive proof unavailable.

    PREPARED plus unchanged original directory is still legacy mode. Once the
    original directory moves, only forward completion under the same supported
    fence is allowed. No old sender is restarted and no ready DB is rebuilt.
    """
    clock = _clock if clock is None else clock
    checkpoint = (lambda phase: None) if checkpoint is None else checkpoint
    home = Path(home).resolve()
    if not home.is_dir() or not isinstance(sessions, dict) or not sessions or not callable(all_sender_fence):
        raise control.ControlError("wake_retention_all_senders_fence_required")
    scopes = {}
    for thread, value in sessions.items():
        if not isinstance(value, dict):
            raise control.ControlError("wake_retention_scope_invalid")
        _, repo = _scope(home, thread, value.get("repo_path"))
        scopes[thread] = (repo, value.get("inventory_reader"))
    scope_hash = _hash({thread:str(repo) for thread,(repo,_) in scopes.items()})
    root, original = home / "watchdog-wake-retention", home / "watchdog-wake-attempts"
    with all_sender_fence() as check:
        _home_fence(check, home, scopes, clock)  # Before any local migration write.
        inventories = {thread: _inventory(home, thread, repo, reader, clock)
                       for thread, (repo, reader) in scopes.items()}
        if root.exists():
            control._wake_private(root, directory=True)
        policy_path, retired = root / "policy.json", root / "legacy-retired"
        previous = control._wake_json(policy_path) if policy_path.exists() else None
        if previous is not None and previous.get("state") == "ready":
            policy = control.control_wake_retention_policy(home)
            with closing(control._wake_open(home, policy)) as db:
                known = db.execute("SELECT " + control._wake_text("thread_id",36) + "," + control._wake_text("repo_path",4096) + " FROM sessions").fetchall()
                if dict(known) != {thread: str(repo) for thread, (repo, _) in scopes.items()}:
                    raise control.ControlError("wake_retention_explicit_enrollment_required")
            return dict(status="ready", duplicate=True, generation=policy["generation"])
        _private_dir(root)
        if previous is None:
            if retired.exists() or (root / "selector.json").exists():
                raise control.ControlError("wake_retention_recovery_unknown")
            if not original.exists():
                _private_dir(original)
                control._wake_attempt_sync(home)
            old_selector = _selector(original)
            generation = str(uuid.uuid4())
        else:
            if (previous.get("schema_version") != 1 or previous.get("kind") != "finite_wake_attempts"
                    or previous.get("state") != "prepared" or previous.get("codex_home") != str(home)
                    or not control._wake_uuid(previous.get("generation"))
                    or not control._wake_number(previous.get("activated_at"))):
                raise control.ControlError("wake_retention_recovery_unknown")
            old_selector, generation = previous.get("legacy_selector"), previous["generation"]
            if not isinstance(old_selector, dict):
                raise control.ControlError("wake_retention_recovery_unknown")
            if retired.exists():
                if _selector(retired) != old_selector or original.is_dir():
                    raise control.ControlError("wake_retention_original_selector_changed")
            elif _selector(original) != old_selector:
                raise control.ControlError("wake_retention_original_selector_changed")
        # No new-generation effect is eligible before READY. A complete fresh
        # whole-home projection may therefore replan PREPARED current scopes
        # after native work/closures change, retaining the original generation,
        # structural selector and monotone floor. Never reset a READY registry.
        replanned = previous is not None and previous.get("sessions_sha256") != scope_hash
        now = max(_now(clock), previous["activated_at"] if previous is not None else 0)
        policy = dict(schema_version=1, kind="finite_wake_attempts", state="prepared", codex_home=str(home),
                      generation=generation, storage="indexed_recent_wakes_v1", activated_at=now,
                      legacy_selector=old_selector, sessions_sha256=scope_hash, session_count=len(scopes))
        _atomic(policy_path, policy)
        checkpoint("prepared")
        legacy = retired if retired.exists() else original
        fd, name = tempfile.mkstemp(prefix=".wake-current-", dir=root)
        os.close(fd)
        stage = Path(name)
        count = 0
        try:
            with closing(sqlite3.connect(stage)) as db:
                db.execute("PRAGMA synchronous=FULL")
                _schema(db, home, generation, now)
                used = set()
                for thread, (repo, _) in scopes.items():
                    db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (thread, str(repo), generation, now, now, ""))
                    for identifier, digest in _live(inventories[thread]).items():
                        if identifier in used:
                            raise control.ControlError("wake_retention_current_collision")
                        used.add(identifier)
                        value = _legacy_record(legacy, identifier, thread, digest)
                        db.execute("INSERT INTO claims VALUES(?,?,?,NULL,NULL,1,?)",
                                   (identifier, thread, value["prompt_sha256"], _json(value)))
                        count += 1
                db.commit()
                # Probe the exact bounded SQL/schema features before the old
                # root moves. Python3.6 syntax is not SQLite feature evidence.
                control._wake_schema(db)
            with stage.open("r+b") as handle:
                os.fsync(handle.fileno())
            _home_fence(check, home, scopes, clock)
            for thread, (repo, reader) in scopes.items():
                if _live(_inventory(home, thread, repo, reader, clock)) != _live(inventories[thread]):
                    raise control.ControlError("wake_retention_current_changed")
            os.replace(stage, root / "attempts.sqlite3")
            control._wake_attempt_sync(root)
            checkpoint("current_durable")
            if not retired.exists():
                if _selector(original) != old_selector:
                    raise control.ControlError("wake_retention_original_selector_changed")
                _home_fence(check, home, scopes, clock)
                os.rename(original, retired)
                control._wake_attempt_sync(home)
                control._wake_attempt_sync(root)
            checkpoint("old_root_moved")
            _publish_sentinel(original,root)
            checkpoint("old_sender_fenced")
            _atomic(root / "selector.json", dict(schema_version=1, kind="wake_storage_fence",
                                                generation=generation, codex_home=str(home)))
            _home_fence(check, home, scopes, clock)
            _atomic(policy_path, dict(policy, state="ready"))
            checkpoint("ready")
        finally:
            if stage.exists():
                stage.unlink()
        return dict(status="ready", duplicate=False, generation=generation, current_claims=count,
                    legacy_history_imported=0, legacy_cleanup_pending=True, prepared_replanned=replanned)


def enroll_wake_session(home, thread, repo, *, inventory_reader, fence, clock=None):
    """Explicitly enroll a new exact current scope after whole-home activation.

    The original structural old-consumer fence must still be present. Callers
    hold the existing current native/owner/sender fence; a new UUID is never
    implicitly enrolled by an incoming callback. No historical ID scan occurs.
    """
    clock = _clock if clock is None else clock
    home, repo = _scope(home, thread, repo)
    if not callable(fence):
        raise control.ControlError("wake_retention_inventory_required")
    with fence():
        inventory = _inventory(home, thread, repo, inventory_reader, clock)
        policy = control.control_wake_retention_policy(home)
        if policy is None:
            raise control.ControlError("wake_retention_not_activated")
        with closing(control._wake_open(home, policy, write=True)) as db:
            try:
                route = control._wake_route(db,thread)
            except control.ControlError as error:
                # Distinguish an absent route from a corrupt existing row.
                if db.execute("SELECT 1 FROM sessions WHERE thread_id=?",(thread,)).fetchone() is not None:
                    raise error
            else:
                if route["repo_path"] != str(repo):
                    raise control.ControlError("wake_retention_scope_invalid")
                return dict(status="ready",duplicate=True,generation=route["generation"])
            floor = db.execute("SELECT " + control._wake_numeric("floor") + " FROM metadata WHERE slot=1").fetchone()[0]
            now = max(_now(clock),floor,policy["activated_at"])
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",(thread,str(repo),policy["generation"],now,now,""))
            for identifier,digest in _live(inventory).items():
                existing = control._wake_recent_read(db,identifier,thread,digest)
                if existing is not None:
                    db.execute("UPDATE claims SET protected=1 WHERE instruction_id=?",(identifier,))
                    continue
                value = dict(schema_version=1,kind="wake_attempt",state="claimed",instruction_id=identifier,
                    thread_id=thread,prompt_sha256=digest,origin="legacy",evidence=dict(current_native_reservation=True))
                db.execute("INSERT INTO claims VALUES(?,?,?,NULL,NULL,1,?)",(identifier,thread,digest,_json(value)))
            validate_inventory(inventory,home,thread,repo,clock=clock)
            db.commit()
            return dict(status="ready",duplicate=False,generation=policy["generation"])


class WakeAttemptMaintenance:
    """Bounded current-work reconciliation/GC, plus incremental legacy cleanup.

    The inventory callback executes inside caller native/owner/sender fences.
    No current proof -> no pin retirement. A row's historical protected flag is
    not proof; every live decision uses actual current Q/turn identity. Locks
    protect in-progress couriers, and failure affects only the current action.
    """
    def __init__(self, home, *, clock=None):
        self.home = Path(home).resolve()
        self.clock = _clock if clock is None else clock
        self._legacy_iterator = None

    def step(self, thread, repo, *, inventory_reader, fence, budget=BUDGET, close=False):
        if type(budget) is not int or not 1 <= budget <= BUDGET or type(close) is not bool or not callable(fence):
            raise control.ControlError("wake_retention_budget_invalid")
        home, repo = _scope(self.home, thread, repo)
        with fence():
            inventory = _inventory(home, thread, repo, inventory_reader, self.clock)
            live = _live(inventory)
            if close and (inventory["queue"]["items"] or inventory["native"]["status"] != "idle"):
                raise control.ControlError("wake_retention_scope_still_current")
            policy = control.control_wake_retention_policy(home)
            if policy is None:
                return dict(status="legacy", examined=0, removed=0)
            # A bounded keyset pass includes live protected rows, avoiding a
            # single long-lived pin starving later eligible retirements.
            with closing(control._wake_open(home, policy)) as db:
                route = control._wake_route(db, thread)
                if route["repo_path"] != str(repo):
                    raise control.ControlError("wake_retention_scope_invalid")
                pin_budget = max(1, budget // 2)
                pins = db.execute("SELECT " + control._wake_text("instruction_id",512) + " FROM claims INDEXED BY wake_current "
                    "WHERE thread_id=? AND protected=1 AND instruction_id>? ORDER BY instruction_id LIMIT ?",
                    (thread, route["pin_cursor"], pin_budget)).fetchall()
                if not pins and route["pin_cursor"]:
                    pins = db.execute("SELECT " + control._wake_text("instruction_id",512) + " FROM claims INDEXED BY wake_current "
                        "WHERE thread_id=? AND protected=1 ORDER BY instruction_id LIMIT ?", (thread, pin_budget)).fetchall()
                candidates = list(pins)
                left = budget - len(candidates)
                if left:
                    candidates += db.execute("SELECT " + control._wake_text("instruction_id",512) + " FROM claims INDEXED BY wake_expiry WHERE thread_id=? "
                        "AND protected=0 AND expires<=? ORDER BY expires,instruction_id LIMIT ?", (thread, _now(self.clock), max(1,left//2))).fetchall()
                left = budget - len(candidates)
                if left:
                    candidates += db.execute("SELECT " + control._wake_text("instruction_id",512) + " FROM claims INDEXED BY wake_pool WHERE thread_id=? "
                        "AND protected=0 AND born IS NOT NULL ORDER BY born,instruction_id LIMIT ?", (thread, left)).fetchall()
                candidates = list(dict.fromkeys(row[0] for row in candidates))
            examined = removed = busy = faults = 0
            for identifier in candidates:
                try:
                    with control.control_wake_attempt_lock(home, identifier, thread):
                        with closing(control._wake_open(home, policy, write=True)) as db:
                            route = control._wake_route(db, thread)
                            if route["repo_path"] != str(repo):
                                raise control.ControlError("wake_retention_scope_invalid")
                            # Recheck freshness after any lock acquisition.
                            validate_inventory(inventory, home, thread, repo, clock=self.clock)
                            record = control._wake_recent_read(db, identifier, thread)
                            if record is None:
                                continue
                            examined += 1
                            if identifier in live:
                                if record["prompt_sha256"] not in (None, live[identifier]):
                                    raise control.ControlError("wake_retention_current_collision")
                                db.execute("UPDATE claims SET protected=1 WHERE instruction_id=?", (identifier,))
                            else:
                                now = max(_now(self.clock), route["checked_at"])
                                # LIMIT is indexed and constant. This candidate
                                # may still be protected on its first visit.
                                born = record["born_at"]
                                newer = db.execute("SELECT " + control._wake_numeric("born") + "," + control._wake_text("instruction_id",512)
                                    + " FROM claims INDEXED BY wake_pool WHERE thread_id=? "
                                    "AND protected=0 AND born IS NOT NULL ORDER BY born DESC,instruction_id DESC LIMIT ?", (thread, LIMIT)).fetchall()
                                if any(not control._wake_number(row[0]) or not isinstance(row[1],str) for row in newer):
                                    raise control.ControlError("wake_attempt_record_invalid")
                                overflow = born is not None and len(newer) >= LIMIT and (born,identifier) < newer[-1]
                                expired = record["expires_at"] is not None and record["expires_at"] <= now
                                if close or record["born_at"] is None or expired or overflow:
                                    born = record["born_at"]
                                    if born is not None:
                                        db.execute("UPDATE metadata SET floor=max(floor,?) WHERE slot=1", (born,))
                                        db.execute("UPDATE sessions SET floor=max(floor,?) WHERE thread_id=?", (born, thread))
                                    db.execute("DELETE FROM claims WHERE instruction_id=?", (identifier,))
                                    removed += 1
                                else:
                                    db.execute("UPDATE claims SET protected=0 WHERE instruction_id=?", (identifier,))
                                db.execute("UPDATE sessions SET checked_at=max(checked_at,?) WHERE thread_id=?", (now, thread))
                            db.commit()
                except control.ControlBusy:
                    busy += 1  # No waiting while a current courier owns it.
                except control.ControlError:
                    faults += 1  # Preserve a bad exact row; unrelated IDs remain usable.
            with closing(control._wake_open(home, policy, write=True)) as db:
                control._wake_route(db, thread)
                validate_inventory(inventory, home, thread, repo, clock=self.clock)
                db.execute("UPDATE sessions SET pin_cursor=? WHERE thread_id=?",
                           (next((row[0] for row in reversed(pins) if isinstance(row[0],str)), ""), thread))
                if close and db.execute("SELECT 1 FROM claims INDEXED BY wake_current WHERE thread_id=? LIMIT 1", (thread,)).fetchone() is None:
                    db.execute("DELETE FROM sessions WHERE thread_id=?", (thread,))
                db.commit()
            return dict(status="current", examined=examined, removed=removed, busy=busy, faults=faults, budget=budget)

    def cleanup_legacy(self, *, budget=BUDGET):
        """Only WatchDog legacy artifacts; native data/unknown files untouched.

        One process-lifetime iterator gives bounded incremental progress even
        past unknown entries. Restart may revisit them; no eternal file index.
        """
        if type(budget) is not int or not 1 <= budget <= BUDGET:
            raise control.ControlError("wake_retention_budget_invalid")
        policy = control.control_wake_retention_policy(self.home)
        if policy is None:
            return dict(status="legacy", examined=0, removed=0)
        root = self.home / "watchdog-wake-retention" / "legacy-retired"
        if not root.exists():
            return dict(status="complete", examined=0, removed=0)
        control._wake_private(root, directory=True)
        if self._legacy_iterator is None:
            self._legacy_iterator = os.scandir(root)
        examined = removed = 0
        for _ in range(budget):
            try:
                entry = next(self._legacy_iterator)
            except StopIteration:
                self._legacy_iterator.close()
                self._legacy_iterator = None
                return dict(status="pass_complete", examined=examined, removed=removed)
            examined += 1
            path = root / entry.name
            if re.fullmatch(r"[0-9a-f]{64}\.lock", entry.name):
                try:
                    control._wake_private(path)
                    if path.stat().st_size <= 1 and path.read_bytes() in (b"",b"\0"):
                        path.unlink()
                        removed += 1
                except (OSError, control.ControlError):
                    pass
                continue
            if re.fullmatch(r"[0-9a-f]{64}\.json", entry.name) is None:
                continue
            try:
                value = control._wake_json(path)
                identifier = value.get("instruction_id")
                if (not isinstance(identifier, str) or hashlib.sha256(identifier.encode()).hexdigest()+".json" != entry.name
                        or value.get("kind") != "wake_attempt" or value.get("state") != "claimed"
                        or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                        or not control._wake_uuid(value.get("thread_id"))
                        or value.get("origin") not in ("legacy", "dispatch") or "prompt_sha256" not in value
                        or value["prompt_sha256"] is None and value["origin"] != "legacy"
                        or value["prompt_sha256"] is not None and re.fullmatch(r"[0-9a-f]{64}", str(value["prompt_sha256"])) is None):
                    continue
                path.unlink()
                removed += 1
            except (OSError, control.ControlError):
                continue
        control._wake_attempt_sync(root)
        return dict(status="partial", examined=examined, removed=removed)
