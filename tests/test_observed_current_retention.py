"""Actual point-reader/stream/Q/pre-prune composition with simulated OS samples."""
from codex_watchdog import observed_writer
from codex_watchdog.current_work_retention import CurrentWorkRetention
from codex_watchdog.native_inflight import NativeInflightError
from test_native_inflight import THREAD, remove_queue
from test_observed_native_work import rig, native_rig, start, terminal
from test_current_work_retention import prepare, journal


def composition(rig, monkeypatch, *, unsupported=False):
    lock = rig.home / "thread-writer-locks" / (THREAD + ".lock")
    lock.parent.mkdir(); lock.write_bytes(b"fixture native resource")
    births = {"linux": "linux:11111111-2222-4333-8444-000000000008:12345",
              "darwin": "darwin:1790000000:123456", "win32": "win32:133000000000000000"}
    def sample(path, identity, pid, platform):
        assert rig.held and path == lock and pid == 123
        if unsupported:
            raise NativeInflightError("observed_writer_lock_unavailable")
        return pid, births[platform]
    monkeypatch.setattr(observed_writer, "_sample", sample)
    monkeypatch.setattr(observed_writer.socket, "gethostname", lambda: "fixture-node")
    def capture():
        assert rig.held
        return observed_writer.capture_writer(rig.home, THREAD, rig.repo, 123, clock=lambda: rig.now)
    rig.observe = lambda: rig.producer.observe(THREAD, rig.repo, current_writer=capture, fence=rig.fence)
    return CurrentWorkRetention(rig.runtime, rig.home, THREAD, rig.repo,
        reader=rig.collect, fence=rig.fence, clock=lambda: rig.now)


def test_actual_capture_stream_and_original_reference_preflight_compose_before_gc(rig, monkeypatch):
    identifier, prompt, current, old = prepare(rig)
    retained = composition(rig, monkeypatch)
    j = journal(rig, retained)
    receipt_keys = {row["key"] for row in current if row["kind"] == "relay_native_receipts"}
    with j.transaction() as db:
        assert j._retention_deferred
        assert db.execute("SELECT count(*) FROM records").fetchone() == (len(current + old),)
        assert db.execute("SELECT sum(protected) FROM recent_records").fetchone() == (len(current),)
        original = dict(db.execute("SELECT key,value FROM records WHERE kind='relay_native_receipts'"))
    remove_queue(rig)
    start(rig, identifier, prompt)
    with j.transaction() as db:
        assert not j._retention_deferred
        assert db.execute("SELECT count(*) FROM records").fetchone() == (len(current),)
        assert dict(db.execute("SELECT key,value FROM records WHERE kind='relay_native_receipts'")) == {key: original[key] for key in receipt_keys}
    terminal(rig)
    with j.transaction() as db:
        assert db.execute("SELECT count(*) FROM records").fetchone() == (0,)
    assert retained.pins.export()["bindings"] == []


def test_unavailable_resource_refuses_only_retention_and_independent_body_commits(rig, monkeypatch):
    _, _, current, old = prepare(rig)
    retained = composition(rig, monkeypatch, unsupported=True)
    j = journal(rig, retained)
    with j.transaction() as db:
        assert j._retention_deferred
        j.put(db, "events", "independent", dict(thread_id=THREAD))
    with j.transaction() as db:
        assert db.execute("SELECT count(*) FROM records").fetchone() == (len(current + old) + 1,)
        assert j.get(db, "events", "independent") == dict(thread_id=THREAD)
