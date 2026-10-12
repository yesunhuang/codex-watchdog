"""One-shot claims reserve only an effect, never future independent work."""
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.notifications import NotificationEvent, NotificationResult
from codex_watchdog.remote_control import notify_local_control


THREAD = "11111111-2222-4333-8444-555555555555"
DIGEST = "a" * 64


def claim(home, key, digest=DIGEST):
    with cs.control_wake_attempt_lock(home, key):
        return cs.control_wake_attempt_claim(home, key, THREAD, digest)


def test_embedded_source_keeps_python36_and_same_claims(tmp_path):
    ast.parse(cs.CONTROL_SOURCE, feature_version=(3, 6))
    namespace = {}
    exec(compile(cs.CONTROL_SOURCE, "<fixture-helper>", "exec"), namespace)
    first = claim(tmp_path, "git:old")
    with namespace["control_wake_attempt_lock"](tmp_path, "git:old"):
        assert namespace["control_wake_attempt_claim"](tmp_path, "git:old", THREAD, DIGEST)["duplicate"]
    assert first["state"] == "claimed"


def test_claim_before_attempt_crash_does_not_retry(tmp_path):
    code = """
import os,sys
from pathlib import Path
from codex_watchdog.control_state import control_wake_attempt_lock,control_wake_attempt_claim
with control_wake_attempt_lock(Path(sys.argv[1]), 'git:crash'):
 control_wake_attempt_claim(Path(sys.argv[1]),'git:crash',sys.argv[2], 'a'*64)
 os._exit(19)
"""
    import os
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), THREAD], env=env,
                            capture_output=True, timeout=10)
    assert result.returncode == 19
    assert claim(tmp_path, "git:crash")["duplicate"]
    assert not claim(tmp_path, "git:next")["duplicate"]


def test_contenders_allow_at_most_one_attempt(tmp_path):
    barrier = threading.Barrier(8)
    def contend(_):
        barrier.wait()
        try:
            return not claim(tmp_path, "slackbot:one")["duplicate"]
        except cs.ControlBusy:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(contend, range(8))) == 1
    assert claim(tmp_path, "slackbot:one")["duplicate"]


def test_same_process_namespaces_do_not_release_the_outer_posix_lock(tmp_path):
    namespace = {}
    exec(compile(cs.CONTROL_SOURCE, "<fixture-helper>", "exec"), namespace)
    with cs.control_wake_attempt_lock(tmp_path, "git:locked"):
        with pytest.raises(cs.ControlBusy):
            with cs.control_wake_attempt_lock(tmp_path, "git:locked"):
                pytest.fail("recursive lock admitted")
        with pytest.raises(namespace["ControlBusy"]):
            with namespace["control_wake_attempt_lock"](tmp_path, "git:locked"):
                pytest.fail("second namespace admitted")
        with cs.control_wake_attempt_lock(tmp_path, "git:independent"):
            cs.control_wake_attempt_claim(tmp_path, "git:independent", THREAD, DIGEST)


def test_claim_requires_exact_held_lock_and_identity(tmp_path):
    with pytest.raises(cs.ControlError, match="lock_required"):
        cs.control_wake_attempt_claim(tmp_path, "git:one", THREAD, DIGEST)
    claim(tmp_path, "git:one")
    before = cs.control_wake_attempt_path(tmp_path, "git:one").read_bytes()
    with pytest.raises(cs.ControlError, match="collision"):
        claim(tmp_path, "git:one", "b" * 64)
    assert cs.control_wake_attempt_path(tmp_path, "git:one").read_bytes() == before
    assert not claim(tmp_path, "git:two")["duplicate"]


def test_corrupt_effect_drops_only_it(tmp_path):
    claim(tmp_path, "git:bad")
    path = cs.control_wake_attempt_path(tmp_path, "git:bad")
    path.write_bytes(b'{"schema_version":1,"schema_version":2}')
    with pytest.raises(cs.ControlError, match="record_invalid"):
        claim(tmp_path, "git:bad")
    assert not claim(tmp_path, "git:good")["duplicate"]


def test_ten_thousand_history_never_scanned(tmp_path, monkeypatch):
    root = tmp_path / "watchdog-wake-attempts"
    root.mkdir(mode=0o700)
    # Unrelated malformed old receipts cannot affect an indexed new-ID lookup.
    for i in range(10001):
        (root / (hashlib.sha256(str(i).encode()).hexdigest() + ".json")).write_bytes(b"old")
    monkeypatch.setattr(Path, "glob", lambda *args, **kwargs: pytest.fail("history scan"))
    monkeypatch.setattr(Path, "rglob", lambda *args, **kwargs: pytest.fail("history scan"))
    assert not claim(tmp_path, "git:current")["duplicate"]
    assert claim(tmp_path, "git:current")["duplicate"]


def test_historical_uncertain_does_not_block_owner_or_change_truth(tmp_path):
    clock = [100.0]
    store = cs.ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", boot_id="old",
                            clock=lambda: clock[0])
    token = store.attach("old", "host", "vscode")
    prepared = store.prepare_notification(token, "old-output", "payload")
    path = store.effect_path("notification", "old-output")
    original = path.read_bytes()
    clock[0] += 1000
    new = store.claim_remote("new", "host", "absent")
    assert new is not None and new["epoch"] > token["epoch"]
    assert path.read_bytes() == original
    assert store.prepare_notification(new, "old-output", "payload")["duplicate"]
    assert prepared["state"] == "uncertain"
    assert not store.prepare_notification(new, "fresh-output", "fresh-payload")["duplicate"]


def test_expired_token_not_kept_live_by_uncertain_audit(tmp_path):
    clock = [100.0]
    store = cs.ControlStore(tmp_path, THREAD, tmp_path / "repo", boot_id="boot", clock=lambda: clock[0])
    token = store.attach("old", "host", "vscode")
    store.prepare_notification(token, "old", "payload")
    clock[0] += 1000
    with pytest.raises(cs.ControlError, match="lease_expired"):
        with store.guard(token):
            pytest.fail("expired capability admitted")


@pytest.mark.parametrize("bad_pointer", [b"{", b"x" * 131073,
                                        b'{"schema_version":1,"nested":' + b'[' * 2000
                                        + b'0' + b']' * 2000 + b'}'],
                         ids=['malformed-json', 'oversized', 'deeply-nested'])
def test_corrupt_old_sender_pointer_does_not_gate_new_effect(tmp_path, bad_pointer):
    from types import SimpleNamespace
    from codex_watchdog.notification_attempts import local_attempt
    store = cs.ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", boot_id="boot")
    token = store.attach("sender", "host", "vscode")
    attempt = local_attempt(tmp_path / "runtime", store)
    attempt.path.parent.mkdir(parents=True)
    attempt.path.write_bytes(bad_pointer)
    event = NotificationEvent("fixture", "stopped", "new", "Fixture", "Fixture")
    calls = []
    def notify(event):
        calls.append(event.event_fingerprint())
        return NotificationResult("sent", "fixture", event.event_fingerprint(), False, (), (), tmp_path, True)
    notifier = SimpleNamespace(runtime=tmp_path / "runtime", notify=notify)
    assert notify_local_control(store, token, event, notifier)["status"] == "sent"
    assert notify_local_control(store, token, event, notifier)["duplicate"]
    assert len(calls) == 1 and attempt.path.read_bytes() == bad_pointer


def test_legacy_local_held_sender_lease_blocks_takeover_after_owner_expiry(tmp_path):
    from codex_watchdog.notification_attempts import local_attempt
    clock = [100.0]
    store = cs.ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", boot_id="boot",
                            clock=lambda: clock[0])
    token = store.attach("legacy", "host", "vscode", ttl=20)
    attempt = local_attempt(store.directory / "runtime", store)
    with attempt.lease():
        intent = attempt.start(token, DIGEST)
        store.prepare_notification(token, DIGEST, DIGEST, intent["operation_id"], intent["sender"])
        clock[0] += 21
        before = store.path.read_bytes()
        with pytest.raises(cs.ControlBusy):
            store.claim_remote("replacement", "host", "vscode", host_observer=True)
        assert store.path.read_bytes() == before
    old = store.effect_path("notification", DIGEST).read_bytes()
    assert store.claim_remote("replacement", "host", "vscode", host_observer=True)
    assert store.effect_path("notification", DIGEST).read_bytes() == old


def test_unobservable_legacy_sender_fences_live_owner_but_not_its_new_work(tmp_path):
    clock = [100.0]
    store = cs.ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", boot_id="boot",
                            clock=lambda: clock[0])
    token = store.attach("desktop", "foreign", "vscode", ttl=20)
    old = store.prepare_notification(token, "old", DIGEST, sender="b" * 64)
    original = store.effect_path("notification", "old").read_bytes()
    assert store.claim_remote("native", "host", "vscode", host_observer=True) is None
    assert store.read()["owner"]["sender_lease_unobservable"] is True
    assert not store.prepare_notification(token, "new", "new-payload")["duplicate"]
    assert store.claim_remote("native", "host", "vscode", host_observer=True) is None
    assert store.effect_path("notification", "old").read_bytes() == original
    clock[0] += 21
    replacement = store.claim_remote("native", "host", "vscode", host_observer=True)
    assert replacement and "sender_lease_unobservable" not in store.read()["owner"]
    # Actual late settlement addresses only its original operation.
    store.prepare_notification(replacement, "current", "current-payload")
    barrier = store.read()["external_effect"]
    store.finish_notification(token, "old", old["operation_id"], {"status": "uncertain"})
    assert store.read()["external_effect"] == barrier


def test_completed_legacy_pointer_does_not_invent_live_owner_fence(tmp_path):
    store = cs.ControlStore(tmp_path / "codex", THREAD, tmp_path / "repo", boot_id="boot")
    token = store.attach("desktop", "foreign", "vscode")
    old = store.prepare_notification(token, "old", DIGEST, sender="b" * 64)
    barrier = store.read()["external_effect"]
    store.finish_notification(token, "old", old["operation_id"], {"status": "uncertain"})
    value = store.read()
    value["external_effect"] = barrier  # Legacy stale pointer, exact completed receipt.
    cs.control_atomic_json(store.path, value)
    assert store.claim_remote("native", "host", "vscode", host_observer=True)
    assert "sender_lease_unobservable" not in store.read()["owner"]


def test_malformed_legacy_claim_cannot_block_distinct_handoff_or_dispatch(tmp_path):
    store = cs.ControlStore(tmp_path, THREAD, tmp_path / "repo", boot_id="boot")
    token = store.attach("desktop", "host", "vscode")
    value = store.read()
    value["remote_state"] = {"pending_instruction_id": "git:bad"}
    cs.control_atomic_json(store.path, value)
    claim(tmp_path, "git:bad")
    path = cs.control_wake_attempt_path(tmp_path, "git:bad")
    path.write_bytes(b"{")
    assert cs.control_adopt_legacy_wake(store, None)[0]["state"] == "ineligible"
    assert path.read_bytes() == b"{"
    assert not claim(tmp_path, "git:fresh")["duplicate"]
    with pytest.raises(cs.ControlError):
        claim(tmp_path, "git:bad")


@pytest.mark.parametrize("origin", ["dispatch", "legacy"])
def test_missing_prompt_field_is_per_id_invalid_without_blocking_legacy_adoption(tmp_path, origin):
    store = cs.ControlStore(tmp_path, THREAD, tmp_path / "repo", boot_id="boot")
    token = store.attach("desktop", "host", "vscode")
    with store.guard(token) as value:
        value["remote_state"] = {"pending_instruction_id": "git:missing-prompt"}
        cs.control_atomic_json(store.path, value)
    with cs.control_wake_attempt_lock(tmp_path, "git:missing-prompt"):
        record = cs.control_wake_attempt_claim(tmp_path, "git:missing-prompt", THREAD,
            None if origin == "legacy" else DIGEST, origin=origin)
    record.pop("duplicate")
    record.pop("prompt_sha256")
    path = cs.control_wake_attempt_path(tmp_path, "git:missing-prompt")
    path.write_text(json.dumps(record))
    saved, owner = path.read_bytes(), store.path.read_bytes()
    namespace = {}
    exec(compile(cs.CONTROL_SOURCE, "<fixture-helper>", "exec"), namespace)
    for read, error in ((cs.control_wake_attempt_read, cs.ControlError),
                        (namespace["control_wake_attempt_read"], namespace["ControlError"])):
        with pytest.raises(error, match="record_invalid"):
            read(tmp_path, "git:missing-prompt")
    with store.guard(token):
        adopted = cs.control_adopt_legacy_wake(store, None)
    assert adopted == [dict(state="ineligible", instruction_id="git:missing-prompt",
                            reason="wake_attempt_record_invalid")]
    assert not claim(tmp_path, "git:independent")["duplicate"]
    with pytest.raises(cs.ControlError, match="record_invalid"):
        claim(tmp_path, "git:missing-prompt")
    assert path.read_bytes() == saved and store.path.read_bytes() == owner
