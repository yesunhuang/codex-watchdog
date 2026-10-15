"""Indexed one-attempt claims shared by native and dependency-free SSH couriers.

Claims survive node/epoch changes. They reserve automatic eligibility, not
delivery or completion. The embedded source remains Python 3.6 compatible.
"""

WAKE_ATTEMPT_SOURCE = r'''
def _wake_attempt_registry():
    return vars(threading).setdefault("_watchdog_wake_attempt_registry",
                                     (weakref.WeakValueDictionary(), threading.Lock()))


def _wake_attempt_after_fork():
    vars(threading).pop("_watchdog_wake_attempt_registry", None)
    vars(threading).pop("_watchdog_wake_attempt_held", None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_wake_attempt_after_fork)


def control_wake_attempt_path(codex_home, instruction_id):
    if (not isinstance(instruction_id, str) or not 1 <= len(instruction_id) <= 512
            or re.fullmatch(r"[A-Za-z0-9._:/+-]+", instruction_id) is None):
        raise ControlError("wake_attempt_instruction_invalid")
    root = Path(codex_home).resolve() / "watchdog-wake-attempts"
    if root.is_symlink() or root.resolve() != root:
        raise ControlError("wake_attempt_path_invalid")
    if root.exists():
        info = root.stat()
        if (not stat.S_ISDIR(info.st_mode) or os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o700
                or hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise ControlError("wake_attempt_boundary_invalid")
    return root / (hashlib.sha256(instruction_id.encode()).hexdigest() + ".json")


def control_wake_attempt_read(codex_home, instruction_id, thread_id=None, prompt_sha256=None):
    path = control_wake_attempt_path(codex_home, instruction_id)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600
                or hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise ValueError()
        with path.open("rb") as handle:
            raw = handle.read(131073)
        if len(raw) > 131072:
            raise ValueError()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_git_retirement_object,
                           parse_constant=_git_retirement_constant)
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1 or value.get("kind") != "wake_attempt"
                or value.get("state") != "claimed" or value.get("instruction_id") != instruction_id
                or str(uuid.UUID(value.get("thread_id"))) != value.get("thread_id")
                or "prompt_sha256" not in value
                or value.get("prompt_sha256") is not None
                and re.fullmatch(r"[0-9a-f]{64}", str(value["prompt_sha256"])) is None
                or value.get("origin") not in ("dispatch", "legacy")):
            raise ValueError()
        if value["prompt_sha256"] is None and value["origin"] != "legacy":
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        raise ControlError("wake_attempt_record_invalid")
    if (thread_id is not None and value["thread_id"] != thread_id
            or prompt_sha256 is not None and value["prompt_sha256"] is not None
            and value["prompt_sha256"] != prompt_sha256):
        raise ControlError("wake_attempt_id_collision")
    return value


@contextlib.contextmanager
def control_wake_attempt_lock(codex_home, instruction_id):
    path = control_wake_attempt_path(codex_home, instruction_id).with_suffix(".lock")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    control_wake_attempt_path(codex_home, instruction_id)
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
        import fcntl
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600):
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


def _wake_attempt_sync(path):
    if os.name != "nt":
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def control_wake_attempt_claim(codex_home, instruction_id, thread_id, prompt_sha256,
                               origin="dispatch", evidence=None):
    path = control_wake_attempt_path(codex_home, instruction_id)
    held = vars(threading).get("_watchdog_wake_attempt_held")
    if held is None or str(path.with_suffix(".lock")) not in getattr(held, "keys", ()):
        raise ControlError("wake_attempt_lock_required")
    if (str(uuid.UUID(thread_id)) != thread_id or origin not in ("dispatch", "legacy")
            or prompt_sha256 is None and origin != "legacy"
            or prompt_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", str(prompt_sha256)) is None):
        raise ControlError("wake_attempt_identity_invalid")
    previous = control_wake_attempt_read(codex_home, instruction_id, thread_id, prompt_sha256)
    if previous is not None:
        return dict(previous, duplicate=True)
    value = dict(schema_version=1, kind="wake_attempt", state="claimed", instruction_id=instruction_id,
                 thread_id=thread_id, prompt_sha256=prompt_sha256, origin=origin)
    if evidence is not None:
        value["evidence"] = evidence
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(raw) > 131072:
        raise ControlError("wake_attempt_evidence_invalid")
    _wake_attempt_sync(path.parent.parent)
    fd, temporary = tempfile.mkstemp(prefix=".claim-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # Exclusive publication. A crash never restores eligibility.
        _wake_attempt_sync(path.parent)
    finally:
        os.unlink(temporary)
        _wake_attempt_sync(path.parent)
    return dict(value, duplicate=False)


def control_adopt_legacy_wake(store, observer):
    """Reserve only the selected old and current pending IDs, never scan history.

    A missing/oversized historic proof is not handoff authority. The known ID is
    still reserved conservatively without inventing delivery/completion truth.
    """
    owners = [(store.path, store.read())]
    node = observer.get("node") if isinstance(observer, dict) else None
    if isinstance(node, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", node):
        path = store.codex_home / "watchdog-nodes" / node / "watchdog-control" / store.thread_id / "owner.json"
        if path != store.path and not path.is_symlink() and path.resolve() == path and path.is_file():
            try:
                with path.open("rb") as handle:
                    raw = handle.read(131073)
                previous = json.loads(raw) if len(raw) <= 131072 else None
            except (OSError, ValueError, RecursionError):
                previous = None
            if (isinstance(previous, dict) and previous.get("thread_id") == store.thread_id
                    and previous.get("repo_path") == store.repo_path):
                owners.append((path, previous))
    adopted = []
    for owner_path, owner in owners:
        state = owner.get("remote_state")
        instruction_id = state.get("pending_instruction_id") if isinstance(state, dict) else None
        if not isinstance(instruction_id, str) or not instruction_id:
            continue
        wake = owner_path.parent.parent.parent / "remote-wake" / (hashlib.sha256(instruction_id.encode()).hexdigest() + ".json")
        digest = None
        evidence = dict(owner_sha256=hashlib.sha256(json.dumps(owner, sort_keys=True).encode()).hexdigest())
        if not wake.is_symlink() and wake.resolve() == wake and wake.is_file():
            try:
                with wake.open("rb") as handle:
                    raw = handle.read(131073)
                record = json.loads(raw) if len(raw) <= 131072 else None
                if (isinstance(record, dict) and record.get("instruction_id") == instruction_id
                        and record.get("thread_id") == store.thread_id):
                    candidate = record.get("prompt_sha256")
                    if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
                        digest = candidate
                    evidence["wake_sha256"] = hashlib.sha256(raw).hexdigest()
            except (OSError, ValueError, RecursionError):
                pass
        try:
            with control_wake_attempt_lock(store.codex_home, instruction_id):
                adopted.append(control_wake_attempt_claim(store.codex_home, instruction_id,
                               store.thread_id, digest, origin="legacy", evidence=evidence))
        except ControlBusy:
            raise  # A currently held courier lock remains a live handoff fence.
        except ControlError as exc:
            # The affected ID already fails closed at dispatch. Its malformed
            # historic proof must not turn into a thread-wide activation gate.
            adopted.append(dict(state="ineligible", instruction_id=instruction_id, reason=str(exc)))
    return adopted
'''

# Keep unactivated homes compatible; activated homes use a finite, fenced lane.
from .wake_retention_protocol import WAKE_RETENTION_SOURCE

WAKE_ATTEMPT_SOURCE += WAKE_RETENTION_SOURCE
