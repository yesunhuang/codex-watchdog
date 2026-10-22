import json
import os
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


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="native same-user kernel proof requires POSIX")
@pytest.mark.parametrize("pid,kind", [(None, "absent"), (777, "vscode")])
def test_foreign_writer_observation_is_exact_current_read_only(helper, pid, kind):
    adapter, target, store, clock, writer = helper
    path = store.codex_home / "thread-writer-locks" / (THREAD + ".lock")
    path.parent.mkdir()
    path.write_bytes(b"native lock fixture")
    writer[0] = pid
    result = adapter.probe(target, control=dict(action="writer_observation", thread_id=THREAD))
    assert result["status"] == "ok"
    assert result["thread_id"] == THREAD and result["repo_path"] == REPO
    assert result["writer_kind"] == kind and result["boot_id"] == "boot"
    assert result["uid"] == os.getuid() and result["writer_lock_inode"] == path.stat().st_ino
    assert result["observed_at"] > 0 and not store.path.exists()
    assert path.read_bytes() == b"native lock fixture"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="native same-user kernel proof requires POSIX")
@pytest.mark.parametrize("missing,pid", [(True, None), (False, 888)])
def test_foreign_writer_observation_never_calls_missing_or_unknown_absent(helper, missing, pid):
    adapter, target, store, clock, writer = helper
    if not missing:
        path = store.codex_home / "thread-writer-locks" / (THREAD + ".lock")
        path.parent.mkdir()
        path.write_bytes(b"native lock fixture")
    writer[0] = pid
    result = adapter.probe(target, control=dict(action="writer_observation", thread_id=THREAD))
    assert result["status"] == "unavailable" and result["reason"] == "control_native_writer_unverified"
    assert not store.path.exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="native same-user kernel proof requires POSIX")
def test_foreign_native_proof_preserves_accepted_host_local_lock_alias(helper, tmp_path, monkeypatch):
    adapter, target, store, clock, writer = helper
    base = tmp_path / "native-user-runtime"
    base.mkdir(mode=0o700)
    native = base / "codex-native-runtime"
    native.mkdir(mode=0o700)
    locks = native / "thread-writer-locks"
    locks.mkdir(mode=0o700)
    path = locks / (THREAD + ".lock")
    path.write_bytes(b"existing host-local native lock")
    logical = store.codex_home / "thread-writer-locks"
    logical.symlink_to(locks, target_is_directory=True)
    adapter.namespace["control_native_runtime_root"] = lambda: native
    writer[0] = None
    result = adapter.probe(target, control=dict(action="writer_observation", thread_id=THREAD))
    assert result["status"] == "ok" and result["writer_kind"] == "absent"
    assert result["writer_lock_path"] == str(path) and result["writer_lock_inode"] == path.stat().st_ino
    assert logical.is_symlink() and path.read_bytes() == b"existing host-local native lock"
    # An arbitrary replacement alias is not the supported native boundary.
    adapter.namespace["control_native_runtime_root"] = lambda: base / "unrelated"
    refused = adapter.probe(target, control=dict(action="writer_observation", thread_id=THREAD))
    assert refused["status"] == "unavailable" and not store.path.exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="native same-user kernel proof requires POSIX")
@pytest.mark.parametrize("alias", [False, True])
def test_foreign_missing_lock_is_explicit_read_only_absence_under_private_parent(helper, tmp_path, alias):
    adapter, target, store, _, _ = helper
    logical = store.codex_home / "thread-writer-locks"
    if alias:
        base = tmp_path / "native-user-runtime"
        base.mkdir(mode=0o700)
        native = base / "codex-native-runtime"
        native.mkdir(mode=0o700)
        parent = native / "thread-writer-locks"
        parent.mkdir(mode=0o700)
        logical.symlink_to(parent, target_is_directory=True)
        adapter.namespace["control_native_runtime_root"] = lambda: native
    else:
        parent = logical
        parent.mkdir(mode=0o700)
    lock = parent / (THREAD + ".lock")
    identity = parent.stat()
    def unexpected_kernel_read(*args):
        pytest.fail("missing leaf must never be opened or created to probe a kernel owner")
    adapter.namespace["control_kernel_owner"] = unexpected_kernel_read
    result = adapter.probe(target, control=dict(action="writer_observation", thread_id=THREAD))
    assert result["status"] == "ok" and result["writer_kind"] == "absent"
    assert result["writer_lock_exists"] is False and result["writer_lock_inode"] is None
    assert result["writer_lock_path"] == str(lock)
    assert result["writer_lock_parent_path"] == str(parent)
    assert result["writer_lock_parent_inode"] == identity.st_ino
    assert result["uid"] == os.getuid() and result["boot_id"] == "boot"
    assert not lock.exists() and not store.path.exists() and list(parent.iterdir()) == []
    assert parent.stat().st_mtime_ns == identity.st_mtime_ns
    assert logical.is_symlink() is alias
    # Other callers still require a real existing lock by default.
    with pytest.raises(ControlError, match="native_writer_unverified"):
        adapter.namespace["control_native_writer_lock"](store.codex_home, THREAD)


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="native same-user kernel proof requires POSIX")
@pytest.mark.parametrize("fault", ["public_parent", "leaf_symlink", "leaf_permission", "wrong_alias"])
def test_foreign_missing_lock_never_treats_invalid_boundary_as_absence(helper, tmp_path, monkeypatch, fault):
    adapter, target, store, _, _ = helper
    parent = store.codex_home / "thread-writer-locks"
    parent.mkdir(mode=0o700)
    lock = parent / (THREAD + ".lock")
    if fault == "public_parent":
        parent.chmod(0o755)
    elif fault == "leaf_symlink":
        lock.symlink_to(tmp_path / "missing-target")
    elif fault == "wrong_alias":
        other = tmp_path / "untrusted-thread-writer-locks"
        parent.rename(other)
        parent.symlink_to(other, target_is_directory=True)
    else:
        original = Path.lstat
        def denied(path, *args, **kwargs):
            if path == lock:
                raise PermissionError("synthetic inaccessible leaf")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "lstat", denied)
    result = adapter.probe(target, control=dict(action="writer_observation", thread_id=THREAD))
    assert result["status"] == "unavailable" and "writer_kind" not in result
    assert not store.path.exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="native same-user kernel proof requires POSIX")
@pytest.mark.parametrize("race", ["leaf_appears", "parent_replaced", "parent_mode_changed"])
def test_foreign_missing_lock_requires_two_absent_samples_of_same_private_parent(helper, race):
    adapter, target, store, _, _ = helper
    parent = store.codex_home / "thread-writer-locks"
    parent.mkdir(mode=0o700)
    lock = parent / (THREAD + ".lock")
    resolver = adapter.namespace["control_native_writer_lock"]
    calls = []
    def racing_resolver(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            if race == "leaf_appears":
                lock.write_bytes(b"new native writer lock")
            elif race == "parent_replaced":
                parent.rename(parent.with_name("old-native-locks"))
                parent.mkdir(mode=0o700)
            else:
                parent.chmod(0o755)
        return resolver(*args, **kwargs)
    adapter.namespace["control_native_writer_lock"] = racing_resolver
    result = adapter.probe(target, control=dict(action="writer_observation", thread_id=THREAD))
    assert len(calls) == 2
    assert result["status"] == "unavailable" and result["reason"] == "control_native_writer_unverified"
    assert not store.path.exists()
    if race == "leaf_appears":
        assert lock.read_bytes() == b"new native writer lock"


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


@pytest.mark.parametrize("historical", [b"{", b"null", b"x" * 131073],
                         ids=["malformed", "null", "oversized"])
def test_acquire_observes_current_git_without_reading_historical_pending_wake(helper, tmp_path, historical):
    adapter, target, store, clock, writer = helper
    client = RemoteControlClient(adapter, tmp_path, instance="desktop", locality="desktop-host")
    token = client.acquire(target, {})["control"]["token"]
    with store.guard(token) as value:
        value["remote_state"] = dict(session_id=THREAD, repo_path=REPO,
            pending_instruction_id="git:historical", future_choice="retain")
        control_atomic_json(store.path, value)
    path = adapter.namespace["wake_record_path"]("git:historical")
    path.write_bytes(historical)
    original_state = store.read()["remote_state"]
    result = client.acquire(target, {})
    assert result["status"] == "ok" and result["control"]["token"] == token
    assert result["git"]["head_oid"] == "a" * 40 and "wake" not in result
    assert path.read_bytes() == historical and store.read()["remote_state"] == original_state
    # Explicit forensic requests retain their distinct API and conservative
    # error/result semantics; only implicit routine acquisition is removed.
    calls = []
    adapter.namespace["observe_pending_wake"] = lambda *args: calls.append(args) or dict(state="uncertain")
    explicit = client.probe(target, token, pending_instruction_id="git:historical")
    assert explicit["wake"]["state"] == "uncertain"
    assert calls == [("git:historical", THREAD, REPO)]


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


def test_uncertain_notification_preserves_dedup_without_blocking_new_owner(helper, tmp_path):
    adapter, target, store, clock, writer = helper
    client = RemoteControlClient(adapter, tmp_path, instance="desktop", locality="desktop-host")
    token = client.acquire(target, {})["control"]["token"]
    prepared = client.request(target, "notification_prepare", token=token, event_id="event", fingerprint="sha")
    assert prepared["receipt"]["duplicate"] is False
    clock[0] += 1000
    writer[0] = None
    replacement = store.claim_remote("remote", "host", "absent")
    assert replacement["epoch"] == 2
    duplicate = client.request(target, "notification_prepare", token=replacement, event_id="event", fingerprint="sha")
    assert duplicate["receipt"]["duplicate"] is True
    assert duplicate["receipt"]["state"] == "uncertain"
    client.request(target, "notification_finish", token=token, event_id="event",
                   operation_id=prepared["receipt"]["operation_id"], result={"status": "sent"})
    assert store.read()["owner"]["instance"] == "remote"


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
        db.execute("CREATE INDEX queued_items_by_thread ON queued_items(thread_id)")
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
    assert result["wake"]["state"] == "rejected"
    assert result["wake"]["reason"] == "wake_attempt_id_collision"
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
    assert relay.thread_store.lookup_reply("event:EvOne")["state"] == "delivered"


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


def test_two_runtime_copies_cannot_admit_two_replies_to_one_remote_ticket(helper, tmp_path):
    from codex_watchdog.slack_relay import SlackReplyRelay
    from codex_watchdog.slack_mapping import SlackRelayTarget
    adapter, target, store, clock, writer = helper
    RemoteControlClient(adapter, tmp_path).acquire(target, {})
    sent = []
    adapter.namespace["dispatch_wake"] = lambda request, thread: (
        sent.append((thread, request["prompt"])) or dict(state="enqueued"))
    relays = []
    for name in ("first", "second"):
        relay = SlackReplyRelay(tmp_path / name, bot_token="xoxb-fixture", app_token="xapp-fixture",
            channel_id="C12345678", allowed_user_ids=("U12345678",),
            remote_ssh_adapter=adapter, queue_dispatcher=SimpleNamespace())
        relay.thread_store.record_thread("C12345678", "1760000000.000100", SlackRelayTarget(
            "test", THREAD, "remote_ssh", target.authority, REPO, target.storage_key), "a" * 64)
        relays.append(relay)
    event = dict(type="message", user="U12345678", channel="C12345678",
                 thread_ts="1760000000.000100", ts="1760000001.000200", text="first")
    assert relays[0].handle_message(event).status == "queued"
    assert relays[1].handle_message(dict(event, ts="1760000002.000300", text="second")).status == "uncertain"
    assert sent == [(THREAD, "first")]


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


@pytest.mark.parametrize("boundary", ["prepare_reply_lost", "finish_before_commit", "finish_reply_lost"])
def test_sender_restart_retains_exact_rpc_attempt_without_resending(helper, tmp_path, monkeypatch, boundary):
    from codex_watchdog.notification_attempts import remote_attempt
    from codex_watchdog.notifications import NotificationEvent
    adapter, target, store, clock, writer = helper
    client = RemoteControlClient(adapter, tmp_path, instance="desktop", locality="desktop-host")
    token = client.acquire(target, {})["control"]["token"]
    event = NotificationEvent("fixture", "stopped", "one", "Fixture", "Fixture")
    notifier = Notifier(tmp_path)
    original = adapter.probe
    def disconnect(*args, **kwargs):
        action = kwargs.get("control", {}).get("action")
        if action == "notification_finish" and boundary == "finish_before_commit":
            raise ControlError("control_transport_unavailable")
        result = original(*args, **kwargs)
        if ((action == "notification_prepare" and boundary == "prepare_reply_lost")
                or (action == "notification_finish" and boundary == "finish_reply_lost")):
            raise ControlError("control_transport_unavailable")
        return result
    monkeypatch.setattr(adapter, "probe", disconnect)
    if boundary == "prepare_reply_lost":
        with pytest.raises(ControlError, match="transport_unavailable"):
            client.notify(target, token, event, notifier)
    else:
        result = client.notify(target, token, event, notifier)
        assert result["status"] == "sent" and "receipt_commit_error_sha256" in result
    attempted = len(notifier.events)
    assert attempted == (0 if boundary == "prepare_reply_lost" else 1)
    intent = remote_attempt(tmp_path, target, token["thread_id"]).read()
    original_operation = intent["operation_id"]
    monkeypatch.setattr(adapter, "probe", original)
    restarted = RemoteControlClient(adapter, tmp_path, instance="replacement", locality="desktop-host")
    clock[0] += 1000
    replacement = restarted.acquire(target, {})["control"]["token"]
    assert replacement["epoch"] == 2 and store.read()["external_effect"] is None
    receipt = json.loads(store.effect_path("notification", event.event_fingerprint()).read_text())
    assert receipt["state"] == ("completed" if boundary == "finish_reply_lost" else "uncertain")
    assert receipt["operation_id"] == original_operation
    assert restarted.notify(target, replacement, event, notifier)["duplicate"]
    assert len(notifier.events) == attempted
    next_event = NotificationEvent("fixture", "stopped", "two", "Fixture", "Fixture")
    assert restarted.notify(target, replacement, next_event, notifier)["status"] == "sent"
    assert len(notifier.events) == attempted + 1


def test_ended_notifier_exception_and_mapping_failure_do_not_freeze_remote_owner(helper, tmp_path):
    from codex_watchdog.notifications import NotificationEvent
    adapter, target, store, clock, writer = helper
    client = RemoteControlClient(adapter, tmp_path)
    token = client.acquire(target, {})["control"]["token"]
    first = NotificationEvent("fixture", "stopped", "one", "Fixture", "Fixture")
    failing = SimpleNamespace(notify=lambda _: (_ for _ in ()).throw(TimeoutError("fixture")))
    assert client.notify(target, token, first, failing)["status"] == "uncertain"
    assert store.read()["external_effect"] is None
    assert client.notify(target, token, first, failing)["duplicate"]
    mapping = SimpleNamespace(notification_mappings=lambda _: (_ for _ in ()).throw(OSError("fixture")))
    second = NotificationEvent("fixture", "stopped", "two", "Fixture", "Fixture")
    healthy = Notifier(tmp_path)
    healthy.slack_thread_store = mapping
    result = client.notify(target, token, second, healthy)
    assert result["status"] == "sent" and result["mapping_error_sha256"]
    assert store.read()["external_effect"] is None


def test_pending_old_view_does_not_freeze_another_thread_in_same_window(helper, tmp_path, monkeypatch):
    from codex_watchdog.notification_attempts import remote_attempt
    from codex_watchdog.notifications import NotificationEvent
    adapter, target, store, clock, writer = helper
    client = RemoteControlClient(adapter, tmp_path)
    token = client.acquire(target, {})["control"]["token"]
    first = NotificationEvent("fixture", "stopped", "old", "Fixture", "Fixture")
    original = adapter.probe
    def fail_finish(*args, **kwargs):
        if kwargs.get("control", {}).get("action") == "notification_finish":
            raise ControlError("control_transport_unavailable")
        return original(*args, **kwargs)
    monkeypatch.setattr(adapter, "probe", fail_finish)
    assert "receipt_commit_error_sha256" in client.notify(target, token, first, Notifier(tmp_path))
    pending = remote_attempt(tmp_path, target, THREAD).read()
    assert pending["phase"] == "terminal"
    monkeypatch.setattr(adapter, "probe", original)
    other = "22222222-2222-4333-8444-555555555555"
    adapter.namespace["control_exact_thread"] = lambda repo, thread: repo == REPO and thread == other
    new_target = RemoteSshTarget(target.authority, target.repo_path, target.storage_key, (other,))
    next_token = client.acquire(new_target, {})["control"]["token"]
    assert next_token["thread_id"] == other
    notifier = Notifier(tmp_path)
    assert client.notify(new_target, next_token, first, notifier)["status"] == "sent"
    assert len(notifier.events) == 1 and store.read()["external_effect"]["id"] == pending["operation_id"]
    assert remote_attempt(tmp_path, target, THREAD).read() == pending
