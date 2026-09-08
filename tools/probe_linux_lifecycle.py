"""Read-only Linux lifecycle evidence; this probe never owns or wakes a thread.

Run beside a disposable VS Code Remote workspace, optionally under tmux or
systemd --user. A unique exact-cwd user thread is pinned for the capture; a
replacement thread is never selected after that. Output contains only counts,
booleans, reason codes, and digests, not paths, IDs, commands, or messages.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, Optional
import uuid


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_database(path: Path, query: str, parameters: tuple = ()) -> list:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
        db.execute("PRAGMA query_only = ON")
        return db.execute(query, parameters).fetchall()


def held_lock(path: Path) -> Optional[bool]:
    import fcntl

    try:
        with path.open("rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                return True if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK) else None
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False
    except OSError:
        return None


def process_inventory(proc_root: Path, lock: Optional[Path]) -> Dict[str, Any]:
    """Read process identity and exact lock FDs; never emit process arguments."""
    processes: Dict[int, dict] = {}
    try:
        boot = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
        entries = list(proc_root.iterdir())
    except OSError:
        return {"status": "unavailable"}
    inaccessible = 0
    for path in entries:
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            args = (path / "cmdline").read_bytes().split(b"\0")
            name = Path(os.fsdecode(args[0])).name
            if name == "codex" and b"app-server" in args:
                kind = "codex_app_server"
            elif b"--type=extensionHost" in args:
                kind = "vscode_extension_host"
            elif any(arg.endswith(b"/server-main.js") for arg in args):
                kind = "vscode_server"
            else:
                continue
            before = (path / "stat").read_text().rsplit(")", 1)[1].split()
            descriptors = []
            for fd in (path / "fd").iterdir():
                try:
                    descriptors.append(os.readlink(fd))
                except FileNotFoundError:
                    continue
            after = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if before[19] != after[19]:
                inaccessible += 1
                continue
            processes[int(path.name)] = {
                "kind": kind,
                "identity_sha256": digest(f"{boot}:{path.name}:{before[19]}"),
                "parent_pid": int(before[1]),
                "holds_target_descriptor": lock is not None and str(lock) in descriptors,
            }
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            inaccessible += 1
    result = []
    for record in processes.values():
        parent = processes.get(record.pop("parent_pid"))
        record["parent_identity_sha256"] = parent["identity_sha256"] if parent else None
        result.append(record)
    return {
        "status": "partial" if inaccessible else "observed",
        "inaccessible_count": inaccessible,
        "processes": sorted(result, key=lambda value: value["identity_sha256"]),
    }


def rollout_metadata(path: Path, codex_home: Path) -> dict:
    """Bound reads to the selected session's tail and discard message contents."""
    try:
        resolved = path.resolve()
        resolved.relative_to((codex_home / "sessions").resolve())
    except (OSError, ValueError):
        return {"status": "outside_sessions"}
    counts = {"task_started": 0, "task_complete": 0, "turn_aborted": 0, "user_message": 0}
    last_turn = None
    last_event = None
    try:
        with resolved.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            offset = max(0, size - 1024 * 1024)
            handle.seek(offset)
            tail = handle.read(size - offset)
            if offset:
                tail = tail.partition(b"\n")[2]
            for line in tail.splitlines():
                try:
                    value = json.loads(line)
                except (UnicodeDecodeError, ValueError):
                    continue
                if not isinstance(value, dict) or value.get("type") != "event_msg":
                    continue
                payload = value.get("payload")
                if not isinstance(payload, dict) or payload.get("type") not in counts:
                    continue
                event = payload["type"]
                counts[event] += 1
                if event != "user_message":
                    last_event = event
                    turn = payload.get("turn_id")
                    last_turn = digest(turn) if isinstance(turn, str) else None
        return {
            "status": "observed", "exists": True, "bytes": size,
            "tail_only": bool(offset), "event_counts": counts,
            "last_turn_event": last_event, "last_turn_sha256": last_turn,
        }
    except OSError:
        return {"status": "unavailable", "exists": False}


class LifecycleProbe:
    def __init__(self, repo: Path, codex_home: Path, *, thread: Optional[str] = None,
                 proc_root: Path = Path("/proc")) -> None:
        self.repo = repo.expanduser().resolve()
        self.codex_home = codex_home.expanduser().resolve()
        self.thread = str(uuid.UUID(thread)) if thread else None
        self.proc_root = proc_root

    def snapshot(self) -> dict:
        result: Dict[str, Any] = {
            "schema_version": 1,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "workspace_sha256": digest(str(self.repo)),
            "target_pinned": self.thread is not None,
            "ownership_established": False,
        }
        lock = None
        try:
            rows = read_database(
                self.codex_home / "state_5.sqlite",
                "SELECT id, rollout_path FROM threads WHERE cwd = ? AND archived = 0 "
                "AND source = 'vscode' AND thread_source = 'user'",
                (str(self.repo),),
            )
            if self.thread is not None:
                rows = [row for row in rows if row[0] == self.thread]
            if len(rows) != 1:
                result["thread_state"] = "ambiguous" if len(rows) > 1 else "unavailable"
            else:
                thread, rollout = rows[0]
                self.thread = str(uuid.UUID(thread))
                result.update(thread_state="present", target_pinned=True,
                              thread_sha256=digest(self.thread))
                lock = self.codex_home / "thread-writer-locks" / f"{self.thread}.lock"
                result["writer_lock_held"] = held_lock(lock)
                result["rollout"] = rollout_metadata(Path(rollout), self.codex_home)
                result["queue"] = self._queue()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            result["thread_state"] = "unavailable"
        if self.thread is not None:
            result["thread_sha256"] = digest(self.thread)
        result["process_inventory"] = process_inventory(self.proc_root, lock)
        return result

    def _queue(self) -> dict:
        compatible = []
        for path in self.codex_home.glob("queue_*.sqlite"):
            try:
                count = read_database(path, "SELECT COUNT(*) FROM queued_items WHERE thread_id = ?", (self.thread,))[0][0]
                revision = read_database(path, "SELECT revision FROM queued_thread_revisions WHERE thread_id = ?", (self.thread,))
                compatible.append({"item_count": count, "revision": revision[0][0] if revision else 0})
            except (OSError, sqlite3.Error):
                continue
        if len(compatible) != 1:
            return {"status": "ambiguous" if compatible else "unavailable"}
        return {"status": "observed", **compatible[0]}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--thread", help="optional exact thread UUID; never retargeted")
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--output", type=Path, help="new JSONL file; existing evidence is never overwritten")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        print(json.dumps({"status": "unavailable", "reason": "linux_required"}))
        return 1
    if not (0 <= args.duration <= 600 and 0.1 <= args.interval <= 30):
        parser.error("duration must be 0..600 and interval 0.1..30 seconds")
    try:
        probe = LifecycleProbe(args.repo, args.codex_home, thread=args.thread)
        output = os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") if args.output else None
    except (OSError, ValueError):
        print(json.dumps({"status": "unavailable", "reason": "probe_setup_failed"}))
        return 1
    deadline = time.monotonic() + args.duration
    try:
        while True:
            line = json.dumps(probe.snapshot(), sort_keys=True)
            if output is not None:
                output.write(line + "\n")
                output.flush()
            else:
                print(line, flush=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
    except KeyboardInterrupt:
        pass
    finally:
        if output is not None:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
