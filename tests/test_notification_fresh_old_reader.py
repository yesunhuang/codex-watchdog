"""Fresh finite output refuses the exact previous-public receipt readers.

These are isolated source compatibility fixtures, not an old executable or a
live native/provider test. Each old reader keeps its original storage/models.
"""
from contextlib import closing
import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
import uuid

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.notification_context import receipt_context
from codex_watchdog.notification_fresh_entry import ensure_fresh_notification_entry
from codex_watchdog.notification_receipt_retention import FRESH_ORIGIN
from codex_watchdog.notification_root_retention import read_notification_root
from codex_watchdog.notification_senders import sender_fence
from codex_watchdog.notifications import NotificationEvent
from codex_watchdog.storage import FileLock, InstructionStore as CurrentInstructionStore


FIXTURE = Path(__file__).parent / "fixtures" / "notification_public_228"
PUBLIC_REVISION = "657a6a28a3a97e467ed0a3f22fee52e46687d756"
FROZEN = {
    "notification_receipts.py": "112b7e7700d1338b902e1a3f701062e6381e032c05a96d870102143e2020b8f8",
    "storage.py": "9d9c7cb29fabffbe5d2d7ebc207a027b90e9bf3c2ebd0451126c351f6415b090",
    "models.py": "07720e85168fc204051025f224e8152d3eb42775590df939df023d7a0f83e79f",
}
NOW = 1_800_000_000.0


@pytest.fixture
def public_228(monkeypatch):
    """Execute only the three pinned modules inside a distinct package alias."""
    manifest = json.loads((FIXTURE / "manifest.json").read_bytes())
    assert manifest["public_revision"] == PUBLIC_REVISION
    assert manifest["canonical_project_version"] == "2.2.8"
    assert set(manifest["source_files"]) == set(FROZEN)
    for name, expected in FROZEN.items():
        raw = (FIXTURE / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected
        assert manifest["source_files"][name]["sha256"] == expected
        assert manifest["source_files"][name]["bytes"] == len(raw)
    license_path = FIXTURE / manifest["license"]["path"]
    assert hashlib.sha256(license_path.read_bytes()).hexdigest() == manifest["license"]["sha256"]

    alias = "_watchdog_public_228_fixture_" + uuid.uuid4().hex
    package = ModuleType(alias)
    package.__package__ = alias
    package.__path__ = [str(FIXTURE)]
    package.__file__ = str(FIXTURE / "__init__.py")
    package.__spec__ = importlib.machinery.ModuleSpec(alias, loader=None, is_package=True)
    monkeypatch.setitem(sys.modules, alias, package)
    loaded = {}
    for leaf in ("models", "storage", "notification_receipts"):
        name = alias + "." + leaf
        path = FIXTURE / (leaf + ".py")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        assert Path(module.__file__).resolve() == path.resolve()
        loaded[leaf] = module
    assert loaded["notification_receipts"].InstructionStore is loaded["storage"].InstructionStore
    assert loaded["storage"].Instruction is loaded["models"].Instruction
    assert loaded["storage"].InstructionStore is not CurrentInstructionStore
    assert loaded["storage"].Instruction.__module__ == alias + ".models"
    return SimpleNamespace(**loaded)


def current_context(kind):
    native = kind == "native"
    event = NotificationEvent(
        "fresh-old-reader-compatibility", "discovery_attention", "original-source",
        "Fixture notification", "Synthetic fresh namespace compatibility",
        source_observer=not native,
        source_thread_id="22222222-3333-4444-8555-666666666666" if native else None,
        source_born=NOW - 1,
    )
    identities = {
        provider: {
            "target_sha256": sha256_text("fixture-target-" + provider),
            "payload_sha256": sha256_text("fixture-payload-" + provider),
        }
        for provider in ("slack", "lark", "smtp", "windows_msg")
    }
    value = receipt_context(event, identities)
    assert value is not None
    assert value["schema_version"] == (1 if native else 2)
    return value


def file_bytes(runtime):
    """Small disposable tree assertion; never a product hot-path history scan."""
    return {
        path.relative_to(runtime).as_posix(): path.read_bytes()
        for path in runtime.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("kind", ("observer", "native"))
@pytest.mark.parametrize("family", ("dual", "fallback"))
def test_fresh_finite_namespace_refuses_public_228_before_claim_or_send(
        tmp_path, public_228, kind, family):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    value = current_context(kind)
    old = public_228.notification_receipts
    entered, possible_sends = [], []
    # Publication and the historical reader attempt each hold the real caller
    # exclusion; no native ownership is manufactured. Snapshot between fences:
    # Windows byte-range locks deliberately deny a second handle's lock read.
    with sender_fence(runtime), FileLock(runtime / "locks" / "notifications.lock"):
        assert not (runtime / "notifications").exists()
        ensure_fresh_notification_entry(runtime, value, clock=lambda: NOW)
        ready = read_notification_root(runtime)
        assert ready is not None and ready["origin"] == FRESH_ORIGIN
        assert ready["floor"] == 0
        directory = runtime / "notifications"
        marker = json.loads((directory / "dual-deliveries.json").read_bytes())
        assert type(marker["schema_version"]) is int and marker["schema_version"] == 3
        for name in ("dual-deliveries.sqlite3", "fallback-deliveries.sqlite3"):
            path = directory / name
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                assert db.execute("PRAGMA user_version").fetchone() == (2,)
    before = file_bytes(runtime)
    with sender_fence(runtime), FileLock(runtime / "locks" / "notifications.lock"):
        if family == "dual":
            store = old.DualDeliveryReceipts(runtime)
            reason = "notification_receipts_legacy_invalid"
        else:
            store = old.FallbackDeliveryReceipts(runtime)
            reason = "notification_fallback_schema_invalid"
        with pytest.raises(old.NotificationReceiptError, match=reason):
            with store as receipts:
                entered.append("before-claim")
                if family == "dual":
                    receipts.claim(value["fingerprint"], "slack")
                else:
                    identity = value["providers"]["smtp"]
                    receipts.claim(value["fingerprint"], "smtp",
                                   identity["target_sha256"], identity["payload_sha256"])
                possible_sends.append("would-send-only-after-a-committed-old-claim")
        assert entered == possible_sends == []
        assert store.db is None
    # Include the released lock files in the complete byte comparison. The
    # fallback reader's transient transaction must roll back without changes.
    assert file_bytes(runtime) == before
