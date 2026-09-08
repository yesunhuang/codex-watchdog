from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

import pytest

from tools import probe_linux_lifecycle as probe_module


THREAD = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def seed(tmp_path, monkeypatch):
    repo = tmp_path / "private-workspace"
    repo.mkdir()
    home = tmp_path / "private-codex-home"
    home.mkdir()
    (home / "sessions").mkdir()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INTEGER, source TEXT, thread_source TEXT)")
    monkeypatch.setattr(probe_module, "held_lock", lambda path: True)
    monkeypatch.setattr(probe_module, "process_inventory", lambda root, lock: {"status": "observed", "processes": []})
    return repo, home


def insert(home, repo, thread=THREAD, **overrides):
    values = dict(id=thread, rollout_path=str(home / "sessions" / f"{thread}.jsonl"),
                  cwd=str(repo), archived=0, source="vscode", thread_source="user")
    values.update(overrides)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", tuple(values.values()))
    return Path(values["rollout_path"])


def test_exact_cwd_ambiguity_never_selects_a_newest_thread(tmp_path, monkeypatch):
    repo, home = seed(tmp_path, monkeypatch)
    insert(home, repo)
    insert(home, repo, OTHER)
    probe = probe_module.LifecycleProbe(repo, home)
    result = probe.snapshot()
    assert result["thread_state"] == "ambiguous"
    assert probe.thread is None
    assert result["ownership_established"] is False
    assert "writer_lock_held" not in result


def test_capture_pins_thread_and_never_switches_after_archive(tmp_path, monkeypatch):
    repo, home = seed(tmp_path, monkeypatch)
    insert(home, repo)
    probe = probe_module.LifecycleProbe(repo, home)
    first = probe.snapshot()
    assert first["thread_state"] == "present"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET archived=1 WHERE id=?", (THREAD,))
    insert(home, repo, OTHER)
    second = probe.snapshot()
    assert second["thread_state"] == "unavailable"
    assert second["thread_sha256"] == first["thread_sha256"]
    assert probe.thread == THREAD
    assert "queue" not in second


def test_pinned_thread_still_requires_exact_cwd_and_user_source(tmp_path, monkeypatch):
    repo, home = seed(tmp_path, monkeypatch)
    insert(home, repo, cwd=str(tmp_path), thread_source="guardian_review")
    result = probe_module.LifecycleProbe(repo, home, thread=THREAD).snapshot()
    assert result["thread_state"] == "unavailable"
    assert "writer_lock_held" not in result


def test_probe_keeps_only_rollout_metadata_and_does_not_change_files(tmp_path, monkeypatch):
    repo, home = seed(tmp_path, monkeypatch)
    rollout = insert(home, repo)
    secret = "PRIVATE_PROMPT_AND_ASSISTANT_SENTINEL"
    events = [
        {"type": "session_meta", "payload": {"id": THREAD, "cwd": str(repo)}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-private"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": secret}},
        {"type": "response_item", "payload": {"type": "message", "content": secret}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-private", "last_agent_message": secret}},
    ]
    rollout.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    result = probe_module.LifecycleProbe(repo, home).snapshot()
    assert result["writer_lock_held"] is True
    assert result["rollout"]["last_turn_event"] == "task_complete"
    assert result["rollout"]["event_counts"] == dict(task_started=1, task_complete=1, user_message=1, turn_aborted=0)
    encoded = json.dumps(result)
    for forbidden in [secret, THREAD, str(repo), str(home), "turn-private"]:
        assert forbidden not in encoded
    assert before == {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}


def test_rollout_rejects_paths_outside_the_selected_codex_sessions(tmp_path):
    path = tmp_path / "private-transcript.jsonl"
    path.write_text("sensitive content", encoding="utf-8")
    assert probe_module.rollout_metadata(path, tmp_path / "codex") == {"status": "outside_sessions"}


def test_rollout_tail_is_bounded_and_labels_incomplete_counts(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "rollout.jsonl"
    path.write_bytes(b"x" * (1024 * 1024 + 10) + b'\n{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn"}}\n')
    result = probe_module.rollout_metadata(path, tmp_path)
    assert result["tail_only"] is True
    assert result["event_counts"]["task_complete"] == 1


def test_process_parentage_and_writer_descriptor_are_correlated_without_argv(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    boot = root / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("private-boot-id")
    lock = tmp_path / "private-writer-lock"
    for pid, parent, args in [(10, 1, b"node\0--type=extensionHost\0"),
                              (20, 10, b"/private/codex\0app-server\0secret-command-argument\0")]:
        path = root / str(pid)
        (path / "fd").mkdir(parents=True)
        (path / "cmdline").write_bytes(args)
        fields = ["S", str(parent)] + ["0"] * 17 + [str(pid + 500)]
        (path / "stat").write_text(f"{pid} (process name) " + " ".join(fields))
    (root / "20/fd/7").touch()
    monkeypatch.setattr(os, "getuid", lambda: root.stat().st_uid, raising=False)
    monkeypatch.setattr(os, "readlink", lambda path: str(lock))
    result = probe_module.process_inventory(root, lock)
    assert result["status"] == "observed"
    records = {record["kind"]: record for record in result["processes"]}
    child = records["codex_app_server"]
    parent = records["vscode_extension_host"]
    assert child["holds_target_descriptor"] is True
    assert child["parent_identity_sha256"] == parent["identity_sha256"]
    assert parent["holds_target_descriptor"] is False
    assert "secret-command-argument" not in json.dumps(result)
    assert "private-boot-id" not in json.dumps(result)
    assert str(lock) not in json.dumps(result)


def test_existing_capture_is_never_overwritten(tmp_path, monkeypatch, capsys):
    output = tmp_path / "capture.jsonl"
    output.write_text("earlier evidence")
    monkeypatch.setattr(probe_module.sys, "platform", "linux")
    result = probe_module.main(["--repo", str(tmp_path), "--output", str(output)])
    assert result == 1
    assert output.read_text() == "earlier evidence"
    assert json.loads(capsys.readouterr().out)["reason"] == "probe_setup_failed"


def test_native_lock_probe_is_read_only_and_distinguishes_released_lock(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    path = tmp_path / "writer.lock"
    assert probe_module.held_lock(path) is False
    assert not path.exists()
    path.write_bytes(b"existing lock metadata")
    with path.open("rb") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert probe_module.held_lock(path) is True
        fcntl.flock(owner, fcntl.LOCK_UN)
    assert probe_module.held_lock(path) is False
    assert path.read_bytes() == b"existing lock metadata"
