from __future__ import annotations

import calendar
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
import uuid

import pytest

from codex_watchdog import platform_adapters
from codex_watchdog import windows_vscode_discovery as win
from codex_watchdog import workspace_discovery
from codex_watchdog.platform_adapters import (
    VSCodeStatusProbe,
    detect_platform_adapter,
    run_vscode_status,
)
from codex_watchdog.windows_vscode_discovery import (
    FALLBACK_SOURCE,
    FILETIME_UNIX_EPOCH,
    WindowsProcessEntry,
    WindowsProcessIdentity,
    WindowsVSCodeLiveWindowIndex,
)
from codex_watchdog.workspace_discovery import (
    CodexSessionResolver,
    EffectiveWorkspaceCatalog,
    LiveVSCodeWindow,
    SessionResolution,
    VSCodeLiveWindowIndex,
    VSCodeWorkspaceDiscovery,
)
from codex_watchdog.workspace_registry import WorkspaceRegistry


HOST_IMAGE = "C:\\Apps\\VS Code\\Code.exe"
APP_IMAGE = (
    "C:\\Home\\.vscode\\extensions\\openai.chatgpt-0.0.1-win32-x64"
    "\\bin\\windows-x86_64\\codex.exe"
)
STATUS_HEADER = "CPU %\tMem MB\t   PID\tProcess\n"
# Discovery dispatches the Windows hooks only when os.name == "nt" as well.
windows_host = pytest.mark.skipif(os.name != "nt", reason="guarded Windows-only discovery path")


def utc_ms(stamp: bytes):
    parsed = time.strptime(stamp[:19].decode("ascii"), "%Y-%m-%d %H:%M:%S")
    return calendar.timegm(parsed) * 1000 + int(stamp[20:23])


def stamp(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms // 1000)) + ".%03d" % (ms % 1000)


def filetime(ms: int) -> int:
    return ms * 10000 + FILETIME_UNIX_EPOCH


class FakeReader:
    """Synthetic read-only process view; counts calls to model races."""

    def __init__(self) -> None:
        self.rows = {}
        self.extra_rows = []
        self.ids = {}
        self.id_calls = {}
        self.id_hooks = {}
        self.entry_calls = 0
        self.entry_hook = None
        self.writers = {}
        self.writer_calls = {}
        self.writer_hooks = {}
        self.held = set()
        self.held_calls = {}
        self.release_after = {}
        self.foreign_locks = set()

    def entries(self, limit):
        rows = list(self.rows.values()) + list(self.extra_rows)
        if self.entry_hook is not None:
            rows = self.entry_hook(self.entry_calls, rows)
        self.entry_calls += 1
        if rows is None or len(rows) > limit:
            return None
        return tuple(rows)

    def identity(self, pid):
        index = self.id_calls.get(pid, 0)
        self.id_calls[pid] = index + 1
        value = self.ids.get(pid)
        hook = self.id_hooks.get(pid)
        return hook(index, value) if hook is not None else value

    def writer(self, lock):
        index = self.writer_calls.get(lock.stem, 0)
        self.writer_calls[lock.stem] = index + 1
        value = self.writers.get(lock.stem)
        hook = self.writer_hooks.get(lock.stem)
        return hook(index, value) if hook is not None else value

    def lock_held(self, lock):
        index = self.held_calls.get(lock.stem, 0)
        self.held_calls[lock.stem] = index + 1
        limit = self.release_after.get(lock.stem)
        return lock.stem in self.held and (limit is None or index < limit)

    def writer_same_user(self, lock, pid, birth):
        identity = self.ids.get(pid)
        return (lock.stem not in self.foreign_locks and identity is not None
                and identity.birth == birth and identity.same_user and identity.alive)


class Fixture:
    """Three healthy windows plus an unrelated fourth window that changes."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.user = tmp_path / "Code" / "User"
        self.codex_home = tmp_path / ".codex"
        self.runtime = tmp_path / "runtime"
        self.reader = FakeReader()
        self.base_ms = int(time.time() * 1000) - 3_600_000
        self.session_dir = self.user.parent / "logs" / time.strftime(
            "%Y%m%dT%H%M%S", time.localtime(self.base_ms // 1000 - 60))
        self.main_pid = 50 + (uuid.uuid4().int % 50)
        self.reader.rows[self.main_pid] = WindowsProcessEntry(self.main_pid, 8, "Code.exe")
        self.reader.ids[self.main_pid] = WindowsProcessIdentity(
            self.main_pid, filetime((self.base_ms // 1000 - 60) * 1000), HOST_IMAGE,
            f'"{HOST_IMAGE}"', True, True)
        self.windows = {}
        for number in (1, 2, 3, 4):
            self._add(number)
        # The fourth window's host PID is reused on every read: never stable.
        changed = self.windows[4]["host"]
        self.reader.id_hooks[changed] = lambda index, value: (
            value if index == 0 else replace(value, birth=value.birth + index))
        self._write_state()

    def _add(self, number: int) -> None:
        offset = 10000 * number + (uuid.uuid4().int % 500) * 4
        host, app = offset, offset + 1
        host_ms = self.base_ms + number * 10_000
        item = dict(
            number=str(number), host=host, app=app, host_ms=host_ms,
            start_ms=host_ms + 200, spawn_ms=host_ms + 400, app_ms=host_ms + 500,
            key="storage" + uuid.uuid4().hex[:12], session=str(uuid.uuid4()),
            repo=(self.tmp / f"repo{number}").resolve(),
        )
        self.windows[number] = item
        item["repo"].mkdir()
        storage = self.user / "workspaceStorage" / item["key"]
        storage.mkdir(parents=True)
        (storage / "workspace.json").write_text(
            json.dumps({"folder": item["repo"].as_uri()}), encoding="utf-8")
        self.set_cache(number, [item["session"]])
        exthost = self.session_dir / f"window{number}" / "exthost"
        (exthost / "openai.chatgpt").mkdir(parents=True)
        item["exthost_log"] = exthost / "exthost.log"
        item["codex_log"] = exthost / "openai.chatgpt" / "Codex.log"
        self.write_exthost(number)
        self.write_codex_log(number)
        self.reader.rows[host] = WindowsProcessEntry(host, self.main_pid, "Code.exe")
        self.reader.rows[app] = WindowsProcessEntry(app, host, "codex.exe")
        self.reader.ids[host] = WindowsProcessIdentity(
            host, filetime(host_ms), HOST_IMAGE,
            f'"{HOST_IMAGE}" --type=utility --utility-sub-type=node.mojom.NodeService '
            f'--user-data-dir="{self.user.parent}"', True, True,
        )
        self.reader.ids[app] = WindowsProcessIdentity(
            app, filetime(item["app_ms"]), APP_IMAGE,
            f"{APP_IMAGE} app-server --analytics-default-enabled", True, True,
        )
        self.reader.writers[item["session"]] = (app, filetime(item["app_ms"]))
        self.reader.held.add(item["session"])

    def write_exthost(self, number: int, *, start_ms=None, extra: str = "", prefix: str = "") -> None:
        item = self.windows[number]
        start = item["start_ms"] if start_ms is None else start_ms
        item["exthost_log"].write_text(
            prefix
            + f"{stamp(start)} [info] Extension host with pid {item['host']} started\n"
            + f"{stamp(start + 1)} [info] loading workspaceStorage/{item['key']}/state\n"
            + extra,
            encoding="utf-8",
        )

    def write_codex_log(self, number: int, *, role: str = "owner", active: str = "true",
                        spawn: bool = True, extra: str = "", prefix: str = "") -> None:
        item = self.windows[number]
        lines = [prefix]
        if spawn:
            lines.append(f"{stamp(item['spawn_ms'])} [info] [CodexMcpConnection] Spawning codex app-server\n")
        if role:
            lines.append(f"{stamp(item['spawn_ms'] + 50)} [info] thread_stream_role_changed "
                         f"conversationId={item['session']} role={role}\n")
        if active:
            lines.append(f"{stamp(item['spawn_ms'] + 60)} [info] thread_stream_view_activity_changed "
                         f"conversationId={item['session']} active={active}\n")
        item["codex_log"].write_text("".join(lines) + extra, encoding="utf-8")

    def set_cache(self, number: int, sessions) -> None:
        database = self.user / "workspaceStorage" / self.windows[number]["key"] / "state.vscdb"
        resources = [{"providerType": "openai-codex",
                      "resource": f"openai-codex://route/local/{session}"} for session in sessions]
        with sqlite3.connect(str(database)) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value BLOB)")
            connection.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                               ("agentSessions.model.cache", json.dumps(resources)))

    def _write_state(self) -> None:
        state = self.user / "globalStorage" / "storage.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"windowsState": {"openedWindows": [
            {"folder": item["repo"].as_uri()} for item in self.windows.values()
        ]}}), encoding="utf-8")
        self.codex_home.mkdir()
        with sqlite3.connect(str(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY NOT NULL, cwd TEXT NOT NULL, "
                "source TEXT NOT NULL, thread_source TEXT, archived INTEGER NOT NULL)")
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, 'vscode', 'user', 0)",
                [(item["session"], str(item["repo"])) for item in self.windows.values()])

    def add_thread(self, number: int, session: str) -> None:
        with sqlite3.connect(str(self.codex_home / "state_5.sqlite")) as connection:
            connection.execute("INSERT INTO threads VALUES (?, ?, 'vscode', 'user', 0)",
                               (session, str(self.windows[number]["repo"])))

    def status(self, numbers, *, app_override=None) -> str:
        lines = [STATUS_HEADER, f"    0\t  100\t{self.main_pid}\tcode main\n"]
        for number in numbers:
            item = self.windows[number]
            app = (app_override or {}).get(number, item["app"])
            lines.append(f"    0\t  100\t{item['host']}\textension-host [{number}]\n")
            lines.append(f"    0\t  100\t{app}\t     {APP_IMAGE} app-server\n")
        return "".join(lines)

    def resolver(self) -> CodexSessionResolver:
        return CodexSessionResolver(
            self.runtime, self.codex_home,
            lock_probe=lambda path: path.stem in self.reader.held,
            writer_process_probe=lambda path: (self.reader.writers.get(path.stem) or (None,))[0],
        )

    def index(self, probe=None) -> WindowsVSCodeLiveWindowIndex:
        selected = probe or VSCodeStatusProbe("error", "vscode_status_empty", "windows_user_install")
        return WindowsVSCodeLiveWindowIndex(
            self.user, status_probe=lambda: selected, reader=self.reader, log_time_ms=utc_ms,
        )

    def discovery(self, probe=None, *, registry=None, index=None) -> VSCodeWorkspaceDiscovery:
        return VSCodeWorkspaceDiscovery(
            self.runtime, codex_home=self.codex_home, user_data_root=self.user,
            registry=registry or WorkspaceRegistry(self.runtime),
            session_resolver=self.resolver(),
            live_window_index=index or self.index(probe),
            git_root_resolver=lambda path: path, sleep=lambda _seconds: None,
        )


def tracked(snapshot):
    return {
        Path(window.repo_root).name: (window.session_id, window.session_source)
        for window in snapshot.windows if window.tracking_status == "tracked"
    }


def window_for(snapshot, fixture, number):
    repo = str(fixture.windows[number]["repo"])
    return [window for window in snapshot.windows if window.repo_root == repo]


def assert_healthy(snapshot, fixture, numbers=(2, 3), source=FALLBACK_SOURCE):
    found = tracked(snapshot)
    for number in numbers:
        item = fixture.windows[number]
        assert found[item["repo"].name] == (item["session"], source)


@windows_host
@pytest.mark.parametrize("reason", [
    "vscode_status_empty", "vscode_status_malformed", "vscode_status_formatter_failed",
    "vscode_status_timeout", "vscode_status_failed", "vscode_cli_unavailable",
])
def test_degraded_cli_recovers_exact_windows_without_registry(tmp_path, reason):
    fixture = Fixture(tmp_path)

    snapshot = fixture.discovery(VSCodeStatusProbe("error", reason)).snapshot()

    assert_healthy(snapshot, fixture, (1, 2, 3))
    assert "repo4" not in tracked(snapshot)
    assert {item.session_id for item in snapshot.effective_workspaces} == {
        fixture.windows[number]["session"] for number in (1, 2, 3)}
    assert reason in snapshot.issues
    assert "vscode_windows_native_fallback" in snapshot.issues
    assert snapshot.status == "partial"
    assert WorkspaceRegistry(fixture.runtime).list_workspaces() == []


@windows_host
def test_valid_cli_agreement_keeps_existing_resolution_path(tmp_path):
    fixture = Fixture(tmp_path)
    probe = VSCodeStatusProbe("available", "vscode_status_available", "windows_user_install",
                              fixture.status((1, 2, 3)))

    snapshot = fixture.discovery(probe).snapshot()

    assert_healthy(snapshot, fixture, (1, 2, 3), "codex_state_vscode_window_cache_owner")
    assert "vscode_windows_native_fallback" not in snapshot.issues


@windows_host
def test_valid_cli_without_extension_hosts_is_distinct_and_native_recovers(tmp_path):
    fixture = Fixture(tmp_path)
    probe = VSCodeStatusProbe("available", "vscode_status_no_extension_hosts",
                              "windows_user_install", fixture.status(()))

    snapshot = fixture.discovery(probe).snapshot()

    assert_healthy(snapshot, fixture, (1, 2, 3))
    assert "vscode_status_no_extension_hosts" not in snapshot.issues
    assert "vscode_windows_native_fallback" in snapshot.issues


@windows_host
def test_cli_native_conflict_fails_closed_for_that_window_only(tmp_path):
    fixture = Fixture(tmp_path)
    other_app = fixture.windows[1]["app"] + 2
    probe = VSCodeStatusProbe("available", "vscode_status_available", "windows_user_install",
                              fixture.status((1,), app_override={1: other_app}))

    snapshot = fixture.discovery(probe).snapshot()

    assert "repo1" not in tracked(snapshot)
    assert_healthy(snapshot, fixture)


def _break(fixture, monkeypatch, case):
    item = fixture.windows[1]
    reader, host, app, session = fixture.reader, item["host"], item["app"], item["session"]
    if case == "pid_reuse_parent_born_after_child":
        reader.ids[host] = replace(reader.ids[host], birth=reader.ids[app].birth + 1)
    elif case == "self_parent":
        reader.rows[app] = WindowsProcessEntry(app, app, "codex.exe")
    elif case == "parent_cycle":
        reader.rows[host] = WindowsProcessEntry(host, app, "Code.exe")
    elif case == "parent_not_code":
        reader.rows[host] = WindowsProcessEntry(host, fixture.main_pid, "node.exe")
    elif case == "duplicate_pid_record":
        reader.extra_rows.append(WindowsProcessEntry(host, fixture.main_pid, "Code.exe"))
    elif case == "foreign_user_host":
        reader.ids[host] = replace(reader.ids[host], same_user=False)
    elif case == "foreign_user_app":
        reader.ids[app] = replace(reader.ids[app], same_user=False)
    elif case == "missing_permissions":
        reader.ids[host] = None
    elif case == "host_exited":
        reader.ids[host] = replace(reader.ids[host], alive=False)
    elif case == "wrong_profile":
        reader.ids[host] = replace(reader.ids[host], command=reader.ids[host].command.replace(
            str(fixture.user.parent), str(fixture.tmp / "Code - Insiders")))
    elif case == "wrong_app_executable":
        reader.ids[app] = replace(reader.ids[app], image="C:\\Tools\\codex.exe",
                                  command="C:\\Tools\\codex.exe app-server")
    elif case == "log_start_before_host_birth":
        fixture.write_exthost(1, start_ms=item["host_ms"] - 5)
    elif case == "log_start_after_child_birth":
        fixture.write_exthost(1, start_ms=item["app_ms"] + 5)
    elif case == "later_app_server_generation":
        fixture.write_codex_log(1, extra=f"{stamp(item['app_ms'] + 10)} [info] "
                                         "[CodexMcpConnection] Spawning codex app-server\n")
    elif case == "no_app_server_generation":
        fixture.write_codex_log(1, spawn=False)
    elif case == "logged_host_exit":
        fixture.write_exthost(1, extra=f"{stamp(item['app_ms'] + 9)} [info] Extension host "
                                       f"with pid {host} exiting with code 0\n")
    elif case == "ambiguous_storage":
        fixture.write_exthost(1, extra="loading workspaceStorage/unrelatedkey/state\n")
    elif case == "duplicate_host_log":
        other = fixture.session_dir / "window9" / "exthost"
        other.mkdir(parents=True)
        (other / "exthost.log").write_text(item["exthost_log"].read_text(encoding="utf-8"),
                                           encoding="utf-8")
    elif case == "two_app_servers":
        reader.rows[app + 2] = WindowsProcessEntry(app + 2, host, "codex.exe")
        reader.ids[app + 2] = replace(reader.ids[app], pid=app + 2)
    elif case == "rebirth_during_read":
        reader.id_hooks[app] = lambda index, value: (
            value if index == 0 else replace(value, birth=value.birth + 1))
    elif case == "exit_during_read":
        reader.entry_hook = lambda index, rows: (
            rows if index == 0 else [row for row in rows if row.pid != app])
    elif case == "oversized_exthost_log":
        monkeypatch.setattr(win, "EXTHOST_LOG_BYTES", 4096)
        fixture.write_exthost(1, prefix="unrelated padding line\n" * 400)
    elif case == "oversized_codex_log":
        monkeypatch.setattr(win, "CODEX_LOG_BYTES", 4096)
        fixture.write_codex_log(1, prefix="unrelated padding line\n" * 400)
    elif case == "writer_is_other_app_server":
        other = fixture.windows[2]
        reader.writers[session] = (other["app"], filetime(other["app_ms"]))
    elif case == "no_writer":
        del reader.writers[session]
    elif case == "writer_pid_reused":
        reader.writers[session] = (app, filetime(item["app_ms"]) + 1)
    elif case == "lock_not_held":
        reader.held.discard(session)
    elif case == "foreign_lock":
        reader.foreign_locks.add(session)
    elif case == "lock_released_during_proof":
        reader.release_after[session] = 1
    elif case == "lock_replaced_at_recheck":
        reader.writer_hooks[session] = lambda index, value: (
            value if index == 0 else (value[0], value[1] + 1))
    elif case == "lock_released_at_recheck":
        reader.release_after[session] = 2
    elif case == "identity_changed_at_admission":
        reader.id_hooks[app] = lambda index, value: (
            value if index < 2 else replace(value, birth=value.birth + 1))
    elif case == "follower":
        fixture.write_codex_log(1, role="follower")
    elif case == "explicitly_inactive":
        fixture.write_codex_log(1, active="false")
    elif case == "no_owner_marker":
        fixture.write_codex_log(1, role="", active="")
    elif case == "resume_failed":
        fixture.write_codex_log(1, extra=f"maybe_resume_failed conversationId={session}\n")
    elif case == "cache_without_thread":
        fixture.set_cache(1, [])
    elif case == "wrong_thread_uuid_in_cache":
        fixture.set_cache(1, [str(uuid.uuid4())])
    elif case == "multiple_loaded_owners":
        extra = str(uuid.uuid4())
        fixture.add_thread(1, extra)
        reader.held.add(extra)
        fixture.write_codex_log(1, extra=(
            f"thread_stream_role_changed conversationId={extra} role=owner\n"
            f"thread_stream_view_activity_changed conversationId={extra} active=true\n"))
    else:
        raise AssertionError(case)


ADMISSION_REASONS = {
    "explicitly_inactive": "windows_native_inactive_rejected",
    "no_owner_marker": "windows_native_owner_unverified",
    "cache_without_thread": "windows_native_cache_mismatch",
    "wrong_thread_uuid_in_cache": "windows_native_cache_mismatch",
    "writer_is_other_app_server": "windows_native_writer_unverified",
    "no_writer": "windows_native_writer_unverified",
    "writer_pid_reused": "windows_native_writer_unverified",
    "foreign_lock": "windows_native_writer_unverified",
    "lock_released_during_proof": "windows_native_writer_unverified",
    "lock_replaced_at_recheck": "windows_native_writer_changed",
    "lock_released_at_recheck": "windows_native_writer_changed",
    "identity_changed_at_admission": "windows_native_identity_changed",
}


@windows_host
@pytest.mark.parametrize("case", [
    "pid_reuse_parent_born_after_child", "self_parent", "parent_cycle", "parent_not_code",
    "duplicate_pid_record", "foreign_user_host", "foreign_user_app", "missing_permissions",
    "host_exited", "wrong_profile", "wrong_app_executable", "log_start_before_host_birth",
    "later_app_server_generation", "no_app_server_generation",
    "logged_host_exit", "ambiguous_storage", "duplicate_host_log", "two_app_servers",
    "rebirth_during_read", "exit_during_read", "oversized_codex_log",
    "writer_is_other_app_server", "no_writer", "writer_pid_reused", "lock_not_held",
    "foreign_lock", "lock_released_during_proof", "lock_replaced_at_recheck",
    "lock_released_at_recheck", "identity_changed_at_admission", "follower",
    "explicitly_inactive", "no_owner_marker", "resume_failed",
    "wrong_thread_uuid_in_cache", "multiple_loaded_owners",
])
def test_one_refused_window_fails_closed_and_healthy_windows_survive(tmp_path, monkeypatch, case):
    fixture = Fixture(tmp_path)
    _break(fixture, monkeypatch, case)

    snapshot = fixture.discovery().snapshot()

    assert "repo1" not in tracked(snapshot)
    assert fixture.windows[1]["session"] not in {
        item.session_id for item in snapshot.effective_workspaces}
    assert_healthy(snapshot, fixture)
    if case in ADMISSION_REASONS:
        [window] = window_for(snapshot, fixture, 1)
        assert window.tracking_status == "unresolved"
        assert window.reason == ADMISSION_REASONS[case]
        assert ADMISSION_REASONS[case] in snapshot.issues


def test_admission_rejects_shared_process_follower_and_ignores_cli_windows(tmp_path):
    fixture = Fixture(tmp_path)
    index = fixture.index()
    live = index.snapshot()
    item = fixture.windows[1]
    window = live[item["key"]]
    database = fixture.user / "workspaceStorage" / item["key"] / "state.vscdb"
    accepted = SessionResolution("resolved", item["session"], "codex_state_vscode_live_owner", None)

    def admit(resolution, windows, key=item["key"]):
        return index.windows_fallback_admission(
            key, resolution, codex_home=fixture.codex_home, window_state_database=database,
            live_windows=windows, session_resolver=fixture.resolver())

    assert admit(accepted, tuple(live.values())).source == FALLBACK_SOURCE
    shared = LiveVSCodeWindow("9", item["host"] + 7, item["app"], fixture.tmp / "other.log")
    assert admit(accepted, (window, shared)).reason == "windows_native_window_ambiguous"
    follower = SessionResolution("resolved", item["session"],
                                 "codex_state_vscode_active_follower_verified_owner",
                                 "vscode_thread_owned_by_another_window")
    assert admit(follower, tuple(live.values())).reason == "windows_native_follower_rejected"
    assert admit(accepted, tuple(live.values()), key="cli-only-key") is accepted


@windows_host
def test_historical_logs_and_sessions_are_not_read(tmp_path):
    fixture = Fixture(tmp_path)
    logs = fixture.user.parent / "logs"
    old_seconds = fixture.base_ms // 1000 - 86400
    for day in range(1, 11):
        session = logs / f"2001{day:02d}01T000000"
        for number in (1, 2, 3):
            exthost = session / f"window{number}" / "exthost"
            exthost.mkdir(parents=True)
            log = exthost / "exthost.log"
            # Reused PIDs with conflicting storage would be ambiguous if read.
            log.write_text(
                "padding historical line\n" * 4000
                + f"Extension host with pid {fixture.windows[number]['host']} started\n"
                + "loading workspaceStorage/historicalkey/state\n", encoding="utf-8")
            if day != 1:
                os.utime(log, (old_seconds, old_seconds))
        (session / "unrelated").mkdir()
    index = fixture.index()
    reads = []
    original = index.collector.read_limited

    def spy(path, limit, budget):
        reads.append(path)
        return original(path, limit, budget)

    index.collector.read_limited = spy

    snapshot = fixture.discovery(index=index).snapshot()

    assert_healthy(snapshot, fixture, (1, 2, 3))
    assert reads and all(fixture.session_dir in path.parents for path in reads)
    # One exthost and Codex.log read per current window plus one admission recheck.
    assert len(reads) <= 3 * len(fixture.windows)


@windows_host
def test_log_index_and_candidate_budgets_refuse_without_guessing(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    registry = WorkspaceRegistry(fixture.runtime)
    manual = registry.add("manual-two", fixture.windows[2]["repo"], fixture.windows[2]["session"]).workspace
    monkeypatch.setattr(win, "CANDIDATE_LIMIT", 2)
    discovery = fixture.discovery(registry=registry, index=fixture.index())

    # The per-snapshot work cap defers fairly: it is not a permanent window cap.
    seen = set()
    for _ in range(2):
        snapshot = discovery.snapshot()
        assert snapshot.status == "partial"
        assert manual in snapshot.effective_workspaces
        assert "windows_native_budget_exceeded" in snapshot.issues
        assert "vscode_status_empty" in snapshot.issues
        fallback = {name for name, (_session, source) in tracked(snapshot).items()
                    if source == FALLBACK_SOURCE}
        assert len(fallback) <= 1
        seen |= fallback
    assert seen == {"repo1", "repo3"}

    monkeypatch.setattr(win, "CANDIDATE_LIMIT", 64)
    # Arbitrary historical directories are never listed by the GUI locator.
    monkeypatch.setattr(win, "LOG_SESSION_SCAN_LIMIT", 1)
    (fixture.user.parent / "logs" / "unrelated-entry").mkdir()
    assert_healthy(fixture.discovery(registry=registry).snapshot(), fixture, (1, 3))


@windows_host
def test_manual_registration_preserved_while_fallback_covers_other_windows(tmp_path):
    fixture = Fixture(tmp_path)
    registry = WorkspaceRegistry(fixture.runtime)
    item = fixture.windows[1]
    manual = registry.add("manual-one", item["repo"], item["session"]).workspace

    snapshot = fixture.discovery(registry=registry).snapshot()

    [window] = window_for(snapshot, fixture, 1)
    assert (window.source, window.workspace_id) == ("manual_override", "manual-one")
    assert manual in snapshot.effective_workspaces
    assert_healthy(snapshot, fixture)
    assert {window.source for window in snapshot.windows
            if window.session_source == FALLBACK_SOURCE} == {"automatic"}


@windows_host
def test_unavailable_native_view_keeps_manual_entries_and_safe_issues(tmp_path):
    fixture = Fixture(tmp_path)
    fixture.reader.entry_hook = lambda _index, _rows: None
    registry = WorkspaceRegistry(fixture.runtime)
    manual = registry.add("manual-one", fixture.windows[1]["repo"], fixture.windows[1]["session"]).workspace

    snapshot = fixture.discovery(registry=registry).snapshot()

    assert snapshot.status == "error"
    assert snapshot.effective_workspaces == (manual,)
    assert snapshot.issues == (
        "vscode_live_status_unavailable", "vscode_status_empty", "windows_native_unavailable")


@windows_host
def test_catalog_polling_path_tracks_fallback_and_identity_is_stable(tmp_path):
    fixture = Fixture(tmp_path)
    registry = WorkspaceRegistry(fixture.runtime)
    catalog = EffectiveWorkspaceCatalog(
        fixture.runtime, registry=registry, discovery=fixture.discovery(registry=registry))

    degraded = catalog.list_workspaces()

    assert {item.session_id for item in degraded} == {
        fixture.windows[number]["session"] for number in (1, 2, 3)}
    assert all(catalog.is_current(item) for item in degraded)
    persisted = json.loads((fixture.runtime / "service" / "workspace-discovery.json").read_text())
    assert "vscode_windows_native_fallback" in persisted["issues"]
    healthy = EffectiveWorkspaceCatalog(fixture.runtime, registry=registry, discovery=fixture.discovery(
        VSCodeStatusProbe("available", "vscode_status_available", "windows_user_install",
                          fixture.status((1, 2, 3))), registry=registry)).list_workspaces()
    # CLI recovery does not change the automatic workspace identity.
    assert sorted(item.workspace_id for item in healthy) == sorted(item.workspace_id for item in degraded)


def windows_adapter(tmp_path, installs=("user",)):
    environment = {"COMSPEC": str(tmp_path / "cmd.exe")}
    roots = {"user": ("LOCALAPPDATA", tmp_path / "Local" / "Programs"),
             "machine": ("ProgramFiles", tmp_path / "Program Files")}
    for install in installs:
        key, root = roots[install]
        code = root / "Microsoft VS Code" / "bin" / "code.cmd"
        code.parent.mkdir(parents=True)
        code.write_text("@echo off\n", encoding="utf-8")
        environment[key] = str(root.parent if install == "user" else root)
    return detect_platform_adapter(system_name="nt", machine="x64", home=tmp_path,
                                   environment=environment, which=lambda _command: None)


VALID_NO_HOSTS = "Version: Code\n" + STATUS_HEADER + "    0\t  100\t    50\tcode main\n"
VALID_HOSTS = VALID_NO_HOSTS + "    0\t  100\t  1000\textension-host [1]\n"


@pytest.mark.parametrize("stdout,stderr,code,status,reason", [
    ("\n", "", 0, "error", "vscode_status_empty"),
    (" \r\n\t\n", "", 0, "error", "vscode_status_empty"),
    ("unexpected diagnostic text\n", "", 0, "error", "vscode_status_malformed"),
    (VALID_NO_HOSTS, "", 0, "available", "vscode_status_no_extension_hosts"),
    (VALID_HOSTS, "", 0, "available", "vscode_status_available"),
    ("\n", "RangeError: Maximum call stack size exceeded at secret-frame", 0, "error",
     "vscode_status_formatter_failed"),
    (VALID_HOSTS, "RangeError: Maximum call stack size exceeded", 1, "error",
     "vscode_status_formatter_failed"),
    ("", "failure mentioning secret-environment", 1, "error", "vscode_status_failed"),
])
def test_windows_status_classification_is_compact_and_safe(tmp_path, stdout, stderr, code, status, reason):
    adapter = windows_adapter(tmp_path)
    probe = run_vscode_status(
        adapter, runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, code, stdout, stderr))

    assert (probe.status, probe.reason) == (status, reason)
    assert probe.output == (stdout if status == "available" else None)
    assert "secret" not in repr(probe)


def test_windows_status_timeout_is_not_retried_and_other_install_can_recover(tmp_path):
    adapter = windows_adapter(tmp_path, ("user", "machine"))
    calls = []

    def hung(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    probe = run_vscode_status(adapter, runner=hung)

    assert (probe.status, probe.reason, probe.output) == ("error", "vscode_status_timeout", None)
    assert len(calls) == 1
    outputs = iter(["\n", VALID_HOSTS])
    recovered = run_vscode_status(adapter, runner=lambda argv, **_kwargs: subprocess.CompletedProcess(
        argv, 0, next(outputs), ""))
    assert (recovered.status, recovered.source) == ("available", "windows_machine_install")


def test_posix_status_and_default_index_are_unchanged(tmp_path):
    code = tmp_path / "bin" / "code"
    code.parent.mkdir()
    code.write_text("#!/bin/sh\n", encoding="utf-8")
    for system in ("Linux", "Darwin"):
        adapter = detect_platform_adapter(
            system_name=system, machine="x86_64", home=tmp_path, environment={},
            which=lambda command: str(code) if command == "code" else None)
        probe = run_vscode_status(
            adapter, runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "\n", ""))
        # Pre-existing POSIX semantics are intentionally not reclassified.
        assert (probe.status, probe.output) == ("available", "\n")
        discovery = VSCodeWorkspaceDiscovery(
            tmp_path / system, codex_home=tmp_path / ".codex", user_data_root=tmp_path / "Code" / "User",
            platform_adapter=adapter)
        assert type(discovery.live_window_index) is VSCodeLiveWindowIndex
        assert discovery._windows_live_issues() == ()


def test_windows_default_index_is_selected_only_on_windows_hosts(tmp_path):
    adapter = windows_adapter(tmp_path)
    discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "runtime", codex_home=tmp_path / ".codex",
        user_data_root=tmp_path / "Code" / "User", platform_adapter=adapter)
    expected = WindowsVSCodeLiveWindowIndex if os.name == "nt" else VSCodeLiveWindowIndex
    assert type(discovery.live_window_index) is expected


def test_adapter_contains_no_literal_machine_or_session_identifiers():
    source = Path(win.__file__).read_text(encoding="utf-8")
    assert re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                     source) is None
    assert re.search(r"[A-Za-z]:\\\\", source) is None
    assert "Users" not in source
    # No account/email identifier; any other "@" may only open a decorator line.
    assert re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", source) is None
    for line in source.splitlines():
        if "@" in line:
            assert re.fullmatch(r"\s*@[A-Za-z_][A-Za-z0-9_.]*(?:\([^@]*\))?", line), line
    assert re.search(r"window[0-9]", source) is None


class FsMeter:
    """Instrument directory listing, file opens/bytes and SQL statements."""

    def __init__(self, monkeypatch) -> None:
        self.active = False
        self.listed, self.opened, self.reads, self.statements = [], [], [], []
        meter = self
        real_scandir, real_iterdir = os.scandir, Path.iterdir
        real_open, real_connect = Path.open, sqlite3.connect

        class Listing:
            def __init__(self, path) -> None:
                self._path, self._iterator = Path(path), real_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._iterator.close()
                return False

            def __iter__(self):
                for entry in self._iterator:
                    meter.listed.append(self._path)
                    yield entry

        class Handle:
            def __init__(self, path, handle) -> None:
                self._path, self._handle = path, handle

            def read(self, *args):
                data = self._handle.read(*args)
                meter.reads.append((self._path, len(data)))
                return data

            def __iter__(self):
                for line in self._handle:
                    meter.reads.append((self._path, len(line)))
                    yield line

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()
                return False

            def __getattr__(self, name):
                return getattr(self._handle, name)

        def scandir(path="."):
            return Listing(path) if meter.active else real_scandir(path)

        def iterdir(self):
            for child in real_iterdir(self):
                if meter.active:
                    meter.listed.append(Path(self))
                yield child

        def open_(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            if not meter.active:
                return handle
            meter.opened.append(Path(self))
            return Handle(Path(self), handle)

        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            if meter.active:
                connection.set_trace_callback(meter.statements.append)
            return connection

        monkeypatch.setattr(os, "scandir", scandir)
        monkeypatch.setattr(Path, "iterdir", iterdir)
        monkeypatch.setattr(Path, "open", open_)
        monkeypatch.setattr(sqlite3, "connect", connect)

    def run(self, call):
        self.listed, self.opened, self.reads, self.statements = [], [], [], []
        self.active = True
        try:
            return call()
        finally:
            self.active = False

    def bytes_from(self, path: Path) -> int:
        return sum(size for read, size in self.reads if read == path)

    def collector_bytes(self, code_root: Path) -> int:
        # Discovery itself reads globalStorage/storage.json outside the budget.
        return sum(size for path, size in self.reads
                   if code_root in path.parents and "globalStorage" not in path.parts)


def cost(meter: FsMeter, index) -> dict:
    budget = index.last_budget
    collection = index.last_collection_budget
    proof = index.collector.proof_budget or win.WorkBudget()
    return {
        "bytes": meter.collector_bytes(index.user_data_root.parent),
        "charged": budget.bytes_charged + collection.bytes_charged + proof.bytes_charged,
        "cache_bytes": budget.cache_charged + collection.cache_charged + proof.cache_charged,
        "entries": 3 * win.DIRECTORY_ENTRY_BUDGET - budget.entries - collection.entries - proof.entries,
        "sql": budget.sql_used + collection.sql_used + proof.sql_used,
    }


@windows_host
def test_large_unrelated_history_has_history_independent_cost(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    meter = FsMeter(monkeypatch)
    index = fixture.index()
    discovery = fixture.discovery(index=index)
    baseline = meter.run(discovery.snapshot)
    baseline_cost = cost(meter, index)
    assert_healthy(baseline, fixture, (1, 2, 3))

    logs = fixture.user.parent / "logs"
    old = fixture.base_ms // 1000 - 30 * 86400
    historical = []
    for day in range(150):
        session = logs / ("2000%04dT000000" % day)
        historical.append(session)
        for number in (1, 2):
            log = session / f"window{number}" / "exthost" / "exthost.log"
            log.parent.mkdir(parents=True)
            log.write_text("historical padding line\n" * 200
                           + f"Extension host with pid {fixture.windows[number]['host']} started\n"
                           + "loading workspaceStorage/historicalkey/state\n", encoding="utf-8")
            os.utime(log, (old, old))
    storage_root = fixture.user / "workspaceStorage"
    for count in range(1000):
        directory = storage_root / f"history{count}"
        directory.mkdir()
        (directory / "workspace.json").write_text(
            json.dumps({"folder": (tmp_path / f"old{count}").as_uri()}), encoding="utf-8")
    with sqlite3.connect(str(fixture.codex_home / "state_5.sqlite")) as connection:
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, 'vscode', 'user', 0)",
            [(str(uuid.uuid4()), str(fixture.windows[1]["repo"] if count % 2 else tmp_path / "old"))
             for count in range(20000)])

    large = meter.run(discovery.snapshot)
    large_cost = cost(meter, index)

    assert tracked(large) == tracked(baseline)
    # Bytes and SQL VM steps do not grow with history; only the capped
    # logs-root listing and the newest sessions' window listings do.
    assert large_cost["bytes"] == baseline_cost["bytes"]
    assert large_cost["charged"] == baseline_cost["charged"]
    assert large_cost["charged"] == large_cost["bytes"] + large_cost["cache_bytes"]
    # Exact-ID index lookups: only per-statement progress rounding may differ,
    # while a scan of the 20,000 added rows alone would far exceed this.
    assert large_cost["sql"] <= baseline_cost["sql"] + 16 * win.SQL_PROGRESS_STEPS
    assert large_cost["sql"] < 20000
    assert large_cost["entries"] == baseline_cost["entries"]
    historical = {session.resolve() for session in historical}
    newest = set(sorted(historical | {fixture.session_dir.resolve()}, key=lambda item: item.name,
                        reverse=True)[:win.LOG_SESSION_LIMIT])
    assert set(meter.listed) <= {logs.resolve()} | newest
    assert storage_root.resolve() not in meter.listed and storage_root not in meter.listed
    assert not any(path.parent.name.startswith("history") for path in meter.opened)
    assert not any(set(path.resolve().parents) & historical for path in meter.opened)
    thread_queries = [statement for statement in meter.statements if "FROM threads" in statement]
    assert thread_queries and all("FROM threads WHERE id = " in query for query in thread_queries)


def test_exact_thread_lookup_uses_the_primary_key_index(tmp_path):
    fixture = Fixture(tmp_path)
    with sqlite3.connect(str(fixture.codex_home / "state_5.sqlite")) as connection:
        plan = connection.execute("EXPLAIN QUERY PLAN " + win.THREAD_LOOKUP,
                                  (str(uuid.uuid4()),)).fetchall()
        cache = fixture.user / "workspaceStorage" / fixture.windows[1]["key"] / "state.vscdb"
    with sqlite3.connect(str(cache)) as connection:
        cache_plan = connection.execute("EXPLAIN QUERY PLAN " + win.CACHE_SIZE_LOOKUP,
                                        ("agentSessions.model.cache",)).fetchall()
    for rows in (plan, cache_plan):
        detail = " ".join(str(row[-1]) for row in rows)
        assert "SEARCH" in detail and "SCAN" not in detail


@windows_host
def test_unindexed_thread_history_is_interrupted_by_the_shared_step_budget(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    with sqlite3.connect(str(fixture.codex_home / "state_5.sqlite")) as connection:
        connection.execute("ALTER TABLE threads RENAME TO indexed_threads")
        connection.execute(
            "CREATE TABLE threads (id TEXT NOT NULL, cwd TEXT NOT NULL, source TEXT NOT NULL, "
            "thread_source TEXT, archived INTEGER NOT NULL)")
        connection.execute("INSERT INTO threads SELECT * FROM indexed_threads")
        connection.executemany("INSERT INTO threads VALUES (?, ?, 'vscode', 'user', 0)",
                               [(str(uuid.uuid4()), str(tmp_path / "old")) for _ in range(50000)])
    monkeypatch.setattr(win, "SQL_STEP_BUDGET", 20000)
    index = fixture.index()

    snapshot = fixture.discovery(index=index).snapshot()

    assert tracked(snapshot) == {}
    assert "windows_native_budget_deferred" in snapshot.issues
    assert index.last_budget.sql_used <= 20000 + 2 * win.SQL_PROGRESS_STEPS


def test_reads_charge_every_attempted_byte_and_stop_once_exhausted(tmp_path):
    collector = win.WindowsNativeCollector(tmp_path)
    budget = win.WorkBudget()
    budget.bytes = 100
    large = tmp_path / "large.log"
    large.write_bytes(b"x\n" * 200)
    small = tmp_path / "small.log"
    small.write_bytes(b"ok\n")

    assert collector.read_limited(large, 1000, budget) is None
    assert (budget.bytes, budget.bytes_charged, budget.reason) == (-1, 101, "budget_exceeded")
    # The allowance is shared: it is not reset for the next candidate.
    assert collector.read_limited(small, 1000, budget) is None
    assert budget.bytes_charged == 101

    fresh = win.WorkBudget()
    assert collector.read_limited(large, 10, fresh) is None  # Oversized: refused unread.
    assert fresh.bytes_charged == 0 and fresh.reason is None
    assert collector.read_limited(small, 10, fresh) == b"ok\n"
    assert collector.refusals["metadata_oversized"] == 1


@windows_host
def test_oversized_log_is_refused_unread_and_later_window_still_maps(tmp_path, monkeypatch):
    monkeypatch.setattr(win, "EXTHOST_LOG_BYTES", 65536)
    fixture = Fixture(tmp_path)
    fixture.write_exthost(1, prefix="unrelated padding line\n" * 4000)
    meter = FsMeter(monkeypatch)
    index = fixture.index()

    snapshot = meter.run(fixture.discovery(index=index).snapshot)

    assert not tracked(snapshot)
    assert "windows_native_log_domain_incomplete" in snapshot.issues
    refused = fixture.windows[1]["exthost_log"].resolve()
    assert refused in meter.opened  # Opened for fstat only, then refused unread.
    assert meter.bytes_from(refused) == 0
    assert index.last_budget.reason is None
    # One shared budget charged every byte of collection, resolution and admission.
    assert index.last_collection_budget.bytes_charged == meter.collector_bytes(index.user_data_root.parent) == 0


@windows_host
def test_deadline_stops_scanning_and_reads_then_rotation_recovers(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    now = [1000.0]
    index = WindowsVSCodeLiveWindowIndex(
        fixture.user, status_probe=lambda: VSCodeStatusProbe("error", "vscode_status_empty"),
        reader=fixture.reader, log_time_ms=utc_ms, clock=lambda: now[0])
    first_host = fixture.windows[1]["host"]

    def expire(call, value):
        if call == 0:
            now[0] += 60  # The deadline passes during the very first host query.
        return value

    fixture.reader.id_hooks[first_host] = expire
    meter = FsMeter(monkeypatch)
    discovery = fixture.discovery(index=index)

    expired = meter.run(discovery.snapshot)

    assert expired.status == "error"
    assert "windows_native_deadline_exceeded" in expired.issues
    # No directory scan and no log/storage read after the deadline.
    assert meter.listed == []
    assert not [path for path in meter.opened
                if "logs" in path.parts or "workspaceStorage" in path.parts]
    assert fixture.reader.id_calls.get(fixture.windows[2]["host"]) is None

    recovered = discovery.snapshot()
    assert_healthy(recovered, fixture, (1, 2, 3))


def _final_race(fixture, case):
    item = fixture.windows[1]
    competitor = str(uuid.uuid4())
    if case in ("competing_active_owner", "owner_switched_away"):
        fixture.add_thread(1, competitor)
        fixture.reader.held.add(competitor)

    def append(path, text):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def race(call, value):
        # Runs inside the first final writer check, after the initial uniqueness
        # test; the old host/app processes and the held lock stay alive.
        if call != 0:
            return value
        later = item["app_ms"] + 20
        if case == "replacement_host_generation":
            append(item["exthost_log"],
                   f"{stamp(later)} [info] Extension host with pid {item['host'] + 3} started\n"
                   f"{stamp(later + 1)} [info] loading workspaceStorage/{item['key']}/state\n")
        elif case == "workspace_switched":
            append(item["exthost_log"],
                   f"{stamp(later)} [info] loading workspaceStorage/otherstorage/state\n")
        elif case == "replacement_app_generation":
            append(item["codex_log"],
                   f"{stamp(later)} [info] [CodexMcpConnection] Spawning codex app-server\n")
        elif case == "replacement_app_process":
            fixture.reader.rows[item["app"] + 2] = WindowsProcessEntry(
                item["app"] + 2, item["host"], "codex.exe")
        elif case == "competing_active_owner":
            append(item["codex_log"],
                   f"{stamp(later)} [info] thread_stream_role_changed "
                   f"conversationId={competitor} role=owner\n"
                   f"{stamp(later + 1)} [info] thread_stream_view_activity_changed "
                   f"conversationId={competitor} active=true\n")
        elif case == "owner_switched_away":
            append(item["codex_log"],
                   f"{stamp(later)} [info] thread_stream_view_activity_changed "
                   f"conversationId={item['session']} active=false\n"
                   f"{stamp(later + 1)} [info] thread_stream_role_changed "
                   f"conversationId={competitor} role=owner\n"
                   f"{stamp(later + 2)} [info] thread_stream_view_activity_changed "
                   f"conversationId={competitor} active=true\n")
        else:
            raise AssertionError(case)
        return value

    fixture.reader.writer_hooks[item["session"]] = race


@windows_host
@pytest.mark.parametrize("case,reason", [
    ("replacement_host_generation", "windows_native_stale_host_generation"),
    ("workspace_switched", "windows_native_stale_host_generation"),
    ("replacement_app_generation", "windows_native_stale_app_server_generation"),
    ("replacement_app_process", "windows_native_app_generation_changed"),
    ("competing_active_owner", "windows_native_selection_changed"),
    ("owner_switched_away", "windows_native_selection_changed"),
])
def test_final_admission_rejects_changes_while_old_processes_and_lock_stay_alive(
        tmp_path, case, reason):
    fixture = Fixture(tmp_path)
    _final_race(fixture, case)

    snapshot = fixture.discovery().snapshot()

    [window] = window_for(snapshot, fixture, 1)
    assert (window.tracking_status, window.reason) == ("unresolved", reason)
    assert fixture.reader.writer_calls[fixture.windows[1]["session"]] == 1
    assert fixture.windows[1]["session"] not in {
        item.session_id for item in snapshot.effective_workspaces}
    assert_healthy(snapshot, fixture)


class RecordingIndex(VSCodeLiveWindowIndex):
    """A custom index exposing every Windows hook name; records any dispatch."""

    def __init__(self, user, live) -> None:
        super().__init__(user, status_runner=lambda: None, native_runner=lambda: ())
        self.live, self.calls = live, []

    def snapshot(self):
        return dict(self.live)

    def windows_discovery_issues(self):
        self.calls.append("issues")
        return ("windows_hook_issue",)

    def windows_storage_keys(self, live_windows, sleep):
        self.calls.append("storage")
        return {}

    def windows_resolve(self, *args, **kwargs):
        self.calls.append("resolve")
        return SessionResolution("unresolved", None, None, "windows_hook_resolution")

    def windows_fallback_admission(self, *args, **kwargs):
        self.calls.append("admit")
        return SessionResolution("unresolved", None, None, "windows_hook_admission")


class PosixOs:
    name = "posix"

    def __getattr__(self, attribute):
        return getattr(os, attribute)


@pytest.mark.parametrize("host", ["posix_adapter", "posix_os_name"])
def test_windows_hooks_never_dispatch_off_windows_even_for_custom_index(tmp_path, monkeypatch, host):
    fixture = Fixture(tmp_path)
    item = fixture.windows[1]
    index = RecordingIndex(fixture.user, {item["key"]: LiveVSCodeWindow(
        item["number"], item["host"], item["app"], item["codex_log"])})
    if host == "posix_adapter":
        adapter = detect_platform_adapter(system_name="Linux", machine="x86_64", home=tmp_path,
                                          environment={}, which=lambda _command: None)
    else:
        adapter = windows_adapter(tmp_path)
        monkeypatch.setattr(workspace_discovery, "os", PosixOs())
    discovery = VSCodeWorkspaceDiscovery(
        fixture.runtime, codex_home=fixture.codex_home, user_data_root=fixture.user,
        registry=WorkspaceRegistry(fixture.runtime), session_resolver=fixture.resolver(),
        live_window_index=index, platform_adapter=adapter,
        git_root_resolver=lambda path: path, sleep=lambda _seconds: None)

    snapshot = discovery.snapshot()

    assert index.calls == []
    assert "windows_hook_issue" not in snapshot.issues
    # The unchanged shared storage scan and resolver handle the window.
    assert tracked(snapshot)["repo1"] == (item["session"], "codex_state_vscode_window_cache_owner")


@windows_host
def test_windows_hooks_dispatch_only_with_both_windows_guards(tmp_path):
    fixture = Fixture(tmp_path)
    item = fixture.windows[1]
    index = RecordingIndex(fixture.user, {item["key"]: LiveVSCodeWindow(
        item["number"], item["host"], item["app"], item["codex_log"])})
    discovery = VSCodeWorkspaceDiscovery(
        fixture.runtime, codex_home=fixture.codex_home, user_data_root=fixture.user,
        registry=WorkspaceRegistry(fixture.runtime), session_resolver=fixture.resolver(),
        live_window_index=index, platform_adapter=windows_adapter(tmp_path),
        git_root_resolver=lambda path: path, sleep=lambda _seconds: None)

    snapshot = discovery.snapshot()

    assert "storage" in index.calls and "issues" in index.calls
    assert "windows_hook_issue" in snapshot.issues
    assert "repo1" not in tracked(snapshot)


class FakePipe:
    def __init__(self, chunks=(), *, endless=None, gate=None) -> None:
        self.chunks, self.endless, self.gate = list(chunks), endless, gate
        self.reads = 0
        self.closed = False

    def read(self, _size):
        self.reads += 1
        if self.chunks:
            return self.chunks.pop(0)
        if self.endless is not None:
            return self.endless
        if self.gate is not None:
            self.gate.wait(10)
        return b""

    def close(self):
        self.closed = True


class FakeChild:
    def __init__(self, stdout, stderr, *, code=0, running=False) -> None:
        self.stdout, self.stderr, self.code, self.running = stdout, stderr, code, running
        self.killed = 0

    def poll(self):
        return None if self.running else self.code

    def wait(self, timeout=None):
        if self.running:
            raise subprocess.TimeoutExpired("diagnostic", timeout)
        return self.code

    def kill(self):
        self.killed += 1
        self.running = False


def default_runner_child(monkeypatch, child):
    created = []

    def popen(argv, **kwargs):
        created.append((argv, kwargs))
        return child

    monkeypatch.setattr(platform_adapters.subprocess, "Popen", popen)
    return created


def test_windows_default_status_runner_bounds_output_while_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_adapters, "_WINDOWS_STATUS_BYTES", 4096)
    stdout = FakePipe(endless=b"x" * 1024)
    child = FakeChild(stdout, FakePipe(), running=True)
    created = default_runner_child(monkeypatch, child)

    probe = run_vscode_status(windows_adapter(tmp_path))

    assert (probe.status, probe.reason, probe.output) == ("error", "vscode_status_oversized", None)
    assert stdout.reads <= 4096 // 1024 + 2
    assert child.killed == 1 and stdout.closed
    [(argv, kwargs)] = created
    assert kwargs["stdout"] == subprocess.PIPE and "capture_output" not in kwargs
    assert argv[-1] == "--status"


def test_windows_default_status_runner_timeout_stops_only_owned_child(tmp_path, monkeypatch):
    gate = threading.Event()
    stdout = FakePipe(gate=gate)
    child = FakeChild(stdout, FakePipe(), running=True)
    created = default_runner_child(monkeypatch, child)
    try:
        probe = run_vscode_status(windows_adapter(tmp_path, ("user", "machine")), timeout_seconds=0.2)

        assert (probe.status, probe.reason, probe.output) == ("error", "vscode_status_timeout", None)
        assert child.killed == 1 and len(created) == 1
        assert not stdout.closed  # Never closed under a still-blocked reader.
    finally:
        gate.set()
        for reader in platform_adapters._WINDOWS_PROBE_READERS:
            reader.join(1)


@pytest.mark.parametrize("stdout,stderr,status,reason", [
    (VALID_HOSTS.replace("\n", "\r\n"), "", "available", "vscode_status_available"),
    (VALID_NO_HOSTS, "", "available", "vscode_status_no_extension_hosts"),
    (" \r\n", "", "error", "vscode_status_empty"),
    ("\n", "RangeError: Maximum call stack size exceeded", "error", "vscode_status_formatter_failed"),
])
def test_windows_default_status_runner_keeps_classification(tmp_path, monkeypatch,
                                                            stdout, stderr, status, reason):
    child = FakeChild(FakePipe([stdout.encode("utf-8")]), FakePipe([stderr.encode("utf-8")]))
    default_runner_child(monkeypatch, child)

    probe = run_vscode_status(windows_adapter(tmp_path))

    assert (probe.status, probe.reason) == (status, reason)
    assert probe.output == (stdout.replace("\r\n", "\n") if status == "available" else None)
    assert child.killed == 0
