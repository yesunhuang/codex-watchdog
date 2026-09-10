import json
from pathlib import Path
import subprocess

import pytest

from codex_watchdog.control_context import acting_as, effect_guard
from codex_watchdog.control_state import ControlError, ControlStore, control_atomic_json
from codex_watchdog.models import sha256_text
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.queue_wake import QueueWakeDispatcher
from codex_watchdog.stop_hook import HookSettings, run_hook_text
from codex_watchdog.storage import InstructionStore
from codex_watchdog.workspace_registry import TrackedWorkspace


THREAD = "11111111-2222-4333-8444-555555555555"
QUEUE = "99999999-aaaa-4bbb-8ccc-dddddddddddd"


@pytest.fixture
def controlled(tmp_path):
    codex = tmp_path / "codex"
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = tmp_path / "runtime"
    store = ControlStore(codex, THREAD, repo, clock=lambda: 100, boot_id="boot")
    token = store.attach("desktop", "desktop-host", "vscode")
    return store, token, codex, repo, runtime


def test_queue_fences_unscoped_and_revived_senders_including_old_journal(controlled):
    store, token, codex, repo, runtime = controlled
    calls = []

    def send(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"Queued message {QUEUE} for thread {THREAD}.\n", "")

    dispatcher = QueueWakeDispatcher(runtime, codex_home=codex, runner=send)
    assert dispatcher.dispatch(THREAD, "no-token", "hello", "test").status == "rejected"
    with acting_as(store, token):
        assert dispatcher.dispatch(THREAD, "one", "hello", "test").status == "enqueued"
        before = (dispatcher.records / (sha256_text("one") + ".json")).read_bytes()
        store.detach(token)
        assert store.claim_remote("remote", "remote-host", "absent") is not None
        assert dispatcher.dispatch(THREAD, "one", "hello", "test").status == "rejected"
        with pytest.raises(ControlError, match="stale_epoch"):
            dispatcher.observe_delivery("one")
        assert (dispatcher.records / (sha256_text("one") + ".json")).read_bytes() == before
        resume = runtime / "resume_prompt.md"
        resume.write_text("keep this prompt", encoding="utf-8")
        with pytest.raises(ControlError, match="stale_epoch"):
            dispatcher.claim_and_dispatch_resume_prompt(THREAD)
        assert resume.read_text(encoding="utf-8") == "keep this prompt"
    assert len(calls) == 1


def test_revived_service_cannot_observe_as_owner_or_advance_cursors(controlled):
    store, token, codex, repo, runtime = controlled
    workspace = TrackedWorkspace.create("disposable", repo, THREAD)

    class Registry:
        def list_workspaces(self):
            return (workspace,)

    class NoGit:
        def observe(self, _):
            pytest.fail("stale service must stop before its workflow")

    service = MvpWatchdogService(runtime, registry=Registry(), codex_home=codex, git_adapter=NoGit())
    with acting_as(store, token):
        store.detach(token)
        store.claim_remote("remote", "remote-host", "absent")
        result = service.run_once().workspaces[0]
    assert result.status == "standby" and result.reason == "control_stale_epoch"
    assert not Path(result.state_path).exists()
    assert not Path(result.observation_path).exists()


def test_revived_notifier_cannot_send_or_update_dedupe_state(controlled):
    store, token, codex, repo, runtime = controlled
    notifier = EnvironmentNotifier(runtime, config=NotificationConfig())
    event = NotificationEvent("disposable", "test", "transition", "subject", "message")
    with acting_as(store, token):
        store.detach(token)
        store.claim_remote("remote", "remote-host", "absent")
        with pytest.raises(ControlError, match="stale_epoch"):
            notifier.notify(event)
    assert not notifier.state_path.exists()


def test_stop_waiter_cannot_consume_after_epoch_changes(controlled, monkeypatch):
    from codex_watchdog import control_context, linux_owner

    store, token, codex, repo, runtime = controlled
    value = store.read()
    value["runtime_path"] = str(runtime)
    control_atomic_json(store.path, value)
    monkeypatch.setattr(control_context.sys, "platform", "linux")
    monkeypatch.setattr(control_context, "ControlStore", lambda *args: store)
    monkeypatch.setattr(control_context, "_hook_descends_from", lambda pid: pid == 777)
    monkeypatch.setattr(linux_owner, "writer_pid", lambda *args: 777)
    monkeypatch.setattr(linux_owner, "vscode_writer", lambda pid: pid == 777)
    tick = [0.0]

    def sleep(seconds):
        tick[0] += seconds
        if store.read()["epoch"] == token["epoch"]:
            store.detach(token)
            store.claim_remote("remote", "remote-host", "absent")
            InstructionStore(runtime).submit("late", "test", "must stay queued", target_session_id=THREAD)

    import io
    from codex_watchdog.stop_hook import run_hook
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_hook(
        HookSettings(runtime, grace_seconds=1, test_mode=True, codex_home=codex),
        stdin=io.StringIO(json.dumps(dict(session_id=THREAD, turn_id="turn", cwd=str(repo), hook_event_name="Stop"))),
        stdout=stdout, stderr=stderr, monotonic=lambda: tick[0], sleep=sleep,
    )
    assert code == 0 and json.loads(stdout.getvalue()) == {}
    assert "control_stale_epoch" in stderr.getvalue()
    assert len(list((runtime / "inbox").glob("*.json"))) == 1
    assert not list((runtime / "inflight").glob("*.json"))


def test_hook_with_no_current_writer_cannot_consume(controlled, monkeypatch):
    from codex_watchdog import control_context, linux_owner
    store, token, codex, repo, runtime = controlled
    monkeypatch.setattr(control_context.sys, "platform", "linux")
    monkeypatch.setattr(control_context, "ControlStore", lambda *args: store)
    monkeypatch.setattr(linux_owner, "writer_pid", lambda *args: None)
    InstructionStore(runtime).submit("one", "test", "stay queued", target_session_id=THREAD)
    code, output, error = run_hook_text(
        dict(session_id=THREAD, turn_id="turn", cwd=str(repo), hook_event_name="Stop"),
        HookSettings(runtime, grace_seconds=0, test_mode=True, codex_home=codex),
    )
    assert code == 0 and json.loads(output) == {}
    assert "writer_unverified" in error
    assert len(list((runtime / "inbox").glob("*.json"))) == 1


def test_transient_control_contention_does_not_shorten_stop_grace(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import io
    from codex_watchdog import stop_hook
    from codex_watchdog.control_state import ControlBusy
    tick = [0.0]
    calls = [0]

    @contextmanager
    def contended(purpose):
        calls[0] += 1
        if calls[0] in (2, 3, 5):
            raise ControlBusy("control_operation_in_progress")
        yield

    monkeypatch.setattr(stop_hook, "current_effect", contended)
    output, error = io.StringIO(), io.StringIO()
    code = stop_hook.run_hook(
        HookSettings(tmp_path, grace_seconds=1, test_mode=True, codex_home=tmp_path/'codex'),
        stdin=io.StringIO(json.dumps(dict(session_id=THREAD, turn_id="turn", cwd=str(tmp_path), hook_event_name="Stop"))),
        stdout=output, stderr=error, monotonic=lambda: tick[0],
        sleep=lambda seconds: tick.__setitem__(0, tick[0]+seconds),
    )
    assert code == 0 and json.loads(output.getvalue()) == {} and not error.getvalue()
    assert tick[0] >= 1
    terminal = [json.loads(p.read_text()) for p in (tmp_path/'audit').glob('*.json')]
    assert any(e.get('outcome') == 'grace_expired_parked' and e.get('hook_duration_ms') >= 1000 for e in terminal)


@pytest.mark.parametrize("contention", ["transient", "persistent", "epoch_changed"])
def test_hook_admission_retries_only_its_captured_epoch(controlled, monkeypatch, contention):
    from contextlib import contextmanager
    import io
    from codex_watchdog import control_context, linux_owner, stop_hook
    from codex_watchdog.control_state import ControlBusy

    store, token, codex, repo, runtime = controlled
    value = store.read()
    value["runtime_path"] = str(runtime)
    control_atomic_json(store.path, value)
    monkeypatch.setattr(control_context.sys, "platform", "linux")
    monkeypatch.setattr(control_context, "ControlStore", lambda *args: store)
    monkeypatch.setattr(control_context, "_hook_descends_from", lambda pid: pid == 777)
    monkeypatch.setattr(linux_owner, "writer_pid", lambda *args: 777)
    monkeypatch.setattr(linux_owner, "vscode_writer", lambda pid: pid == 777)
    original_guard = store.guard
    calls, tick = [0], [0.0]

    @contextmanager
    def contended(captured, **kwargs):
        calls[0] += 1
        if calls[0] == 1 or contention == "persistent":
            raise ControlBusy("control_operation_in_progress")
        with original_guard(captured, **kwargs) as current:
            yield current

    monkeypatch.setattr(store, "guard", contended)

    def sleep(seconds):
        tick[0] += seconds
        if contention == "epoch_changed" and store.read()["epoch"] == token["epoch"]:
            store.detach(token)
            store.claim_remote("remote", "remote-host", "absent")
            InstructionStore(runtime).submit("new-owner", "test", "keep queued", target_session_id=THREAD)

    output, error = io.StringIO(), io.StringIO()
    code = stop_hook.run_hook(
        HookSettings(runtime, grace_seconds=0.2, test_mode=True, codex_home=codex),
        stdin=io.StringIO(json.dumps(dict(session_id=THREAD, turn_id="turn", cwd=str(repo), hook_event_name="Stop"))),
        stdout=output, stderr=error, monotonic=lambda: tick[0], sleep=sleep,
    )
    assert code == 0 and json.loads(output.getvalue()) == {}
    assert calls[0] > 1
    if contention == "transient":
        assert not error.getvalue() and tick[0] >= 0.25
        terminal = [json.loads(p.read_text()) for p in (runtime / "audit").glob("*.json")]
        assert any(e.get("outcome") == "grace_expired_parked" and e.get("hook_duration_ms") >= 200
                   for e in terminal)
    else:
        assert not list((runtime / "audit").glob("*.json"))
        assert not list((runtime / "inflight").glob("*.json"))
        if contention == "persistent":
            assert tick[0] == pytest.approx(1.0)
            assert "control_operation_in_progress" in error.getvalue()
        else:
            assert "control_stale_epoch" in error.getvalue()
            assert len(list((runtime / "inbox").glob("*.json"))) == 1
