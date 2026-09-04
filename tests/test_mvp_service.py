from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Union

import pytest

from codex_watchdog.git_adapter import GitObservation
from codex_watchdog.models import sha256_text
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.notifications import NotificationResult
from codex_watchdog.queue_wake import QueueReceipt
from codex_watchdog.storage import InstructionStore
from codex_watchdog.workspace_registry import TrackedWorkspace


THREAD_A = "11111111-2222-4333-8444-555555555555"
THREAD_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OID_A = "a" * 40
OID_B = "b" * 40
OID_C = "c" * 40


class FakeRegistry:
    def __init__(self, workspaces: Sequence[TrackedWorkspace]) -> None:
        self.workspaces = tuple(workspaces)

    def list_workspaces(self) -> List[TrackedWorkspace]:
        return list(self.workspaces)


class FakeAdapter:
    def __init__(
        self,
        observations: Dict[str, List[Union[GitObservation, Exception]]],
        log: Optional[List[str]] = None,
    ) -> None:
        self.observations = observations
        self.log = log if log is not None else []

    def observe(self, repo_root: Path) -> GitObservation:
        key = str(Path(repo_root).resolve())
        self.log.append(f"observe:{Path(key).name}")
        values = self.observations[key]
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value


class FakeMutator:
    def __init__(self, *, preserve=None, log: Optional[List[str]] = None,) -> None:
        self.preserve_result = preserve
        self.log = log if log is not None else []
        self.preserve_calls: List[GitObservation] = []

    def preserve_and_push(self, repo_root: Path, expected: GitObservation):
        raise AssertionError("production WatchDog must never mutate Git")


class FakeNotifier:
    def __init__(
        self,
        runtime: Path,
        log: Optional[List[str]] = None,
        *,
        status: str = "sent",
        channel: str = "test",
    ) -> None:
        self.runtime = runtime
        self.log = log if log is not None else []
        self.status = status
        self.channel = channel
        self.events = []

    def notify(self, event) -> NotificationResult:
        self.log.append(f"notify:{event.event_type}")
        self.events.append(event)
        return NotificationResult(
            status=self.status,
            channel=self.channel,
            event_fingerprint=event.event_fingerprint(),
            duplicate=False,
            attempted_channels=("test",),
            configuration_issues=(),
            state_path=self.runtime / "notifications" / "last-events.json",
            state_persisted=True,
        )


class FakeQueue:
    def __init__(
        self,
        *,
        resume_receipt: Optional[QueueReceipt] = None,
        remote_receipt: Optional[QueueReceipt] = None,
        remote_receipts: Optional[Sequence[QueueReceipt]] = None,
        remove_resume: bool = True,
    ) -> None:
        self.resume_receipt = resume_receipt
        self.remote_receipt = remote_receipt
        self.remote_receipts = tuple(remote_receipts or ())
        self.remove_resume = remove_resume
        self.resume_calls: List[str] = []
        self.remote_calls: List[tuple[str, str, Optional[str]]] = []
        self.runtime: Optional[Path] = None

    def claim_and_dispatch_resume_prompt(
        self, thread_id: str
    ) -> Optional[QueueReceipt]:
        self.resume_calls.append(thread_id)
        if self.remove_resume and self.runtime is not None:
            (self.runtime / "resume_prompt.md").unlink(missing_ok=True)
        return self.resume_receipt

    def dispatch_remote_update(
        self, thread_id: str, remote_oid: str, workspace_id: Optional[str] = None
    ) -> QueueReceipt:
        self.remote_calls.append((thread_id, remote_oid, workspace_id))
        if self.remote_receipts:
            index = min(len(self.remote_calls) - 1, len(self.remote_receipts) - 1)
            return self.remote_receipts[index]
        assert self.remote_receipt is not None
        return self.remote_receipt


class FakeRemoteSshAdapter:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def probe(self, target, **kwargs):
        self.calls.append((target, kwargs))
        value = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return value


class FakeRemoteRegistry(FakeRegistry):
    def __init__(self, runtime: Path, *, storage_key: str = "d" * 32) -> None:
        super().__init__([])
        self.path = runtime / "service" / "workspace-discovery.json"
        self.last_snapshot = None
        self.remote_window = SimpleNamespace(
            locality="remote_ssh",
            remote_authority="ssh-remote+hpc-login.example.edu",
            workspace_path="/home/operator/ProjectAlpha",
            workspace_storage_key=storage_key,
        )

    def list_workspaces(self) -> List[TrackedWorkspace]:
        self.last_snapshot = SimpleNamespace(
            status="partial",
            windows=(self.remote_window,),
            effective_workspaces=(),
            issues=("remote_agent_required",),
        )
        return []


def test_remote_targets_skip_excluded_windows() -> None:
    snapshot = SimpleNamespace(
        windows=(
            SimpleNamespace(
                locality="remote_ssh",
                tracking_status="excluded",
                remote_authority="ssh-remote+example.invalid",
                workspace_path="/home/user/repo",
                workspace_storage_key="a" * 32,
            ),
        )
    )

    assert MvpWatchdogService._remote_targets(snapshot) == ()


def workspace(root: Path, workspace_id: str = "watchdog", thread: str = THREAD_A):
    return TrackedWorkspace.create(workspace_id, root, thread)


def observation(
    root: Path,
    *,
    topology: str = "equal",
    head: str = OID_A,
    upstream: Optional[str] = None,
    dirty: bool = False,
    untracked: bool = False,
    blockers: Sequence[str] = (),
) -> GitObservation:
    upstream_oid = upstream if upstream is not None else head
    return GitObservation(
        repo_root=str(root.resolve()),
        status="blocked" if blockers else "observed",
        topology=topology,
        branch="main",
        upstream="origin/main",
        head_oid=head,
        upstream_oid=upstream_oid,
        dirty_tracked=dirty,
        untracked_present=untracked,
        blockers=tuple(blockers),
        error_sha256=None,
        error_chars=0,
        observed_at="2026-09-01T00:00:00Z",
    )


def receipt(
    thread: str,
    instruction_id: str,
    *,
    status: str = "enqueued",
    deduplicated: bool = False,
) -> QueueReceipt:
    delivered = status in ("enqueued", "consumed_or_started", "started")
    return QueueReceipt(
        instruction_id=instruction_id,
        thread_id=thread,
        status=status,
        stdout="not retained in MVP result",
        stderr="not retained in MVP result",
        returncode=0 if delivered else -1,
        queue_message_id=(
            "99999999-8888-4777-8666-555555555555" if delivered else None
        ),
        deduplicated=deduplicated,
    )


def write_audit(
    runtime: Path,
    name: str,
    tracked: TrackedWorkspace,
    *,
    audit_id: str,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    completed: bool = True,
    event_type: str = "Stop",
    outcome: Optional[str] = None,
    turn_id: Optional[str] = None,
    started_at: str = "2026-09-01T00:00:00Z",
    completed_at: str = "2026-09-01T00:00:02Z",
    recorded_at: str = "2026-09-01T00:00:03Z",
    stop_hook_active: Optional[bool] = None,
    invocation_id: Optional[str] = None,
    last_output: str = "final output",
) -> Path:
    selected_outcome = (
        outcome
        if outcome is not None
        else "grace_expired_parked"
        if completed
        else "waiting"
    )
    value = {
        "schema_version": 1,
        "event_type": event_type,
        "outcome": selected_outcome,
        "audit_id": audit_id,
        "invocation_id": (
            invocation_id if invocation_id is not None else f"invocation-{audit_id}"
        ),
        "session_id": session_id if session_id is not None else tracked.session_id,
        "turn_id": turn_id if turn_id is not None else f"turn-{audit_id}",
        "workspace": str(repo_root if repo_root is not None else tracked.repo_root),
        "hook_started_at": started_at,
        "recorded_at": recorded_at,
        "stop_hook_active": (
            stop_hook_active
            if stop_hook_active is not None
            else selected_outcome
            in ("continuation_confirmed_then_parked", "loop_guard_parked")
        ),
        "last_assistant_message_available": True,
        "last_output_sha256": sha256_text(last_output) if last_output else None,
        "last_output_chars": len(last_output),
    }
    if completed:
        value["hook_completed_at"] = completed_at
    path = runtime / "audit" / name
    InstructionStore._atomic_json(path, value)
    return path


def write_stop_output(
    runtime: Path,
    tracked: TrackedWorkspace,
    invocation_id: str,
    turn_id: str,
    output: str,
    *,
    session_id: Optional[str] = None,
) -> Path:
    path = runtime / "transient" / "stop-output" / f"{invocation_id}.json"
    InstructionStore._atomic_json(
        path,
        {
            "schema_version": 1,
            "invocation_id": invocation_id,
            "session_id": session_id if session_id is not None else tracked.session_id,
            "turn_id": turn_id,
            "workspace": str(tracked.repo_root),
            "last_assistant_message": output,
            "last_output_sha256": sha256_text(output) if output else None,
            "last_output_chars": len(output),
        },
    )
    return path


def write_rollout(
    codex_home: Path,
    tracked: TrackedWorkspace,
    turn_id: str,
    *,
    later_turn_id: Optional[str] = None,
    suffix: str = "one",
    last_output: str = "final output",
) -> Path:
    def event(timestamp: str, kind: str, turn: str) -> Dict[str, object]:
        payload: Dict[str, object] = {"type": kind, "turn_id": turn}
        if kind == "item_completed":
            payload.update(
                thread_id=tracked.session_id,
                item={"type": "UserMessage", "content": []},
            )
        return {"timestamp": timestamp, "type": "event_msg", "payload": payload}

    events = [
        event("2026-09-01T00:00:00.500Z", "task_started", turn_id),
        event("2026-09-01T00:00:01Z", "item_completed", turn_id),
        {
            **event("2026-09-01T00:00:03Z", "task_complete", turn_id),
            "payload": {
                "type": "task_complete",
                "turn_id": turn_id,
                "last_agent_message": last_output,
            },
        },
    ]
    if later_turn_id is not None:
        events.extend(
            [
                event("2026-09-01T00:00:04Z", "task_started", later_turn_id),
                event("2026-09-01T00:00:04.100Z", "item_completed", later_turn_id),
            ]
        )
    path = (
        codex_home
        / "sessions"
        / "2026"
        / "09"
        / "01"
        / f"rollout-{suffix}-{tracked.session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def test_rollout_task_complete_notifies_when_stop_hook_is_missing(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "ProjectAlpha"
    repo.mkdir()
    tracked = workspace(repo, "vscode-internal")
    write_rollout(
        runtime / "codex-home",
        tracked,
        "turn-complete",
        last_output="ProjectAlpha final output",
    )
    notifier = FakeNotifier(runtime)
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [observation(repo)]}),
        FakeMutator(),
        notifier,
        FakeQueue(),
    )

    first = service.run_once().workspaces[0]
    second = service.run_once().workspaces[0]

    assert first.stop_count == 1
    assert first.stop_audit_id == "rollout:turn-complete"
    assert second.stop_count == 0
    assert [event.event_type for event in notifier.events] == ["codex_parked"]
    assert notifier.events[0].workspace_id == "vscode-internal"
    assert notifier.events[0].subject == "[Codex Watchdog] ProjectAlpha stopped"
    assert "ProjectAlpha final output" in notifier.events[0].message
    assert not list((runtime / "transient" / "stop-output").glob("*.json"))


def build_service(
    runtime: Path,
    workspaces: Sequence[TrackedWorkspace],
    adapter: FakeAdapter,
    mutator: FakeMutator,
    notifier: FakeNotifier,
    queue: FakeQueue,
    **kwargs,
) -> MvpWatchdogService:
    queue.runtime = runtime
    kwargs.setdefault("codex_home", runtime / "codex-home")
    kwargs.setdefault("clock", lambda: datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc))
    return MvpWatchdogService(
        runtime,
        registry=FakeRegistry(workspaces),
        git_adapter=adapter,
        notifier=notifier,
        queue_dispatcher=queue,
        **kwargs,
    )


def test_default_baselines_then_matches_only_completed_exact_stop(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    tracked = workspace(repo)
    write_audit(runtime, "a-old.json", tracked, audit_id="old")
    write_audit(runtime, "b-waiting.json", tracked, audit_id="waiting", completed=False)
    clean = observation(repo)
    adapter = FakeAdapter({str(repo.resolve()): [clean]})
    notifier = FakeNotifier(runtime)
    mutator = FakeMutator()
    queue = FakeQueue()
    service = build_service(runtime, [tracked], adapter, mutator, notifier, queue)

    first = service.run_once()
    assert first.workspaces[0].stop_count == 0

    write_audit(runtime, "z-exact.json", tracked, audit_id="exact")
    write_audit(
        runtime, "z-wrong-path.json", tracked, audit_id="wrong-path", repo_root=other,
    )
    write_audit(
        runtime,
        "z-wrong-session.json",
        tracked,
        audit_id="wrong-session",
        session_id=THREAD_B,
    )
    write_audit(
        runtime, "z-new-waiting.json", tracked, audit_id="new-wait", completed=False
    )
    second = service.run_once()
    third = service.run_once()

    assert second.workspaces[0].stop_count == 1
    assert second.workspaces[0].stop_audit_id == "exact"
    assert third.workspaces[0].stop_count == 0
    assert [event.event_type for event in notifier.events] == ["codex_parked"]
    assert notifier.events[0].subject == "[Codex Watchdog] repo stopped"
    assert notifier.events[0].workspace_id == "watchdog"
    assert notifier.events[0].message.startswith(
        "Repository: repo\nWorkspace ID: watchdog\n"
    )
    assert not mutator.preserve_calls


def test_opt_in_replays_latest_stop_without_mutating_git(tmp_path: Path,) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    write_audit(
        runtime,
        "a-first.json",
        tracked,
        audit_id="first",
        started_at="2026-08-31T23:59:57Z",
        completed_at="2026-08-31T23:59:58Z",
        recorded_at="2026-08-31T23:59:59Z",
    )
    write_audit(runtime, "b-latest.json", tracked, audit_id="latest")
    write_rollout(runtime / "codex-home", tracked, "turn-latest")
    write_audit(
        runtime, "z-unrelated.json", tracked, audit_id="unrelated", session_id=THREAD_B,
    )
    log: List[str] = []
    dirty = observation(repo, dirty=True)
    final = observation(repo, head=OID_C)
    adapter = FakeAdapter({str(repo.resolve()): [dirty, final]}, log=log)
    mutator = FakeMutator(log=log)
    notifier = FakeNotifier(runtime, log=log)
    service = build_service(
        runtime,
        [tracked],
        adapter,
        mutator,
        notifier,
        FakeQueue(),
        replay_latest_stop=True,
    )

    result = service.run_once()

    workspace_result = result.workspaces[0]
    assert workspace_result.stop_count == 1
    assert workspace_result.stop_audit_id == "latest"
    assert not mutator.preserve_calls
    assert [event.event_type for event in notifier.events] == ["codex_parked"]
    assert "preserve" not in log


def test_matching_terminal_output_is_sent_exactly_then_spool_is_deleted(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    invocation_id = "12345678-1234-4234-8234-123456789abc"
    turn_id = "turn-stop-output"
    output = "Exact Codex final output.\nA second line is preserved."
    write_audit(
        runtime,
        "a-stop.json",
        tracked,
        audit_id="stop-output",
        invocation_id=invocation_id,
        turn_id=turn_id,
        last_output=output,
    )
    spool = write_stop_output(runtime, tracked, invocation_id, turn_id, output)
    notifier = FakeNotifier(runtime)
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [observation(repo)]}),
        FakeMutator(),
        notifier,
        FakeQueue(),
        replay_latest_stop=True,
    )

    result = service.run_once().workspaces[0]

    assert result.notifications[0]["status"] == "sent"
    assert f"Codex final output:\n{'-' * 48}\n{output}\n{'-' * 48}" in (
        notifier.events[0].message
    )
    assert not spool.exists()
    durable_audit = (runtime / "audit" / "a-stop.json").read_text(encoding="utf-8")
    assert output not in durable_audit


@pytest.mark.parametrize(
    ("status", "channel", "mismatched"),
    [("audit_only", "local_audit", False), ("sent", "test", True),],
)
def test_undelivered_or_mismatched_terminal_output_is_retained_and_not_attached(
    tmp_path: Path, status: str, channel: str, mismatched: bool
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    invocation_id = "87654321-4321-4321-8321-cba987654321"
    turn_id = "turn-retained-output"
    output = "Do not attach this to the wrong notification."
    write_audit(
        runtime,
        "a-stop.json",
        tracked,
        audit_id="retained-output",
        invocation_id=invocation_id,
        turn_id=turn_id,
        last_output=output,
    )
    spool = write_stop_output(
        runtime,
        tracked,
        invocation_id,
        turn_id,
        output,
        session_id=THREAD_B if mismatched else None,
    )
    notifier = FakeNotifier(runtime, status=status, channel=channel)
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [observation(repo)]}),
        FakeMutator(),
        notifier,
        FakeQueue(),
        replay_latest_stop=True,
    )

    service.run_once()

    assert spool.exists()
    if mismatched:
        assert output not in notifier.events[0].message
    else:
        assert output in notifier.events[0].message


def test_return_intent_is_ignored_until_a_parked_stop_arrives(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    clean = observation(repo)
    notifier = FakeNotifier(runtime)
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [clean]}),
        FakeMutator(),
        notifier,
        FakeQueue(),
    )
    service.run_once()
    write_audit(
        runtime,
        "z-return-intent.json",
        tracked,
        audit_id="return-intent",
        outcome="return_intent",
    )

    continuing = service.run_once()
    write_audit(
        runtime,
        "zz-confirmed-parked.json",
        tracked,
        audit_id="confirmed-parked",
        outcome="continuation_confirmed_then_parked",
    )
    parked = service.run_once()

    assert continuing.workspaces[0].stop_count == 0
    assert parked.workspaces[0].stop_count == 1
    assert parked.workspaces[0].stop_audit_id == "confirmed-parked"
    assert [event.event_type for event in notifier.events] == ["codex_parked"]


def test_remote_oid_stays_pending_until_existing_queue_record_is_consumed(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    remote = observation(repo, topology="remote_ahead", head=OID_A, upstream=OID_B)
    adapter = FakeAdapter({str(repo.resolve()): [remote, remote, remote]})
    mutator = FakeMutator()
    queue = FakeQueue(
        remote_receipts=(
            receipt(THREAD_A, "git:watchdog:bbbb"),
            receipt(
                THREAD_A, "git:watchdog:bbbb", status="enqueued", deduplicated=True,
            ),
            receipt(
                THREAD_A,
                "git:watchdog:bbbb",
                status="consumed_or_started",
                deduplicated=True,
            ),
        )
    )
    service = build_service(
        runtime, [tracked], adapter, mutator, FakeNotifier(runtime), queue,
    )

    first = service.run_once()
    first_state = json.loads(service.state_path("watchdog").read_text(encoding="utf-8"))
    second = service.run_once()
    second_state = json.loads(
        service.state_path("watchdog").read_text(encoding="utf-8")
    )
    third = service.run_once()

    assert first.workspaces[0].wake["kind"] == "remote_update"
    assert first.workspaces[0].wake["status"] == "enqueued"
    assert second.workspaces[0].wake["status"] == "enqueued"
    assert second.workspaces[0].wake["deduplicated"] is True
    assert third.workspaces[0].wake["status"] == "consumed_or_started"
    assert queue.remote_calls == [
        (THREAD_A, OID_B, "watchdog"),
        (THREAD_A, OID_B, "watchdog"),
        (THREAD_A, OID_B, "watchdog"),
    ]
    assert not mutator.preserve_calls
    assert first.workspaces[0].initial_git["head_oid"] == OID_A
    assert first.workspaces[0].final_git["head_oid"] == OID_A
    assert first_state["pending_remote_oid"] == OID_B
    assert first_state["last_remote_oid"] is None
    assert second_state["pending_remote_oid"] == OID_B
    state = json.loads(service.state_path("watchdog").read_text(encoding="utf-8"))
    assert state["pending_remote_oid"] is None
    assert state["last_remote_oid"] == OID_B


def test_transient_observation_preserves_cursor_pending_wake_and_temporal_prompt(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime.mkdir()
    tracked = workspace(repo)
    resume = runtime / "resume_prompt.md"
    resume.write_text("Archive this after the resumed task uses it.", encoding="utf-8")
    write_audit(runtime, "z-pending-stop.json", tracked, audit_id="pending-stop")
    changing = observation(
        repo,
        topology="unknown",
        head=OID_A,
        upstream=OID_B,
        blockers=("state_changed_during_observation",),
    )
    stable = observation(repo, topology="remote_ahead", head=OID_A, upstream=OID_B)
    notifier = FakeNotifier(runtime)
    queue = FakeQueue(remote_receipt=receipt(THREAD_A, "git:pending", status="started"))
    adapter = FakeAdapter({str(repo.resolve()): [changing, changing, stable]})
    service = build_service(
        runtime, [tracked], adapter, FakeMutator(), notifier, queue,
    )
    original_state = {
        "schema_version": 2,
        "workspace_id": "watchdog",
        "repo_root": str(repo.resolve()),
        "session_id": THREAD_A,
        "audit_cursor": "a-before.json",
        "last_remote_oid": OID_A,
        "pending_remote_oid": OID_B,
        "pending_remote_detected_at": "2026-09-01T00:00:00Z",
    }
    InstructionStore._atomic_json(service.state_path("watchdog"), original_state)

    first = service.run_once().workspaces[0]

    assert first.status == "completed"
    assert first.stop_count == 0
    assert first.wake is None
    assert first.notifications == ()
    assert queue.remote_calls == []
    assert notifier.events == []
    assert resume.is_file()
    assert (
        json.loads(service.state_path("watchdog").read_text(encoding="utf-8"))
        == original_state
    )

    second = service.run_once().workspaces[0]

    assert second.stop_count == 1
    assert second.wake["status"] == "started"
    assert queue.remote_calls == [(THREAD_A, OID_B, "watchdog")]
    assert resume.is_file()
    assert [event.event_type for event in notifier.events] == ["codex_parked"]
    final_state = json.loads(service.state_path("watchdog").read_text(encoding="utf-8"))
    assert final_state["audit_cursor"] != original_state["audit_cursor"]
    assert final_state["pending_remote_oid"] is None
    assert final_state["last_remote_oid"] == OID_B


def test_newer_remote_oid_does_not_replace_an_enqueued_pending_wake(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    remote_b = observation(repo, topology="remote_ahead", head=OID_A, upstream=OID_B)
    remote_c = observation(repo, topology="remote_ahead", head=OID_A, upstream=OID_C)
    queue = FakeQueue(
        remote_receipts=(
            receipt(THREAD_A, "git:b"),
            receipt(THREAD_A, "git:b", deduplicated=True),
        )
    )
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [remote_b, remote_c]}),
        FakeMutator(),
        FakeNotifier(runtime),
        queue,
    )

    service.run_once()
    service.run_once()

    state = json.loads(service.state_path("watchdog").read_text(encoding="utf-8"))
    assert state["pending_remote_oid"] == OID_B
    assert queue.remote_calls == [
        (THREAD_A, OID_B, "watchdog"),
        (THREAD_A, OID_B, "watchdog"),
    ]


def test_remote_update_wake_does_not_require_parked_rollout_evidence(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    write_audit(runtime, "a-parked.json", tracked, audit_id="parked")
    write_rollout(
        runtime / "codex-home", tracked, "turn-parked", later_turn_id="turn-active",
    )
    remote = observation(repo, topology="remote_ahead", head=OID_A, upstream=OID_B)
    notifier = FakeNotifier(runtime)
    mutator = FakeMutator()
    queue = FakeQueue(remote_receipt=receipt(THREAD_A, "git:remote"))
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [remote]}),
        mutator,
        notifier,
        queue,
    )

    result = service.run_once().workspaces[0]

    assert result.wake["status"] == "enqueued"
    assert queue.remote_calls == [(THREAD_A, OID_B, "watchdog")]
    assert notifier.events == []


def test_later_queue_evidence_does_not_block_a_new_remote_oid_wake(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    write_audit(runtime, "a-parked.json", tracked, audit_id="parked")
    write_rollout(runtime / "codex-home", tracked, "turn-parked")
    InstructionStore._atomic_json(
        runtime / "wake" / "records" / "later.json",
        {
            "schema_version": 2,
            "instruction_id": "resume:later",
            "thread_id": THREAD_A,
            "state": "enqueued",
            "created_at": "2026-09-01T00:00:04Z",
            "completed_at": "2026-09-01T00:00:04.100Z",
        },
    )
    remote = observation(repo, topology="remote_ahead", head=OID_A, upstream=OID_B)
    notifier = FakeNotifier(runtime)
    mutator = FakeMutator()
    queue = FakeQueue(remote_receipt=receipt(THREAD_A, "git:new-remote"))
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [remote]}),
        mutator,
        notifier,
        queue,
    )

    result = service.run_once().workspaces[0]

    assert result.wake["status"] == "enqueued"
    assert queue.remote_calls == [(THREAD_A, OID_B, "watchdog")]


def test_activity_appearing_after_pending_persist_does_not_enable_git_mutation(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    parked_audit = write_audit(runtime, "a-parked.json", tracked, audit_id="parked")
    rollout = write_rollout(runtime / "codex-home", tracked, "turn-parked")
    remote = observation(repo, topology="remote_ahead", head=OID_A, upstream=OID_B)
    mutator = FakeMutator()
    notifier = FakeNotifier(runtime)
    queue = FakeQueue(remote_receipt=receipt(THREAD_A, "git:racing"))
    state_path = runtime / "service" / "state" / (f"{sha256_text('watchdog')}.json")
    InstructionStore._atomic_json(
        state_path,
        {
            "schema_version": 1,
            "workspace_id": "watchdog",
            "repo_root": str(repo.resolve()),
            "session_id": THREAD_A,
            "audit_cursor": parked_audit.name,
            "pending_remote_oid": None,
            "pending_remote_detected_at": None,
        },
    )
    injected_activity = False

    def racing_writer(path: Path, value: Dict[str, object]) -> None:
        nonlocal injected_activity
        InstructionStore._atomic_json(path, value)
        if not injected_activity and value.get("pending_remote_oid") == OID_B:
            injected_activity = True
            later = {
                "timestamp": "2026-09-01T00:00:04Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-active"},
            }
            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(later) + "\n")

    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [remote]}),
        mutator,
        notifier,
        queue,
        atomic_writer=racing_writer,
    )

    result = service.run_once().workspaces[0]

    assert injected_activity is True
    assert result.status == "completed"
    assert result.wake["status"] == "enqueued"
    assert queue.remote_calls == [(THREAD_A, OID_B, "watchdog")]


def test_pending_remote_oid_is_reconciled_when_codex_already_synchronized(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    write_audit(runtime, "a-parked.json", tracked, audit_id="parked")
    write_rollout(runtime / "codex-home", tracked, "turn-parked")
    equal = observation(repo, head=OID_B)
    adapter = FakeAdapter({str(repo.resolve()): [equal]})
    queue = FakeQueue()
    service = build_service(
        runtime, [tracked], adapter, FakeMutator(), FakeNotifier(runtime), queue,
    )
    InstructionStore._atomic_json(
        service.state_path("watchdog"),
        {
            "schema_version": 1,
            "workspace_id": "watchdog",
            "repo_root": str(repo.resolve()),
            "session_id": THREAD_A,
            "audit_cursor": None,
            "pending_remote_oid": OID_B,
            "pending_remote_detected_at": "2026-09-01T00:00:00Z",
        },
    )

    result = service.run_once()

    assert result.workspaces[0].wake is None
    assert queue.remote_calls == []
    state = json.loads(service.state_path("watchdog").read_text(encoding="utf-8"))
    assert state["pending_remote_oid"] is None
    assert state["last_remote_oid"] == OID_B


def test_local_push_oid_advance_updates_baseline_without_remote_wake(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    equal = observation(repo, head=OID_B)
    queue = FakeQueue()
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [equal]}),
        FakeMutator(),
        FakeNotifier(runtime),
        queue,
    )
    InstructionStore._atomic_json(
        service.state_path("watchdog"),
        {
            "schema_version": 2,
            "workspace_id": "watchdog",
            "repo_root": str(repo.resolve()),
            "session_id": THREAD_A,
            "audit_cursor": None,
            "last_remote_oid": OID_A,
            "pending_remote_oid": None,
            "pending_remote_detected_at": None,
        },
    )

    result = service.run_once().workspaces[0]

    assert result.wake is None
    assert queue.remote_calls == []
    state = json.loads(service.state_path("watchdog").read_text(encoding="utf-8"))
    assert state["last_remote_oid"] == OID_B


def test_remote_wake_keeps_temporal_prompt_visible_for_fixed_wake_turn(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    write_audit(runtime, "a-parked.json", tracked, audit_id="parked")
    write_rollout(runtime / "codex-home", tracked, "turn-parked")
    runtime.mkdir(exist_ok=True)
    (runtime / "resume_prompt.md").write_text("Continue safely.", encoding="utf-8")
    remote = observation(repo, topology="remote_ahead", head=OID_A, upstream=OID_B)
    queue = FakeQueue(
        resume_receipt=receipt(THREAD_A, "resume:one"),
        remote_receipt=receipt(THREAD_A, "git:later"),
    )
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [remote]}),
        FakeMutator(),
        FakeNotifier(runtime),
        queue,
    )

    result = service.run_once()

    assert result.workspaces[0].wake["kind"] == "remote_update"
    assert queue.resume_calls == []
    assert queue.remote_calls == [(THREAD_A, OID_B, "watchdog")]
    assert (runtime / "resume_prompt.md").read_text(encoding="utf-8") == (
        "Continue safely."
    )
    state = json.loads(service.state_path("watchdog").read_text(encoding="utf-8"))
    assert state["pending_remote_oid"] == OID_B


def test_untracked_remote_update_wakes_codex_without_watchdog_mutation(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    remote = observation(
        repo, topology="remote_ahead", head=OID_A, upstream=OID_B, untracked=True,
    )
    notifier = FakeNotifier(runtime)
    mutator = FakeMutator()
    queue = FakeQueue(remote_receipt=receipt(THREAD_A, "git:untracked"))
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [remote]}),
        mutator,
        notifier,
        queue,
    )

    result = service.run_once()

    assert not mutator.preserve_calls
    assert queue.remote_calls == [(THREAD_A, OID_B, "watchdog")]
    assert notifier.events == []


def test_matching_stop_does_not_notify_for_untracked_files(tmp_path: Path,) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    write_audit(runtime, "a-stop.json", tracked, audit_id="stop")
    write_rollout(runtime / "codex-home", tracked, "turn-stop")
    untracked = observation(repo, untracked=True)
    mutator = FakeMutator()
    notifier = FakeNotifier(runtime)
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [untracked, untracked]}),
        mutator,
        notifier,
        FakeQueue(),
        replay_latest_stop=True,
    )

    result = service.run_once().workspaces[0]

    assert not mutator.preserve_calls
    assert result.final_git["untracked_present"] is True
    assert [event.event_type for event in notifier.events] == ["codex_parked"]
    assert [event.subject for event in notifier.events] == [
        "[Codex Watchdog] repo stopped"
    ]
    assert {event.workspace_id for event in notifier.events} == {"watchdog"}


@pytest.mark.parametrize(
    "git_kwargs",
    (
        {"dirty": True},
        {"untracked": True},
        {"topology": "local_ahead", "head": OID_B, "upstream": OID_A},
    ),
    ids=("dirty-tracked", "untracked", "local-ahead"),
)
def test_ordinary_local_git_state_is_observed_without_notification(
    tmp_path: Path, git_kwargs,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    git_state = observation(repo, **git_kwargs)
    notifier = FakeNotifier(runtime)
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [git_state]}),
        FakeMutator(),
        notifier,
        FakeQueue(),
    )

    result = service.run_once().workspaces[0]

    assert result.final_git["dirty_tracked"] is git_state.dirty_tracked
    assert result.final_git["untracked_present"] is git_state.untracked_present
    assert result.final_git["topology"] == git_state.topology
    assert notifier.events == []


def test_genuine_git_blocker_still_notifies(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    blocked = observation(repo, blockers=("merge_in_progress",))
    notifier = FakeNotifier(runtime)
    service = build_service(
        runtime,
        [tracked],
        FakeAdapter({str(repo.resolve()): [blocked]}),
        FakeMutator(),
        notifier,
        FakeQueue(),
    )

    result = service.run_once().workspaces[0]

    assert result.final_git["blockers"] == ["merge_in_progress"]
    assert [event.event_type for event in notifier.events] == ["git_attention"]
    assert notifier.events[0].subject == "[Codex Watchdog] repo needs Git attention"


def test_workspace_failure_is_isolated_and_result_is_json_safe(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    first_repo = tmp_path / "first"
    second_repo = tmp_path / "second"
    first_repo.mkdir()
    second_repo.mkdir()
    first = workspace(first_repo, "a-first", THREAD_A)
    second = workspace(second_repo, "z-second", THREAD_B)
    adapter = FakeAdapter(
        {
            str(first_repo.resolve()): [RuntimeError("private path detail")],
            str(second_repo.resolve()): [observation(second_repo)],
        }
    )
    service = build_service(
        runtime,
        [second, first],
        adapter,
        FakeMutator(),
        FakeNotifier(runtime),
        FakeQueue(),
    )

    result = service.run_once()
    encoded = json.dumps(result.to_dict())

    assert [item.workspace_id for item in result.workspaces] == [
        "a-first",
        "z-second",
    ]
    assert result.workspaces[0].status == "error"
    assert result.workspaces[1].status == "completed"
    assert "private path detail" not in encoded
    assert not result.ok


def test_ambiguous_resume_is_retained_while_exact_remote_wake_proceeds(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    first_repo = tmp_path / "first"
    second_repo = tmp_path / "second"
    first_repo.mkdir()
    second_repo.mkdir()
    runtime.mkdir()
    resume = runtime / "resume_prompt.md"
    resume.write_text("Needs an exact target.", encoding="utf-8")
    first = workspace(first_repo, "a-first", THREAD_A)
    second = workspace(second_repo, "z-second", THREAD_B)
    notifier = FakeNotifier(runtime)
    queue = FakeQueue(remote_receipt=receipt(THREAD_A, "git:resume-ambiguous"))
    service = build_service(
        runtime,
        [first, second],
        FakeAdapter(
            {
                str(first_repo.resolve()): [
                    observation(
                        first_repo,
                        topology="remote_changed",
                        head=OID_A,
                        upstream=OID_B,
                    )
                ],
                str(second_repo.resolve()): [observation(second_repo)],
            }
        ),
        FakeMutator(),
        notifier,
        queue,
    )

    result = service.run_once()

    assert result.ok
    assert resume.exists()
    assert not queue.resume_calls
    assert queue.remote_calls == [(THREAD_A, OID_B, "a-first")]
    assert [event.event_type for event in notifier.events] == [
        "resume_prompt_ambiguous"
    ]


def test_stop_written_during_cycle_remains_after_scanned_cursor(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)
    clean = observation(repo)

    class RacingAdapter:
        def __init__(self) -> None:
            self.wrote_stop = False

        def observe(self, repo_root: Path) -> GitObservation:
            if not self.wrote_stop:
                self.wrote_stop = True
                write_audit(
                    runtime, "0002-raced-stop.json", tracked, audit_id="raced-stop",
                )
            return clean

    service = build_service(
        runtime,
        [tracked],
        RacingAdapter(),
        FakeMutator(),
        FakeNotifier(runtime),
        FakeQueue(),
    )

    first = service.run_once()
    second = service.run_once()
    third = service.run_once()

    assert first.workspaces[0].stop_count == 0
    assert second.workspaces[0].stop_count == 1
    assert second.workspaces[0].stop_audit_id == "raced-stop"
    assert third.workspaces[0].stop_count == 0


def test_run_is_immediate_uses_injected_sleep_and_handles_ctrl_c(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    sleeps: List[float] = []
    emitted = []
    service = MvpWatchdogService(
        runtime,
        registry=FakeRegistry([]),
        git_adapter=FakeAdapter({}),
        notifier=FakeNotifier(runtime),
        queue_dispatcher=FakeQueue(),
        sleep=sleeps.append,
    )

    assert service.run(7.5, max_cycles=2, emit=emitted.append) == 0
    assert len(emitted) == 2
    assert sleeps == [7.5]
    assert all(json.dumps(value) for value in emitted)

    interrupted = MvpWatchdogService(
        tmp_path / "interrupt-runtime",
        registry=FakeRegistry([]),
        git_adapter=FakeAdapter({}),
        notifier=FakeNotifier(runtime),
        queue_dispatcher=FakeQueue(),
        sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert interrupted.run(1.0) == 0


def test_run_owns_slack_reply_listener_for_foreground_lifetime(tmp_path: Path) -> None:
    calls = []

    class FakeRelay:
        def start(self):
            calls.append("start")

        def close(self):
            calls.append("close")

    service = MvpWatchdogService(
        tmp_path / "runtime",
        registry=FakeRegistry([]),
        git_adapter=FakeAdapter({}),
        notifier=FakeNotifier(tmp_path),
        queue_dispatcher=FakeQueue(),
        slack_reply_relay=FakeRelay(),
        sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert service.run(1.0) == 0
    assert calls == ["start", "close"]


def test_cycle_reports_the_fresh_discovery_snapshot_summary(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = workspace(repo)

    class DiscoveryRegistry(FakeRegistry):
        path = runtime / "service" / "workspace-discovery.json"
        last_snapshot = None

        def list_workspaces(self) -> List[TrackedWorkspace]:
            self.last_snapshot = SimpleNamespace(
                status="partial",
                windows=(object(), object()),
                effective_workspaces=(tracked,),
                issues=("remote_agent_required",),
            )
            return super().list_workspaces()

    registry = DiscoveryRegistry([tracked])
    service = MvpWatchdogService(
        runtime,
        registry=registry,
        git_adapter=FakeAdapter({str(repo.resolve()): [observation(repo)]}),
        notifier=FakeNotifier(runtime),
        queue_dispatcher=FakeQueue(),
        codex_home=runtime / "codex-home",
    )

    result = service.run_once()

    assert result.discovery == {
        "status": "partial",
        "window_count": 2,
        "tracked_workspace_count": 1,
        "remote_workspace_count": 0,
        "issues": ["remote_agent_required"],
        "path": str(registry.path),
    }
    assert result.to_dict()["discovery"] == result.discovery


def test_remote_ssh_completion_sends_exact_output_once_with_locality_label(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    remote_window = SimpleNamespace(
        locality="remote_ssh",
        remote_authority="ssh-remote+hpc-login.example.edu",
        workspace_path="/home/operator/ProjectAlpha",
        workspace_storage_key="a" * 32,
        session_id=THREAD_A,
        session_candidates=(THREAD_A,),
    )

    class RemoteDiscoveryRegistry(FakeRegistry):
        path = runtime / "service" / "workspace-discovery.json"
        last_snapshot = None

        def list_workspaces(self) -> List[TrackedWorkspace]:
            self.last_snapshot = SimpleNamespace(
                status="partial",
                windows=(remote_window,),
                effective_workspaces=(),
                issues=("remote_agent_required",),
            )
            return []

    output = "Exact final output from Remote-SSH ProjectAlpha."
    probe = {
        "status": "ok",
        "session_id": THREAD_A,
        "repo_path": "/home/operator/ProjectAlpha",
        "git": {
            "status": "observed",
            "topology": "equal",
            "head_oid": OID_A,
            "upstream_oid": OID_A,
            "dirty_tracked": False,
            "untracked_present": False,
            "blockers": [],
        },
        "completion": {
            "turn_id": "remote-turn",
            "completed_at": "2026-09-02T00:00:00Z",
            "final_output": output,
            "final_output_sha256": sha256_text(output),
            "final_output_chars": len(output),
        },
    }
    adapter = FakeRemoteSshAdapter([probe])
    notifier = FakeNotifier(runtime)
    service = MvpWatchdogService(
        runtime,
        registry=RemoteDiscoveryRegistry([]),
        git_adapter=FakeAdapter({}),
        notifier=notifier,
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=adapter,
        codex_home=runtime / "codex-home",
        clock=lambda: datetime(2026, 9, 2, 1, tzinfo=timezone.utc),
    )
    first = service.run_once()
    second = service.run_once()

    assert first.workspaces[0].status == "completed"
    assert first.workspaces[0].stop_count == 1
    assert second.workspaces[0].stop_count == 0
    assert [event.event_type for event in notifier.events] == ["codex_parked"]
    assert (
        notifier.events[0].subject
        == "[Codex Watchdog] ProjectAlpha @ hpc-login stopped"
    )
    assert notifier.events[0].message.endswith(output)
    assert adapter.calls[0][0].expected_session_ids == (THREAD_A,)
    observation = json.loads(
        Path(first.workspaces[0].observation_path).read_text(encoding="utf-8")
    )
    assert output not in json.dumps(observation)
    assert "final_output" not in observation["completion"]


def test_remote_ssh_ordinary_git_state_does_not_notify(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    remote_window = SimpleNamespace(
        locality="remote_ssh",
        remote_authority="ssh-remote+hpc-login.example.edu",
        workspace_path="/home/operator/ProjectAlpha",
        workspace_storage_key="c" * 32,
    )

    class RemoteDiscoveryRegistry(FakeRegistry):
        last_snapshot = None

        def list_workspaces(self) -> List[TrackedWorkspace]:
            self.last_snapshot = SimpleNamespace(
                status="partial",
                windows=(remote_window,),
                effective_workspaces=(),
                issues=("remote_agent_required",),
            )
            return []

    probe = {
        "status": "ok",
        "session_id": THREAD_A,
        "repo_path": "/home/operator/ProjectAlpha",
        "git": {
            "status": "observed",
            "topology": "local_ahead",
            "head_oid": OID_B,
            "upstream_oid": OID_A,
            "dirty_tracked": True,
            "untracked_present": True,
            "blockers": [],
        },
        "completion": None,
    }
    notifier = FakeNotifier(runtime)
    service = MvpWatchdogService(
        runtime,
        registry=RemoteDiscoveryRegistry([]),
        git_adapter=FakeAdapter({}),
        notifier=notifier,
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=FakeRemoteSshAdapter([probe]),
        codex_home=runtime / "codex-home",
    )

    result = service.run_once().workspaces[0]

    assert result.final_git["topology"] == "local_ahead"
    assert result.final_git["dirty_tracked"] is True
    assert result.final_git["untracked_present"] is True
    assert notifier.events == []


def test_remote_ssh_unreachable_adapter_still_notifies(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    notifier = FakeNotifier(runtime)
    service = MvpWatchdogService(
        runtime,
        registry=FakeRemoteRegistry(runtime),
        git_adapter=FakeAdapter({}),
        notifier=notifier,
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=FakeRemoteSshAdapter(
            [{"status": "unavailable", "reason": "ssh_authentication_failed"}]
        ),
        codex_home=runtime / "codex-home",
    )

    first = service.run_once().workspaces[0]
    second = service.run_once().workspaces[0]

    assert first.status == "error"
    assert first.notifications == ()
    assert second.status == "error"
    assert [event.event_type for event in notifier.events] == [
        "remote_adapter_attention"
    ]
    assert (
        notifier.events[0].subject
        == "[Codex Watchdog] ProjectAlpha @ hpc-login monitoring lost"
    )


def test_remote_ssh_one_failed_check_does_not_report_an_outage(tmp_path: Path,) -> None:
    runtime = tmp_path / "runtime"
    healthy = {
        "status": "ok",
        "session_id": THREAD_A,
        "git": {
            "status": "observed",
            "topology": "equal",
            "head_oid": OID_A,
            "upstream_oid": OID_A,
            "blockers": [],
        },
        "completion": None,
    }
    notifier = FakeNotifier(runtime)
    service = MvpWatchdogService(
        runtime,
        registry=FakeRemoteRegistry(runtime),
        git_adapter=FakeAdapter({}),
        notifier=notifier,
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=FakeRemoteSshAdapter(
            [{"status": "unavailable", "reason": "remote_ssh_failed"}, healthy,]
        ),
        codex_home=runtime / "codex-home",
    )

    service.run_once()
    service.run_once()

    assert notifier.events == []
    remote_workspace_id = service._remote_targets(service.registry.last_snapshot)[
        0
    ].workspace_id
    state = json.loads(
        service.state_path(remote_workspace_id).read_text(encoding="utf-8")
    )
    assert state["connection_status"] == "connected"
    assert state["consecutive_failures"] == 0


def test_remote_ssh_loss_delivery_retries_until_handled(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    notifier = FakeNotifier(runtime, status="delivery_failed", channel="local_audit")
    service = MvpWatchdogService(
        runtime,
        registry=FakeRemoteRegistry(runtime),
        git_adapter=FakeAdapter({}),
        notifier=notifier,
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=FakeRemoteSshAdapter(
            [{"status": "unavailable", "reason": "remote_ssh_failed"}]
        ),
        codex_home=runtime / "codex-home",
    )

    service.run_once()
    service.run_once()
    third = service.run_once().workspaces[0]

    assert [event.event_type for event in notifier.events] == [
        "remote_adapter_attention",
        "remote_adapter_attention",
    ]
    assert third.notifications[0]["status"] == "delivery_failed"
    state = json.loads(Path(third.state_path).read_text(encoding="utf-8"))
    assert state["connection_alert_pending"] is True


def test_remote_ssh_recovery_notifies_and_a_later_outage_notifies_again(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    unavailable = {"status": "unavailable", "reason": "remote_ssh_failed"}
    healthy = {
        "status": "ok",
        "session_id": THREAD_A,
        "git": {
            "status": "observed",
            "topology": "equal",
            "head_oid": OID_A,
            "upstream_oid": OID_A,
            "blockers": [],
        },
        "completion": None,
    }
    notifier = FakeNotifier(runtime)
    service = MvpWatchdogService(
        runtime,
        registry=FakeRemoteRegistry(runtime),
        git_adapter=FakeAdapter({}),
        notifier=notifier,
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=FakeRemoteSshAdapter(
            [unavailable, unavailable, healthy, unavailable, unavailable]
        ),
        codex_home=runtime / "codex-home",
    )

    results = [service.run_once().workspaces[0] for _ in range(5)]

    assert [event.event_type for event in notifier.events] == [
        "remote_adapter_attention",
        "remote_adapter_recovered",
        "remote_adapter_attention",
    ]
    assert results[2].notifications[0]["status"] == "sent"
    assert (
        notifier.events[1].subject
        == "[Codex Watchdog] ProjectAlpha @ hpc-login monitoring restored"
    )
    assert (
        notifier.events[0].transition_fingerprint
        != notifier.events[2].transition_fingerprint
    )


def test_remote_vscode_window_disappearance_and_return_are_notified(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    remote_window = SimpleNamespace(
        locality="remote_ssh",
        tracking_status="remote_adapter",
        remote_authority="ssh-remote+gpu-lab-personal",
        workspace_path="/home/operator/LocalCodexWatchDog",
        workspace_storage_key="e" * 32,
    )

    class ChangingRemoteRegistry(FakeRegistry):
        path = runtime / "service" / "workspace-discovery.json"
        last_snapshot = None

        def __init__(self) -> None:
            super().__init__([])
            self.windows = [(remote_window,), (), (), (remote_window,)]

        def list_workspaces(self) -> List[TrackedWorkspace]:
            current = self.windows.pop(0)
            self.last_snapshot = SimpleNamespace(
                status="ok", windows=current, effective_workspaces=(), issues=(),
            )
            return []

    healthy = {
        "status": "ok",
        "session_id": THREAD_A,
        "git": {
            "status": "observed",
            "topology": "equal",
            "head_oid": OID_A,
            "upstream_oid": OID_A,
            "blockers": [],
        },
        "completion": None,
    }
    notifier = FakeNotifier(runtime)
    service = MvpWatchdogService(
        runtime,
        registry=ChangingRemoteRegistry(),
        git_adapter=FakeAdapter({}),
        notifier=notifier,
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=FakeRemoteSshAdapter([healthy, healthy]),
        codex_home=runtime / "codex-home",
    )

    results = [service.run_once() for _ in range(4)]

    assert [len(result.workspaces) for result in results] == [1, 1, 1, 1]
    assert results[1].workspaces[0].notifications == ()
    assert [event.event_type for event in notifier.events] == [
        "remote_adapter_attention",
        "remote_adapter_recovered",
    ]
    assert "no longer sees" in notifier.events[0].message
    assert results[3].workspaces[0].status == "completed"


def test_remote_ssh_git_blocker_still_notifies(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    notifier = FakeNotifier(runtime)
    service = MvpWatchdogService(
        runtime,
        registry=FakeRemoteRegistry(runtime),
        git_adapter=FakeAdapter({}),
        notifier=notifier,
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=FakeRemoteSshAdapter(
            [
                {
                    "status": "ok",
                    "session_id": THREAD_A,
                    "repo_path": "/home/operator/ProjectAlpha",
                    "git": {
                        "status": "blocked",
                        "topology": "unknown",
                        "head_oid": OID_A,
                        "upstream_oid": OID_A,
                        "dirty_tracked": False,
                        "untracked_present": False,
                        "blockers": ["merge_in_progress"],
                    },
                    "completion": None,
                }
            ]
        ),
        codex_home=runtime / "codex-home",
    )

    service.run_once()

    assert [event.event_type for event in notifier.events] == ["git_attention"]
    assert (
        notifier.events[0].subject
        == "[Codex Watchdog] ProjectAlpha @ hpc-login needs Git attention"
    )


def test_remote_ssh_uncertain_wake_still_notifies(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    remote_git = {
        "status": "observed",
        "topology": "remote_changed",
        "head_oid": OID_A,
        "upstream_oid": OID_B,
        "dirty_tracked": False,
        "untracked_present": False,
        "blockers": [],
    }
    base_probe = {
        "status": "ok",
        "session_id": THREAD_A,
        "repo_path": "/home/operator/ProjectAlpha",
        "git": remote_git,
        "completion": None,
    }
    notifier = FakeNotifier(runtime)
    service = MvpWatchdogService(
        runtime,
        registry=FakeRemoteRegistry(runtime),
        git_adapter=FakeAdapter({}),
        notifier=notifier,
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=FakeRemoteSshAdapter(
            [base_probe, {**base_probe, "wake": {"state": "uncertain"}}]
        ),
        codex_home=runtime / "codex-home",
    )

    result = service.run_once().workspaces[0]

    assert result.wake["state"] == "uncertain"
    assert [event.event_type for event in notifier.events] == ["wake_uncertain"]
    assert (
        notifier.events[0].subject
        == "[Codex Watchdog] ProjectAlpha @ hpc-login wake is uncertain"
    )


def test_remote_ssh_git_change_queues_exact_thread_once_and_reconciles_start(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    remote_window = SimpleNamespace(
        locality="remote_ssh",
        remote_authority="ssh-remote+hpc-login.example.edu",
        workspace_path="/home/operator/ProjectAlpha",
        workspace_storage_key="b" * 32,
    )

    class RemoteDiscoveryRegistry(FakeRegistry):
        last_snapshot = None

        def list_workspaces(self) -> List[TrackedWorkspace]:
            self.last_snapshot = SimpleNamespace(
                status="partial",
                windows=(remote_window,),
                effective_workspaces=(),
                issues=("remote_agent_required",),
            )
            return []

    remote_git = {
        "status": "observed",
        "topology": "remote_changed",
        "head_oid": OID_A,
        "upstream_oid": OID_B,
        "dirty_tracked": False,
        "untracked_present": False,
        "blockers": [],
    }
    base_probe = {
        "status": "ok",
        "session_id": THREAD_A,
        "repo_path": "/home/operator/ProjectAlpha",
        "git": remote_git,
        "completion": None,
    }
    adapter = FakeRemoteSshAdapter(
        [
            base_probe,
            {**base_probe, "wake": {"state": "enqueued"}},
            {**base_probe, "wake": {"state": "started"}},
        ]
    )
    service = MvpWatchdogService(
        runtime,
        registry=RemoteDiscoveryRegistry([]),
        git_adapter=FakeAdapter({}),
        notifier=FakeNotifier(runtime),
        queue_dispatcher=FakeQueue(),
        remote_ssh_adapter=adapter,
        codex_home=runtime / "codex-home",
    )

    first = service.run_once().workspaces[0]
    second = service.run_once().workspaces[0]

    wake_calls = [kwargs for _target, kwargs in adapter.calls if "wake" in kwargs]
    assert len(wake_calls) == 1
    assert wake_calls[0]["wake"]["prompt"].startswith("You were resumed by WatchDog")
    assert first.wake["state"] == "enqueued"
    assert second.wake["state"] == "started"
    state = json.loads(Path(second.state_path).read_text(encoding="utf-8"))
    assert state["pending_instruction_id"] is None
    assert state["last_remote_oid"] == OID_B


def test_discovery_error_makes_an_empty_cycle_unsuccessful(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"

    class DiscoveryErrorRegistry(FakeRegistry):
        path = runtime / "service" / "workspace-discovery.json"
        last_snapshot = None

        def list_workspaces(self) -> List[TrackedWorkspace]:
            self.last_snapshot = SimpleNamespace(
                status="error",
                windows=(),
                effective_workspaces=(),
                issues=("vscode_live_status_unavailable",),
            )
            return []

    service = MvpWatchdogService(
        runtime,
        registry=DiscoveryErrorRegistry([]),
        git_adapter=FakeAdapter({}),
        notifier=FakeNotifier(runtime),
        queue_dispatcher=FakeQueue(),
        codex_home=runtime / "codex-home",
    )

    result = service.run_once()

    assert result.status == "completed"
    assert result.discovery["status"] == "error"
    assert result.ok is False


def test_workspace_session_change_retains_resume_prompt_and_defers_wake(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime.mkdir()
    resume = runtime / "resume_prompt.md"
    resume.write_text("Continue safely.", encoding="utf-8")
    tracked = workspace(repo)

    class ChangedRegistry(FakeRegistry):
        @staticmethod
        def is_current(_workspace: TrackedWorkspace) -> bool:
            return False

    queue = FakeQueue()
    notifier = FakeNotifier(runtime)
    service = MvpWatchdogService(
        runtime,
        registry=ChangedRegistry([tracked]),
        git_adapter=FakeAdapter({str(repo.resolve()): [observation(repo)]}),
        notifier=notifier,
        queue_dispatcher=queue,
        codex_home=runtime / "codex-home",
    )

    result = service.run_once().workspaces[0]

    assert result.wake["status"] == "deferred_workspace_changed"
    assert resume.is_file()
    assert queue.resume_calls == []
    assert [event.event_type for event in notifier.events] == ["wake_deferred"]


def test_mvp_uses_codex_home_environment_with_injected_queue(
    tmp_path: Path, monkeypatch
) -> None:
    codex_home = (tmp_path / "custom-codex-home").resolve()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    service = MvpWatchdogService(
        tmp_path / "runtime", registry=FakeRegistry([]), queue_dispatcher=FakeQueue(),
    )

    assert service.codex_home == codex_home
