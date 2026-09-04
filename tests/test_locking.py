from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from codex_watchdog.storage import FileLock, StoreBusyError


def _hold_lock(path: str, ready, release) -> None:
    with FileLock(Path(path)):
        ready.set()
        release.wait(10)


def test_file_lock_excludes_another_process_and_releases_after_exit(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    path = tmp_path / "process.lock"
    process = context.Process(
        target=_hold_lock, args=(str(path), ready, release), daemon=True
    )
    process.start()
    assert ready.wait(10)

    with pytest.raises(StoreBusyError):
        with FileLock(path):
            pass

    release.set()
    process.join(10)
    assert process.exitcode == 0
    with FileLock(path):
        pass
