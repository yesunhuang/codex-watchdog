"""A genuine activation must not pretend an outstanding Git tip was delivered."""
import json
import sqlite3

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.queue_wake import QueueWakeDispatcher
from codex_watchdog.remote_ssh import RemoteSshTarget
from codex_watchdog.storage import InstructionStore
from test_mvp_service import (
    FakeAdapter, FakeMutator, FakeNotifier, FakeQueue, FakeRegistry,
    OID_A, OID_B, OID_C, THREAD_A, build_service, observation, receipt, workspace,
)
from test_queue_wake import create_queue_database


ACTIVE = dict(status="ACTIVE", activated=True, notifications=())


def local_case(tmp_path, *, queue=None, upstream=OID_B, head=OID_A):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    runtime = tmp_path / "runtime"
    tracked = workspace(repo)
    queue = queue or FakeQueue(remote_receipt=receipt(THREAD_A, "git:fixture"))
    adapter = FakeAdapter({str(repo.resolve()): [observation(
        repo, topology="equal" if head == upstream else "remote_ahead",
        head=head, upstream=upstream)]})
    service = build_service(runtime, [tracked], adapter, FakeMutator(),
                            FakeNotifier(runtime), queue)
    service._lifecycle_state = lambda _: dict(ACTIVE)
    return service, tracked, adapter, queue


def save_local(service, tracked, *, attempted=OID_A):
    InstructionStore._atomic_json(service.state_path(tracked.workspace_id), dict(
        schema_version=2, workspace_id=tracked.workspace_id,
        repo_root=str(tracked.repo_root), session_id=tracked.session_id,
        audit_cursor=None, last_remote_oid=attempted,
        last_attempted_remote_oid=attempted,
        pending_remote_oid=None, pending_remote_detected_at=None))


def test_activation_dispatches_current_unattempted_tip_once_across_restart(tmp_path):
    service, tracked, adapter, queue = local_case(tmp_path)
    save_local(service, tracked)
    first = service.run_once().workspaces[0]
    assert first.wake["status"] == "enqueued"
    assert queue.remote_calls == [(THREAD_A, OID_B, tracked.workspace_id)]
    service.run_once()
    restarted, _, _, _ = local_case(tmp_path, queue=queue)
    restarted.run_once()
    assert len(queue.remote_calls) == 1
    adapter.observations[str(tracked.repo_root)] = [observation(
        tracked.repo_root, topology="remote_ahead", upstream=OID_C)]
    service.run_once()
    assert [call[1] for call in queue.remote_calls] == [OID_B, OID_C]


@pytest.mark.parametrize("status", ["coalesced", "deferred"])
def test_activation_does_not_spend_tip_before_deferred_admission(tmp_path, status):
    queue = FakeQueue(remote_receipts=[receipt(THREAD_A, "git:fixture", status=status),
                                       receipt(THREAD_A, "git:fixture")])
    service, tracked, _, _ = local_case(tmp_path, queue=queue)
    save_local(service, tracked)
    assert service.run_once().workspaces[0].wake["status"] == status
    state = json.loads(service.state_path(tracked.workspace_id).read_text())
    assert state["last_attempted_remote_oid"] == OID_A
    service._lifecycle_state = lambda _: dict(ACTIVE, activated=False)
    assert service.run_once().workspaces[0].wake["status"] == "enqueued"
    assert len(queue.remote_calls) == 2


def test_dormant_session_stays_quiet_until_genuine_activation(tmp_path):
    service, tracked, _, queue = local_case(tmp_path)
    save_local(service, tracked)
    service._lifecycle_state = lambda _: dict(status="DORMANT", notifications=())
    assert service.run_once().workspaces[0].wake is None
    assert queue.remote_calls == []
    service._lifecycle_state = lambda _: dict(ACTIVE)
    assert service.run_once().workspaces[0].wake["status"] == "enqueued"
    assert len(queue.remote_calls) == 1


def test_equal_refs_do_not_invent_a_git_attempt_on_activation(tmp_path):
    service, tracked, _, queue = local_case(tmp_path, head=OID_B)
    save_local(service, tracked)
    assert service.run_once().workspaces[0].wake is None
    state = json.loads(service.state_path(tracked.workspace_id).read_text())
    assert state["last_attempted_remote_oid"] == OID_A
    assert state["last_remote_oid"] == OID_B
    assert queue.remote_calls == []


@pytest.mark.parametrize("attempted", [OID_A, OID_B], ids=["state-behind", "state-current"])
def test_activation_preserves_real_uncertain_claim_but_later_tip_proceeds(tmp_path, attempted):
    home = tmp_path / "home"
    home.mkdir()
    db = home / "queue_1.sqlite"
    create_queue_database(db)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE INDEX queued_items_thread_order_idx ON queued_items(thread_id)")
    calls = []

    def unavailable(argv, **kwargs):
        calls.append(argv)
        raise OSError("uncertain fixture transport")

    queue = QueueWakeDispatcher(tmp_path / "runtime", codex_home=home,
                                codex_executable="fixture-codex", runner=unavailable,
                                queue_database=db)
    service, tracked, adapter, _ = local_case(tmp_path, queue=queue)
    identifier = "git:" + sha256_text(tracked.workspace_id)[:16] + ":" + OID_B
    assert queue.dispatch_remote_update(THREAD_A, OID_B, tracked.workspace_id).status == "uncertain"
    record = queue.records / (sha256_text(identifier) + ".json")
    original = record.read_bytes()
    save_local(service, tracked, attempted=attempted)
    wake = service.run_once().workspaces[0].wake
    if attempted == OID_B:
        assert wake is None
    else:
        assert wake["status"] == "uncertain" and wake["deduplicated"]
    assert record.read_bytes() == original and len(calls) == 1
    adapter.observations[str(tracked.repo_root)] = [observation(
        tracked.repo_root, topology="remote_ahead", upstream=OID_C)]
    assert service.run_once().workspaces[0].wake["status"] == "uncertain"
    assert record.read_bytes() == original and len(calls) == 2


class RemoteFixture:
    def __init__(self, *, status="enqueued"):
        self.upstream = OID_B
        self.status = status
        self.attempts = []

    def probe(self, target, **kwargs):
        result = dict(status="ok", session_id=THREAD_A, completion=None,
                      git=dict(status="observed", topology="remote_ahead", head_oid=OID_A,
                               upstream_oid=self.upstream, blockers=[]))
        if "wake" in kwargs:
            self.attempts.append(kwargs["wake"]["instruction_id"])
            result["wake"] = dict(state=self.status)
        return result


def remote_case(tmp_path, adapter):
    service = MvpWatchdogService(tmp_path, registry=FakeRegistry([]),
        git_adapter=FakeAdapter({}), notifier=FakeNotifier(tmp_path),
        queue_dispatcher=FakeQueue(), remote_ssh_adapter=adapter,
        codex_home=tmp_path / "home")
    target = RemoteSshTarget("ssh-remote+fixture", "/home/user/repo", "a" * 32)
    return service, target


def test_remote_activation_dispatches_current_tip_once_across_restart(tmp_path):
    adapter = RemoteFixture()
    service, target = remote_case(tmp_path, adapter)
    service._run_remote_owned(target, "first", lifecycle=dict(ACTIVE))
    assert len(adapter.attempts) == 1
    restarted, target = remote_case(tmp_path, adapter)
    restarted._run_remote_owned(target, "second", lifecycle=dict(ACTIVE))
    assert len(adapter.attempts) == 1
    adapter.upstream = OID_C
    restarted._run_remote_owned(target, "third", lifecycle=dict(ACTIVE))
    assert len(adapter.attempts) == 2 and adapter.attempts[0] != adapter.attempts[1]


def test_remote_reactivation_keeps_old_uncertainty_terminal(tmp_path):
    adapter = RemoteFixture(status="uncertain")
    service, target = remote_case(tmp_path, adapter)
    service._run_remote_owned(target, "first", lifecycle=dict(ACTIVE, activated=False))
    assert len(adapter.attempts) == 1
    service._run_remote_owned(target, "reactivated", lifecycle=dict(ACTIVE))
    assert len(adapter.attempts) == 1
    adapter.upstream = OID_C
    service._run_remote_owned(target, "new-tip", lifecycle=dict(ACTIVE))
    assert len(adapter.attempts) == 2
