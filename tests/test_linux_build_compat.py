from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import linux_package_compat as compat
from scripts import linux_package_licenses as licenses


def test_glibc_floor_checks_every_bundled_library_numerically(tmp_path, monkeypatch):
    files = [tmp_path / name for name in ('bootloader', 'libpython.so', 'extension.so')]
    for path in files:
        path.write_bytes(b'fixture')
    requirements = {files[0]: 'Name: GLIBC_2.9', files[1]: 'Name: GLIBC_2.28', files[2]: 'Name: GLIBC_2.17'}
    inspected = []
    def read(args, **kwargs):
        inspected.append(Path(args[-1]))
        return requirements[Path(args[-1])]
    monkeypatch.setattr(compat.subprocess, 'check_output', read)
    assert compat.verify_glibc_requirements(files, 'x64') == '2.28'
    assert set(inspected) == set(files)
    requirements[files[2]] = 'Name: GLIBC_2.35'
    with pytest.raises(RuntimeError, match='exceeds the x64'):
        compat.verify_glibc_requirements(files, 'x64')
    assert compat.verify_glibc_requirements(files, 'arm64') == '2.35'


@pytest.mark.parametrize('requirement', ['GLIBC_PRIVATE', 'GLIBC_ABI_DT_RELR'])
def test_unknown_glibc_contract_fails_closed(requirement):
    with pytest.raises(RuntimeError, match='private or nonnumeric'):
        compat.glibc_versions('Name: ' + requirement)


def test_missing_elf_requirements_fail_closed(tmp_path, monkeypatch):
    binary = tmp_path / 'binary'
    binary.touch()
    monkeypatch.setattr(compat.subprocess, 'check_output', lambda *args, **kwargs: '')
    with pytest.raises(RuntimeError, match='missing'):
        compat.verify_glibc_requirements([binary], 'x64')


def rpm_fixture(tmp_path, monkeypatch, *, owners='openssl-libs\n', present=True):
    source = tmp_path / 'native.so'
    source.write_bytes(b'native fixture')
    license_path = tmp_path / 'LICENSE'
    if present:
        license_path.write_text('Retained vendor license text')
    monkeypatch.setattr(licenses.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=owners))
    def query(args, **kwargs):
        return str(license_path) + '\n' if '--licensefiles' in args else '1.1.1-vendor-update'
    monkeypatch.setattr(licenses.subprocess, 'check_output', query)
    return source, license_path


def test_rpm_license_text_is_copied_without_build_paths(tmp_path, monkeypatch):
    source, license_path = rpm_fixture(tmp_path, monkeypatch)
    destination = tmp_path / 'inventory'
    record = licenses.rpm_license_record(destination, source)
    assert record['name'] == 'rpm-openssl-libs' and record['version'] == '1.1.1-vendor-update'
    assert str(tmp_path) not in str(record)
    assert (destination / record['license_files'][0]['path']).read_bytes() == license_path.read_bytes()


def test_rpm_ambiguous_ownership_fails_closed(tmp_path, monkeypatch):
    source, _ = rpm_fixture(tmp_path, monkeypatch, owners='owner-a\nowner-b\n')
    with pytest.raises(RuntimeError, match='ambiguous'):
        licenses.rpm_license_record(tmp_path / 'inventory', source)


def test_rpm_missing_license_fails_closed(tmp_path, monkeypatch):
    source, _ = rpm_fixture(tmp_path, monkeypatch, present=False)
    with pytest.raises(RuntimeError, match='license text is missing'):
        licenses.rpm_license_record(tmp_path / 'inventory', source)


@pytest.mark.parametrize('declaration', ['Public Domain', 'Different license'])
def test_sqlite_fallback_requires_exact_rpm_public_domain_declaration(tmp_path, monkeypatch, declaration):
    source, _ = rpm_fixture(tmp_path, monkeypatch, owners='sqlite-libs\n', present=False)
    sqlite = source.with_name('libsqlite3.so.0.8.6')
    source.rename(sqlite)
    def query(args, **kwargs):
        if '--licensefiles' in args:
            return ''
        if '%{LICENSE}' in args:
            return declaration
        return '3.26.0-vendor-update'
    monkeypatch.setattr(licenses.subprocess, 'check_output', query)
    destination = tmp_path / 'inventory'
    if declaration != 'Public Domain':
        with pytest.raises(RuntimeError, match='license text is missing'):
            licenses.rpm_license_record(destination, sqlite)
    else:
        record = licenses.rpm_license_record(destination, sqlite)
        assert record['license'] == 'Public Domain'
        copied = destination / record['license_files'][0]['path']
        assert 'https://www.sqlite.org/copyright.html' in copied.read_text()
