"""Exact uncertain Git retirement preserves delivery evidence and native work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.linux_auto import HostRemoteAdapter
from codex_watchdog.node_observation import NodeObservation, observation_lock
from test_linux_node import THREAD, make_store, nodes
from test_node_observation import foreign_absence


INSTRUCTION = "git:" + "a" * 16 + ":" + "b" * 40
PROMPT = "Inspect the saved Git change without replaying an uncertain delivery."
PROMPT_SHA = hashlib.sha256(PROMPT.encode()).hexdigest()
AUTHORIZATION_SHA = "e" * 64
OTHER_THREAD = "22222222-2222-4222-8222-222222222222"


def make_symlink(link, target, **kwargs):
    try:
        link.symlink_to(target, **kwargs)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink privilege")
        raise


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and not p.name.endswith(".lock")}


def assert_handoff_preserves_evidence(rig, before):
    after = snapshot(rig.home)
    observer = str(rig.original.path.relative_to(rig.home))
    assert all(after[key] == raw for key, raw in before.items() if key != observer)
    assert set(after) - set(before) <= {
        str(cs.control_wake_attempt_path(rig.home, instruction).relative_to(rig.home))
        for instruction in (INSTRUCTION, (rig.old_store.read().get("remote_state") or {}).get("pending_instruction_id"))
        if instruction
    }


def stamp(home):
    path = home / "sessions/thread.jsonl"
    info = path.stat()
    return [str(path), info.st_ino, info.st_size, info.st_mtime_ns]


def admit(observation):
    with observation.guard(rollout_stamp=stamp(observation.store.codex_home)) as allowed:
        return allowed


def mutate(path, **fields):
    value = json.loads(path.read_bytes())
    value.update(fields)
    path.write_text(json.dumps(value, sort_keys=True))


@pytest.fixture
def retirement(nodes, tmp_path, monkeypatch):
    home, repo, host, roots, pid = nodes
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(home))
    old_store = make_store(home, repo, boot="boot-original")
    old_store.enroll_node()
    old_token = old_store.claim_remote("original-dog", "original-locality", "vscode", host_observer=True)
    original = NodeObservation(old_store, old_token)
    assert admit(original)
    with old_store.guard(old_token) as value:
        value["future_owner_field"] = {"retain": [1, "unchanged"]}
        value["remote_state"] = dict(session_id=THREAD, repo_path=str(repo),
            pending_instruction_id=INSTRUCTION, pending_remote_oid="b" * 40,
            last_remote_oid="c" * 40, future_state_field={"preserve": True})
        cs.control_atomic_json(old_store.path, value)
    pid[0] = None
    assert admit(original)
    mutate(original.path, future_observer_field={"retain": ["original"]})
    wake_path = roots["login-a.example"] / "remote-wake" / (
        hashlib.sha256(INSTRUCTION.encode()).hexdigest() + ".json")
    wake_path.parent.mkdir()
    wake_path.write_text(json.dumps(dict(instruction_id=INSTRUCTION, thread_id=THREAD,
        prompt_sha256=PROMPT_SHA, state="uncertain", returncode=1,
        rollout_path=str(home / "sessions/thread.jsonl"), rollout_baseline_offset=0,
        queue_baseline_revision=0, stdout_sha256="1" * 64, stderr_sha256="2" * 64,
        future_wake_field={"preserve": [1, 2]}), sort_keys=True))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT,payload_json TEXT)")
        db.execute("CREATE INDEX fixture_pending_thread ON queued_items(thread_id)")
        db.execute("CREATE TABLE queued_thread_revisions(thread_id TEXT PRIMARY KEY,revision INTEGER)")
        db.execute("INSERT INTO queued_thread_revisions VALUES (?,0)", (THREAD,))
    preserved = home / "protected-provider-state"
    preserved.mkdir()
    for name in ("grants.json", "tickets.json", "notification.json", "bindings.json"):
        (preserved / name).write_text(json.dumps({"schema_version": 1, "future": name}))
    host[0], pid[0] = "login-b.example", 123
    current_store = make_store(home, repo, boot="boot-current")
    current_store.enroll_node()
    current_token = current_store.claim_remote("current-dog", "current-locality", "vscode", host_observer=True)
    current = NodeObservation(current_store, current_token)
    current.foreign_writer_check = lambda expected: foreign_absence(expected, home)
    adapter = HostRemoteAdapter(home)
    adapter.namespace["control_kernel_owner"] = lambda path: pid[0]
    adapter.namespace["control_vscode_writer"] = lambda value: value == 123
    adapter.namespace["ControlStore"] = lambda h, t, r: make_store(h, r, boot="boot-current")
    monkeypatch.setattr(adapter.namespace["subprocess"], "run",
        lambda *a, **k: pytest.fail("retirement must not run a courier, provider or native command"))
    args = dict(instruction_id=INSTRUCTION, prompt_sha256=PROMPT_SHA,
        original_node="login-a.example", original_owner_epoch=old_token["epoch"],
        original_owner_sha256=digest(old_store.path), original_wake_sha256=digest(wake_path),
        original_observer_sha256=digest(original.path), authorization_sha256=AUTHORIZATION_SHA)
    marker = home / "watchdog-git-retirements" / (hashlib.sha256(INSTRUCTION.encode()).hexdigest() + ".json")
    return SimpleNamespace(home=home, repo=repo, host=host, roots=roots, pid=pid,
        original=original, old_store=old_store, old_token=old_token, wake_path=wake_path,
        current=current, current_store=current_store, current_token=current_token,
        adapter=adapter, args=args, marker=marker, protected=preserved)


def retire(rig, **changes):
    from codex_watchdog.git_wake_retirement import retire_git_wake
    return retire_git_wake(rig.current_store, **dict(rig.args, **changes))


def test_original_uncertain_git_is_reserved_without_blocking_native_observer(retirement):
    rig = retirement
    before = snapshot(rig.home)
    assert admit(rig.current)
    assert_handoff_preserves_evidence(rig, before)
    assert json.loads(rig.wake_path.read_bytes())["state"] == "uncertain"
    assert not rig.marker.exists()


def test_empty_queue_and_external_writer_do_not_settle_original_uncertainty(retirement):
    rig = retirement
    assert rig.pid[0] == 123
    with sqlite3.connect(rig.home / "queue_1.sqlite") as db:
        assert db.execute("SELECT count(*) FROM queued_items WHERE thread_id=?", (THREAD,)).fetchone() == (0,)
    original = rig.wake_path.read_bytes()
    assert admit(rig.current)
    assert rig.wake_path.read_bytes() == original
    assert cs.control_wake_attempt_read(rig.home, INSTRUCTION)["state"] == "claimed"


def test_retirement_is_additive_and_only_normal_observation_hands_back(retirement):
    rig = retirement
    before = snapshot(rig.home)
    result = retire(rig)
    after = snapshot(rig.home)
    assert set(after) - set(before) == {str(rig.marker.relative_to(rig.home))}
    assert all(after[key] == raw for key, raw in before.items())
    saved = json.loads(rig.marker.read_bytes())
    assert saved["schema_version"] == 1 and saved["kind"] == "git_wake_retirement"
    assert saved["state"] == "retired"
    assert saved["thread_id"] == THREAD and saved["repo_path"] == str(rig.repo)
    assert all(saved[key] == value for key, value in rig.args.items())
    assert result["duplicate"] is False
    assert rig.pid[0] == 123
    assert cs.control_read_json(rig.original.path)["wake_pending"] is True
    assert json.loads(rig.wake_path.read_bytes())["state"] == "uncertain"
    assert admit(rig.current)
    assert cs.control_read_json(rig.current.path)["node"] == "login-b.example"
    assert rig.old_store.path.read_bytes() == before[str(rig.old_store.path.relative_to(rig.home))]
    assert rig.wake_path.read_bytes() == before[str(rig.wake_path.relative_to(rig.home))]
    assert rig.pid[0] == 123


def test_retirement_repeat_is_immutable_and_has_no_second_effect(retirement):
    rig = retirement
    retire(rig)
    before = snapshot(rig.home)
    result = retire(rig)
    assert result["duplicate"] is True
    assert snapshot(rig.home) == before


@pytest.mark.parametrize("field,value", [
    ("instruction_id", "git:" + "f" * 16 + ":" + "b" * 40),
    ("prompt_sha256", "f" * 64),
    ("original_node", "login-b.example"),
    ("original_owner_epoch", 99),
    ("original_owner_sha256", "f" * 64),
    ("original_wake_sha256", "f" * 64),
    ("original_observer_sha256", "f" * 64),
])
def test_exact_retirement_input_mismatch_is_read_only(retirement, field, value):
    before = snapshot(retirement.home)
    with pytest.raises((cs.ControlError, ValueError)):
        retire(retirement, **{field: value})
    assert snapshot(retirement.home) == before


@pytest.mark.parametrize("instruction", [
    "git-attention:" + "a" * 16 + ":" + "b" * 64,
    "slackbot:" + "a" * 40, "slack:" + "a" * 40, "lark:" + "a" * 40,
    "linux-continue:" + THREAD + ":" + OTHER_THREAD,
    "git:" + "a" * 16 + ":" + "B" * 40,
    "git:" + "a" * 15 + ":" + "b" * 40,
])
def test_non_git_and_noncanonical_triggers_are_ineligible(retirement, instruction):
    before = snapshot(retirement.home)
    with pytest.raises((cs.ControlError, ValueError)):
        retire(retirement, instruction_id=instruction)
    assert snapshot(retirement.home) == before


@pytest.mark.parametrize("field,value", [
    ("original_node", "../login-a.example"),
    ("original_node", "/login-a.example"),
    ("original_owner_epoch", True), ("original_owner_epoch", -1),
    ("authorization_sha256", ""), ("authorization_sha256", "not-a-digest"),
    ("prompt_sha256", None),
])
def test_invalid_retirement_identity_and_authorization_shape_is_read_only(retirement, field, value):
    before = snapshot(retirement.home)
    with pytest.raises((cs.ControlError, ValueError, TypeError)):
        retire(retirement, **{field: value})
    assert snapshot(retirement.home) == before


@pytest.mark.parametrize("location,field,value", [
    ("wake", "thread_id", OTHER_THREAD), ("wake", "instruction_id", "git:" + "c" * 16 + ":" + "b" * 40),
    ("wake", "prompt_sha256", "f" * 64), ("wake", "state", "started"),
    ("wake", "queue_message_id", OTHER_THREAD),
    ("owner", "thread_id", OTHER_THREAD), ("owner", "repo_path", "/fixture/other-repo"),
    ("observer", "thread_id", OTHER_THREAD), ("observer", "repo_path", "/fixture/other-repo"),
    ("observer", "node", "login-b.example"), ("observer", "writer_live", True),
    ("observer", "wake_pending", False),
])
def test_even_repinned_conflicting_original_evidence_is_ineligible(retirement, location, field, value):
    rig = retirement
    paths = {"wake": rig.wake_path, "owner": rig.old_store.path, "observer": rig.original.path}
    mutate(paths[location], **{field: value})
    hash_field = {"wake": "original_wake_sha256", "owner": "original_owner_sha256",
                  "observer": "original_observer_sha256"}[location]
    rig.args[hash_field] = digest(paths[location])
    before = snapshot(rig.home)
    with pytest.raises((cs.ControlError, ValueError)):
        retire(rig)
    assert snapshot(rig.home) == before


@pytest.mark.parametrize("queued_thread", [THREAD, OTHER_THREAD])
def test_native_queued_work_is_preserved_and_exact_session_work_blocks(retirement, queued_thread):
    rig = retirement
    with sqlite3.connect(rig.home / "queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES (?,?,?)", (OTHER_THREAD, queued_thread, '{"keep":true}'))
    before = snapshot(rig.home)
    if queued_thread == THREAD:
        with pytest.raises((cs.ControlError, ValueError)):
            retire(rig)
        assert snapshot(rig.home) == before
    else:
        retire(rig)
        after = snapshot(rig.home)
        assert all(after[key] == raw for key, raw in before.items())


def test_original_node_other_pending_id_is_reserved_after_retirement(retirement):
    rig = retirement
    retire(rig)
    value = rig.old_store.read()
    value["remote_state"]["pending_instruction_id"] = "git:" + "a" * 16 + ":" + "c" * 40
    cs.control_atomic_json(rig.old_store.path, value)
    before = snapshot(rig.home)
    assert admit(rig.current)
    assert_handoff_preserves_evidence(rig, before)


def test_shared_observation_lock_blocks_a_concurrent_retirement(retirement):
    rig = retirement
    before = snapshot(rig.home)
    with observation_lock(rig.original.lock_path):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(retire, rig)
            with pytest.raises(cs.ControlBusy):
                future.result(timeout=5)
    assert snapshot(rig.home) == before


def test_original_owner_lock_blocks_retirement_without_a_marker(retirement):
    rig = retirement
    before = snapshot(rig.home)
    with cs.control_file_lock(rig.old_store.lock_path):
        with pytest.raises(cs.ControlBusy):
            retire(rig)
    assert snapshot(rig.home) == before


def test_old_uncertain_receipt_is_not_rewritten_by_shared_replay_refusal(retirement):
    rig = retirement
    retire(rig)
    saved = snapshot(rig.home)
    rig.adapter.namespace["_CONTROL_ACTIVE"] = (rig.current_store, rig.current_token)
    response = rig.adapter.namespace["dispatch_wake"](
        {"instruction_id": INSTRUCTION, "prompt": PROMPT}, THREAD)
    assert response["state"] not in ("dispatching", "enqueued", "started", "consumed_or_started")
    assert snapshot(rig.home) == saved
    assert json.loads(rig.wake_path.read_bytes())["state"] == "uncertain"


def test_retired_id_refusal_survives_old_owner_replacement(retirement):
    rig = retirement
    retire(rig)
    value = rig.old_store.read()
    value["epoch"] += 1
    value["remote_state"]["pending_instruction_id"] = None
    cs.control_atomic_json(rig.old_store.path, value)
    saved = snapshot(rig.home)
    rig.adapter.namespace["_CONTROL_ACTIVE"] = (rig.current_store, rig.current_token)
    response = rig.adapter.namespace["dispatch_wake"](
        {"instruction_id": INSTRUCTION, "prompt": PROMPT}, THREAD)
    assert response["state"] not in ("dispatching", "enqueued", "started", "consumed_or_started")
    assert snapshot(rig.home) == saved


@pytest.mark.parametrize("scope", ["thread", "repo", "prompt"])
def test_shared_reader_rejects_tombstone_scope_collisions(retirement, scope):
    rig = retirement
    retire(rig)
    arguments = dict(thread_id=THREAD, repo_path=str(rig.repo), prompt_sha256=PROMPT_SHA)
    arguments[{"thread": "thread_id", "repo": "repo_path", "prompt": "prompt_sha256"}[scope]] = {
        "thread": OTHER_THREAD, "repo": "/fixture/other-repo", "prompt": "f" * 64}[scope]
    with pytest.raises(cs.ControlError):
        cs.control_git_retirement_read(rig.home, INSTRUCTION, **arguments)


@pytest.mark.parametrize("problem", ["owner_instance", "owner_missing", "owner_role", "owner_epoch", "observer_boot", "observer_epoch"])
def test_repinned_original_owner_and_selected_relay_owner_must_agree(retirement, problem):
    rig = retirement
    owner = rig.old_store.read()
    observer = cs.control_read_json(rig.original.path)
    if problem == "owner_instance":
        owner["owner"]["instance"] = "replacement-original-dog"
    elif problem == "owner_missing":
        owner["owner"] = None
    elif problem == "owner_role":
        owner["owner"]["role"] = "local"
    elif problem == "owner_epoch":
        owner["epoch"] += 1
        rig.args["original_owner_epoch"] = owner["epoch"]
    elif problem == "observer_boot":
        observer["boot_id"] = "different-boot"
    else:
        observer["relay_owner"]["local_epoch"] += 1
    cs.control_atomic_json(rig.old_store.path, owner)
    cs.control_atomic_json(rig.original.path, observer)
    rig.args["original_owner_sha256"] = digest(rig.old_store.path)
    rig.args["original_observer_sha256"] = digest(rig.original.path)
    before = snapshot(rig.home)
    with pytest.raises(cs.ControlError):
        retire(rig)
    assert snapshot(rig.home) == before


@pytest.mark.parametrize("problem", ["missing", "malformed", "extra_database", "symlink"])
def test_ambiguous_or_unavailable_native_queue_cannot_authorize_retirement(retirement, problem):
    rig = retirement
    queue = rig.home / "queue_1.sqlite"
    if problem == "missing":
        queue.unlink()
    elif problem == "malformed":
        queue.write_bytes(b"invalid native queue")
    elif problem == "extra_database":
        (rig.home / "queue_2.sqlite").write_bytes(queue.read_bytes())
    else:
        saved = rig.home / "queue-saved.sqlite"
        queue.rename(saved)
        make_symlink(queue, saved)
    before = snapshot(rig.home)
    with pytest.raises(cs.ControlError):
        retire(rig)
    assert snapshot(rig.home) == before


@pytest.mark.parametrize("problem", ["no_native_writer", "unverified_writer", "repo_changed", "external_effect"])
def test_current_native_target_must_be_proved_without_replacing_it(retirement, monkeypatch, problem):
    rig = retirement
    if problem == "no_native_writer":
        rig.pid[0] = None
    elif problem == "unverified_writer":
        rig.pid[0] = 999
    elif problem == "repo_changed":
        with sqlite3.connect(rig.home / "state_5.sqlite") as db:
            db.execute("UPDATE threads SET cwd=? WHERE id=?", (str(rig.repo.parent / "other"), THREAD))
    else:
        value = rig.current_store.read()
        value["external_effect"] = dict(id="effect-in-progress", epoch=value["epoch"],
            instance=value["owner"]["instance"], started=100)
        cs.control_atomic_json(rig.current_store.path, value)
    before = snapshot(rig.home)
    with pytest.raises(cs.ControlError):
        retire(rig)
    assert snapshot(rig.home) == before


def test_current_and_original_effect_barriers_remain_independent(retirement):
    rig = retirement
    value = rig.old_store.read()
    value["external_effect"] = dict(id="original-effect", epoch=value["epoch"],
        instance=value["owner"]["instance"], started=100)
    cs.control_atomic_json(rig.old_store.path, value)
    rig.args["original_owner_sha256"] = digest(rig.old_store.path)
    before = snapshot(rig.home)
    with pytest.raises(cs.ControlError):
        retire(rig)
    assert snapshot(rig.home) == before


@pytest.mark.parametrize("location", ["wake", "owner", "observer", "retirement_root"])
def test_symlinked_original_or_reservation_path_is_refused(retirement, location):
    rig = retirement
    if location == "retirement_root":
        outside = rig.home / "other-retirements"
        outside.mkdir(mode=0o700)
        make_symlink(rig.marker.parent, outside, target_is_directory=True)
    else:
        path = {"wake": rig.wake_path, "owner": rig.old_store.path, "observer": rig.original.path}[location]
        saved = path.with_suffix(".saved")
        path.rename(saved)
        make_symlink(path, saved)
    before = snapshot(rig.home)
    with pytest.raises(cs.ControlError):
        retire(rig)
    assert snapshot(rig.home) == before


@pytest.mark.parametrize("problem", ["boolean_schema", "state", "duplicate_key", "nan", "missing_field", "empty", "symlink", "extra_link"])
def test_malformed_existing_reservation_is_never_overwritten_or_dispatched(retirement, problem):
    rig = retirement
    retire(rig)
    if problem == "boolean_schema":
        mutate(rig.marker, schema_version=True)
    elif problem == "state":
        mutate(rig.marker, state="completed")
    elif problem == "duplicate_key":
        raw = rig.marker.read_text().rstrip()
        rig.marker.write_text(raw[:-1] + ',"state":"retired"}')
    elif problem == "nan":
        raw = rig.marker.read_text().rstrip()
        rig.marker.write_text(raw[:-1] + ',"future":NaN}')
    elif problem == "missing_field":
        value = json.loads(rig.marker.read_bytes())
        del value["authorization_sha256"]
        rig.marker.write_text(json.dumps(value))
    elif problem == "empty":
        rig.marker.write_bytes(b"")
    elif problem == "symlink":
        saved = rig.marker.with_suffix(".saved")
        rig.marker.rename(saved)
        make_symlink(rig.marker, saved)
    else:
        os.link(rig.marker, rig.marker.with_suffix(".unremoved-temporary"))
    before = snapshot(rig.home)
    with pytest.raises(cs.ControlError):
        retire(rig)
    with pytest.raises(cs.ControlError):
        cs.control_git_retirement_read(rig.home, INSTRUCTION)
    rig.adapter.namespace["_CONTROL_ACTIVE"] = (rig.current_store, rig.current_token)
    result = rig.adapter.namespace["dispatch_wake"]({"instruction_id": INSTRUCTION, "prompt": PROMPT}, THREAD)
    assert result["state"] == "rejected"
    assert snapshot(rig.home) == before


def test_unknown_reservation_fields_survive_idempotent_read_and_repeat(retirement):
    rig = retirement
    retire(rig)
    mutate(rig.marker, future_retirement_field={"preserve": ["unknown", 1]})
    before = snapshot(rig.home)
    assert retire(rig)["future_retirement_field"] == {"preserve": ["unknown", 1]}
    assert cs.control_git_retirement_read(rig.home, INSTRUCTION)["future_retirement_field"] == {"preserve": ["unknown", 1]}
    assert snapshot(rig.home) == before


class PublicationCrash(BaseException):
    pass


def test_crash_before_publication_leaves_original_barrier_and_all_evidence(retirement, monkeypatch):
    from codex_watchdog import git_wake_retirement as module
    rig = retirement
    before = snapshot(rig.home)
    def crash(*args, **kwargs):
        raise PublicationCrash("before reservation link")
    monkeypatch.setattr(module.os, "link", crash)
    with pytest.raises(PublicationCrash):
        retire(rig)
    assert not rig.marker.exists()
    assert snapshot(rig.home) == before
    assert admit(rig.current)


def test_failure_after_durable_publication_keeps_one_permanent_reservation(retirement, monkeypatch):
    from codex_watchdog import git_wake_retirement as module
    rig = retirement
    original_sync = module._sync_directory
    calls = []
    def crash(path):
        calls.append(path)
        original_sync(path)
        if path == rig.marker.parent and rig.marker.exists() and calls.count(path) == 1:
            raise PublicationCrash("after durable reservation link")
    before = snapshot(rig.home)
    monkeypatch.setattr(module, "_sync_directory", crash)
    with pytest.raises(PublicationCrash):
        retire(rig)
    assert rig.marker.is_file()
    assert rig.marker.stat().st_nlink == 1
    after = snapshot(rig.home)
    assert all(after[key] == raw for key, raw in before.items())
    monkeypatch.setattr(module, "_sync_directory", original_sync)
    assert retire(rig)["duplicate"] is True
    assert snapshot(rig.home) == after


@pytest.mark.parametrize("new_work", ["queue", "pending"])
def test_new_work_before_publication_preserves_position_and_prevents_retirement(retirement, monkeypatch, new_work):
    from codex_watchdog import git_wake_retirement as module
    rig = retirement
    publish = module._publish
    def change_then_publish(path, value, validate):
        if new_work == "queue":
            with sqlite3.connect(rig.home / "queue_1.sqlite") as db:
                db.execute("INSERT INTO queued_items VALUES (?,?,?)", (OTHER_THREAD, THREAD, '{"new_work":true}'))
        else:
            owner = rig.old_store.read()
            owner["remote_state"]["pending_instruction_id"] = "slack:pending-other-work"
            cs.control_atomic_json(rig.old_store.path, owner)
        publish(path, value, validate)
    before = {p: raw for p, raw in snapshot(rig.home).items() if p in (
        str(rig.wake_path.relative_to(rig.home)), str(rig.original.path.relative_to(rig.home)))}
    monkeypatch.setattr(module, "_publish", change_then_publish)
    with pytest.raises(cs.ControlError):
        retire(rig)
    assert not rig.marker.exists()
    assert all((rig.home / p).read_bytes() == raw for p, raw in before.items())
    if new_work == "queue":
        rig.current.relay_handoff_safe = lambda: False
        assert not admit(rig.current)
    else:
        assert admit(rig.current)
        assert cs.control_wake_attempt_read(rig.home, "slack:pending-other-work")["state"] == "claimed"


def test_postpublication_new_work_is_not_overwritten_or_granted_handback(retirement, monkeypatch):
    from codex_watchdog import git_wake_retirement as module
    rig = retirement
    link = module.os.link
    def publish_then_queue(source, destination, *args, **kwargs):
        link(source, destination, *args, **kwargs)
        with sqlite3.connect(rig.home / "queue_1.sqlite") as db:
            db.execute("INSERT INTO queued_items VALUES (?,?,?)", (OTHER_THREAD, THREAD, '{"keep":true}'))
    monkeypatch.setattr(module.os, "link", publish_then_queue)
    with pytest.raises(cs.ControlError):
        retire(rig)
    assert rig.marker.exists()
    saved = snapshot(rig.home)
    rig.current.relay_handoff_safe = lambda: False
    assert not admit(rig.current)
    assert snapshot(rig.home) == saved
    with sqlite3.connect(rig.home / "queue_1.sqlite") as db:
        assert db.execute("SELECT payload_json FROM queued_items WHERE id=?", (OTHER_THREAD,)).fetchone() == ('{"keep":true}',)


def test_embedded_and_native_id_lock_exclude_each_other_in_same_process(retirement):
    rig = retirement
    with cs.control_git_retirement_lock(rig.home, INSTRUCTION):
        with pytest.raises(rig.adapter.namespace["ControlBusy"]):
            with rig.adapter.namespace["control_git_retirement_lock"](rig.home, INSTRUCTION):
                pytest.fail("separate embedded namespace must not inherit held native authority")
    with rig.adapter.namespace["control_git_retirement_lock"](rig.home, INSTRUCTION):
        with pytest.raises(cs.ControlBusy):
            with cs.control_git_retirement_lock(rig.home, INSTRUCTION):
                pytest.fail("native namespace must not inherit held embedded authority")


def test_local_queue_dispatcher_never_creates_a_replay_record(retirement):
    from codex_watchdog.control_context import acting_as
    from codex_watchdog.queue_wake import QueueWakeDispatcher
    rig = retirement
    retire(rig)
    dispatcher = QueueWakeDispatcher(Path(rig.current_store.read()["runtime_path"]),
        codex_executable="fixture-codex", codex_home=rig.home,
        runner=lambda *a, **k: pytest.fail("retired Git ID must never reach a native courier"))
    before = snapshot(rig.home)
    with acting_as(rig.current_store, rig.current_token):
        result = dispatcher.dispatch(THREAD, INSTRUCTION, PROMPT, "remote_git")
    assert result.status == "rejected" and "retir" in result.stderr
    assert snapshot(rig.home) == before


def test_native_relay_pending_barrier_is_not_discounted_by_git_retirement(retirement):
    rig = retirement
    retire(rig)
    rig.current.relay_handoff_safe = lambda: False
    before = snapshot(rig.home)
    assert not admit(rig.current)
    assert rig.current.reason == "control_node_relay_pending"
    assert snapshot(rig.home) == before


def test_saved_original_writer_flag_is_not_fresh_native_proof(retirement):
    rig = retirement
    retire(rig)
    mutate(rig.original.path, writer_live=True)
    rig.current.foreign_writer_check = None
    before = snapshot(rig.home)
    assert not admit(rig.current)
    assert rig.current.reason == "control_node_foreign_writer_unverified"
    assert snapshot(rig.home) == before
    rig.current.foreign_writer_check = lambda expected: dict(foreign_absence(expected, rig.home), boot_id="new-native-boot")
    assert admit(rig.current)
    assert_handoff_preserves_evidence(rig, before)


def test_same_id_nested_lock_refuses_and_retains_outer_exclusion(retirement):
    rig = retirement
    with cs.control_git_retirement_lock(rig.home, INSTRUCTION):
        with pytest.raises(cs.ControlBusy):
            with cs.control_git_retirement_lock(rig.home, INSTRUCTION):
                pytest.fail("retirement admission may not be nested")
        with ThreadPoolExecutor(max_workers=1) as pool:
            def enter_from_thread():
                with cs.control_git_retirement_lock(rig.home, INSTRUCTION):
                    return True
            with pytest.raises(cs.ControlBusy):
                pool.submit(enter_from_thread).result(timeout=5)
    with cs.control_git_retirement_lock(rig.home, INSTRUCTION):
        pass


def test_retirement_creation_is_linux_only_without_affecting_existing_state(retirement, monkeypatch):
    rig = retirement
    before = snapshot(rig.home)
    monkeypatch.setattr(cs.sys, "platform", "darwin")
    with pytest.raises(cs.ControlError):
        retire(rig)
    assert snapshot(rig.home) == before


def test_absent_retirement_preserves_windows_dispatch_and_existing_directory_modes(retirement):
    rig = retirement
    namespace = rig.adapter.namespace
    class WindowsOs:
        name = "nt"
        def __getattr__(self, key):
            return getattr(os, key)
    lock_entries = []
    @contextmanager
    def windows_file_lock(path):
        lock_entries.append(path)
        yield
    namespace["os"] = WindowsOs()
    namespace["sys"] = SimpleNamespace(platform="win32")
    namespace["control_file_lock"] = windows_file_lock
    namespace["codex_executable"] = lambda: "fixture-codex.exe"
    calls = []
    def courier(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0,
            stdout="Queued message " + OTHER_THREAD + " for thread " + THREAD + ".", stderr="")
    class FixtureSubprocess:
        run = staticmethod(courier)
        def __getattr__(self, key):
            return getattr(subprocess, key)
    namespace["subprocess"] = FixtureSubprocess()
    rig.marker.parent.mkdir(mode=0o755)
    rig.marker.parent.chmod(0o755)
    before_mode = rig.marker.parent.stat().st_mode
    assert namespace["control_git_retirement_read"](rig.home, INSTRUCTION) is None
    response = namespace["dispatch_wake"]({"instruction_id": INSTRUCTION + "-fresh", "prompt": PROMPT}, THREAD)
    assert response["state"] == "enqueued" and len(calls) == 1
    assert lock_entries and rig.marker.parent.stat().st_mode == before_mode
    assert not rig.marker.exists()


def test_old_node_revisit_never_changes_uncertain_delivery_into_completion(retirement):
    rig = retirement
    retire(rig)
    rig.host[0], rig.pid[0] = "login-a.example", None
    saved_owner = rig.old_store.path.read_bytes()
    saved_wake = rig.wake_path.read_bytes()
    assert admit(rig.original)
    assert rig.old_store.path.read_bytes() == saved_owner
    assert rig.wake_path.read_bytes() == saved_wake
    assert json.loads(saved_wake)["state"] == "uncertain"
    rig.host[0], rig.pid[0] = "login-b.example", 123
    assert admit(rig.current)
    assert rig.pid[0] == 123


def test_shared_dispatch_mismatch_refuses_without_new_local_wake_record(retirement):
    rig = retirement
    retire(rig)
    rig.adapter.namespace["_CONTROL_ACTIVE"] = (rig.current_store, rig.current_token)
    before = snapshot(rig.home)
    result = rig.adapter.namespace["dispatch_wake"]({"instruction_id": INSTRUCTION, "prompt": "different prompt"}, THREAD)
    assert result["state"] == "rejected"
    assert snapshot(rig.home) == before


def test_repeat_cannot_change_the_original_authorization_or_hashes(retirement):
    rig = retirement
    retire(rig)
    before = snapshot(rig.home)
    with pytest.raises(cs.ControlError):
        retire(rig, authorization_sha256="f" * 64)
    assert snapshot(rig.home) == before


@pytest.mark.parametrize("proof", ["absent", "wrong_kind", "foreign_session", "forged_valid_shape"])
def test_node_local_retired_classification_needs_a_real_shared_reservation(retirement, proof):
    rig = retirement
    record = json.loads(rig.wake_path.read_bytes())
    record.update(state="retired", reason="git_wake_retired_no_replay")
    if proof != "absent":
        record["retirement"] = dict(schema_version=1, kind="git_wake_retirement", state="retired",
            thread_id=THREAD, repo_path=str(rig.repo), **rig.args)
        if proof == "wrong_kind":
            record["retirement"]["kind"] = "relay_completion"
        elif proof == "foreign_session":
            record["retirement"]["thread_id"] = OTHER_THREAD
    rig.wake_path.write_text(json.dumps(record))
    before = snapshot(rig.home)
    rig.adapter.namespace["_CONTROL_ACTIVE"] = (rig.current_store, rig.current_token)
    with pytest.raises(rig.adapter.namespace["ControlError"], match="retirement"):
        rig.adapter.namespace["observe_wake"](dict(record))
    assert admit(rig.current)
    assert_handoff_preserves_evidence(rig, before)


def test_retirement_metadata_is_not_a_fabricated_native_delivery_receipt(retirement):
    rig = retirement
    retire(rig)
    original = rig.wake_path.read_bytes()
    observed = rig.adapter.namespace["observe_wake"](json.loads(original))
    assert observed["state"] == "retired"
    assert observed["reason"] == "git_wake_retired_no_replay"
    assert observed["retirement"]["instruction_id"] == INSTRUCTION
    assert observed.get("queue_message_id") is None and observed.get("started_turn_id") is None
    assert rig.wake_path.read_bytes() == original
    assert json.loads(original)["state"] == "uncertain"


def test_new_node_observes_shared_retirement_without_creating_a_local_wake_record(retirement):
    rig = retirement
    retire(rig)
    local_records = rig.roots["login-b.example"] / "remote-wake"
    assert not local_records.exists()
    before = snapshot(rig.home)
    observed = rig.adapter.namespace["observe_pending_wake"](INSTRUCTION, THREAD, str(rig.repo))
    assert observed["state"] == "retired" and observed["instruction_id"] == INSTRUCTION
    assert observed["retirement"]["thread_id"] == THREAD
    assert observed.get("queue_message_id") is None and observed.get("started_turn_id") is None
    assert not local_records.exists()
    assert snapshot(rig.home) == before


@pytest.mark.parametrize("new_work", ["pending", "external_effect"])
def test_historical_pointer_changes_during_gate_preserve_one_attempt_without_deadlock(retirement, new_work):
    rig = retirement
    retire(rig)
    def change_after_initial_validation():
        value = rig.old_store.read()
        if new_work == "pending":
            value["remote_state"]["pending_instruction_id"] = "slack:unresolved-original-work"
        else:
            value["external_effect"] = dict(id="effect-started-during-handoff", epoch=value["epoch"],
                instance=value["owner"]["instance"], started=100)
        cs.control_atomic_json(rig.old_store.path, value)
        return True
    rig.current.relay_handoff_safe = change_after_initial_validation
    original_observer = rig.original.path.read_bytes()
    original_wake = rig.wake_path.read_bytes()
    original_current_owner = rig.current_store.path.read_bytes()
    assert admit(rig.current)
    assert cs.control_read_json(rig.original.path)["node"] == "login-b.example"
    assert rig.wake_path.read_bytes() == original_wake
    assert rig.current_store.path.read_bytes() == original_current_owner
    assert rig.pid[0] == 123
