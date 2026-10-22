"""C1: every Windows entry JSON read is bounded before parsing."""
from pathlib import Path
import json
import os
from types import SimpleNamespace

import pytest

from test_windows_vscode_discovery import Fixture, FsMeter, assert_healthy, tracked, win, windows_host


@windows_host
def test_oversized_global_document_preserves_exact_manual_registration(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    discovery = fixture.discovery()
    item = fixture.windows[2]
    manual = discovery.registry.add("manual-two", item["repo"], item["session"]).workspace
    path = fixture.user / "globalStorage/storage.json"
    path.write_bytes(b" " * (win.METADATA_JSON_BYTES + 1))
    meter = FsMeter(monkeypatch)
    snapshot = meter.run(discovery.snapshot)
    assert snapshot.status == "error"
    assert snapshot.effective_workspaces == (manual,)
    assert "vscode_window_state_unavailable" in snapshot.issues
    assert meter.bytes_from(path) == 0
    assert discovery.registry.list_workspaces() == [manual]


@windows_host
def test_unsafe_saved_workspace_does_not_starve_healthy_folder_windows(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    item = fixture.windows[1]
    saved = tmp_path / "open.code-workspace"
    saved.write_bytes(b" " * (win.METADATA_JSON_BYTES + 1))
    state = fixture.user / "globalStorage/storage.json"
    document = json.loads(state.read_text('utf-8'))
    document["windowsState"]["openedWindows"][0] = {"workspace": saved.as_uri()}
    state.write_text(json.dumps(document), encoding='utf-8')
    mapping = fixture.user / "workspaceStorage" / item["key"] / "workspace.json"
    mapping.write_text(json.dumps({"workspace": saved.as_uri()}), encoding='utf-8')
    meter = FsMeter(monkeypatch)
    discovery = fixture.discovery()
    snapshot = meter.run(discovery.snapshot)
    assert "repo1" not in tracked(snapshot)
    assert_healthy(snapshot, fixture)
    assert "workspace_file_unavailable" in snapshot.issues
    assert meter.bytes_from(saved) == 0
    assert discovery._windows_json_budget.bytes_charged == state.stat().st_size


def test_concurrent_growth_reads_at_most_cap_plus_one_before_refusal(tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    path.write_bytes(b" " * 4096)
    monkeypatch.setattr(win, "METADATA_JSON_BYTES", 128)
    real_stat = win.os.fstat
    monkeypatch.setattr(win.os, "fstat", lambda fd: SimpleNamespace(
        st_mode=real_stat(fd).st_mode, st_size=2))
    budget = win.WorkBudget()
    assert win.read_metadata_json(path, budget, lambda _: None) is None
    assert budget.bytes_charged == 129


def test_deep_and_malformed_documents_have_bounded_retry_work(tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    path.write_bytes(b"[" * 2000 + b"]" * 2000)
    budget = win.WorkBudget()
    sleeps = []
    assert win.read_metadata_json(path, budget, sleeps.append) is None
    assert budget.bytes_charged <= 3 * path.stat().st_size
    assert sleeps == [.02, .02]
    expired = win.WorkBudget(clock=lambda: 0)
    expired.deadline = -1
    assert win.read_metadata_json(path, expired, sleeps.append) is None
    assert expired.bytes_charged == 0


def test_large_integer_is_refused_before_decimal_conversion(tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    path.write_bytes(b'{"unrelated":' + b'9' * 10000 + b'}')
    calls = []
    real_integer = win._metadata_integer
    def integer(token):
        calls.append(len(token))
        return real_integer(token)
    monkeypatch.setattr(win, '_metadata_integer', integer)
    assert win.read_metadata_json(path, win.WorkBudget(), lambda _: None) is None
    assert calls == [10000] * 3
    assert real_integer('-123') == -123


@windows_host
def test_saved_workspace_phase_survives_independent_native_timeout(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    item = fixture.windows[1]
    saved = tmp_path / 'open.code-workspace'
    saved.write_text(json.dumps({'folders': [{'path': str(item['repo'])}]}), encoding='utf-8')
    state = fixture.user / 'globalStorage/storage.json'
    document = json.loads(state.read_text('utf-8'))
    document['windowsState']['openedWindows'][0] = {'workspace': saved.as_uri()}
    state.write_text(json.dumps(document), encoding='utf-8')
    (fixture.user / 'workspaceStorage' / item['key'] / 'workspace.json').write_text(
        json.dumps({'workspace': saved.as_uri()}), encoding='utf-8')
    now = [0.0]
    original_budget = win.WorkBudget
    monkeypatch.setattr(win, 'WorkBudget', lambda *_, **__: original_budget(clock=lambda: now[0]))
    index = fixture.index()
    original_probe = index.status_probe
    def probe():
        now[0] += 30  # Native/status work is independent of JSON parser work.
        return original_probe()
    index.status_probe = probe
    discovery = fixture.discovery(index=index)
    assert_healthy(discovery.snapshot(), fixture, (1, 2, 3))
    assert discovery._windows_json_budget.bytes_charged == state.stat().st_size + saved.stat().st_size


@windows_host
def test_metadata_budget_is_reset_per_snapshot_and_every_read_has_a_size(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    discovery = fixture.discovery()
    selected = fixture.user / "globalStorage/storage.json"
    real_open = Path.open
    requests = []

    class Handle:
        def __init__(self, wrapped):
            self.wrapped = wrapped
        def read(self, size=-1):
            requests.append(size)
            return self.wrapped.read(size)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.wrapped.close()
        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    def open_(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return Handle(handle) if path == selected else handle

    monkeypatch.setattr(Path, 'open', open_)
    first = discovery.snapshot()
    first_budget = discovery._windows_json_budget
    assert_healthy(first, fixture, (1, 2, 3))
    second = discovery.snapshot()
    assert_healthy(second, fixture, (1, 2, 3))
    assert discovery._windows_json_budget is not first_budget
    assert first_budget.bytes_charged == discovery._windows_json_budget.bytes_charged == selected.stat().st_size
    assert requests == [win.METADATA_JSON_BYTES + 1] * 2


def test_posix_metadata_reader_retains_existing_behavior(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    discovery = fixture.discovery()
    discovery.platform_adapter = SimpleNamespace(system='linux')
    monkeypatch.setattr(win, 'read_metadata_json', lambda *_: pytest.fail('Windows reader dispatched on POSIX'))
    path = tmp_path / 'metadata.json'
    path.write_text('{"valid":true}', encoding='utf-8')
    assert discovery._read_json_retry(path) == {"valid": True}
