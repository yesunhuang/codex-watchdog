"""Bounded, read-only completion proof for one already admitted native wake.

Only hash-only parsing progress is retained. A queue ACK, removed queue row or
native start is not completion. No history discovery, query or replay occurs.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .models import sha256_text, validate_instruction_id


_FIELDS = ("thread_id", "instruction_id", "prompt_sha256", "rollout_path",
           "rollout_baseline_offset", "queue_message_id")
_ANCHOR = 16


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("native_duplicate_key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("native_nonstandard_number")


def _blocked(state, reason, *, invalid=False):
    state.update(reason=reason, invalid=invalid, bytes_read=state.get("bytes_read", 0))
    return state, False


def _event(state, receipt, event):
    if not isinstance(event, dict):
        raise ValueError("native_event_invalid")
    if event.get("type") != "event_msg":
        return
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("native_event_invalid")
    kind, turn = payload.get("type"), payload.get("turn_id")
    if state["turn_id"] is not None and turn == state["turn_id"]:
        if "thread_id" in payload and payload["thread_id"] != receipt["thread_id"]:
            raise ValueError("native_thread_conflict")
    if kind == "task_complete":
        if state["turn_id"] is not None and turn == state["turn_id"]:
            state["terminal"] = True
        return
    if kind != "item_completed":
        return
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "UserMessage":
        return
    content = item.get("content")
    if (not isinstance(content, list) or len(content) != 1
            or not isinstance(content[0], dict) or content[0].get("type") != "text"
            or not isinstance(content[0].get("text"), str)):
        return
    text = content[0]["text"]
    identity = "[CODEX_WATCHDOG_WAKE id=" + receipt["instruction_id"] + " "
    if not text.startswith(identity):
        return
    marker = identity + "sha256=" + receipt["prompt_sha256"] + "]\n"
    if not text.startswith(marker):
        raise ValueError("native_marker_conflict")
    prompt = text[len(marker):]
    if (sha256_text(prompt) != receipt["prompt_sha256"]
            and not (prompt.endswith("\n") and sha256_text(prompt[:-1]) == receipt["prompt_sha256"])):
        raise ValueError("native_prompt_conflict")
    if payload.get("thread_id") != receipt["thread_id"] or not _uuid(turn):
        raise ValueError("native_message_identity_invalid")
    if state["turn_id"] is not None:
        raise ValueError("native_message_ambiguous")
    state["turn_id"] = turn


def completion_progress(receipt, progress=None, budget=65536):
    """Return (hash-only progress, completed), reading at most budget bytes.

    The immutable receipt binds the exact source and dispatch boundary. Only
    complete JSONL events advance the offset. An unfinished or over-budget
    event remains unread; callers may use a larger explicit bounded budget.
    Completion is reported only after reaching the current fixed snapshot's
    end, so a competing matching turn already in that snapshot cannot hide
    behind the first terminal event.
    """
    state = deepcopy(progress) if isinstance(progress, dict) else {}
    state["bytes_read"] = 0
    try:
        if not isinstance(receipt, dict) or any(field not in receipt for field in _FIELDS):
            raise ValueError("native_receipt_invalid")
        if (not _uuid(receipt["thread_id"]) or not _uuid(receipt["queue_message_id"])
                or not _digest(receipt["prompt_sha256"])
                or not isinstance(receipt["rollout_path"], str)
                or not Path(receipt["rollout_path"]).is_absolute()
                or type(receipt["rollout_baseline_offset"]) is not int
                or receipt["rollout_baseline_offset"] < 0):
            raise ValueError("native_receipt_invalid")
        validate_instruction_id(receipt["instruction_id"])
        fingerprint = sha256_text(json.dumps({field: receipt[field] for field in _FIELDS},
            sort_keys=True, separators=(",", ":"), allow_nan=False))
        if progress is None:
            state.update(schema_version=1, receipt_sha256=fingerprint,
                offset=receipt["rollout_baseline_offset"], inode=None,
                observed_size=receipt["rollout_baseline_offset"], anchor_sha256=None,
                turn_id=None, terminal=False, invalid=False)
        if (type(state.get("schema_version")) is not int or state["schema_version"] != 1
                or state.get("receipt_sha256") != fingerprint
                or type(state.get("offset")) is not int
                or state["offset"] < receipt["rollout_baseline_offset"]
                or type(state.get("observed_size")) is not int
                or state["observed_size"] < state["offset"]
                or (state.get("inode") is not None and type(state["inode"]) is not int)
                or (state.get("anchor_sha256") is not None and not _digest(state["anchor_sha256"]))
                or (state.get("turn_id") is not None and not _uuid(state["turn_id"]))
                or type(state.get("terminal")) is not bool
                or type(state.get("invalid")) is not bool
                or (state["terminal"] and state["turn_id"] is None)):
            raise ValueError("native_progress_invalid")
        if state["invalid"]:
            return state, False
        if type(budget) is not int or budget < _ANCHOR * 2:
            return _blocked(state, "native_budget_invalid")
        path = Path(receipt["rollout_path"])
        with path.open("rb") as stream:
            snapshot = os.fstat(stream.fileno())
            if (snapshot.st_size < state["observed_size"]
                    or state["inode"] not in (None, snapshot.st_ino)):
                raise ValueError("native_rollout_changed")
            offset = state["offset"]
            width = min(_ANCHOR, offset)
            stream.seek(offset - width)
            anchor = stream.read(width)
            state["bytes_read"] += len(anchor)
            if (width and (len(anchor) != width or not anchor.endswith(b"\n"))
                    or state["anchor_sha256"] not in (None, hashlib.sha256(anchor).hexdigest())):
                raise ValueError("native_boundary_changed")
            stream.seek(offset)
            chunk = stream.read(min(budget - state["bytes_read"], snapshot.st_size - offset))
            state["bytes_read"] += len(chunk)
            used = 0
            for line in chunk.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    break
                _event(state, receipt, json.loads(line,
                    object_pairs_hook=_object, parse_constant=_constant))
                used += len(line)
            current = path.stat()
            if current.st_ino != snapshot.st_ino or current.st_size < snapshot.st_size:
                raise ValueError("native_rollout_changed")
            state.update(offset=offset + used, inode=snapshot.st_ino,
                observed_size=snapshot.st_size,
                anchor_sha256=hashlib.sha256((anchor + chunk[:used])[-_ANCHOR:]).hexdigest())
            if state["offset"] != snapshot.st_size:
                return _blocked(state, "native_event_unread")
            state["reason"] = "native_completed" if state["terminal"] else "native_completion_unproven"
            return state, state["terminal"]
    except OSError:
        return _blocked(state, "native_rollout_unavailable")
    except (ValueError, TypeError, UnicodeError, KeyError, OverflowError, RecursionError):
        return _blocked(state, "native_completion_invalid", invalid=True)
