from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from .models import sha256_text, utc_now, validate_instruction_id, validate_prompt
from .storage import InstructionCollisionError, InstructionStore


REMOTE_UPDATE_PROMPT = """You were resumed by WatchDog.

1. Check for a local temporal/resume prompt first.
   - If it is relevant to the current thread/task, treat it as the highest-priority continuation instruction.
   - After it has served its purpose, archive it under `.codex-watchdog/resume/archive/` if it contains information worth retaining for future work; otherwise delete it.
   - If it is clearly unrelated, do not execute it.
   - If relevance is genuinely uncertain, do not delete it; leave it intact and report the ambiguity.

2. Then inspect and synchronize Git safely if the repository changed remotely.
   - WatchDog observes Git but never stages, commits, pulls, merges, or pushes. Inspect any unfinished local work and safely commit or publish it when that is clearly appropriate for the current task.
   - Handle ordinary synchronization and straightforward conflicts autonomously when the correct resolution is clear and consistent with the current task.
   - Do not discard work, hard-reset, force-push, overwrite remote history, delete unknown files, or make an arbitrary conflict choice merely to hide uncertainty.
3. Then inspect the latest progress report for a new unprocessed `## comment`.
4. Continue only actionable work; do not invent new tasks.
5. If a substantive decision is genuinely unclear, stop and report the exact decision needed."""

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_OID = re.compile(r"^[0-9a-fA-F]{7,64}$")
_QUEUE_ACK = re.compile(
    r"^Queued message "
    r"(?P<message_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}) "
    r"for thread "
    r"(?P<thread_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.$"
)
_CODEX_EXTENSION_VERSION = re.compile(
    r"^openai\.chatgpt-(?P<version>\d+(?:\.\d+)+)(?:-|$)", re.IGNORECASE
)


def _extension_version_key(path: Path) -> Tuple[Tuple[int, ...], str]:
    try:
        extension_name = path.parents[2].name
    except IndexError:
        return (), str(path).casefold()
    match = _CODEX_EXTENSION_VERSION.match(extension_name)
    version = (
        tuple(int(part) for part in match.group("version").split("."))
        if match is not None
        else ()
    )
    return version, str(path).casefold()


def _resolve_codex_executable(
    *,
    home: Optional[Path] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    platform_name: Optional[str] = None,
) -> str:
    """Resolve the first-party CLI for shells that do not inherit VS Code's PATH."""
    which_command = shutil.which if which is None else which
    located = which_command("codex")
    if located:
        return str(Path(located).expanduser().resolve())

    selected_home = Path.home() if home is None else Path(home)
    selected_platform = os.name if platform_name is None else platform_name
    executable_name = "codex.exe" if selected_platform == "nt" else "codex"
    roots = (
        selected_home / ".vscode" / "extensions",
        selected_home / ".vscode-insiders" / "extensions",
        selected_home / ".vscode-server" / "extensions",
        selected_home / ".vscode-server-insiders" / "extensions",
    )
    candidates: List[Path] = []
    for root in roots:
        try:
            resolved_root = root.expanduser().resolve()
        except OSError:
            continue
        for candidate in root.glob(f"openai.chatgpt-*/bin/*/{executable_name}"):
            try:
                resolved = candidate.resolve()
                resolved.relative_to(resolved_root)
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            if selected_platform != "nt" and not os.access(resolved, os.X_OK):
                continue
            candidates.append(resolved)
    if candidates:
        return str(max(set(candidates), key=_extension_version_key))
    return "codex"


def _canonical_uuid(value: str, label: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ValueError(f"{label} must be a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be a UUID") from exc


@dataclass(frozen=True)
class QueueReceipt:
    instruction_id: str
    thread_id: str
    status: str
    stdout: str
    stderr: str
    returncode: int
    queue_message_id: Optional[str] = None
    deduplicated: bool = False


class QueueWakeDispatcher:
    """Narrow adapter over the installed, first-party `codex queue` command."""

    def __init__(
        self,
        runtime: Path,
        codex_executable: Optional[str] = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        codex_home: Optional[Path] = None,
        queue_database: Optional[Path] = None,
    ) -> None:
        self.runtime = Path(runtime)
        self.records = self.runtime / "wake" / "records"
        self.resume_inflight = self.runtime / "resume" / "inflight"
        if codex_executable is not None and (
            not isinstance(codex_executable, str) or not codex_executable.strip()
        ):
            raise ValueError("codex executable must be a non-empty string")
        self.codex_executable = (
            codex_executable
            if codex_executable is not None
            else _resolve_codex_executable()
        )
        self.runner = runner
        self.codex_home = (
            Path(
                (
                    Path(codex_home)
                    if codex_home is not None
                    else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
                )
            )
            .expanduser()
            .resolve()
        )
        self.queue_database = (
            Path(queue_database).expanduser().resolve()
            if queue_database is not None
            else None
        )
        if (
            self.queue_database is not None
            and self.queue_database.parent != self.codex_home
        ):
            raise ValueError("queue database must be inside the selected Codex home")
        self.store = InstructionStore(self.runtime)

    def dispatch(
        self,
        thread_id: str,
        instruction_id: str,
        prompt: str,
        source: str,
        timeout_seconds: float = 30.0,
    ) -> QueueReceipt:
        thread_id = _canonical_uuid(thread_id, "thread id")
        instruction_id = validate_instruction_id(instruction_id)
        prompt = validate_prompt(prompt)
        digest = sha256_text(prompt)
        marker = f"[CODEX_WATCHDOG_WAKE id={instruction_id} sha256={digest}]"
        database = self._select_queue_database()
        baseline_revision = None
        if database is not None:
            try:
                _, baseline_revision = self._queue_snapshot(database, thread_id)
            except (OSError, sqlite3.Error):
                database = None
        rollout = self._find_rollout(thread_id)
        rollout_offset = None
        if rollout is not None:
            try:
                rollout_offset = rollout.stat().st_size
            except OSError:
                rollout = None
        self.records.mkdir(parents=True, exist_ok=True)
        record_name = sha256_text(instruction_id) + ".json"
        record_path = self.records / record_name
        existing_record: Optional[Dict[str, Any]] = None
        existing_state: Optional[str] = None
        with self.store.store_lock():
            if record_path.exists():
                existing = json.loads(record_path.read_text(encoding="utf-8"))
                if (
                    existing.get("prompt_sha256") != digest
                    or existing.get("thread_id") != thread_id
                    or existing.get("source") != source
                ):
                    raise InstructionCollisionError(
                        f"wake id {instruction_id!r} already exists for different metadata or content"
                    )
                existing_state = str(existing.get("state", "uncertain"))
                if existing_state == "accepted":
                    existing_state = "enqueued"
                existing_record = existing
            else:
                record = {
                    "schema_version": 2,
                    "instruction_id": instruction_id,
                    "thread_id": thread_id,
                    "source": source,
                    "prompt_sha256": digest,
                    "state": "dispatching",
                    "created_at": utc_now(),
                    "queue_database": str(database) if database is not None else None,
                    "queue_baseline_revision": baseline_revision,
                    "rollout_path": str(rollout) if rollout is not None else None,
                    "rollout_baseline_offset": rollout_offset,
                }
                self.store._atomic_json(record_path, record)

        if existing_record is not None and existing_state is not None:
            if existing_state in ("enqueued", "consumed_or_started"):
                observed = self.observe_delivery(instruction_id)
                return QueueReceipt(**{**observed.__dict__, "deduplicated": True})
            return self._receipt_from_record(
                existing_record, existing_state, deduplicated=True
            )

        wrapped = f"{marker}\n{prompt}"
        try:
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(self.codex_home)
            completed = self.runner(
                [
                    self.codex_executable,
                    "queue",
                    "--thread",
                    thread_id,
                    "--message",
                    wrapped,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                env=environment,
            )
            stdout = completed.stdout.strip()
            stderr = completed.stderr.strip()
            returncode = completed.returncode
            acknowledgement = _QUEUE_ACK.fullmatch(stdout)
            acknowledged_message = None
            acknowledged_thread = None
            if acknowledgement is not None:
                try:
                    acknowledged_message = _canonical_uuid(
                        acknowledgement.group("message_id"), "queue message id"
                    )
                    acknowledged_thread = _canonical_uuid(
                        acknowledgement.group("thread_id"), "acknowledged thread id"
                    )
                except ValueError:
                    acknowledgement = None
            if completed.returncode == 0 and acknowledged_thread == thread_id:
                status = "enqueued"
                queue_message_id = acknowledged_message
            else:
                status = "uncertain"
                queue_message_id = None
                if completed.returncode == 0 and acknowledgement is None:
                    stderr = (
                        stderr + "\n" if stderr else ""
                    ) + "queue acknowledgement could not be parsed"
        except (OSError, subprocess.TimeoutExpired) as exc:
            status = "uncertain"
            stdout = ""
            stderr = str(exc)
            returncode = -1
            queue_message_id = None

        record.update(
            state=status,
            completed_at=utc_now(),
            stdout_sha256=sha256_text(stdout) if stdout else None,
            stdout_chars=len(stdout),
            stderr_sha256=sha256_text(stderr) if stderr else None,
            stderr_chars=len(stderr),
            returncode=returncode,
            queue_message_id=queue_message_id,
        )
        with self.store.store_lock():
            current = json.loads(record_path.read_text(encoding="utf-8"))
            current.update(record)
            self.store._atomic_json(record_path, current)
        self.store.record_audit(
            {
                "event_type": "queue_wake",
                "outcome": status,
                "session_id": thread_id,
                "instruction_id": instruction_id,
                "instruction_source": source,
                "instruction_sha256": digest,
                "queue_message_id": queue_message_id,
            }
        )
        return QueueReceipt(
            instruction_id,
            thread_id,
            status,
            stdout,
            stderr,
            returncode,
            queue_message_id,
        )

    def observe_delivery(
        self, instruction_id: str, queue_database: Optional[Path] = None
    ) -> QueueReceipt:
        """Passively promote an enqueue using queue and rollout evidence."""
        instruction_id = validate_instruction_id(instruction_id)
        record_path = self.records / (sha256_text(instruction_id) + ".json")
        with self.store.store_lock():
            if not record_path.exists():
                raise FileNotFoundError(f"wake record not found: {instruction_id}")
            record = json.loads(record_path.read_text(encoding="utf-8"))

        state = str(record.get("state", "uncertain"))
        if state == "accepted":
            state = "enqueued"
        queue_message_id = record.get("queue_message_id")
        thread_id = str(record.get("thread_id", ""))
        if (
            state not in ("enqueued", "consumed_or_started")
            or not queue_message_id
            or not _UUID.fullmatch(thread_id)
        ):
            return self._receipt_from_record(record, state)

        recorded_database = record.get("queue_database")
        if queue_database is not None:
            database = Path(queue_database).expanduser().resolve()
            if recorded_database is not None and database != Path(recorded_database):
                raise ValueError("queue database does not match the dispatch record")
        elif recorded_database is not None:
            database = Path(recorded_database)
        else:
            database = None

        now = utc_now()
        if database is not None and database.is_file():
            snapshot_succeeded = False
            try:
                item_payload, revision = self._queue_snapshot(
                    database, thread_id, queue_message_id
                )
                snapshot_succeeded = True
            except (OSError, sqlite3.Error):
                item_payload, revision = None, None
            marker = self._marker(record)
            item_seen = item_payload is not None and marker in item_payload
            previously_seen = record.get("queue_row_seen") is True
            baseline = record.get("queue_baseline_revision")
            immediate_cycle = (
                isinstance(baseline, int)
                and isinstance(revision, int)
                and revision >= baseline + 2
            )
            if item_seen:
                record.update(queue_row_seen=True, queue_row_seen_at=now)
            elif (
                snapshot_succeeded
                and state == "enqueued"
                and (previously_seen or immediate_cycle)
            ):
                state = "consumed_or_started"
                record.update(state=state, consumed_or_started_at=now)
            record.update(
                last_observed_at=now, queue_revision=revision,
            )

        rollout_started = self._rollout_started(record)
        if rollout_started is not None:
            state = "started"
            record.update(
                state=state, started_at=now, started_turn_id=rollout_started,
            )
        with self.store.store_lock():
            current = json.loads(record_path.read_text(encoding="utf-8"))
            current_state = str(current.get("state", "uncertain"))
            rank = {
                "uncertain": 0,
                "dispatching": 0,
                "enqueued": 1,
                "consumed_or_started": 2,
                "started": 3,
            }
            if rank.get(current_state, 0) > rank.get(state, 0):
                return self._receipt_from_record(current, current_state)
            current.update(record)
            current["state"] = state
            self.store._atomic_json(record_path, current)
            record = current
        return self._receipt_from_record(record, state)

    def _select_queue_database(self) -> Optional[Path]:
        if self.queue_database is not None:
            return (
                self.queue_database
                if self._compatible_queue_database(self.queue_database)
                else None
            )
        pinned = self.codex_home / "queue_1.sqlite"
        if self._compatible_queue_database(pinned):
            return pinned.resolve()
        candidates = [
            path.resolve()
            for path in self.codex_home.glob("queue_*.sqlite")
            if self._compatible_queue_database(path)
        ]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _compatible_queue_database(database: Path) -> bool:
        if not database.is_file():
            return False
        try:
            uri = database.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
                connection.execute("PRAGMA query_only = ON")
                item_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(queued_items)")
                }
                revision_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(queued_thread_revisions)"
                    )
                }
            return {"id", "thread_id", "payload_json"}.issubset(item_columns) and {
                "thread_id",
                "revision",
            }.issubset(revision_columns)
        except (OSError, sqlite3.Error):
            return False

    @staticmethod
    def _queue_snapshot(
        database: Path, thread_id: str, queue_message_id: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[int]]:
        uri = database.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            item = None
            if queue_message_id is not None:
                item = connection.execute(
                    "SELECT payload_json FROM queued_items WHERE id = ? AND thread_id = ?",
                    (queue_message_id, thread_id),
                ).fetchone()
            revision = connection.execute(
                "SELECT revision FROM queued_thread_revisions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return (
            str(item[0]) if item is not None else None,
            int(revision[0]) if revision is not None else 0,
        )

    def _find_rollout(self, thread_id: str) -> Optional[Path]:
        sessions = self.codex_home / "sessions"
        if not sessions.is_dir():
            return None
        matches = list(sessions.rglob(f"rollout-*{thread_id}.jsonl"))
        return matches[0].resolve() if len(matches) == 1 else None

    @staticmethod
    def _marker(record: Dict[str, Any]) -> str:
        return (
            "[CODEX_WATCHDOG_WAKE "
            f"id={record.get('instruction_id')} sha256={record.get('prompt_sha256')}]"
        )

    @classmethod
    def _rollout_started(cls, record: Dict[str, Any]) -> Optional[str]:
        rollout_path = record.get("rollout_path")
        baseline = record.get("rollout_baseline_offset")
        thread_id = record.get("thread_id")
        if (
            not isinstance(rollout_path, str)
            or not isinstance(baseline, int)
            or not isinstance(thread_id, str)
        ):
            return None
        path = Path(rollout_path)
        try:
            with path.open("rb") as handle:
                handle.seek(baseline)
                appended = handle.read()
        except (OSError, ValueError):
            return None
        if not appended.endswith(b"\n"):
            appended = (
                appended.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in appended else b""
            )
        events: List[Dict[str, Any]] = []
        for line in appended.splitlines():
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        started_turns = {
            payload.get("turn_id")
            for event in events
            for payload in [event.get("payload")]
            if event.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "task_started"
            and isinstance(payload.get("turn_id"), str)
        }
        marker = cls._marker(record)
        prefix = marker + "\n"
        for event in events:
            payload = event.get("payload")
            if (
                event.get("type") != "event_msg"
                or not isinstance(payload, dict)
                or payload.get("type") != "item_completed"
                or payload.get("thread_id") != thread_id
                or payload.get("turn_id") not in started_turns
            ):
                continue
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "UserMessage":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                    and part["text"].startswith(prefix)
                    and sha256_text(part["text"][len(prefix) :])
                    == record.get("prompt_sha256")
                ):
                    return str(payload["turn_id"])
        return None

    @staticmethod
    def _receipt_from_record(
        record: Dict[str, Any], state: str, deduplicated: bool = False
    ) -> QueueReceipt:
        return QueueReceipt(
            instruction_id=str(record.get("instruction_id", "")),
            thread_id=str(record.get("thread_id", "")),
            status=state,
            stdout="",
            stderr="",
            returncode=int(record.get("returncode", -1)),
            queue_message_id=record.get("queue_message_id"),
            deduplicated=deduplicated,
        )

    def dispatch_remote_update(
        self, thread_id: str, remote_oid: str, workspace_id: Optional[str] = None
    ) -> QueueReceipt:
        if not _OID.fullmatch(remote_oid):
            raise ValueError("remote oid must be 7-64 hexadecimal characters")
        scope = workspace_id if workspace_id is not None else thread_id
        if not scope.strip():
            raise ValueError("workspace id must not be empty")
        return self.dispatch(
            thread_id,
            f"git:{sha256_text(scope)[:16]}:{remote_oid.lower()}",
            REMOTE_UPDATE_PROMPT,
            "remote_git",
        )

    def claim_and_dispatch_resume_prompt(
        self, thread_id: str
    ) -> Optional[QueueReceipt]:
        source = self.runtime / "resume_prompt.md"
        if not source.exists():
            return None
        self.resume_inflight.mkdir(parents=True, exist_ok=True)
        instruction_id = f"resume:{uuid.uuid4()}"
        destination = self.resume_inflight / f"{instruction_id.replace(':', '-')}.md"
        with self.store.store_lock():
            if not source.exists():
                return None
            os.replace(source, destination)
        prompt = destination.read_text(encoding="utf-8")
        validate_prompt(prompt)
        lifecycle_prompt = (
            f"{prompt}\n\n"
            "After handling this prompt, end with exactly one of:\n"
            "RESUME_PROMPT_DISPOSITION: DISCARD\n"
            "RESUME_PROMPT_DISPOSITION: ARCHIVE"
        )
        return self.dispatch(
            thread_id, instruction_id, lifecycle_prompt, "resume_prompt"
        )
