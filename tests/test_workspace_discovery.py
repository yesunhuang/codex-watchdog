from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Iterable, List, Tuple

from codex_watchdog.workspace_discovery import (
    CodexSessionResolver,
    EffectiveWorkspaceCatalog,
    LiveVSCodeWindow,
    SessionResolution,
    VSCodeLiveWindowIndex,
    VSCodeWorkspaceDiscovery,
    codex_log_owns_session,
)
from codex_watchdog.workspace_registry import WorkspaceRegistry


SESSION_CURRENT = "11111111-2222-4333-8444-555555555555"
SESSION_OLD = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SESSION_OTHER = "12345678-1234-4234-8234-123456789abc"


class GitRootResolver:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.calls: List[Path] = []

    def __call__(self, path: Path) -> Path:
        self.calls.append(path)
        return self.repo_root


class FakeLiveWindowIndex:
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root

    def snapshot(self):
        return {
            path.name: LiveVSCodeWindow(
                path.name, 100, 200, self.storage_root.parent / "Codex.log",
            )
            for path in self.storage_root.iterdir()
            if path.is_dir()
        }


def write_windows_state(path: Path, entries: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"windowsState": {"openedWindows": list(entries)}}),
        encoding="utf-8",
    )


def write_workspace(storage_root: Path, storage_id: str, uri: str) -> None:
    workspace = storage_root / storage_id
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text(
        json.dumps({"folder": uri}), encoding="utf-8"
    )


def write_threads(
    codex_home: Path, rows: Iterable[Tuple[str, str, str, str, int]]
) -> None:
    rows = list(rows)
    codex_home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(codex_home / "state_5.sqlite")) as connection:
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT NOT NULL, cwd TEXT NOT NULL, source TEXT NOT NULL, "
            "thread_source TEXT, archived INTEGER NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO threads "
            "(id, cwd, source, thread_source, archived) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    storage_root = codex_home.parent / "Code" / "User" / "workspaceStorage"
    if storage_root.is_dir():
        resources = [
            {
                "providerType": "openai-codex",
                "resource": f"openai-codex://route/local/{session_id}",
            }
            for session_id, _cwd, _source, _thread_source, _archived in rows
        ]
        for workspace_storage in storage_root.iterdir():
            if not workspace_storage.is_dir():
                continue
            with sqlite3.connect(str(workspace_storage / "state.vscdb")) as connection:
                connection.execute(
                    "CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)"
                )
                connection.execute(
                    "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                    ("agentSessions.model.cache", json.dumps(resources)),
                )


def make_discovery(
    tmp_path: Path,
    git_root_resolver,
    lock_probe,
    *,
    registry: WorkspaceRegistry = None,
    exclude=(),
) -> VSCodeWorkspaceDiscovery:
    runtime = tmp_path / "runtime"
    codex_home = tmp_path / ".codex"
    selected_registry = registry or WorkspaceRegistry(runtime)
    user_data_root = tmp_path / "Code" / "User"
    session_resolver = CodexSessionResolver(
        runtime,
        codex_home,
        lock_probe=lock_probe,
        owner_probe=lambda _path, _session_id: True,
    )
    return VSCodeWorkspaceDiscovery(
        runtime,
        codex_home=codex_home,
        user_data_root=user_data_root,
        registry=selected_registry,
        session_resolver=session_resolver,
        live_window_index=FakeLiveWindowIndex(user_data_root / "workspaceStorage"),
        git_root_resolver=git_root_resolver,
        exclude=exclude,
        sleep=lambda _seconds: None,
    )


def test_resolves_exact_open_local_window_without_recency_guess(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    storage_id = "0123456789abcdef0123456789abcdef"
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", storage_id, uri)
    write_threads(
        tmp_path / ".codex",
        [
            (SESSION_OLD, str(repo), "vscode", "user", 0),
            (SESSION_CURRENT, str(repo), "vscode", "user", 0),
            (SESSION_OTHER, str(repo), '{"subagent":{}}', "subagent", 0),
        ],
    )
    git = GitRootResolver(repo)
    discovery = make_discovery(tmp_path, git, lambda path: path.stem == SESSION_CURRENT)

    snapshot = discovery.snapshot()

    assert snapshot.status == "ok"
    assert len(snapshot.effective_workspaces) == 1
    workspace = snapshot.effective_workspaces[0]
    assert workspace.repo_root == repo
    assert workspace.session_id == SESSION_CURRENT
    assert workspace.workspace_id.startswith("vscode-")
    assert snapshot.windows[0].workspace_storage_key == storage_id
    assert snapshot.windows[0].tracking_status == "tracked"
    assert snapshot.windows[0].session_source == "codex_state_vscode_window_cache_owner"
    assert git.calls == [repo]
    assert snapshot.to_dict()["schema_version"] == 1


def test_two_held_user_threads_for_same_exact_cwd_fail_closed(tmp_path: Path,) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)
    write_threads(
        tmp_path / ".codex",
        [
            (SESSION_OLD, str(repo), "vscode", "user", 0),
            (SESSION_CURRENT, str(repo), "vscode", "user", 0),
        ],
    )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda _path: True
    ).snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].tracking_status == "unresolved"
    assert snapshot.windows[0].reason == "ambiguous_loaded_threads"
    assert snapshot.windows[0].session_id is None


def test_query_filters_archived_non_vscode_subagent_and_other_cwd_threads(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    other = (tmp_path / "other").resolve()
    repo.mkdir()
    other.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)
    write_threads(
        tmp_path / ".codex",
        [
            (SESSION_CURRENT, str(repo), "vscode", "user", 0),
            (SESSION_OLD, str(repo), "vscode", "user", 1),
            (SESSION_OTHER, str(repo), "cli", "user", 0),
            (
                "87654321-4321-4321-8321-cba987654321",
                str(repo),
                "vscode",
                "subagent",
                0,
            ),
            ("fedcba98-7654-4321-8765-abcdef123456", str(other), "vscode", "user", 0,),
        ],
    )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda path: path.stem == SESSION_CURRENT,
    ).snapshot()

    assert [item.session_id for item in snapshot.effective_workspaces] == [
        SESSION_CURRENT
    ]


def test_remote_window_becomes_adapter_candidate_without_local_git_or_session(
    tmp_path: Path,
) -> None:
    uri = "vscode-remote://ssh-remote%2Bexample.invalid/home/user/repo"
    user_data = tmp_path / "Code" / "User"
    write_windows_state(
        user_data / "globalStorage" / "storage.json",
        [{"folder": uri, "remoteAuthority": "ssh-remote+example.invalid"}],
    )
    write_workspace(user_data / "workspaceStorage", "remote-key", uri)

    def unexpected_git(_path):
        raise AssertionError("remote discovery must not invoke local Git")

    def unexpected_lock(_path):
        raise AssertionError("remote discovery must not inspect local Codex locks")

    snapshot = make_discovery(tmp_path, unexpected_git, unexpected_lock).snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].workspace_storage_key == "remote-key"
    assert snapshot.windows[0].locality == "remote_ssh"
    assert snapshot.status == "ok"
    assert snapshot.windows[0].tracking_status == "remote_adapter"
    assert snapshot.windows[0].reason is None
    assert snapshot.to_dict()["remote_workspace_count"] == 1


def test_remote_window_can_be_excluded_by_exact_repository_name(tmp_path: Path) -> None:
    uri = "vscode-remote://ssh-remote%2Bexample.invalid/home/user/repo"
    user_data = tmp_path / "Code" / "User"
    write_windows_state(
        user_data / "globalStorage" / "storage.json",
        [{"folder": uri, "remoteAuthority": "ssh-remote+example.invalid"}],
    )
    write_workspace(user_data / "workspaceStorage", "remote-key", uri)

    snapshot = make_discovery(
        tmp_path, lambda _path: None, lambda _path: False, exclude=("repo",),
    ).snapshot()

    assert snapshot.windows[0].locality == "remote_ssh"
    assert snapshot.windows[0].tracking_status == "excluded"
    assert snapshot.windows[0].reason == "tracking_excluded"
    assert snapshot.to_dict()["remote_workspace_count"] == 0


def test_remote_window_carries_single_local_session_claim_for_remote_verification(
    tmp_path: Path,
) -> None:
    remote_path = "/home/user/repo"
    uri = "vscode-remote://ssh-remote%2Bexample.invalid" + remote_path
    user_data = tmp_path / "Code" / "User"
    write_windows_state(
        user_data / "globalStorage" / "storage.json",
        [{"folder": uri, "remoteAuthority": "ssh-remote+example.invalid"}],
    )
    write_workspace(user_data / "workspaceStorage", "remote-key", uri)
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, remote_path, "vscode", "user", 0)],
    )

    def unexpected_git(_path):
        raise AssertionError("remote discovery must not invoke local Git")

    def unexpected_lock(_path):
        raise AssertionError("a remote claim must not use a local writer lock")

    snapshot = make_discovery(tmp_path, unexpected_git, unexpected_lock).snapshot()

    assert snapshot.windows[0].tracking_status == "remote_adapter"
    assert snapshot.windows[0].session_id == SESSION_CURRENT
    assert snapshot.windows[0].session_source == "vscode_window_cache_remote_claim"
    assert snapshot.windows[0].session_candidates == (SESSION_CURRENT,)


def test_manual_registration_overrides_open_repo_and_closed_manual_remains(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    open_repo = (tmp_path / "open-repo").resolve()
    closed_repo = (tmp_path / "closed-repo").resolve()
    open_repo.mkdir()
    closed_repo.mkdir()
    registry = WorkspaceRegistry(runtime)
    open_manual = registry.add("manual-open", open_repo, SESSION_OLD).workspace
    closed_manual = registry.add("manual-closed", closed_repo, SESSION_OTHER).workspace
    uri = open_repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)

    def unexpected_lock(_path):
        raise AssertionError("manual override must skip automatic session resolution")

    discovery = make_discovery(
        tmp_path, GitRootResolver(open_repo), unexpected_lock, registry=registry,
    )
    snapshot = discovery.snapshot()

    assert snapshot.effective_workspaces == tuple(
        sorted((closed_manual, open_manual), key=lambda item: item.workspace_id)
    )
    assert snapshot.windows[0].source == "manual_override"
    assert snapshot.windows[0].workspace_id == "manual-open"
    assert snapshot.windows[0].session_source == "manual_registry"


def test_duplicate_workspace_storage_mapping_fails_before_git(tmp_path: Path,) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "one", uri)
    write_workspace(user_data / "workspaceStorage", "two", uri)

    def unexpected_git(_path):
        raise AssertionError("Git must not run before exact storage mapping")

    snapshot = make_discovery(tmp_path, unexpected_git, lambda _path: False).snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].tracking_status == "unresolved"
    assert snapshot.windows[0].reason == "workspace_storage_ambiguous"


def test_duplicate_historical_storage_with_one_live_key_resolves(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "live-key", uri)
    write_workspace(user_data / "workspaceStorage", "historical-key", uri)
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, str(repo), "vscode", "user", 0)]
    )

    class OneLiveWindow:
        @staticmethod
        def snapshot():
            return {"live-key": LiveVSCodeWindow("4", 100, 200, tmp_path / "Codex.log")}

    resolver = CodexSessionResolver(
        tmp_path / "runtime",
        tmp_path / ".codex",
        lock_probe=lambda path: path.stem == SESSION_CURRENT,
        owner_probe=lambda _path, _session: True,
    )
    discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "runtime",
        codex_home=tmp_path / ".codex",
        user_data_root=user_data,
        session_resolver=resolver,
        live_window_index=OneLiveWindow(),
        git_root_resolver=GitRootResolver(repo),
        sleep=lambda _seconds: None,
    )

    snapshot = discovery.snapshot()

    assert len(snapshot.windows) == 1
    assert snapshot.windows[0].workspace_storage_key == "live-key"
    assert [item.session_id for item in snapshot.effective_workspaces] == [
        SESSION_CURRENT
    ]


def test_stale_last_active_with_missing_storage_is_not_counted_as_live(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    stale_uri = (tmp_path / "closed").resolve().as_uri()
    user_data = tmp_path / "Code" / "User"
    state = user_data / "globalStorage" / "storage.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "windowsState": {
                    "openedWindows": [{"folder": uri}],
                    "lastActiveWindow": {"folder": stale_uri},
                }
            }
        ),
        encoding="utf-8",
    )
    write_workspace(user_data / "workspaceStorage", "live-key", uri)
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, str(repo), "vscode", "user", 0)]
    )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda path: path.stem == SESSION_CURRENT
    ).snapshot()

    assert len(snapshot.windows) == 1
    assert snapshot.windows[0].workspace_uri == uri
    assert snapshot.issues == ("workspace_storage_missing",)


def test_live_storage_key_absent_from_window_state_is_reported(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "live-key", uri)
    write_workspace(
        user_data / "workspaceStorage",
        "unmatched-live-key",
        (tmp_path / "unlisted").resolve().as_uri(),
    )
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, str(repo), "vscode", "user", 0)]
    )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda path: path.stem == SESSION_CURRENT
    ).snapshot()

    assert len(snapshot.windows) == 1
    assert snapshot.status == "partial"
    assert snapshot.issues == ("vscode_live_window_state_unmatched",)


def test_malformed_window_state_keeps_manual_entries_and_safe_issue(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    registry = WorkspaceRegistry(runtime)
    registered = registry.add("manual", repo, SESSION_CURRENT).workspace
    state = tmp_path / "Code" / "User" / "globalStorage" / "storage.json"
    state.parent.mkdir(parents=True)
    state.write_text("{not-json", encoding="utf-8")
    discovery = make_discovery(
        tmp_path,
        lambda _path: (_ for _ in ()).throw(AssertionError("Git must not run")),
        lambda _path: False,
        registry=registry,
    )

    snapshot = discovery.snapshot()

    assert snapshot.status == "error"
    assert snapshot.effective_workspaces == (registered,)
    assert snapshot.windows == ()
    assert snapshot.issues == ("vscode_window_state_unavailable",)
    catalog = EffectiveWorkspaceCatalog(runtime, registry=registry, discovery=discovery)
    assert catalog.is_current(registered) is True


def test_discovery_uses_codex_home_environment_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    codex_home = (tmp_path / "custom-codex-home").resolve()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "runtime",
        user_data_root=tmp_path / "Code" / "User",
        sleep=lambda _seconds: None,
    )

    assert discovery.codex_home == codex_home


def test_effective_catalog_is_registry_compatible_and_persists_safe_snapshot(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, str(repo), "vscode", "user", 0)],
    )
    runtime = tmp_path / "runtime"
    discovery = make_discovery(
        tmp_path, GitRootResolver(repo), lambda path: path.stem == SESSION_CURRENT,
    )
    catalog = EffectiveWorkspaceCatalog(runtime, discovery=discovery)

    workspaces = catalog.list_workspaces()

    assert [workspace.session_id for workspace in workspaces] == [SESSION_CURRENT]
    assert catalog.last_snapshot is not None
    durable = json.loads(catalog.path.read_text(encoding="utf-8"))
    assert durable == catalog.last_snapshot.to_dict()
    assert "prompt" not in json.dumps(durable).casefold()


def test_no_held_exact_user_thread_is_not_resolved(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, str(repo), "vscode", "user", 0)],
    )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda _path: False
    ).snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].reason == "no_loaded_vscode_thread"


def test_catalog_refreshes_open_windows_and_keeps_automatic_id_stable(
    tmp_path: Path,
) -> None:
    first_repo = (tmp_path / "first").resolve()
    second_repo = (tmp_path / "second").resolve()
    first_repo.mkdir()
    second_repo.mkdir()
    first_uri = first_repo.as_uri()
    second_uri = second_repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    state_path = user_data / "globalStorage" / "storage.json"
    write_workspace(user_data / "workspaceStorage", "first-key", first_uri)
    write_workspace(user_data / "workspaceStorage", "second-key", second_uri)
    write_threads(
        tmp_path / ".codex",
        [
            (SESSION_CURRENT, str(first_repo), "vscode", "user", 0),
            (SESSION_OTHER, str(second_repo), "vscode", "user", 0),
        ],
    )
    discovery = make_discovery(
        tmp_path,
        lambda path: path.resolve(),
        lambda path: path.stem in (SESSION_CURRENT, SESSION_OTHER),
    )
    catalog = EffectiveWorkspaceCatalog(tmp_path / "runtime", discovery=discovery)

    write_windows_state(state_path, [{"folder": first_uri}])
    first = catalog.list_workspaces()
    first_again = catalog.list_workspaces()
    write_windows_state(state_path, [{"folder": second_uri}])
    second = catalog.list_workspaces()

    assert [item.repo_root for item in first] == [first_repo]
    assert first_again[0].workspace_id == first[0].workspace_id
    assert [item.repo_root for item in second] == [second_repo]
    assert second[0].workspace_id != first[0].workspace_id


def test_last_active_window_is_deduplicated_from_opened_windows(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    state = user_data / "globalStorage" / "storage.json"
    state.parent.mkdir(parents=True)
    entry = {"folder": uri}
    state.write_text(
        json.dumps(
            {"windowsState": {"openedWindows": [entry], "lastActiveWindow": entry,}}
        ),
        encoding="utf-8",
    )
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, str(repo), "vscode", "user", 0)]
    )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda path: path.stem == SESSION_CURRENT,
    ).snapshot()

    assert len(snapshot.windows) == 1
    assert len(snapshot.effective_workspaces) == 1


def test_same_repo_with_distinct_window_sessions_fails_closed(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    first = repo / "first"
    second = repo / "second"
    first.mkdir(parents=True)
    second.mkdir()
    first_uri = first.as_uri()
    second_uri = second.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(
        user_data / "globalStorage" / "storage.json",
        [{"folder": first_uri}, {"folder": second_uri}],
    )
    write_workspace(user_data / "workspaceStorage", "first-key", first_uri)
    write_workspace(user_data / "workspaceStorage", "second-key", second_uri)

    class WindowSessionResolver:
        @staticmethod
        def resolve(paths, *, codex_log, window_state_database):
            assert codex_log is not None
            assert window_state_database is not None
            local_path = tuple(paths)[0]
            session_id = SESSION_CURRENT if local_path == first else SESSION_OTHER
            return SessionResolution("resolved", session_id, "test_writer_lock", None)

    discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "runtime",
        codex_home=tmp_path / ".codex",
        user_data_root=user_data,
        session_resolver=WindowSessionResolver(),
        live_window_index=FakeLiveWindowIndex(user_data / "workspaceStorage"),
        git_root_resolver=lambda _path: repo,
        sleep=lambda _seconds: None,
    )

    snapshot = discovery.snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.issues == ("ambiguous_repo_ownership",)
    assert {window.reason for window in snapshot.windows} == {
        "ambiguous_repo_ownership"
    }


def test_exact_repository_name_can_be_excluded(tmp_path: Path) -> None:
    repo = (tmp_path / "scratch").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)

    discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "runtime",
        codex_home=tmp_path / ".codex",
        user_data_root=user_data,
        live_window_index=FakeLiveWindowIndex(user_data / "workspaceStorage"),
        git_root_resolver=GitRootResolver(repo),
        exclude=("scratch",),
        sleep=lambda _seconds: None,
    )

    snapshot = discovery.snapshot()

    assert snapshot.status == "ok"
    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].tracking_status == "excluded"
    assert snapshot.windows[0].reason == "tracking_excluded"


def test_status_parser_binds_extension_hosts_to_vscode_codex_children() -> None:
    status = "\n".join(
        (
            "CPU %\tMem MB\tPID\tProcess",
            "    0\t100\t1000\textension-host [4]",
            "    0\t100\t1001\t     c:\\Users\\u\\.vscode\\extensions\\openai.chatgpt-1-win32-x64\\bin\\windows-x86_64\\codex.exe -c x app-server --analytics-default-enabled",
            "    0\t100\t2000\textension-host [5]",
            "    0\t100\t3000\twindow [4] (repo)",
        )
    )

    assert VSCodeLiveWindowIndex._parse_status(status) == {
        "4": (1000, 1001),
        "5": (2000, None),
    }


def test_live_window_log_requires_latest_exact_extension_host_pid(
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "Code" / "User"
    exthost = user_data.parent / "logs" / "20260901T100000" / "window4" / "exthost"
    exthost.mkdir(parents=True)
    (exthost / "exthost.log").write_text(
        "Extension host with pid 999 started\n"
        "loading workspaceStorage/storage-key/extension-state\n"
        "Extension host with pid 1000 started\n",
        encoding="utf-8",
    )
    codex_log = exthost / "openai.chatgpt" / "Codex.log"
    codex_log.parent.mkdir()
    codex_log.write_text("privacy-safe lifecycle log\n", encoding="utf-8")
    status = (
        "CPU %\tMem MB\tPID\tProcess\n"
        "0\t100\t1000\textension-host [4]\n"
        "0\t100\t1001\t     c:\\Users\\u\\.vscode\\extensions\\"
        "openai.chatgpt-1-win32-x64\\codex.exe app-server\n"
    )

    snapshot = VSCodeLiveWindowIndex(user_data, status_runner=lambda: status).snapshot()

    assert snapshot == {"storage-key": LiveVSCodeWindow("4", 1000, 1001, codex_log)}


def test_newest_window_log_pid_mismatch_does_not_fall_back_to_stale_log(
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "Code" / "User"
    logs = user_data.parent / "logs"
    for session, host_pid, storage_key in (
        ("20260901T100000", 999, "new-stale-key"),
        ("20260831T100000", 1000, "old-reused-pid-key"),
    ):
        exthost = logs / session / "window4" / "exthost"
        exthost.mkdir(parents=True)
        (exthost / "exthost.log").write_text(
            f"Extension host with pid {host_pid} started\n"
            f"loading workspaceStorage/{storage_key}/extension-state\n",
            encoding="utf-8",
        )
    status = "CPU %\tMem MB\tPID\tProcess\n0\t100\t1000\textension-host [4]\n"

    snapshot = VSCodeLiveWindowIndex(user_data, status_runner=lambda: status).snapshot()

    assert snapshot == {}


def test_replacement_host_survives_paired_old_host_shutdown(tmp_path: Path) -> None:
    user_data = tmp_path / "Code" / "User"
    exthost = user_data.parent / "logs" / "20260901T100000" / "window7" / "exthost"
    exthost.mkdir(parents=True)
    (exthost / "exthost.log").write_text(
        "Extension host with pid 999 started\n"
        "Extension host with pid 1000 started\n"
        "loading workspaceStorage/storage-key/extension-state\n"
        "Extension host terminating: received terminate message from renderer\n"
        "Extension host with pid 999 exiting with code 0\n",
        encoding="utf-8",
    )
    status = "CPU %\tMem MB\tPID\tProcess\n0\t100\t1000\textension-host [7]\n"

    snapshot = VSCodeLiveWindowIndex(user_data, status_runner=lambda: status).snapshot()

    assert snapshot == {"storage-key": LiveVSCodeWindow("7", 1000, None, None)}


def test_terminated_latest_extension_host_log_is_not_live(tmp_path: Path) -> None:
    user_data = tmp_path / "Code" / "User"
    exthost = user_data.parent / "logs" / "20260901T100000" / "window4" / "exthost"
    exthost.mkdir(parents=True)
    (exthost / "exthost.log").write_text(
        "Extension host with pid 1000 started\n"
        "loading workspaceStorage/storage-key/extension-state\n"
        "Extension host terminating: renderer closed the MessagePort\n",
        encoding="utf-8",
    )
    status = "CPU %\tMem MB\tPID\tProcess\n0\t100\t1000\textension-host [4]\n"

    assert (
        VSCodeLiveWindowIndex(user_data, status_runner=lambda: status).snapshot() == {}
    )


def test_codex_log_requires_latest_exact_thread_role_to_be_owner(
    tmp_path: Path,
) -> None:
    log = tmp_path / "Codex.log"
    log.write_bytes(
        b"thread_stream_role_changed conversationId="
        + SESSION_CURRENT.encode("ascii")
        + b" role=owner\n"
        + b"unrelated prompt-like line must be ignored\n"
        + b"thread_stream_role_changed conversationId="
        + SESSION_CURRENT.encode("ascii")
        + b" role=observer\n"
    )

    assert codex_log_owns_session(log, SESSION_CURRENT) is False
    with log.open("ab") as handle:
        handle.write(
            b"maybe_resume_success assignedStreamRole=owner conversationId="
            + SESSION_CURRENT.encode("ascii")
            + b"\n"
        )
    assert codex_log_owns_session(log, SESSION_CURRENT) is True
    assert codex_log_owns_session(log, SESSION_OTHER) is False


def test_codex_log_accepts_owner_at_crlf_end_and_rejects_prefixed_id(
    tmp_path: Path,
) -> None:
    log = tmp_path / "Codex.log"
    log.write_bytes(
        b"thread_stream_role_changed conversationId="
        + SESSION_CURRENT.encode("ascii")
        + b"extra role=owner\r\n"
        + b"thread_stream_role_changed conversationId="
        + SESSION_CURRENT.encode("ascii")
        + b" role=owner\r\n"
    )

    assert codex_log_owns_session(log, SESSION_CURRENT) is True


def test_codex_log_owner_does_not_cross_app_server_generation(tmp_path: Path) -> None:
    log = tmp_path / "Codex.log"
    owner = (
        b"thread_stream_role_changed conversationId="
        + SESSION_CURRENT.encode("ascii")
        + b" role=owner\n"
    )
    spawn = b"[info] [CodexMcpConnection] Spawning codex app-server\n"
    log.write_bytes(owner + spawn)

    assert codex_log_owns_session(log, SESSION_CURRENT) is False

    with log.open("ab") as handle:
        handle.write(owner)
    assert codex_log_owns_session(log, SESSION_CURRENT) is True

    with log.open("ab") as handle:
        handle.write(
            b"thread_stream_role_changed conversationId="
            + SESSION_CURRENT.encode("ascii")
            + b" role="
        )
    assert codex_log_owns_session(log, SESSION_CURRENT) is False


def test_persisted_but_nonlive_window_is_not_reported_or_tracked(
    tmp_path: Path,
) -> None:
    live_repo = (tmp_path / "live").resolve()
    stale_repo = (tmp_path / "stale").resolve()
    live_repo.mkdir()
    stale_repo.mkdir()
    live_uri = live_repo.as_uri()
    stale_uri = stale_repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(
        user_data / "globalStorage" / "storage.json",
        [{"folder": live_uri}, {"folder": stale_uri}],
    )
    write_workspace(user_data / "workspaceStorage", "live-key", live_uri)
    write_workspace(user_data / "workspaceStorage", "stale-key", stale_uri)
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, str(live_repo), "vscode", "user", 0)],
    )

    class OneLiveWindow:
        @staticmethod
        def snapshot():
            return {"live-key": LiveVSCodeWindow("4", 100, 200, tmp_path / "Codex.log")}

    resolver = CodexSessionResolver(
        tmp_path / "runtime",
        tmp_path / ".codex",
        lock_probe=lambda path: path.stem == SESSION_CURRENT,
        owner_probe=lambda _path, _session: True,
    )
    discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "runtime",
        codex_home=tmp_path / ".codex",
        user_data_root=user_data,
        session_resolver=resolver,
        live_window_index=OneLiveWindow(),
        git_root_resolver=lambda path: path,
        sleep=lambda _seconds: None,
    )

    snapshot = discovery.snapshot()

    assert len(snapshot.windows) == 1
    assert snapshot.windows[0].workspace_uri == live_uri
    assert [item.repo_root for item in snapshot.effective_workspaces] == [live_repo]
    assert snapshot.issues == ("vscode_live_window_unmapped",)


def test_held_user_thread_must_belong_to_exact_window_session_cache(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)
    write_threads(
        tmp_path / ".codex",
        [
            (SESSION_CURRENT, str(repo), "vscode", "user", 0),
            (SESSION_OTHER, str(repo), "vscode", "user", 0),
        ],
    )
    database = user_data / "workspaceStorage" / "storage-1" / "state.vscdb"
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            (
                json.dumps(
                    [
                        {
                            "providerType": "openai-codex",
                            "resource": f"openai-codex://route/local/{SESSION_OTHER}",
                        }
                    ]
                ),
                "agentSessions.model.cache",
            ),
        )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda path: path.stem == SESSION_CURRENT,
    ).snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].reason == "no_loaded_vscode_thread"


def test_malformed_window_session_cache_fails_closed(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, str(repo), "vscode", "user", 0)]
    )
    database = user_data / "workspaceStorage" / "storage-1" / "state.vscdb"
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            ("{not-json", "agentSessions.model.cache"),
        )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda _path: True,
    ).snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].reason == "vscode_session_cache_unavailable"
