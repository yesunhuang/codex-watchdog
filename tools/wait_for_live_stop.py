"""Publish one acceptance instruction when a new live Stop hook starts waiting.

This helper is intentionally narrow.  Start it before sending the expendable
acceptance turn: it snapshots existing audit ids, waits for a new Stop/waiting
audit record, and targets one instruction to that record's exact session.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, TextIO, Union


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from codex_watchdog.storage import InstructionStore  # noqa: E402


TERMINAL_OUTCOMES = frozenset(
    {
        "return_intent",
        "grace_expired_parked",
        "loop_guard_parked",
        "continuation_confirmed_then_parked",
        "duplicate_turn_parked",
        "lock_busy_failed_open",
        "lock_busy_grace_expired_parked",
        "handler_error_failed_open",
    }
)


class WaitingStopTimeout(TimeoutError):
    """No matching new Stop/waiting audit appeared before the deadline."""


class TerminalAuditTimeout(TimeoutError):
    """A targeted instruction was published but its hook did not terminate."""

    def __init__(self, message: str, evidence: Dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def _audit_records(runtime: Path) -> Iterable[Dict[str, Any]]:
    audit = runtime / "audit"
    for path in sorted(audit.glob("*.json"), key=lambda item: item.name):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            yield value


def _audit_ids(runtime: Path) -> set[str]:
    return {
        audit_id
        for record in _audit_records(runtime)
        if isinstance((audit_id := record.get("audit_id")), str) and audit_id
    }


def _canonical_workspace(value: Union[str, Path]) -> str:
    resolved = Path(value).resolve(strict=False)
    text = os.path.normpath(str(resolved))
    return os.path.normcase(text) if os.name == "nt" else text


def _same_workspace(recorded: Any, expected: Optional[Path]) -> bool:
    if expected is None:
        return True
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        return _canonical_workspace(recorded) == _canonical_workspace(expected)
    except (OSError, RuntimeError, ValueError):
        return False


def _new_waiting_stop(
    runtime: Path,
    existing_audit_ids: set[str],
    expected_cwd: Optional[Path],
    expected_session: Optional[str],
) -> Optional[Dict[str, str]]:
    for record in _audit_records(runtime):
        audit_id = record.get("audit_id")
        if (
            not isinstance(audit_id, str)
            or not audit_id
            or audit_id in existing_audit_ids
            or record.get("event_type") != "Stop"
            or record.get("outcome") != "waiting"
            or (
                expected_session is not None
                and record.get("session_id") != expected_session
            )
            or not _same_workspace(record.get("workspace"), expected_cwd)
        ):
            continue
        required = {
            "audit_id": audit_id,
            "invocation_id": record.get("invocation_id"),
            "session_id": record.get("session_id"),
            "turn_id": record.get("turn_id"),
        }
        if all(isinstance(value, str) and value for value in required.values()):
            return required  # type: ignore[return-value]
    return None


def _terminal_audit(runtime: Path, invocation_id: str) -> Optional[Dict[str, Any]]:
    for record in _audit_records(runtime):
        outcome = record.get("outcome")
        if (
            record.get("invocation_id") != invocation_id
            or not isinstance(outcome, str)
            or outcome not in TERMINAL_OUTCOMES
        ):
            continue
        terminal: Dict[str, Any] = {"terminal_outcome": outcome}
        duration = record.get("hook_duration_ms")
        if (
            isinstance(duration, int)
            and not isinstance(duration, bool)
            and duration >= 0
        ):
            terminal["hook_duration_ms"] = duration
        return terminal
    return None


def wait_and_submit(
    runtime: Path,
    instruction_id: str,
    prompt: str,
    source: str = "live_acceptance",
    timeout: float = 120.0,
    poll: float = 0.05,
    expected_cwd: Optional[Path] = None,
    expected_session: Optional[str] = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Wait for one new live Stop/waiting audit and publish one targeted intent."""
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if poll <= 0:
        raise ValueError("poll must be greater than zero")
    if expected_session == "":
        raise ValueError("expected_session must not be empty")

    runtime = Path(runtime)
    existing_audit_ids = _audit_ids(runtime)
    deadline = monotonic() + timeout

    while True:
        waiting = _new_waiting_stop(
            runtime, existing_audit_ids, expected_cwd, expected_session
        )
        if waiting is not None:
            result = InstructionStore(runtime).submit(
                instruction_id, source, prompt, target_session_id=waiting["session_id"],
            )
            evidence = {
                "schema_version": 1,
                "event": "live_stop_instruction_submitted",
                "audit_id": waiting["audit_id"],
                "invocation_id": waiting["invocation_id"],
                "session_id": waiting["session_id"],
                "turn_id": waiting["turn_id"],
                "instruction_id": result.instruction.instruction_id,
                "instruction_sha256": result.instruction.prompt_sha256,
                "status": result.status,
            }

            terminal_deadline = monotonic() + timeout
            while True:
                terminal = _terminal_audit(runtime, waiting["invocation_id"])
                if terminal is not None:
                    evidence.update(terminal)
                    return evidence
                terminal_remaining = terminal_deadline - monotonic()
                if terminal_remaining <= 0:
                    raise TerminalAuditTimeout(
                        "instruction was submitted, but no terminal audit for "
                        f"invocation {waiting['invocation_id']} appeared within "
                        f"{timeout:g} seconds",
                        evidence,
                    )
                sleep(min(poll, terminal_remaining))

        remaining = deadline - monotonic()
        if remaining <= 0:
            filters = []
            if expected_cwd is not None:
                filters.append(f"workspace={_canonical_workspace(expected_cwd)!r}")
            if expected_session is not None:
                filters.append(f"session={expected_session!r}")
            suffix = " matching " + ", ".join(filters) if filters else ""
            raise WaitingStopTimeout(
                f"no new Stop/waiting audit{suffix} observed within {timeout:g} seconds"
            )
        sleep(min(poll, remaining))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--id", required=True, dest="instruction_id")
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--message")
    prompt.add_argument("--prompt-file", type=Path)
    parser.add_argument("--source", default="live_acceptance")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--poll", type=float, default=0.05)
    parser.add_argument("--expected-cwd", type=Path)
    parser.add_argument("--expected-session")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    try:
        message = (
            args.message
            if args.message is not None
            else args.prompt_file.read_text(encoding="utf-8")
        )
        evidence = wait_and_submit(
            args.runtime,
            args.instruction_id,
            message,
            source=args.source,
            timeout=args.timeout,
            poll=args.poll,
            expected_cwd=args.expected_cwd,
            expected_session=args.expected_session,
        )
    except WaitingStopTimeout as exc:
        stderr.write(f"wait_for_live_stop waiting phase timed out: {exc}\n")
        return 2
    except TerminalAuditTimeout as exc:
        partial = dict(exc.evidence)
        partial["terminal_status"] = "timed_out"
        stdout.write(json.dumps(partial, sort_keys=True) + "\n")
        stdout.flush()
        stderr.write(f"wait_for_live_stop terminal phase timed out: {exc}\n")
        return 3
    except Exception as exc:
        stderr.write(f"wait_for_live_stop failed: {exc}\n")
        return 1

    stdout.write(json.dumps(evidence, sort_keys=True) + "\n")
    stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
