"""Independent integrated regressions for the Windows-only discovery hotfix."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid

import pytest

from test_windows_vscode_discovery import (
    Fixture, FakeChild, FakePipe, HOST_IMAGE, WindowsProcessEntry,
    WindowsProcessIdentity, assert_healthy, default_runner_child, filetime,
    platform_adapters, stamp, tracked, win, windows_adapter, windows_host,
)


@windows_host
@pytest.mark.parametrize("count", [4, 1000])
def test_newer_cli_log_history_cannot_hide_original_windows(tmp_path, count):
    fixture = Fixture(tmp_path)
    for number in range(count):
        (fixture.user.parent / "logs" / ("20990101T%06d" % number)).mkdir()
    index = fixture.index()
    assert_healthy(fixture.discovery(index=index).snapshot(), fixture, (1, 2, 3))
    assert index.last_collection_budget.entries > win.DIRECTORY_ENTRY_BUDGET - 100


@windows_host
def test_delayed_output_channel_and_empty_hint_use_original_held_writer(tmp_path):
    fixture = Fixture(tmp_path)
    for number in (1, 2, 3):
        item = fixture.windows[number]
        fixture.set_cache(number, [])
        fixture.write_exthost(number, start_ms=item["app_ms"] + 17000)
        item["spawn_ms"] = item["app_ms"] + 25000
        fixture.write_codex_log(number)
    assert_healthy(fixture.discovery().snapshot(), fixture, (1, 2, 3))


@windows_host
@pytest.mark.parametrize("change", ["inactive", "conflicting_cache", "replacement_child"])
def test_final_fresh_proof_rejects_late_changes(tmp_path, change, monkeypatch):
    fixture = Fixture(tmp_path)
    index = fixture.index()
    item = fixture.windows[1]
    original = index.collector.read_from
    changed = [False]

    def read(path, offset, limit, budget):
        result = original(path, offset, limit, budget)
        if path == item["codex_log"] and budget is index.last_budget and not changed[0]:
            changed[0] = True
            if change == "inactive":
                result += ("thread_stream_view_activity_changed conversationId="
                           + item["session"] + " active=false\n").encode()
            elif change == "conflicting_cache":
                fixture.set_cache(1, [str(uuid.uuid4())])
            else:
                fixture.reader.rows[item["app"] + 2] = WindowsProcessEntry(
                    item["app"] + 2, item["host"], "codex.exe")
        return result

    monkeypatch.setattr(index.collector, "read_from", read)
    snapshot = fixture.discovery(index=index).snapshot()
    assert changed[0]
    assert "repo1" not in tracked(snapshot)
    assert_healthy(snapshot, fixture)


def test_cache_size_and_parser_share_one_metered_snapshot(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    item = fixture.windows[1]
    database = fixture.user / "workspaceStorage" / item["key"] / "state.vscdb"
    real_connect = sqlite3.connect
    with real_connect(str(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    changed = [False]
    connections = []

    class Connection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            cursor = super().execute(statement, parameters)
            if statement == win.CACHE_SIZE_LOOKUP and not changed[0]:
                changed[0] = True
                # A concurrent growth/replacement after size selection cannot
                # change this read transaction's bounded value.
                with real_connect(str(database)) as writer:
                    writer.execute("UPDATE ItemTable SET value=? WHERE key=?", (
                        "[" + " " * (win.CACHE_VALUE_BYTES + 1) + "]",
                        win._CODEX_SESSION_CACHE_KEY))
            return cursor

    def connect(*args, **kwargs):
        connections.append(args)
        return real_connect(*args, factory=Connection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(win.CodexSessionResolver, "window_session_candidates",
                        staticmethod(lambda *_: pytest.fail("unmetered reopen")))
    assert fixture.index()._cache_sessions(database, win.WorkBudget()) == {item["session"]}
    assert changed[0] and len(connections) == 1


@windows_host
def test_collection_pressure_does_not_starve_completed_admissions(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    monkeypatch.setattr(win, "EXTHOST_LOG_BYTES", 4096)
    monkeypatch.setattr(win, "CODEX_LOG_BYTES", 8192)
    monkeypatch.setattr(win, "CACHE_VALUE_BYTES", 1024)
    monkeypatch.setattr(win, "TOTAL_LOG_BYTES", 20000)
    for number in (1, 2, 3):
        fixture.write_codex_log(number, prefix="padding\n" * 800)
    discovery = fixture.discovery()
    seen = set()
    exhausted = False
    for _ in range(8):
        snapshot = discovery.snapshot()
        seen.update(tracked(snapshot))
        exhausted |= bool(discovery.live_window_index.collector.proof_budget.reason)
    assert exhausted
    assert seen == {"repo1", "repo2", "repo3"}


@windows_host
def test_incomplete_domain_never_accepts_first_seen_match(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    item = fixture.windows[1]
    duplicate = fixture.session_dir / "window99/exthost/exthost.log"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(item["exthost_log"].read_bytes())
    # Keep the entry document within its independent allowance so this fixture
    # exercises mapping exhaustion, rather than stopping at the earlier C1 gate.
    (fixture.user / "globalStorage/storage.json").write_text(json.dumps({
        "windowsState": {"openedWindows": [{"folder": item["repo"].as_uri()}]}
    }), encoding="utf-8")
    monkeypatch.setattr(win, "TOTAL_LOG_BYTES", 350)
    snapshot = fixture.discovery().snapshot()
    assert not tracked(snapshot)
    assert "windows_native_log_domain_incomplete" in snapshot.issues


@windows_host
@pytest.mark.parametrize("pressure", ["oversized", "mapping_budget"])
def test_unreadable_domain_preserves_independent_complete_gui(tmp_path, monkeypatch, pressure):
    fixture = Fixture(tmp_path)
    main = fixture.main_pid + 100000
    born = fixture.base_ms // 1000 + 5
    folder = fixture.user.parent / "logs" / time.strftime("%Y%m%dT%H%M%S", time.localtime(born))
    fixture.reader.rows[main] = WindowsProcessEntry(main, 8, "Code.exe")
    fixture.reader.ids[main] = WindowsProcessIdentity(
        main, filetime(born * 1000), HOST_IMAGE, '"' + HOST_IMAGE + '"', True, True)
    for number in (2, 3, 4):
        item = fixture.windows[number]
        source = fixture.session_dir / ("window" + str(number))
        target = folder / source.name
        target.parent.mkdir(exist_ok=True, parents=True)
        shutil.move(str(source), str(target))
        item["exthost_log"] = target / "exthost/exthost.log"
        item["codex_log"] = target / "exthost/openai.chatgpt/Codex.log"
        fixture.reader.rows[item["host"]] = WindowsProcessEntry(item["host"], main, "Code.exe")
    if pressure == "oversized":
        monkeypatch.setattr(win, "EXTHOST_LOG_BYTES", 4096)
    else:
        monkeypatch.setattr(win, "TOTAL_LOG_BYTES", 131072)
        monkeypatch.setattr(win, "CODEX_LOG_BYTES", 4096)
        monkeypatch.setattr(win, "EXTHOST_LOG_BYTES", 65536)
        monkeypatch.setattr(win, "CACHE_VALUE_BYTES", 1024)
        for number in (91, 92, 93):
            log = fixture.session_dir / ("window" + str(number)) / "exthost/exthost.log"
            log.parent.mkdir(parents=True)
            log.write_bytes(("padding\n" * 7500 + stamp(fixture.base_ms) +
                             " Extension host with pid 999999 started\n").encode())
    fixture.write_exthost(1, prefix="unreadable oversized metadata\n" * 1500)
    discovery = fixture.discovery()
    snapshot = discovery.snapshot()
    if pressure == "mapping_budget":
        snapshot = discovery.snapshot()  # Next GUI domain gets its fair turn.
    assert "repo1" not in tracked(snapshot)
    assert "windows_native_log_domain_incomplete" in snapshot.issues
    assert_healthy(snapshot, fixture)


def test_repeated_timeout_keeps_at_most_one_owned_probe(tmp_path, monkeypatch):
    gate = threading.Event()
    child = FakeChild(FakePipe(gate=gate), FakePipe(), running=True)
    created = default_runner_child(monkeypatch, child)
    adapter = windows_adapter(tmp_path)
    try:
        first = platform_adapters.run_vscode_status(adapter, timeout_seconds=.05)
        assert first.reason == "vscode_status_timeout"
        for _ in range(30):
            assert platform_adapters.run_vscode_status(
                adapter, timeout_seconds=.05).reason == "vscode_status_probe_pending"
        assert len(created) == 1 and child.killed == 1
        assert len(platform_adapters._WINDOWS_PROBE_READERS) == 2
    finally:
        gate.set()
        for reader in platform_adapters._WINDOWS_PROBE_READERS:
            reader.join(1)
