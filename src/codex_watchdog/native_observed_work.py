"""Current stream work bound to a real observed writer, not a control grant.

The caller retains its existing exact editor/observer/provider fences. A fresh
resource-to-process observation and actual native birth generation are required;
neither a held-lock Boolean, PID alone, old log nor invented epoch suffices.
This read-only subject cannot authorize ControlStore selection or handback.
"""
from pathlib import Path
import re

from .native_inflight import NativeInflightError, PROOF_SECONDS, _number
from .native_stream_work import NativeStreamWork, _validate_stream_work

KIND = "native_observed_writer_stream_work"
PROOF_KIND = "observed_native_writer_stream"
WRITER_KIND = "observed_native_writer"


def validate_writer(value, scope):
    """Validate the disjoint evidence shape; actual reads remain caller-owned."""
    fields = {"kind", "platform", "node", "pid", "process_birth", "lock_path",
              "lock_device", "lock_inode"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("kind") != WRITER_KIND
            or value.get("platform") not in ("linux", "darwin", "win32")
            or type(value.get("pid")) is not int or value["pid"] <= 0
            or not isinstance(value.get("node"), str) or not 1 <= len(value["node"]) <= 256
            or any(c in value["node"] for c in ("\0", "\n", "/", "\\"))
            or any(type(value.get(k)) is not int or value[k] < 0
                   for k in ("lock_device", "lock_inode"))
            or value.get("lock_path") != str(Path(scope["codex_home"]) /
                "thread-writer-locks" / (scope["thread_id"] + ".lock"))):
        raise NativeInflightError("native_observed_writer_unverified")
    birth = value.get("process_birth")
    patterns = {
        "linux": r"linux:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[1-9][0-9]{0,19}",
        "darwin": r"darwin:[1-9][0-9]{0,19}:(?:0|[1-9][0-9]{0,5})",
        "win32": r"win32:[1-9][0-9]{0,19}",
    }
    if (not isinstance(birth, str)
            or re.fullmatch(patterns[value["platform"]], birth) is None
            or value["platform"] == "win32" and int(birth.split(":")[1]) >= 2**64):
        raise NativeInflightError("native_observed_writer_generation_unverified")
    return dict(value)


def _writer_proof(value, scope, current):
    if (not isinstance(value, dict) or any(value.get(k) != v for k,v in scope.items())
            or not _number(value.get("observed_at")) or not _number(current)
            or not 0 <= current-value["observed_at"] <= PROOF_SECONDS
            or "owner" in value):
        raise NativeInflightError("native_observed_writer_unverified")
    return validate_writer(value.get("writer"), scope)


class ObservedNativeStreamWork(NativeStreamWork):
    """Existing bounded reducer, with an explicitly observed process subject.

    ``current_writer`` supplies fresh before/after real resource+birth evidence
    under ``fence``. First observation, restart, writer replacement and gaps are
    unknown. Only a continuously observed start/input/terminal proves a stream
    lifecycle; it is never an RPC idle response or ownership capability.
    """
    _kind = KIND
    _proof_kind = PROOF_KIND
    _owner_proof = staticmethod(_writer_proof)

    def observe(self, thread_id, repo_path, *, current_writer, fence):
        return super().observe(thread_id, repo_path, current_owner=current_writer, fence=fence)


def validate_observed_work(proof, scope, current):
    return _validate_stream_work(proof, scope, current, kind=KIND,
        proof_kind=PROOF_KIND, owner_validator=lambda value: validate_writer(value, scope))
