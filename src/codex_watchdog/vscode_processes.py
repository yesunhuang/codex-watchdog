"""Read-only POSIX evidence independent of VS Code's status process tree."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Dict, Optional, Tuple

from .platform_adapters import is_codex_app_server_description


@dataclass(frozen=True)
class NativeVSCodeHost:
    pid: int
    codex_pid: int
    log: Path


def _processes() -> Dict[int, Tuple[int, str, str]]:
    result = subprocess.run(
        ['ps', '-axww', '-o', 'uid=,pid=,ppid=,lstart=,command='],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=10,
        env={**os.environ, 'LC_ALL': 'C'},
    )
    result.check_returncode()
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 8)
        if len(parts) != 9 or not all(value.isdigit() for value in parts[:3]):
            continue
        if int(parts[0]) == os.getuid():
            rows[int(parts[1])] = (int(parts[2]), ' '.join(parts[3:8]), parts[8])
    return rows


def _host_command(command: str) -> bool:
    # Modern VS Code runs extension hosts as Electron node utility processes.
    # The exact open exthost log below distinguishes them from other utilities.
    return (
        '--utility-sub-type=node.mojom.NodeService' in command
        or re.search(r'(?:^|\s)--type=extensionHost(?:\s|$)', command) is not None
    )


def _open_logs(pids: Tuple[int, ...]) -> Dict[int, Tuple[Path, ...]]:
    if sys.platform == 'linux':
        result = {}
        for pid in pids:
            paths = set()
            for fd in (Path('/proc') / str(pid) / 'fd').iterdir():
                try:
                    path = Path(os.readlink(fd))
                except OSError:
                    continue
                if path.name == 'exthost.log':
                    paths.add(path)
            result[pid] = tuple(paths)
        return result
    result = subprocess.run(
        ['/usr/sbin/lsof', '-a', '-p', ','.join(map(str, pids)), '-Fpn'],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=10,
    )
    result.check_returncode()
    paths_by_pid = {pid: set() for pid in pids}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            current = int(line[1:])
        elif current in paths_by_pid and line.startswith('n/'):
            path = Path(line[1:])
            if path.name == 'exthost.log':
                paths_by_pid[current].add(path)
    return {pid: tuple(paths) for pid, paths in paths_by_pid.items()}


def native_vscode_hosts() -> Optional[Tuple[NativeVSCodeHost, ...]]:
    """Require live parentage, an open log and unchanged process generations.

    No processes, locks, logs or conversations are changed. Missing native
    evidence cannot authorize a target. Windows keeps its existing status path.
    """
    if sys.platform not in ('darwin', 'linux'):
        return ()
    try:
        before = _processes()
        children: Dict[int, list] = {}
        # Link after collecting every row: child-before-parent order is valid.
        for pid, (parent, _started, command) in before.items():
            host = before.get(parent)
            if host and _host_command(host[2]) and is_codex_app_server_description(command):
                children.setdefault(parent, []).append(pid)
        candidates = {pid: ids[0] for pid, ids in children.items() if len(ids) == 1}
        if not candidates:
            return ()
        if len(candidates) > 64:
            return None
        logs = _open_logs(tuple(candidates))
        after = _processes()
        return tuple(
            NativeVSCodeHost(pid, child, logs[pid][0])
            for pid, child in candidates.items()
            if len(logs.get(pid, ())) == 1
            and before[pid] == after.get(pid)
            and before[child] == after.get(child)
        )
    except (OSError, subprocess.SubprocessError):
        return None
