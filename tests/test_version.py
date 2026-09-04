from __future__ import annotations

from pathlib import Path
import re

import pytest

from codex_watchdog import __version__, cli


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project_block = pyproject.read_text(encoding="utf-8").split("[project]", 1)[1]
    project_block = project_block.split("\n[", 1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project_block, re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_package_version_comes_from_pyproject() -> None:
    assert __version__ == _pyproject_version() == "0.2.0"


def test_cli_version_matches_project(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == f"codex-watchdog {__version__}\n"
