"""A short local label, never an authorization token or provider routing key."""
import hashlib
import json
import re
import secrets
import socket

from .storage import FileLock, InstructionStore


def device_label(runtime, *, hostname=None):
    path = runtime / "messaging-device.json"
    with FileLock(runtime / "locks" / "messaging-device.lock"):
        if path.is_symlink():
            raise ValueError("messaging_device_identity_invalid")
        if not path.exists():
            InstructionStore._atomic_json(path, dict(schema_version=1, seed=secrets.token_hex(16)))
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError("messaging_device_identity_invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1 or
                not isinstance(value.get("seed"), str) or not re.fullmatch(r"[a-f0-9]{32}", value["seed"])):
            raise ValueError("messaging_device_identity_invalid")
    # The hostname distinguishes nodes sharing a home/runtime. The random seed
    # distinguishes unrelated installations using the same hostname. No MAC or
    # raw hostname is stored. Renaming a host never moves its polling cursor.
    name = socket.gethostname() if hostname is None else hostname
    return hashlib.sha256((name.casefold() + "\0" + value["seed"]).encode()).hexdigest()[:12]
