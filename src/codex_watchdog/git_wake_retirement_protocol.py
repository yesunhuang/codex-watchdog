"""Shared no-replay reservation used by native and dependency-free SSH couriers.

The embedded source retains Python 3.6 compatibility, like CONTROL_SOURCE.
It only reads retirements; creating one is a separate explicit native action.
"""

GIT_WAKE_RETIREMENT_SOURCE = r'''
import stat
import threading
import weakref

_GIT_RETIREMENT_ID = re.compile(r"git:[0-9a-f]{16}:(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
def _git_retirement_registry():
    # CONTROL_SOURCE also executes in each HostRemoteAdapter namespace. POSIX
    # locks are process-owned, so every namespace must share the thread mutex.
    return vars(threading).setdefault("_watchdog_git_retirement_registry",
                                     (weakref.WeakValueDictionary(), threading.Lock()))


def _git_retirement_after_fork():
    vars(threading).pop("_watchdog_git_retirement_registry", None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_git_retirement_after_fork)


def control_git_retirement_path(codex_home, instruction_id):
    if not isinstance(instruction_id, str) or _GIT_RETIREMENT_ID.fullmatch(instruction_id) is None:
        raise ControlError("git_retirement_instruction_invalid")
    home = Path(codex_home).resolve()
    root = home / "watchdog-git-retirements"
    if root.is_symlink() or root.resolve() != root:
        raise ControlError("git_retirement_path_invalid")
    if root.exists():
        info = root.stat()
        if (not stat.S_ISDIR(info.st_mode) or os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o700
                or hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise ControlError("git_retirement_boundary_invalid")
    return root / (hashlib.sha256(instruction_id.encode("utf-8")).hexdigest() + ".json")


def _git_retirement_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate retirement field")
        value[key] = item
    return value


def _git_retirement_constant(value):
    raise ValueError("invalid retirement constant")


def control_git_retirement_read(codex_home, instruction_id, thread_id=None,
                                repo_path=None, prompt_sha256=None):
    if not isinstance(instruction_id, str) or _GIT_RETIREMENT_ID.fullmatch(instruction_id) is None:
        return None
    path = control_git_retirement_path(codex_home, instruction_id)
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
                or value["schema_version"] != 1 or value.get("kind") != "git_wake_retirement"
                or value.get("state") != "retired" or value.get("instruction_id") != instruction_id
                or str(uuid.UUID(value.get("thread_id"))) != value.get("thread_id")
                or not isinstance(value.get("repo_path"), str)
                or not Path(value["repo_path"]).is_absolute()
                or str(Path(value["repo_path"]).resolve()) != value["repo_path"]
                or not isinstance(value.get("original_node"), str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value["original_node"]) is None
                or type(value.get("original_owner_epoch")) is not int or value["original_owner_epoch"] < 0):
            raise ValueError()
        for key in ("prompt_sha256", "original_owner_sha256", "original_wake_sha256",
                    "original_observer_sha256", "authorization_sha256"):
            if not isinstance(value.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
                raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        raise ControlError("git_retirement_record_invalid")
    for key, expected in (("thread_id", thread_id), ("repo_path", repo_path), ("prompt_sha256", prompt_sha256)):
        if expected is not None and value[key] != expected:
            raise ControlError("git_retirement_id_collision")
    return value


@contextlib.contextmanager
def control_git_retirement_lock(codex_home, instruction_id):
    # Other instruction kinds retain their existing locks and behavior.
    if not isinstance(instruction_id, str) or _GIT_RETIREMENT_ID.fullmatch(instruction_id) is None:
        yield
        return
    path = control_git_retirement_path(codex_home, instruction_id).with_suffix(".lock")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    control_git_retirement_path(codex_home, instruction_id)
    key = str(path)
    mutexes, registry_lock = _git_retirement_registry()
    with registry_lock:
        mutex = mutexes.get(key)
        if mutex is None:
            mutex = threading.Lock()
            mutexes[key] = mutex
    if not mutex.acquire(False):
        raise ControlBusy("git_retirement_busy")
    try:
        if path.is_symlink() or path.resolve() != path:
            raise ControlError("git_retirement_lock_invalid")
        if os.name == "nt":
            with control_file_lock(path):
                yield
            return
        import fcntl
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ControlError("git_retirement_lock_invalid")
            try:
                fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ControlBusy("git_retirement_busy")
                raise
            try:
                yield
            finally:
                fcntl.lockf(handle, fcntl.LOCK_UN)
    finally:
        mutex.release()
'''
