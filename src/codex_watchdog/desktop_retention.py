"""Optional schema3 GC bound to one lexical desktop session.

The desktop workspace cycle already holds its real observer cycle and exact
``effect_guard``. Inside that scope, on the same thread, a provider journal may
borrow current observed-writer proof for exactly this session. Elsewhere it
keeps ``unsupported_retention`` and continues without GC. This adapter never
migrates, selects a runtime, creates control authority or writes native state.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import os
from pathlib import Path
import threading
import time
import uuid

from .control_context import current_control, effect_guard
from .control_state import ControlError
from .current_work_retention import CurrentWorkRetention, _runtime_identity
from .native_inflight import NativeInflightError, PROOF_SECONDS, _number, collect_native_work
from .native_observed_work import ObservedNativeStreamWork
from .observed_writer import capture_writer
from .native_stream_work import stream_exact_thread

# Innermost live activation lease in this exact context.
_active = ContextVar("watchdog_desktop_retention", default=None)
# (lease, pids or None, taken_at) for the innermost provider transaction fence.
# Process-local and transaction-scoped; never a durable saved-PID authority.
_route = ContextVar("watchdog_desktop_retention_route", default=None)


def _executor():
    return os.getpid(), threading.get_ident()


def _session(value):
    try:
        canonical = str(uuid.UUID(value))
        if canonical != value.lower():
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise NativeInflightError("desktop_retention_session_invalid") from None
    return canonical


def _root(value, reason):
    path = Path(value)
    # A relative or redirected root would bind proof to a different runtime.
    if not path.is_absolute() or path.resolve() != path:
        raise NativeInflightError(reason)
    return path


def _pids(value):
    if (not isinstance(value, (list, tuple)) or not value
            or any(type(pid) is not int or not 0 < pid < 2**31 for pid in value)
            or len(set(value)) != len(value)):
        raise NativeInflightError("desktop_retention_route_invalid")
    return tuple(value)


class _Lease:
    """One activation's borrowable proof; a closed lease never regains it."""

    def __init__(self, binding):
        self.binding = binding
        self.executor = _executor()
        self.closed = False

    def live(self):
        return (not self.closed and _active.get() is self
                and self.executor == _executor() and current_control() is None)

    @contextmanager
    def fence(self):
        """Entered before the provider lock/BEGIN and held through commit."""
        if not self.live():
            # Stale, escaped or borrowed: the independent provider body still
            # runs; the reader refuses inside before_prune's SAVEPOINT.
            token = _route.set((self, None, None))
            try:
                yield
            finally:
                _route.reset(token)
            return
        binding = self.binding
        with effect_guard(binding.home, binding.session):
            pids = taken = None
            if self.live():
                try:
                    taken = binding.clock()
                    binding.check_runtime()
                    # Refuse the optional route read before spawning an OS
                    # inspection when this exact native scope is unavailable.
                    if stream_exact_thread(binding.home, binding.session, binding.repo):
                        pids = _pids(binding.route_reader())
                except ControlError:
                    if self.live():
                        binding.producer.forget(binding.session)
                    raise
                except Exception:
                    pids = None  # A missing route means unsupported GC only.
                except BaseException:
                    if self.live():
                        binding.producer.forget(binding.session)
                    raise
            token = _route.set((self, pids, taken))
            try:
                if pids is None and self.live():
                    binding.producer.forget(binding.session)
                yield
            finally:
                _route.reset(token)

    def before_prune(self, journal, db):
        if not self.live():
            raise NativeInflightError("desktop_retention_scope_inactive")
        return self.binding.retention.before_prune(journal, db)


class _Binding:
    """Reusable exact (session, repo) proof consumer for one shared runtime."""

    def __init__(self, owner, session, repo):
        self.runtime, self.home, self.clock = owner.runtime, owner.home, owner.clock
        self.route_reader, self.producer = owner.route_reader, owner.producer
        self.session, self.repo = session, repo
        self.runtime_identity = owner.runtime_identity
        # before_prune sets journal._retention_session to this exact UUID.
        self.retention = CurrentWorkRetention(self.runtime, self.home, session, repo,
            reader=self.read, fence=self._fence, clock=self.clock)

    def _fence(self):
        lease = _active.get()
        return (lease if lease is not None and lease.binding is self else _Lease(self)).fence()

    def check_runtime(self):
        if _runtime_identity(self.runtime) != self.runtime_identity:
            raise NativeInflightError("desktop_retention_runtime_changed")

    def _current(self):
        lease = _active.get()
        if lease is None or lease.binding is not self or not lease.live():
            raise NativeInflightError("desktop_retention_scope_inactive")
        self.check_runtime()
        route = _route.get()
        if route is None or route[0] is not lease or route[1] is None:
            raise NativeInflightError("desktop_retention_route_unavailable")
        return route

    def _writer(self):
        _, pids, taken = self._current()
        current = self.clock()
        if not _number(taken) or not _number(current) or not 0 <= current - taken <= PROOF_SECONDS:
            raise NativeInflightError("desktop_retention_route_stale")
        found = []
        for pid in pids:
            try:
                found.append(capture_writer(self.home, self.session, self.repo, pid, clock=self.clock))
            except NativeInflightError:
                continue  # This PID is not the session's current writer.
        self._current()
        ended = self.clock()
        if not _number(ended) or not 0 <= ended - taken <= PROOF_SECONDS:
            raise NativeInflightError("desktop_retention_route_stale")
        if len(found) != 1:
            raise NativeInflightError("desktop_retention_writer_ambiguous" if found
                                      else "desktop_retention_writer_unavailable")
        return found[0]

    def read(self):
        try:
            self._current()
        except NativeInflightError:
            lease = _active.get()
            route = _route.get()
            if (lease is not None and lease.binding is self and lease.live()
                    and route is not None and route[0] is lease):
                self.producer.forget(self.session)
            raise
        return collect_native_work(self.home, self.session, self.repo,
            current_native=lambda: self.producer.observe(self.session, self.repo,
                current_writer=self._writer, fence=nullcontext),
            fence=nullcontext, clock=self.clock)


class DesktopRetention:
    """Per-current-session bindings over one shared stream producer.

    Bindings are O(active sessions); retiring one drops only memory.
    """

    def __init__(self, runtime, home, *, route_reader, clock=None):
        if not callable(route_reader):
            raise NativeInflightError("desktop_retention_route_reader_required")
        self.runtime = _root(runtime, "desktop_retention_runtime_redirected")
        self.runtime_identity = _runtime_identity(self.runtime)
        self.home = Path(home)
        if not self.home.is_absolute():
            raise NativeInflightError("desktop_retention_home_invalid")
        self.route_reader = route_reader
        self.clock = time.time if clock is None else clock
        self.producer = ObservedNativeStreamWork(self.home, clock=self.clock)
        self._bindings = {}

    def _binding(self, workspace):
        session = _session(workspace.session_id)
        repo = Path(workspace.repo_root)
        if not repo.is_absolute():
            raise NativeInflightError("desktop_retention_repo_invalid")
        key = (session, str(repo.resolve()))
        binding = self._bindings.get(key)
        if binding is None:
            binding = self._bindings[key] = _Binding(self, session, Path(key[1]))
        return binding

    @contextmanager
    def activate(self, workspace):
        """Caller already holds its observer cycle and exact effect_guard."""
        if current_control() is not None:
            yield  # Coordinated sessions use their own truthful actor path.
            return
        binding = self._binding(workspace)
        lease = _Lease(binding)
        token = _active.set(lease)
        try:
            try:
                with lease.fence():
                    binding.read()  # Warm the stream baseline for quiet cycles.
            except ControlError:
                raise
            except Exception:
                pass  # Optional; the caller's cycle continues without it.
            yield
        finally:
            lease.closed = True
            _active.reset(token)

    def retain_sessions(self, ids):
        keep = set()
        for value in ids:
            try:
                keep.add(_session(value))
            except NativeInflightError:
                continue
        retired = [key for key in self._bindings if key[0] not in keep]
        for key in retired:
            del self._bindings[key]
        for session in sorted({key[0] for key in retired}):
            self.producer.forget(session)


def current_retention(runtime):
    """The live lexical binding for exactly ``runtime``, else None. No native reads."""
    lease = _active.get()
    if lease is None or not lease.live():
        return None
    try:
        path = Path(runtime)
        if not path.is_absolute() or path != lease.binding.runtime or path.resolve() != path:
            return None
        if _runtime_identity(path) != lease.binding.runtime_identity:
            return None
        lease.binding.check_runtime()
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
    return lease
