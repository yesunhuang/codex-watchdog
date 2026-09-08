from __future__ import annotations

import json
from pathlib import Path

import pytest

import codex_watchdog.storage as storage_module
from codex_watchdog.storage import InstructionCollisionError, InstructionStore


def test_submit_is_idempotent_and_uses_windows_safe_filename(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    first = store.submit("git:workspace:abc1234", "remote_git", "Continue once")
    second = store.submit("git:workspace:abc1234", "remote_git", "Continue once")

    assert first.status == "created"
    assert second.status == "queued"
    assert first.path == second.path
    assert ":" not in first.path.name
    assert len(first.path.stem) == 64


def test_same_id_with_different_prompt_is_collision(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    store.submit("manual-1", "manual", "First")

    with pytest.raises(InstructionCollisionError):
        store.submit("manual-1", "manual", "Different")


def test_claim_is_at_most_once_and_confirmation_moves_to_consumed(
    tmp_path: Path,
) -> None:
    store = InstructionStore(tmp_path)
    store.submit("manual-1", "manual", "Continue")

    claimed = store.claim_next("session-1", "turn-1")
    assert claimed is not None
    assert claimed.state == "return_intent"
    assert store.claim_next("session-1", "turn-1") is None
    assert list(store.list_state("inbox")) == []
    assert [item.instruction_id for item in store.list_state("inflight")] == [
        "manual-1"
    ]

    confirmed = store.confirm_continuation("session-1", "turn-1")
    assert confirmed is not None
    assert confirmed.state == "continued_confirmed"
    assert list(store.list_state("inflight")) == []
    assert [item.instruction_id for item in store.list_state("consumed")] == [
        "manual-1"
    ]


def test_wrong_turn_cannot_confirm_inflight_instruction(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    store.submit("manual-1", "manual", "Continue")
    store.claim_next("session-1", "turn-1")

    assert store.confirm_continuation("session-1", "turn-2") is None
    assert [item.state for item in store.list_state("inflight")] == ["return_intent"]


def test_malformed_inbox_file_is_never_interpreted_as_prompt(tmp_path: Path) -> None:
    store = InstructionStore(tmp_path)
    store.ensure()
    (store.inbox / "malformed.json").write_text("{not-json", encoding="utf-8")

    assert store.claim_next("session-1", "turn-1") is None
    assert (store.inbox / "malformed.json").exists()


def test_targeted_instruction_cannot_be_claimed_by_another_session(
    tmp_path: Path,
) -> None:
    store = InstructionStore(tmp_path)
    store.submit(
        "targeted-1",
        "manual",
        "Continue only the intended conversation",
        target_session_id="session-2",
    )

    assert store.claim_next("session-1", "turn-1") is None
    claimed = store.claim_next("session-2", "turn-2")

    assert claimed is not None
    assert claimed.target_session_id == "session-2"


def test_inbox_claim_order_is_creation_order_not_hashed_filename(
    tmp_path: Path,
) -> None:
    store = InstructionStore(tmp_path)
    store.submit("z-last-hash-unknown", "manual", "First submitted")
    store.submit("a-first-hash-unknown", "manual", "Second submitted")

    claimed = store.claim_next("session-1", "turn-1")

    assert claimed is not None
    assert claimed.prompt == "First submitted"


@pytest.mark.parametrize("clock_values", [
    ["2026-09-08T05:00:00Z", "2026-09-08T05:00:00Z"],
    ["2026-09-08T05:00:00Z", "2026-09-08T04:59:59Z"],
])
def test_creation_order_survives_clock_ties_rollback_and_store_restart(tmp_path, monkeypatch, clock_values):
    from codex_watchdog import models

    values = iter(clock_values)
    monkeypatch.setattr(models, "utc_now", lambda: next(values))
    first = InstructionStore(tmp_path).submit("z-first", "manual", "First submitted")
    first_bytes = first.path.read_bytes()
    second = InstructionStore(tmp_path).submit("a-second", "manual", "Second submitted")
    assert first.path.read_bytes() == first_bytes
    assert first.instruction.schema_version == second.instruction.schema_version == 1
    assert storage_module._creation_time(first.instruction) < storage_module._creation_time(second.instruction)
    # State transitions may share the wall clock; the ordering allocation is durable.
    monkeypatch.setattr(models, "utc_now", lambda: "2026-09-08T05:00:00Z")
    assert InstructionStore(tmp_path).claim_next("session", "turn-1").prompt == "First submitted"
    assert InstructionStore(tmp_path).claim_next("session", "turn-2").prompt == "Second submitted"


def test_upgrade_reads_legacy_timestamps_without_rewriting_existing_queue(tmp_path, monkeypatch):
    from codex_watchdog import models

    store = InstructionStore(tmp_path)
    first = store.submit("z-legacy", "manual", "Legacy first")
    value = json.loads(first.path.read_text())
    value["created_at"] = "2026-09-08T05:00:00Z"
    first.path.write_text(json.dumps(value))
    before = first.path.read_bytes()
    monkeypatch.setattr(models, "utc_now", lambda: "2026-09-08T05:00:00.000000Z")
    second = InstructionStore(tmp_path).submit("a-new", "manual", "New second")
    assert first.path.read_bytes() == before
    assert json.loads(second.path.read_text())["schema_version"] == 1
    assert store.claim_next("session", "turn-1").prompt == "Legacy first"
    assert store.claim_next("session", "turn-2").prompt == "New second"


def windows_permission_error(winerror: int) -> PermissionError:
    error = PermissionError(winerror, "simulated Windows replace failure")
    error.winerror = winerror
    return error


def test_atomic_json_retries_transient_windows_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"
    real_replace = storage_module.os.replace
    calls = 0
    sleeps = []

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise windows_permission_error(5)
        real_replace(source, destination)

    monkeypatch.setattr(storage_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage_module.os, "replace", flaky_replace)
    monkeypatch.setattr(storage_module.time, "sleep", sleeps.append)

    InstructionStore._atomic_json(path, {"state": "started"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"state": "started"}
    assert calls == 3
    assert sleeps == [0.01, 0.02]


def test_atomic_json_exhausts_transient_windows_replace_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"
    calls = 0
    sleeps = []

    def always_busy(_source: Path, _destination: Path) -> None:
        nonlocal calls
        calls += 1
        raise windows_permission_error(32)

    monkeypatch.setattr(storage_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage_module.os, "replace", always_busy)
    monkeypatch.setattr(storage_module.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError) as caught:
        InstructionStore._atomic_json(path, {"state": "started"})

    assert caught.value.winerror == 32
    assert calls == 5
    assert sleeps == [0.01, 0.02, 0.04, 0.08]
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_does_not_retry_other_permission_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"
    calls = 0
    sleeps = []

    def denied(_source: Path, _destination: Path) -> None:
        nonlocal calls
        calls += 1
        raise windows_permission_error(1314)

    monkeypatch.setattr(storage_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage_module.os, "replace", denied)
    monkeypatch.setattr(storage_module.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError) as caught:
        InstructionStore._atomic_json(path, {"state": "started"})

    assert caught.value.winerror == 1314
    assert calls == 1
    assert sleeps == []
