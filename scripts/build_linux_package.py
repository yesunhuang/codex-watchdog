"""Build one native Linux architecture with a pinned, source-free package recipe."""

from __future__ import annotations

import argparse
import ast
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

from linux_package_licenses import add_native_inventory
from linux_package_compat import verify_glibc_requirements


ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = {"aarch64": ("arm64", 183), "x86_64": ("x64", 62)}


def collected_binaries(value):
    if isinstance(value, (list, tuple)):
        if len(value) == 3 and all(isinstance(item, str) for item in value) and value[2] in ("BINARY", "EXTENSION"):
            yield tuple(value)
        else:
            for item in value:
                yield from collected_binaries(item)


def main() -> None:
    if sys.platform != "linux" or platform.machine() not in ARCHITECTURES:
        raise SystemExit("Build natively on Linux ARM64 or x86-64.")
    import PyInstaller

    if PyInstaller.__version__ != "6.22.2" or sys.version_info[:3] != (3, 12, 14):
        raise SystemExit("This recipe requires Python 3.12.14 and PyInstaller 6.22.2.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--native-runtime-package", type=Path,
                        help="verified previous Linux package supplying baseline system libraries")
    args = parser.parse_args()
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True).strip():
        raise SystemExit("Package builds require a clean committed checkout.")
    architecture, machine = ARCHITECTURES[platform.machine()]
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    name = "codex-watchdog-v" + version + "-linux-" + architecture
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive, package = output / (name + ".zip"), output / name
    if archive.exists() or package.exists():
        raise SystemExit("Refusing to replace an existing package artifact.")
    build_parent = ROOT / "build"
    build_parent.mkdir(exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix="linux-package-", dir=build_parent))
    native_runtime = None
    build_env = dict(os.environ)
    if args.native_runtime_package:
        from linux_runtime_reuse import NativeRuntime
        native_runtime = NativeRuntime(args.native_runtime_package, architecture)
        libraries = build / "native-runtime"
        native_runtime.extract(libraries)
        build_env["LD_LIBRARY_PATH"] = str(libraries) + os.pathsep + build_env.get("LD_LIBRARY_PATH", "")
    metadata_name = "codex_watchdog-" + version + ".dist-info"
    metadata = build / metadata_name
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: codex-watchdog\nVersion: " + version + "\n", encoding="utf-8")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--console",
        "--name", "codex-watchdog", "--paths", str(ROOT / "src"),
        "--distpath", str(build / "binary"), "--workpath", str(build / "work"),
        "--specpath", str(build / "spec"),
        "--add-data", str(metadata) + os.pathsep + metadata_name,
        "--collect-submodules", "msal_extensions",
        "--collect-submodules", "slack_bolt", "--collect-submodules", "slack_sdk",
        "--collect-data", "certifi", "--exclude-module", "tkinter",
        str(ROOT / "packaging/linux_entry.py"),
    ], cwd=ROOT, env=build_env, check=True)
    executable = build / "binary/codex-watchdog"
    header = executable.read_bytes()[:20]
    if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != machine:
        raise SystemExit("Built executable has the wrong ELF architecture.")
    if subprocess.check_output([str(executable), "--version"], text=True).strip() != "codex-watchdog " + version:
        raise SystemExit("Executable version differs from pyproject.toml.")
    package.mkdir()
    shutil.copy2(executable, package / "codex-watchdog")
    for source, target in (("LICENSE", "LICENSE"), ("docs/LINUX_PACKAGE.md", "LINUX_PACKAGE.md"),
                           ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md")):
        shutil.copy2(ROOT / source, package / target)
    subprocess.run([
        sys.executable, str(ROOT / "tools/generate_dependency_licenses.py"),
        "--output", str(package / "THIRD_PARTY_LICENSES"),
        "--include-distribution", "pyinstaller", "--include-distribution", "importlib-metadata",
        "--include-distribution", "packaging", "--include-distribution", "setuptools",
        "--include-distribution", "zipp", "--include-distribution", "tomli",
    ], check=True)
    analysis = list((build / "work").rglob("Analysis-00.toc"))
    if len(analysis) != 1:
        raise SystemExit("Build dependency graph is absent or ambiguous.")
    binaries = list(collected_binaries(ast.literal_eval(analysis[0].read_text(encoding="utf-8"))))
    if not binaries:
        raise SystemExit("Build dependency graph contains no native libraries.")
    minimum_glibc = verify_glibc_requirements(
        [executable, *(Path(original) for _, original, _ in binaries)], architecture)
    add_native_inventory(package / "THIRD_PARTY_LICENSES", binaries, native_runtime=native_runtime)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    files = {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in package.rglob("*") if path.is_file()}
    manifest = {"schema_version": 1, "version": version, "platform": "linux", "architecture": architecture,
                "source_commit": commit, "build_glibc": platform.libc_ver()[1],
                "minimum_glibc": minimum_glibc,
                "build_python": platform.python_version(), "pyinstaller": PyInstaller.__version__, "files": files}
    if native_runtime:
        manifest["native_runtime_source"] = native_runtime.provenance()
    (package / "package-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as target:
        for path in sorted(path for path in package.rglob("*") if path.is_file()):
            target.write(path, name + "/" + path.relative_to(package).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + ".sha256")).write_text(digest + "  " + archive.name + "\n", encoding="utf-8")
    print(json.dumps({"status": "built", "version": version, "architecture": architecture,
                      "source_commit": commit, "archive": archive.name, "sha256": digest}, sort_keys=True))


if __name__ == "__main__":
    main()
