from pathlib import Path
import json
import os
import sqlite3
import sys

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.control_context import acting_as, current_effect, hook_owner
from codex_watchdog.linux_auto import HostRemoteAdapter, LinuxAutoWatchdog
from codex_watchdog.linux_binding import LinuxBinding, LinuxBindingError, reservation_path
from codex_watchdog.remote_control import RemoteControlClient
from codex_watchdog.remote_ssh import RemoteSshTarget


THREAD = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.sys, "platform", "linux")
    host = ["login-a.example"]
    monkeypatch.setattr(cs.socket, "gethostname", lambda: host[0])
    home = tmp_path / "codex"
    repo = tmp_path / "project"
    repo.mkdir()
    rollout = home / "sessions/thread.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    (home / "thread-writer-locks").mkdir()
    (home / "thread-writer-locks" / (THREAD + ".lock")).touch()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id,cwd,source,thread_source,archived,rollout_path)")
        db.execute("INSERT INTO threads VALUES (?,?,'vscode','user',0,?)", (THREAD, str(repo), str(rollout)))
    roots = {}
    for node in ("login-a.example", "login-b.example"):
        host[0] = node
        root = cs.control_node_directory(home)
        cs.control_atomic_json(root / "node.json", dict(schema_version=1, node=node, codex_home=str(home)))
        roots[node] = root
    host[0] = "login-a.example"
    pid = [123]
    monkeypatch.setattr(cs, "control_kernel_owner", lambda path: pid[0])
    monkeypatch.setattr(cs, "control_vscode_writer", lambda value: value == 123)
    return home, repo, host, roots, pid


def make_store(home, repo, boot="boot-a"):
    return cs.ControlStore(home, THREAD, repo, clock=lambda: 100, boot_id=boot)


def test_each_node_enrolls_only_its_native_writer_and_preserves_shared_history(nodes):
    home, repo, host, roots, pid = nodes
    a = make_store(home, repo)
    assert a.enroll_node()
    original = a.path.read_bytes()
    assert not a.enroll_node()
    host[0] = "login-b.example"
    b = make_store(home, repo)
    assert a.directory != b.directory
    pid[0] = None
    with pytest.raises(cs.ControlError, match="initial_attachment_unverified"):
        b.enroll_node()
    assert not b.path.exists()
    assert a.path.read_bytes() == original
    assert not (home / "watchdog-control").exists()
    assert cs.control_native_repo(home, THREAD) == str(repo)
    assert reservation_path(home, THREAD).parent == roots[host[0]] / "watchdog-linux"


def test_reboot_needs_fresh_local_writer_before_resuming_shared_home_thread(nodes):
    home, repo, _, _, pid = nodes
    a = make_store(home, repo)
    a.enroll_node()
    after_reboot = make_store(home, repo, "boot-new")
    pid[0] = None
    assert after_reboot.claim_remote("dog", "host-a", "absent", host_observer=True) is None
    pid[0] = 123
    token = after_reboot.claim_remote("dog", "host-a", "vscode", host_observer=True)
    assert token is not None
    assert after_reboot.read()["native_boot_id"] == "boot-new"


def test_effect_guard_keeps_native_home_separate_from_state_namespace(nodes):
    home, repo, _, _, _ = nodes
    store = make_store(home, repo)
    store.enroll_node()
    token = store.claim_remote("dog", "host-a", "vscode", host_observer=True)
    with acting_as(store, token), current_effect():
        assert store.codex_home == home
        assert store.read()["epoch"] == token["epoch"]
    assert not (home / "watchdog-control").exists()


def test_existing_hook_command_cannot_write_legacy_runtime_on_a_new_node(nodes):
    home, _, _, roots, _ = nodes
    with hook_owner(home / "legacy-runtime", home, {"session_id": THREAD}) as runtime:
        assert runtime == roots["login-a.example"] / "runtime"
    with pytest.raises(LinuxBindingError, match="outside_namespace"):
        LinuxBinding(home / "legacy-runtime", home)


def test_shared_logs_do_not_select_a_thread_on_another_node(nodes):
    home, repo, _, roots, pid = nodes
    adapter = HostRemoteAdapter(home)
    ns = adapter.namespace
    ns["control_kernel_owner"] = lambda path: pid[0]
    ns["control_vscode_writer"] = lambda value: value == 123
    ns["log_session_state"] = lambda thread: (True, True)
    ns["ControlStore"] = lambda h, t, r: make_store(h, r)
    ns["ControlError"] = cs.ControlError
    ns["control_exact_thread"] = lambda r, t: r == "/work/project" and t == THREAD
    target = RemoteSshTarget("ssh-remote+cluster.example", "/work/project", "a" * 32, (THREAD,))
    client = RemoteControlClient(adapter, roots["login-a.example"] / "runtime")
    pid[0] = None
    with pytest.raises(cs.ControlError, match="remote_thread_unresolved"):
        client.acquire(target, None)
    result = adapter.probe(target, control=dict(action="relay", thread_id=THREAD),
                           wake=dict(instruction_id="reply", prompt="test"))
    assert result["reason"] == "control_node_thread_unregistered"
    assert ns["wake_record_path"]("reply").parent == roots["login-a.example"] / "remote-wake"
    assert not (home / "watchdog-control").exists()


def test_node_discovery_filters_native_targets_before_creating_records(nodes, monkeypatch):
    from codex_watchdog import linux_auto
    home, repo, _, roots, pid = nodes
    monkeypatch.setattr(linux_auto, "locality_identity", lambda: "host-a")
    monkeypatch.setattr(linux_auto, "writer_pid", lambda *args: pid[0])
    monkeypatch.setattr(linux_auto, "vscode_writer", lambda p: p == 123)
    runtime = roots["login-a.example"] / "runtime"
    dog = LinuxAutoWatchdog(runtime, home, store_factory=lambda h, t, r: make_store(h, r))
    pid[0] = None
    assert dog._discover_native() == []
    assert not make_store(home, repo).path.exists()
    pid[0] = 123
    dog.exclude = (str(repo).casefold(),)
    assert dog._discover_native() == []
    assert not make_store(home, repo).path.exists()
    dog.exclude = ()
    assert dog._discover_native() == []
    assert make_store(home, repo).read()["runtime_path"] == str(make_store(home, repo).directory / "runtime")


def test_unconfigured_host_keeps_previous_paths(nodes):
    home, repo, host, _, _ = nodes
    host[0] = "login-legacy.example"
    assert make_store(home, repo).directory == home / "watchdog-control" / THREAD
    assert reservation_path(home, THREAD) == home / "watchdog-linux" / (THREAD + ".json")


def test_bad_marker_never_falls_back_to_shared_state(nodes):
    home, _, host, roots, _ = nodes
    cs.control_atomic_json(roots[host[0]] / "node.json", dict(schema_version=1, node="wrong", codex_home=str(home)))
    with pytest.raises(cs.ControlError, match="configuration_mismatch"):
        cs.control_root(home)


def test_slack_polling_keeps_mappings_and_cursors_with_their_originating_node(nodes):
    from types import SimpleNamespace
    from codex_watchdog.notifications import NotificationConfig
    from codex_watchdog.slack_relay import SlackReplyRelay
    from codex_watchdog.slack_mapping import SlackRelayTarget
    from codex_watchdog.slack_poll import SlackReplyPoller
    home, repo, host, _, _ = nodes
    a = make_store(home, repo)
    a.enroll_node()
    config = NotificationConfig(slack_bot_token="xoxb-test", slack_channel_id="C12345678",
                                slack_allowed_user_ids=("U12345678",), slack_reply_mode="poll")
    relay_a = SlackReplyRelay.from_notification_config(a.directory / "runtime", config,
                                                       queue_dispatcher=SimpleNamespace(), remote_ssh_adapter=None)
    relay_a.thread_store.record_thread("C12345678", "1789111611.674949",
                                      SlackRelayTarget("a", THREAD, "process_local"), "a" * 64)
    host[0] = "login-b.example"
    b = make_store(home, repo)
    relay_b = SlackReplyRelay.from_notification_config(b.directory / "runtime", config,
                                                       queue_dispatcher=SimpleNamespace(), remote_ssh_adapter=None)
    assert relay_b.thread_store.poll_mappings() == {}
    assert SlackReplyPoller(relay_b, api=lambda *args: pytest.fail("foreign mapping polled")).poll_once() == []
    assert len(relay_a.thread_store.poll_mappings()) == 1
    assert SlackReplyPoller(relay_a).path != SlackReplyPoller(relay_b).path


@pytest.mark.skipif(sys.platform != "linux", reason="native Linux private-file and systemd-unit acceptance")
def test_node_unit_install_is_host_conditioned_private_and_idempotent(tmp_path, monkeypatch):
    from codex_watchdog.linux_node import prepare_node
    monkeypatch.setattr(cs.socket, "gethostname", lambda: "node-a.example")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    home = tmp_path / "codex"
    executable = tmp_path / "bin with spaces" / "codex-watchdog"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    env = tmp_path / "private.env"
    env.write_text("SLACK_CHANNEL_ID=fixture\n")
    env.chmod(0o600)
    preview = prepare_node(executable, home, env)
    assert not home.exists()
    assert "ConditionHost=node-a.example" in preview["unit"]
    assert "CODEX_WATCHDOG_SLACK_REPLY_MODE=poll" in preview["unit"]
    assert "SLACK_CHANNEL_ID" not in preview["unit"]
    installed = prepare_node(executable, home, env, install=True)
    assert installed["status"] == "installed"
    assert prepare_node(executable, home, env, install=True) == installed
    with pytest.raises(cs.ControlError, match="existing_unit_conflict"):
        prepare_node(executable, home, env, interval=15, install=True)
    assert cs.control_state_home(home) == home / "watchdog-nodes/node-a.example"
    assert Path(installed["unit_path"]).stat().st_mode & 0o077 == 0
    # Unknown durable configuration is preserved by repeated setup.
    marker = cs.control_state_home(home) / "node.json"
    value = json.loads(marker.read_text())
    value["future_choice"] = "preserve"
    cs.control_atomic_json(marker, value)
    prepare_node(executable, home, env, install=True)
    assert json.loads(marker.read_text())["future_choice"] == "preserve"
    # The same shared home on another node does not activate this namespace.
    monkeypatch.setattr(cs.socket, "gethostname", lambda: "node-b.example")
    assert cs.control_state_home(home) == home
    from codex_watchdog.linux_node import locality_identity
    legacy = home / "watchdog-linux" / (THREAD + ".json")
    cs.control_atomic_json(legacy, dict(schema_version=1, locality_sha256=locality_identity()))
    before = legacy.read_bytes()
    with pytest.raises(cs.ControlError, match="legacy_migration_required"):
        prepare_node(executable, home, env, install=True)
    assert legacy.read_bytes() == before
