"""An opt-in systemd user unit and state directory for this native Linux host."""

from pathlib import Path
import os
import stat
import sys

from .control_state import (
    ControlError, control_atomic_json, control_file_lock, control_node_directory,
    control_node_name, control_read_json, control_state_home,
)
from .linux_binding import locality_identity


def _unit_quote(value, *, argument=False):
    # systemd has its own escaping and specifiers; this is not a shell command.
    text = str(value)
    if any(ord(c) < 32 for c in text):
        raise ControlError("linux_node_unit_value_invalid")
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (text.replace("$", "$$") if argument else text) + '"'


def prepare_node(executable, codex_home, environment_file, *, interval=30, install=False):
    if sys.platform != "linux":
        raise ControlError("linux_required")
    if type(interval) is not int or not 1 <= interval <= 60:
        raise ControlError("control_interval_must_be_1_to_60_seconds")
    home = Path(codex_home).resolve()
    executable = Path(executable).resolve(strict=True)
    environment = Path(environment_file).resolve(strict=True)
    if (str(environment) != str(environment).strip()
            or any(ord(c) < 32 or c in "*?[" for c in str(environment))):
        raise ControlError("linux_node_environment_path_unsupported")
    info = environment.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ControlError("linux_node_environment_not_private")
    if not executable.is_file() or not os.access(str(executable), os.X_OK):
        raise ControlError("linux_node_executable_unavailable")
    node = control_node_name()
    root = control_node_directory(home)
    configured = control_state_home(home) != home
    if not configured:
        # An existing node's saved bindings require an explicit, reviewed
        # migration. New nodes must never adopt another node's shared state.
        for path in (home / "watchdog-linux").glob("*.json"):
            if control_read_json(path).get("locality_sha256") == locality_identity():
                raise ControlError("linux_node_legacy_migration_required")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        for path in (home / "watchdog-control").glob("*/owner.json"):
            previous = control_read_json(path)
            if ((previous.get("owner") or {}).get("boot_id") == boot
                    or previous.get("native_boot_id") == boot):
                raise ControlError("linux_node_legacy_migration_required")
    name = "codex-watchdog-node-" + node + ".service"
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    if not config.is_absolute():
        raise ControlError("linux_node_config_not_absolute")
    unit = config / "systemd/user" / name
    runtime = root / "runtime"
    args = ["/usr/bin/env", "CODEX_WATCHDOG_SLACK_REPLY_MODE=poll",
            executable, "--runtime", runtime, "--codex-home", home, "linux-auto-run",
            "--interval", str(interval), "--renew-lease", "--continue-interrupted"]
    contents = (
        "# Managed by Codex WatchDog linux-node-install; schema_version=1\n"
        "[Unit]\nDescription=Codex WatchDog on " + node + "\nConditionHost=" + node + "\n\n"
        "[Service]\nType=simple\nUMask=0077\n"
        "EnvironmentFile=" + str(environment).replace("%", "%%") + "\n"
        "ExecStart=" + " ".join(_unit_quote(value, argument=True) for value in args) + "\n"
        "Restart=no\nKillMode=mixed\nTimeoutStopSec=infinity\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    marker = root / "node.json"
    result = dict(schema_version=1, node=node, service=name, runtime=str(runtime),
                  status="preview", unit=contents)
    if not install:
        return result
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.resolve() != root or root.is_symlink():
        raise ControlError("control_node_configuration_mismatch")
    with control_file_lock(root / "install.lock"):
        value = dict(schema_version=1, node=node, codex_home=str(home))
        if marker.exists():
            previous = control_read_json(marker)
            if any(previous.get(k) != v for k, v in value.items()):
                raise ControlError("control_node_configuration_mismatch")
            value = previous  # Preserve future/unknown nonconflicting keys.
        if unit.exists() and (unit.is_symlink() or unit.read_text(encoding="utf-8") != contents):
            raise ControlError("linux_node_existing_unit_conflict")
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        unit.parent.mkdir(parents=True, exist_ok=True)
        if not unit.exists():
            with unit.open("x", encoding="utf-8") as handle:
                handle.write(contents)
            unit.chmod(0o600)
        # Activate path selection only after the complete unit exists. Neither
        # writing nor enabling this host-conditioned unit affects other nodes.
        if not marker.exists():
            control_atomic_json(marker, value)
    result.update(status="installed", unit_path=str(unit))
    return result
