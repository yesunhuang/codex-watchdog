"""Generate a resolved runtime inventory and copy distributable license texts."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT_DISTRIBUTION = "codex-watchdog"
LICENSE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")


def _active_requirements(distribution: metadata.Distribution) -> Iterable[str]:
    for raw_requirement in distribution.requires or ():
        requirement = Requirement(raw_requirement)
        if requirement.marker is not None and not requirement.marker.evaluate(
            {"extra": ""}
        ):
            continue
        yield requirement.name


def _runtime_distributions(root: str) -> List[metadata.Distribution]:
    pending = [root]
    visited: Set[str] = set()
    resolved: List[metadata.Distribution] = []
    while pending:
        requested = pending.pop()
        normalized = canonicalize_name(requested)
        if normalized in visited:
            continue
        visited.add(normalized)
        distribution = metadata.distribution(requested)
        resolved.append(distribution)
        pending.extend(_active_requirements(distribution))
    return sorted(
        resolved,
        key=lambda item: canonicalize_name(item.metadata["Name"]),
    )


def _license_name(distribution: metadata.Distribution) -> str:
    value = distribution.metadata.get("License-Expression")
    if value:
        return value.strip()
    value = distribution.metadata.get("License")
    if value and value.strip() and value.strip().upper() != "UNKNOWN":
        return " ".join(value.split())
    classifiers = distribution.metadata.get_all("Classifier") or ()
    license_classifiers = [
        classifier.split(" :: ")[-1]
        for classifier in classifiers
        if classifier.startswith("License ::")
    ]
    return ", ".join(license_classifiers) or "See copied license text"


def _project_url(distribution: metadata.Distribution) -> str:
    for value in distribution.metadata.get_all("Project-URL") or ():
        label, separator, url = value.partition(",")
        if separator and label.strip().casefold() in {
            "homepage",
            "repository",
            "source",
        }:
            return url.strip()
    return (distribution.metadata.get("Home-page") or "").strip()


def _license_files(distribution: metadata.Distribution) -> List[Path]:
    candidates: List[Path] = []
    for relative in distribution.files or ():
        name = Path(str(relative)).name.upper()
        if name.startswith(LICENSE_PREFIXES):
            located = Path(distribution.locate_file(relative))
            if located.is_file():
                candidates.append(located)
    unique: Dict[str, Path] = {}
    for candidate in candidates:
        unique[str(candidate.resolve()).casefold()] = candidate
    return sorted(unique.values(), key=lambda item: str(item).casefold())


def _copy_license_files(
    distribution: metadata.Distribution, destination: Path
) -> List[Dict[str, str]]:
    name = canonicalize_name(distribution.metadata["Name"])
    target_directory = destination / name
    copied: List[Dict[str, str]] = []
    used_names: Set[str] = set()
    for index, source in enumerate(_license_files(distribution), start=1):
        target_name = source.name
        if target_name.casefold() in used_names:
            target_name = f"{index:02d}-{target_name}"
        used_names.add(target_name.casefold())
        target_directory.mkdir(parents=True, exist_ok=True)
        target = target_directory / target_name
        shutil.copyfile(source, target)
        copied.append(
            {
                "path": target.relative_to(destination).as_posix(),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    return copied


def _copy_python_license(destination: Path) -> Dict[str, object]:
    candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE_PYTHON.txt",
    ]
    source = next((candidate for candidate in candidates if candidate.is_file()), None)
    if source is None:
        raise FileNotFoundError("the build Python license text was not found")
    target_directory = destination / "python"
    target_directory.mkdir(parents=True, exist_ok=True)
    target = target_directory / source.name
    shutil.copyfile(source, target)
    return {
        "name": "CPython runtime",
        "version": sys.version.split()[0],
        "license": "Python Software Foundation License",
        "url": "https://www.python.org/psf/license/",
        "license_files": [
            {
                "path": target.relative_to(destination).as_posix(),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        ],
    }


def generate(destination: Path, include_distributions: Sequence[str]) -> Dict:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    distributions = _runtime_distributions(ROOT_DISTRIBUTION)
    present = {canonicalize_name(item.metadata["Name"]): item for item in distributions}
    for requested in include_distributions:
        normalized = canonicalize_name(requested)
        if normalized not in present:
            present[normalized] = metadata.distribution(requested)
    records: List[Dict[str, object]] = []
    for normalized in sorted(present):
        distribution = present[normalized]
        if normalized == canonicalize_name(ROOT_DISTRIBUTION):
            continue
        records.append(
            {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "license": _license_name(distribution),
                "url": _project_url(distribution),
                "license_files": _copy_license_files(distribution, destination),
            }
        )
    records.append(_copy_python_license(destination))
    records.sort(key=lambda item: str(item["name"]).casefold())
    inventory = {
        "schema_version": 1,
        "generated_by": "tools/generate_dependency_licenses.py",
        "packages": records,
    }
    (destination / "inventory.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# Resolved dependency and runtime licenses",
        "",
        "This inventory was generated from the isolated Windows release environment.",
        "License texts copied from installed distribution metadata are stored below.",
        "",
        "| Package | Version | Declared license | Project |",
        "| --- | --- | --- | --- |",
    ]
    for record in records:
        url = str(record["url"])
        project = f"[link]({url})" if url else "—"
        license_name = str(record["license"]).replace("|", "\\|")
        lines.append(
            f"| {record['name']} | {record['version']} | {license_name} | {project} |"
        )
    lines.extend(
        [
            "",
            "`inventory.json` records the SHA-256 digest of every copied license file.",
            "",
        ]
    )
    (destination / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return inventory


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-distribution", action="append", default=[])
    args = parser.parse_args(argv)
    inventory = generate(args.output, args.include_distribution)
    print(
        json.dumps(
            {"status": "generated", "package_count": len(inventory["packages"])},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
