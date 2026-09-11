import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from scripts.linux_runtime_reuse import NativeRuntime


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def previous(tmp_path, monkeypatch):
    package = tmp_path / "previous"
    package.mkdir()
    binary = b"fixture executable"
    library = b"fixture system library"
    license_data = b"Exact vendor license text"
    license_file = "native-vendor/LICENSE"
    record = dict(name="vendor-library", version="1.2.3", license_files=[
        dict(path=license_file, sha256=digest(license_data))])
    inventory = dict(schema_version=1, packages=[record], native_libraries=[
        dict(file="libfixture.so.1", kind="BINARY", sha256=digest(library), license_package=record["name"])])
    files = {"codex-watchdog": binary, "THIRD_PARTY_LICENSES/" + license_file: license_data,
             "THIRD_PARTY_LICENSES/inventory.json": json.dumps(inventory).encode()}
    for name, data in files.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = dict(schema_version=1, platform="linux", architecture="arm64", version="0.2.19",
                    source_commit="a" * 40, files={name: digest(data) for name, data in files.items()})
    (package / "package-manifest.json").write_text(json.dumps(manifest))
    reader = lambda path: SimpleNamespace(extract=lambda name: library)
    monkeypatch.setitem(sys.modules, "PyInstaller.archive.readers", SimpleNamespace(CArchiveReader=reader))
    return package


def test_reuse_preserves_library_version_license_bytes_and_public_provenance(previous, tmp_path):
    runtime = NativeRuntime(previous, "arm64")
    runtime.extract(tmp_path / "libraries")
    destination = tmp_path / "licenses"
    record = runtime.license_record(destination, tmp_path / "libraries/libfixture.so.1")
    assert record["version"] == "1.2.3"
    assert (destination / "native-vendor/LICENSE").read_bytes() == b"Exact vendor license text"
    assert runtime.provenance()["version"] == "0.2.19"
    assert str(tmp_path) not in json.dumps(runtime.provenance())


@pytest.mark.parametrize("file", ["codex-watchdog", "THIRD_PARTY_LICENSES/inventory.json", "THIRD_PARTY_LICENSES/native-vendor/LICENSE"])
def test_tampered_previous_package_is_rejected(previous, tmp_path, file):
    (previous / file).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        runtime = NativeRuntime(previous, "arm64")
        runtime.extract(tmp_path / "libraries")
        runtime.license_record(tmp_path / "licenses", tmp_path / "libraries/libfixture.so.1")


def test_wrong_architecture_and_changed_extracted_library_are_rejected(previous, tmp_path):
    with pytest.raises(ValueError, match="architecture"):
        NativeRuntime(previous, "x64")
    runtime = NativeRuntime(previous, "arm64")
    runtime.extract(tmp_path / "libraries")
    library = tmp_path / "libraries/libfixture.so.1"
    library.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        runtime.license_record(tmp_path / "licenses", library)


def test_license_path_cannot_escape_previous_package(previous, tmp_path):
    runtime = NativeRuntime(previous, "arm64")
    runtime.extract(tmp_path / "libraries")
    runtime.inventory["packages"][0]["license_files"][0]["path"] = "../../outside"
    with pytest.raises(ValueError):
        runtime.license_record(tmp_path / "licenses", tmp_path / "libraries/libfixture.so.1")
