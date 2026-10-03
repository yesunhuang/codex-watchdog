from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import zipfile

import pytest


path = Path(__file__).resolve().parents[1] / "scripts/verify_linux_release_assets.py"
spec = importlib.util.spec_from_file_location("verify_linux_release_assets", path)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.mark.parametrize("changed", [None, "receipt_commit", "manifest_commit", "receipt_status", "missing_architecture"])
def test_release_requires_both_passed_packages_at_the_exact_commit(tmp_path, changed):
    for architecture in ("arm64", "x64"):
        if architecture == "arm64" and changed == "missing_architecture":
            continue
        name = "codex-watchdog-v0.2.4-linux-" + architecture
        manifest = {"schema_version": 1, "version": "0.2.4", "platform": "linux",
                    "architecture": architecture, "source_commit": "a" * 40}
        receipt = {**manifest, "status": "passed"}
        if architecture == "arm64":
            if changed == "receipt_commit":
                receipt["source_commit"] = "b" * 40
            elif changed == "manifest_commit":
                manifest["source_commit"] = "b" * 40
            elif changed == "receipt_status":
                receipt["status"] = "failed"
        (tmp_path / ("linux-" + architecture + "-package-acceptance.json")).write_text(json.dumps(receipt))
        with zipfile.ZipFile(tmp_path / (name + ".zip"), "w") as archive:
            archive.writestr(name + "/package-manifest.json", json.dumps(manifest))
    if changed is None:
        verifier.verify(tmp_path, "0.2.4", "a" * 40)
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            verifier.verify(tmp_path, "0.2.4", "a" * 40)


@pytest.mark.parametrize("changed", [None, "missing_guide", "bad_hash", "missing_command", "missing_runtime"])
def test_current_release_requires_hash_verified_receipt_rollback_guide(tmp_path, changed):
    for architecture in ("arm64", "x64"):
        name = "codex-watchdog-v2.2.4-linux-" + architecture
        guide = b"Stop all processes; --runtime notification-receipts-export all sent and uncertain receipts."
        if architecture == "arm64" and changed == "missing_command":
            guide = guide.replace(b"notification-receipts-export", b"removed")
        elif architecture == "arm64" and changed == "missing_runtime":
            guide = guide.replace(b"--runtime", b"removed")
        manifest = {"schema_version": 1, "version": "2.2.4", "platform": "linux",
                    "architecture": architecture, "source_commit": "a" * 40,
                    "files": {"NOTIFICATION_RECEIPTS.md": hashlib.sha256(guide).hexdigest()}}
        if architecture == "arm64" and changed == "bad_hash":
            manifest["files"]["NOTIFICATION_RECEIPTS.md"] = "0" * 64
        (tmp_path / ("linux-" + architecture + "-package-acceptance.json")).write_text(
            json.dumps({**manifest, "status": "passed"}), encoding="utf-8")
        with zipfile.ZipFile(tmp_path / (name + ".zip"), "w") as archive:
            archive.writestr(name + "/package-manifest.json", json.dumps(manifest))
            if architecture != "arm64" or changed != "missing_guide":
                archive.writestr(name + "/NOTIFICATION_RECEIPTS.md", guide)
    if changed is None:
        verifier.verify(tmp_path, "2.2.4", "a" * 40)
    else:
        with pytest.raises(ValueError, match="rollback guide"):
            verifier.verify(tmp_path, "2.2.4", "a" * 40)
