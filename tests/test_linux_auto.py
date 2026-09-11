from pathlib import Path
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from codex_watchdog import linux_auto, linux_binding, linux_owner
from codex_watchdog.app_server import AppServerError
from codex_watchdog.control_state import ControlStore, control_atomic_json
from codex_watchdog.linux_auto import LinuxAutoWatchdog
from codex_watchdog.linux_owner import LinuxThreadOwner
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig


THREAD = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    repo = tmp_path / "disposable"
    repo.mkdir()
    rollout = home / "sessions" / "thread.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id,cwd,source,thread_source,archived,rollout_path)")
        db.execute("INSERT INTO threads VALUES (?,?,'vscode','user',0,?)", (THREAD, str(repo), str(rollout)))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id,thread_id,payload_json)")
        db.execute("CREATE TABLE queued_thread_revisions(thread_id,revision)")
    monkeypatch.setattr(linux_binding, "locality_identity", lambda: "remote-host")
    monkeypatch.setattr(linux_auto, "locality_identity", lambda: "remote-host")
    writer = [777]
    monkeypatch.setattr(linux_owner, "writer_pid", lambda *args: writer[0])
    monkeypatch.setattr(linux_auto, "writer_pid", lambda *args: writer[0])
    monkeypatch.setattr(linux_owner, "vscode_writer", lambda pid: pid == 777)
    monkeypatch.setattr(linux_auto, "vscode_writer", lambda pid: pid == 777)
    clock = [100.0]
    factory = lambda h, t, r: ControlStore(h, t, r, clock=lambda: clock[0], boot_id="boot")
    store = factory(home, THREAD, repo)
    local = store.attach("desktop", "desktop-host", "vscode", ttl=20)
    with store.guard(local) as value:
        value["runtime_path"] = str(store.directory / "runtime")
        value["remote_target"] = dict(authority="ssh-remote+example.invalid", repo_path=str(repo), storage_key="a" * 32)
        control_atomic_json(store.path, value)
    clients = []

    class Client:
        def __init__(self, executable, codex_home, cwd, event):
            self.process = SimpleNamespace(pid=900 + len(clients), returncode=None)
            self.status = "idle"
            self.calls = []
            self.closed = False
            clients.append(self)

        def initialize(self):
            pass

        def request(self, method, parameters):
            self.calls.append((method, parameters))
            assert parameters["threadId"] == THREAD
            assert method in ("thread/read", "thread/resume")
            if method == "thread/resume":
                assert writer[0] is None
                writer[0] = self.process.pid
            return dict(thread=dict(id=THREAD, cwd=str(repo), status=dict(type=self.status)))

        def pump(self, timeout):
            if self.process.returncode is not None:
                raise AppServerError("app_server_exited")

        def close(self):
            self.closed = True
            if writer[0] == self.process.pid:
                writer[0] = None

    def make_agent(exclude=(), **options):
        return LinuxAutoWatchdog(
            tmp_path / "agent-runtime", home, executable="fixture-codex", exclude=exclude, **options,
            store_factory=factory,
            owner_factory=lambda binding, **kwargs: LinuxThreadOwner(binding, client_factory=Client, **kwargs),
            service_factory=lambda runtime, **kwargs: SimpleNamespace(
                run_once=lambda: pytest.fail("unexpected cycle"),
                notifier=EnvironmentNotifier(runtime, config=NotificationConfig()),
            ),
        )

    agents = []

    def agent(exclude=(), **options):
        value = make_agent(exclude, **options)
        agents.append(value)
        return value

    yield store, local, writer, clock, clients, agent
    for dog in agents:
        for item in dog.controllers.values():
            if item["owner"].client is not None:
                item["owner"].client.close()
            item["locks"].close()


def test_host_observes_attached_thread_and_keeps_observation_after_idle_handback(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent = make_agent()
    assert agent.step(observe=False)[0]["state"] == "observing"
    assert clients == []
    assert store.read()["owner"]["host_observer"] is True
    assert store.read()["epoch"] == 2
    from codex_watchdog.control_state import ControlError
    with pytest.raises(ControlError, match="stale_epoch"):
        store.detach(local)
    writer[0] = None
    owned = agent.step(observe=False)[0]
    assert owned["state"] == "owned" and owned["epoch"] == 2
    assert len(clients) == 1
    owner = agent.controllers[THREAD]["owner"]
    assert owner.binding.workspace(owner.binding.load()).session_id == THREAD
    assert store.read()["writer_pid"] == clients[0].process.pid
    owner.thread_status = clients[0].status = "active"
    assert store.attach("desktop-returned", "desktop-host", "remote") is None
    busy = agent.step(observe=False)[0]
    assert busy["state"] == "releasing"
    assert not clients[0].closed
    assert store.read()["epoch"] == 2
    owner.thread_status = clients[0].status = "idle"
    released = agent.step(observe=False)[0]
    assert released["state"] == "waiting_for_attach"
    assert clients[0].closed and writer[0] is None
    assert owner.binding.load()["state"] == "armed"
    writer[0] = 777
    assert store.attach("desktop-returned", "desktop-host", "vscode") is None
    assert agent.step(observe=False)[0]["state"] == "observing"
    assert store.read()["epoch"] == 2 and store.read()["state"] == "DETACHED_REMOTE"
    assert len(clients) == 1  # Never resume the VS Code writer.


def test_desktop_crash_takeover_and_remote_crash_restart_keep_same_thread(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    first = make_agent()
    clock[0] += 21
    assert first.step(observe=False)[0]["state"] == "observing"
    writer[0] = None
    assert first.step(observe=False)[0]["epoch"] == 2
    item = first.controllers.pop(THREAD)
    # Process/stdio death releases kernel locks, but does not rewrite old state.
    item["owner"].client.close()
    item["locks"].close()
    assert item["owner"].binding.load()["state"] == "armed"
    restarted = make_agent()
    assert restarted.step(observe=False)[0]["state"] == "standby"
    clock[0] += 121
    result = restarted.step(observe=False)[0]
    assert result["state"] == "owned" and result["epoch"] == 3
    assert len(clients) == 2


def detached(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    store.detach(local)
    writer[0] = None
    agent = make_agent()
    assert agent.step(observe=False)[0]["state"] == "owned"
    return agent, agent.controllers[THREAD]["owner"]


@pytest.mark.parametrize("transport", ["slack", "smtp", "fallback"])
def test_writer_loss_alert_and_recovery_use_configured_transport_once(scenario, monkeypatch, transport):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    messages = []

    def post(url, payload, timeout):
        if transport == "fallback":
            raise OSError("fixture provider unavailable")
        messages.append(json.loads(payload)["text"])
        return 200

    class Smtp:
        def ehlo(self): pass
        def send_message(self, message):
            messages.append(str(message))
            return {}
        def quit(self): pass

    config = NotificationConfig(
        slack_webhook_url="https://hooks.slack.invalid/fixture" if transport != "smtp" else None,
        smtp_host="smtp.invalid", smtp_sender="watchdog@example.invalid",
        smtp_recipients=("operator@example.invalid",), smtp_security="plain",
    )
    owner.health.notifier = EnvironmentNotifier(owner.binding.runtime, config=config,
        http_post=post, smtp_factory=lambda *args: Smtp())
    writer[0] = None
    first = agent.step(observe=False)[0]
    assert first["reason"] == "linux_writer_changed"
    assert first["notification"]["status"] == ("sent_fallback" if transport == "fallback" else "sent")
    assert first["notification"]["channel"] == ("slack" if transport == "slack" else "smtp")
    assert len(messages) == 1 and "can no longer watch or control" in messages[0]
    assert agent.step(observe=False)[0]["state"] == "blocked"
    assert len(messages) == 1 and len(clients) == 1
    assert len(clients[0].calls) == 2  # No retry/resume, fork, queue or turn start.
    writer[0] = clients[0].process.pid
    monkeypatch.setattr(agent, "_cycle", lambda item: SimpleNamespace(status="completed"))
    assert agent.step()[0]["state"] == "owned"
    assert len(messages) == 2 and "again" in messages[1]
    agent.step()
    assert len(messages) == 2
    assert len(list((store.directory / "effects").glob("*.json"))) == 2


def test_missing_thread_metadata_still_reports_under_original_capability(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    messages = []
    owner.health.notifier = EnvironmentNotifier(owner.binding.runtime,
        config=NotificationConfig(slack_webhook_url="https://hooks.slack.invalid/fixture"),
        http_post=lambda *args: messages.append(args) or 200)
    with sqlite3.connect(owner.binding.codex_home / "state_5.sqlite") as db:
        db.execute("DELETE FROM threads WHERE id=?", (THREAD,))
    assert agent.step()[0]["notification"]["status"] == "sent"
    assert agent.step()[0]["reason"] == "linux_exact_thread_unavailable"
    assert len(messages) == 1 and len(clients) == 1
    assert store.read()["epoch"] == 2


def test_stale_or_expired_owner_cannot_send_health_alert_or_change_health_state(scenario, monkeypatch):
    from codex_watchdog import control_state
    store, local, writer, clock, clients, make_agent = scenario
    monkeypatch.setattr(control_state, "control_kernel_owner", lambda path: writer[0])
    agent, owner = detached(scenario)
    clock[0] += 121
    writer[0] = None
    assert agent.step()[0]["notification"]["status"] == "blocked"
    assert not owner.health.path.exists()
    replacement = store.claim_remote("replacement", "remote-host", "absent")
    assert replacement["epoch"] == 3
    before = store.path.read_bytes()
    assert agent.step()[0]["reason"] == "control_stale_epoch"
    assert not owner.health.path.exists() and store.path.read_bytes() == before


def test_normal_handback_does_not_create_loss_alert(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    assert store.attach("desktop-returned", "desktop-host", "remote") is None
    assert agent.step(observe=False)[0]["state"] == "waiting_for_attach"
    assert not owner.health.path.exists()
    assert not list((store.directory / "effects").glob("*.json"))


def test_idle_parking_retains_host_epoch_and_completion_observation(scenario, monkeypatch):
    store, _, writer, _, clients, _ = scenario
    agent, owner = detached(scenario)
    epoch = store.read()["epoch"]
    observed = []
    monkeypatch.setattr(agent, "_cycle", lambda item: observed.append(item["token"]) or SimpleNamespace(status="completed"))
    owner._idle_since -= 6
    assert agent.step()[0]["state"] == "parked"
    assert clients[0].closed and writer[0] is None
    assert store.read()["writer_pid"] is None
    assert store.read()["owner"]["host_observer"] is True
    assert store.read()["attached_request"] is None
    assert agent.step()[0]["state"] == "parked"
    assert len(clients) == 1 and len(observed) == 2
    assert store.read()["epoch"] == epoch
    writer[0] = 777
    assert agent.step()[0]["state"] == "observing"
    assert len(clients) == 1 and store.read()["epoch"] == epoch


def test_failed_alert_does_not_become_successful_suppression(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    sends = []

    def fail(*args):
        sends.append(1)
        raise OSError("fixture secret must not appear in health status")

    owner.health.notifier = EnvironmentNotifier(owner.binding.runtime,
        config=NotificationConfig(slack_webhook_url="https://hooks.slack.invalid/fixture"), http_post=fail)
    writer[0] = None
    assert agent.step()[0]["notification"]["status"] == "delivery_failed"
    assert agent.step()[0]["notification"]["status"] == "delivery_failed"
    assert sends == [1]  # Never replay an external send with an uncertain outcome.
    state = json.loads(owner.health.path.read_text())
    assert state["pending"] and "fixture secret" not in owner.health.path.read_text()


def test_loss_alert_survives_remote_process_restart_without_resending(scenario, monkeypatch):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    sends = []
    notifier = EnvironmentNotifier(owner.binding.runtime,
        config=NotificationConfig(slack_webhook_url="https://hooks.slack.invalid/fixture"),
        http_post=lambda *args: sends.append(args) or 200)
    owner.health.notifier = notifier
    writer[0] = None
    assert agent.step()[0]["notification"]["status"] == "sent"
    item = agent.controllers.pop(THREAD)
    item["owner"].client.close()
    item["locks"].close()
    clock[0] += 121
    restarted = make_agent()
    assert restarted.step(observe=False)[0]["state"] == "owned"
    replacement = restarted.controllers[THREAD]["owner"]
    replacement.health.notifier = notifier
    writer[0] = None
    assert restarted.step()[0]["notification"]["status"] == "sent"
    assert len(sends) == 1
    writer[0] = replacement.client.process.pid
    monkeypatch.setattr(restarted, "_cycle", lambda item: SimpleNamespace(status="completed"))
    restarted.step()
    assert len(sends) == 2


@pytest.mark.parametrize("busy", [False, True])
def test_observation_failure_reports_but_brief_contention_does_not(scenario, monkeypatch, busy):
    from codex_watchdog.control_state import ControlBusy, ControlError
    agent, owner = detached(scenario)

    def fail(item):
        raise (ControlBusy("control_operation_in_progress") if busy else ControlError("control_probe_failed"))

    monkeypatch.setattr(agent, "_cycle", fail)
    result = agent.step()[0]
    assert result["state"] == "blocked"
    assert owner.health.path.exists() is not busy
    if not busy:
        assert result["notification"]["status"] == "audit_only"


def test_uncertain_alert_keeps_canonical_barrier_and_is_never_replayed(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    attempts = []

    class UncertainNotifier:
        def notify(self, event):
            attempts.append(event)
            raise RuntimeError("fixture interrupted before recording an external outcome")

    owner.health.notifier = UncertainNotifier()
    writer[0] = None
    assert agent.step()[0]["notification"]["status"] == "blocked"
    assert store.read()["external_effect"] is not None
    assert agent.step()[0]["notification"]["reason"] == "control_notification_outcome_uncertain"
    assert len(attempts) == 1


def test_handback_racing_observation_does_not_raise_a_loss_alert(scenario, monkeypatch):
    from codex_watchdog.control_state import ControlError
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)

    def handback(item):
        assert store.attach("returning-desktop", "desktop-host", "remote") is None
        raise ControlError("control_handback_pending")

    monkeypatch.setattr(agent, "_cycle", handback)
    assert "notification" not in agent.step()[0]
    assert not owner.health.path.exists()
    assert agent.step(observe=False)[0]["state"] == "waiting_for_attach"
    assert all(call[1]["threadId"] == THREAD for client in clients for call in client.calls)
    assert not any(call[0] in ("thread/start", "turn/start") for client in clients for call in client.calls)


def test_excluded_workspace_never_binds_or_opens_a_writer(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    before = store.path.read_bytes()
    agent = make_agent(exclude=(Path(store.repo_path).name,))
    assert agent.step(observe=False) == []
    assert clients == []
    assert store.path.read_bytes() == before


def test_second_remote_process_stays_standby(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    store.detach(local)
    writer[0] = None
    first, second = make_agent(), make_agent()
    assert first.step(observe=False)[0]["state"] == "owned"
    assert second.step(observe=False)[0]["state"] == "standby"
    assert len(clients) == 1 and store.read()["epoch"] == 2


@pytest.mark.skipif(sys.platform == "win32", reason="host helper requires an absolute POSIX repository path")
def test_attached_completion_is_sent_by_host_once_without_resuming_vscode(scenario):
    from codex_watchdog.mvp_service import MvpWatchdogService
    from codex_watchdog.control_state import ControlError
    store, local, writer, clock, clients, make_agent = scenario
    agent = make_agent()
    sent = []

    def service_factory(runtime, **kwargs):
        ns = kwargs["remote_ssh_adapter"].namespace
        ns["ControlStore"] = lambda *args: store
        ns["ControlError"] = ControlError
        ns["control_exact_thread"] = lambda repo, thread: repo == store.repo_path and thread == THREAD
        ns["git_observation"] = lambda repo: dict(status="observed", topology="equal", blockers=[],
                                                  head_oid="a" * 40, upstream_oid="a" * 40)
        ns["rollout_completion"] = lambda thread: dict(
            turn_id="five-minute-turn", completed_at="2026-01-01T00:00:00Z", final_output="yes",
            final_output_sha256="8a798890fe93817163b10b5f7bd2ca4d25d84c52739a645a889c173eee7d9d3d",
            final_output_chars=3,
        )
        notifier = EnvironmentNotifier(runtime,
            config=NotificationConfig(slack_webhook_url="https://hooks.slack.invalid/host"),
            http_post=lambda url, payload, timeout: sent.append(json.loads(payload)["text"]) or 200)
        return MvpWatchdogService(runtime, notifier=notifier, **kwargs)

    agent.service_factory = service_factory
    result = agent.step()[0]
    assert result["state"] == "observing", result
    assert len(sent) == 1 and sent[0].rstrip().endswith("yes")
    assert store.read()["owner"]["instance"] == agent.instance
    assert store.read()["remote_state"]["last_completion_turn"] == "five-minute-turn"
    assert writer[0] == 777 and clients == []
    assert agent.step()[0]["state"] == "observing"
    assert len(sent) == 1
    with pytest.raises(ControlError, match="stale_epoch"):
        store.prepare_notification(local, "desktop-copy", "desktop-copy")
    assert len(list((store.directory / "effects").glob("*.json"))) == 1


def test_expired_observer_reacquires_after_desktop_fallback_without_resuming(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent = make_agent()
    assert agent.step(observe=False)[0]["state"] == "observing"
    clock[0] += 121
    assert store.attach("fallback", "desktop-host", "vscode")["epoch"] == 3
    assert agent.step(observe=False)[0]["reason"] == "control_stale_epoch"
    assert not agent.controllers and not clients
    result = agent.step(observe=False)[0]
    assert result["state"] == "observing" and result["epoch"] == 4
    assert not clients and writer[0] == 777


def test_stopping_observer_releases_authority_without_touching_vscode(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent = make_agent()
    assert agent.step(observe=False)[0]["state"] == "observing"
    agent.stopping = True
    assert agent.step(observe=False)[0]["state"] == "released"
    assert writer[0] == 777 and not clients and not agent.controllers
    assert store.attach("desktop", "desktop-host", "vscode")["epoch"] == 3


def test_operator_release_stays_paused_until_explicit_bind(scenario, monkeypatch):
    from codex_watchdog import control_state
    from codex_watchdog.control_state import ControlError
    store, local, writer, clock, clients, make_agent = scenario
    agent = make_agent()
    monkeypatch.setattr(control_state, "ControlStore", lambda *a, **kw: store)
    monkeypatch.setattr(linux_auto, "ControlStore", lambda *a, **kw: store)
    store.detach(local)
    writer[0] = None
    agent.step(observe=False)
    binding = agent.controllers[THREAD]["owner"].binding
    workspace = binding.workspace(binding.load())
    # A stale worker cannot bypass its capability by directly changing binding state.
    with pytest.raises(ControlError, match="capability_required"):
        binding.set_state("released")
    with pytest.raises(ControlError, match="capability_required"):
        binding.bind(workspace, 60)
    binding.request_release()
    assert agent.step(observe=False)[0]["state"] == "released"
    assert agent.step(observe=False)[0]["state"] == "standby"
    assert len(clients) == 1 and store.read()["auto_paused"]
    linux_auto.bind_for_operator(binding, workspace, 60)
    assert not store.read()["auto_paused"]
    assert agent.step(observe=False)[0]["state"] == "owned"
    assert len(clients) == 2


def test_exited_backend_releases_stale_claim_and_recovers_same_thread(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    exited = clients[0]
    exited.process.returncode = -15
    writer[0] = None
    result = agent.step(observe=False)[0]
    assert result["reason"] == "app_server_exited"
    assert json.loads(owner.status_path.read_text())["owner_state"] == "blocked"
    assert not agent.controllers and exited.closed
    assert store.read()["owner"] is None and store.read()["writer_pid"] is None
    desktop = store.attach("returning-desktop", "desktop-host", "absent")
    assert desktop is not None  # A dead controller cannot block its workspace.
    assert agent.step(observe=False)[0]["state"] == "owned"
    assert len(clients) == 2
    assert all(params["threadId"] == THREAD for client in clients for _, params in client.calls)
    assert not any(method == "turn/start" for client in clients for method, _ in client.calls)


def test_exited_backend_preserves_replacement_vscode_writer(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    clients[0].process.returncode = -9
    writer[0] = 777
    assert agent.step(observe=False)[0]["reason"] == "app_server_exited"
    assert not agent.controllers and writer[0] == 777
    assert agent.step(observe=False)[0]["state"] == "observing"
    assert len(clients) == 1 and writer[0] == 777


def test_failed_monitor_does_not_renew_forever_or_close_live_backend(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    writer[0] = None  # Uncertain writer loss; the owned child is still alive.
    assert agent.step(observe=False)[0]["state"] == "blocked"
    expires = store.read()["owner"]["expires"]
    clock[0] += 60
    assert agent.step(observe=False)[0]["state"] == "blocked"
    assert store.read()["owner"]["expires"] == expires
    assert not clients[0].closed and len(clients) == 1
    assert json.loads(owner.status_path.read_text())["reason"] == "linux_writer_changed"


@pytest.mark.parametrize("matches", [False, True])
def test_repository_scope_selects_exact_path_and_keeps_native_thread_identity(scenario, matches):
    store, local, writer, clock, clients, make_agent = scenario
    selected = Path(store.repo_path) if matches else Path(store.repo_path).parent / "other" / Path(store.repo_path).name
    agent = make_agent(repos=(str(selected),))
    before = store.path.read_bytes()
    result = agent.step(observe=False)
    if matches:
        assert result[0]["state"] == "observing"
        assert set(agent.controllers) == {THREAD}
    else:
        assert result == [] and store.path.read_bytes() == before
    assert not clients and writer[0] == 777


def test_repository_scope_includes_new_registered_thread_without_merging_siblings(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    second = "22222222-3333-4444-8555-666666666666"
    home = store.directory.parent.parent
    repo = Path(store.repo_path)
    rollout = home / "sessions" / "second.jsonl"
    rollout.write_text("{}\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("INSERT INTO threads VALUES (?,?,'vscode','user',0,?)", (second, str(repo), str(rollout)))
    other = ControlStore(home, second, repo, clock=lambda: clock[0], boot_id="boot")
    token = other.attach("other-desktop", "desktop-host", "vscode")
    with other.guard(token) as value:
        value["runtime_path"] = str(other.directory / "runtime")
        value["remote_target"] = dict(authority="ssh-remote+example.invalid", repo_path=str(repo), storage_key="a" * 32)
        control_atomic_json(other.path, value)
    agent = make_agent(repos=(str(repo),))
    assert [row["state"] for row in agent.step(observe=False)] == ["observing", "observing"]
    assert set(agent.controllers) == {THREAD, second}
    assert len({item["target"].workspace_id for item in agent.controllers.values()}) == 2
    assert {item["owner"].binding.load()["thread_id"] for item in agent.controllers.values()} == {THREAD, second}
    assert not clients and writer[0] == 777


def test_initial_resume_exit_is_not_retried_until_native_attachment(scenario, monkeypatch):
    store, local, writer, clock, clients, make_agent = scenario
    store.detach(local)
    writer[0] = None
    agent = make_agent()
    original = LinuxThreadOwner._resume

    def fail(owner, workspace):
        owner.client = owner.client_factory(owner.executable, owner.binding.codex_home, workspace.repo_root, owner._event)
        owner.client.process.returncode = 1
        raise AppServerError("app_server_exited")

    monkeypatch.setattr(LinuxThreadOwner, "_resume", fail)
    assert agent.step(observe=False)[0]["reason"] == "app_server_exited"
    assert not agent.controllers and store.read()["owner"] is None
    monkeypatch.setattr(LinuxThreadOwner, "_resume", original)
    assert agent.step(observe=False)[0]["state"] == "blocked"
    assert len(clients) == 1
    writer[0] = 777
    assert agent.step(observe=False)[0]["state"] == "observing"
    assert len(clients) == 1


def test_exited_backend_preserves_uncertain_notification_barrier(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent, owner = detached(scenario)
    class UncertainNotifier:
        def notify(self, event):
            raise RuntimeError("interrupted notification")
    owner.health.notifier = UncertainNotifier()
    clients[0].process.returncode = -15
    writer[0] = None
    assert agent.step(observe=False)[0]["recovery"]["reason"] == "control_release_not_safe"
    barrier = store.read()["external_effect"]
    assert barrier is not None and not agent.controllers
    assert agent.step(observe=False)[0]["reason"] == "control_external_effect_unresolved"
    assert store.read()["external_effect"] == barrier and len(clients) == 1


def test_auto_cli_passes_repository_scope(scenario, monkeypatch):
    from codex_watchdog import cli
    store, local, writer, clock, clients, make_agent = scenario
    calls = []
    class Agent:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
        def run(self, *args, **kwargs):
            return 0
    monkeypatch.setattr(linux_auto, "LinuxAutoWatchdog", Agent)
    assert cli.main(["--runtime", str(store.directory / "runtime"), "--codex-home", str(store.directory.parent.parent),
                     "linux-auto-run", "--repo", store.repo_path]) == 0
    assert calls[0]["repos"] == [Path(store.repo_path)]
    assert calls[0]["threads"] == []
