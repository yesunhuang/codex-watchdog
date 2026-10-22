"""Read-only check for an automatic Git wake already in a native queue.

The helper runs under the caller's existing per-session owner/queue lock. It
never claims, queues, retries or writes; it only reports what one bounded
current snapshot of the exact session's queue proves. GIT_QUEUE_SOURCE is
executed here so the identical source can be embedded in a bare Python probe.
"""

GIT_QUEUE_SOURCE = r'''
import hashlib
import json
import pathlib
import re
import sqlite3
import uuid

_MAX_LIMIT = 1024
_MAX_PAYLOAD_BYTES = 131072
_MAX_COLUMNS = 64
_MAX_PLAN_ROWS = 8
_PROGRESS_STEP = 1000
_PROGRESS_CALLS = 2000
_BUSY_TIMEOUT_SECONDS = 0.25
_REQUIRED_COLUMNS = frozenset(("id", "thread_id", "payload_json"))
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_WAKE = re.compile(
    r"\A\[CODEX_WATCHDOG_WAKE id=git:[0-9a-f]{16}:[0-9a-f]{7,64}"
    r" sha256=([0-9a-f]{64})\]\n(.*)\Z",
    re.DOTALL,
)
# Existence check only, so rows are not sorted. Oversized payloads are never
# fetched; only their UTF-8 byte length is compared inside SQLite.
_QUERY = (
    "SELECT id, CASE WHEN typeof(payload_json) = 'text'"
    " AND length(CAST(payload_json AS BLOB)) <= ? THEN payload_json END,"
    " typeof(payload_json) FROM queued_items WHERE thread_id = ? LIMIT ?"
)


class GitQueueUnknown(ValueError):
    """The current queue snapshot cannot prove whether a Git wake is pending."""

    def __init__(self, reason):
        ValueError.__init__(self, reason)
        self.reason = reason


def _canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _payload_status(payload, kind, prompt_sha256):
    """Return "match", None for unrelated input, or an unknown-evidence reason."""
    if kind != "text":
        return "unsupported-payload"
    if payload is None:
        return "oversized-payload"
    try:
        value = json.loads(_text(payload))
    except (ValueError, RecursionError):
        return "malformed-payload"
    user = value.get("UserInput") if isinstance(value, dict) else None
    content = user.get("content") if isinstance(user, dict) else None
    if not isinstance(content, list) or len(content) != 1:
        return None
    part = content[0]
    if not isinstance(part, dict) or part.get("type") != "text":
        return None
    text = part.get("text")
    found = _WAKE.match(text) if isinstance(text, str) else None
    if found is None or found.group(1) != prompt_sha256:
        return None
    try:
        body = found.group(2).encode("utf-8")
    except UnicodeEncodeError:
        return None
    if hashlib.sha256(body).hexdigest() != prompt_sha256:
        return None
    return "match"


def _read_snapshot(connection, thread_id, prompt_sha256, limit):
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    kinds = connection.execute(
        "SELECT type FROM sqlite_master WHERE name = 'queued_items' LIMIT 2"
    ).fetchall()
    if not kinds:
        raise GitQueueUnknown("missing-table")
    if [_text(row[0]) for row in kinds] != ["table"]:
        raise GitQueueUnknown("unsupported-schema")
    columns = connection.execute("PRAGMA table_info(queued_items)").fetchmany(
        _MAX_COLUMNS + 1
    )
    names = set(_text(row[1]) for row in columns)
    if len(columns) > _MAX_COLUMNS or not _REQUIRED_COLUMNS <= names:
        raise GitQueueUnknown("unsupported-schema")
    params = (_MAX_PAYLOAD_BYTES, thread_id, limit + 1)
    plan = connection.execute("EXPLAIN QUERY PLAN " + _QUERY, params).fetchmany(
        _MAX_PLAN_ROWS + 1
    )
    details = [_text(row[-1]) for row in plan]
    indexed = any(d.startswith("SEARCH") and "(thread_id=?" in d for d in details)
    unsafe = any(
        "SCAN" in d or "TEMP B-TREE" in d or "AUTOMATIC" in d for d in details
    )
    if len(details) > _MAX_PLAN_ROWS or not indexed or unsafe:
        raise GitQueueUnknown("unindexed-thread-lookup")
    rows = connection.execute(_QUERY, params).fetchmany(limit + 1)
    unknown = None
    for row_id, payload, kind in rows[:limit]:
        status = _payload_status(payload, _text(kind), prompt_sha256)
        if status == "match":
            try:
                queued = _text(row_id)
            except UnicodeDecodeError:
                queued = None
            if _canonical_uuid(queued):
                return queued
            status = "invalid-queued-id"
        if unknown is None:
            unknown = status
    if len(rows) > limit:
        raise GitQueueUnknown("queue-overflow")
    if unknown is not None:
        raise GitQueueUnknown(unknown)
    return None


def pending_git_wake(database, thread_id, prompt_sha256, *, limit=128):
    """Return the queued UUID of a matching current Git wake, or None.

    None means a complete bounded snapshot of this exact session's queue holds
    no matching wake. Anything that cannot be proven raises GitQueueUnknown.
    Read-only; the caller holds the per-session owner/queue lock.
    """
    if not _canonical_uuid(thread_id):
        raise GitQueueUnknown("invalid-thread-id")
    if not isinstance(prompt_sha256, str) or not _SHA256.match(prompt_sha256):
        raise GitQueueUnknown("invalid-prompt-sha256")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise GitQueueUnknown("invalid-limit")
    if not 1 <= limit <= _MAX_LIMIT:
        raise GitQueueUnknown("invalid-limit")
    try:
        path = pathlib.Path(database)
        present = path.is_file()
        uri = path.absolute().as_uri() + "?mode=ro"
    except (OSError, TypeError, ValueError):
        raise GitQueueUnknown("unreadable-database") from None
    if not present:
        raise GitQueueUnknown("missing-database")
    calls = [0]

    def progress():
        calls[0] += 1
        return 1 if calls[0] > _PROGRESS_CALLS else 0

    try:
        connection = sqlite3.connect(
            uri, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None, uri=True
        )
    except sqlite3.Error:
        raise GitQueueUnknown("unreadable-database") from None
    try:
        connection.text_factory = bytes
        connection.set_progress_handler(progress, _PROGRESS_STEP)
        return _read_snapshot(connection, thread_id, prompt_sha256, limit)
    except sqlite3.Error:
        if calls[0] > _PROGRESS_CALLS:
            raise GitQueueUnknown("progress-budget-exceeded") from None
        raise GitQueueUnknown("unreadable-database") from None
    except UnicodeDecodeError:
        raise GitQueueUnknown("unsupported-schema") from None
    finally:
        connection.close()
'''

exec(GIT_QUEUE_SOURCE)

__all__ = ["GIT_QUEUE_SOURCE", "GitQueueUnknown", "pending_git_wake"]
