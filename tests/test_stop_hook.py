from __future__ import annotations

import io
import json
from pathlib import Path

from codex_watchdog.models import sha256_text
from codex_watchdog.stop_hook import HookSettings, run_hook, run_hook_text
from codex_watchdog.storage import InstructionStore, StoreBusyError


def stop_payload(active: bool = False, turn_id: str = "turn-1") -> dict:
    return {
        "session_id": "session-1",
        "turn_id": turn_id,
        "transcript_path": None,
        "cwd": "D:\\project",
        "hook_event_name": "Stop",
        "model": "test-model",
        "permission_mode": "dontAsk",
        "stop_hook_active": active,
        "last_assistant_message": "recognizable final output",
    }


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds
        if self.on_sleep is not None:
            callback, self.on_sleep = self.on_sleep, None
            callback()


def settings(tmp_path: Path, grace: float = 1.0) -> HookSettings:
    return HookSettings(tmp_path, grace_seconds=grace, poll_seconds=0.1, test_mode=True)


def test_instruction_arriving_during_grace_returns_one_block(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    clock = FakeClock()
    clock.on_sleep = lambda: store.submit("instruction-1", "manual", "Continue Ω")
    stdout = io.StringIO()

    code = run_hook(
        settings(tmp_path),
        stdin=io.StringIO(json.dumps(stop_payload())),
        stdout=stdout,
        stderr=io.StringIO(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    response = json.loads(stdout.getvalue())
    assert code == 0
    assert response["decision"] == "block"
    assert "id=instruction-1" in response["reason"]
    assert response["reason"].endswith("Continue Ω")
    assert [item.instruction_id for item in store.list_state("inflight")] == [
        "instruction-1"
    ]
    assert not list(tmp_path.glob("transient/stop-output/*.json"))


def test_transient_store_contention_during_claim_is_retried(
    tmp_path: Path, monkeypatch
) -> None:
    store = InstructionStore(tmp_path)
    store.submit("instruction-1", "manual", "Continue after contention")
    clock = FakeClock()
    real_claim = InstructionStore.claim_next
    calls = 0

    def contended_claim(self, session_id: str, turn_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StoreBusyError("simulated publisher collision")
        return real_claim(self, session_id, turn_id)

    monkeypatch.setattr(InstructionStore, "claim_next", contended_claim)
    stdout = io.StringIO()

    code = run_hook(
        settings(tmp_path),
        stdin=io.StringIO(json.dumps(stop_payload())),
        stdout=stdout,
        stderr=io.StringIO(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert code == 0
    assert calls == 2
    assert json.loads(stdout.getvalue())["decision"] == "block"
    assert clock.value == 0.1


def test_transient_store_contention_during_confirmation_is_retried(
    tmp_path: Path, monkeypatch
) -> None:
    store = InstructionStore(tmp_path)
    store.submit("instruction-1", "manual", "Confirm after contention")
    store.claim_next("session-1", "turn-1")
    clock = FakeClock()
    real_confirm = InstructionStore.confirm_continuation
    calls = 0

    def contended_confirm(self, session_id: str, turn_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StoreBusyError("simulated publisher collision")
        return real_confirm(self, session_id, turn_id)

    monkeypatch.setattr(InstructionStore, "confirm_continuation", contended_confirm)

    code, stdout, stderr = run_hook_text(stop_payload(active=True), settings(tmp_path))

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout) == {}
    assert calls == 2
    assert [item.instruction_id for item in store.list_state("consumed")] == [
        "instruction-1"
    ]


def test_store_contention_never_claims_after_grace_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    store = InstructionStore(tmp_path)
    store.submit("instruction-1", "manual", "Leave queued after contention")
    clock = FakeClock()
    calls = 0

    def always_contended(_self, _session_id: str, _turn_id: str):
        nonlocal calls
        calls += 1
        raise StoreBusyError("simulated persistent publisher collision")

    monkeypatch.setattr(InstructionStore, "claim_next", always_contended)
    stdout = io.StringIO()

    code = run_hook(
        settings(tmp_path, grace=0.2),
        stdin=io.StringIO(json.dumps(stop_payload())),
        stdout=stdout,
        stderr=io.StringIO(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert code == 0
    assert json.loads(stdout.getvalue()) == {}
    assert calls == 2
    assert clock.value == 0.2
    assert [item.instruction_id for item in store.list_state("inbox")] == [
        "instruction-1"
    ]
    outcomes = {
        json.loads(path.read_text(encoding="utf-8"))["outcome"]
        for path in store.audit.glob("*.json")
    }
    assert "lock_busy_grace_expired_parked" in outcomes


def test_grace_expiry_parks_without_continuation(tmp_path: Path) -> None:
    clock = FakeClock()
    stdout = io.StringIO()

    code = run_hook(
        settings(tmp_path, grace=0.3),
        stdin=io.StringIO(json.dumps(stop_payload())),
        stdout=stdout,
        stderr=io.StringIO(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert code == 0
    assert json.loads(stdout.getvalue()) == {}
    outcomes = [
        json.loads(path.read_text(encoding="utf-8"))["outcome"]
        for path in InstructionStore(tmp_path).audit.glob("*.json")
    ]
    assert "grace_expired_parked" in outcomes


def test_stop_hook_active_confirms_then_parks_without_claiming_next(
    tmp_path: Path,
) -> None:
    store = InstructionStore(tmp_path)
    store.submit("instruction-1", "manual", "First")
    store.claim_next("session-1", "turn-1")
    store.submit("instruction-2", "manual", "Must remain queued")

    code, stdout, stderr = run_hook_text(stop_payload(active=True), settings(tmp_path))

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout) == {}
    assert [item.instruction_id for item in store.list_state("consumed")] == [
        "instruction-1"
    ]
    assert [item.instruction_id for item in store.list_state("inbox")] == [
        "instruction-2"
    ]


def test_duplicate_inactive_stop_for_same_turn_cannot_emit_twice(
    tmp_path: Path,
) -> None:
    store = InstructionStore(tmp_path)
    store.submit("instruction-1", "manual", "First")
    first = run_hook_text(stop_payload(), settings(tmp_path))
    store.submit("instruction-2", "manual", "Second")
    second = run_hook_text(stop_payload(), settings(tmp_path, grace=0.0))

    assert json.loads(first[1])["decision"] == "block"
    assert json.loads(second[1]) == {}
    assert [item.instruction_id for item in store.list_state("inbox")] == [
        "instruction-2"
    ]


def test_three_distinct_turns_can_each_consume_one_distinct_id(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    for number in range(1, 4):
        instruction_id = f"instruction-{number}"
        turn_id = f"turn-{number}"
        store.submit(instruction_id, "manual", f"Prompt {number}")
        response = run_hook_text(stop_payload(turn_id=turn_id), settings(tmp_path))[1]
        assert json.loads(response)["decision"] == "block"
        active_response = run_hook_text(
            stop_payload(active=True, turn_id=turn_id), settings(tmp_path)
        )[1]
        assert json.loads(active_response) == {}

    assert [item.instruction_id for item in store.list_state("consumed")] == [
        "instruction-1",
        "instruction-2",
        "instruction-3",
    ]


def test_permission_request_is_recorded_as_pre_routing_not_user_wait(
    tmp_path: Path,
) -> None:
    payload = stop_payload()
    payload.update(
        hook_event_name="PermissionRequest",
        tool_name="Bash",
        tool_input={"command": "example"},
    )
    payload.pop("stop_hook_active")
    payload.pop("last_assistant_message")

    code, stdout, stderr = run_hook_text(payload, settings(tmp_path))

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout) == {}
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.joinpath("audit").glob("*.json")
    ]
    assert records[0]["outcome"] == "permission_observed_pre_routing"


def test_durable_audit_is_hash_only_while_terminal_output_is_spooled(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    run_hook(
        settings(tmp_path, grace=0.0),
        stdin=io.StringIO(json.dumps(stop_payload())),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.joinpath("audit").glob("*.json")
    ]
    assert records[0]["last_output_sha256"] == sha256_text("recognizable final output")
    assert records[0]["last_assistant_message_available"] is True
    assert "recognizable final output" not in json.dumps(records)
    spools = list(tmp_path.glob("transient/stop-output/*.json"))
    assert len(spools) == 1
    transient = json.loads(spools[0].read_text(encoding="utf-8"))
    assert transient["last_assistant_message"] == "recognizable final output"
    assert transient["invocation_id"] == records[0]["invocation_id"]
    assert transient["session_id"] == records[0]["session_id"]
    assert transient["turn_id"] == records[0]["turn_id"]


def test_last_output_availability_distinguishes_empty_from_absent(
    tmp_path: Path,
) -> None:
    empty = stop_payload()
    empty["last_assistant_message"] = ""
    run_hook_text(empty, settings(tmp_path, grace=0.0))

    absent = stop_payload(turn_id="turn-2")
    absent.pop("last_assistant_message")
    run_hook_text(absent, settings(tmp_path, grace=0.0))

    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.joinpath("audit").glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["outcome"]
        == "grace_expired_parked"
    ]
    availability = {
        record["turn_id"]: record["last_assistant_message_available"]
        for record in records
    }
    assert availability == {"turn-1": True, "turn-2": False}


def test_invalid_json_fails_open_without_protocol_contamination(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_hook(
        settings(tmp_path), stdin=io.StringIO("not json"), stdout=stdout, stderr=stderr,
    )

    assert code == 0
    assert stdout.getvalue() == ""
    assert "ignored invalid" in stderr.getvalue()


def test_string_false_is_not_treated_as_active_loop_guard(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    store.submit("instruction-1", "manual", "Continue")
    payload = stop_payload()
    payload["stop_hook_active"] = "false"

    _, stdout, _ = run_hook_text(payload, settings(tmp_path))

    assert json.loads(stdout)["decision"] == "block"


def test_instruction_published_after_deadline_is_not_claimed(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    clock = FakeClock()
    clock.on_sleep = lambda: store.submit("late-1", "manual", "Too late")
    stdout = io.StringIO()

    run_hook(
        settings(tmp_path, grace=0.05),
        stdin=io.StringIO(json.dumps(stop_payload())),
        stdout=stdout,
        stderr=io.StringIO(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert json.loads(stdout.getvalue()) == {}
    assert [item.instruction_id for item in store.list_state("inbox")] == ["late-1"]


def test_audit_correlates_hook_start_and_completion_timing(tmp_path: Path) -> None:
    clock = FakeClock()

    run_hook(
        settings(tmp_path, grace=0.2),
        stdin=io.StringIO(json.dumps(stop_payload())),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in tmp_path.joinpath("audit").glob("*.json")
    ]
    waiting = next(record for record in records if record["outcome"] == "waiting")
    completed = next(
        record for record in records if record["outcome"] == "grace_expired_parked"
    )
    assert waiting["invocation_id"] == completed["invocation_id"]
    assert waiting["hook_started_at"] == completed["hook_started_at"]
    assert "hook_completed_at" not in waiting
    assert completed["hook_completed_at"]
    assert completed["hook_duration_ms"] == 200
