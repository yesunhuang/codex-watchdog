import os
from pathlib import Path

import pytest

from codex_watchdog import process_environment


@pytest.mark.parametrize('original', [None, '', '/user/library path:/vendor/lib'])
def test_frozen_linux_codex_restores_loader_without_changing_parent(monkeypatch, original):
    monkeypatch.setattr(process_environment.sys, 'platform', 'linux')
    monkeypatch.setattr(process_environment.sys, 'frozen', True, raising=False)
    monkeypatch.setenv('LD_LIBRARY_PATH', '/tmp/_MEI-fixture/bundled-libraries')
    monkeypatch.setenv('CODEX_HOME', '/different/codex')
    monkeypatch.setenv('RETAIN_PROVIDER_SETTING', 'opaque-fixture-setting')
    monkeypatch.delenv('LD_LIBRARY_PATH_ORIG', raising=False)
    if original is not None:
        monkeypatch.setenv('LD_LIBRARY_PATH_ORIG', original)
    before = dict(os.environ)
    target = Path('explicit-codex-home')
    child = process_environment.codex_process_environment(target)
    assert child.get('LD_LIBRARY_PATH') == original
    assert child['CODEX_HOME'] == str(target)
    assert child['RETAIN_PROVIDER_SETTING'] == 'opaque-fixture-setting'
    assert dict(os.environ) == before


@pytest.mark.parametrize('platform,frozen', [('linux', False), ('darwin', True), ('win32', True)])
def test_source_mac_and_windows_loader_environments_are_preserved(monkeypatch, platform, frozen):
    monkeypatch.setattr(process_environment.sys, 'platform', platform)
    monkeypatch.setattr(process_environment.sys, 'frozen', frozen, raising=False)
    monkeypatch.setenv('LD_LIBRARY_PATH', '/explicit/user/libraries')
    monkeypatch.setenv('LD_LIBRARY_PATH_ORIG', '/distinct/original')
    before = dict(os.environ)
    target = Path('explicit-codex-home')
    assert process_environment.codex_process_environment(target) == {**before, 'CODEX_HOME': str(target)}
