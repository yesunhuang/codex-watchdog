from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from codex_watchdog import linux_auto, linux_binding, linux_owner
from codex_watchdog.control_state import ControlStore, control_atomic_json
from codex_watchdog.linux_auto import LinuxAutoWatchdog
from codex_watchdog.linux_owner import LinuxThreadOwner


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
            service_factory=lambda *args, **kwargs: SimpleNamespace(run_once=lambda: pytest.fail("unexpected cycle")),
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
