from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from codex_watchdog.activity_signals import native_activity
from codex_watchdog.workspace_registry import TrackedWorkspace

THREAD = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"


def fixture(tmp_path, events):
    home, repo = tmp_path / "native", tmp_path / "repo"
    repo.mkdir()
    path = home / "sessions" / ("rollout-" + THREAD + ".jsonl")
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,source TEXT,thread_source TEXT,archived INTEGER,rollout_path TEXT,created_at INTEGER,history_mode TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?, 'vscode','user',0,?,0,'legacy')", (THREAD, str(repo), str(path)))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT)")
        db.execute("CREATE INDEX native_thread ON queued_items(thread_id)")
    return home, repo, path, TrackedWorkspace.create("fixture", repo, THREAD)


def event(kind, stamp, **extra):
    return dict(timestamp=stamp, type="event_msg", payload=dict(type=kind, turn_id=TURN, **extra))


def test_exact_fresh_user_and_model_completion_are_activity(tmp_path, monkeypatch):
    events = [event("task_started", "2026-10-14T10:00:00Z"),
              event("item_completed", "2026-10-14T10:00:01Z", item=dict(type="UserMessage", content=[dict(type="text", text="Useful work")])),
              event("task_complete", "2026-10-14T10:00:02Z")]
    home, repo, _, workspace = fixture(tmp_path, events)
    monkeypatch.setattr(Path, "rglob", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("history scan")))
    result = native_activity(home, workspace)
    assert result["genuine_at"] == datetime(2026,10,14,10,0,2,tzinfo=timezone.utc).timestamp()
    assert result["turn_active"] is False and result["queue_active"] is False and result["exact"]


def test_watchdog_historical_queue_and_stable_rollout_are_not_activity(tmp_path):
    home, _, _, workspace = fixture(tmp_path, [
        event("task_started", "2026-10-14T10:00:00Z"),
        event("item_completed", "2026-10-14T10:00:01Z", item=dict(type="UserMessage", content=[dict(type="text", text="[CODEX_WATCHDOG_WAKE id=git:old sha256=old]\nHistoric work")])),
        event("task_complete", "2026-10-14T10:00:02Z")])
    first = native_activity(home, workspace)
    assert first["genuine_at"] == 0  # immutable real native creation, not the WatchDog turn
    second=native_activity(home,workspace)
    assert second['genuine_at']==first['genuine_at']
    assert second['cursor']['offset']==first['cursor']['offset']
    assert second['cursor']['boundary_sha256']==first['cursor']['boundary_sha256']
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES('accepted',?)", (THREAD,))
    assert native_activity(home, workspace)["queue_active"] is True
    assert native_activity(home, workspace)["genuine_at"] == 0


def test_long_live_turn_retains_monitoring_without_manufactured_activity(tmp_path):
    home, _, _, workspace = fixture(tmp_path, [event("task_started", "2026-10-01T10:00:00Z")])
    result = native_activity(home, workspace)
    assert result["turn_active"] is True and result["genuine_at"] == 0


def test_incomplete_or_corrupt_evidence_cannot_prove_idle(tmp_path):
    home, _, path, workspace = fixture(tmp_path, [event("task_complete", "2026-10-14T10:00:00Z")])
    with path.open("ab") as stream:
        stream.write(b'broken\n')
    result = native_activity(home, workspace)
    assert not result["complete_tail"] and result["turn_active"] is None
    path.write_text("".join(json.dumps(event("task_complete", "2026-10-14T10:00:00Z")) + "\n" for _ in range(600)), encoding="utf-8")
    result = native_activity(home, workspace)
    assert not result["complete_tail"] and result["turn_active"] is None


def test_wrong_metadata_never_activates_another_session(tmp_path):
    home, repo, _, workspace = fixture(tmp_path, [dict(type="session_meta", payload=dict(id=TURN, cwd=str(tmp_path)))])
    result = native_activity(home, workspace)
    assert result["exact"] is False and result["genuine_at"] is None
    assert result["reason"] == "native_activity_evidence_unavailable"


def test_large_history_uses_current_cursor_and_does_not_renew_activity(tmp_path):
    from codex_watchdog.activity_signals import NativeActivityTracker
    old=[dict(type='event_msg',timestamp='2026-01-01T00:00:00Z',payload=dict(type='tool_output',text='x'*1000)) for _ in range(1000)]
    home,_,path,workspace=fixture(tmp_path,old+[
        event('task_started','2026-10-01T10:00:00Z'),
        event('item_completed','2026-10-01T10:00:01Z',item=dict(type='UserMessage',content=[dict(type='text',text='Real user activity')])),
        event('task_complete','2026-10-01T10:00:02Z')])
    clock=[datetime(2026,10,1,10,0,3,tzinfo=timezone.utc).timestamp()]
    tracker=NativeActivityTracker(tmp_path/'observer',home,clock=lambda:clock[0])
    first=tracker.observe(workspace)
    assert first['genuine_at']==clock[0]-1 and first['turn_active'] is False
    clock[0]+=72*3600
    second=tracker.observe(workspace)
    assert second['genuine_at']==first['genuine_at'] and second['turn_active'] is False
    assert second['cursor']['offset']==path.stat().st_size
    assert second['complete_tail']


def test_quiet_coverage_is_not_fresh_activity_and_large_gap_is_unknown(tmp_path):
    from codex_watchdog.activity_signals import NativeActivityTracker
    home,_,path,workspace=fixture(tmp_path,[event('task_complete','2026-01-01T00:00:00Z')])
    clock=[datetime(2026,10,1,tzinfo=timezone.utc).timestamp()]
    tracker=NativeActivityTracker(tmp_path/'observer',home,clock=lambda:clock[0])
    first=tracker.observe(workspace)
    assert first['genuine_at']==0 and first['turn_active'] is None
    clock[0]+=72*3600
    second=tracker.observe(workspace)
    assert second['genuine_at']==0 and second['turn_active'] is False
    with path.open('a',encoding='utf-8') as stream:
        for _ in range(600):stream.write(json.dumps(dict(type='event_msg',timestamp='2026-10-05T00:00:00Z',payload=dict(type='tool_output',text='x'*1000)))+'\n')
    clock[0]+=3600
    gap=tracker.observe(workspace)
    assert gap['turn_active'] is None and gap['genuine_at']==0


def test_nanosecond_native_timestamps_are_supported_on_python39(tmp_path):
    home,_,_,workspace=fixture(tmp_path,[event('user_message','2026-10-01T10:00:01.123456789Z',message='Fresh input')])
    signal=native_activity(home,workspace)
    assert signal['genuine_at']==datetime(2026,10,1,10,0,1,123456,tzinfo=timezone.utc).timestamp()


def test_partial_record_is_read_when_its_newline_arrives(tmp_path):
    from codex_watchdog.activity_signals import NativeActivityTracker
    home, _, path, workspace = fixture(tmp_path, [])
    stamp = datetime(2026,10,1,tzinfo=timezone.utc).timestamp()
    tracker = NativeActivityTracker(tmp_path/'observer',home,clock=lambda:stamp)
    pending = json.dumps(event('user_message','2026-10-01T00:00:00Z',message='Real input')).encode()
    path.write_bytes(pending[:-5])
    first = tracker.observe(workspace)
    assert first['cursor']['offset'] == 0 and first['turn_active'] is None
    with path.open('ab') as stream: stream.write(pending[-5:]+b'\n')
    second = tracker.observe(workspace)
    assert second['genuine_at'] == stamp and second['cursor']['offset'] == path.stat().st_size


def test_invalid_optional_cursor_does_not_poison_fresh_evidence(tmp_path):
    home, _, path, workspace = fixture(tmp_path,[event('user_message','2026-10-01T00:00:00Z',message='Real input')])
    stamp = datetime(2026,10,1,tzinfo=timezone.utc).timestamp()
    valid = native_activity(home,workspace,observed_at=stamp)['cursor']
    for mutation in (dict(genuine_at='poison'),dict(observed_since=-1),dict(lifecycle=['task_complete',TURN,float('nan')]),dict(activity_known=1)):
        result = native_activity(home,workspace,cursor=dict(valid,**mutation),observed_at=stamp)
        assert result['exact'] and result['genuine_at'] == stamp


def test_cursor_clock_rollback_and_future_native_input(tmp_path):
    home, _, _, workspace = fixture(tmp_path,[event('user_message','2026-10-01T00:00:00Z',message='Real input'),
        event('user_message','2099-01-01T00:00:00Z',message='Future input')])
    stamp = datetime(2026,10,1,tzinfo=timezone.utc).timestamp()
    first = native_activity(home,workspace,observed_at=stamp)
    second = native_activity(home,workspace,cursor=first['cursor'],observed_at=stamp-60)
    assert second['genuine_at'] == stamp and second['cursor']['checked_at'] == stamp


def test_actual_execution_of_a_routed_user_command_is_activity(tmp_path):
    home,_,_,workspace=fixture(tmp_path,[event('task_started','2026-10-01T00:00:00Z'),
        event('user_message','2026-10-01T00:00:01Z',message='[CODEX_WATCHDOG_WAKE id=slackbot:fixture sha256='+('a'*64)+']\nUseful authorized work'),
        event('task_complete','2026-10-01T00:00:02Z')])
    result=native_activity(home,workspace)
    assert result['genuine_at']==datetime(2026,10,1,0,0,2,tzinfo=timezone.utc).timestamp()
    assert result['turn_active'] is False


def test_supported_paginated_native_history_proves_completed_turn(tmp_path):
    home,_,_,workspace=fixture(tmp_path,[event('user_message','2026-10-01T00:00:00Z',message='Real input'),
        event('task_complete','2026-10-01T00:00:01Z')])
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute("UPDATE threads SET history_mode='paginated' WHERE id=?",(THREAD,))
    assert native_activity(home,workspace)['turn_active'] is False
