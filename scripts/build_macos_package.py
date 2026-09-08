"""Build an allowlisted ARM64 CLI preview with the pinned PyInstaller recipe."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("The package must be built natively on Apple Silicon.")
    import PyInstaller

    if PyInstaller.__version__ != "6.22.2":
        raise SystemExit("The package recipe requires PyInstaller 6.22.2.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    name = "codex-watchdog-v" + version + "-macos-arm64-preview"
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (name + ".zip")
    if archive.exists() or (output / name).exists():
        raise SystemExit("Refusing to replace an existing package artifact.")
    build_parent = ROOT / "build"
    build_parent.mkdir(exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix="macos-package-", dir=build_parent))
    # pip's direct_url.json records the private build checkout. Runtime version
    # discovery needs only Name/Version, so supply that metadata explicitly.
    metadata_name = "codex_watchdog-" + version + ".dist-info"
    metadata = build / metadata_name
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: codex-watchdog\nVersion: " + version + "\n", encoding="utf-8")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--console",
        "--target-arch", "arm64", "--name", "codex-watchdog",
        "--paths", str(ROOT / "src"), "--distpath", str(build / "binary"),
        "--workpath", str(build / "work"), "--specpath", str(build / "spec"),
        "--add-data", str(metadata) + os.pathsep + metadata_name,
        "--collect-submodules", "msal_extensions",
        "--collect-submodules", "slack_bolt", "--collect-submodules", "slack_sdk",
        "--collect-data", "certifi", "--exclude-module", "tkinter",
        str(ROOT / "packaging/macos_entry.py"),
    ], cwd=ROOT, check=True)
    executable = build / "binary/codex-watchdog"
    architecture = subprocess.check_output(["/usr/bin/lipo", "-archs", str(executable)], text=True).strip()
    if architecture != "arm64":
        raise SystemExit("The output is not a thin ARM64 executable.")
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(executable)], check=True)
    reported = subprocess.check_output([str(executable), "--version"], text=True).strip()
    if reported != "codex-watchdog " + version:
        raise SystemExit("Executable version does not match pyproject.toml.")
    package = output / name
    package.mkdir()
    shutil.copy2(executable, package / "codex-watchdog")
    for filename in ("watchdog-macos.sh", "setup-slack-relay-macos.sh"):
        # Source checkouts on Windows can have CRLF; packaged POSIX scripts must not.
        (package / filename).write_bytes((ROOT / filename).read_bytes().replace(b"\r\n", b"\n"))
        (package / filename).chmod(0o755)
    shutil.copy2(ROOT / "LICENSE", package / "LICENSE")
    shutil.copy2(ROOT / "docs/MACOS_PACKAGE.md", package / "MACOS_PACKAGE.md")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", package / "THIRD_PARTY_NOTICES.md")
    subprocess.run([
        sys.executable, str(ROOT / "tools/generate_dependency_licenses.py"),
        "--output", str(package / "THIRD_PARTY_LICENSES"),
        "--include-distribution", "pyinstaller", "--include-distribution", "macholib",
        "--include-distribution", "importlib-metadata", "--include-distribution", "packaging",
        "--include-distribution", "setuptools", "--include-distribution", "zipp",
    ], check=True)
    files = {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in package.rglob("*") if p.is_file()}
    (package / "package-manifest.json").write_text(json.dumps({
        "schema_version": 1, "version": version, "platform": "macos", "architecture": "arm64",
        "signing": "ad_hoc_developer_preview", "files": files,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as target:
        for path in sorted(p for p in package.rglob("*") if p.is_file()):
            target.write(path, name + "/" + path.relative_to(package).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + ".sha256")).write_text(digest + "  " + archive.name + "\n", encoding="utf-8")
    print(json.dumps({"status": "built", "version": version, "architecture": architecture,
                      "archive": archive.name, "sha256": digest}, sort_keys=True))


if __name__ == "__main__":
    main()
