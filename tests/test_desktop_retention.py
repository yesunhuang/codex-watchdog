"""Lexical exact-session desktop retention over real journals and native/Q fixtures.

OS evidence is simulated at observed_writer._sample; no native session, provider
call, client, grant or history walk is involved.
"""
from contextlib import closing, contextmanager
import contextvars
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from codex_watchdog import desktop_retention, observed_writer
from codex_watchdog.control_context import effect_guard
from codex_watchdog.desktop_retention import DesktopRetention, current_retention
from codex_watchdog.native_inflight import NativeInflightError, PROOF_SECONDS
from codex_watchdog.reply_tickets import ReplyTickets
from codex_watchdog.legacy_reply_tickets import select_reply_journal
from test_native_inflight import THREAD, OTHER, NS, NOW, rows_for, remove_queue, sha
from test_observed_native_work import rig, native_rig, start, terminal
from test_current_work_retention import prepare

BIRTHS = {"linux": lambda pid: "linux:11111111-2222-4333-8444-000000000008:%d" % (12000 + pid),
          "darwin": lambda pid: "darwin:1790000000:%d" % pid,
          "win32": lambda pid: "win32:%d" % (133000000000000000 + pid)}
COLUMNS = ("namespace", "kind", "key", "value", "fingerprint", "thread_id", "active", "created_at")


@pytest.fixture
def desk(rig, monkeypatch):
    lock = rig.home / "thread-writer-locks" / (THREAD + ".lock")
    lock.parent.mkdir(); lock.write_bytes(b"fixture native resource")
    rig.route, rig.alive, rig.route_calls, rig.route_hook = [123], {123}, 0, None
    def sample(path, identity, pid, platform):
        route = desktop_retention._route.get()
        assert route is not None and route[1] is not None and path == lock
        if pid not in rig.alive:
            raise NativeInflightError("observed_writer_lock_unavailable")
        return pid, BIRTHS[platform](pid)
    monkeypatch.setattr(observed_writer, "_sample", sample)
    monkeypatch.setattr(observed_writer.socket, "gethostname", lambda: "fixture-node")
    def route_reader():
        rig.route_calls += 1
        return rig.route_hook() if rig.route_hook is not None else list(rig.route)
    return DesktopRetention(rig.runtime, rig.home, route_reader=route_reader, clock=lambda: rig.now)


def workspace(rig):
    return SimpleNamespace(session_id=THREAD, repo_root=rig.repo)


@contextmanager
def cycle(rig, desk):
    with effect_guard(rig.home, THREAD), desk.activate(workspace(rig)):
        yield current_retention(rig.runtime)


def journal(rig, lease, namespace=NS, provider="slack", runtime=None):
    runtime = rig.runtime if runtime is None else runtime
    return ReplyTickets(runtime / namespace, runtime / "locks" / (provider + ".lock"), provider, runtime,
        lambda: {}, clock=lambda: rig.now, before_prune=lease.before_prune, retention_fence=lease.fence)


def present(rig, rows):
    with closing(sqlite3.connect(rig.runtime / "slack" / "reply-tickets.sqlite3")) as db:
        return sum(db.execute("SELECT count(*) FROM records WHERE namespace=? AND kind=? AND key=?",
                              (row["namespace"], row["kind"], row["key"])).fetchone()[0] for row in rows)


def add_neighbor(rig):
    rows = rows_for("slackbot:" + sha("neighbor")[:40], "neighbor expired work", suffix="neighbor", thread=OTHER)
    with closing(sqlite3.connect(rig.runtime / "slack" / "reply-tickets.sqlite3")) as db:
        for row in rows:
            db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)", tuple(row[name] for name in COLUMNS))
            db.execute("INSERT INTO recent_records VALUES(?,?,?,?,?,?,?)",
                       (row["namespace"], row["kind"], row["key"], NOW - 400000, NOW - 100000, 0, OTHER))
        db.commit()
    return rows


def quiet_cycle(rig, desk):
    with cycle(rig, desk) as lease:
        with journal(rig, lease).transaction():
            pass


def test_production_factory_uses_only_current_lexical_session_proof(rig, desk):
    current, old = advance_to_active(rig, desk)
    neighbor = add_neighbor(rig)
    args = (rig.runtime/NS, rig.runtime/'locks/provider.lock', 'slack', rig.runtime, lambda: {})
    outside = select_reply_journal(*args, clock=lambda: rig.now)
    with outside.transaction() as db:
        assert outside._retention_deferred
        outside.put(db, 'events', 'independent', dict(thread_id=OTHER))
    assert present(rig, current+old+neighbor) == len(current+old+neighbor)
    with cycle(rig, desk):
        selected = select_reply_journal(*args, clock=lambda: rig.now)
        with selected.transaction():
            assert not selected._retention_deferred
    assert present(rig, old) == 0 and present(rig, current+neighbor) == len(current+neighbor)
    with selected.transaction() as db:
        assert selected._retention_deferred
        selected.put(db, 'events', 'later', dict(thread_id=OTHER))


def advance_to_active(rig, desk):
    identifier, prompt, current, old = prepare(rig)
    quiet_cycle(rig, desk)  # Unknown baseline: pins current Q, deletes nothing.
    assert present(rig, current + old) == len(current + old)
    remove_queue(rig)
    start(rig, identifier, prompt)
    return current, old


INDEPENDENT = [dict(namespace=NS, kind="events", key="independent")]


def test_exact_session_lifecycle_pins_originals_and_retires_only_bound_uuid(rig, desk):
    identifier, prompt, current, old = prepare(rig)
    neighbor = add_neighbor(rig)
    receipts = {row["key"] for row in current if row["kind"] == "relay_native_receipts"}
    query = "SELECT key,value FROM records WHERE kind='relay_native_receipts' AND thread_id=?"
    with cycle(rig, desk) as lease:
        assert lease is not None and lease.binding.session == THREAD
        j = journal(rig, lease)
        with j.transaction() as db:
            assert j._retention_deferred  # First observation is an unknown baseline.
            assert db.execute("SELECT sum(protected) FROM recent_records").fetchone() == (len(current),)
            original = dict(db.execute(query, (THREAD,)))
    assert present(rig, current + old + neighbor) == len(current + old + neighbor)
    remove_queue(rig)
    start(rig, identifier, prompt)
    with cycle(rig, desk) as lease:
        j = journal(rig, lease)
        with j.transaction() as db:
            assert not j._retention_deferred
            assert dict(db.execute(query, (THREAD,))) == {key: original[key] for key in receipts}
    assert present(rig, old) == 0 and present(rig, current + neighbor) == len(current + neighbor)
    terminal(rig)
    quiet_cycle(rig, desk)
    assert present(rig, current) == 0
    assert present(rig, neighbor) == len(neighbor)  # Expired, but another session's rows.
    assert lease.binding.retention.pins.export()["bindings"] == []


@pytest.mark.parametrize("route", [None, [], (), [0], [-1], [True], [123, 123], ["123"], [2**31], 123, "fault"])
def test_missing_or_invalid_route_defers_only_gc_and_independent_write_commits(rig, desk, route):
    current, old = advance_to_active(rig, desk)
    def hook():
        if route == "fault":
            raise OSError("synthetic editor process table unavailable")
        return route
    rig.route_hook = hook
    with cycle(rig, desk) as lease:
        j = journal(rig, lease)
        with j.transaction() as db:
            assert j._retention_deferred
            j.put(db, "events", "independent", dict(thread_id=THREAD))
    assert present(rig, current + old) == len(current + old)
    assert present(rig, INDEPENDENT) == 1


@pytest.mark.parametrize("alive,collected", [(set(), False), ({123, 456}, False), ({123}, True)])
def test_exactly_one_current_writer_among_route_pids_is_required(rig, desk, alive, collected):
    current, old = advance_to_active(rig, desk)
    rig.route, rig.alive = [456, 123], alive
    with cycle(rig, desk) as lease:
        j = journal(rig, lease)
        with j.transaction():
            assert j._retention_deferred is not collected
    assert present(rig, old) == (0 if collected else len(old))
    assert present(rig, current) == len(current)


def test_unexpected_base_exception_from_route_is_not_swallowed(rig, desk):
    class Interrupted(BaseException):
        pass
    def hook():
        raise Interrupted()
    rig.route_hook = hook
    with pytest.raises(Interrupted):
        with cycle(rig, desk):
            pass


def test_route_is_read_once_per_provider_transaction_and_nested_fence_restores_outer(rig, desk):
    prepare(rig)
    with cycle(rig, desk) as lease:
        assert rig.route_calls == 1  # The optional warm-up read.
        with journal(rig, lease).transaction():
            assert rig.route_calls == 2  # Stream samples took no new snapshot.
            outer = desktop_retention._route.get()
            assert outer[0] is lease and outer[1] == (123,)
            rig.route = [123, 456]
            with lease.fence():
                assert desktop_retention._route.get()[1] == (123, 456) and rig.route_calls == 3
            assert desktop_retention._route.get() is outer
        assert desktop_retention._route.get() is None


@pytest.mark.parametrize("change", ["expired", "replaced"])
def test_expired_or_replaced_route_cannot_create_idle(rig, desk, change):
    current, old = advance_to_active(rig, desk)
    quiet_cycle(rig, desk)
    assert present(rig, old) == 0 and present(rig, current) == len(current)
    terminal(rig)
    if change == "expired":
        def hook():
            rig.now += PROOF_SECONDS + 1  # The route read outlives its proof window.
            return [123]
        rig.route_hook = hook
    else:
        rig.route, rig.alive = [456], {456}
    with cycle(rig, desk) as lease:
        j = journal(rig, lease)
        with j.transaction():
            assert j._retention_deferred
    assert present(rig, current) == len(current)


def test_same_context_nesting_and_exception_restore_outer_binding(rig, desk):
    prepare(rig)
    with effect_guard(rig.home, THREAD):
        with desk.activate(workspace(rig)):
            outer = current_retention(rig.runtime)
            assert outer is not None
            with pytest.raises(RuntimeError, match="fixture body"):
                with desk.activate(workspace(rig)):
                    inner = current_retention(rig.runtime)
                    assert inner is not None and inner is not outer and inner.binding is outer.binding
                    raise RuntimeError("fixture body")
            assert current_retention(rig.runtime) is outer and outer.live() and not inner.live()
            closed = journal(rig, inner)
            with closed.transaction():
                assert closed._retention_deferred
        assert current_retention(rig.runtime) is None and not outer.live()


def test_escaped_journal_after_scope_close_defers_deletion_only(rig, desk):
    current, old = advance_to_active(rig, desk)
    with cycle(rig, desk) as lease:
        escaped = journal(rig, lease)
    calls = rig.route_calls
    with effect_guard(rig.home, THREAD):
        with escaped.transaction() as db:
            assert escaped._retention_deferred
            escaped.put(db, "events", "independent", dict(thread_id=THREAD))
    assert rig.route_calls == calls and not lease.live()
    assert present(rig, current + old) == len(current + old) and present(rig, INDEPENDENT) == 1
    quiet_cycle(rig, desk)  # A live scope's own journal may then collect.
    assert present(rig, old) == 0 and present(rig, current) == len(current)


def test_copied_context_or_new_thread_cannot_borrow_eligibility(rig, desk):
    current, old = advance_to_active(rig, desk)
    seen, errors = [], []
    with cycle(rig, desk) as lease:
        copied = contextvars.copy_context()
        def borrowed():
            try:
                seen.append(current_retention(rig.runtime))
                j = journal(rig, lease)
                with j.transaction() as db:
                    seen.append(j._retention_deferred)
                    j.put(db, "events", "independent", dict(thread_id=THREAD))
            except BaseException as error:
                errors.append(error)
        for target in (lambda: copied.run(borrowed), borrowed):
            worker = threading.Thread(target=target)
            worker.start(); worker.join(10)
        assert current_retention(rig.runtime) is lease
    assert errors == [] and seen == [None, True, None, True]
    assert present(rig, current + old) == len(current + old) and present(rig, INDEPENDENT) == 1


def test_mismatched_runtime_or_coordinated_capability_cannot_borrow_proof(rig, desk, monkeypatch, tmp_path):
    current, old = advance_to_active(rig, desk)
    coordinated = []
    monkeypatch.setattr(desktop_retention, "current_control", lambda: coordinated[0] if coordinated else None)
    with cycle(rig, desk) as lease:
        other = tmp_path / "other-runtime"
        assert current_retention(other) is None and current_retention("runtime") is None
        assert current_retention(rig.runtime / "slack" / "..") is None
        foreign = journal(rig, lease, runtime=other)
        with foreign.transaction():
            assert foreign._retention_deferred  # Proof is bound to the exact runtime.
        coordinated.append(("coordinated-store", {}))
        assert current_retention(rig.runtime) is None
        j = journal(rig, lease)
        with j.transaction():
            assert j._retention_deferred
    assert present(rig, current + old) == len(current + old)
    calls = rig.route_calls
    with desk.activate(workspace(rig)):
        assert current_retention(rig.runtime) is None
    assert rig.route_calls == calls


def test_redirected_or_relative_roots_refuse_activation_preparation(rig):
    for runtime in ("relative-runtime", rig.runtime / "slack" / ".."):
        with pytest.raises(NativeInflightError):
            DesktopRetention(runtime, rig.home, route_reader=list)
    desk = DesktopRetention(rig.runtime, rig.home, route_reader=list)
    for value in (SimpleNamespace(session_id="not-a-session", repo_root=rig.repo),
                  SimpleNamespace(session_id=THREAD, repo_root="relative-repo")):
        with pytest.raises(NativeInflightError):
            with desk.activate(value):
                pass


def test_commit_remains_inside_the_real_supplied_fence(rig, desk, monkeypatch):
    prepare(rig)
    seen = []
    connect = sqlite3.connect
    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(lambda sql: seen.append(desktop_retention._route.get()) if sql == "COMMIT" else None)
        return db
    monkeypatch.setattr("codex_watchdog.reply_tickets.sqlite3.connect", traced)
    with cycle(rig, desk) as lease:
        with journal(rig, lease).transaction():
            pass
    assert seen and all(route is not None and route[0] is lease and route[1] == (123,) for route in seen)


def test_retain_sessions_drops_only_retired_in_memory_binding(rig, desk, monkeypatch):
    prepare(rig)
    with cycle(rig, desk) as lease:
        binding = lease.binding
    forgotten = []
    monkeypatch.setattr(desk.producer, "forget", forgotten.append)
    def files():
        return sorted((str(path), path.read_bytes()) for root in (rig.home, rig.runtime)
                      for path in root.rglob("*") if path.is_file())
    before = files()
    desk.retain_sessions([THREAD.upper()])
    assert forgotten == [] and len(desk._bindings) == 1
    desk.retain_sessions([OTHER, "not-a-session"])
    assert forgotten == [THREAD] and desk._bindings == {}
    assert files() == before
    with cycle(rig, desk) as lease:
        assert lease.binding is not binding


def test_schema2_journal_is_unchanged_and_not_migrated_under_binding(rig, desk):
    path = rig.runtime / "feishu" / "reply-tickets.sqlite3"
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE records(namespace TEXT)")
        db.execute("PRAGMA user_version=2")
        db.commit()
    before = path.read_bytes()
    with cycle(rig, desk) as lease:
        j = journal(rig, lease, "feishu/poll-relay-state.json", "feishu")
        with pytest.raises(ValueError, match="reply_ticket_retention_migration_required"):
            with j.transaction():
                pass
    assert path.read_bytes() == before


def test_route_gap_requires_new_unknown_baseline_before_retirement(rig, desk):
    # Successor of C334's unchanged six-original-reference counterexample.
    current, old = advance_to_active(rig, desk)
    quiet_cycle(rig, desk)
    assert present(rig, current) == len(current) == 6 and present(rig, old) == 0
    rig.route_hook = lambda: None
    quiet_cycle(rig, desk)
    assert present(rig, current) == 6
    terminal(rig)
    rig.route_hook = None
    with cycle(rig, desk) as lease:
        with lease.fence():
            recovered = lease.binding.read()
        j = journal(rig, lease)
        with j.transaction():
            assert j._retention_deferred
    assert recovered['native']['status'] == 'unknown' and recovered['status'] == 'unknown'
    assert present(rig, current) == 6


def test_stale_lease_read_cannot_reset_another_live_activation(rig, desk):
    current, old = advance_to_active(rig, desk)
    with cycle(rig, desk) as stale:
        pass
    with cycle(rig, desk) as live:
        original = dict(desk.producer._slots)
        assert original
        with stale.fence():
            with pytest.raises(NativeInflightError):
                stale.binding.read()
        assert desk.producer._slots == original
        j = journal(rig, live)
        with j.transaction() as db:
            assert not j._retention_deferred
            assert present(rig, current) == 6


def test_replaced_physical_runtime_cannot_borrow_old_proof(rig, desk):
    import shutil
    current, old = advance_to_active(rig, desk)
    with cycle(rig, desk) as lease:
        original = rig.runtime.with_name('original-runtime')
        rig.runtime.rename(original)
        shutil.copytree(original, rig.runtime)
        assert current_retention(rig.runtime) is None
        j = journal(rig, lease)
        with j.transaction() as db:
            assert j._retention_deferred
            j.put(db, 'events', 'independent-after-replacement', dict(thread_id=THREAD))
    assert present(rig, current+old) == len(current+old)


def test_case_equal_paths_do_not_substitute_physical_runtime(rig, monkeypatch):
    from pathlib import Path
    from codex_watchdog import current_work_retention
    other = rig.runtime.with_name(rig.runtime.name.capitalize())
    other.mkdir(exist_ok=True)
    if other.samefile(rig.runtime):
        pytest.skip('case-sensitive directory distinction unavailable on this filesystem')
    class CaseFoldPath(type(Path())):
        def __eq__(self, other):
            return str(self).lower() == str(other).lower()
        __hash__ = None
    monkeypatch.setattr(desktop_retention, 'Path', CaseFoldPath)
    monkeypatch.setattr(current_work_retention, 'Path', CaseFoldPath)
    desk = DesktopRetention(rig.runtime, rig.home, route_reader=list, clock=lambda: rig.now)
    with cycle(rig, desk) as lease:
        assert CaseFoldPath(other) == lease.binding.runtime
        assert current_retention(other) is None
        foreign = SimpleNamespace(database=CaseFoldPath(other)/'slack/reply-tickets.sqlite3',
                                  runtime=CaseFoldPath(other), provider='slack')
        with sqlite3.connect(':memory:') as db:
            with pytest.raises(NativeInflightError, match='runtime_changed'):
                lease.binding.retention.before_prune(foreign, db)
