from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace

import pytest

from codex_watchdog import linux_auto, linux_binding, linux_owner
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
            self.process = SimpleNamespace(pid=900 + len(clients))
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
            pass

        def close(self):
            self.closed = True
            if writer[0] == self.process.pid:
                writer[0] = None

    def make_agent(exclude=()):
        return LinuxAutoWatchdog(
            tmp_path / "agent-runtime", home, executable="fixture-codex", exclude=exclude,
            store_factory=factory,
            owner_factory=lambda binding, **kwargs: LinuxThreadOwner(binding, client_factory=Client, **kwargs),
            service_factory=lambda runtime, **kwargs: SimpleNamespace(
                run_once=lambda: pytest.fail("unexpected cycle"),
                notifier=EnvironmentNotifier(runtime, config=NotificationConfig()),
            ),
        )

    agents = []

    def agent(exclude=()):
        value = make_agent(exclude)
        agents.append(value)
        return value

    yield store, local, writer, clock, clients, agent
    for dog in agents:
        for item in dog.controllers.values():
            if item["owner"].client is not None:
                item["owner"].client.close()
            item["locks"].close()


def test_automatic_exact_thread_lifecycle_and_idle_handback(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    agent = make_agent()
    assert agent.step(observe=False)[0]["state"] == "standby"
    assert clients == []
    assert not (store.directory / "runtime" / "linux" / "binding.json").exists()
    store.detach(local)
    assert agent.step(observe=False)[0]["state"] == "standby"
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
    assert released["state"] == "released"
    assert clients[0].closed and writer[0] is None
    assert agent.controllers == {}
    assert store.attach("desktop-returned", "desktop-host", "vscode")["epoch"] == 3
    assert agent.step(observe=False)[0]["state"] == "standby"


def test_desktop_crash_takeover_and_remote_crash_restart_keep_same_thread(scenario):
    store, local, writer, clock, clients, make_agent = scenario
    first = make_agent()
    clock[0] += 21
    assert first.step(observe=False)[0]["state"] == "standby"
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
    monkeypatch.setattr(agent, "_cycle", lambda item: None)
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
    assert agent.step(observe=False)[0]["state"] == "released"
    assert not owner.health.path.exists()
    assert not list((store.directory / "effects").glob("*.json"))


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
    monkeypatch.setattr(restarted, "_cycle", lambda item: None)
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
