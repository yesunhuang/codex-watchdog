"""A Git blocker is actionable even when no upstream commit can be read."""
import json

import pytest

from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.remote_ssh import RemoteSshTarget
from test_mvp_service import FakeAdapter, FakeNotifier, FakeQueue, FakeRegistry


THREAD = "11111111-2222-4333-8444-555555555555"
HEAD = "a" * 40
PREVIOUS = "b" * 40
TARGET = RemoteSshTarget("ssh-remote+node.example", "/work/project", "a" * 32, (THREAD,))


class GitBlocker:
    def __init__(self, *, upstream=None, blocker="ls_remote_failed"):
        self.git = dict(status="blocked", head_oid=HEAD, upstream_oid=upstream,
                        topology=None, blockers=[blocker])
        self.receipt_state = "enqueued"
        self.deliveries = {}
        self.calls = []
        self.session = THREAD

    def probe(self, target, **options):
        assert target == TARGET
        self.calls.append(options)
        result = dict(status="ok", session_id=self.session, repo_path=target.repo_path,
                      git=dict(self.git), completion=None)
        wake = options.get("wake")
        if wake is not None:
            self.deliveries.setdefault(wake["instruction_id"], wake["prompt"])
        if wake is not None or options.get("pending_instruction_id"):
            result["wake"] = dict(state=self.receipt_state)
        return result


def service(runtime, adapter):
    return MvpWatchdogService(
        runtime, codex_home=runtime / "codex-home", registry=FakeRegistry([]),
        git_adapter=FakeAdapter({}), notifier=FakeNotifier(runtime),
        queue_dispatcher=FakeQueue(), remote_ssh_adapter=adapter,
    )


@pytest.mark.parametrize("upstream,blocker", [(None, "ls_remote_failed"), (HEAD, "merge_in_progress")])
def test_git_attention_queues_existing_thread_without_changed_upstream_and_survives_restart(tmp_path, upstream, blocker):
    adapter = GitBlocker(upstream=upstream, blocker=blocker)
    first_service = service(tmp_path, adapter)
    state_path = first_service.state_path(TARGET.workspace_id)
    initial = first_service._read_remote_state(state_path, TARGET)
    initial.update(session_id=THREAD, last_remote_oid=PREVIOUS, retained_user_choice=True)
    first_service.atomic_writer(state_path, initial)

    first = first_service._run_remote_owned(TARGET, "first")
    assert first.wake is not None and first.wake["state"] == "enqueued"
    assert len(adapter.deliveries) == 1
    instruction, prompt = next(iter(adapter.deliveries.items()))
    assert instruction.startswith("git-attention:")
    assert "Git attention" in prompt and "Codex" in prompt
    assert adapter.calls[-1]["wake"]["instruction_id"] == instruction
    state = json.loads(state_path.read_text())
    assert state["pending_instruction_id"] == instruction
    assert state["pending_remote_oid"] is None

    restarted = service(tmp_path, adapter)
    restarted._run_remote_owned(TARGET, "still-enqueued")
    assert json.loads(state_path.read_text())["pending_instruction_id"] == instruction
    adapter.receipt_state = "started"
    restarted._run_remote_owned(TARGET, "confirmed")
    restarted._run_remote_owned(TARGET, "repeat")
    assert len([c for c in adapter.calls if "wake" in c]) == 1
    state = json.loads(state_path.read_text())
    assert state["pending_instruction_id"] is None
    assert state["last_remote_oid"] == (HEAD if upstream else PREVIOUS)
    assert state["retained_user_choice"] is True


def test_uncertain_attention_wake_is_not_resent_or_replaced_by_a_new_commit(tmp_path):
    adapter = GitBlocker()
    adapter.receipt_state = "uncertain"
    instance = service(tmp_path, adapter)
    result = instance._run_remote_owned(TARGET, "first")
    assert result.wake is not None and result.wake["state"] == "uncertain"
    instruction = next(iter(adapter.deliveries))
    adapter.git.update(upstream_oid=PREVIOUS, topology="remote_changed", blockers=[])
    restarted = service(tmp_path, adapter)
    restarted._run_remote_owned(TARGET, "new-commit")
    assert len([c for c in adapter.calls if "wake" in c]) == 1
    assert json.loads(restarted.state_path(TARGET.workspace_id).read_text())["pending_instruction_id"] == instruction


def test_commit_wake_covers_the_same_git_attention_without_a_second_prompt(tmp_path):
    adapter = GitBlocker(upstream=PREVIOUS, blocker="merge_in_progress")
    adapter.git["topology"] = "remote_changed"
    instance = service(tmp_path, adapter)
    instance._run_remote_owned(TARGET, "commit")
    assert len(adapter.deliveries) == 1
    assert next(iter(adapter.deliveries)).startswith("git:")
    adapter.receipt_state = "started"
    instance._run_remote_owned(TARGET, "confirmed")
    instance._run_remote_owned(TARGET, "repeat")
    assert len([c for c in adapter.calls if "wake" in c]) == 1


@pytest.mark.parametrize("unresolved", [False, True])
def test_transient_git_observation_or_unresolved_thread_does_not_wake(tmp_path, unresolved):
    adapter = GitBlocker(blocker="state_changed_during_observation")
    if unresolved:
        adapter.session = None
        adapter.git["blockers"] = ["ls_remote_failed"]
    instance = service(tmp_path, adapter)
    instance._run_remote_owned(TARGET, "no-wake")
    assert adapter.deliveries == {}
