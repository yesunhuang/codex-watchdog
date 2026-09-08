"""Reject Linux packages whose ELF requirements exceed their declared baseline."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

GLIBC_LIMITS = {"x64": (2, 28), "arm64": (2, 35)}


def glibc_versions(output: str) -> set[tuple[int, ...]]:
    names = set(re.findall(r"\bName: (GLIBC_[A-Za-z0-9_.]+)", output))
    if any(re.fullmatch(r"GLIBC_[0-9]+(?:\.[0-9]+)+", name) is None for name in names):
        raise RuntimeError("Unsupported private or nonnumeric glibc requirement")
    return {tuple(map(int, name[6:].split("."))) for name in names}


def verify_glibc_requirements(paths: list[Path], architecture: str) -> str:
    versions = set()
    for path in sorted(set(path.resolve(strict=True) for path in paths)):
        output = subprocess.check_output(["readelf", "--version-info", str(path)], text=True)
        versions.update(glibc_versions(output))
    if not versions:
        raise RuntimeError("ELF glibc requirements are missing")
    required = max(versions)
    if required > GLIBC_LIMITS[architecture]:
        raise RuntimeError("Bundled ELF requires glibc " + ".".join(map(str, required))
                           + "; exceeds the " + architecture + " package baseline")
    return ".".join(map(str, required))
