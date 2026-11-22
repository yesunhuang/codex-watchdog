"""An immutable activation signal from a newly attached, verified editor writer.

Only the already selected Linux observer calls this. Process birth, rather than
the time of discovery, prevents a stable writer or daemon restart from keeping
an idle conversation active. No process, ownership or native queue is changed.
"""
import math
import os
from pathlib import Path
import sys
import uuid


def _boot_time():
    with Path("/proc/stat").open("rb") as stream:
        raw = stream.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("attachment_boot_time_unavailable")
    values = [line.split() for line in raw.splitlines() if line.startswith(b"btime ")]
    if len(values) != 1 or len(values[0]) != 2 or not values[0][1].isdigit():
        raise ValueError("attachment_boot_time_unavailable")
    return int(values[0][1])


def native_reattachment_at(codex_home, workspace):
    """Return the exact held VS Code writer's fixed birth, or no signal.

    The caller retains the existing selected observer/owner guard. A failed
    optional proof must not stop independent Git or completion observation.
    WatchDog-owned App Server children are deliberately not editor activity.
    """
    if sys.platform != "linux":
        return None
    from .linux_owner import writer_pid, vscode_writer
    from .observed_writer import capture_writer, _linux_birth

    try:
        pid = writer_pid(Path(codex_home), workspace.session_id)
        if pid is None or not vscode_writer(pid):
            return None
        proof = capture_writer(codex_home, workspace.session_id, workspace.repo_root, pid)
        writer = proof["writer"]
        if writer["platform"] != "linux" or writer["pid"] != pid:
            return None
        birth = writer["process_birth"]
        platform, boot, ticks = birth.split(":")
        if platform != "linux" or str(uuid.UUID(boot)) != boot or not ticks.isdigit():
            return None
        hz = os.sysconf("SC_CLK_TCK")
        if type(hz) is not int or not 0 < hz <= 1000000 or int(ticks) <= 0:
            return None
        attached_at = _boot_time() + int(ticks) / hz
        if not math.isfinite(attached_at) or attached_at <= 0:
            return None
        # Recheck the same kernel process generation and exact held resource;
        # PID reuse or writer handoff invalidates only this optional signal.
        if (_linux_birth(pid) != birth or writer_pid(Path(codex_home), workspace.session_id) != pid
                or not vscode_writer(pid)):
            return None
        return attached_at
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
