"""Verify that a PE executable embeds every image from the approved ICO."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pefile


REQUIRED_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
RT_ICON = 3
RT_GROUP_ICON = 14


@dataclass(frozen=True)
class IconImage:
    width: int
    height: int
    payload: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


def _dimension(value: int) -> int:
    return 256 if value == 0 else value


def read_ico(path: Path) -> Tuple[IconImage, ...]:
    data = path.read_bytes()
    if len(data) < 6:
        raise ValueError("ICO header is truncated")
    reserved, image_type, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or image_type != 1 or count <= 0:
        raise ValueError("file is not a valid icon container")
    directory_end = 6 + count * 16
    if directory_end > len(data):
        raise ValueError("ICO directory is truncated")
    images: List[IconImage] = []
    for index in range(count):
        (
            width,
            height,
            _color_count,
            _reserved,
            _planes,
            _bit_count,
            size,
            offset,
        ) = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        if size <= 0 or offset < directory_end or offset + size > len(data):
            raise ValueError(f"ICO image {index} has invalid bounds")
        images.append(
            IconImage(
                _dimension(width),
                _dimension(height),
                data[offset : offset + size],
            )
        )
    return tuple(images)


def _resource_payloads(pe: pefile.PE, resource_type: int) -> Dict[int, List[bytes]]:
    payloads: Dict[int, List[bytes]] = {}
    resources = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    if resources is None:
        return payloads
    for type_entry in resources.entries:
        if type_entry.id != resource_type:
            continue
        for name_entry in type_entry.directory.entries:
            if name_entry.id is None:
                continue
            for language_entry in name_entry.directory.entries:
                data = language_entry.data.struct
                payload = pe.get_data(data.OffsetToData, data.Size)
                payloads.setdefault(int(name_entry.id), []).append(payload)
    return payloads


def _group_images(group: bytes, icons: Dict[int, List[bytes]]) -> Tuple[IconImage, ...]:
    if len(group) < 6:
        raise ValueError("group icon header is truncated")
    reserved, image_type, count = struct.unpack_from("<HHH", group, 0)
    if reserved != 0 or image_type != 1 or count <= 0 or 6 + count * 14 > len(group):
        raise ValueError("group icon directory is invalid")
    images: List[IconImage] = []
    for index in range(count):
        (
            width,
            height,
            _color_count,
            _reserved,
            _planes,
            _bit_count,
            size,
            resource_id,
        ) = struct.unpack_from("<BBBBHHIH", group, 6 + index * 14)
        matching = [
            payload for payload in icons.get(resource_id, ()) if len(payload) == size
        ]
        if len(matching) != 1:
            raise ValueError(
                f"icon resource {resource_id} is missing or ambiguous for group entry {index}"
            )
        images.append(IconImage(_dimension(width), _dimension(height), matching[0]))
    return tuple(images)


def _signature(images: Iterable[IconImage]) -> Counter:
    return Counter((image.width, image.height, image.sha256) for image in images)


def verify_executable_icon(executable: Path, icon: Path) -> dict:
    expected = read_ico(icon)
    expected_sizes = tuple(
        sorted({image.width for image in expected if image.width == image.height})
    )
    if expected_sizes != REQUIRED_ICON_SIZES:
        raise ValueError(
            f"approved ICO sizes {expected_sizes} do not match required {REQUIRED_ICON_SIZES}"
        )
    pe = pefile.PE(str(executable), fast_load=True)
    matched_group: Optional[int] = None
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
        icons = _resource_payloads(pe, RT_ICON)
        groups = _resource_payloads(pe, RT_GROUP_ICON)
        expected_signature = _signature(expected)
        for group_id, variants in groups.items():
            for group in variants:
                try:
                    actual = _group_images(group, icons)
                except ValueError:
                    continue
                if _signature(actual) == expected_signature:
                    matched_group = group_id
                    break
            if matched_group is not None:
                break
        if matched_group is None:
            raise ValueError(
                "the PE contains no group icon whose image payloads match the approved ICO"
            )
    finally:
        pe.close()
    return {
        "status": "verified",
        "executable": str(executable.resolve()),
        "icon": str(icon.resolve()),
        "icon_sha256": hashlib.sha256(icon.read_bytes()).hexdigest(),
        "sizes": list(expected_sizes),
        "matched_group_id": matched_group,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--icon", required=True, type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify_executable_icon(args.executable, args.icon)
    except (OSError, ValueError, pefile.PEFormatError) as exc:
        print(
            json.dumps({"status": "failed", "reason": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
