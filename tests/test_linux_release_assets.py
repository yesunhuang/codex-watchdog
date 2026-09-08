from __future__ import annotations

import importlib.util
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
