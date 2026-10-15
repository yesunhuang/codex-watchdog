"""Compose retention with the already admitted Linux actor and shared fence."""
from contextlib import contextmanager, nullcontext
import os
import hashlib
import threading

from .control_context import current_control, current_effect
from .control_state import control_read_json
from .current_work_retention import CurrentWorkRetention
from .native_stream_work import NativeStreamWork
from .relay_authority import bind_service_authority
from .session_authority_retention import STORAGE, migrate_session_authority


def prepare_linux_retention(item):
    """One explicit cold attempt per controller, only with complete proof.

    Unknown native work skips this affected structural action. No alternative
    actor, guessed epoch or shared-home wake migration is created. The old
    shared provider source, other UUIDs and login5 hold remain untouched.
    """
    authority, owner = item.get("relay_authority"), item["owner"]
    native = getattr(owner, "native_work", None)
    if authority is None or native is None:
        return dict(status="unsupported_current_native_producer")
    observation = authority.observation
    selected = current_control()
    if selected is None or selected[0].path != authority.store.path or selected[1] != authority.token:
        return dict(status="current_owner_capability_required")
    if not observation._admitted or observation._admitted_executor != (os.getpid(), threading.get_ident()):
        return dict(status="current_observer_admission_required")
    producer = item.get("retention_stream")
    if producer is None:
        producer = item["retention_stream"] = NativeStreamWork(authority.store.codex_home, clock=native.clock)

    @contextmanager
    def fence():
        # Socket ingress has the existing authority/journal fence, but no
        # observer capability. Its preflight must defer GC and let authorized
        # independent admission continue under those existing guards.
        current = current_control()
        admitted = observation._admitted and observation._admitted_executor == (os.getpid(), threading.get_ident())
        if current is None or not admitted:
            yield
            return
        if current[0].path != authority.store.path or current[1] != authority.token:
            raise ValueError("native_retention_foreign_capability")
        with current_effect("writer"):
            observation.validate_snapshot(active_control=(authority.store, authority.token))
            yield

    def reader():
        observation.validate_snapshot(active_control=(authority.store, authority.token))
        return native.collect(fence=nullcontext, input_reader=lambda: producer.observe(
            authority.thread_id, authority.repo_path, current_owner=native.current_owner, fence=nullcontext))

    manifest = control_read_json(authority.manifest)
    if manifest.get("storage") != STORAGE:
        if item.get("retention_cold_attempted"):
            return item["retention_preparation"]
        with fence():
            inventory = reader()
            if inventory.get("status") != "complete":
                return dict(status="current_native_unknown", reason=inventory.get("reason"))
        item["retention_cold_attempted"] = True
        try:
            cluster = control_read_json(authority.root/"cluster.json")
            providers = set(cluster.get("providers", ())) | {provider for provider in ("slack", "lark", "onebot")
                if (authority.runtime/provider/"reply-tickets.sqlite3").exists()}
            result = migrate_session_authority(authority,
                sources={p:authority.runtime/p/"reply-tickets.sqlite3" for p in providers},
                inflight_reader=reader, sender_fence=nullcontext, clock=native.clock)
        except Exception as error:
            result = dict(status="cold_preparation_refused", reason=type(error).__name__,
                          error_sha256=hashlib.sha256(str(error).encode()).hexdigest())
        item["retention_preparation"] = result
        if result["status"] not in ("migrated", "already_migrated"):
            return result
        # Mutable trackers and journals must use the selected exact runtime;
        # notification claims retain their original portable notification root.
        bind_service_authority(item["service"],authority)
    retained = getattr(authority,"retention_binding",None)
    if retained is None or retained.runtime != authority.runtime:
        authority.retention_binding = CurrentWorkRetention(authority.runtime,authority.store.codex_home,
            authority.thread_id,authority.repo_path,reader=reader,fence=fence,clock=native.clock)
    return dict(status="selected", runtime=STORAGE)
