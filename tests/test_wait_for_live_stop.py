from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from codex_watchdog.storage import InstructionStore
from tools.wait_for_live_stop import (
    TerminalAuditTimeout,
    main,
    wait_and_submit,
)


def _waiting(
    store: InstructionStore,
    audit_id: str,
    invocation_id: str,
    session_id: str,
    turn_id: str,
    workspace: str = "D:/acceptance",
) -> None:
    store.record_audit(
        {
            "audit_id": audit_id,
            "event_type": "Stop",
            "outcome": "waiting",
            "invocation_id": invocation_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "workspace": workspace,
        }
    )


def _terminal(
    store: InstructionStore,
    invocation_id: str,
    outcome: str = "return_intent",
    duration: int = 125,
) -> None:
    store.record_audit(
        {
            "event_type": "Stop",
            "outcome": outcome,
            "invocation_id": invocation_id,
            "session_id": "session-a",
            "turn_id": "turn-a",
            "hook_duration_ms": duration,
        }
    )


def test_targets_exact_session_from_one_new_waiting_stop(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    _waiting(store, "old-audit", "old-invocation", "old-session", "old-turn")

    now = 0.0
    phase = 0

    def sleep(seconds: float) -> None:
        nonlocal now, phase
        now += seconds
        if phase == 0:
            phase = 1
            _waiting(
                store,
                "new-audit-wrong-workspace",
                "wrong-invocation-a",
                "session-a",
                "turn-a",
                workspace=str(tmp_path.parent),
            )
            _waiting(
                store,
                "new-audit-wrong-session",
                "wrong-invocation-b",
                "session-b",
                "turn-b",
                workspace=str(tmp_path),
            )
            _waiting(
                store,
                "new-audit-match",
                "new-invocation-a",
                "session-a",
                "turn-a",
                workspace=str(tmp_path),
            )
        elif phase == 1:
            phase = 2
            _terminal(store, "wrong-invocation-a", outcome="grace_expired_parked")
            _terminal(store, "new-invocation-a")

    evidence = wait_and_submit(
        tmp_path,
        "live-b-1",
        "Continue with the acceptance marker.",
        timeout=1.0,
        poll=0.05,
        expected_cwd=tmp_path / ".",
        expected_session="session-a",
        monotonic=lambda: now,
        sleep=sleep,
    )

    instructions = list(store.list_state("inbox"))
    assert len(instructions) == 1
    assert instructions[0].target_session_id == "session-a"
    assert evidence == {
        "schema_version": 1,
        "event": "live_stop_instruction_submitted",
        "audit_id": "new-audit-match",
        "invocation_id": "new-invocation-a",
        "session_id": "session-a",
        "turn_id": "turn-a",
        "instruction_id": "live-b-1",
        "instruction_sha256": instructions[0].prompt_sha256,
        "status": "created",
        "terminal_outcome": "return_intent",
        "hook_duration_ms": 125,
    }
    assert "Continue with the acceptance marker." not in json.dumps(evidence)


def test_ignores_new_nonwaiting_audits_and_times_out(tmp_path: Path) -> None:
    InstructionStore(tmp_path).record_audit(
        {
            "event_type": "Stop",
            "outcome": "grace_expired_parked",
            "invocation_id": "invocation-a",
            "session_id": "session-a",
            "turn_id": "turn-a",
        }
    )
    ticks = iter((0.0, 1.0))

    with pytest.raises(TimeoutError, match="no new Stop/waiting audit"):
        wait_and_submit(
            tmp_path,
            "live-timeout",
            "unused",
            timeout=0.5,
            monotonic=lambda: next(ticks),
            sleep=lambda _: None,
        )

    assert list(InstructionStore(tmp_path).list_state("inbox")) == []


def test_terminal_timeout_is_distinct_after_exactly_one_submit(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    now = 0.0
    waiting_written = False

    def sleep(seconds: float) -> None:
        nonlocal now, waiting_written
        now += seconds
        if not waiting_written:
            waiting_written = True
            _waiting(
                store, "new-audit", "new-invocation", "session-a", "turn-a",
            )

    with pytest.raises(TerminalAuditTimeout) as raised:
        wait_and_submit(
            tmp_path,
            "live-terminal-timeout",
            "submitted once",
            timeout=0.1,
            poll=0.05,
            monotonic=lambda: now,
            sleep=sleep,
        )

    assert raised.value.evidence["status"] == "created"
    assert raised.value.evidence["invocation_id"] == "new-invocation"
    assert len(list(store.list_state("inbox"))) == 1


def test_cli_timeout_is_nonzero_and_emits_no_evidence(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(
        [
            "--runtime",
            str(tmp_path),
            "--id",
            "live-timeout",
            "--message",
            "unused",
            "--timeout",
            "0.001",
            "--poll",
            "0.001",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 2
    assert stdout.getvalue() == ""
    assert "timed out" in stderr.getvalue()
