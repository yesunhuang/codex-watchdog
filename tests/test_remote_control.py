import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from codex_watchdog.control_state import ControlStore, ControlError, control_atomic_json
from codex_watchdog.linux_auto import HostRemoteAdapter
from codex_watchdog.models import sha256_text
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.notifications import NotificationResult
from codex_watchdog.remote_control import RemoteControlClient
from codex_watchdog.remote_ssh import RemoteSshTarget
from codex_watchdog.queue_wake import REMOTE_UPDATE_PROMPT


THREAD = "11111111-2222-4333-8444-555555555555"
QUEUE = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
REPO = "/work/disposable"


@pytest.fixture
def helper(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("CODEX_HOME", str(codex))
    clock = [100.0]
    writer = [777]
    adapter = HostRemoteAdapter(codex)
    ns = adapter.namespace
    ns["ControlStore"] = lambda c, t, r: ControlStore(c, t, r, clock=lambda: clock[0], boot_id="boot")
    ns["ControlError"] = ControlError
    ns["control_kernel_owner"] = lambda p: writer[0]
    ns["control_vscode_writer"] = lambda pid: pid == 777
    ns["control_exact_thread"] = lambda repo, thread: repo == REPO and thread == THREAD
    ns["log_session_state"] = lambda thread: (writer[0] == 777, True)
    ns["resolve_session"] = lambda *args: (THREAD, None)
    ns["git_observation"] = lambda repo: dict(status="observed", topology="equal", head_oid="a" * 40,
                                               upstream_oid="a" * 40, blockers=[])
    ns["rollout_completion"] = lambda thread: None
    target = RemoteSshTarget("ssh-remote+example.invalid", REPO, "a" * 32, (THREAD,))
    store = ns["ControlStore"](codex, THREAD, REPO)
    return adapter, target, store, clock, writer


class Notifier:
    def __init__(self, runtime):
        self.runtime = runtime
        self.events = []

    def notify(self, event):
        self.events.append(event)
        return NotificationResult("sent", "test", event.event_fingerprint(), False, ("test",), (),
                                  self.runtime / "notifications.json", True)


def make_service(helper, tmp_path, notifier=None):
    adapter, target, store, clock, writer = helper

    class Registry:
        def __init__(self):
            self.visible = True

        def list_workspaces(self):
            window = SimpleNamespace(locality="remote_ssh", remote_authority=target.authority,
                                     workspace_path=target.repo_path, workspace_storage_key=target.storage_key,
                                     session_candidates=(THREAD,), tracking_status="remote_adapter")
            self.last_snapshot = SimpleNamespace(status="ok", windows=(window,) if self.visible else (),
                                                  effective_workspaces=(), issues=())
            return []

    runtime = tmp_path / "desktop"
    service = MvpWatchdogService(runtime, registry=Registry(), remote_ssh_adapter=adapter,
                                 notifier=notifier or Notifier(runtime), codex_home=tmp_path / "desktop-codex")
    return service


def test_desktop_service_auto_observes_and_detaches_without_manual_binding(helper, tmp_path):
    service = make_service(helper, tmp_path)
    adapter, target, store, clock, writer = helper
    first = service.run_once()
    assert first.workspaces[0].status == "completed"
    assert store.read()["state"] == "ATTACHED_LOCAL"
    assert store.read()["epoch"] == 1
    assert store.read()["runtime_path"] == str(store.directory / "runtime")
    assert not (store.directory.parent.parent / "watchdog-linux").exists()
    assert store.claim_remote("remote", "host", "vscode") is None
    service.registry.visible = False
    closed = service.run_once()
    assert closed.workspaces[0].reason == "control_desktop_detached"
    assert store.read()["state"] == "HANDOFF"
    assert store.claim_remote("remote", "host", "present") is None
    writer[0] = None
    assert store.claim_remote("remote", "host", "absent")["epoch"] == 2


def test_two_desktops_share_canonical_notification_receipt_across_aliases(helper, tmp_path):
    adapter, target, store, clock, writer = helper
    adapter.namespace["rollout_completion"] = lambda _: dict(
        turn_id="turn-one", completed_at="2026-01-01T00:00:00Z", final_output="done",
        final_output_sha256=sha256_text("done"), final_output_chars=4,
    )
    service = make_service(helper, tmp_path)
    assert service.run_once().workspaces[0].stop_count == 1
    assert len(service.notifier.events) == 1
    second = make_service(helper, tmp_path / "other")
    assert second.run_once().workspaces[0].status == "standby"
    assert second.notifier.events == []
    service.registry.visible = False
    service.run_once()
    alias = RemoteSshTarget("ssh-remote+another-alias.invalid", REPO, "b" * 32, (THREAD,))
    result = second._run_remote_workspace(alias, "cycle-two")
    assert result.status == "completed"
    assert result.stop_count == 0
    assert second.notifier.events == []
    assert store.read()["epoch"] == 2


def test_remote_crash_local_fallback_and_stale_save_are_fenced(helper, tmp_path):
    adapter, target, store, clock, writer = helper
    client = RemoteControlClient(adapter, tmp_path, instance="desktop", locality="desktop-host")
    local = client.acquire(target, {})["control"]["token"]
    client.request(target, "detach", token=local)
    writer[0] = None
    remote = store.claim_remote("remote", "remote-host", "absent", ttl=10)
    writer[0] = 999
    with store.guard(remote) as value:
        value["writer_pid"] = 999
        control_atomic_json(store.path, value)
    assert client.acquire(target, {})["control"]["token"] is None
    assert store.read()["state"] == "HANDOFF"
    clock[0] += 11
    # Writer survival after controller loss remains a barrier to takeover.
    assert client.acquire(target, {})["control"]["token"] is None
    writer[0] = 777
    replacement = client.acquire(target, {})["control"]["token"]
    assert replacement["epoch"] == 3
    before = store.path.read_bytes()
    with pytest.raises(ControlError, match="stale_epoch"):
        client.save(target, remote, dict(session_id=THREAD, repo_path=REPO))
    assert store.path.read_bytes() == before


def test_uncertain_notification_blocks_takeover_instead_of_duplicate(helper, tmp_path):
    adapter, target, store, clock, writer = helper
    client = RemoteControlClient(adapter, tmp_path, instance="desktop", locality="desktop-host")
    token = client.acquire(target, {})["control"]["token"]
    prepared = client.request(target, "notification_prepare", token=token, event_id="event", fingerprint="sha")
    assert prepared["receipt"]["duplicate"] is False
    clock[0] += 1000
    writer[0] = None
    with pytest.raises(ControlError, match="external_effect_unresolved"):
        store.claim_remote("remote", "host", "absent")
    duplicate = client.request(target, "notification_prepare", token=token, event_id="event", fingerprint="sha")
    assert duplicate["receipt"]["duplicate"] is True
    assert duplicate["receipt"]["state"] == "uncertain"
    client.request(target, "notification_finish", token=token, event_id="event",
                   operation_id=prepared["receipt"]["operation_id"], result={"status": "sent"})
    assert store.claim_remote("remote", "host", "absent")["epoch"] == 2


def test_prior_released_binding_runtime_is_reused_from_saved_profile(helper, tmp_path):
    adapter, target, store, clock, writer = helper
    runtime = (tmp_path / "previous-runtime").resolve()
    binding = store.directory.parent.parent / "watchdog-linux" / (THREAD + ".json")
    control_atomic_json(binding, dict(schema_version=1, thread_id=THREAD, state="released",
                                      runtime_sha256=sha256_text(str(runtime))))
    profile = Path.home() / ".local/share" / "codex-watchdog" / "linux-launcher.json"
    control_atomic_json(profile, dict(schema_version=1, runtime=str(runtime), future_choice="keep"))
    before = profile.read_bytes(), binding.read_bytes()
    service = make_service(helper, tmp_path)
    assert service.run_once().workspaces[0].status == "completed"
    assert store.read()["runtime_path"] == str(runtime)
    assert (profile.read_bytes(), binding.read_bytes()) == before


def test_remote_update_uses_one_journal_across_desktop_and_remote_epoch(helper, tmp_path):
    adapter, target, store, clock, writer = helper
    ns = adapter.namespace
    codex = store.directory.parent.parent
    with sqlite3.connect(codex / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id,thread_id,payload_json)")
        db.execute("CREATE TABLE queued_thread_revisions(thread_id,revision)")
    ns["codex_executable"] = lambda: "fixture-codex"
    ns["rollout_path"] = lambda _: None
    calls = []

    def queue(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"Queued message {QUEUE} for thread {THREAD}.\n", "")

    ns["subprocess"] = SimpleNamespace(run=queue, DEVNULL=-3, PIPE=-1, SubprocessError=subprocess.SubprocessError)
    ns["git_observation"] = lambda repo: dict(status="observed", topology="remote_ahead",
                                               head_oid="a" * 40, upstream_oid="b" * 40, blockers=[])
    service = make_service(helper, tmp_path)
    first = service.run_once().workspaces[0]
    assert first.wake["state"] == "enqueued"
    assert len(calls) == 1
    service.registry.visible = False
    service.run_once()
    writer[0] = None
    remote = store.claim_remote("remote", "host", "absent")
    state = store.read()["remote_state"]
    result = service.remote_control.probe(target, remote, wake=dict(
        instruction_id=state["pending_instruction_id"], prompt=REMOTE_UPDATE_PROMPT,
    ))
    assert result["wake"]["state"] == "enqueued"
    assert len(calls) == 1
    result = service.remote_control.probe(target, remote, wake=dict(
        instruction_id=state["pending_instruction_id"], prompt="changed content",
    ))
    # Changed content is an explicit collision and never a second courier call.
    assert result["wake"]["reason"] == "remote_wake_id_collision"
    assert len(calls) == 1


def test_controlled_slack_reply_and_ack_are_once_across_handoff(helper, tmp_path):
    from codex_watchdog.slack_relay import SlackReplyRelay
    from codex_watchdog.slack_mapping import SlackRelayTarget
    adapter, target, store, clock, writer = helper
    client = RemoteControlClient(adapter, tmp_path)
    local = client.acquire(target, {})["control"]["token"]
    sent, acknowledgements = [], []
    adapter.namespace["dispatch_wake"] = lambda request, thread: (
        sent.append((thread, request["prompt"])) or dict(state="enqueued"))
    relay = SlackReplyRelay(tmp_path, bot_token="xoxb-fixture", app_token="xapp-fixture", channel_id="C12345678",
                            allowed_user_ids=("U12345678",), remote_ssh_adapter=adapter,
                            queue_dispatcher=SimpleNamespace())
    relay.thread_store.record_thread("C12345678", "1760000000.000100", SlackRelayTarget(
        "test", THREAD, "remote_ssh", target.authority, REPO, target.storage_key,
    ), "a" * 64)
    event = dict(type="message", user="U12345678", channel="C12345678",
                 thread_ts="1760000000.000100", ts="1760000001.000200", text="continue fixture")
    slack = SimpleNamespace(chat_postMessage=lambda **kw: acknowledgements.append(kw))
    relay._handle_bolt_message(event, {"event_id": "EvOne"}, slack)
    assert len(sent) == len(acknowledgements) == 1
    store.detach(local)
    writer[0] = None
    remote = store.claim_remote("remote", "host", "absent")
    relay._handle_bolt_message(event, {"event_id": "EvOne"}, slack)
    assert len(sent) == len(acknowledgements) == 1
    assert store.read()["epoch"] == remote["epoch"] == 2
    assert not relay.thread_store.lookup_reply("event:EvOne")


def test_stale_slack_ack_cannot_send_after_queue_handoff(helper, tmp_path, monkeypatch):
    from codex_watchdog.slack_relay import SlackReplyRelay, SlackReplyResult
    adapter, target, store, clock, writer = helper
    client = RemoteControlClient(adapter, tmp_path)
    local = client.acquire(target, {})["control"]["token"]
    relay = SlackReplyRelay(tmp_path, bot_token="xoxb-fixture", app_token="xapp-fixture", channel_id="C12345678",
                            allowed_user_ids=("U12345678",), remote_ssh_adapter=adapter,
                            queue_dispatcher=SimpleNamespace())
    result = SlackReplyResult("queued", "test", "instruction", "enqueued", control=(client, target, local))
    monkeypatch.setattr(relay, "handle_message", lambda *a, **kw: result)
    store.detach(local)
    store.claim_remote("remote", "host", "absent")
    sent = []
    relay._handle_bolt_message(dict(channel="C12345678", thread_ts="1760000000.000100"), {},
                               SimpleNamespace(chat_postMessage=lambda **kw: sent.append(kw)))
    assert sent == []


def test_healthy_standby_and_control_transport_error_are_distinct(helper, tmp_path):
    first = make_service(helper, tmp_path)
    first.run_once()
    standby = make_service(helper, tmp_path / "second").run_once()
    assert standby.ok and standby.workspaces[0].status == "standby"
    helper[0].namespace["control_exact_thread"] = lambda *a: (_ for _ in ()).throw(OSError("offline"))
    failed = first.run_once()
    assert not failed.ok and failed.workspaces[0].status == "error"


def test_slack_mapping_survives_upgrade_notification_and_owner_handoff(helper, tmp_path):
    from codex_watchdog.slack_mapping import SlackThreadStore, SlackRelayTarget
    from codex_watchdog.notifications import NotificationEvent
    adapter, target, store, clock, writer = helper
    previous = SlackThreadStore(tmp_path / "previous")
    route = SlackRelayTarget("old", THREAD, "remote_ssh", target.authority, REPO, target.storage_key)
    previous.record_thread("C12345678", "1760000000.000100", route, "a" * 64)
    client = RemoteControlClient(adapter, tmp_path)
    local = client.acquire(target, {}, relay_mappings=previous.mappings_for_threads((THREAD,)))["control"]["token"]
    event = NotificationEvent("test", "codex_stop", "turn-one", "Done", "Fixture only")

    class MappedNotifier(Notifier):
        slack_thread_store = previous

        def notify(self, event):
            previous.record_thread("C12345678", "1760000001.000200", route, event.event_fingerprint())
            return super().notify(event)

    client.notify(target, local, event, MappedNotifier(tmp_path))
    store.detach(local)
    writer[0] = None
    remote = store.claim_remote("remote", "host", "absent")
    replacement = SlackThreadStore(tmp_path / "remote")
    mappings = store.slack_mappings()
    replacement.cache_mappings(mappings, SlackRelayTarget("new", THREAD, "process_local"))
    for ts in ("1760000000.000100", "1760000001.000200"):
        assert replacement.lookup_thread("C12345678", ts).target.thread_id == THREAD
    before = replacement.path.read_bytes()
    replacement.cache_mappings(mappings, SlackRelayTarget("new", THREAD, "process_local"))
    assert replacement.path.read_bytes() == before
    assert client.notify(target, remote, event, MappedNotifier(tmp_path))["duplicate"]
