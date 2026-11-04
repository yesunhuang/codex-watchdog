"""Start one already-admitted automatic Git head after native interruption.

No enqueue, prompt, historical search, or retry of an ambiguous native start.
The caller keeps the existing Linux owner and its exact writer/queue fence.
"""
from contextlib import closing, ExitStack
from itertools import islice
import re
import sqlite3
import time
import uuid

from .app_server import AppServerError
from .control_context import current_control, current_effect
from .control_state import (
    ControlError, control_courier_identity, control_courier_path,
    control_courier_payload, control_courier_receipt, control_git_retirement_lock,
    control_git_retirement_read,
    control_node_name, control_read_json,
)
from .models import sha256_text, utc_now
from .queue_wake import REMOTE_UPDATE_PROMPT
from .storage import FileLock, InstructionStore


class LinuxGitQueueStart:
    def __init__(self, binding):
        self.binding = binding
        self.root = binding.runtime / "linux" / "git-queue-starts"
        self.lock = binding.runtime / "locks" / "linux-git-queue-start.lock"

    def _head(self, thread):
        paths = list(islice(self.binding.codex_home.glob("queue_*.sqlite"), 2))
        if len(paths) != 1:
            return None
        path = paths[0]
        if path.is_symlink() or path.resolve() != path or not path.is_file():
            return None
        query = ("SELECT id,CASE WHEN typeof(payload_json)='text' AND "
                 "length(CAST(payload_json AS BLOB))<=131072 THEN payload_json END "
                 "FROM queued_items WHERE thread_id=? ORDER BY queue_order LIMIT 1")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: 1, 200000)
            if db.execute("SELECT type FROM sqlite_master WHERE name='queued_items' LIMIT 2").fetchall() != [("table",)]:
                return None
            plan = db.execute("EXPLAIN QUERY PLAN " + query, (thread,)).fetchmany(9)
            if (len(plan) > 8 or not any("SEARCH" in row[3] and "thread_id=?" in row[3] for row in plan)
                    or any("SCAN" in row[3] or "TEMP B-TREE" in row[3] for row in plan)):
                return None
            return db.execute(query, (thread,)).fetchone()

    def _candidate(self, workspace):
        thread = workspace.session_id
        head = self._head(thread)
        if head is None or str(uuid.UUID(head[0])) != head[0]:
            return None
        parsed = control_courier_payload(head[1])
        prefix = "git:" + sha256_text(workspace.workspace_id)[:16] + ":"
        if (parsed is None or not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{7,64}", parsed[0])
                or parsed[1] != sha256_text(REMOTE_UPDATE_PROMPT)):
            return None
        path = control_courier_path(self.binding.codex_home, thread, head[0])
        entry = control_read_json(path)
        receipt = control_courier_receipt(self.binding.codex_home, parsed[0])
        identity = control_courier_identity(receipt)
        if (type(entry.get("schema_version")) is not int or entry.get("schema_version") != 1
                or entry.get("kind") != "current_courier"
                or entry.get("node") != control_node_name() or receipt.get("state") != "enqueued"
                or identity["thread_id"] != thread or identity["queue_message_id"] != head[0]
                or parsed != (identity["instruction_id"], identity["prompt_sha256"])
                or any(entry.get(k) != v for k, v in identity.items())
                or control_git_retirement_read(self.binding.codex_home, parsed[0], thread,
                                               prompt_sha256=parsed[1]) is not None):
            return None
        return dict(identity, repo_path=str(workspace.repo_root))

    def _eligible_owner(self, owner, workspace):
        selected = current_control()
        if (selected is None or not owner.node_local or not owner.resumed
                or owner.client is None or owner.thread != workspace.session_id
                or owner.thread_status != "idle" or owner.approval_required
                or owner.release_requested or owner.yield_requested):
            return False
        store, token = selected
        if (store.codex_home != self.binding.codex_home or store.thread_id != workspace.session_id
                or store.repo_path != str(workspace.repo_root) or token.get("role") != "remote"):
            return False
        store._check(store.read(), token)
        value = self.binding.load()
        return value["state"] == "armed" and value["expires_at"] > time.time()

    def step(self, owner, workspace, latest):
        if not self._eligible_owner(owner, workspace):
            return None
        with current_effect("queue"), FileLock(self.lock), ExitStack() as admitted:
            try:
                identity = self._candidate(workspace)
            except (ControlError, OSError, ValueError, TypeError, sqlite3.Error):
                return None  # Only this unsafe current action is deferred.
            if identity is None:
                return None
            try:
                admitted.enter_context(control_git_retirement_lock(
                    self.binding.codex_home, identity["instruction_id"]))
            except ControlError:
                return None
            path = self.root / (identity["queue_message_id"] + ".json")
            if path.is_symlink() or path.resolve() != path:
                return None
            if path.exists():
                try:
                    previous = control_read_json(path)
                except (ControlError, OSError):
                    return dict(status="claimed", queue_message_id=identity["queue_message_id"])
                if any(previous.get(k) != v for k, v in identity.items()):
                    return None
                # A claimed effect is terminal even if native Q still contains it.
                return dict(status="claimed", queue_message_id=identity["queue_message_id"])
            interrupted = latest(owner)
            if interrupted is None or interrupted[1] != "interrupted":
                return None
            proof = owner.native_work.current_native()
            thread = proof["thread_read"]["thread"]
            if (thread.get("id") != workspace.session_id or thread.get("cwd") != str(workspace.repo_root)
                    or thread.get("status", {}).get("type") != "idle"
                    or not self._eligible_owner(owner, workspace)
                    or latest(owner) != interrupted):
                return None
            try:
                if self._candidate(workspace) != identity:
                    return None
            except (ControlError, OSError, ValueError, TypeError, sqlite3.Error):
                return None
            current = owner.native_work.current_owner()
            if current["owner"] != proof["owner"] or not self._eligible_owner(owner, workspace):
                return None
            record = dict(schema_version=1, **identity, state="uncertain", created_at=utc_now(),
                          interrupted_turn_id=interrupted[0], owner=current["owner"])
            InstructionStore._atomic_json(path, record)  # Before the one external attempt.
            try:
                response = owner.client.request("thread/queue/start", {
                    "threadId": workspace.session_id,
                    "queuedSubmissionId": identity["queue_message_id"],
                })
                turn = response["turn"]
                identifier = str(uuid.UUID(turn["id"]))
                if turn["status"] != "inProgress":
                    raise ValueError()
            except (AppServerError, KeyError, ValueError, TypeError, AttributeError):
                return dict(status="uncertain", queue_message_id=identity["queue_message_id"])
            record.update(state="requested", native_turn_id=identifier)
            InstructionStore._atomic_json(path, record)
            return dict(status="requested", queue_message_id=identity["queue_message_id"],
                        native_turn_id=identifier)
