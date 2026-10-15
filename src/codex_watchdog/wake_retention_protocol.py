"""Finite wake claims for a positively fenced shared home (Python 3.6 source)."""

WAKE_RETENTION_SOURCE = r'''
import sqlite3 as _wake_sqlite
import datetime as _wake_datetime
import stat as _wake_stat

_WAKE_TTL = 72 * 60 * 60
_WAKE_CAP = 256
_WAKE_SENTINEL = b"Codex WatchDog finite wake registry v1\n"
_wake_v1_path = control_wake_attempt_path
_wake_v1_read = control_wake_attempt_read
_wake_v1_lock = control_wake_attempt_lock
_wake_v1_claim = control_wake_attempt_claim
_wake_v1_adopt = control_adopt_legacy_wake


def _wake_time():
    return _wake_datetime.datetime.now(_wake_datetime.timezone.utc).timestamp()


def _wake_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _wake_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _wake_private(path, directory=False):
    try:
        info = path.lstat()
        if (path.resolve() != path or path.is_symlink()
                or (not _wake_stat.S_ISDIR(info.st_mode) if directory else not _wake_stat.S_ISREG(info.st_mode))
                or not directory and info.st_nlink != 1
                or os.name != "nt" and _wake_stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
                or hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise ValueError()
    except (OSError, ValueError):
        raise ControlError("wake_retention_boundary_invalid")


def _wake_json(path, limit=131072):
    _wake_private(path)
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_git_retirement_object,
                           parse_constant=_git_retirement_constant) if len(raw) <= limit else None
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        raise ControlError("wake_retention_metadata_invalid")


def control_wake_retention_policy(home):
    home = Path(home).resolve()
    root = home / "watchdog-wake-retention"
    policy = root / "policy.json"
    if not root.exists() and not root.is_symlink():
        return None
    _wake_private(root, directory=True)
    original = home / "watchdog-wake-attempts"
    selector = root / "selector.json"
    if not selector.exists() and not (root / "legacy-retired").exists():
        # Preparation is not activation. A positively usable original directory
        # remains the selector until the structural fence has actually crossed.
        if original.is_dir() and not original.is_symlink():
            _wake_private(original, directory=True)
            if not policy.exists():
                return None
            prepared = _wake_json(policy)
            old = prepared.get("legacy_selector")
            info = original.stat()
            if (prepared.get("state") == "prepared" and prepared.get("codex_home") == str(home)
                    and isinstance(old, dict) and old.get("device") == info.st_dev and old.get("inode") == info.st_ino):
                return None
        elif not original.exists() and not original.is_symlink() and not policy.exists():
            return None
    value = _wake_json(policy)
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("kind") != "finite_wake_attempts" or value.get("state") != "ready"
            or value.get("codex_home") != str(home) or not _wake_uuid(value.get("generation"))
            or value.get("storage") != "indexed_recent_wakes_v1"
            or not _wake_number(value.get("activated_at"))):
        raise ControlError("wake_retention_not_activated")
    fence = _wake_json(selector)
    if fence != dict(schema_version=1, kind="wake_storage_fence", generation=value["generation"], codex_home=str(home)):
        raise ControlError("wake_retention_old_consumer_fence_missing")
    _wake_private(original)
    with original.open("rb") as handle:
        if handle.read(len(_WAKE_SENTINEL) + 1) != _WAKE_SENTINEL:
            raise ControlError("wake_retention_old_consumer_fence_missing")
    return value


def _wake_schema(db):
    expected = {
        "metadata": [("slot", 1), ("generation", 0), ("codex_home", 0), ("floor", 0)],
        "sessions": [("thread_id", 1), ("repo_path", 0), ("generation", 0), ("floor", 0), ("checked_at", 0), ("pin_cursor", 0)],
        "claims": [("instruction_id", 1), ("thread_id", 0), ("prompt_sha256", 0), ("born", 0), ("expires", 0), ("protected", 0), ("value", 0)],
    }
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise ControlError("wake_retention_schema_invalid")
    for table, shape in expected.items():
        # Basic PRAGMA works on the existing Python3.6 native SQLite. A bounded
        # SQL-text projection first excludes oversized malformed identifiers.
        definition = db.execute("SELECT CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN sql END "
                                "FROM sqlite_master WHERE type='table' AND name=? LIMIT 2", (table,)).fetchall()
        if len(definition) != 1 or not isinstance(definition[0][0],str):
            raise ControlError("wake_retention_schema_invalid")
        rows = db.execute("PRAGMA table_info(" + table + ")").fetchmany(32)
        if [(row[1],row[5]) for row in rows] != shape:
            raise ControlError("wake_retention_schema_invalid")
    for index, names in (("wake_current", ["thread_id", "protected", "instruction_id"]),
                         ("wake_expiry", ["thread_id", "protected", "expires", "instruction_id"]),
                         ("wake_pool", ["thread_id", "protected", "born", "instruction_id"])):
        definition = db.execute("SELECT CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN sql END "
                                "FROM sqlite_master WHERE type='index' AND name=? LIMIT 2", (index,)).fetchall()
        if len(definition) != 1 or not isinstance(definition[0][0],str):
            raise ControlError("wake_retention_schema_invalid")
        rows = db.execute("PRAGMA index_info(" + index + ")").fetchmany(8)
        if [row[2] for row in rows] != names:
            raise ControlError("wake_retention_schema_invalid")


def _wake_text(column, limit):
    return "CASE WHEN typeof(" + column + ")='text' AND length(CAST(" + column + " AS BLOB))<=" + str(limit) + " THEN " + column + " END"


def _wake_numeric(column):
    return "CASE WHEN typeof(" + column + ") IN ('integer','real','null') THEN " + column + " ELSE 'invalid' END"


def _wake_open(home, policy, write=False):
    path = Path(home).resolve() / "watchdog-wake-retention" / "attempts.sqlite3"
    _wake_private(path)
    if any(Path(str(path) + suffix).is_symlink() for suffix in ("-journal", "-wal", "-shm")):
        raise ControlError("wake_retention_boundary_invalid")
    db = _wake_sqlite.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=0.25)
    try:
        steps = [0]
        def bounded():
            steps[0] += 100
            return steps[0] > 100000
        db.set_progress_handler(bounded, 100)
        if write:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
        else:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
        _wake_schema(db)
        identity = db.execute("SELECT " + _wake_text("generation",36) + "," + _wake_text("codex_home",4096)
                              + "," + _wake_numeric("floor") + " FROM metadata WHERE slot=1").fetchone()
        if (identity is None or identity[:2] != (policy["generation"], policy["codex_home"])
                or not _wake_number(identity[2])):
            raise ControlError("wake_retention_registry_mismatch")
        return db
    except BaseException:
        db.close()
        raise


def _wake_route(db, thread):
    if not _wake_uuid(thread):
        raise ControlError("wake_attempt_exact_context_required")
    row = db.execute("SELECT " + ",".join((_wake_text("repo_path",4096),_wake_text("generation",36),
                      _wake_numeric("floor"),_wake_numeric("checked_at"),_wake_text("pin_cursor",512)))
                      + " FROM sessions WHERE thread_id=?", (thread,)).fetchone()
    if (row is None or not isinstance(row[0], str) or not Path(row[0]).is_absolute()
            or not _wake_uuid(row[1]) or not _wake_number(row[2]) or not _wake_number(row[3])
            or not isinstance(row[4], str) or len(row[4]) > 512):
        raise ControlError("wake_retention_session_not_activated")
    return dict(repo_path=row[0], generation=row[1], floor=row[2], checked_at=row[3], pin_cursor=row[4])


def _wake_recent_read(db, instruction, thread=None, prompt=None):
    row = db.execute("SELECT " + _wake_text("thread_id",36) + "," + _wake_text("prompt_sha256",64)
                     + "," + _wake_numeric("born") + "," + _wake_numeric("expires")
                     + ",CASE WHEN typeof(protected)='integer' THEN protected END," + _wake_text("value",131072) + ","
                     + "(prompt_sha256 IS NULL OR typeof(prompt_sha256)='text' AND length(CAST(prompt_sha256 AS BLOB))=64) "
                     "FROM claims WHERE instruction_id=?", (instruction,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[5], object_pairs_hook=_git_retirement_object, parse_constant=_git_retirement_constant)
        if (row[6] != 1 or not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] not in (1, 2) or value.get("kind") != "wake_attempt"
                or value.get("state") != "claimed" or value.get("instruction_id") != instruction
                or not _wake_uuid(row[0]) or value.get("thread_id") != row[0]
                or "prompt_sha256" not in value or value["prompt_sha256"] != row[1]
                or row[1] is not None and re.fullmatch(r"[0-9a-f]{64}", str(row[1])) is None
                or type(row[4]) is not int or row[4] not in (0, 1)
                or row[2] is None and (row[3] is not None or row[4] != 1)
                or row[2] is not None and (not _wake_number(row[2]) or row[3] != row[2] + _WAKE_TTL)
                or value.get("origin") not in ("dispatch", "legacy")):
            raise ValueError()
        if value["schema_version"] == 2 and (value.get("born_at") != row[2] or value.get("expires_at") != row[3]):
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise ControlError("wake_attempt_record_invalid")
    if (thread is not None and thread != row[0]
            or prompt is not None and row[1] is not None and prompt != row[1]):
        raise ControlError("wake_attempt_id_collision")
    return dict(value, retention_schema=1, born_at=row[2], expires_at=row[3], current_pin=bool(row[4]))


def _wake_context(context, thread, route, now, floor):
    # This is a trusted caller adapter under its existing exact sender/native
    # and active-parent/source fences, not an authorization supplied by a user.
    if (not isinstance(context, dict) or type(context.get("schema_version")) is not int
            or context["schema_version"] != 1 or context.get("thread_id") != thread
            or context.get("generation") != route["generation"] or context.get("repo_path") != route["repo_path"]
            or not _wake_number(context.get("born_at")) or not max(route["floor"], floor) < context["born_at"] <= now
            or now - context["born_at"] >= _WAKE_TTL
            or not _wake_number(context.get("validated_at")) or not 0 <= now - context["validated_at"] <= 5
            or context.get("source_kind") not in ("provider_parent", "current_git", "native_turn", "resume_prompt")
            or not isinstance(context.get("source_key"), str) or not 1 <= len(context["source_key"]) <= 512
            or any(c in context["source_key"] for c in ("\0", "\n"))):
        raise ControlError("wake_attempt_current_eligibility_required")


def _wake_stripe(home, instruction):
    if (not isinstance(instruction, str) or not 1 <= len(instruction) <= 512
            or re.fullmatch(r"[A-Za-z0-9._:/+-]+", instruction) is None):
        raise ControlError("wake_attempt_instruction_invalid")
    return Path(home).resolve() / "watchdog-wake-retention" / "locks" / (hashlib.sha256(instruction.encode()).hexdigest()[:2] + ".lock")


def control_wake_attempt_path(home, instruction_id):
    if control_wake_retention_policy(home) is not None:
        raise ControlError("wake_attempt_exact_context_required")
    return _wake_v1_path(home, instruction_id)


def control_wake_attempt_read(home, instruction_id, thread_id=None, prompt_sha256=None, context=None):
    policy = control_wake_retention_policy(home)
    if policy is None:
        return _wake_v1_read(home, instruction_id, thread_id, prompt_sha256)
    _wake_stripe(home, instruction_id)  # Validate ID before any database work.
    with contextlib.closing(_wake_open(home, policy)) as db:
        _wake_route(db, thread_id)
        return _wake_recent_read(db, instruction_id, thread_id, prompt_sha256)


@contextlib.contextmanager
def control_wake_attempt_lock(home, instruction_id, thread_id=None, context=None):
    policy = control_wake_retention_policy(home)
    if policy is None:
        with _wake_v1_lock(home, instruction_id):
            yield
        return
    with contextlib.closing(_wake_open(home, policy)) as db:
        _wake_route(db, thread_id)
    path = _wake_stripe(home, instruction_id)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    _wake_private(path.parent, directory=True)
    key = str(path)
    mutexes, registry_lock = _wake_attempt_registry()
    with registry_lock:
        mutex = mutexes.get(key)
        if mutex is None:
            mutex = threading.Lock()
            mutexes[key] = mutex
    if not mutex.acquire(False):
        raise ControlBusy("wake_attempt_busy")
    held = vars(threading).setdefault("_watchdog_wake_attempt_held", threading.local())
    keys = getattr(held, "keys", set())
    try:
        if path.is_symlink() or path.resolve() != path:
            raise ControlError("wake_attempt_lock_invalid")
        if os.name == "nt":
            with control_file_lock(path):
                held.keys = keys | {key}
                try:
                    yield
                finally:
                    held.keys = keys
            return
        # POSIX record locks preserve the original cross-node shared-file
        # courier fence. flock alone is host-local on supported GPFS hosts.
        import fcntl
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b") as handle:
            info = os.fstat(handle.fileno())
            if (not _wake_stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or _wake_stat.S_IMODE(info.st_mode) != 0o600):
                raise ControlError("wake_attempt_lock_invalid")
            try:
                fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ControlBusy("wake_attempt_busy")
                raise
            held.keys = keys | {key}
            try:
                yield
            finally:
                held.keys = keys
                fcntl.lockf(handle, fcntl.LOCK_UN)
    finally:
        mutex.release()


def control_wake_attempt_claim(home, instruction_id, thread_id, prompt_sha256,
                               origin="dispatch", evidence=None, context=None):
    policy = control_wake_retention_policy(home)
    if policy is None:
        return _wake_v1_claim(home, instruction_id, thread_id, prompt_sha256, origin, evidence)
    path = _wake_stripe(home, instruction_id)
    held = vars(threading).get("_watchdog_wake_attempt_held")
    if held is None or str(path) not in getattr(held, "keys", ()):
        raise ControlError("wake_attempt_lock_required")
    if (not _wake_uuid(thread_id) or origin not in ("dispatch", "legacy")
            or re.fullmatch(r"[0-9a-f]{64}", str(prompt_sha256)) is None):
        raise ControlError("wake_attempt_identity_invalid")
    with contextlib.closing(_wake_open(home, policy, write=True)) as db:
        route = _wake_route(db, thread_id)
        previous = _wake_recent_read(db, instruction_id, thread_id, prompt_sha256)
        if previous is not None:
            return dict(previous, duplicate=True)
        now = max(_wake_time(), route["checked_at"])
        if origin != "dispatch":
            raise ControlError("wake_retention_legacy_import_required")
        floor = db.execute("SELECT " + _wake_numeric("floor") + " FROM metadata WHERE slot=1").fetchone()[0]
        _wake_context(context, thread_id, route, now, floor)
        value = dict(schema_version=2, kind="wake_attempt", state="claimed", instruction_id=instruction_id,
            thread_id=thread_id, prompt_sha256=prompt_sha256, origin="dispatch", born_at=context["born_at"],
            expires_at=context["born_at"] + _WAKE_TTL, generation=route["generation"],
            source_kind=context["source_kind"], source_key=context["source_key"])
        if evidence is not None:
            value["evidence"] = evidence
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > 131072:
            raise ControlError("wake_attempt_evidence_invalid")
        db.execute("INSERT INTO claims VALUES(?,?,?,?,?,1,?)", (instruction_id, thread_id, prompt_sha256,
                   value["born_at"], value["expires_at"], raw))
        db.execute("UPDATE sessions SET checked_at=? WHERE thread_id=?", (now, thread_id))
        db.commit()  # Durable reservation precedes the one external attempt.
        return dict(value, duplicate=False)


def control_adopt_legacy_wake(store, observer):
    if control_wake_retention_policy(store.codex_home) is not None:
        return []  # Cold import pins current Q/turn; historic pending is not live.
    return _wake_v1_adopt(store, observer)
'''
