"""Tests for the read-only pending Git wake queue predicate."""

import hashlib
import itertools
import json
import sqlite3
import time

import pytest

from codex_watchdog import git_wake_queue as gwq
from codex_watchdog.git_wake_queue import GitQueueUnknown, pending_git_wake

THREAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
BODY = "Git changed on main.\n"
SHA = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
MARKER = "[CODEX_WATCHDOG_WAKE id=git:0123456789abcdef:abcdef0 sha256=%s]\n"
_ORDER = itertools.count()


def item(n):
    return "%08x-0000-4000-8000-%012x" % (n, n)


def wake(body=BODY, digest=None):
    if digest is None:
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return MARKER % digest + body


def payload(*texts):
    parts = [{"type": "text", "text": text} for text in texts]
    return json.dumps({"UserInput": {"content": parts}}, ensure_ascii=False)


def make_db(tmp_path, index=True, name="queue.sqlite"):
    path = tmp_path / name
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE queued_items (id TEXT PRIMARY KEY, thread_id TEXT,"
            " payload_json TEXT, queue_order INTEGER)"
        )
        if index:
            con.execute(
                "CREATE INDEX queued_items_thread"
                " ON queued_items (thread_id, queue_order)"
            )
        con.commit()
    finally:
        con.close()
    return path


def execute(path, sql, rows):
    con = sqlite3.connect(str(path))
    try:
        with con:
            con.executemany(sql, rows)
    finally:
        con.close()


def insert(path, *rows):
    execute(
        path,
        "INSERT INTO queued_items (id, thread_id, payload_json, queue_order)"
        " VALUES (?, ?, ?, ?)",
        [(row_id, thread, value, next(_ORDER)) for row_id, thread, value in rows],
    )


def reason(database, *args, **kwargs):
    with pytest.raises(GitQueueUnknown) as caught:
        pending_git_wake(database, *args, **kwargs)
    assert str(caught.value) == caught.value.reason
    return caught.value.reason


@pytest.fixture
def db(tmp_path):
    return make_db(tmp_path)


def test_error_type_is_value_error():
    assert issubclass(GitQueueUnknown, ValueError)


def test_current_git_wake_matches(db):
    insert(
        db,
        (item(1), THREAD, payload("please review")),
        (item(2), THREAD, payload(wake())),
    )
    assert pending_git_wake(db, THREAD, SHA) == item(2)


def test_empty_queue_has_no_pending_wake(db):
    assert pending_git_wake(db, THREAD, SHA) is None


def test_wake_in_other_session_is_not_pending(db):
    insert(db, (item(1), OTHER, payload(wake())))
    assert pending_git_wake(db, THREAD, SHA) is None
    assert pending_git_wake(db, OTHER, SHA) == item(1)


def test_non_git_inputs_are_unrelated(db):
    git_id = "id=git:0123456789abcdef:abcdef0"
    image = {"UserInput": {"content": [{"type": "image", "text": wake()}]}}
    insert(
        db,
        (item(1), THREAD, payload("human: " + wake())),
        (item(2), THREAD, payload(wake(), "second part")),
        (item(3), THREAD, payload(wake().replace(git_id, "id=resume:0123456789abcdef"))),
        (item(4), THREAD, json.dumps({"Other": {"text": wake()}})),
        (item(5), THREAD, json.dumps([wake()])),
        (item(6), THREAD, json.dumps(image)),
        (item(7), THREAD, payload(wake().replace("abcdef0", "ABCDEF0"))),
        (item(8), THREAD, payload(wake().replace("abcdef0 ", "abcdef "))),
        (item(9), THREAD, payload()),
    )
    assert pending_git_wake(db, THREAD, SHA) is None


def test_wrong_or_tampered_digest_is_unrelated(db):
    insert(
        db,
        (item(1), THREAD, payload(wake("Another tip.\n"))),
        (item(2), THREAD, payload(wake("Tampered body.\n", digest=SHA))),
        (item(3), THREAD, payload(wake(digest="0" * 64))),
    )
    assert pending_git_wake(db, THREAD, SHA) is None
    other_sha = hashlib.sha256(b"Another tip.\n").hexdigest()
    assert pending_git_wake(db, THREAD, other_sha) == item(1)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("{not json", "malformed-payload"),
        (None, "unsupported-payload"),
        (sqlite3.Binary(payload(wake()).encode("utf-8")), "unsupported-payload"),
        (payload("x" * 131072), "oversized-payload"),
    ],
    ids=["malformed", "null", "blob", "oversized"],
)
def test_unknown_rows_fail_closed(db, value, expected):
    insert(db, (item(1), THREAD, payload("human")), (item(2), THREAD, value))
    assert reason(db, THREAD, SHA) == expected


def test_invalid_utf8_text_fails_closed(db):
    execute(
        db,
        "INSERT INTO queued_items (id, thread_id, payload_json, queue_order)"
        " VALUES (?, ?, CAST(? AS TEXT), 0)",
        [(item(1), THREAD, sqlite3.Binary(b"\xff\xfe{"))],
    )
    assert reason(db, THREAD, SHA) == "malformed-payload"


def test_positive_match_survives_unknown_rows(db):
    insert(
        db,
        (item(1), THREAD, "{bad"),
        (item(2), THREAD, payload("x" * gwq._MAX_PAYLOAD_BYTES)),
        (item(3), THREAD, None),
        (item(4), THREAD, payload(wake())),
    )
    assert pending_git_wake(db, THREAD, SHA) == item(4)


@pytest.mark.parametrize(
    "row_id", ["not-a-uuid", item(0xABC).upper(), "{%s}" % item(0xABC)]
)
def test_matching_wake_with_invalid_queue_id_fails_closed(db, row_id):
    insert(db, (row_id, THREAD, payload(wake())))
    assert reason(db, THREAD, SHA) == "invalid-queued-id"


def test_overflow_never_reports_empty(db):
    insert(db, *[(item(n), THREAD, payload("human %d" % n)) for n in range(1, 4)])
    assert reason(db, THREAD, SHA, limit=2) == "queue-overflow"
    assert pending_git_wake(db, THREAD, SHA, limit=3) is None


def test_match_within_limit_is_returned_despite_overflow(db):
    insert(
        db,
        (item(1), THREAD, payload(wake())),
        (item(2), THREAD, payload("a")),
        (item(3), THREAD, payload("b")),
    )
    assert pending_git_wake(db, THREAD, SHA, limit=1) == item(1)


def test_unindexed_thread_lookup_fails_closed(tmp_path):
    database = make_db(tmp_path, index=False)
    assert reason(database, THREAD, SHA) == "unindexed-thread-lookup"
    insert(database, (item(1), THREAD, payload(wake())))
    assert reason(database, THREAD, SHA) == "unindexed-thread-lookup"


def sized_wake_payload(size):
    base = len(payload(wake("")).encode("utf-8"))
    body = "a" * (size - base)
    text = payload(wake(body))
    assert len(text.encode("utf-8")) == size
    return text, hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_payload_byte_budget_is_exact(db):
    budget = gwq._MAX_PAYLOAD_BYTES
    assert budget == 131072
    exact, exact_sha = sized_wake_payload(budget)
    over, over_sha = sized_wake_payload(budget + 1)
    insert(db, (item(1), THREAD, exact), (item(2), OTHER, over))
    assert pending_git_wake(db, THREAD, exact_sha) == item(1)
    assert reason(db, OTHER, over_sha) == "oversized-payload"


def test_payload_budget_counts_utf8_bytes(db):
    body = "\u00e9" * 70000
    text = payload(wake(body))
    assert len(text) < gwq._MAX_PAYLOAD_BYTES < len(text.encode("utf-8"))
    insert(db, (item(1), THREAD, text))
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert reason(db, THREAD, body_sha) == "oversized-payload"


def test_queue_removal_and_reinsert_follow_current_snapshot(db):
    insert(db, (item(1), THREAD, payload(wake())))
    assert pending_git_wake(db, THREAD, SHA) == item(1)
    execute(db, "DELETE FROM queued_items WHERE id = ?", [(item(1),)])
    assert pending_git_wake(db, THREAD, SHA) is None
    insert(db, (item(2), THREAD, payload(wake())))
    assert pending_git_wake(db, THREAD, SHA) == item(2)


def test_reads_preserve_database_and_directory(db):
    insert(db, (item(1), THREAD, payload(wake())), (item(2), THREAD, "{bad"))
    before = db.read_bytes()
    listing = sorted(p.name for p in db.parent.iterdir())
    assert pending_git_wake(db, THREAD, SHA) == item(1)
    assert pending_git_wake(db, OTHER, SHA) is None
    assert reason(db, THREAD, "0" * 64) == "malformed-payload"
    assert reason(db, THREAD, SHA, limit=0) == "invalid-limit"
    assert db.read_bytes() == before
    assert sorted(p.name for p in db.parent.iterdir()) == listing


def vm_ops(database, sql, params):
    con = sqlite3.connect(str(database))
    calls = [0]

    def tick():
        calls[0] += 1
        return 0

    try:
        con.set_progress_handler(tick, 100)
        con.execute(sql, params).fetchall()
        ops = calls[0] * 100
        con.set_progress_handler(None, 100)
        plan = [str(row[-1]) for row in con.execute("EXPLAIN QUERY PLAN " + sql, params)]
    finally:
        con.close()
    return ops, plan


def test_large_other_session_history_keeps_bounded_indexed_cost(db, monkeypatch):
    execute(
        db,
        "INSERT INTO queued_items VALUES (?, ?, ?, ?)",
        [(item(n), OTHER, payload(wake()), n) for n in range(1, 10001)],
    )
    insert(db, (item(20000), THREAD, payload(wake())))
    params = (gwq._MAX_PAYLOAD_BYTES, THREAD, 129)
    indexed_ops, plan = vm_ops(db, gwq._QUERY, params)
    scanned = gwq._QUERY.replace("FROM queued_items", "FROM queued_items NOT INDEXED")
    scan_ops, _ = vm_ops(db, scanned, params)
    assert any(d.startswith("SEARCH") and "(thread_id=?" in d for d in plan)
    assert not any("SCAN" in d or "TEMP B-TREE" in d for d in plan)
    assert indexed_ops <= 1000 < 10000 <= scan_ops
    monkeypatch.setattr(gwq, "_PROGRESS_STEP", 100)
    monkeypatch.setattr(gwq, "_PROGRESS_CALLS", 100)
    assert pending_git_wake(db, THREAD, SHA) == item(20000)
    assert pending_git_wake(db, OTHER, SHA, limit=4) == item(1)


def test_progress_ceiling_fails_closed(db, monkeypatch):
    insert(db, *[(item(n), THREAD, payload("human %d" % n)) for n in range(1, 50)])
    monkeypatch.setattr(gwq, "_PROGRESS_STEP", 1)
    monkeypatch.setattr(gwq, "_PROGRESS_CALLS", 5)
    assert reason(db, THREAD, SHA) == "progress-budget-exceeded"


def test_busy_writer_fails_closed_quickly(db):
    writer = sqlite3.connect(str(db), isolation_level=None)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        assert reason(db, THREAD, SHA) == "unreadable-database"
        assert time.monotonic() - started < 5
    finally:
        writer.execute("ROLLBACK")
        writer.close()


@pytest.mark.parametrize(
    "thread_id, prompt, limit, expected",
    [
        ("not-a-uuid", SHA, 128, "invalid-thread-id"),
        ("{%s}" % THREAD, SHA, 128, "invalid-thread-id"),
        (THREAD.replace("-", ""), SHA, 128, "invalid-thread-id"),
        (item(0xABC).upper(), SHA, 128, "invalid-thread-id"),
        (None, SHA, 128, "invalid-thread-id"),
        (THREAD, "A" * 64, 128, "invalid-prompt-sha256"),
        (THREAD, SHA[:-1], 128, "invalid-prompt-sha256"),
        (THREAD, SHA + "\n", 128, "invalid-prompt-sha256"),
        (THREAD, None, 128, "invalid-prompt-sha256"),
        (THREAD, SHA, 0, "invalid-limit"),
        (THREAD, SHA, 1025, "invalid-limit"),
        (THREAD, SHA, True, "invalid-limit"),
        (THREAD, SHA, "8", "invalid-limit"),
    ],
)
def test_invalid_arguments(db, thread_id, prompt, limit, expected):
    assert reason(db, thread_id, prompt, limit=limit) == expected


def test_missing_database_and_schema_fail_closed(tmp_path):
    absent = tmp_path / "absent.sqlite"
    assert reason(absent, THREAD, SHA) == "missing-database"
    assert not absent.exists()
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(str(empty)).close()
    assert reason(empty, THREAD, SHA) == "missing-table"
    view = tmp_path / "view.sqlite"
    con = sqlite3.connect(str(view))
    try:
        con.execute("CREATE TABLE base (id TEXT, thread_id TEXT, payload_json TEXT)")
        con.execute("CREATE INDEX base_thread ON base (thread_id)")
        con.execute("CREATE VIEW queued_items AS SELECT * FROM base")
        con.commit()
    finally:
        con.close()
    assert reason(view, THREAD, SHA) == "unsupported-schema"
    narrow = tmp_path / "narrow.sqlite"
    con = sqlite3.connect(str(narrow))
    try:
        con.execute("CREATE TABLE queued_items (id TEXT PRIMARY KEY, thread_id TEXT)")
        con.execute("CREATE INDEX narrow_thread ON queued_items (thread_id)")
        con.commit()
    finally:
        con.close()
    assert reason(narrow, THREAD, SHA) == "unsupported-schema"


def test_embedded_source_is_self_contained(db):
    insert(db, (item(1), THREAD, payload(wake())))
    namespace = {"__name__": "remote_probe"}
    exec(gwq.GIT_QUEUE_SOURCE, namespace)
    assert namespace["pending_git_wake"](db, THREAD, SHA) == item(1)
    assert issubclass(namespace["GitQueueUnknown"], ValueError)
    assert "codex_watchdog" not in gwq.GIT_QUEUE_SOURCE
    assert "ORDER BY" not in gwq.GIT_QUEUE_SOURCE
