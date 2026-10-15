"""Actual filesystem/SQLite history-growth counters for current snapshot paths."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from codex_watchdog import control_state as cs


SID = "11111111-2222-4333-8444-555555555555"
QID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
NOW = 1800000000.0


def test_actual_h0_1k_10k_100k_fixed_active_queue_and_zero_recent_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.time, "time", lambda: NOW)
    home = tmp_path / "home"
    home.mkdir()
    store = cs.ControlStore(home, SID, tmp_path / "repo", boot_id="fixture")
    entries = [dict(ticket_schema=1,
        created_at=datetime.fromtimestamp(NOW-10+n, timezone.utc).isoformat().replace("+00:00", "Z"),
        channel_id="C12345678", thread_ts="1799999%03d.000001" % n,
        event_fingerprint=hashlib.sha256(str(n).encode()).hexdigest()) for n in range(4)]
    with cs.control_file_lock(store.lock_path):
        store.merge_relay_mappings({}, entries)
    snapshot = cs.control_mapping_path(store)
    fixed = snapshot.read_bytes()
    empty = cs.control_mapping_empty(store)
    cs.control_atomic_json(snapshot, empty)
    zero = snapshot.read_bytes()
    snapshot.write_bytes(fixed)
    prompt = "fixture body"
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    instruction = "slackbot:history-fixture"
    raw = json.dumps({"UserInput": {"content": [{"type": "text", "text":
        "[CODEX_WATCHDOG_WAKE id=%s sha256=%s]\n%s" % (instruction, digest, prompt)}]}})
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.executescript("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT,payload_json TEXT);"
                         "CREATE INDEX queued_items_thread_order_idx ON queued_items(thread_id,id);")
        db.execute("INSERT INTO queued_items VALUES(?,?,?)", (QID, SID, raw))
        plan = db.execute("EXPLAIN QUERY PLAN SELECT id,payload_json FROM queued_items WHERE thread_id=?", (SID,)).fetchall()
    receipt = dict(thread_id=SID, queue_message_id=QID, instruction_id=instruction,
                   prompt_sha256=digest, state="enqueued")
    legacy = home / "remote-wake"
    legacy.mkdir()
    (legacy / (hashlib.sha256(instruction.encode()).hexdigest()+".json")).write_text(json.dumps(receipt))
    cs.control_publish_courier_association(home, receipt)
    maps = store.directory / "slack-threads"
    maps.mkdir()
    counters = Counter()
    real_open, real_glob, real_connect = Path.open, Path.glob, sqlite3.connect

    class MeteredFile:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            self.handle.__enter__()
            return self
        def __exit__(self, *args):
            return self.handle.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def read(self, *args):
            value = self.handle.read(*args)
            counters["logical_file_bytes_read"] += len(value if isinstance(value, bytes) else value.encode())
            return value

    def metered_open(path, mode="r", *args, **kwargs):
        counters["path_open_calls"] += 1
        if path.parent in (legacy, maps):
            counters["legacy_receipt_points_opened"] += 1
        return MeteredFile(real_open(path, mode, *args, **kwargs))

    def metered_glob(path, pattern):
        assert path not in (legacy, maps), "hot path enumerated historical directory"
        for item in real_glob(path, pattern):
            counters["dirents_yielded"] += 1
            yield item

    def metered_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(lambda statement: counters.update(sql_statements=1))
        db.set_progress_handler(lambda: counters.update(sql_vm_steps=1) or 0, 1)
        return db

    results = []
    previous = 0
    for history in (0, 1000, 10000, 100000):
        for n in range(previous, history):
            (maps / ("old-%06d.json" % n)).write_bytes(b'{"schema_version":1,"retired":true}')
            (legacy / ("old-%06d.json" % n)).write_bytes(b'{"state":"uncertain"}')
        previous = history
        for active, queued in ((4, 1), (0, 0)):
            snapshot.write_bytes(fixed if active else zero)
            with real_connect(home / "queue_1.sqlite") as db:
                db.execute("DELETE FROM queued_items")
                if queued:
                    db.execute("INSERT INTO queued_items VALUES(?,?,?)", (QID, SID, raw))
            counters.clear()
            with monkeypatch.context() as patch:
                patch.setattr(Path, "open", metered_open)
                patch.setattr(Path, "glob", metered_glob)
                patch.setattr(cs.sqlite3, "connect", metered_connect)
                assert len(store.relay_mappings()) == active
                assert cs.control_has_current_courier(home, SID) is bool(queued)
            row = dict(H=history, A=active, Q=queued, counts=dict(counters),
                       logical_native_payload_bytes_returned=len(raw.encode()) * queued,
                       provider_calls=0, native_rows_modified_by_measured_calls=0,
                       kernel_physical_io_measured=False)
            results.append(row)
            print("CURRENT_SNAPSHOT_HISTORY " + json.dumps(row, sort_keys=True))
    for active in (0, 4):
        counts = [row["counts"] for row in results if row["A"] == active]
        assert all(value == counts[0] for value in counts)
    assert any("SEARCH" in row[3] and "thread_id" in row[3] for row in plan)
    print("CURRENT_SNAPSHOT_QUERY_PLAN " + json.dumps(plan))


def test_actual_current_session_and_queue_sweeps_are_separate_from_history(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.time, "time", lambda: NOW)
    home = tmp_path / "home"
    home.mkdir()
    stores = []
    for n in range(12):
        sid = "11111111-2222-4333-8444-%012d" % n
        store = cs.ControlStore(home, sid, tmp_path / "repo", boot_id="fixture")
        entries = [dict(ticket_schema=1, created_at="2027-01-15T07:59:50Z",
            channel_id="C12345678", thread_ts="1799999%03d.000001" % i,
            event_fingerprint=hashlib.sha256(str(i).encode()).hexdigest()) for i in range(4)]
        cs.control_mapping_merge(store, entries, now=NOW)
        stores.append(store)
    original_read = cs.control_read_json
    counts = Counter()
    def read(path):
        counts["snapshot_point_reads"] += 1
        counts["logical_json_bytes_read"] += path.stat().st_size
        return original_read(path)
    with monkeypatch.context() as patch:
        patch.setattr(cs, "control_read_json", read)
        for active in (1, 4, 12):
            counts.clear()
            assert sum(len(store.relay_mappings()) for store in stores[:active]) == 4 * active
            assert counts["snapshot_point_reads"] == active
            print("CURRENT_SESSION_SWEEP " + json.dumps(dict(sessions=active, tickets=4*active, counts=dict(counts))))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.executescript("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT,payload_json TEXT);"
                         "CREATE INDEX queued_items_thread_order_idx ON queued_items(thread_id,id);")
    real_connect = cs.sqlite3.connect
    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(lambda statement: counts.update(sql_statements=1))
        db.set_progress_handler(lambda: counts.update(sql_vm_steps=1) or 0, 1)
        return db
    previous = None
    for queued in (0, 1, 32, 256):
        with real_connect(home / "queue_1.sqlite") as db:
            db.execute("DELETE FROM queued_items")
            for n in range(queued):
                qid = "aaaaaaaa-bbbb-4ccc-8ddd-%012d" % n
                db.execute("INSERT INTO queued_items VALUES(?,?,?)", (qid, SID,
                    '{"UserInput":{"content":[{"type":"text","text":"ordinary input"}]}}'))
        counts.clear()
        with monkeypatch.context() as patch:
            patch.setattr(cs.sqlite3, "connect", connect)
            assert cs.control_has_current_courier(home, SID) is False
        assert counts["sql_statements"] == 3
        if previous is not None:
            assert counts["sql_vm_steps"] > previous
        previous = counts["sql_vm_steps"]
        print("CURRENT_QUEUE_SWEEP " + json.dumps(dict(Q=queued, counts=dict(counts),
            native_rows_modified_by_measured_call=0, provider_calls=0)))
