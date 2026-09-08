from __future__ import annotations

import json
import os
from pathlib import Path
import shlex

import pytest

from tools.install_user_hooks import (
    _command,
    build_hooks_document,
    install_hooks,
)


def fake_installation(tmp_path: Path, windows_safe: bool = True):
    root_name = "repo" if windows_safe else "repo with spaces"
    repo_root = tmp_path / root_name
    script = repo_root / "tools" / "codex_watchdog_hook.py"
    script.parent.mkdir(parents=True)
    script.write_text("# probe\n", encoding="utf-8")
    python = tmp_path / ("python" if windows_safe else "python with spaces")
    python.write_text("", encoding="utf-8")
    return repo_root, python


def test_document_uses_target_locality_and_test_grace(tmp_path: Path) -> None:
    repo_root, python = fake_installation(tmp_path)
    runtime = repo_root / ".codex-watchdog" / "remote-acceptance"

    document = build_hooks_document(repo_root, runtime, python, 30, 0.1, True)

    stop = document["hooks"]["Stop"][0]["hooks"][0]
    assert str(repo_root.resolve()) in stop["command"]
    assert str(runtime.resolve()) in stop["command"]
    assert "--grace-seconds 30" in stop["command"]
    assert stop["command"].endswith("--test-mode")
    assert stop["timeout"] == 60
    assert ("commandWindows" in stop) is (os.name == "nt")


def test_windows_command_refuses_paths_that_require_quotes(tmp_path: Path) -> None:
    repo_root, python = fake_installation(tmp_path, windows_safe=False)

    with pytest.raises(ValueError, match="quote-free"):
        build_hooks_document(
            repo_root,
            repo_root / ".codex-watchdog",
            python,
            30,
            0.1,
            True,
            target_windows=True,
        )


def test_posix_command_quotes_each_argument() -> None:
    command = _command(
        ["/home/user/python", "/home/user/repo with spaces/hook.py"], windows=False
    )

    assert command == "/home/user/python '/home/user/repo with spaces/hook.py'"


def test_posix_document_supports_whitespace_in_all_command_paths(
    tmp_path: Path,
) -> None:
    repo_root, python = fake_installation(tmp_path, windows_safe=False)
    runtime = repo_root / "runtime with spaces"

    document = build_hooks_document(
        repo_root,
        runtime,
        python,
        30,
        0.1,
        True,
        target_windows=False,
    )

    expected = [
        str(python.resolve()),
        str((repo_root / "tools" / "codex_watchdog_hook.py").resolve()),
        "--runtime",
        str(runtime.resolve()),
        "hook",
        "--grace-seconds",
        "30",
        "--poll-seconds",
        "0.1",
        "--test-mode",
    ]
    for event in ("PermissionRequest", "Stop"):
        hook = document["hooks"][event][0]["hooks"][0]
        assert shlex.split(hook["command"]) == expected
        assert "commandWindows" not in hook


def test_windows_command_preserves_quote_free_arguments() -> None:
    parts = [
        r"C:\Python311\python.exe",
        r"C:\CodexWatchDog\tools\codex_watchdog_hook.py",
        "--runtime",
        r"C:\CodexWatchDog\.codex-watchdog",
        "hook",
    ]

    assert _command(parts, windows=True) == " ".join(parts)


@pytest.mark.skipif(os.name == "nt", reason="POSIX virtualenv uses symlinks")
def test_posix_document_preserves_virtualenv_python_symlink(tmp_path: Path) -> None:
    repo_root, base_python = fake_installation(tmp_path)
    virtualenv_python = tmp_path / "venv with spaces" / "bin" / "python"
    virtualenv_python.parent.mkdir(parents=True)
    virtualenv_python.symlink_to(base_python)

    document = build_hooks_document(
        repo_root,
        repo_root / ".codex-watchdog",
        virtualenv_python,
        30,
        0.1,
        False,
        target_windows=False,
    )

    command = document["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert shlex.split(command)[0] == str(virtualenv_python.absolute())
    assert shlex.split(command)[0] != str(base_python.resolve())


def test_install_is_idempotent_but_refuses_different_existing_file(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    document = {"hooks": {"Stop": []}}

    first, path = install_hooks(codex_home, document)
    second, duplicate_path = install_hooks(codex_home, document)

    assert first == "installed"
    assert second == "unchanged"
    assert path == duplicate_path
    assert json.loads(path.read_text(encoding="utf-8")) == document

    path.write_text('{"existing": true}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        install_hooks(codex_home, document)


def test_install_refuses_dangling_symlink_when_supported(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    path = codex_home / "hooks.json"
    try:
        path.symlink_to(codex_home / "missing.json")
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RuntimeError, match="symlink"):
        install_hooks(codex_home, {"hooks": {}})
