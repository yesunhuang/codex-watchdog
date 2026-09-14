from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import re
from typing import Any, Dict, Optional
import uuid

from .models import sha256_text
from .control_state import control_root, control_read_json
from .remote_control import RemoteControlClient


_DELIVERED_STATES = frozenset({"enqueued", "consumed_or_started", "started"})


def reply_relay_from_config(runtime, config, *, queue_dispatcher, remote_ssh_adapter):
    if getattr(config, "selected_interactive_transport", "slack") == "both":
        relays = [reply_relay_from_config(
            runtime, replace(config, interactive_transport=provider),
            queue_dispatcher=queue_dispatcher, remote_ssh_adapter=remote_ssh_adapter)
            for provider in ("slack", "lark")]
        relays = [relay for relay in relays if relay is not None]
        return CombinedReplyRelays(relays) if relays else None
    if getattr(config, "selected_interactive_transport", "slack") == "lark":
        from .lark_relay import LarkReplyRelay
        if not config.lark.relay_configured:
            return None
        return LarkReplyRelay(runtime, config.lark, queue_dispatcher=queue_dispatcher,
                              remote_ssh_adapter=remote_ssh_adapter, timeout=config.timeout_seconds)
    from .slack_relay import SlackReplyRelay
    return SlackReplyRelay.from_notification_config(
        runtime, config, queue_dispatcher=queue_dispatcher, remote_ssh_adapter=remote_ssh_adapter)


class CombinedThreadStores:
    """Expose both providers' existing mapping envelopes to the ownership fence."""

    def __init__(self, *stores):
        self.stores = tuple(store for store in stores if store is not None)

    def notification_mappings(self, fingerprint):
        return [entry for store in self.stores for entry in store.notification_mappings(fingerprint)]

    def has_notification_mapping(self, fingerprint):
        return bool(self.notification_mappings(fingerprint))

    def mappings_for_threads(self, thread_ids):
        return [entry for store in self.stores for entry in store.mappings_for_threads(thread_ids)]

    def cache_mappings(self, entries, target):
        entries = tuple(entries)
        for store in self.stores:
            store.cache_mappings(entries, target)


class CombinedReplyRelays:
    """Two existing provider listeners, sharing one monitor and Codex dispatcher."""

    def __init__(self, relays):
        self.relays = tuple(relays)
        self.thread_store = CombinedThreadStores(*(relay.thread_store for relay in self.relays))

    def start(self):
        try:
            for relay in self.relays:
                relay.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        failure = None
        for relay in reversed(self.relays):
            try:
                relay.close()
            except Exception as error:
                failure = error
        if failure is not None:
            raise failure

_REMOTE_AUTHORITY = re.compile(
    r"^ssh-remote\+[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$"
)

_STORAGE_KEY = re.compile(r"^[0-9a-f]{32}$")


def _canonical_uuid(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        return None
    return parsed if parsed == value.lower() else None


@dataclass(frozen=True)
class RelayTarget:
    workspace_id: str
    thread_id: str
    execution_locality: str
    remote_authority: Optional[str] = None
    remote_repo_path: Optional[str] = None
    remote_storage_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("relay workspace id must be a non-empty string")
        if _canonical_uuid(self.thread_id) is None:
            raise ValueError("relay thread id must be a canonical UUID")
        if self.execution_locality not in ("process_local", "remote_ssh"):
            raise ValueError("relay execution locality is invalid")
        remote_values = (
            self.remote_authority,
            self.remote_repo_path,
            self.remote_storage_key,
        )
        if self.execution_locality == "process_local":
            if any(value is not None for value in remote_values):
                raise ValueError("local relay target cannot contain remote routing")
            return
        if (
            not isinstance(self.remote_authority, str)
            or _REMOTE_AUTHORITY.fullmatch(self.remote_authority) is None
            or not isinstance(self.remote_repo_path, str)
            or not self.remote_repo_path.startswith("/")
            or not isinstance(self.remote_storage_key, str)
            or _STORAGE_KEY.fullmatch(self.remote_storage_key) is None
        ):
            raise ValueError("remote relay target routing is invalid")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "thread_id": self.thread_id,
            "execution_locality": self.execution_locality,
            "remote_authority": self.remote_authority,
            "remote_repo_path": self.remote_repo_path,
            "remote_storage_key": self.remote_storage_key,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RelayTarget":
        if not isinstance(value, dict) or frozenset(value) != frozenset(
            {
                "workspace_id",
                "thread_id",
                "execution_locality",
                "remote_authority",
                "remote_repo_path",
                "remote_storage_key",
            }
        ):
            raise ValueError("Slack relay target is malformed")
        return cls(**value)


@dataclass(frozen=True)
class ReplyResult:
    status: str
    workspace_id: Optional[str] = None
    instruction_id: Optional[str] = None
    delivery_status: Optional[str] = None
    duplicate: bool = False
    error_sha256: Optional[str] = None
    control: Optional[tuple] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "workspace_id": self.workspace_id,
            "instruction_id": self.instruction_id,
            "delivery_status": self.delivery_status,
            "duplicate": self.duplicate,
            "error_sha256": self.error_sha256,
        }


class ExactThreadRelay:
    """The existing fenced Codex route shared by interactive transports."""

    reply_source = "slack_reply"

    def _controlled_reply(self, mapping, event_key, instruction_id, text, *, check_legacy_receipt=True):
        from .remote_ssh import RemoteSshTarget
        target = mapping.target
        if target.execution_locality == "remote_ssh":
            adapter = self.remote_ssh_adapter
            if not getattr(adapter, "supports_control", False):
                return None
            remote = RemoteSshTarget(target.remote_authority, target.remote_repo_path,
                                     target.remote_storage_key, (target.thread_id,))
        else:
            codex_home = getattr(self.queue_dispatcher, "codex_home", None)
            if codex_home is None:
                return None
            path = control_root(codex_home) / target.thread_id / "owner.json"
            if not path.exists():
                return None
            value = control_read_json(path)
            from .linux_auto import HostRemoteAdapter
            adapter = HostRemoteAdapter(codex_home)
            remote = RemoteSshTarget("ssh-remote+localhost", value["repo_path"], "0" * 32, (target.thread_id,))
        # Preserve an old runtime's terminal/uncertain reply receipt. The new
        # authoritative relay journal must not resurrect a pre-upgrade request.
        old = self.thread_store.lookup_reply(event_key) if check_legacy_receipt else None
        if old is not None:
            return ReplyResult("duplicate", target.workspace_id, instruction_id,
                                     old.get("delivery_status"), duplicate=True)
        probe = adapter.probe(remote, control=dict(action="relay", thread_id=target.thread_id),
                              wake=dict(instruction_id=instruction_id, prompt=text))
        if probe.get("legacy") is True:
            return None
        if probe.get("status") != "ok":
            return ReplyResult("deferred", target.workspace_id, instruction_id,
                                     probe.get("reason", "control_transport_unavailable"))
        delivery = probe.get("wake", {}).get("state", "uncertain")
        control = RemoteControlClient(adapter, self.runtime)
        return ReplyResult(
            "duplicate" if probe.get("duplicate") else "queued" if delivery in _DELIVERED_STATES else "uncertain",
            target.workspace_id, instruction_id, delivery, duplicate=bool(probe.get("duplicate")),
            control=(control, remote, probe["control"]["token"]),
        )

    def _dispatch(
        self, mapping: Any, instruction_id: str, text: str
    ) -> str:
        from .remote_ssh import RemoteSshTarget
        target = mapping.target
        if target.execution_locality == "process_local":
            return self.queue_dispatcher.dispatch(
                target.thread_id, instruction_id, text, self.reply_source,
            ).status
        assert target.remote_authority is not None
        assert target.remote_repo_path is not None
        assert target.remote_storage_key is not None
        remote_target = RemoteSshTarget(
            target.remote_authority,
            target.remote_repo_path,
            target.remote_storage_key,
            expected_session_ids=(target.thread_id,),
        )
        probe = self.remote_ssh_adapter.probe(
            remote_target, wake={"instruction_id": instruction_id, "prompt": text},
        )
        if probe.get("status") != "ok":
            return str(probe.get("reason", "remote_adapter_unavailable"))
        wake = probe.get("wake")
        if not isinstance(wake, dict):
            return "remote_wake_missing"
        return str(wake.get("state", "uncertain"))

    @staticmethod
    def _error_digest(error: Exception) -> str:
        error_type = f"{type(error).__module__}.{type(error).__qualname__}"
        try:
            detail = str(error)
        except Exception:
            detail = ""
        return sha256_text(
            json.dumps(["slack_reply_dispatch_failed", error_type, detail])
        )
