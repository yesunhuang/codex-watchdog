"""Resolve the project version from installed metadata or the source tree."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re


_DISTRIBUTION_NAME = "codex-watchdog"
_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$')


def _source_tree_version() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    in_project = False
    for raw_line in pyproject.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line == "[project]":
            in_project = True
            continue
        if line.startswith("["):
            in_project = False
        if in_project:
            match = _VERSION_LINE.fullmatch(line)
            if match:
                return match.group(1)
    raise RuntimeError(f"project version is missing from {pyproject}")


def project_version() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        return _source_tree_version()
    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        raise RuntimeError(
            "codex-watchdog distribution metadata is unavailable"
        ) from None


__version__ = project_version()
