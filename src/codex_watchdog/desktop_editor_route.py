"""Fresh editor process candidates without a routing-log/history lookup.

These candidates are not an exact-thread proof. The observed-writer reader
must independently match one actual resource holder and its process birth.
Nothing here starts a client, reads a saved PID or changes native ownership.
"""
import sys

from .native_inflight import NativeInflightError
from .platform_adapters import detect_platform_adapter, run_vscode_status
from .vscode_processes import native_vscode_hosts
from .workspace_discovery import VSCodeLiveWindowIndex


def editor_pids(platform_adapter=None):
    if sys.platform == "win32":
        adapter = detect_platform_adapter() if platform_adapter is None else platform_adapter
        status = run_vscode_status(adapter)
        if not isinstance(status.output, str):
            raise NativeInflightError("desktop_editor_route_unavailable")
        # Parse the current process tree only. In particular do not call the
        # window index's log-directory/extension-log correlation on each send.
        windows = VSCodeLiveWindowIndex._parse_status(status.output)
        candidates = [pid for _, pid in windows.values() if pid is not None]
    elif sys.platform in ("linux", "darwin"):
        hosts = native_vscode_hosts()
        if hosts is None:
            raise NativeInflightError("desktop_editor_route_unavailable")
        # This existing OS reader rechecks actual parent/child generations and
        # the extension host's current open log. It does not read that log.
        candidates = [host.codex_pid for host in hosts]
    else:
        raise NativeInflightError("desktop_editor_route_unsupported")
    if any(type(pid) is not int or pid <= 0 for pid in candidates):
        raise NativeInflightError("desktop_editor_route_unavailable")
    if len(candidates) != len(set(candidates)):
        raise NativeInflightError("desktop_editor_route_ambiguous")
    return tuple(candidates)
