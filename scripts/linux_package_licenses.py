"""Account for every native library collected by the Linux PyInstaller build."""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig


def rpm_license_record(destination: Path, source: Path) -> dict:
    result = subprocess.run(["rpm", "-qf", "--queryformat", "%{NAME}\n", str(source)],
                            capture_output=True, text=True)
    names = set(result.stdout.splitlines())
    if result.returncode or len(names) != 1:
        raise RuntimeError("RPM library ownership is missing or ambiguous")
    package = names.pop()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", package) is None:
        raise RuntimeError("Invalid RPM package name")
    version = subprocess.check_output(
        ["rpm", "-q", "--queryformat", "%{VERSION}-%{RELEASE}", package], text=True).strip()
    paths = subprocess.check_output(["rpm", "-q", "--licensefiles", package], text=True).splitlines()
    licenses = sorted({Path(path).resolve(strict=True) for path in paths if Path(path).is_file()})
    public_domain = (not licenses and package == "sqlite-libs"
                     and re.fullmatch(r"libsqlite3\.so(?:\.[0-9]+)*", source.name) is not None
                     and subprocess.check_output(["rpm", "-q", "--queryformat", "%{LICENSE}", package],
                                                 text=True).strip() == "Public Domain")
    if public_domain:
        licenses = [Path(__file__).resolve().parents[1] / "packaging/licenses/sqlite-public-domain.txt"]
    if not licenses:
        raise RuntimeError("RPM native-library license text is missing: " + package)
    files = []
    for index, license_path in enumerate(licenses):
        relative = "native-rpm-" + package + "/" + str(index) + "-" + license_path.name
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(license_path, target)
        files.append({"path": relative, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    return {"name": "rpm-" + package, "version": version,
            "license": "Public Domain" if public_domain else "See copied RPM license text",
            "url": "https://www.sqlite.org/copyright.html" if public_domain else "https://access.redhat.com/articles/4238681",
            "license_files": files}


def add_native_inventory(destination: Path, binaries: list[tuple[str, str, str]]) -> None:
    inventory_path = destination / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    records = {item["name"].lower().replace("_", "-"): item for item in inventory["packages"]}
    distribution_files = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"].lower().replace("_", "-")
        for relative in distribution.files or ():
            path = Path(distribution.locate_file(relative))
            if ".so" in path.name:
                distribution_files[path.resolve()] = name
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    native = []
    for target, original, kind in sorted(set(binaries)):
        source = Path(original).resolve(strict=True)
        owner = distribution_files.get(source)
        if owner is not None:
            if owner not in records:
                raise RuntimeError("Bundled native distribution missing from license inventory: " + owner)
        elif source.name.startswith("libpython") or (stdlib / "lib-dynload") in source.parents:
            owner = "cpython runtime"
        elif shutil.which("rpm") and not shutil.which("dpkg-query"):
            record = rpm_license_record(destination, source)
            owner = record["name"]
            records[owner] = record
        else:
            candidates = {str(source), str(Path(original))}
            for candidate in tuple(candidates):
                if candidate.startswith("/usr/lib/"):
                    candidates.add(candidate[4:])
                elif candidate.startswith("/lib/"):
                    candidates.add("/usr" + candidate)
            package = None
            for candidate in sorted(candidates):
                result = subprocess.run(["dpkg-query", "-S", candidate], capture_output=True, text=True)
                matches = {line.split(": ", 1)[0] for line in result.stdout.splitlines() if ": " in line}
                if result.returncode == 0 and len(matches) == 1:
                    package = matches.pop()
                    break
            if package is None:
                raise RuntimeError("Unaccounted bundled native library: " + target)
            owner = "debian-" + package
            if owner not in records:
                copyright_file = Path("/usr/share/doc") / package.split(":", 1)[0] / "copyright"
                if not copyright_file.is_file():
                    raise RuntimeError("System library copyright text missing: " + package)
                relative = "native-" + package.replace(":", "-") + "/copyright"
                copied = destination / relative
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(copyright_file, copied)
                version = subprocess.check_output(
                    ["dpkg-query", "-W", "--showformat=${Version}", package], text=True).strip()
                license_files = [{"path": relative, "sha256": hashlib.sha256(copied.read_bytes()).hexdigest()}]
                for name in sorted(set(re.findall(r"/usr/share/common-licenses/([A-Za-z0-9_.+-]+)", copied.read_text()))):
                    name = name.rstrip(".")
                    if name in ("", ".", ".."):
                        raise RuntimeError("Invalid common-license reference")
                    common = Path("/usr/share/common-licenses") / name
                    if not common.is_file():
                        raise RuntimeError("Referenced system license missing: " + name)
                    common_relative = "native-" + package.replace(":", "-") + "/" + name
                    common_target = destination / common_relative
                    shutil.copyfile(common, common_target)
                    license_files.append({"path": common_relative, "sha256": hashlib.sha256(common_target.read_bytes()).hexdigest()})
                records[owner] = {"name": owner, "version": version,
                                  "license": "See copied distribution copyright and license text",
                                  "url": "https://packages.ubuntu.com/",
                                  "license_files": license_files}
        native.append({"file": target, "kind": kind, "license_package": records[owner]["name"],
                       "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    inventory["packages"] = sorted(records.values(), key=lambda item: item["name"].casefold())
    inventory["native_libraries"] = native
    inventory["bootloader_license_package"] = "pyinstaller"
    inventory_path.write_text(json.dumps(inventory, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with (destination / "README.md").open("a", encoding="utf-8") as handle:
        handle.write("\nLinux native libraries are listed individually in `inventory.json`, with their\n"
                     "digest and corresponding dependency or distribution license text. CPython\n"
                     "extension modules use the CPython runtime license; the executable bootloader\n"
                     "uses PyInstaller's license and bootloader exception. The system glibc and ELF\n"
                     "loader remain external operating-system prerequisites.\n")
