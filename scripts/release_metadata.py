"""Select an immutable version release and its immediately previous public version."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Optional, Sequence

from packaging.version import Version


SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


def project_version(text: str) -> str:
    project = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', project[1]) if project else None
    if match is None or SEMVER.fullmatch(match[1]) is None:
        raise ValueError("pyproject.toml must contain an explicit SemVer project version")
    Version(match[1])
    return match[1]


def release_plan(current: str, before: Optional[str], release_tags: Sequence[str],
                 existing_tags: Sequence[str]) -> dict:
    if before == current:
        return {"should_release": "false", "version": current}
    tag = "v" + current
    if tag in existing_tags or tag in release_tags:
        raise ValueError("the version already has a tag or release; it will not be overwritten")
    published = [(Version(value[1:]), value) for value in release_tags
                 if value.startswith("v") and SEMVER.fullmatch(value[1:])]
    if not published:
        raise ValueError("a previous public release is required for the upgrade acceptance gate")
    previous, previous_tag = max(published)
    if Version(current) <= previous:
        raise ValueError("the new version must be newer than every published release")
    return {"should_release": "true", "version": current, "tag": tag,
            "previous_tag": previous_tag, "previous_version": previous_tag[1:]}


def main() -> None:
    current = project_version(Path("pyproject.toml").read_text(encoding="utf-8"))
    before = None
    before_sha = os.environ.get("BEFORE_SHA", "")
    if before_sha and not re.fullmatch(r"0+", before_sha):
        if re.fullmatch(r"[0-9a-f]{40,64}", before_sha) is None:
            raise ValueError("invalid previous commit")
        result = subprocess.run(["git", "show", before_sha + ":pyproject.toml"],
                                capture_output=True, text=True, check=False)
        if result.returncode == 0:
            before = project_version(result.stdout)
    if before == current:
        plan = {"should_release": "false", "version": current}
    else:
        repository = os.environ["GITHUB_REPOSITORY"]
        releases = json.loads(subprocess.check_output(
            ["gh", "release", "list", "--repo", repository, "--exclude-drafts",
             "--limit", "1000", "--json", "tagName"], text=True))
        # Include drafts in collision detection without treating them as a tested predecessor.
        all_releases = json.loads(subprocess.check_output(
            ["gh", "release", "list", "--repo", repository, "--limit", "1000",
             "--json", "tagName"], text=True))
        tags = subprocess.check_output(["git", "tag", "--list"], text=True).splitlines()
        tags.extend(item["tagName"] for item in all_releases)
        plan = release_plan(current, before, [item["tagName"] for item in releases], tags)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in plan.items():
            output.write(key + "=" + value + "\n")
    print(json.dumps(plan, sort_keys=True))


if __name__ == "__main__":
    main()
