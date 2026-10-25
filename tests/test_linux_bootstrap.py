"""Portable temporary-file/manager fixtures; no native services or sessions."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from codex_watchdog import control_state as cs, linux_bootstrap as b
from codex_watchdog.linux_node import node_unit_contents
from codex_watchdog.storage import FileLock


pytestmark = pytest.mark.skipif(
    os.name == 'nt', reason='Linux recovery fixtures require POSIX modes, locks and pipes')


THREAD = '11111111-2222-4333-8444-555555555555'
NODE = 'fixture-node.example'
NATIVE_PROOF = b._native_proof


class Manager:
    def __init__(self, unit, drops):
        self.unit, self.drops, self.calls = unit, drops, []
        self.load, self.active, self.pid = 'not-found', 'inactive', '0'
        self.after_reload = None
        self.failed_command = None
        self.cached_arguments = None
        self.typed_calls = 0
        self.before_typed_read = None

    def arguments(self):
        if self.cached_arguments is not None:
            return self.cached_arguments
        lines = [line for line in self.unit.read_text().splitlines() if line.startswith('ExecStart=')]
        return shlex.split(lines[-1][len('ExecStart='):])

    def typed_exec(self, unit, deadline, clock):
        assert unit == self.unit.name
        self.typed_calls += 1
        if self.before_typed_read:
            self.before_typed_read(self)
        args = self.arguments()
        return ('a(sasbttttuii) 1 "/usr/bin/env" ' + str(len(args)) + ' '
                + ' '.join(json.dumps(a) for a in args) + ' false 0 0 0 0 0 0 0\n')

    def __call__(self, args, deadline, clock):
        self.calls.append(list(args))
        if args[0] == self.failed_command:
            raise cs.ControlError('linux_bootstrap_systemctl_failed')
        if args[0] == 'show':
            rows = dict(LoadState=self.load, ActiveState=self.active,
                        SubState='running' if self.active == 'active' else 'dead',
                        MainPID=self.pid, FragmentPath=str(self.unit) if self.load == 'loaded' else '',
                        DropInPaths=' '.join(str(p) for p in self.drops), KillMode='mixed',
                        TimeoutStopUSec='infinity', Restart='no', Job='',
                        ExecStart=' '.join(self.arguments()) if self.load == 'loaded' else '',
                        ExecMainStartTimestamp='previous start' if self.pid != '0' else '')
            return ''.join(k + '=' + v + '\n' for k, v in rows.items())
        if args == ['daemon-reload']:
            self.load = 'loaded'
            if self.after_reload:
                self.after_reload()
            return ''
        if args == ['start', self.unit.name]:
            self.active, self.pid = 'active', '444'
            return ''
        pytest.fail('Unexpected manager operation: ' + repr(args))

    def effects(self):
        return [c for c in self.calls if c[0] != 'show']


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(b.sys, 'platform', 'linux')
    if not hasattr(os, 'getuid'):
        monkeypatch.setattr(os, 'getuid', lambda: 0, raising=False)
    monkeypatch.setattr(cs.socket, 'gethostname', lambda: NODE)
    home = tmp_path / 'codex'
    root = home / 'watchdog-nodes' / NODE
    root.mkdir(parents=True, mode=0o700)
    cs.control_atomic_json(root / 'node.json', dict(schema_version=1, node=NODE,
                                                  codex_home=str(home), future_choice='retain'))
    config = tmp_path / 'config'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config))
    unit = config / 'systemd/user' / ('codex-watchdog-node-' + NODE + '.service')
    unit.parent.mkdir(parents=True)
    env = tmp_path / 'notify.env'
    env.write_text('PROVIDER_CREDENTIAL=fixture-only\n')
    env.chmod(0o600)
    exe = tmp_path / 'accepted package' / 'codex-watchdog'
    exe.parent.mkdir()
    exe.write_bytes(b'fixture binary that is never executed\n')
    exe.chmod(0o700)
    manifest = exe.parent / 'package-manifest.json'
    manifest.write_text(json.dumps(dict(schema_version=1, platform='linux', version='retained',
        files={'codex-watchdog': hashlib.sha256(exe.read_bytes()).hexdigest()})))
    unit.write_text(node_unit_contents(NODE, exe, root / 'runtime', home, env, 30))
    unit.chmod(0o600)
    enabled = unit.parent / 'default.target.wants' / unit.name
    enabled.parent.mkdir()
    enabled.symlink_to(unit)
    dropdir = unit.with_name(unit.name + '.d')
    dropdir.mkdir()
    prologue = dropdir / '30-native-runtime.conf'
    prologue.write_text(b.NATIVE_PROLOGUE)
    prologue.chmod(0o600)
    repo = tmp_path / 'repo'
    repo.mkdir()
    payload = dict(session_id=THREAD, cwd=str(repo), hook_event_name='Stop', turn_id='fixture-turn')
    proof = dict(pid=123, platform='linux', process_birth='linux:fixture-boot:77', lock_inode=55)
    monkeypatch.setattr(b, '_native_proof', lambda h, p: dict(proof))
    manager = Manager(unit, [prologue])
    monkeypatch.setattr(b, '_busctl', manager.typed_exec)
    # Opaque prior state is never parsed or changed by bootstrap recovery.
    preserved = {}
    for name, data in [('watchdog-control/' + THREAD + '/owner.json', b'{"old-owner":"keep"}'),
                       ('runtime/inbox/reserved.json', b'uncertain effect must stay reserved'),
                       ('runtime/grants.bin', b'exact original grant bytes')]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        preserved[path] = data
    preserved[root / 'node.json'] = (root / 'node.json').read_bytes()
    return SimpleNamespace(home=home, root=root, unit=unit, env=env, exe=exe, manifest=manifest,
                           manager=manager, payload=payload, proof=proof, preserved=preserved,
                           repo=repo, prologue=prologue, monkeypatch=monkeypatch)


def ensure(s):
    return b.ensure_node_service(s.home, s.payload, run=s.manager)


def test_missing_search_path_recovers_original_service_once_and_preserves_state(configured):
    s = configured
    assert ensure(s) == {'status': 'started'}
    assert s.manager.effects() == [['daemon-reload'], ['start', s.unit.name]]
    assert ensure(s) == {'status': 'active'}
    assert len(s.manager.effects()) == 2
    assert json.loads((s.root / 'service-bootstrap.json').read_text())['phase'] == 'start_observed'
    for p, raw in s.preserved.items():
        assert p.read_bytes() == raw
    assert 'fixture-only' not in (s.root / 'service-bootstrap.json').read_text()


def test_active_service_needs_no_artifact_hash_or_new_state(configured):
    s = configured
    s.manager.load, s.manager.active, s.manager.pid = 'loaded', 'active', '444'
    s.manifest.unlink()
    assert ensure(s) == {'status': 'active'}
    assert s.manager.effects() == []
    assert not (s.root / 'service-bootstrap.json').exists()


@pytest.mark.parametrize('active', ['inactive', 'failed', 'activating', 'deactivating'])
def test_loaded_operator_stopped_or_uncertain_service_is_not_restarted(configured, active):
    s = configured
    s.manager.load, s.manager.active = 'loaded', active
    assert ensure(s)['status'] == 'refused'
    assert s.manager.effects() == []
    assert not (s.root / 'service-bootstrap.json').exists()


@pytest.mark.parametrize('fault', ['callback', 'unit', 'dropin', 'image', 'manifest', 'manifest_shape', 'environment',
                                  'oversized_unit', 'dropin_budget', 'directory_budget', 'interval',
                                  'enabled', 'wrong_node', 'install_symlink', 'record_symlink'])
def test_unverified_targets_refuse_without_service_effect(configured, fault, tmp_path):
    s = configured
    if fault == 'callback':
        def invalid(*_):
            raise cs.ControlError('linux_bootstrap_native_hook_unverified')
        s.monkeypatch.setattr(b, '_native_proof', invalid)
    elif fault == 'unit':
        s.unit.write_text(s.unit.read_text().replace('ConditionHost=' + NODE, 'ConditionHost=other.example'))
    elif fault == 'dropin':
        s.prologue.write_text('[Service]\nExecStartPre=/bin/unsafe\n')
    elif fault == 'image':
        s.exe.write_bytes(b'changed executable')
    elif fault == 'manifest':
        s.manifest.write_text('{}')
    elif fault == 'manifest_shape':
        s.manifest.write_text('[null]')
    elif fault == 'oversized_unit':
        s.unit.write_text('#' * (b.FILE_BYTES + 1))
    elif fault == 'dropin_budget':
        for i in range(32):
            (s.prologue.parent / ('extra-%02d.conf' % i)).write_text(b.NATIVE_PROLOGUE)
    elif fault == 'directory_budget':
        for i in range(64):
            (s.prologue.parent / ('unrelated-%02d' % i)).touch()
    elif fault == 'interval':
        s.unit.write_text(s.unit.read_text().replace('"30"', '"' + '9' * 5000 + '"'))
    elif fault == 'environment':
        s.env.chmod(0o644)
    elif fault == 'enabled':
        (s.unit.parent / 'default.target.wants' / s.unit.name).unlink()
    elif fault == 'wrong_node':
        cs.control_atomic_json(s.root / 'node.json', dict(schema_version=1, node='other', codex_home=str(s.home)))
    else:
        target = tmp_path / 'unrelated'
        target.write_bytes(b'preserve unrelated file')
        (s.root / ('install.lock' if fault == 'install_symlink' else 'service-bootstrap.json')).symlink_to(target)
    assert ensure(s)['status'] == 'refused'
    assert s.manager.effects() == []
    if fault in ('install_symlink', 'record_symlink'):
        assert target.read_bytes() == b'preserve unrelated file'


def test_competing_manual_agent_is_preserved(configured):
    s = configured
    p = s.root / 'watchdog-control/agent.lock'
    p.touch()
    from codex_watchdog import linux_owner
    s.monkeypatch.setattr(linux_owner, 'kernel_lock_owner', lambda _: 999)
    assert ensure(s)['reason'] == 'linux_bootstrap_competing_agent'
    assert s.manager.effects() == []


def test_concurrent_install_callback_does_not_start_a_second_actor(configured):
    s = configured
    with FileLock(s.root / 'install.lock'):
        assert ensure(s)['reason'] == 'linux_bootstrap_busy'
        assert s.manager.effects() == []
    assert ensure(s) == {'status': 'started'}
    assert s.manager.effects().count(['start', s.unit.name]) == 1


def test_later_genuine_callback_can_forward_complete_reload_without_repeating_it(configured):
    s = configured
    calls = [0]
    def proof(*_):
        calls[0] += 1
        return dict(s.proof, pid=888) if calls[0] == 3 else dict(s.proof)
    s.monkeypatch.setattr(b, '_native_proof', proof)
    assert ensure(s)['reason'] == 'linux_bootstrap_native_hook_changed'
    assert s.manager.effects() == [['daemon-reload']]
    assert json.loads((s.root / 'service-bootstrap.json').read_text())['phase'] == 'load_verified'
    assert ensure(s) == {'status': 'started'}
    assert s.manager.effects() == [['daemon-reload'], ['start', s.unit.name]]


def test_changed_configuration_after_reload_does_not_start_or_restore_it(configured):
    s = configured
    s.manager.after_reload = lambda: s.unit.write_text(s.unit.read_text() + '# owner change\n')
    assert ensure(s)['status'] == 'refused'
    assert s.manager.effects() == [['daemon-reload']]
    assert s.unit.read_text().endswith('# owner change\n')


def test_effective_manager_dropin_mismatch_refuses_start(configured):
    s = configured
    s.manager.after_reload = lambda: s.manager.drops.append(Path('/unverified/global.conf'))
    assert ensure(s)['reason'] == 'linux_bootstrap_effective_unit_unverified'
    assert s.manager.effects() == [['daemon-reload']]


def test_manager_change_during_payload_recheck_refuses_start(configured):
    s = configured
    original = b._unit_snapshot
    calls = [0]
    def changed(*args):
        snapshot = original(*args)
        calls[0] += 1
        if calls[0] == 2:
            s.manager.active, s.manager.pid = 'active', '777'
        return snapshot
    s.monkeypatch.setattr(b, '_unit_snapshot', changed)
    assert ensure(s)['reason'] == 'linux_bootstrap_manager_changed'
    assert s.manager.effects() == [['daemon-reload']]
    assert s.manager.pid == '777'


@pytest.mark.parametrize('fault', ['image', 'runtime', 'extra', 'split_path'])
def test_cached_effective_argv_mismatch_refuses_before_start(configured, fault):
    s = configured
    args = list(s.manager.arguments())
    if fault == 'image':
        args[2] = '/unreviewed/cached-monitor'
    elif fault == 'runtime':
        args[4] = '/different/runtime'
    elif fault == 'extra':
        args.append('--unreviewed')
    else:
        # Same flattened human argv text; different real string boundaries.
        args[2:3] = args[2].split(' ')
    s.manager.cached_arguments = args
    assert ensure(s)['reason'] == 'linux_bootstrap_effective_exec_unverified'
    assert s.manager.effects() == [['daemon-reload']]


def test_effective_argv_change_at_last_recheck_refuses_start(configured):
    s = configured
    def change(manager):
        if manager.typed_calls == 2:
            manager.cached_arguments = ['/unreviewed']
    s.manager.before_typed_read = change
    assert ensure(s)['reason'] == 'linux_bootstrap_effective_exec_unverified'
    assert s.manager.effects() == [['daemon-reload']]


@pytest.mark.parametrize('raw', ['a(sasbttttuii) 0', 'a(sasbttttuii) 2', 'invalid',
                                'x' * (b.OUTPUT_BYTES + 1)])
def test_malformed_or_unbounded_typed_exec_refuses(configured, raw):
    s = configured
    s.monkeypatch.setattr(b, '_busctl', lambda *_: raw)
    assert ensure(s)['status'] == 'refused'
    assert s.manager.effects() == [['daemon-reload']]


@pytest.mark.parametrize('fault', ['ignore_failure', 'prior_start', 'different_launcher', 'missing_client'])
def test_typed_command_policy_or_missing_tool_refuses_start(configured, fault):
    s = configured
    original = s.manager.typed_exec
    def unsafe(*args):
        if fault == 'missing_client':
            raise FileNotFoundError('fixture busctl unavailable')
        raw = original(*args)
        if fault == 'ignore_failure':
            return raw.replace(' false ', ' true ')
        if fault == 'prior_start':
            return raw.replace('false 0 0 0 0 0 0 0', 'false 1 1 0 0 0 0 0')
        return raw.replace('1 "/usr/bin/env"', '1 "/unreviewed/env"')
    s.monkeypatch.setattr(b, '_busctl', unsafe)
    assert ensure(s)['status'] == 'refused'
    assert s.manager.effects() == [['daemon-reload']]


@pytest.mark.parametrize('command', ['daemon-reload', 'start'])
def test_failed_effect_does_not_reset_state_or_loop(configured, command):
    s = configured
    s.manager.failed_command = command
    assert ensure(s)['reason'] == 'linux_bootstrap_systemctl_failed'
    assert s.manager.effects().count([command] if command == 'daemon-reload' else [command, s.unit.name]) == 1
    for p, raw in s.preserved.items():
        assert p.read_bytes() == raw


def test_installed_profile_pin_is_reused_when_bundle_manifest_is_not_installed(configured, tmp_path):
    s = configured
    s.manifest.unlink()
    config = tmp_path / 'launcher'
    config.mkdir()
    s.monkeypatch.setenv('CODEX_WATCHDOG_LINUX_CONFIG_DIR', str(config))
    profile = config / 'linux-launcher.json'
    profile.write_text(json.dumps(dict(schema_version=1, install_dir=str(s.exe.parent),
                                      codex_home=str(s.home), files={'codex-watchdog': hashlib.sha256(s.exe.read_bytes()).hexdigest()},
                                      future_choice='retain')))
    profile.chmod(0o600)
    before = profile.read_bytes()
    assert ensure(s) == {'status': 'started'}
    assert profile.read_bytes() == before


def test_bootstrap_is_reached_before_old_hook_owner_refusal(configured):
    from codex_watchdog import stop_hook
    s = configured
    s.monkeypatch.setattr(b, '_systemctl', s.manager)
    @contextmanager
    def old_owner(*_, **__):
        assert s.manager.active == 'active'
        raise cs.ControlError('control_stale_epoch')
        yield
    s.monkeypatch.setattr(stop_hook, 'hook_owner', old_owner)
    settings = stop_hook.HookSettings(s.root / 'runtime', grace_seconds=0, test_mode=True, codex_home=s.home)
    code, output, error = stop_hook.run_hook_text(s.payload, settings)
    assert code == 0 and json.loads(output) == {}
    assert 'control_stale_epoch' in error
    assert s.manager.effects() == [['daemon-reload'], ['start', s.unit.name]]
    for p, raw in s.preserved.items():
        assert p.read_bytes() == raw


def test_real_proof_rejects_unrelated_ancestry_before_capture(configured):
    from codex_watchdog import control_context, linux_owner, observed_writer
    s = configured
    s.monkeypatch.setattr(b, 'control_native_repo', lambda h, t: str(s.repo))
    s.monkeypatch.setattr(linux_owner, 'writer_pid', lambda *_: 123)
    s.monkeypatch.setattr(linux_owner, 'vscode_writer', lambda _: True)
    s.monkeypatch.setattr(control_context, '_hook_descends_from', lambda _: False)
    s.monkeypatch.setattr(observed_writer, 'capture_writer', lambda *_: pytest.fail('unrelated ancestry captured'))
    with pytest.raises(cs.ControlError, match='native_hook_unverified'):
        NATIVE_PROOF(s.home, s.payload)


def test_native_generation_is_rechecked_after_point_capture(configured):
    from codex_watchdog import control_context, linux_owner, observed_writer
    s = configured
    s.monkeypatch.setattr(b, 'control_native_repo', lambda h, t: str(s.repo))
    current = [123]
    s.monkeypatch.setattr(linux_owner, 'writer_pid', lambda *_: current[0])
    s.monkeypatch.setattr(linux_owner, 'vscode_writer', lambda _: True)
    s.monkeypatch.setattr(control_context, '_hook_descends_from', lambda _: True)
    def capture(*_):
        current[0] = None
        return {'writer': dict(s.proof)}
    s.monkeypatch.setattr(observed_writer, 'capture_writer', capture)
    with pytest.raises(cs.ControlError, match='native_hook_unverified'):
        NATIVE_PROOF(s.home, s.payload)
    assert s.manager.effects() == []


def test_optional_bootstrap_failure_keeps_original_hook_guard(configured):
    from codex_watchdog import stop_hook
    s = configured
    def broken(*_, **__):
        raise RuntimeError('fixture optional helper failure')
    s.monkeypatch.setattr(b, 'ensure_node_service', broken)
    reached = []
    @contextmanager
    def original(*_, **__):
        reached.append(True)
        raise cs.ControlError('control_stale_epoch')
        yield
    s.monkeypatch.setattr(stop_hook, 'hook_owner', original)
    settings = stop_hook.HookSettings(s.root / 'runtime', grace_seconds=0, test_mode=True, codex_home=s.home)
    code, output, error = stop_hook.run_hook_text(s.payload, settings)
    assert reached == [True] and code == 0 and json.loads(output) == {}
    assert 'bootstrap unavailable' in error and 'control_stale_epoch' in error
    assert s.manager.effects() == []


def test_budget_expiry_prevents_any_service_effect(configured):
    s = configured
    ticks = iter((0, 6))
    value = b.ensure_node_service(s.home, s.payload, run=s.manager, clock=lambda: next(ticks))
    assert value['reason'] == 'linux_bootstrap_deadline'
    assert s.manager.calls == []


@pytest.mark.parametrize('platform,event', [('darwin', 'Stop'), ('win32', 'Stop'), ('linux', 'Unrelated')])
def test_other_platforms_and_events_do_nothing(configured, platform, event):
    s = configured
    s.monkeypatch.setattr(b.sys, 'platform', platform)
    assert b.ensure_node_service(s.home, dict(s.payload, hook_event_name=event), run=s.manager) == {'status': 'skipped'}
    assert s.manager.calls == []


@pytest.mark.skipif(os.name == 'nt', reason='POSIX pipe read used only by Linux bootstrap')
@pytest.mark.parametrize('case', ['oversized', 'timeout'])
def test_systemctl_output_and_time_are_bounded_to_the_owned_child(monkeypatch, case):
    original = subprocess.Popen
    children = []
    def fixture_child(args, **kwargs):
        assert args[:3] == ['/usr/bin/systemctl', '--user', '--no-ask-password']
        code = "print('x' * 40000)" if case == 'oversized' else 'import time;time.sleep(10)'
        child = original([sys.executable, '-c', code], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(b.subprocess, 'Popen', fixture_child)
    start = time.monotonic()
    with pytest.raises(cs.ControlError):
        b._systemctl(['show', 'fixture.service'], start + 0.3, time.monotonic)
    assert time.monotonic() - start < 2
    assert len(children) == 1 and children[0].poll() is not None
