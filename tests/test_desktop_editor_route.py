"""Current process candidates are independent of historical routing logs."""
from types import SimpleNamespace

import pytest

from codex_watchdog import desktop_editor_route as route
from codex_watchdog.native_inflight import NativeInflightError


def test_windows_reads_current_tree_without_window_log_index(monkeypatch):
    adapter = object()
    monkeypatch.setattr(route, "sys", SimpleNamespace(platform="win32"))
    status = ("0\t100\t100\textension-host [1]\n"
              "0\t100\t101\t  C:\\extensions\\openai.chatgpt-1\\bin\\codex.exe app-server\n")
    seen = []
    def read(selected):
        assert selected is adapter
        seen.append(True)
        return SimpleNamespace(output=status)
    monkeypatch.setattr(route, "run_vscode_status", read)
    monkeypatch.setattr(route.VSCodeLiveWindowIndex, "snapshot",
                        lambda *a: pytest.fail("historical log correlation"))
    monkeypatch.setattr(route, "native_vscode_hosts",
                        lambda: pytest.fail("wrong platform process reader"))
    assert route.editor_pids(adapter) == (101,)
    assert seen == [True]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_posix_uses_existing_fresh_parent_generation_evidence(monkeypatch, platform):
    monkeypatch.setattr(route, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(route, "native_vscode_hosts", lambda: (
        SimpleNamespace(codex_pid=104), SimpleNamespace(codex_pid=108)))
    monkeypatch.setattr(route, "run_vscode_status",
                        lambda *a: pytest.fail("no desktop status process needed"))
    assert route.editor_pids() == (104, 108)


@pytest.mark.parametrize("hosts", [None, (SimpleNamespace(codex_pid=True),),
    (SimpleNamespace(codex_pid=-1),),
    (SimpleNamespace(codex_pid=104), SimpleNamespace(codex_pid=104))])
def test_unavailable_invalid_or_duplicate_route_refuses(monkeypatch, hosts):
    monkeypatch.setattr(route, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(route, "native_vscode_hosts", lambda: hosts)
    with pytest.raises(NativeInflightError):
        route.editor_pids()


def test_no_editor_is_no_route_not_an_idle_proof(monkeypatch):
    monkeypatch.setattr(route, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(route, "native_vscode_hosts", lambda: ())
    assert route.editor_pids() == ()


def test_unsupported_platform_refuses_without_discovery(monkeypatch):
    monkeypatch.setattr(route, "sys", SimpleNamespace(platform="unsupported"))
    monkeypatch.setattr(route, "native_vscode_hosts", lambda: pytest.fail("OS query"))
    with pytest.raises(NativeInflightError, match="unsupported"):
        route.editor_pids()
