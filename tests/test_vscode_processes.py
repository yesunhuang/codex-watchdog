from pathlib import Path
from types import SimpleNamespace
import os
import sys

import pytest

from codex_watchdog import vscode_processes as native
from codex_watchdog.workspace_discovery import LiveVSCodeWindow, VSCodeLiveWindowIndex


CODEX = '/home/u/.vscode/extensions/openai.chatgpt-1/bin/linux-x86_64/codex app-server'
HOST = '/usr/share/code/code --type=utility --utility-sub-type=node.mojom.NodeService'


def test_process_collection_keeps_child_before_parent_and_filters_users(monkeypatch):
    monkeypatch.setattr(native.os, 'getuid', lambda: 501, raising=False)
    monkeypatch.setattr(native.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout=f'501 12 14 Mon Sep 14 12:00:01 2026 {CODEX}\n'
               f'501 14 99 Mon Sep 14 12:00:00 2026 {HOST}\n'
               '502 15 99 Mon Sep 14 12:00:00 2026 other\n',
        check_returncode=lambda: None,
    ))
    result = native._processes()
    assert result == {12: (14, 'Mon Sep 14 12:00:01 2026', CODEX),
                      14: (99, 'Mon Sep 14 12:00:00 2026', HOST)}


@pytest.mark.parametrize('change', ['none', 'pid_reused', 'child_exited', 'two_children', 'two_logs', 'wrong_parent'])
def test_native_probe_requires_unchanged_generation_and_unambiguous_open_log(monkeypatch, change):
    monkeypatch.setattr(native.sys, 'platform', 'darwin')
    before = {12: (14, 'generation', CODEX), 14: (99, 'generation', HOST)}
    if change == 'two_children':
        before[13] = (14, 'generation', CODEX)
    if change == 'wrong_parent':
        before[14] = (99, 'generation', '/usr/bin/python')
    after = dict(before)
    if change == 'pid_reused':
        after[14] = (99, 'replacement-generation', HOST)
    if change == 'child_exited':
        del after[12]
    snapshots = iter([before, after])
    monkeypatch.setattr(native, '_processes', lambda: next(snapshots))
    log = Path('/Code/logs/session/window3/exthost/exthost.log')
    monkeypatch.setattr(native, '_open_logs', lambda pids: {
        14: (log, Path('/other/exthost.log')) if change == 'two_logs' else (log,),
    })
    assert native.native_vscode_hosts() == (
        (native.NativeVSCodeHost(14, 12, log),) if change == 'none' else ()
    )


def test_open_log_parser_preserves_pid_boundaries_and_spaces(monkeypatch):
    monkeypatch.setattr(native.sys, 'platform', 'darwin')
    monkeypatch.setattr(native.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout='p14\nn/Library/Application Support/Code/logs/a/window3/exthost/exthost.log\n'
               'p15\nn/other/window4/exthost/exthost.log\nn/unrelated\n',
        check_returncode=lambda: None,
    ))
    assert native._open_logs((14, 15)) == {
        14: (Path('/Library/Application Support/Code/logs/a/window3/exthost/exthost.log'),),
        15: (Path('/other/window4/exthost/exthost.log'),),
    }


@pytest.mark.skipif(sys.platform != 'linux', reason='requires Linux proc file descriptors')
def test_linux_open_log_is_bound_to_the_actual_process(tmp_path):
    log = tmp_path / 'exthost.log'
    with log.open('w'):
        assert native._open_logs((os.getpid(),)) == {os.getpid(): (log,)}
    assert native._open_logs((os.getpid(),)) == {os.getpid(): ()}


def test_failed_native_probe_keeps_verified_status_path(tmp_path):
    user = tmp_path / 'Code' / 'User'
    log = user.parent / 'logs' / 'session' / 'window3' / 'exthost' / 'exthost.log'
    log.parent.mkdir(parents=True)
    log.write_text('Extension host with pid 14 started\nworkspaceStorage/known/state\n')
    index = VSCodeLiveWindowIndex(user, native_runner=lambda: None,
        status_runner=lambda: '0\t100\t14\textension-host [3]')
    assert index.snapshot() == {'known': LiveVSCodeWindow('3', 14, None, None)}


@pytest.mark.parametrize('status', [None, '0\t100\t99\tcode main', '0\t100\t14\textension-host [3]'])
def test_native_evidence_recovers_omitted_host_or_codex(tmp_path, status):
    user = tmp_path / 'Code' / 'User'
    log = user.parent / 'logs' / 'session' / 'window3' / 'exthost' / 'exthost.log'
    log.parent.mkdir(parents=True)
    log.write_text('Extension host with pid 14 started\nworkspaceStorage/exact-storage/state\n')
    # A later unrelated CLI status session must not hide the host's actual open log.
    (user.parent / 'logs' / 'zz-newer-cli-session').mkdir()
    index = VSCodeLiveWindowIndex(user, status_runner=lambda: status,
        native_runner=lambda: (native.NativeVSCodeHost(14, 12, log),))
    assert index.snapshot() == {'exact-storage': LiveVSCodeWindow('3', 14, 12, None)}


@pytest.mark.parametrize('failure', ['exited', 'other_generation', 'other_profile', 'conflict', 'multiple_hosts'])
def test_native_recovery_never_guesses_conflicting_or_stale_workspace(tmp_path, failure):
    user = tmp_path / 'Code' / 'User'
    log = user.parent / 'logs' / 'session' / 'window3' / 'exthost' / 'exthost.log'
    log.parent.mkdir(parents=True)
    content = 'Extension host with pid 14 started\nworkspaceStorage/exact-storage/state\n'
    if failure == 'exited':
        content += 'Extension host with pid 14 exiting with code 0\n'
    if failure == 'other_generation':
        content += 'Extension host with pid 16 started\nworkspaceStorage/different/state\n'
    log.write_text(content)
    hosts = [native.NativeVSCodeHost(14, 12, log)]
    if failure == 'other_profile':
        user = tmp_path / 'Code - Insiders' / 'User'
        (user.parent / 'logs').mkdir(parents=True)
    if failure == 'multiple_hosts':
        hosts.append(hosts[0])
    status = '0\t100\t18\textension-host [3]' if failure == 'conflict' else None
    snapshot = VSCodeLiveWindowIndex(user, status_runner=lambda: status,
        native_runner=lambda: tuple(hosts)).snapshot()
    assert not snapshot
