from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterable, List, Tuple

import pytest

from codex_watchdog.workspace_discovery import (
    _posix_writer_lock_is_held,
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock is unavailable")
def test_posix_writer_lock_accepts_darwin_eagain_35(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    lock = tmp_path / "thread.lock"
    lock.write_bytes(b"")

    monkeypatch.setattr(errno, "EAGAIN", 35)

    def held(_descriptor: int, _operation: int) -> None:
        raise BlockingIOError(35, "writer lock held")

    monkeypatch.setattr(fcntl, "flock", held)

    assert _posix_writer_lock_is_held(lock) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock is unavailable")
def test_posix_writer_lock_rejects_unrelated_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    lock = tmp_path / "thread.lock"
    lock.write_bytes(b"")

    def failed(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.EIO, "unrelated failure")

    monkeypatch.setattr(fcntl, "flock", failed)

    assert _posix_writer_lock_is_held(lock) is False


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
    owner_probe=None,
) -> VSCodeWorkspaceDiscovery:
    runtime = tmp_path / "runtime"
    codex_home = tmp_path / ".codex"
    selected_registry = registry or WorkspaceRegistry(runtime)
    user_data_root = tmp_path / "Code" / "User"
    session_resolver = CodexSessionResolver(
        runtime,
        codex_home,
        lock_probe=lock_probe,
        owner_probe=(
            owner_probe
            if owner_probe is not None
            else lambda _path, _session_id: True
        ),
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
    database = user_data / "workspaceStorage" / "storage-1" / "state.vscdb"
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            (
                json.dumps(
                    [
                        {
                            "providerType": "openai-codex",
                            "resource": (
                                "openai-codex://route/local/" + SESSION_CURRENT
                            ),
                        }
                    ]
                ),
                "agentSessions.model.cache",
            ),
        )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda _path: True
    ).snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].tracking_status == "unresolved"
    assert snapshot.windows[0].reason == "ambiguous_loaded_threads"
    assert snapshot.windows[0].session_id is None


@pytest.mark.parametrize("old_active,current_active,expected", [
    ("false", "true", SESSION_CURRENT),
    ("true", "false", SESSION_OLD),
    ("true", "true", None),
    ("false", "false", None),
    (None, "true", None),
    ("true", None, None),
    ("invalid", "true", None),
])
def test_multiple_loaded_owners_require_one_explicit_active_view(
    tmp_path, old_active, current_active, expected,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex_home = tmp_path / ".codex"
    write_threads(codex_home, [
        (SESSION_OLD, str(repo), "vscode", "user", 0),
        (SESSION_CURRENT, str(repo), "vscode", "user", 0),
    ])
    log = tmp_path / "Codex.log"
    lines = []
    for session, active in ((SESSION_OLD, old_active), (SESSION_CURRENT, current_active)):
        lines.append(f"thread_stream_role_changed conversationId={session} role=owner")
        if active is not None:
            lines.append(f"thread_stream_view_activity_changed conversationId={session} active={active} streamRole=owner")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    resolver = CodexSessionResolver(tmp_path / "runtime", codex_home, lock_probe=lambda _: True)
    result = resolver.resolve((repo,), codex_log=log, window_state_database=None)
    assert result.session_id == expected
    assert result.source == ("codex_state_vscode_active_owner" if expected else None)
    assert result.reason == (None if expected else "ambiguous_loaded_threads")

    # Replacing the App Server invalidates activity as well as stream ownership.
    with log.open("a", encoding="utf-8") as handle:
        handle.write("[CodexMcpConnection] Spawning codex app-server\n")
        for session in (SESSION_OLD, SESSION_CURRENT):
            handle.write(f"thread_stream_role_changed conversationId={session} role=owner\n")
    assert resolver.resolve((repo,), codex_log=log, window_state_database=None).session_id is None


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
        def resolve(paths, *, codex_log, window_state_database, live_windows=()):
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


def test_status_parser_binds_posix_codex_children() -> None:
    status = "\n".join(
        (
            "CPU %\tMem MB\tPID\tProcess",
            "0\t100\t4100\textension-host [7]",
            "0\t100\t4101\t     /Users/u/.vscode/extensions/openai.chatgpt-1-darwin-arm64/bin/darwin-arm64/codex -c x app-server",
            "0\t100\t5100\textension-host [8]",
            "0\t100\t5101\t     /home/u/.vscode/extensions/openai.chatgpt-1-linux-x64/bin/linux-x86_64/codex app-server",
        )
    )

    assert VSCodeLiveWindowIndex._parse_status(status) == {
        "7": (4100, 4101),
        "8": (5100, 5101),
    }


def test_live_window_log_requires_latest_exact_extension_host_pid(
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "Code" / "User"
    exthost = user_data.parent / "logs" / "20260901T100000" / "window4" / "exthost"
    exthost.mkdir(parents=True)
    (exthost / "exthost.log").write_text(
        "Extension host with pid 999 started\n"
        "loading workspaceStorage/previous-key/extension-state\n"
        "Extension host with pid 1000 started\n"
        "loading workspaceStorage/storage-key/extension-state\n",
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


@pytest.mark.parametrize("current_log", [
    "Extension host with pid 1000 started\n",
    "Extension host with pid 1000 started\n"
    "loading workspaceStorage/first-key/state\n"
    "loading workspaceStorage/second-key/state\n",
    "Extension host with pid 1000 started\n"
    "loading workspaceStorage/previous-key/state\n"
    "Extension host with pid 1002 started\n"
    "loading workspaceStorage/previous-key/state\n",
])
def test_current_host_never_borrows_or_guesses_workspace_storage(
    tmp_path: Path, current_log: str,
) -> None:
    user_data = tmp_path / "Code" / "User"
    exthost = user_data.parent / "logs" / "20260901T100000" / "window4" / "exthost"
    exthost.mkdir(parents=True)
    (exthost / "exthost.log").write_text(
        "Extension host with pid 999 started\n"
        "loading workspaceStorage/previous-key/state\n"
        "Extension host with pid 999 exiting with code 0\n" + current_log,
        encoding="utf-8",
    )
    status = "0\t100\t1000\textension-host [4]\n"

    assert VSCodeLiveWindowIndex(user_data, status_runner=lambda: status).snapshot() == {}


def test_reused_window_tracks_local_and_remote_same_name_independently(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "project").resolve()
    repo.mkdir()
    local_uri = repo.as_uri()
    remote_uri = "vscode-remote://ssh-remote%2Bexample.invalid/home/user/project"
    previous_uri = "vscode-remote://ssh-remote%2Bexample.invalid/home/user/other"
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [
        {"folder": local_uri}, {"folder": remote_uri},
    ])
    for key, uri in (("local-key", local_uri), ("remote-key", remote_uri),
                     ("previous-key", previous_uri)):
        write_workspace(user_data / "workspaceStorage", key, uri)
    write_threads(tmp_path / ".codex", [
        (SESSION_CURRENT, str(repo), "vscode", "user", 0),
        (SESSION_OTHER, "/home/user/project", "vscode", "user", 0),
    ])
    for key, session in (("local-key", SESSION_CURRENT), ("remote-key", SESSION_OTHER)):
        with sqlite3.connect(str(user_data / "workspaceStorage" / key / "state.vscdb")) as db:
            db.execute("UPDATE ItemTable SET value = ? WHERE key = ?", (
                json.dumps([{"providerType": "openai-codex",
                             "resource": f"openai-codex://route/local/{session}"}]),
                "agentSessions.model.cache",
            ))
    logs = user_data.parent / "logs" / "20260901T100000"
    for window, host, key in (("1", 1000, "local-key"), ("2", 2000, "remote-key")):
        exthost = logs / ("window" + window) / "exthost"
        exthost.mkdir(parents=True)
        previous = (
            "Extension host with pid 999 started\n"
            "loading workspaceStorage/previous-key/state\n"
            "Extension host terminating: received terminate message from renderer\n"
            "Extension host with pid 999 exiting with code 0\n"
        ) if window == "1" else ""
        (exthost / "exthost.log").write_text(
            previous + f"Extension host with pid {host} started\n"
            f"loading workspaceStorage/{key}/state\n", encoding="utf-8",
        )
        if window == "1":
            codex_log = exthost / "openai.chatgpt" / "Codex.log"
            codex_log.parent.mkdir()
            codex_log.write_text(
                f"thread_stream_role_changed conversationId={SESSION_CURRENT} role=owner\n",
                encoding="utf-8",
            )
    status = (
        "0\t100\t1000\textension-host [1]\n"
        "0\t100\t1001\t     /home/user/.vscode/extensions/openai.chatgpt-1/"
        "bin/linux-x64/codex app-server\n"
        "0\t100\t2000\textension-host [2]\n"
    )
    git = GitRootResolver(repo)
    discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "runtime", codex_home=tmp_path / ".codex", user_data_root=user_data,
        session_resolver=CodexSessionResolver(
            tmp_path / "runtime", tmp_path / ".codex",
            lock_probe=lambda path: path.stem == SESSION_CURRENT,
        ),
        live_window_index=VSCodeLiveWindowIndex(user_data, status_runner=lambda: status),
        git_root_resolver=git,
    )

    snapshot = discovery.snapshot()

    assert snapshot.status == "ok"
    assert [(w.repo_root, w.session_id) for w in snapshot.effective_workspaces] == [
        (repo, SESSION_CURRENT),
    ]
    assert [(w.locality, w.tracking_status, w.session_id) for w in snapshot.windows] == [
        ("process_local", "tracked", SESSION_CURRENT),
        ("remote_ssh", "remote_adapter", SESSION_OTHER),
    ]
    assert git.calls == [repo]


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


def test_stale_window_cache_does_not_suppress_exact_live_owner(
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

    assert [item.session_id for item in snapshot.effective_workspaces] == [
        SESSION_CURRENT
    ]
    assert snapshot.windows[0].tracking_status == "tracked"
    assert snapshot.windows[0].session_source == "codex_state_vscode_live_owner"


def test_empty_window_cache_allows_exact_live_owner_fallback(tmp_path: Path) -> None:
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
            ("[]", "agentSessions.model.cache"),
        )

    snapshot = make_discovery(
        tmp_path, GitRootResolver(repo), lambda path: path.stem == SESSION_CURRENT,
    ).snapshot()

    assert [item.session_id for item in snapshot.effective_workspaces] == [
        SESSION_CURRENT
    ]
    assert snapshot.windows[0].session_source == "codex_state_vscode_live_owner"


def test_held_cached_thread_without_exact_window_owner_fails_closed(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    uri = repo.as_uri()
    user_data = tmp_path / "Code" / "User"
    write_windows_state(user_data / "globalStorage" / "storage.json", [{"folder": uri}])
    write_workspace(user_data / "workspaceStorage", "storage-1", uri)
    write_threads(
        tmp_path / ".codex", [(SESSION_CURRENT, str(repo), "vscode", "user", 0)]
    )

    snapshot = make_discovery(
        tmp_path,
        GitRootResolver(repo),
        lambda path: path.stem == SESSION_CURRENT,
        owner_probe=lambda _path, _session_id: False,
    ).snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].tracking_status == "unresolved"
    assert snapshot.windows[0].reason == "no_loaded_vscode_thread"


def test_native_writer_survives_discarded_routing_log(tmp_path: Path) -> None:
    discovery, target_log, _other_log, windows = cross_window_discovery(tmp_path)
    target_log.write_text("new log content without old routing markers\n", encoding="utf-8")
    discovery.session_resolver.writer_process_probe = lambda _path: 101

    result = discovery.snapshot()

    target = next(window for window in result.windows if window.workspace_storage_key == "target")
    assert target.session_id == SESSION_CURRENT
    assert target.session_source == "codex_state_vscode_native_writer"
    assert target.reason is None
    assert windows["target"].codex_app_server_pid == 101


def test_all_local_windows_survive_discarded_routing_logs(tmp_path: Path) -> None:
    discovery, target_log, other_log, _windows = cross_window_discovery(tmp_path)
    for log in (target_log, other_log):
        log.write_text("retained log has no routing history\n", encoding="utf-8")
    discovery.session_resolver.writer_process_probe = lambda path: (
        101 if path.stem == SESSION_CURRENT else 201
    )

    snapshot = discovery.snapshot()

    assert {item.repo_root.name: item.session_id for item in snapshot.effective_workspaces} == {
        "target": SESSION_CURRENT, "other": SESSION_OTHER,
    }
    assert all(window.session_source == "codex_state_vscode_native_writer" for window in snapshot.windows)
    assert snapshot.issues == ()


@pytest.mark.parametrize("missing_proof", [
    "other_writer", "no_writer", "released_during_probe", "missing_cache", "unreadable_log",
    "multiple_threads", "duplicate_window", "resume_failed", "unsubscribed", "inactive", "unknown_role",
])
def test_discarded_log_still_requires_exact_native_owner(tmp_path: Path, missing_proof: str) -> None:
    discovery, target_log, _other_log, windows = cross_window_discovery(tmp_path)
    target_log.write_text("new log without routing markers\n", encoding="utf-8")
    resolver = discovery.session_resolver
    resolver.writer_process_probe = lambda _path: 101
    if missing_proof == "other_writer":
        resolver.writer_process_probe = lambda _path: 201
    elif missing_proof == "no_writer":
        resolver.writer_process_probe = lambda _path: None
    elif missing_proof == "released_during_probe":
        def release(_path):
            resolver.lock_probe = lambda _path: False
            return 101
        resolver.writer_process_probe = release
    elif missing_proof == "missing_cache":
        resolver.window_session_candidates = lambda _path: set()
    elif missing_proof == "unreadable_log":
        target_log.unlink()
    elif missing_proof == "multiple_threads":
        with sqlite3.connect(str(tmp_path / ".codex/state_5.sqlite")) as db:
            db.execute("INSERT INTO threads VALUES (?, ?, 'vscode', 'user', 0)",
                       (SESSION_OLD, str(tmp_path / "target")))
    elif missing_proof == "duplicate_window":
        windows["duplicate"] = LiveVSCodeWindow("3", 300, 101, target_log)
    else:
        event = {
            "resume_failed": "maybe_resume_failed",
            "unsubscribed": "inactive_thread_unsubscribed",
            "inactive": "thread_stream_view_activity_changed active=false",
            "unknown_role": "thread_stream_role_changed role=unknown",
        }[missing_proof]
        target_log.write_text(f"{event} conversationId={SESSION_CURRENT}\n", encoding="utf-8")

    target = next(window for window in discovery.snapshot().windows if window.workspace_storage_key == "target")

    assert target.session_id is None
    assert target.tracking_status == "unresolved"


def cross_window_discovery(tmp_path: Path):
    """Two different repositories; the second server owns both exact threads."""
    user_data = tmp_path / "Code" / "User"
    target_repo, other_repo = tmp_path / "target", tmp_path / "other"
    target_repo.mkdir()
    other_repo.mkdir()
    write_windows_state(user_data / "globalStorage" / "storage.json", [
        {"folder": target_repo.as_uri()}, {"folder": other_repo.as_uri()},
    ])
    write_workspace(user_data / "workspaceStorage", "target", target_repo.as_uri())
    write_workspace(user_data / "workspaceStorage", "other", other_repo.as_uri())
    write_threads(tmp_path / ".codex", [
        (SESSION_CURRENT, str(target_repo), "vscode", "user", 0),
        (SESSION_OTHER, str(other_repo), "vscode", "user", 0),
    ])
    target_log, other_log = tmp_path / "target.log", tmp_path / "other.log"
    target_log.write_text(
        f"thread_stream_role_changed conversationId={SESSION_CURRENT} role=follower\n"
        f"thread_stream_view_activity_changed conversationId={SESSION_CURRENT} active=true\n",
        encoding="utf-8",
    )
    other_log.write_text(
        f"thread_stream_role_changed conversationId={SESSION_CURRENT} role=owner\n"
        f"thread_stream_role_changed conversationId={SESSION_OTHER} role=owner\n",
        encoding="utf-8",
    )
    windows = {
        "target": LiveVSCodeWindow("1", 100, 101, target_log),
        "other": LiveVSCodeWindow("2", 200, 201, other_log),
    }

    class LiveIndex:
        def snapshot(self):
            return windows

    resolver = CodexSessionResolver(
        tmp_path / "runtime", tmp_path / ".codex", lock_probe=lambda _path: True,
        writer_process_probe=lambda _path: 201,
    )
    discovery = VSCodeWorkspaceDiscovery(
        tmp_path / "runtime", codex_home=tmp_path / ".codex",
        user_data_root=user_data, session_resolver=resolver,
        live_window_index=LiveIndex(), git_root_resolver=lambda path: path,
    )
    return discovery, target_log, other_log, windows


def test_active_follower_keeps_exact_thread_separate_from_owners_workspace(tmp_path: Path) -> None:
    discovery, _target_log, _other_log, _windows = cross_window_discovery(tmp_path)

    snapshot = discovery.snapshot()

    assert {item.repo_root.name: item.session_id for item in snapshot.effective_workspaces} == {
        "target": SESSION_CURRENT, "other": SESSION_OTHER,
    }
    target = next(window for window in snapshot.windows if window.workspace_storage_key == "target")
    assert target.tracking_status == "tracked"
    assert target.session_source == "codex_state_vscode_active_follower_verified_owner"
    assert target.reason == "vscode_thread_owned_by_another_window"
    assert snapshot.issues == ("vscode_thread_owned_by_another_window",)
    # Discovery observes the roles; it must not rewrite them to fake a transfer.
    assert codex_log_owns_session(_target_log, SESSION_CURRENT) is False


@pytest.mark.parametrize("missing_proof", [
    "inactive", "unknown_activity", "unlocked", "owner_exited", "different_thread",
    "owner_restarted", "owner_unsubscribed", "wrong_cwd", "multiple_owners",
    "unknown_writer", "different_writer", "lock_released_during_probe",
])
def test_cross_window_follower_requires_all_live_exact_proofs(tmp_path: Path, missing_proof: str) -> None:
    discovery, target_log, other_log, windows = cross_window_discovery(tmp_path)
    if missing_proof == "inactive":
        with target_log.open("a") as handle:
            handle.write(f"thread_stream_view_activity_changed conversationId={SESSION_CURRENT} active=false\n")
    elif missing_proof == "unknown_activity":
        target_log.write_text(f"thread_stream_role_changed conversationId={SESSION_CURRENT} role=follower\n")
    elif missing_proof == "unlocked":
        discovery.session_resolver.lock_probe = lambda path: path.stem != SESSION_CURRENT
    elif missing_proof == "owner_exited":
        windows["other"] = LiveVSCodeWindow("2", 200, None, other_log)
    elif missing_proof == "different_thread":
        other_log.write_text(f"thread_stream_role_changed conversationId={SESSION_OTHER} role=owner\n")
    elif missing_proof == "owner_restarted":
        with other_log.open("a") as handle:
            handle.write("[CodexMcpConnection] Spawning codex app-server\n")
    elif missing_proof == "owner_unsubscribed":
        with other_log.open("a") as handle:
            handle.write(f"inactive_thread_unsubscribed conversationId={SESSION_CURRENT} status=unsubscribed\n")
    elif missing_proof == "wrong_cwd":
        with sqlite3.connect(str(tmp_path / ".codex" / "state_5.sqlite")) as connection:
            connection.execute("UPDATE threads SET cwd = ? WHERE id = ?", (str(tmp_path / "unrelated"), SESSION_CURRENT))
    elif missing_proof == "multiple_owners":
        duplicate = tmp_path / "duplicate.log"
        duplicate.write_text(f"thread_stream_role_changed conversationId={SESSION_CURRENT} role=owner\n")
        windows["duplicate"] = LiveVSCodeWindow("3", 300, 201, duplicate)
    elif missing_proof == "unknown_writer":
        discovery.session_resolver.writer_process_probe = lambda _path: None
    elif missing_proof == "different_writer":
        discovery.session_resolver.writer_process_probe = lambda _path: 999
    elif missing_proof == "lock_released_during_probe":
        def release(_path):
            discovery.session_resolver.lock_probe = lambda _lock: False
            return 201
        discovery.session_resolver.writer_process_probe = release

    snapshot = discovery.snapshot()

    assert SESSION_CURRENT not in [item.session_id for item in snapshot.effective_workspaces]
    if missing_proof in {"owner_exited", "different_thread", "owner_restarted", "owner_unsubscribed"}:
        assert "vscode_thread_owner_unverified" in snapshot.issues
    elif missing_proof == "multiple_owners":
        assert "ambiguous_vscode_thread_owner" in snapshot.issues


def test_actual_writer_disambiguates_stale_owner_logs_in_other_live_windows(tmp_path: Path) -> None:
    discovery, _target_log, _other_log, windows = cross_window_discovery(tmp_path)
    stale = tmp_path / "stale.log"
    stale.write_text(f"thread_stream_role_changed conversationId={SESSION_CURRENT} role=owner\n")
    windows["stale"] = LiveVSCodeWindow("3", 300, 301, stale)

    snapshot = discovery.snapshot()

    target = next(item for item in snapshot.effective_workspaces if item.repo_root.name == "target")
    assert target.session_id == SESSION_CURRENT


def test_native_ownership_return_keeps_discovery_identity(tmp_path: Path) -> None:
    discovery, target_log, other_log, _windows = cross_window_discovery(tmp_path)
    before = next(item for item in discovery.snapshot().effective_workspaces if item.session_id == SESSION_CURRENT)
    with other_log.open("a") as handle:
        handle.write(f"inactive_thread_unsubscribed conversationId={SESSION_CURRENT} status=unsubscribed\n")
    with target_log.open("a") as handle:
        handle.write(f"thread_stream_role_changed conversationId={SESSION_CURRENT} role=owner\n")

    snapshot = discovery.snapshot()

    after = next(item for item in snapshot.effective_workspaces if item.session_id == SESSION_CURRENT)
    assert after.has_same_registration(before)
    assert snapshot.issues == ()


def test_unverified_active_follower_does_not_fall_back_to_old_same_repo_owner(tmp_path: Path) -> None:
    discovery, target_log, other_log, _windows = cross_window_discovery(tmp_path)
    with sqlite3.connect(str(tmp_path / ".codex" / "state_5.sqlite")) as connection:
        connection.execute("INSERT INTO threads VALUES (?, ?, 'vscode', 'user', 0)",
                           (SESSION_OLD, str(tmp_path / "target")))
    with target_log.open("a") as handle:
        handle.write(f"thread_stream_role_changed conversationId={SESSION_OLD} role=owner\n")
        handle.write(f"thread_stream_view_activity_changed conversationId={SESSION_OLD} active=false\n")
    other_log.write_text(f"thread_stream_role_changed conversationId={SESSION_OTHER} role=owner\n")

    snapshot = discovery.snapshot()

    assert all(item.repo_root.name != "target" for item in snapshot.effective_workspaces)
    assert "vscode_thread_owner_unverified" in snapshot.issues


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
        tmp_path,
        GitRootResolver(repo),
        lambda _path: True,
        owner_probe=lambda _path, _session_id: False,
    ).snapshot()

    assert snapshot.effective_workspaces == ()
    assert snapshot.windows[0].reason == "vscode_session_cache_unavailable"
