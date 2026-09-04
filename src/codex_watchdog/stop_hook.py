from __future__ import annotations

from dataclasses import dataclass
import io
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, TextIO
import uuid

from .models import sha256_text, utc_now
from .storage import InstructionStore, StoreBusyError


_STOP_OUTPUT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class HookSettings:
    runtime: Path
    grace_seconds: float = 600.0
    poll_seconds: float = 0.5
    test_mode: bool = False

    def validate(self) -> None:
        minimum, maximum = (0.0, 30.0) if self.test_mode else (300.0, 1_200.0)
        if not minimum <= self.grace_seconds <= maximum:
            raise ValueError(
                f"grace_seconds must be between {minimum:g} and {maximum:g} seconds"
            )
        if not 0.01 <= self.poll_seconds <= 5.0:
            raise ValueError("poll_seconds must be between 0.01 and 5 seconds")


def _audit_fields(payload: Dict[str, Any], outcome: str) -> Dict[str, Any]:
    last_message = payload.get("last_assistant_message")
    last_text = last_message if isinstance(last_message, str) else ""
    cwd = payload.get("cwd")
    return {
        "event_type": payload.get("hook_event_name", "invalid"),
        "outcome": outcome,
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "workspace": cwd if isinstance(cwd, str) else None,
        "permission_mode": payload.get("permission_mode"),
        "stop_hook_active": payload.get("stop_hook_active") is True,
        "last_assistant_message_available": isinstance(last_message, str),
        "last_output_sha256": sha256_text(last_text) if last_text else None,
        "last_output_chars": len(last_text),
    }


def _emit(value: Dict[str, Any], stdout: TextIO) -> None:
    stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def _spool_terminal_output(
    runtime: Path, payload: Dict[str, Any], invocation_id: str
) -> None:
    """Atomically retain raw terminal output outside the durable audit trail."""
    message = payload.get("last_assistant_message")
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    workspace = payload.get("cwd")
    if (
        not isinstance(message, str)
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(turn_id, str)
        or not turn_id
        or not isinstance(workspace, str)
        or not workspace
    ):
        return
    InstructionStore._atomic_json(
        runtime / "transient" / "stop-output" / f"{invocation_id}.json",
        {
            "schema_version": _STOP_OUTPUT_SCHEMA_VERSION,
            "invocation_id": invocation_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "workspace": workspace,
            "last_assistant_message": message,
            "last_output_sha256": sha256_text(message) if message else None,
            "last_output_chars": len(message),
        },
    )


def run_hook(
    settings: HookSettings,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run one hook invocation; every local failure is deliberately fail-open."""
    try:
        settings.validate()
        payload = json.load(stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")
    except Exception as exc:
        stderr.write(f"codex-watchdog hook ignored invalid input/config: {exc}\n")
        return 0

    store = InstructionStore(settings.runtime)
    invocation_id = str(uuid.uuid4())
    hook_started_at = utc_now()
    hook_started_tick = monotonic()

    def audit_fields(outcome: str, completed: bool = False) -> Dict[str, Any]:
        event = _audit_fields(payload, outcome)
        event.update(
            invocation_id=invocation_id, hook_started_at=hook_started_at,
        )
        if completed:
            event.update(
                hook_completed_at=utc_now(),
                hook_duration_ms=max(0, int((monotonic() - hook_started_tick) * 1_000)),
            )
        return event

    def retry_store(operation: Callable[[], Any], deadline: float) -> Any:
        """Retry only transient cross-process store-lock contention."""
        while True:
            try:
                return operation()
            except StoreBusyError:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise
                sleep(min(settings.poll_seconds, remaining))
                if monotonic() >= deadline:
                    raise

    def record_terminal(event: Dict[str, Any]) -> None:
        try:
            _spool_terminal_output(settings.runtime, payload, invocation_id)
        except Exception as exc:
            stderr.write(f"codex-watchdog transient output spool failed open: {exc}\n")
        store.record_audit(event)

    event_name = payload.get("hook_event_name")
    if event_name != "Stop":
        try:
            outcome = (
                "permission_observed_pre_routing"
                if event_name == "PermissionRequest"
                else "observed"
            )
            store.record_audit(audit_fields(outcome, completed=True))
        except Exception as exc:
            stderr.write(f"codex-watchdog audit failed open: {exc}\n")
        _emit({}, stdout)
        return 0

    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(turn_id, str)
        or not turn_id
    ):
        try:
            store.record_audit(
                audit_fields("invalid_stop_context_parked", completed=True)
            )
        except Exception:
            pass
        _emit({}, stdout)
        return 0

    try:
        with store.session_lock(session_id):
            if payload.get("stop_hook_active") is True:
                contention_deadline = monotonic() + min(
                    1.0, max(0.1, settings.poll_seconds * 4)
                )
                confirmed = retry_store(
                    lambda: store.confirm_continuation(session_id, turn_id),
                    contention_deadline,
                )
                event = audit_fields("loop_guard_parked", completed=True)
                if confirmed is not None:
                    event.update(
                        instruction_id=confirmed.instruction_id,
                        instruction_sha256=confirmed.prompt_sha256,
                        outcome="continuation_confirmed_then_parked",
                    )
                record_terminal(event)
                _emit({}, stdout)
                return 0

            if store.turn_has_intent(session_id, turn_id):
                record_terminal(audit_fields("duplicate_turn_parked", completed=True))
                _emit({}, stdout)
                return 0

            waiting = audit_fields("waiting")
            waiting["grace_seconds"] = settings.grace_seconds
            store.record_audit(waiting)
            deadline = monotonic() + settings.grace_seconds
            first_claim = True
            while True:
                if not first_claim and monotonic() >= deadline:
                    record_terminal(
                        audit_fields("grace_expired_parked", completed=True)
                    )
                    _emit({}, stdout)
                    return 0
                try:
                    instruction = retry_store(
                        lambda: store.claim_next(session_id, turn_id), deadline
                    )
                except StoreBusyError:
                    record_terminal(
                        audit_fields("lock_busy_grace_expired_parked", completed=True)
                    )
                    _emit({}, stdout)
                    return 0
                first_claim = False
                if instruction is not None:
                    event = audit_fields("return_intent", completed=True)
                    event.update(
                        instruction_id=instruction.instruction_id,
                        instruction_source=instruction.source,
                        instruction_sha256=instruction.prompt_sha256,
                    )
                    store.record_audit(event)
                    reason = (
                        "[CODEX_WATCHDOG_INSTRUCTION "
                        f"id={instruction.instruction_id} sha256={instruction.prompt_sha256}]\n"
                        f"{instruction.prompt}"
                    )
                    _emit({"decision": "block", "reason": reason}, stdout)
                    return 0
                remaining = deadline - monotonic()
                if remaining <= 0:
                    record_terminal(
                        audit_fields("grace_expired_parked", completed=True)
                    )
                    _emit({}, stdout)
                    return 0
                sleep(min(settings.poll_seconds, remaining))
    except StoreBusyError as exc:
        stderr.write(f"codex-watchdog concurrent hook failed open: {exc}\n")
        try:
            record_terminal(audit_fields("lock_busy_failed_open", completed=True))
        except Exception:
            pass
    except Exception as exc:
        stderr.write(f"codex-watchdog hook failed open: {exc}\n")
        try:
            record_terminal(audit_fields("handler_error_failed_open", completed=True))
        except Exception:
            pass

    _emit({}, stdout)
    return 0


def run_hook_text(
    payload: Dict[str, Any], settings: HookSettings
) -> tuple[int, str, str]:
    """Test helper that exercises the actual stdin/stdout protocol."""
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_hook(settings, stdin=stdin, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()
