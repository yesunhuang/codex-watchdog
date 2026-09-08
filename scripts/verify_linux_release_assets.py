"""Require both Linux package receipts to identify the exact release commit."""

from __future__ import annotations

import argparse
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
