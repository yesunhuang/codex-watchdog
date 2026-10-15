from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.observed_notification_birth import observe_notification_source as observe, SLOTS
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.storage import InstructionStore

SID = "11111111-2222-4333-8444-555555555555"
OLD, NEW = sha256_text("old"), sha256_text("new")


def test_original_current_source_survives_poll_and_serialized_restart():
    state = {}
    assert observe(state, "git_attention", SID, OLD, 100, allow_initial=True) == 100
    state = json.loads(json.dumps(state))
    assert observe(state, "git_attention", SID, OLD, 1000000, allow_initial=True) == 100
    assert state["notification_births"]["points"]["git_attention"]["generation"] == 1


def test_existing_unknown_baseline_cannot_be_retimestamped_by_retry():
    state = {}
    assert observe(state, "git_attention", SID, OLD, 100) is None
    assert observe(state, "git_attention", SID, OLD, 200, allow_initial=True) is None
    assert observe(state, "git_attention", SID, NEW, 300) == 300
    assert observe(state, "git_attention", SID, NEW, 400) == 300


def test_delayed_callback_and_clock_rollback_preserve_new_current_point():
    state = {}
    observe(state, "git_attention", SID, OLD, 100, allow_initial=True)
    observe(state, "git_attention", SID, NEW, 200)
    expected = deepcopy(state)
    assert observe(state, "git_attention", SID, OLD, 150) is None
    assert observe(state, "git_attention", SID, OLD, 200) is None
    assert state == expected
    assert observe(state, "wake_status", SID, OLD, 150, allow_initial=True) == 150


def test_fixed_current_slots_do_not_accumulate_notification_history():
    state = {}
    for i in range(10000):
        observe(state, "git_attention", SID, sha256_text(str(i)), i)
    assert len(state["notification_births"]["points"]) == 1
    assert len(json.dumps(state)) < 512
    with pytest.raises(ValueError):
        observe(state, "history-10000", SID, NEW, 10001)


@pytest.mark.parametrize("damage", ["future-schema", "extra-slot", "bad-point", "foreign-session"])
def test_corrupt_source_is_preserved_and_independent_slot_can_progress(damage):
    state = {}
    observe(state, "git_attention", SID, OLD, 100, allow_initial=True)
    points = state["notification_births"]
    if damage == "future-schema": points["schema_version"] = 2
    elif damage == "extra-slot": points["points"]["unknown"] = {}
    elif damage == "bad-point": points["points"]["git_attention"]["born"] = True
    else: points["points"]["git_attention"]["session_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    expected = deepcopy(state)
    with pytest.raises(ValueError): observe(state, "git_attention", SID, NEW, 200)
    assert state == expected
    if damage in ("bad-point", "foreign-session"):
        assert observe(state, "wake_status", SID, NEW, 200, allow_initial=True) == 200


def service(tmp_path, *, writer=InstructionStore._atomic_json):
    monitor = MvpWatchdogService.__new__(MvpWatchdogService)
    monitor.runtime = tmp_path
    monitor.states = tmp_path / "mvp/states"
    monitor.atomic_writer = writer
    captured = []

    def notify(event):
        # Original point has been durably published before this attempted send.
        saved = json.loads(monitor.state_path("fixture").read_text())
        assert saved["notification_births"]["points"]["git_attention"]["born"] == event.source_born
        captured.append(event)
        return dict(event_type=event.event_type)

    monitor._safe_notify = notify
    workspace = SimpleNamespace(workspace_id="fixture", session_id=SID, repo_root=Path(tmp_path))
    observation = SimpleNamespace(blockers=("missing_upstream",), head_oid="a"*40,
                                  topology="missing_upstream", observed_at=100)
    return monitor, workspace, observation, captured


def test_actual_mvp_source_birth_is_committed_before_notice_and_not_refreshed(tmp_path):
    monitor, workspace, observation, captured = service(tmp_path)
    state = {}
    monitor._attention_notifications(workspace, observation, None, source_state=state, allow_initial_source=True)
    assert captured[-1].source_born == 100
    observation.observed_at = 200
    monitor._attention_notifications(workspace, observation, None, source_state=state)
    assert captured[-1].source_born == 100
    observation.observed_at, observation.blockers = 300, ()
    monitor._attention_notifications(workspace, observation, None, source_state=state)
    observation.observed_at, observation.blockers = 400, ("missing_upstream",)
    monitor._attention_notifications(workspace, observation, None, source_state=state)
    assert captured[-1].source_born == 400
    assert len(state["notification_births"]["points"]) == 2


def test_failed_source_publication_drops_current_notice_without_send(tmp_path):
    def broken(*args): raise OSError("fixture write refused")
    monitor, workspace, observation, captured = service(tmp_path, writer=broken)
    assert monitor._attention_notifications(workspace, observation, None, source_state={}, allow_initial_source=True) == []
    assert not captured


def test_bad_git_source_point_does_not_block_independent_wake_notice(tmp_path):
    monitor, workspace, observation, captured = service(tmp_path)
    state = {}
    monitor._attention_notifications(workspace, observation, None, source_state=state, allow_initial_source=True)
    state["notification_births"]["points"]["git_attention"]["born"] = True
    captured.clear()
    monitor._safe_notify = lambda event: captured.append(event) or dict(event_type=event.event_type)
    observation.observed_at = 200
    monitor._attention_notifications(workspace, observation,
        dict(status="uncertain", kind="remote_update", instruction_id="new-wake"), source_state=state)
    assert [e.event_type for e in captured] == ["wake_uncertain"]
    assert captured[0].source_born == 200
    assert state["notification_births"]["points"]["git_attention"]["born"] is True


def test_remote_current_source_keeps_original_birth_across_probes(tmp_path):
    monitor, workspace, observation, captured = service(tmp_path)
    target = SimpleNamespace(workspace_id="fixture")
    state = dict(last_seen_at=100)
    assert monitor._remote_notification_source_birth(target, state, "git_attention", SID, OLD,
                                                     allow_initial=True) == 100
    state = json.loads(monitor.state_path("fixture").read_text())
    state["last_seen_at"] = 200
    assert monitor._remote_notification_source_birth(target, state, "git_attention", SID, OLD) == 100
    state["last_seen_at"] = 300
    assert monitor._remote_notification_source_birth(target, state, "git_attention", SID, NEW) == 300
