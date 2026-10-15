"""Read bounded, exact native evidence; a stable writer is not user activity.

An incomplete tail is deliberately an unknown idle sensor. WatchDog can keep
observing a long turn, but must not infer 72 hours of inactivity from missing
history or refresh activity from its own queued messages.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import hashlib
import math
import re
from pathlib import Path
import sqlite3
import uuid

from .native_rollout import find_native_rollout
from .storage import FileLock, InstructionStore

TAIL_BYTES = 256 * 1024
TAIL_RECORDS = 512


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _valid_cursor(cursor):
    """Optional local evidence must be coherent before it can prove quiet time."""
    if not isinstance(cursor, dict):
        return False
    checked = cursor.get("checked_at")
    if (not _number(checked) or not _number(cursor.get("observed_since"))
            or cursor["observed_since"] > checked
            or type(cursor.get("offset")) is not int or cursor["offset"] < 0
            or not isinstance(cursor.get("path"), str) or len(cursor["path"]) > 4096
            or re.fullmatch(r"[0-9a-f]{64}", str(cursor.get("boundary_sha256"))) is None
            or type(cursor.get("activity_known")) is not bool):
        return False
    genuine = cursor.get("genuine_at")
    if genuine is not None and (not _number(genuine) or genuine > checked):
        return False
    last = cursor.get("lifecycle")
    if last is not None and (not isinstance(last, list) or len(last) != 3
            or last[0] not in ("task_started", "task_complete", "turn_aborted")
            or _turn(last[1]) is None or not _number(last[2]) or last[2] > checked):
        return False
    human = cursor.get("human_turn")
    return human is None or _turn(human) is not None


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        value = re.sub(r"(\.\d{6})\d+(?=Z|[+-]\d\d:\d\d$)",r"\1",value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None


def _turn(value):
    try:
        return value if str(uuid.UUID(value)) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _user_text(payload):
    if payload.get("type") == "user_message":
        return payload.get("message")
    if payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "UserMessage":
        return None
    parts = item.get("content")
    if (not isinstance(parts, list) or not parts
            or any(not isinstance(part, dict) or part.get("type") != "text"
                   or not isinstance(part.get("text"), str) for part in parts)):
        return None
    return "\n".join(part["text"] for part in parts)


def native_activity(codex_home, workspace, *, queue_database=None, cursor=None, observed_at=None):
    """Return evidence only, never enqueue, wake, adopt or modify native state.

    Caller must already have verified the current exact native owner. Neither
    this DB lookup nor a rollout's mtime proves an owner or a fresh user action.
    """
    home = Path(codex_home).resolve()
    result = dict(genuine_at=None, turn_active=None, queue_active=None,
                  exact=False, complete_tail=False, reason=None, cursor=None)
    try:
        path = find_native_rollout(home,workspace.session_id,workspace.repo_root)
        if path is None:
            raise ValueError("native_activity_exact_thread_unavailable")
        current = datetime.now(timezone.utc).timestamp() if observed_at is None else observed_at
        if not _number(current):
            raise ValueError("native_activity_clock_invalid")
        cursor = cursor if _valid_cursor(cursor) else None
        if cursor is not None:
            current = max(current, cursor["checked_at"])
        birth, history_known = None, False
        try:
            with closing(sqlite3.connect((home/"state_5.sqlite").as_uri()+"?mode=ro",uri=True,timeout=0.25)) as db:
                row=db.execute("SELECT created_at,history_mode FROM threads WHERE id=?",(workspace.session_id,)).fetchone()
            if row is not None:
                birth=row[0] if type(row[0]) in (int,float) and 0<=row[0]<=current else None
                history_known=row[1] in ("legacy","paginated")
        except sqlite3.Error:
            pass
        continuous=False
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - TAIL_BYTES)
            if isinstance(cursor,dict) and cursor.get("path")==str(path):
                offset=cursor.get("offset")
                if type(offset) is int and 0<=offset<=size and size-offset<=TAIL_BYTES:
                    stream.seek(max(0,offset-4096))
                    boundary=stream.read(min(offset,4096))
                    if hashlib.sha256(boundary).hexdigest()==cursor.get("boundary_sha256"):
                        start=offset
                        continuous=True
            stream.seek(start)
            raw = stream.read(TAIL_BYTES)
            stream.seek(max(0,size-4096))
            end_hash=hashlib.sha256(stream.read(min(size,4096))).hexdigest()
        read_start=start
        if start and not continuous:
            split = raw.find(b"\n")
            raw = raw[split + 1:] if split >= 0 else b""
            read_start=start+split+1 if split>=0 else size
        lines = raw.splitlines(keepends=True)
        complete = (start == 0 or continuous) and len(lines) <= TAIL_RECORDS
        ended_cleanly=True
        if lines and not lines[-1].endswith(b"\n"):
            lines.pop()
            complete = False
            ended_cleanly=False
        committed_offset=read_start+sum(len(line) for line in lines)
        if len(lines) > TAIL_RECORDS:
            lines = lines[-TAIL_RECORDS:]
        genuine = []
        if continuous and cursor.get("activity_known") and cursor.get("genuine_at") is not None:
            genuine.append(cursor["genuine_at"])
        human_turns, watchdog_turns = set(), set()
        if continuous and cursor.get("human_turn"):
            human_turns.add(cursor["human_turn"])
        lifecycle = []
        if continuous and isinstance(cursor.get("lifecycle"),list) and len(cursor["lifecycle"])==3:
            lifecycle.append(tuple(cursor["lifecycle"]))
        invalid = False
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                invalid = True
                continue
            if not isinstance(event, dict):
                invalid = True
                continue
            if event.get("type") == "session_meta":
                meta = event.get("payload")
                if (not isinstance(meta, dict) or meta.get("id") != workspace.session_id
                        or Path(meta.get("cwd", "")).resolve() != workspace.repo_root):
                    raise ValueError("native_activity_identity_mismatch")
                continue
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                invalid = True
                continue
            stamp = _stamp(event.get("timestamp"))
            if stamp is not None and (stamp < 0 or stamp > current):
                stamp = None
            turn = _turn(payload.get("turn_id"))
            text = _user_text(payload)
            if isinstance(text, str) and text.strip() and stamp is not None:
                routed_user = re.match(r"\[CODEX_WATCHDOG_WAKE id=(?:slack|slackbot|lark|onebot):[^\s]+ sha256=[0-9a-f]{64}\]",text) is not None
                if "[CODEX_WATCHDOG_WAKE " in text and not routed_user:
                    if turn is not None:
                        watchdog_turns.add(turn)
                else:
                    genuine.append(stamp)
                    if turn is not None:
                        human_turns.add(turn)
            if payload.get("type") in ("task_started", "task_complete", "turn_aborted"):
                if turn is not None and stamp is not None:
                    lifecycle.append((payload["type"], turn, stamp))
        for kind, turn, stamp in lifecycle:
            if kind == "task_complete" and turn in human_turns and turn not in watchdog_turns:
                genuine.append(stamp)
        # A partial/malformed history cannot prove quiet time. Positive fresh
        # input and positive live-task evidence remain independently useful.
        coverage=(continuous or start==0) and complete and not invalid
        known=bool(genuine) and ended_cleanly and not invalid
        since=cursor.get("observed_since",current) if continuous and coverage else current
        # Continuous quiet coverage is distinct from activity. It never assigns
        # a new activity time: the immutable actual native creation remains the
        # baseline when no subsequent user event can be recovered.
        if not known and coverage and history_known and birth is not None and current-since>=72*60*60:
            genuine.append(birth)
            known=True
        if not genuine and birth is not None:
            genuine.append(birth)
        result.update(exact=True, complete_tail=complete and not invalid,
                      genuine_at=max(genuine) if genuine else None)
        if lifecycle:
            last_kind, _, _ = lifecycle[-1]
            if last_kind == "task_started":
                result["turn_active"] = True
            elif history_known and ended_cleanly and not invalid and known:
                result["turn_active"] = False
        elif history_known and start==0 and result["complete_tail"] and known:
            result["turn_active"] = False
        last=lifecycle[-1] if lifecycle else None
        human=last[1] if last and last[1] in human_turns and last[1] not in watchdog_turns else None
        if committed_offset!=size:
            with path.open('rb') as stream:
                stream.seek(max(0,committed_offset-4096))
                end_hash=hashlib.sha256(stream.read(min(committed_offset,4096))).hexdigest()
        result["cursor"]=dict(path=str(path),offset=committed_offset,boundary_sha256=end_hash,
            observed_since=since,checked_at=current,activity_known=known,genuine_at=result["genuine_at"],
            lifecycle=list(last) if last else None,human_turn=human)
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        result["reason"] = "native_activity_evidence_unavailable"
        return result
    # Only the supported exact queue is consulted; no recursive native-history
    # discovery or old courier scanning. An unknown queue protects monitoring.
    database = Path(queue_database) if queue_database is not None else home / "queue_1.sqlite"
    try:
        if database.parent.resolve() != home or database.is_symlink():
            raise ValueError()
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=0.25)) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute("SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1",
                             (workspace.session_id,)).fetchone()
        result["queue_active"] = row is not None
    except (OSError, ValueError, sqlite3.Error):
        result["reason"] = "native_activity_queue_unknown"
    return result


class NativeActivityTracker:
    """One durable current cursor per UUID; bounded incremental native reads."""
    def __init__(self,runtime,codex_home,*,clock=None):
        self.runtime=Path(runtime).resolve()
        self.codex_home=Path(codex_home).resolve()
        self.clock=clock if clock is not None else lambda:datetime.now(timezone.utc).timestamp()

    def observe(self,workspace,*,queue_database=None):
        thread=str(uuid.UUID(workspace.session_id))
        path=self.runtime/"activity-cursors"/(thread+".json")
        if path.is_symlink() or path.parent.resolve()!=path.parent:
            raise ValueError("native_activity_cursor_redirected")
        lock=self.runtime/"locks"/("activity-cursor-"+thread+".lock")
        with FileLock(lock):
            prior=None
            try:
                with path.open("rb") as stream:raw=stream.read(16385)
                state=json.loads(raw) if len(raw)<=16384 else None
                if isinstance(state,dict) and state.get("schema_version")==1 and state.get("thread_id")==thread:
                    prior=state.get("cursor")
            except (OSError,ValueError):
                pass  # An old optional cursor cannot stall independently fresh evidence.
            result=native_activity(self.codex_home,workspace,queue_database=queue_database,
                                   cursor=prior,observed_at=self.clock())
            if result["exact"] and result["cursor"] is not None:
                InstructionStore._atomic_json(path,dict(schema_version=1,thread_id=thread,cursor=result["cursor"]))
            return result
