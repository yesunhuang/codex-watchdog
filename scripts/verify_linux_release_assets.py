"""Require both Linux package receipts to identify the exact release commit."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def verify(directory: Path, version: str, commit: str) -> None:
    for architecture in ("arm64", "x64"):
        name = "codex-watchdog-v" + version + "-linux-" + architecture
        receipt = json.loads((directory / ("linux-" + architecture + "-package-acceptance.json")).read_text())
        if any(receipt.get(key) != value for key, value in {
            "schema_version": 1, "status": "passed", "version": version,
            "architecture": architecture, "source_commit": commit,
        }.items()):
            raise ValueError("Linux acceptance does not identify this release: " + architecture)
        with zipfile.ZipFile(directory / (name + ".zip")) as archive:
            manifest = json.loads(archive.read(name + "/package-manifest.json"))
            core_version = tuple(int(part) for part in version.split("-", 1)[0].split("+", 1)[0].split("."))
            if core_version >= (2, 2, 4):
                try:
                    guide = archive.read(name + "/NOTIFICATION_RECEIPTS.md")
                except KeyError:
                    raise ValueError("Linux receipt rollback guide is missing: " + architecture) from None
                if manifest.get("files", {}).get("NOTIFICATION_RECEIPTS.md") != hashlib.sha256(guide).hexdigest():
                    raise ValueError("Linux receipt rollback guide hash is invalid: " + architecture)
                text = guide.decode("utf-8")
                if not all(phrase in text for phrase in ("notification-receipts-export", "--runtime", "sent", "uncertain")):
                    raise ValueError("Linux receipt rollback guide is incomplete: " + architecture)
        if any(manifest.get(key) != value for key, value in {
            "schema_version": 1, "version": version, "platform": "linux",
            "architecture": architecture, "source_commit": commit,
        }.items()):
            raise ValueError("Linux package does not identify this release: " + architecture)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    arguments = parser.parse_args()
    verify(arguments.directory, arguments.version, arguments.commit)
