"""The optional Linux recovery must not intercept hooks on other platforms."""
import builtins
from contextlib import contextmanager
import io
import json

import pytest

from codex_watchdog import stop_hook


@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_non_linux_hook_keeps_original_guard_without_importing_bootstrap(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(stop_hook.sys, 'platform', platform)
    calls = []
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name.endswith('linux_bootstrap'):
            pytest.fail('A non-Linux hook imported the optional service bootstrap')
        return original_import(name, *args, **kwargs)

    @contextmanager
    def original_owner(runtime, *args, **kwargs):
        calls.append('owner_guard')
        yield runtime

    def original_hook(*args, **kwargs):
        calls.append('hook')
        return 0

    monkeypatch.setattr(builtins, '__import__', checked_import)
    monkeypatch.setattr(stop_hook, 'hook_owner', original_owner)
    monkeypatch.setattr(stop_hook, '_run_hook', original_hook)
    payload = {'hook_event_name': 'Stop', 'session_id': '11111111-2222-4333-8444-555555555555'}
    settings = stop_hook.HookSettings(tmp_path / 'runtime', codex_home=tmp_path / 'codex', test_mode=True)
    result = stop_hook.run_hook(settings, stdin=io.StringIO(json.dumps(payload)),
                                stdout=io.StringIO(), stderr=io.StringIO())
    assert result == 0
    assert calls == ['owner_guard', 'hook']
