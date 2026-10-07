"""Opt-in continuation of an interrupted turn after exact Linux writer acquisition."""

from contextlib import closing
import json
import sqlite3
import time
import uuid

from .control_context import current_control, effect_guard
from .app_server import AppServerError
from .linux_binding import LinuxBindingError, read_json
from .models import sha256_text, utc_now
from .notifications import NotificationEvent, notification_workspace_label
from .remote_control import notify_local_control
from .storage import FileLock, InstructionStore


CONTINUATION_PROMPT = (
    "The user enabled automatic continuation after interruption. The Linux WatchDog "
    "has acquired this same conversation after its previous owner released it. "
    "Continue the existing task from where it stopped. Inspect the current state "
    "before repeating any operation whose outcome is uncertain. Preserve the user's "
    "scope, instructions and permission requirements. If the task is already complete "
    "or needs a user decision or approval, say so and stop. Do not invent new work."
)


class LinuxContinuation:
    def __init__(self, binding, dispatcher, notifier):
        self.binding = binding
        self.dispatcher = dispatcher
        self.notifier = notifier
        self.path = binding.runtime / "linux" / "continuation.json"
        self.lock = binding.runtime / "locks" / "linux-continuation.lock"

    @staticmethod
    def _instruction(thread, turn):
        return "linux-continue:" + thread + ":" + turn

    def _read(self, thread):
        if not self.path.exists():
            return None
        value = read_json(self.path)
        try:
            turn = str(uuid.UUID(value["interrupted_turn_id"]))
            if (value["thread_sha256"] != sha256_text(thread)
                    or value["instruction_id"] != self._instruction(thread, turn)
                    or value["status"] not in (
                "prepared", "enqueued", "consumed_or_started", "started", "uncertain", "dispatching",
                    ) or not isinstance(value.get("notifications", {}), dict)
                    or any(not isinstance(notice, dict) for notice in value.get("notifications", {}).values())):
                raise ValueError()
            attempt = value.get("native_queue_start")
            if attempt is not None:
                if (not isinstance(attempt, dict) or attempt.get("state") not in (
                        "uncertain", "requested", "observed")
                        or str(uuid.UUID(attempt["queue_message_id"])) != attempt["queue_message_id"]):
                    raise ValueError()
                if attempt["state"] == "requested":
                    if str(uuid.UUID(attempt["response_turn_id"])) != attempt["response_turn_id"]:
                        raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise LinuxBindingError("linux_continuation_state_invalid") from exc
        return value

    def _save(self, value):
        InstructionStore._atomic_json(self.path, value)

    @staticmethod
    def _summary(value):
        if value is None:
            return {"status": "watching"}
        result = {"status": value["status"],
                  "interrupted_turn_sha256": sha256_text(value["interrupted_turn_id"])}
        if value.get("attention"):
            result["attention"] = value["attention"]
        if value.get("native_queue_start"):
            result["native_queue_start"] = value["native_queue_start"]["state"]
        notices = value.get("notifications", {})
        if notices:
            result["notifications"] = {key: notice.get("status", "uncertain")
                                       for key, notice in notices.items()}
        return result

    def _notify(self, value, workspace, kind):
        notices = value.setdefault("notifications", {})
        if kind in notices:
            return  # Even an interrupted/uncertain external send is never repeated.
        label = notification_workspace_label(workspace.workspace_id, workspace.repo_root)
        started = kind == "started"
        message = (
            f"WatchDog automatically continued interrupted work in {label}. "
            f"The continuation has started in the same Codex thread {workspace.session_id}. "
            if started else
            f"WatchDog automatic continuation needs attention in {label}, Codex thread "
            f"{workspace.session_id}. " + (
                "The continuation itself was interrupted; it will not be retried in a loop."
                if kind == "interrupted" else
                "Its delivery outcome is uncertain; WatchDog will not send a duplicate continuation."
            )
        )
        event = NotificationEvent(
            workspace_id=workspace.workspace_id,
            event_type="linux_continuation_" + kind,
            transition_fingerprint=sha256_text(value["instruction_id"]),
            subject=f"[Codex Watchdog] {label} automatic continuation " + ("started" if started else "needs attention"),
            message=message,
        )
        selected = current_control()
        if selected is not None:
            # The canonical notification ledger retains an uncertain send across owners.
            result = notify_local_control(*selected, event, self.notifier)
        else:
            with effect_guard(self.binding.codex_home, workspace.session_id, "notification"):
                binding = self.binding.load()
                if binding["state"] != "armed" or binding["expires_at"] <= time.time():
                    return
                notices[kind] = {"status": "uncertain"}
                self._save(value)
                result = self.notifier.notify(event).to_dict()
        notices[kind] = result
        self._save(value)

    def _latest(self, owner):
        result = owner.client.request("thread/turns/list", {
            "threadId": owner.thread, "limit": 1,
            "sortDirection": "desc", "itemsView": "notLoaded",
        })
        turns = result.get("data")
        if not isinstance(turns, list) or len(turns) > 1:
            raise LinuxBindingError("linux_continuation_turn_unverified")
        if not turns:
            return None
        turn = turns[0]
        try:
            identifier = str(uuid.UUID(turn["id"]))
            status = turn["status"]
            if status not in ("completed", "interrupted", "failed", "inProgress"):
                raise ValueError()
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise LinuxBindingError("linux_continuation_turn_unverified") from exc
        return identifier, status

    def _observe_receipt(self, value, thread):
        if value is None:
            return
        record = self.dispatcher.records / (sha256_text(value["instruction_id"]) + ".json")
        if record.exists():
            receipt = self.dispatcher.observe_delivery(value["instruction_id"])
            if receipt.thread_id != thread or receipt.status not in (
                "enqueued", "consumed_or_started", "started", "uncertain", "dispatching",
            ):
                raise LinuxBindingError("linux_continuation_receipt_invalid")
            value["status"] = receipt.status
            if receipt.status == "started":
                queued = json.loads(record.read_text(encoding="utf-8"))
                try:
                    value["continuation_turn_id"] = str(uuid.UUID(queued["started_turn_id"]))
                except (KeyError, ValueError, TypeError, AttributeError) as exc:
                    raise LinuxBindingError("linux_continuation_start_unverified") from exc
                if value.get("native_queue_start"):
                    value["native_queue_start"]["state"] = "observed"
                    if value.get("attention") == "continuation_queue_start_uncertain":
                        value.pop("attention")
            self._save(value)
        elif value["status"] != "prepared":
            raise LinuxBindingError("linux_continuation_receipt_missing")

    def reconcile(self, owner):
        """Observe an existing receipt under the owner fence without a native client.

        Parked monitoring must retain its parked state. Receipt evidence cannot
        authorize a new continuation or an external notification here.
        """
        with FileLock(self.lock):
            value = self._read(owner.thread)
            self._observe_receipt(value, owner.thread)
            return self._summary(value)

    def notify_pending(self, owner, workspace):
        # Canonical notification preparation owns its own control lock. Never
        # call it while the writer step still holds that lock.
        from .linux_owner import writer_pid
        with FileLock(self.lock):
            value = self._read(owner.thread)
            if value is None:
                return self._summary(value)
            binding = self.binding.load()
            if (owner.release_requested or binding["state"] != "armed"
                    or binding["expires_at"] <= time.time()
                    or writer_pid(self.binding.codex_home, owner.thread) != owner.client.process.pid):
                return self._summary(value)
            if value["status"] == "started":
                self._notify(value, workspace, "started")
            if value.get("attention") == "continuation_interrupted":
                self._notify(value, workspace, "interrupted")
            if (value["status"] in ("uncertain", "dispatching")
                    or value.get("native_queue_start", {}).get("state") == "uncertain"):
                self._notify(value, workspace, "uncertain")
            return self._summary(value)

    def _start_enqueued(self, owner, workspace, value):
        """Start the existing head item after an interrupted native turn.

        Recent native queue watchers intentionally leave interrupted threads
        stopped. thread/queue/start atomically starts an existing item only if
        idle, retaining its client ID and deleting it through the native queue.
        Never turn queue contents into a new prompt or repeat an uncertain RPC.
        Called under the existing writer admission fence and continuation lock.
        """
        from .linux_owner import writer_pid

        if (value.get("native_queue_start") is not None
                or owner.thread_status != "idle" or owner.approval_required
                or self._latest(owner) != (value["interrupted_turn_id"], "interrupted")):
            return
        record_path = self.dispatcher.records / (sha256_text(value["instruction_id"]) + ".json")
        with record_path.open("rb") as handle:
            raw = handle.read(65537)
        try:
            record = json.loads(raw) if len(raw) <= 65536 else None
        except (TypeError, ValueError) as exc:
            raise LinuxBindingError("linux_continuation_queue_start_unverified") from exc
        if not isinstance(record, dict) or record.get("schema_version") != 2:
            raise LinuxBindingError("linux_continuation_queue_start_unverified")
        if (record.get("thread_id") != owner.thread
                or record.get("instruction_id") != value["instruction_id"]
                or record.get("state") != "enqueued"
                or record.get("prompt_sha256") != sha256_text(CONTINUATION_PROMPT)):
            raise LinuxBindingError("linux_continuation_queue_start_unverified")
        try:
            queue_id = str(uuid.UUID(record["queue_message_id"]))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise LinuxBindingError("linux_continuation_queue_start_unverified") from exc
        database = self.dispatcher._select_queue_database()
        if database is None:
            return
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(queued_items)")}
            if "queue_order" not in columns:
                return  # An older/unknown queue cannot prove FIFO targeting.
            head = db.execute("SELECT id,payload_json FROM queued_items WHERE thread_id=? "
                              "ORDER BY queue_order LIMIT 1", (owner.thread,)).fetchone()
        if head is None or head[0] != queue_id:
            return  # Never bypass a human or another admitted item ahead of us.
        try:
            payload = json.loads(head[1])
        except (TypeError, ValueError) as exc:
            raise LinuxBindingError("linux_continuation_queue_start_unverified") from exc
        expected = (f"[CODEX_WATCHDOG_WAKE id={value['instruction_id']} "
                    f"sha256={record['prompt_sha256']}]\n" + CONTINUATION_PROMPT)
        user_input = payload.get("UserInput") if isinstance(payload, dict) else None
        content = user_input.get("content") if isinstance(user_input, dict) else None
        if (not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict)
                or content[0].get("type") != "text" or content[0].get("text") != expected):
            raise LinuxBindingError("linux_continuation_queue_start_unverified")
        current = owner.client.request("thread/read", {"threadId": owner.thread, "includeTurns": False})
        owner._check_thread(current, workspace)
        binding = self.binding.load()
        if (owner.release_requested or owner.yield_requested or owner.approval_required
                or binding["state"] != "armed" or binding["expires_at"] <= time.time()
                or owner.thread_status != "idle" or current["thread"].get("status", {}).get("type") != "idle"
                or writer_pid(self.binding.codex_home, owner.thread) != owner.client.process.pid
                or self._latest(owner) != (value["interrupted_turn_id"], "interrupted")):
            return
        binding = self.binding.load()
        if (owner.release_requested or owner.yield_requested or owner.approval_required
                or owner.thread_status != "idle" or binding["state"] != "armed"
                or binding["expires_at"] <= time.time()
                or writer_pid(self.binding.codex_home, owner.thread) != owner.client.process.pid):
            return
        # Persist before the RPC: even a crash or lost reply cannot dispatch it twice.
        attempt = {"state": "uncertain", "queue_message_id": queue_id, "created_at": utc_now()}
        value["native_queue_start"] = attempt
        self._save(value)
        try:
            result = owner.client.request("thread/queue/start", {
                "threadId": owner.thread, "queuedSubmissionId": queue_id,
            })
            turn = result["turn"]
            identifier = str(uuid.UUID(turn["id"]))
            if turn["status"] != "inProgress":
                raise ValueError()
        except (AppServerError, KeyError, ValueError, TypeError, AttributeError):
            value["attention"] = "continuation_queue_start_uncertain"
            self._save(value)
            return
        attempt.update(state="requested", response_turn_id=identifier)
        self._save(value)
        # The ordinary queue/rollout receipt still establishes actual admission.

    def step(self, owner, workspace):
        # Called under the owner's existing control fence and lifetime locks.
        from .linux_owner import pending_count, writer_pid

        with FileLock(self.lock):
            value = self._read(owner.thread)
            self._observe_receipt(value, owner.thread)
            if value is not None and value["status"] == "enqueued":
                self._start_enqueued(owner, workspace, value)
                return self._summary(value)
            if value is not None and value["status"] not in ("prepared", "started"):
                return self._summary(value)

            if owner.thread_status != "idle" or owner.approval_required:
                return self._summary(value)
            latest = self._latest(owner)
            if latest is None or latest[1] != "interrupted":
                return self._summary(value)
            turn = latest[0]
            if value is not None:
                if value.get("continuation_turn_id") == turn:
                    value["attention"] = "continuation_interrupted"
                    self._save(value)
                    return self._summary(value)
                if value["interrupted_turn_id"] == turn and value["status"] != "prepared":
                    return self._summary(value)

            current = owner.client.request("thread/read", {"threadId": owner.thread, "includeTurns": False})
            owner._check_thread(current, workspace)
            status = current["thread"].get("status")
            if not isinstance(status, dict):
                raise LinuxBindingError("linux_continuation_status_unverified")
            binding = self.binding.load()
            if (owner.release_requested or owner.approval_required or binding["state"] != "armed"
                    or binding["expires_at"] <= time.time() or owner.thread_status != "idle"
                    or status.get("type") != "idle"
                    or writer_pid(self.binding.codex_home, owner.thread) != owner.client.process.pid
                    or pending_count(self.binding.codex_home, owner.thread) != 0):
                return self._summary(value)
            # Recheck after the live status round-trip: a queued user turn may have started.
            if self._latest(owner) != latest:
                return self._summary(value)
            binding = self.binding.load()
            if (owner.release_requested or owner.approval_required or owner.thread_status != "idle"
                    or binding["state"] != "armed" or binding["expires_at"] <= time.time()
                    or writer_pid(self.binding.codex_home, owner.thread) != owner.client.process.pid
                    or pending_count(self.binding.codex_home, owner.thread) != 0):
                return self._summary(value)
            if value is None or value["interrupted_turn_id"] != turn:
                value = {"schema_version": 1, "thread_sha256": sha256_text(owner.thread),
                         "interrupted_turn_id": turn, "instruction_id": self._instruction(owner.thread, turn),
                         "status": "prepared", "created_at": utc_now(), "notifications": {}}
                self._save(value)
            receipt = self.dispatcher.dispatch(owner.thread, value["instruction_id"],
                                               CONTINUATION_PROMPT, "linux_interruption")
            if receipt.status != "rejected":
                value["status"] = receipt.status
                self._save(value)
            # A rejection before queue admission can safely retry after revalidation.
            # Confirm the native start on a subsequent check, never from an enqueue ACK.
            return self._summary(value)
