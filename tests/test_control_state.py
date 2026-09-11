from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest

from codex_watchdog.control_state import (
    CONTROL_SOURCE, ControlBusy, ControlError, ControlStore, control_atomic_json,
)


THREAD = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def control(tmp_path):
    now = [100.0]
    store = ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo",
                         clock=lambda: now[0], boot_id="boot-one")
    return store, now


def attached(store):
    return store.attach("desktop-one", "desktop-host", "vscode", ttl=20)


def detached(store):
    local = attached(store)
    store.detach(local)
    return store.claim_remote("remote-one", "remote-host", "absent")


def test_initial_attachment_requires_exact_writer_and_remote_never_invents_target(control):
    store, _ = control
    with pytest.raises(ControlError, match="attachment_unverified"):
        store.attach("desktop-one", "desktop-host", "absent")
    with pytest.raises(ControlError, match="record_unreadable"):
        store.claim_remote("remote-one", "remote-host", "absent")
    assert not store.path.exists()
    assert attached(store)["epoch"] == 1


def test_host_priority_fences_live_desktop_and_falls_back_after_host_expiry(control):
    store, now = control
    local = attached(store)
    remote = store.claim_remote("host", "remote-host", "vscode", ttl=30, host_observer=True)
    assert remote["epoch"] == 2
    assert store.read()["owner"]["host_observer"] is True
    with pytest.raises(ControlError, match="stale_epoch"):
        store.prepare_notification(local, "old", "old")
    assert store.attach("desktop-one", "desktop-host", "vscode") is None
    assert store.read()["state"] == "DETACHED_REMOTE"
    assert store.claim_remote("second-host", "remote-host", "vscode", host_observer=True) is None
    now[0] += 31
    fallback = store.attach("desktop-one", "desktop-host", "vscode")
    assert fallback["epoch"] == 3
    with pytest.raises(ControlError, match="stale_epoch"):
        store.renew(remote)
    assert store.claim_remote("host-returned", "remote-host", "vscode", host_observer=True)["epoch"] == 4


def test_host_priority_preserves_uncertain_send_and_completed_receipts(control):
    store, _ = control
    local = attached(store)
    prepared = store.prepare_notification(local, "completion", "fingerprint")
    before = store.path.read_bytes()
    with pytest.raises(ControlError, match="external_effect_unresolved"):
        store.claim_remote("host", "remote-host", "vscode", host_observer=True)
    assert store.path.read_bytes() == before
    store.finish_notification(local, "completion", prepared["operation_id"], {"status": "sent"}, ())
    remote = store.claim_remote("host", "remote-host", "vscode", host_observer=True)
    assert store.prepare_notification(remote, "completion", "fingerprint")["duplicate"]


@pytest.mark.parametrize("writer", ["remote", "unknown", "present"])
def test_host_priority_never_displaces_unverified_or_other_native_writer(control, writer):
    store, _ = control
    attached(store)
    before = store.path.read_bytes()
    assert store.claim_remote("host", "remote-host", writer, host_observer=True) is None
    assert store.path.read_bytes() == before


def test_old_desktop_handoff_does_not_block_host_with_verified_vscode_writer(control, monkeypatch):
    from codex_watchdog import control_state
    store, _ = control
    attached(store)
    token = store.claim_remote("host", "remote-host", "vscode", host_observer=True)
    # v0.2.14-v0.2.16 helpers request HANDOFF even when VS Code already owns it.
    with store.guard(token) as value:
        value.update(state="HANDOFF", attached_request=store._identity("old-desktop", "desktop-host", 900))
        control_atomic_json(store.path, value)
    monkeypatch.setattr(control_state, "control_kernel_owner", lambda lock: 777)
    monkeypatch.setattr(control_state, "control_vscode_writer", lambda pid: pid == 777)
    with store.guard(token, purpose="queue"):
        pass
    assert store.observe_writer(token, "vscode") is False
    assert store.read()["state"] == "DETACHED_REMOTE" and store.read()["epoch"] == 2
    assert store.read()["attached_request"] is None


def test_attach_standby_detach_remote_restart_reattach_handback(control):
    store, now = control
    local = attached(store)
    assert store.claim_remote("remote-one", "remote-host", "vscode") is None
    assert store.attach("desktop-one", "desktop-host", "vscode")["epoch"] == 1
    store.detach(local)
    assert store.claim_remote("remote-one", "remote-host", "vscode") is None
    remote = store.claim_remote("remote-one", "remote-host", "absent")
    assert remote["epoch"] == 2
    now[0] += 31
    assert store.claim_remote("remote-restart", "remote-host", "remote") is None
    restarted = store.claim_remote("remote-restart", "remote-host", "absent")
    assert restarted["epoch"] == 3
    assert store.attach("desktop-returned", "desktop-host", "remote") is None
    assert store.read()["state"] == "HANDOFF"
    with pytest.raises(ControlError, match="handback_pending"):
        with store.guard(restarted, purpose="queue"):
            pytest.fail("a handback cannot accept a new turn")
    with store.guard(restarted, purpose="stop"):
        pass  # Existing work drains before the writer releases.
    with pytest.raises(ControlError, match="release_not_safe"):
        store.release_remote(restarted, "remote")
    store.release_remote(restarted, "absent")
    assert store.claim_remote("remote-other", "remote-host", "absent") is None
    returned = store.attach("desktop-returned", "desktop-host", "vscode")
    assert returned["epoch"] == 4
    assert store.read()["state"] == "ATTACHED_LOCAL"
    for stale in (local, remote, restarted):
        with pytest.raises(ControlError, match="stale_epoch"):
            with store.guard(stale):
                pytest.fail("stale owner revived")


@pytest.mark.parametrize("purpose", ["queue", "stop", "state", "notification", "writer"])
def test_every_effect_rejects_stale_epoch_and_expired_lease(control, purpose):
    store, now = control
    local = attached(store)
    now[0] += 21
    with pytest.raises(ControlError, match="lease_expired"):
        with store.guard(local, purpose=purpose):
            pytest.fail("expired effect")
    replacement = store.attach("desktop-two", "desktop-host-two", "vscode")
    assert replacement["epoch"] == 2
    with pytest.raises(ControlError, match="stale_epoch"):
        with store.guard(local, purpose=purpose):
            pytest.fail("stale effect")


def test_local_crash_or_network_loss_never_steals_a_live_writer(control):
    store, now = control
    local = attached(store)
    now[0] += 21
    assert store.claim_remote("remote-one", "remote-host", "vscode") is None
    assert store.claim_remote("remote-one", "remote-host", "unknown") is None
    with pytest.raises(ControlError, match="lease_expired"):
        store.renew(local)
    assert store.claim_remote("remote-one", "remote-host", "absent")["epoch"] == 2


def test_remote_absent_local_fallback_and_returning_remote(control):
    store, now = control
    local = attached(store)
    # The helper remains a fenced local owner without a remote process.
    with store.guard(local, purpose="queue"):
        pass
    store.detach(local)
    remote = store.claim_remote("remote-one", "remote-host", "absent")
    now[0] += 31
    replacement = store.attach("desktop-fallback", "desktop-host", "vscode")
    assert replacement["epoch"] == 3
    with pytest.raises(ControlError, match="stale_epoch"):
        store.renew(remote)
    assert store.claim_remote("remote-returned", "remote-host", "vscode") is None


def test_simultaneous_desktops_and_remotes_issue_only_one_epoch(control):
    store, _ = control
    barrier = threading.Barrier(2)

    def claim(instance, remote=False):
        barrier.wait()
        try:
            if remote:
                return store.claim_remote(instance, "remote-host", "absent")
            return store.attach(instance, "desktop-host", "vscode")
        except ControlBusy:
            return None

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(claim, "desktop-" + str(i)) for i in range(2)]
        winners = [t for t in (f.result() for f in futures) if t is not None]
    assert len(winners) == 1
    assert store.read()["epoch"] == 1
    store.detach(winners[0])
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(claim, "remote-" + str(i), True) for i in range(2)]
        winners = [t for t in (f.result() for f in futures) if t is not None]
    assert len(winners) == 1
    assert winners[0]["epoch"] == 2


@pytest.mark.parametrize("kind", ["queue", "stop", "notification"])
def test_effect_receipts_deduplicate_across_handoff_and_restart(control, kind):
    store, _ = control
    local = attached(store)
    effects = []
    action = lambda: effects.append(kind)
    first = store.once(local, kind, "event-one", "sha-one", action)
    assert first["state"] == "completed" and first["duplicate"] is False
    store.detach(local)
    remote = store.claim_remote("remote-one", "remote-host", "absent")
    second = store.once(remote, kind, "event-one", "sha-one", action)
    assert second["duplicate"] is True
    assert effects == [kind]
    with pytest.raises(ControlError, match="id_collision"):
        store.once(remote, kind, "event-one", "sha-different", action)


def test_uncertain_send_is_never_replayed_by_a_replacement_owner(control):
    store, now = control
    token = detached(store)
    effects = []

    def interrupted():
        effects.append("sent")
        raise OSError("receipt lost")

    with pytest.raises(OSError):
        store.once(token, "notification", "one", "sha", interrupted)
    now[0] += 31
    replacement = store.claim_remote("remote-restarted", "remote-host", "absent")
    receipt = store.once(replacement, "notification", "one", "sha", interrupted)
    assert receipt["state"] == "uncertain" and receipt["duplicate"]
    assert effects == ["sent"]


def test_external_action_barrier_survives_timeout_disconnect_and_reboot(control):
    store, now = control
    token = attached(store)
    store.begin_external(token, "operation-one")
    now[0] += 3601
    rebooted = ControlStore(store.directory.parent.parent, THREAD, store.repo_path,
                            clock=lambda: 1, boot_id="boot-two")
    for candidate in (store, rebooted):
        with pytest.raises(ControlError, match="external_effect_unresolved"):
            candidate.claim_remote("remote-one", "remote-host", "absent")
        with pytest.raises(ControlError, match="external_effect_unresolved"):
            candidate.attach("desktop-other", "desktop-host", "vscode")
    with pytest.raises(ControlError, match="external_effect_mismatch"):
        store.finish_external(token, "wrong-operation")
    store.finish_external(token, "operation-one")
    assert rebooted.claim_remote("remote-one", "remote-host", "absent")["epoch"] == 2


def test_effect_and_handoff_share_one_kernel_lock(control):
    store, _ = control
    token = attached(store)
    with store.guard(token):
        with pytest.raises(ControlBusy):
            store.detach(token)
        with pytest.raises(ControlBusy):
            store.attach("desktop-two", "desktop-host", "vscode")


def test_unknown_settings_preserved_and_foreign_schema_never_overwritten(control):
    store, _ = control
    token = attached(store)
    value = store.read()
    value["future_settings"] = {"keep": True}
    control_atomic_json(store.path, value)
    store.renew(token)
    assert store.read()["future_settings"] == {"keep": True}
    value["schema_version"] = 2
    control_atomic_json(store.path, value)
    before = store.path.read_bytes()
    with pytest.raises(ControlError, match="unreadable"):
        store.attach("desktop-one", "desktop-host", "vscode")
    assert store.path.read_bytes() == before


def test_target_cannot_move_or_cross_workspaces(control):
    store, _ = control
    token = attached(store)
    other = ControlStore(store.directory.parent.parent, THREAD, Path(store.repo_path) / "other",
                         boot_id="boot-one")
    with pytest.raises(ControlError, match="mismatch"):
        other.attach("desktop-one", "desktop-host", "vscode")
    altered = dict(token, repo_path=other.repo_path)
    with pytest.raises(ControlError, match="stale_epoch"):
        with store.guard(altered):
            pytest.fail("cross-workspace token")


def test_ssh_helper_uses_identical_dependency_free_core(control):
    import ast
    # Parse under the oldest supported helper syntax, not just the build Python.
    ast.parse(CONTROL_SOURCE, feature_version=6)
    namespace = {}
    exec(CONTROL_SOURCE, namespace)
    store, _ = control
    helper = namespace["ControlStore"](store.directory.parent.parent, THREAD, store.repo_path,
                                       clock=store.clock, boot_id=store.boot_id)
    token = attached(helper)
    store.detach(token)
    assert helper.claim_remote("remote-one", "remote-host", "absent")["epoch"] == 2


def test_completed_notification_receipt_recovers_interrupted_barrier_clear(control):
    store, now = control
    token = attached(store)
    prepared = store.prepare_notification(token, "event", "sha")
    receipt = dict(prepared, state="completed", result={"status": "sent"})
    control_atomic_json(store.effect_path("notification", "event"), receipt)
    now[0] += 21
    replacement = store.claim_remote("remote", "host", "absent")
    assert replacement["epoch"] == 2
    assert store.read()["external_effect"] is None
    assert store.prepare_notification(replacement, "event", "sha")["duplicate"]


def test_expired_remote_lease_requires_unchanged_epoch_and_live_exact_writer(control, monkeypatch):
    from codex_watchdog import control_state
    store, now = control
    token = detached(store)
    with store.guard(token) as value:
        value["writer_pid"] = 123
        control_atomic_json(store.path, value)
    now[0] += 31
    monkeypatch.setattr(control_state, "control_kernel_owner", lambda path: 123)
    store.renew(token)
    assert store.read()["epoch"] == token["epoch"]
    now[0] += 31
    monkeypatch.setattr(control_state, "control_kernel_owner", lambda path: None)
    with pytest.raises(ControlError, match="lease_expired"):
        store.renew(token)
    replacement = store.claim_remote("replacement", "host", "absent")
    monkeypatch.setattr(control_state, "control_kernel_owner", lambda path: 123)
    with pytest.raises(ControlError, match="stale_epoch"):
        store.renew(token)
    assert store.read()["epoch"] == replacement["epoch"]
