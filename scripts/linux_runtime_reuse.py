"""Reuse verified system libraries from an earlier package on newer build hosts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class NativeRuntime:
    def __init__(self, package: Path, architecture: str):
        self.package = package.resolve(strict=True)
        self.manifest = json.loads((self.package / "package-manifest.json").read_text())
        if (self.manifest.get("schema_version") != 1 or self.manifest.get("platform") != "linux"
                or self.manifest.get("architecture") != architecture):
            raise ValueError("Native runtime package architecture/schema mismatch")
        self.inventory = json.loads(self.read("THIRD_PARTY_LICENSES/inventory.json"))
        if self.inventory.get("schema_version") != 1:
            raise ValueError("Native runtime inventory schema mismatch")
        self.entries = {}

    def read(self, relative):
        path = (self.package / relative).resolve(strict=True)
        path.relative_to(self.package)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.manifest["files"].get(relative):
            raise ValueError("Native runtime package checksum mismatch")
        return data

    def extract(self, destination: Path):
        from PyInstaller.archive.readers import CArchiveReader

        self.read("codex-watchdog")
        archive = CArchiveReader(str(self.package / "codex-watchdog"))
        destination.mkdir()
        for item in self.inventory["native_libraries"]:
            name = item["file"]
            if (item["kind"] != "BINARY" or Path(name).name != name
                    or not name.startswith("lib") or ".so" not in name
                    or item["license_package"].lower() == "cpython runtime"):
                continue
            data = archive.extract(name)
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("Native runtime library checksum mismatch")
            path = destination / name
            path.write_bytes(data)
            self.entries[path.resolve()] = item
        if not self.entries:
            raise ValueError("Native runtime package has no reusable system libraries")

    def license_record(self, destination: Path, source: Path):
        item = self.entries.get(source.resolve())
        if item is None:
            return None
        if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Reused native library changed")
        records = [r for r in self.inventory["packages"] if r["name"] == item["license_package"]]
        if len(records) != 1 or not records[0].get("license_files"):
            raise ValueError("Reused native library license is missing or ambiguous")
        for license_file in records[0]["license_files"]:
            relative = license_file["path"]
            target = (destination / relative).resolve()
            target.relative_to(destination.resolve())
            data = self.read("THIRD_PARTY_LICENSES/" + relative)
            if hashlib.sha256(data).hexdigest() != license_file["sha256"]:
                raise ValueError("Reused native license checksum mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() != data:
                raise ValueError("Reused native license conflicts with build inventory")
            target.write_bytes(data)
        return records[0]

    def provenance(self):
        return {"version": self.manifest["version"], "source_commit": self.manifest["source_commit"],
                "inventory_sha256": self.manifest["files"]["THIRD_PARTY_LICENSES/inventory.json"]}
