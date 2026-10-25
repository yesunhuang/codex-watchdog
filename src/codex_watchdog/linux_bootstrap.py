"""Bounded recovery of an opted-in missing user unit on a real native hook.

This starts only the retained systemd service. It neither enrolls a conversation
nor adopts an owner epoch. Fresh native-writer proof remains mandatory, and the
normal controller alone performs subsequent session/relay selection.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import sys
import time
import uuid

from .control_state import (ControlError, control_atomic_json, control_native_repo,
                            control_node_name, control_state_home)
from .linux_node import _unit_quote, node_unit_contents
from .storage import FileLock, StoreBusyError


BOOTSTRAP_SECONDS = 5.0
OUTPUT_BYTES = 16384
FILE_BYTES = 65536
IMAGE_BYTES = 128 * 1024 * 1024
PROPERTIES = ('LoadState,ActiveState,SubState,MainPID,FragmentPath,DropInPaths,'
              'KillMode,TimeoutStopUSec,Restart,ExecMainStartTimestamp,Job,ExecStart')
NATIVE_PROLOGUE = ('[Service]\nExecStartPre=/usr/bin/mkdir -p -m 0700 '
                   '%t/codex-native-runtime/thread-writer-locks %t/codex-native-runtime/tmp\n')


def _need(value, reason):
    if not value:
        raise ControlError('linux_bootstrap_' + reason)


def _tick(deadline, clock):
    _need(clock() < deadline, 'deadline')


def _private_file(path, limit=FILE_BYTES, *, private=False):
    path = Path(path)
    before = path.stat()
    _need(not path.is_symlink() and path.resolve() == path and stat.S_ISREG(before.st_mode)
          and before.st_uid == os.getuid() and before.st_nlink == 1
          and not before.st_mode & (0o077 if private else 0o022),
          'file_identity')
    _need(before.st_size <= limit, 'file_oversized')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    after = path.stat()
    _need(len(raw) <= limit and _identity(before) == _identity(after), 'file_changed')
    return raw


def _directory(path):
    info = path.stat()
    _need(not path.is_symlink() and path.resolve() == path and stat.S_ISDIR(info.st_mode)
          and info.st_uid == os.getuid() and not info.st_mode & 0o022, 'directory_unverified')


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _native_proof(home, payload):
    from .control_context import _hook_descends_from
    from .linux_owner import writer_pid, vscode_writer
    from .observed_writer import capture_writer
    thread = payload.get('session_id')
    _need(isinstance(thread, str) and str(uuid.UUID(thread)) == thread, 'thread_unverified')
    cwd = payload.get('cwd')
    _need(isinstance(cwd, str) and Path(cwd).is_absolute(), 'repo_unverified')
    repo = Path(cwd).resolve()
    _need(control_native_repo(home, thread) == str(repo), 'repo_unverified')
    pid = writer_pid(home, thread)
    _need(pid is not None and vscode_writer(pid) and _hook_descends_from(pid), 'native_hook_unverified')
    proof = capture_writer(home, thread, repo, pid)
    _need(proof['writer']['platform'] == 'linux' and writer_pid(home, thread) == pid
          and vscode_writer(pid) and _hook_descends_from(pid), 'native_hook_unverified')
    return proof['writer']


def _command_output(arguments, deadline, clock):
    """Bound bytes/time while reading; kill only this owned manager CLI child."""
    _tick(deadline, clock)
    child = subprocess.Popen(arguments,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, close_fds=True)
    chunks = {child.stdout: bytearray(), child.stderr: bytearray()}
    total = 0
    try:
        with selectors.DefaultSelector() as ready:
            for stream in chunks:
                ready.register(stream, selectors.EVENT_READ)
            while ready.get_map():
                _tick(deadline, clock)
                events = ready.select(max(0, deadline - clock()))
                _need(events, 'systemctl_timeout')
                for key, _ in events:
                    block = os.read(key.fileobj.fileno(), 4096)
                    if not block:
                        ready.unregister(key.fileobj)
                        continue
                    total += len(block)
                    _need(total <= OUTPUT_BYTES, 'systemctl_output_oversized')
                    chunks[key.fileobj].extend(block)
        _tick(deadline, clock)
        code = child.wait(timeout=max(0.001, deadline - clock()))
        _need(code == 0, 'systemctl_failed')
        return bytes(chunks[child.stdout]).decode('utf-8', errors='strict')
    finally:
        if child.poll() is None:
            child.kill()
            # The service is owned by systemd, never by this inspection child.
        child.stdout.close()
        child.stderr.close()
        try:
            child.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass


def _systemctl(arguments, deadline, clock):
    return _command_output(['/usr/bin/systemctl', '--user', '--no-ask-password', *arguments],
                           deadline, clock)


def _busctl(unit, deadline, clock):
    # systemd's unit object path escapes every non-ASCII-alphanumeric byte.
    path = '/org/freedesktop/systemd1/unit/' + ''.join(
        char if char.isascii() and char.isalnum() else '_%02x' % ord(char) for char in unit)
    return _command_output(['/usr/bin/busctl', '--user', 'get-property',
                            'org.freedesktop.systemd1', path,
                            'org.freedesktop.systemd1.Service', 'ExecStart'], deadline, clock)


def _bus_tokens(raw):
    fields, position = [], 0
    for match in re.finditer(r'"(?:[^"\\\x00-\x1f]|\\[^\r\n])*"|[^\s"]+', raw):
        gap = raw[position:match.start()]
        _need(gap.isspace() or not fields and not gap, 'effective_exec_unverified')
        fields.append(match.group())
        _need(len(fields) <= 64, 'effective_exec_unverified')
        position = match.end()
    _need(position == len(raw) or raw[position:].isspace(), 'effective_exec_unverified')
    return fields


def _bus_string(token):
    _need(len(token) >= 2 and token[0] == token[-1] == '"', 'effective_exec_unverified')
    raw, result, position = token[1:-1].encode('utf-8'), bytearray(), 0
    escapes = {ord(a): ord(b) for a, b in zip('abfnrtv\\\"\'', '\a\b\f\n\r\t\v\\\"\'')}
    while position < len(raw):
        char = raw[position]
        position += 1
        if char == 92:
            _need(position < len(raw), 'effective_exec_unverified')
            char = raw[position]
            position += 1
            if char in escapes:
                char = escapes[char]
            elif char == ord('x'):
                digits = raw[position:position + 2]
                _need(re.fullmatch(b'[0-9a-fA-F]{2}', digits), 'effective_exec_unverified')
                char, position = int(digits, 16), position + 2
            elif ord('0') <= char <= ord('7'):
                digits = bytes([char]) + raw[position:position + 2]
                _need(re.fullmatch(b'[0-7]{3}', digits), 'effective_exec_unverified')
                char, position = int(digits, 8), position + 2
            else:
                _need(False, 'effective_exec_unverified')
        _need(0 < char <= 255, 'effective_exec_unverified')
        result.append(char)
    return result.decode('utf-8', errors='strict')


def _effective_exec(unit, expected, deadline, clock):
    _tick(deadline, clock)
    raw = _busctl(unit, deadline, clock)
    _need(len(raw.encode()) <= OUTPUT_BYTES, 'effective_exec_oversized')
    # Typed D-Bus output retains argv cardinality and quoted string boundaries;
    # systemctl's human-readable argv[] joins strings and loses that distinction.
    fields = _bus_tokens(raw)
    end = 4 + len(expected)
    _need(len(fields) == end + 8 and fields[:2] == ['a(sasbttttuii)', '1']
          and _bus_string(fields[2]) == '/usr/bin/env' and fields[3] == str(len(expected))
          and [_bus_string(field) for field in fields[4:end]] == expected and fields[end] == 'false'
          and fields[end + 1:] == ['0'] * 7, 'effective_exec_unverified')
    return fields


def _state(unit, run, deadline, clock):
    _tick(deadline, clock)
    raw = run(['show', unit, '--property=' + PROPERTIES], deadline, clock)
    _need(len(raw.encode()) <= OUTPUT_BYTES, 'systemctl_output_oversized')
    rows = [line.split('=', 1) for line in raw.splitlines()]
    _need(all(len(row) == 2 for row in rows) and len({row[0] for row in rows}) == len(rows),
          'manager_state_unverified')
    value = dict(rows)
    _need(set(value) == set(PROPERTIES.split(',')), 'manager_state_unverified')
    return value


def _command(line, home, runtime):
    _need(line.startswith('ExecStart='), 'unit_unsupported')
    args = shlex.split(line[len('ExecStart='):])
    _need(len(args) == 12 and args[:2] == ['/usr/bin/env', 'CODEX_WATCHDOG_SLACK_REPLY_MODE=poll']
          and args[3:9] == ['--runtime', str(runtime), '--codex-home', str(home), 'linux-auto-run', '--interval']
          and args[10:] == ['--renew-lease', '--continue-interrupted']
          and re.fullmatch('[1-9][0-9]?', args[9]) and int(args[9]) <= 60,
          'unit_unsupported')
    _need(line == 'ExecStart=' + ' '.join(_unit_quote(a, argument=True) for a in args),
          'unit_unsupported')
    path = Path(args[2])
    _need(path.is_absolute() and '..' not in path.parts and not any(c in str(path) for c in '$%\n\r'),
          'unit_unsupported')
    return path, int(args[9])


def _unit_snapshot(unit, node, home, root, deadline, clock):
    _directory(unit.parent)
    raw = _private_file(unit)
    text = raw.decode('utf-8')
    commands = [line for line in text.splitlines() if line.startswith('ExecStart=')]
    envs = [line[len('EnvironmentFile='):] for line in text.splitlines() if line.startswith('EnvironmentFile=')]
    _need(len(commands) == len(envs) == 1, 'unit_unsupported')
    environment = Path(envs[0])
    _need(environment.is_absolute() and not any(c in str(environment) for c in '*?[]%\n\r\t '),
          'environment_unverified')
    selected, interval = _command(commands[0], home, root / 'runtime')
    selected_command = commands[0]
    _need(text == node_unit_contents(node, selected, root / 'runtime', home, environment, interval),
          'unit_unsupported')
    values = {str(unit): hashlib.sha256(raw).hexdigest(),
              str(environment): hashlib.sha256(_private_file(environment, private=True)).hexdigest()}
    directory = unit.with_name(unit.name + '.d')
    drops = []
    if directory.exists():
        _directory(directory)
        for index, path in enumerate(directory.iterdir()):
            _need(index < 64, 'dropin_directory_budget')
            _tick(deadline, clock)
            if path.name.endswith('.conf'):
                drops.append(path)
                _need(len(drops) <= 32, 'dropin_budget')
    for path in sorted(drops):
        raw = _private_file(path)
        text = raw.decode('utf-8')
        values[str(path)] = hashlib.sha256(raw).hexdigest()
        if text == NATIVE_PROLOGUE:
            continue
        lines = [line for line in text.splitlines() if line and not line.startswith('#')]
        _need(len(lines) == 3 and lines[:2] == ['[Service]', 'ExecStart='], 'dropin_unsupported')
        selected, _ = _command(lines[2], home, root / 'runtime')
        selected_command = lines[2]
    manifest = selected.parent / 'package-manifest.json'
    if manifest.exists() or manifest.is_symlink():
        package = json.loads(_private_file(manifest))
        _need(isinstance(package, dict) and type(package.get('schema_version')) is int and package['schema_version'] == 1
              and package.get('platform') == 'linux', 'image_unverified')
    else:
        from .linux_package import config_directory
        manifest = config_directory() / 'linux-launcher.json'
        package = json.loads(_private_file(manifest, private=True))
        _need(isinstance(package, dict) and type(package.get('schema_version')) is int and package['schema_version'] == 1
              and package.get('install_dir') == str(selected.parent)
              and package.get('codex_home') == str(home), 'image_unverified')
    _need(isinstance(package.get('files'), dict), 'image_unverified')
    expected = package['files'].get('codex-watchdog')
    _need(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected)
          and selected.name == 'codex-watchdog', 'image_unverified')
    identity = selected.stat()
    _need(not selected.is_symlink() and selected.resolve() == selected and stat.S_ISREG(identity.st_mode)
          and identity.st_uid == os.getuid() and not identity.st_mode & 0o022
          and identity.st_mode & 0o111 and identity.st_size <= IMAGE_BYTES, 'image_unverified')
    digest = hashlib.sha256()
    total = 0
    with selected.open('rb') as stream:
        while True:
            _tick(deadline, clock)
            block = stream.read(128 * 1024)
            if not block:
                break
            total += len(block)
            _need(total <= IMAGE_BYTES, 'image_oversized')
            digest.update(block)
    _need(digest.hexdigest() == expected, 'image_unverified')
    after = selected.stat()
    _need(_identity(identity) == _identity(after), 'image_changed')
    values[str(manifest)] = hashlib.sha256(_private_file(manifest)).hexdigest()
    values[str(selected)] = expected
    return values, drops, shlex.split(selected_command[len('ExecStart='):])


def _missing(state):
    return (state['LoadState'] == 'not-found' and state['ActiveState'] == 'inactive'
            and state['SubState'] == 'dead' and state['MainPID'] == '0' and state['Job'] in ('', '0'))


def _unfinished_reload(state, prior, boot):
    # Only forward-complete this helper's never-started current-boot recovery.
    # A previously running/stopped/failed service is not a bootstrap target.
    return (prior.get('boot_id') == boot
            and prior.get('phase') in ('reload_intent', 'load_verified', 'start_intent')
            and state['LoadState'] == 'loaded' and state['ActiveState'] == 'inactive'
            and state['SubState'] == 'dead' and state['MainPID'] == '0'
            and not state['ExecMainStartTimestamp'] and state['Job'] in ('', '0'))


def _read_record(path):
    value = json.loads(_private_file(path, private=True)) if path.exists() or path.is_symlink() else {}
    _need(isinstance(value, dict) and (not value or type(value.get('schema_version')) is int
                                     and value['schema_version'] == 1), 'record_unsupported')
    return value


def ensure_node_service(home, payload, *, run=None, clock=time.monotonic):
    """No effect on other platforms, unconfigured hosts or active services."""
    if sys.platform != 'linux' or payload.get('hook_event_name') not in ('Stop', 'PermissionRequest'):
        return dict(status='skipped')
    run = _systemctl if run is None else run
    attempted = False
    try:
        home = Path(home).resolve()
        root = control_state_home(home)
        if root == home:
            return dict(status='skipped')
        node = control_node_name()
        _directory(root)
        marker = json.loads(_private_file(root / 'node.json', private=True))
        _need(type(marker.get('schema_version')) is int and marker['schema_version'] == 1,
              'node_unverified')
        config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
        _need(config.is_absolute() and config.resolve() == config, 'config_unverified')
        unit = config / 'systemd/user' / ('codex-watchdog-node-' + node + '.service')
        if not unit.exists():
            return dict(status='skipped')
        deadline = clock() + BOOTSTRAP_SECONDS
        proof = _native_proof(home, payload)
        boot = proof['process_birth'].split(':', 2)[1]
        before = _state(unit.name, run, deadline, clock)
        if before['LoadState'] == 'loaded' and before['ActiveState'] == 'active':
            return dict(status='active')
        record = root / 'service-bootstrap.json'
        prior = _read_record(record)
        if not _missing(before) and not _unfinished_reload(before, prior, boot):
            return dict(status='refused', reason='linux_bootstrap_not_missing_unit')
        enabled = unit.parent / 'default.target.wants' / unit.name
        _need(enabled.is_symlink() and enabled.resolve() == unit, 'unit_not_enabled')
        install_lock = root / 'install.lock'
        if install_lock.exists() or install_lock.is_symlink():
            _private_file(install_lock, 4096)
        with FileLock(install_lock):
            state = _state(unit.name, run, deadline, clock)
            if state['LoadState'] == 'loaded' and state['ActiveState'] == 'active':
                return dict(status='active')
            prior = _read_record(record)
            missing = _missing(state)
            _need(missing or _unfinished_reload(state, prior, boot), 'manager_changed')
            from .linux_owner import kernel_lock_owner
            agent = root / 'watchdog-control/agent.lock'
            if agent.exists() or agent.is_symlink():
                _private_file(agent, 4096)
            _need(not agent.exists() or kernel_lock_owner(agent) is None, 'competing_agent')
            pins, drops, arguments = _unit_snapshot(unit, node, home, root, deadline, clock)
            pin_digest = hashlib.sha256(json.dumps(pins, sort_keys=True).encode()).hexdigest()
            _need(missing or prior.get('unit_snapshot_sha256') == pin_digest, 'unit_changed')
            _need(_native_proof(home, payload) == proof, 'native_hook_changed')
            receipt = dict(prior, schema_version=1, attempt=str(uuid.uuid4()), node=node,
                           boot_id=boot, phase='reload_intent' if missing else 'load_verified',
                           unit_sha256=pins[str(unit)], unit_snapshot_sha256=pin_digest)
            control_atomic_json(record, receipt)
            attempted = True
            if missing:
                run(['daemon-reload'], deadline, clock)
            loaded = _state(unit.name, run, deadline, clock)
            _need(loaded['LoadState'] == 'loaded' and loaded['ActiveState'] == 'inactive'
                  and loaded['MainPID'] == '0' and loaded['FragmentPath'] == str(unit)
                  and loaded['DropInPaths'] == ' '.join(str(p) for p in sorted(drops))
                  and loaded['KillMode'] == 'mixed' and loaded['TimeoutStopUSec'] == 'infinity'
                  and loaded['Restart'] == 'no' and not loaded['ExecMainStartTimestamp'] and loaded['Job'] in ('', '0')
                  and loaded['ExecStart'],
                  'effective_unit_unverified')
            effective = _effective_exec(unit.name, arguments, deadline, clock)
            receipt['phase'] = 'load_verified'
            control_atomic_json(record, receipt)
            _need(_unit_snapshot(unit, node, home, root, deadline, clock)[0] == pins, 'unit_changed')
            _need(_native_proof(home, payload) == proof, 'native_hook_changed')
            _need(not agent.exists() or kernel_lock_owner(agent) is None, 'competing_agent')
            _need(enabled.is_symlink() and enabled.resolve() == unit, 'unit_not_enabled')
            _need(_state(unit.name, run, deadline, clock) == loaded, 'manager_changed')
            _need(_effective_exec(unit.name, arguments, deadline, clock) == effective,
                  'effective_exec_changed')
            receipt['phase'] = 'start_intent'
            control_atomic_json(record, receipt)
            run(['start', unit.name], deadline, clock)
            after = _state(unit.name, run, deadline, clock)
            _need(after['LoadState'] == 'loaded' and after['ActiveState'] in ('active', 'activating'),
                  'start_unverified')
            receipt['phase'] = 'start_observed'
            control_atomic_json(record, receipt)
            return dict(status='started')
    except StoreBusyError:
        return dict(status='refused', reason='linux_bootstrap_busy')
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, ControlError) else 'linux_bootstrap_unavailable'
        return dict(status='refused', reason=reason, attempted=attempted)
