from __future__ import annotations

from pathlib import Path

from tools.verify_windows_executable_icon import REQUIRED_ICON_SIZES, read_ico


def test_checked_in_windows_icon_has_all_required_square_sizes() -> None:
    root = Path(__file__).resolve().parents[1]
    images = read_ico(root / "images" / "codex-watchdog.ico")

    assert tuple(sorted(image.width for image in images)) == REQUIRED_ICON_SIZES
    assert all(image.width == image.height for image in images)
    assert all(image.payload for image in images)
