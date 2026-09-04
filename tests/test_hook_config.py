from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.run_native_hook_probe import _join_hook_command


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_example_windows_hook_commands_are_quote_free() -> None:
    document = json.loads((REPO_ROOT / "examples" / "hooks.json").read_text())

    commands = [
        hook["commandWindows"]
        for groups in document["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]

    assert commands
    assert all('"' not in command for command in commands)


def test_windows_probe_command_rejects_paths_that_need_quotes() -> None:
    with pytest.raises(ValueError, match="quote-free"):
        _join_hook_command(
            [r"C:\Program Files\Python\python.exe", r"D:\repo\hook.py"], windows=True,
        )


def test_windows_probe_command_is_quote_free_for_safe_paths() -> None:
    command = _join_hook_command(
        [r"C:\Python\python.exe", r"D:\repo\hook.py", "--test-mode"], windows=True,
    )

    assert command == r"C:\Python\python.exe D:\repo\hook.py --test-mode"
    assert '"' not in command
