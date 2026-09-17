"""No-argument Mac ZIP launcher acceptance in an isolated home and real TTY."""
from pathlib import Path
import argparse
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def check(package):
    assert sys.platform == 'darwin', 'Native macOS acceptance required'
    package = package.resolve()
    name = 'Install and Start Codex WatchDog.command'
    with tempfile.TemporaryDirectory(prefix='watchdog-finder-launch-') as directory:
        root = Path(directory).resolve()
        home = root / 'user home with spaces'
        work = root / 'unrelated working directory'
        home.mkdir(); work.mkdir()
        bundle = root / 'download with spaces'
        shutil.copytree(package, bundle)
        launcher = bundle / name
        subprocess.run(['/bin/bash', '-n', str(launcher)], check=True)
        assert os.access(launcher, os.X_OK)
        env = {k:v for k,v in os.environ.items()
               if not k.startswith(('CODEX_WATCHDOG_', 'PYTHON')) and k != 'CODEX_HOME'}
        config = home / 'custom config with spaces'
        codex = home / '.codex'
        codex.mkdir()
        (codex / 'config.toml').write_text('# existing Codex trust remains unchanged\n')
        (codex / 'hooks.json').write_text(json.dumps({'future': 'preserve', 'hooks': {
            'Stop': [{'hooks': [{'type': 'command', 'command': '/usr/bin/true'}]}]}}))
        trust = (codex / 'config.toml').read_bytes()
        security = root / 'empty keychain fixture'
        security.write_text('#!/bin/sh\nexit 44\n'); security.chmod(0o700)
        path = root / 'path without python'; path.mkdir()
        for tool in ('bash','cat','uname','dirname','date','git'):
            source = '/bin/' + tool if tool in ('bash','cat','date') else '/usr/bin/' + tool
            (path / tool).symlink_to(source)
        env.update(HOME=str(home), PATH=str(path), CODEX_HOME=str(codex),
                   CODEX_WATCHDOG_MACOS_CONFIG_DIR=str(config),
                   CODEX_WATCHDOG_MACOS_SECURITY_BIN=str(security))
        profile_file = config / 'macos-launcher.json'
        marker = config / 'messaging-setup.json'
        protected = None

        def installed_before_pairing():
            value = json.loads(profile_file.read_text())
            assert value['version'] == json.loads((bundle / 'package-manifest.json').read_text())['version']
            assert Path(value['runtime']) == config / 'runtime'
            assert (Path(value['install_dir']) / 'codex-watchdog').is_file()
            document = json.loads((codex / 'hooks.json').read_text())
            assert document['future'] == 'preserve'
            assert document['hooks']['Stop'][0]['hooks'][0]['command'] == '/usr/bin/true'
            own = [hook for groups in document['hooks'].values() for group in groups
                   for hook in group['hooks'] if 'codex-watchdog' in hook['command']]
            assert len(own) == 2
            assert all(shlex.split(hook['command'])[0] == str(Path(value['install_dir'])/'codex-watchdog') for hook in own)
            assert (codex / 'config.toml').read_bytes() == trust

        def launch(choice=None, busy_check=False):
            import pty
            child, master = pty.fork()
            if child == 0:
                os.chdir(work)
                os.execve(launcher, [str(launcher)], env)
            output, status, chosen, stopped, cycle = b'', None, False, False, False
            acknowledged_error = False
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try: output += os.read(master, 8192)
                        except OSError: pass
                    if b'Choose 1-5:' in output and not chosen:
                        assert choice is not None, 'Saved setup prompted again'
                        installed_before_pairing()
                        if protected:
                            assert all(p.read_bytes() == raw for p,raw in protected.items())
                        os.write(master, {'cancel': b'\x03', 'eof': b'\x04'}.get(choice, b'4\n'))
                        chosen = True
                    if b'Press Return to close this launcher.' in output and not acknowledged_error:
                        os.write(master, b'\n')
                        acknowledged_error = True
                    if b'"workspace_count": 0' in output and not cycle:
                        cycle = True
                        assert choice not in ('cancel', 'eof')
                        if busy_check:
                            before = {p:p.read_bytes() for p in (profile_file, codex/'hooks.json', marker)}
                            second = subprocess.run([str(launcher)], cwd=work, env=env, stdin=subprocess.DEVNULL,
                                                    capture_output=True, text=True, timeout=20)
                            assert second.returncode == 1 and 'macos_install_busy' in second.stderr
                            assert 'Choose 1-5:' not in second.stdout
                            assert all(p.read_bytes() == raw for p,raw in before.items())
                        os.write(master, b'\x03'); stopped = True
                    pid, exit_status = os.waitpid(child, os.WNOHANG)
                    if pid:
                        status = exit_status
                        break
                assert status is not None and os.waitstatus_to_exitcode(status) in (0,130,-signal.SIGINT), output.decode(errors='replace')
                if choice in ('cancel', 'eof'): assert chosen and not cycle
                else: assert cycle and stopped
                if choice is None: assert b'Choose 1-5:' not in output
            finally:
                if status is None:
                    os.killpg(child, signal.SIGTERM)
                    os.waitpid(child, 0)
                os.close(master)

        launch('cancel')
        assert json.loads(marker.read_text())['status'] == 'cancelled'
        protected = {p:p.read_bytes() for p in (profile_file, codex/'hooks.json', codex/'config.toml')}
        launch('eof')
        assert json.loads(marker.read_text())['status'] == 'cancelled'
        launch('skip', busy_check=True)
        assert json.loads(marker.read_text())['status'] == 'skipped'
        protected[marker] = marker.read_bytes()
        launch()
        assert all(p.read_bytes() == raw for p,raw in protected.items())
    return dict(schema_version=1,status='passed',no_argument_real_tty=True,
                extracted_path_with_spaces=True,unrelated_working_directory=True,
                python_hidden=True,install_and_hooks_before_pairing=True,
                cancellation_retried=True,ctrl_c_and_eof_cancel_cleanly=True,skip_preserved_on_relaunch=True,
                busy_duplicate_refused=True,custom_config_preserved=True,
                unrelated_hooks_and_codex_trust_preserved=True,
                provider_contacted=False,production_state_modified=False)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path,required=True)
    print(json.dumps(check(parser.parse_args().package),sort_keys=True))
