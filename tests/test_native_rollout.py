"""Exact native lookup: native fixtures only, no provider/CLI or session scans."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from codex_watchdog import native_rollout
from codex_watchdog.native_rollout import find_native_rollout
from codex_watchdog.queue_wake import QueueWakeDispatcher
from test_queue_wake import FakeRunner, THREAD_ID


OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def native_state(home, repo, thread=THREAD_ID, *, indexed=True):
    home.mkdir(parents=True, exist_ok=True)
    repo.mkdir(parents=True, exist_ok=True)
    rollout = home / "sessions" / "2026" / "10" / "14" / ("rollout-test-" + thread + ".jsonl")
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_bytes(b'{"type":"session_meta"}\n')
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT%s, cwd TEXT, source TEXT, "
                   "thread_source TEXT, archived INTEGER, rollout_path TEXT, unknown TEXT)"
                   % (" PRIMARY KEY" if indexed else ""))
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
                   (thread, str(repo), "vscode", "user", 0, str(rollout), "preserve"))
    return rollout


@pytest.fixture
def native(tmp_path):
    home, repo = tmp_path / "profile", tmp_path / "repo"
    return home, repo, native_state(home, repo)


def change(home, **fields):
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?",
                   (*fields.values(), THREAD_ID))


def test_exact_native_lookup_preserves_native_bytes_and_ignores_other_rollout(native):
    home, repo, rollout = native
    sibling = rollout.with_name("rollout-extra-" + THREAD_ID + ".jsonl")
    sibling.write_bytes(b"unindexed historical sibling")
    before = {p: p.read_bytes() for p in (home / "state_5.sqlite", rollout, sibling)}
    assert find_native_rollout(home, THREAD_ID, repo) == rollout
    assert find_native_rollout(home, THREAD_ID) == rollout
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize("field,value", [
    ("source", "cli"), ("source", '{"subagent":"other"}'),
    ("source", None), ("thread_source", "subagent"), ("thread_source", None),
    ("archived", 1), ("archived", None), ("archived", "false"),
    ("cwd", "relative/repo"), ("cwd", "/missing-native-repository"),
    ("cwd", "x" * 4097), ("rollout_path", "relative.jsonl"),
    ("rollout_path", "x" * 4097), ("rollout_path", None),
])
def test_invalid_current_row_never_falls_back_to_matching_history(native, field, value):
    home, repo, rollout = native
    change(home, **{field: value})
    assert find_native_rollout(home, THREAD_ID, repo) is None
    assert rollout.is_file()


@pytest.mark.parametrize("thread", [OTHER, OTHER.upper(), "../" + THREAD_ID, "bad", None])
def test_only_exact_canonical_uuid_is_selected(native, thread):
    home, repo, _ = native
    assert find_native_rollout(home, thread, repo) is None


def test_uppercase_uuid_cannot_select_existing_lowercase_row(tmp_path):
    home, repo = tmp_path / "profile", tmp_path / "repo"
    rollout = native_state(home, repo, OTHER)
    assert find_native_rollout(home, OTHER, repo) == rollout
    assert find_native_rollout(home, OTHER.upper(), repo) is None


def test_native_collation_cannot_substitute_noncanonical_stored_uuid(tmp_path):
    home, repo = tmp_path / "profile", tmp_path / "repo"
    rollout = native_state(home, repo, OTHER)
    database = home / "state_5.sqlite"
    database.unlink()
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY COLLATE NOCASE,cwd,source,thread_source,archived,rollout_path)")
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)",
                   (OTHER.upper(), str(repo), "vscode", "user", 0, str(rollout)))
    assert find_native_rollout(home, OTHER, repo) is None


def test_controlled_queue_baseline_binds_repository(native, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from codex_watchdog import queue_wake
    home, repo, rollout = native
    selected = SimpleNamespace(repo_path=repo)
    monkeypatch.setattr(queue_wake, "current_control", lambda: (selected, {}))
    dispatcher = QueueWakeDispatcher(home / "runtime", codex_home=home, codex_executable="fake")
    assert dispatcher._find_rollout(THREAD_ID) == rollout
    other = tmp_path / "different-repository"
    other.mkdir()
    selected.repo_path = other
    assert dispatcher._find_rollout(THREAD_ID) is None


def test_wrong_repo_and_profile_do_not_reuse_a_cached_path(native, tmp_path):
    home, repo, rollout = native
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    assert find_native_rollout(home, THREAD_ID, repo) == rollout
    assert find_native_rollout(home, THREAD_ID, other_repo) is None
    other_home = tmp_path / "other-profile"
    other = native_state(other_home, other_repo)
    assert find_native_rollout(other_home, THREAD_ID, other_repo) == other
    assert find_native_rollout(other_home, THREAD_ID, repo) is None
    change(home, archived=1)
    assert find_native_rollout(home, THREAD_ID, repo) is None
    change(home, archived=0, source="cli")
    assert find_native_rollout(home, THREAD_ID, repo) is None


def test_disappearance_reappearance_and_restart_have_no_negative_cache(native):
    home, repo, rollout = native
    rollout.unlink()
    assert find_native_rollout(home, THREAD_ID, repo) is None
    rollout.touch()
    first = QueueWakeDispatcher(home / "runtime", codex_home=home, codex_executable="fake")
    assert first._find_rollout(THREAD_ID) == rollout
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("DELETE FROM threads WHERE id=?", (THREAD_ID,))
    restarted = QueueWakeDispatcher(home / "runtime", codex_home=home, codex_executable="fake")
    assert restarted._find_rollout(THREAD_ID) is None
    assert first._find_rollout(THREAD_ID) is None


@pytest.mark.parametrize("kind", ["outside", "wrong_filename", "parent_traversal", "leaf_link", "directory_link", "sessions_link", "database_link", "wal_link"])
def test_alias_and_path_confusion_refuse_without_touching_native(native, tmp_path, kind):
    home, repo, rollout = native
    if kind == "outside":
        outside = tmp_path / rollout.name
        outside.write_bytes(b"outside")
        change(home, rollout_path=str(outside))
    elif kind == "wrong_filename":
        wrong = rollout.with_name("rollout-test-" + OTHER + ".jsonl")
        rollout.rename(wrong)
        change(home, rollout_path=str(wrong))
    elif kind == "parent_traversal":
        change(home, rollout_path=str(rollout.parent / ".." / "14" / rollout.name))
    else:
        if kind == "leaf_link":
            path = rollout
        elif kind == "directory_link":
            path = rollout.parent
        elif kind == "sessions_link":
            path = home / "sessions"
        elif kind == "database_link":
            path = home / "state_5.sqlite"
        else:
            path = home / "state_5.sqlite-wal"
            path.touch()
        moved = tmp_path / (kind + "-target")
        path.rename(moved)
        try:
            path.symlink_to(moved, target_is_directory=moved.is_dir())
        except OSError:
            pytest.skip("symlink privilege unavailable")
    assert find_native_rollout(home, THREAD_ID, repo) is None


@pytest.mark.parametrize("kind", ["missing", "old_filename", "corrupt", "old_columns", "view", "no_index", "duplicate"])
def test_native_schema_ambiguity_and_old_state_are_preserved(native, kind):
    home, repo, rollout = native
    database = home / "state_5.sqlite"
    if kind in ("missing", "old_filename"):
        database.rename(home / "state_4.sqlite")
    elif kind == "corrupt":
        database.write_bytes(b"corrupt native state")
    else:
        database.unlink()
        with sqlite3.connect(database) as db:
            if kind == "old_columns":
                db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT)")
                db.execute("INSERT INTO threads VALUES (?,?)", (THREAD_ID, str(rollout)))
            elif kind == "view":
                db.execute("CREATE VIEW threads AS SELECT 1 AS id")
            else:
                db.execute("CREATE TABLE threads(id,cwd,source,thread_source,archived,rollout_path)")
                row = (THREAD_ID, str(repo), "vscode", "user", 0, str(rollout))
                db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", row)
                if kind == "duplicate":
                    db.execute("CREATE INDEX exact_id ON threads(id)")
                    db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", row)
    before = database.read_bytes() if database.exists() else None
    assert find_native_rollout(home, THREAD_ID, repo) is None
    assert (database.read_bytes() if database.exists() else None) == before
    assert rollout.is_file()


def test_native_write_lock_is_bounded_optional_evidence_gap(native):
    home, repo, _ = native
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("BEGIN EXCLUSIVE")
        assert find_native_rollout(home, THREAD_ID, repo) is None


def test_optional_lookup_failure_does_not_replay_or_gate_independent_queue(native):
    home, repo, _ = native
    runner = FakeRunner()
    dispatcher = QueueWakeDispatcher(home / "runtime", codex_home=home,
                                      codex_executable="fake", runner=runner)
    change(home, archived=1)
    assert dispatcher.dispatch(THREAD_ID, "first", "one", "manual").status == "enqueued"
    record = dispatcher.records / (hashlib.sha256(b"first").hexdigest() + ".json")
    assert json.loads(record.read_text())["rollout_path"] is None
    assert dispatcher.dispatch(THREAD_ID, "first", "one", "manual").deduplicated
    assert dispatcher.dispatch(THREAD_ID, "second", "two", "manual").status == "enqueued"
    assert len(runner.calls) == 2


@pytest.mark.parametrize("history", [0, 1000, 10000, 100000])
def test_history_does_not_increase_lookup_work(tmp_path, monkeypatch, history):
    home, repo = tmp_path / "profile", tmp_path / "repo"
    rollout = native_state(home, repo)
    old = home / "sessions" / "old"
    old.mkdir()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
                       ((str(i), str(repo), "cli", "user", 1, str(old / (str(i) + ".jsonl")), "old")
                        for i in range(history)))
    for i in range(history):
        (old / (str(i) + ".jsonl")).touch()
    real_connect = sqlite3.connect
    stats = {}

    class Measured(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            stats["sql_statements"] += 1
            return super().execute(sql, *args, **kwargs)

        def set_progress_handler(self, callback, interval):
            def count():
                stats["vm_steps"] += 1
                return callback() if stats["vm_steps"] % interval == 0 else 0
            return super().set_progress_handler(count, 1)

    real_stat, real_lstat = Path.stat, Path.lstat
    def stat_call(path, *args, **kwargs):
        stats["stat_calls"] += 1
        return real_stat(path, *args, **kwargs)
    def lstat_call(path, *args, **kwargs):
        stats["lstat_calls"] += 1
        return real_lstat(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(native_rollout.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Measured, **k))
        patch.setattr(Path, "stat", stat_call)
        patch.setattr(Path, "lstat", lstat_call)
        for name in ("rglob", "glob", "iterdir", "read_bytes", "read_text", "open"):
            patch.setattr(Path, name, lambda *a, **k: pytest.fail("lookup must not enumerate or read native history"))
        observations = []
        for target, expected in ((THREAD_ID, rollout), (OTHER, None)):
            stats.clear()
            stats.update(sql_statements=0, vm_steps=0, stat_calls=0, lstat_calls=0)
            assert find_native_rollout(home, target, repo) == expected
            observations.append(dict(stats, target_present=expected is not None))
        # Exact missing UUID tests zero recent work amid the full historical set.
        assert [item["sql_statements"] for item in observations] == [5, 5]
        assert all(item["vm_steps"] < 250 for item in observations)
    print("NATIVE_ROLLOUT_WORK " + json.dumps(dict(history=history, active=1, queue=0,
          operations=observations, native_history_content_bytes_read=0,
          native_file_writes=0, directory_entries_visited=0), sort_keys=True))
    assert rollout.read_bytes() == b'{"type":"session_meta"}\n'
