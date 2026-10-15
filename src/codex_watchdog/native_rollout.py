"""Read one current native VS Code thread; never discover by walking history."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
import time
from typing import Optional
import uuid


# These are the state_5 fields already used by native workspace discovery and
# linux_binding.exact_thread. Do not infer older/newer schemas or scan rollouts.
_QUERY = """SELECT
    CASE WHEN length(CAST(id AS BLOB)) = 36 THEN id END,
    CASE WHEN length(CAST(cwd AS BLOB)) <= 4096 THEN cwd END,
    CASE WHEN length(CAST(source AS BLOB)) <= 32 THEN source END,
    CASE WHEN length(CAST(thread_source AS BLOB)) <= 32 THEN thread_source END,
    archived,
    CASE WHEN length(CAST(rollout_path AS BLOB)) <= 4096 THEN rollout_path END
    FROM threads WHERE id = ? LIMIT 2"""


def _regular(path: Path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or path.resolve(strict=True) != path:
        raise ValueError("native_rollout_aliased_or_nonregular_file")
    return value


def _native_path(raw: str) -> Path:
    """Accept standard Windows extended anchors, preserving physical identity.

    Normalize only the spelling, before the existing containment/alias checks.
    Device namespaces and traversal never gain a resolution fallback.
    """
    path = Path(raw)
    if not isinstance(path, PureWindowsPath):
        return path
    if ".." in path.parts or path.drive.startswith("\\\\.\\"):
        raise ValueError("native_rollout_unsafe_windows_path")
    if not path.drive.startswith("\\\\?\\"):
        return path
    spelling = str(path)
    if spelling[:8].lower() == "\\\\?\\unc\\":
        path = Path("\\\\?\\UNC\\" + spelling[8:])
    drive = re.fullmatch(r"\\\\\?\\([A-Za-z]:)", path.drive)
    unc = re.fullmatch(r'\\\\\?\\UNC\\([^\\/:<>"|?*\x00-\x1f]+)\\([^\\/:<>"|?*\x00-\x1f]+)',
                       path.drive, flags=re.IGNORECASE)
    if path.root != "\\" or (drive is None and unc is None):
        raise ValueError("native_rollout_unsafe_windows_namespace")
    anchor = drive[1] + "\\" if drive else "\\\\" + unc[1] + "\\" + unc[2] + "\\"
    normalized = Path(anchor, *path.parts[1:])
    if not normalized.samefile(Path(raw)):
        raise ValueError("native_rollout_windows_identity_changed")
    return normalized


def find_native_rollout(
    codex_home: Path, thread_id: str, repo_path: Optional[Path] = None,
) -> Optional[Path]:
    """Return only an indexed, unarchived exact native user-thread path.

    Callers with a workspace must supply its repository. The queue-only courier
    has an exact native UUID but no workspace argument; its optional evidence
    uses the native row's existing cwd. This lookup grants no dispatch authority.
    Missing/unsupported/corrupt evidence returns None without a history fallback.
    No positive or negative cache can hide a changed profile or archived thread.
    """
    try:
        if not isinstance(thread_id, str) or str(uuid.UUID(thread_id)) != thread_id:
            return None
        home = Path(codex_home).expanduser().resolve(strict=True)
        database = home / "state_5.sqlite"
        before = _regular(database)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(database) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _regular(sidecar)
        started = time.monotonic()
        steps = 0

        def budget():
            nonlocal steps
            steps += 1000
            return steps > 100000 or time.monotonic() - started > 0.25

        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                                     timeout=0.1)) as db:
            db.set_progress_handler(budget, 1000)
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
            if db.execute("SELECT type FROM sqlite_master WHERE name='threads'").fetchmany(2) != [("table",)]:
                return None
            plan = db.execute("EXPLAIN QUERY PLAN " + _QUERY, (thread_id,)).fetchmany(8)
            # A compatible native index is required before any thread rows are
            # visited. LIMIT alone would still scan H rows on a missing UUID.
            if len(plan) != 1 or not plan[0][3].startswith(("SEARCH threads ", "SEARCH TABLE threads ")) or "(id=?)" not in plan[0][3]:
                return None
            rows = db.execute(_QUERY, (thread_id,)).fetchmany(2)
        after = _regular(database)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or len(rows) != 1:
            return None
        stored_id, cwd, source, thread_source, archived, raw_path = rows[0]
        if stored_id != thread_id or source != "vscode" or thread_source != "user" or type(archived) is not int or archived != 0:
            return None
        if not isinstance(cwd, str) or not isinstance(raw_path, str):
            return None
        repository, path = _native_path(cwd), _native_path(raw_path)
        if not repository.is_absolute() or not repository.is_dir():
            return None
        if repo_path is not None and repository.resolve(strict=True) != Path(repo_path).resolve(strict=True):
            return None
        sessions = home / "sessions"
        if not sessions.is_dir() or sessions.resolve(strict=True) != sessions:
            return None
        if not path.is_absolute() or ".." in path.parts:
            return None
        path.relative_to(sessions)
        if not path.name.startswith("rollout-") or not path.name.endswith("-" + thread_id + ".jsonl"):
            return None
        _regular(path)
        return path
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error):
        return None
