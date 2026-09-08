import os
from pathlib import Path
import ssl

import certifi
import pytest

from codex_watchdog import linux_package
from codex_watchdog.posix_package import PackageError


def frozen_linux(monkeypatch):
    monkeypatch.setattr(linux_package.sys, 'platform', 'linux')
    monkeypatch.setattr(linux_package.sys, 'frozen', True, raising=False)
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    monkeypatch.delenv('SSL_CERT_DIR', raising=False)


def test_linux_package_uses_valid_host_roots_with_verification_enabled(tmp_path, monkeypatch):
    frozen_linux(monkeypatch)
    host_ca = tmp_path / 'host trust.pem'
    host_ca.write_bytes(Path(certifi.where()).read_bytes())
    monkeypatch.setattr(linux_package, 'SYSTEM_CA_FILES', (host_ca,))
    assert linux_package.configure_packaged_ca() == 'linux_system_bundle'
    assert os.environ['SSL_CERT_FILE'] == str(host_ca)
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()['x509_ca'] > 0


def test_linux_package_falls_back_only_when_host_bundle_is_absent(tmp_path, monkeypatch):
    frozen_linux(monkeypatch)
    monkeypatch.setattr(linux_package, 'SYSTEM_CA_FILES', (tmp_path / 'missing.pem',))
    assert linux_package.configure_packaged_ca() == 'bundled_certifi'
    assert os.environ['SSL_CERT_FILE'] == certifi.where()


def test_linux_package_rejects_invalid_host_bundle(tmp_path, monkeypatch):
    frozen_linux(monkeypatch)
    invalid = tmp_path / 'invalid.pem'
    invalid.write_text('invalid certificate fixture')
    monkeypatch.setattr(linux_package, 'SYSTEM_CA_FILES', (invalid,))
    with pytest.raises(PackageError, match='linux_ca_bundle_invalid'):
        linux_package.configure_packaged_ca()
    assert 'SSL_CERT_FILE' not in os.environ


@pytest.mark.parametrize('name', ['SSL_CERT_FILE', 'SSL_CERT_DIR'])
def test_linux_package_preserves_explicit_certificate_settings(monkeypatch, name):
    frozen_linux(monkeypatch)
    monkeypatch.setenv(name, '')
    before = dict(os.environ)
    assert linux_package.configure_packaged_ca() == 'environment'
    assert dict(os.environ) == before


@pytest.mark.parametrize('platform,frozen', [('linux', False), ('darwin', True), ('win32', True)])
def test_linux_ca_selection_does_not_change_source_or_other_platforms(monkeypatch, platform, frozen):
    monkeypatch.setattr(linux_package.sys, 'platform', platform)
    monkeypatch.setattr(linux_package.sys, 'frozen', frozen, raising=False)
    before = dict(os.environ)
    assert linux_package.configure_packaged_ca() == 'source_or_other_platform'
    assert dict(os.environ) == before
