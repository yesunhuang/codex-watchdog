"""Forward-only host completion observation from two current Stop points.

No history discovery, native action, receipt repair or external effect occurs
here. The caller supplies its positively bound controlled runtime and retains
the ordinary owner/sender fences for notification and cursor publication.
"""
from datetime import timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid

from .stop_snapshots import StopSnapshots, STOP_MAX_BYTES, STOP_RETENTION, timestamp

CURSOR = "host_completion_cursor"
OUTPUT_MAX_BYTES = 262144


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate host completion key")
        result[key] = value
    return result


def _integer(value):
    if len(value) > 128:
        raise ValueError("oversized host completion integer")
    return int(value)


def _float(value):
    result = float(value) if len(value) <= 128 else float("inf")
    if not math.isfinite(result):
        raise ValueError("nonfinite host completion number")
    return result


def _output_matches(output, chars, digest):
    if not isinstance(output, str) or type(chars) is not int or len(output) != chars:
        return False
    try:
        data = output.encode("utf-8")
    except UnicodeError:
        return False
    return (len(data) <= OUTPUT_MAX_BYTES
            and (hashlib.sha256(data).hexdigest() if output else None) == digest)


def _read(path, limit=STOP_MAX_BYTES):
    """No symlink/FIFO, oversized allocation or changing-file acceptance."""
    try:
        if path.resolve() != path.absolute() or path.is_symlink():
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                    or hasattr(os, "getuid") and before.st_uid != os.getuid()):
                return None
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if (path.resolve() != path.absolute() or path.is_symlink() or len(raw) > limit
                or identity(before) != identity(after) or identity(after) != identity(current)):
            return None
        value = json.loads(raw, object_pairs_hook=_pairs, parse_int=_integer, parse_float=_float,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError, RuntimeError, OverflowError):
        return None


class HostCompletions:
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        # A malformed persistent cursor is retained. A process-local baseline
        # still permits a genuinely later completion without rewriting it.
        self.refused_scope = None
        self.refused_cursor = None

    def observe(self, state, session, repo, fallback, now, fallback_delay):
        if now.utcoffset() is None:
            return None
        now = now.astimezone(timezone.utc)
        if not _uuid(session) or self.runtime.resolve() != self.runtime.absolute():
            return None
        repo = Path(repo).resolve()
        snapshots = StopSnapshots(self.runtime, reader=_read)
        path = snapshots.path(session, repo)
        raw = _read(path)
        seen = (hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
                                        allow_nan=False).encode()).hexdigest() if raw is not None else None)
        raw_event = raw.get("event") if isinstance(raw, dict) else None
        baseline_turn = raw_event.get("turn_id") if isinstance(raw_event, dict) else None
        if not _uuid(baseline_turn):
            baseline_turn = fallback.get("turn_id") if isinstance(fallback, dict) else None
        baseline_turn = baseline_turn if _uuid(baseline_turn) else None
        scope = (session, str(repo))
        cursor = state.get(CURSOR)
        valid = (isinstance(cursor, dict) and cursor.get("schema_version") == 1
                 and type(cursor.get("schema_version")) is int and cursor.get("session_id") == session
                 and cursor.get("repo_path") == str(repo)
                 and timestamp(cursor.get("floor")) is not None
                 and timestamp(cursor["floor"]) <= now
                 and (cursor.get("seen") is None or isinstance(cursor["seen"], str)
                      and len(cursor["seen"]) == 64 and all(c in "0123456789abcdef" for c in cursor["seen"]))
                 and (cursor.get("turn_id") is None or _uuid(cursor["turn_id"])))
        if not valid:
            if CURSOR not in state:
                cursor = dict(schema_version=1, session_id=session, repo_path=str(repo),
                              floor=now.isoformat(), seen=seen, turn_id=baseline_turn)
                state[CURSOR] = cursor
                return None
            if self.refused_scope != scope:
                self.refused_scope = scope
                self.refused_cursor = dict(schema_version=1, session_id=session, repo_path=str(repo),
                                          floor=now.isoformat(), seen=seen, turn_id=baseline_turn)
                return None
            cursor = self.refused_cursor
        else:
            self.refused_scope = self.refused_cursor = None
        floor = timestamp(cursor["floor"])
        if seen is not None and seen != cursor["seen"]:
            # Consume this current identity even when its output is unsafe.
            # Late repair of that same terminal is never a replay mechanism.
            cursor["seen"] = seen
            raw_turn = raw.get("event", {}).get("turn_id") if isinstance(raw.get("event"), dict) else None
            cursor["turn_id"] = raw_turn if _uuid(raw_turn) else None
            snapshot = snapshots.read(session, repo)
            if snapshot is None:
                cursor["floor"] = now.isoformat()
                return None
            event = snapshot["event"]
            completed, recorded = timestamp(event["hook_completed_at"]), timestamp(event["recorded_at"])
            published = timestamp(snapshot["published_at"])
            turn, invocation = event["turn_id"], event["invocation_id"]
            cursor["turn_id"] = turn if _uuid(turn) else None
            if not (floor < completed <= recorded <= published <= now
                    and now - completed < STOP_RETENTION and _uuid(turn) and _uuid(invocation)):
                cursor["floor"] = now.isoformat()
                return None
            cursor["floor"] = completed.isoformat()
            if turn == state.get("last_completion_turn"):
                return None
            chars, digest = event["last_output_chars"], event["last_output_sha256"]
            output = ""
            if chars:
                spool = _read(self.runtime / "transient/stop-output" / (invocation + ".json"), OUTPUT_MAX_BYTES)
                if (spool is None or spool.get("schema_version") != 1 or type(spool.get("schema_version")) is not int
                        or type(spool.get("last_output_chars")) is not int
                        or any(spool.get(k) != v for k, v in dict(invocation_id=invocation, session_id=session,
                            turn_id=turn, workspace=str(repo), last_output_chars=chars, last_output_sha256=digest).items())
                        or not isinstance(spool.get("last_assistant_message"), str)):
                    return None
                output = spool["last_assistant_message"]
                if not _output_matches(output, chars, digest):
                    return None
            return dict(turn_id=turn, completed_at=event["hook_completed_at"], final_output=output,
                        final_output_sha256=digest, final_output_chars=chars, source="compact_stop",
                        invocation_id=invocation, hook_outcome=event["outcome"])
        if raw is None and (path.exists() or path.is_symlink()):
            cursor["floor"] = now.isoformat()
            return None
        # Same-turn fallback cannot bypass a refused/missing output spool.
        if not isinstance(fallback, dict) or fallback.get("turn_id") in (cursor.get("turn_id"), state.get("last_completion_turn")):
            return None
        completed = timestamp(fallback.get("completed_at"))
        output = fallback.get("final_output")
        if (not _uuid(fallback.get("turn_id")) or completed is None or not floor < completed <= now
                or not fallback_delay <= (now - completed).total_seconds() < STOP_RETENTION.total_seconds()
                or not _output_matches(output, fallback.get("final_output_chars"), fallback.get("final_output_sha256"))):
            return None
        cursor["floor"] = completed.isoformat()
        return fallback
