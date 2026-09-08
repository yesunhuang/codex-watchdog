from pathlib import Path
import ssl

import certifi
import pytest

from codex_watchdog import macos_package as package


@pytest.fixture
def frozen_mac(monkeypatch):
    monkeypatch.setattr(package.sys, "platform", "darwin")
    monkeypatch.setattr(package.sys, "frozen", True, raising=False)
    # Record both original keys so direct process-local configuration is undone.
    for variable in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.setenv(variable, "fixture-before-clear")
        monkeypatch.delenv(variable)


def test_frozen_mac_selects_valid_system_bundle(tmp_path, monkeypatch, frozen_mac):
    bundle = tmp_path / "system CA.pem"
    bundle.write_bytes(Path(certifi.where()).read_bytes())
    monkeypatch.setattr(package, "SYSTEM_CA_FILE", bundle)
    assert package.configure_packaged_ca() == "macos_system_bundle"
    assert package.os.environ["SSL_CERT_FILE"] == str(bundle)
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()["x509_ca"] > 0
    assert package.configure_packaged_ca() == "environment"


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
@pytest.mark.parametrize("value", ["", "/operator/selected/trust"])
def test_explicit_ca_selection_is_preserved(monkeypatch, frozen_mac, variable, value):
    monkeypatch.setenv(variable, value)
    before = dict(package.os.environ)
    assert package.configure_packaged_ca() == "environment"
    assert dict(package.os.environ) == before


def test_missing_system_bundle_uses_shipped_certifi(tmp_path, monkeypatch, frozen_mac):
    monkeypatch.setattr(package, "SYSTEM_CA_FILE", tmp_path / "missing")
    assert package.configure_packaged_ca() == "bundled_certifi"
    assert package.os.environ["SSL_CERT_FILE"] == certifi.where()


def test_bad_system_bundle_fails_closed_without_environment_change(tmp_path, monkeypatch, frozen_mac):
    bundle = tmp_path / "private path.pem"
    bundle.write_text("not a certificate")
    monkeypatch.setattr(package, "SYSTEM_CA_FILE", bundle)
    with pytest.raises(package.PackageError, match="^macos_ca_bundle_invalid$"):
        package.configure_packaged_ca()
    assert "SSL_CERT_FILE" not in package.os.environ


def test_source_environment_is_not_reconfigured(monkeypatch, frozen_mac):
    monkeypatch.setattr(package.sys, "frozen", False)
    assert package.configure_packaged_ca() == "source_or_other_platform"
    assert "SSL_CERT_FILE" not in package.os.environ


def test_tls_probe_uses_verified_context_without_credentials(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b'{"ok": true}'

    def open_request(request, *, context, timeout):
        assert request.full_url == "https://slack.com/api/api.test"
        assert request.data == b"" and request.get_method() == "POST"
        assert "Authorization" not in request.headers
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert timeout == 15
        return Response()

    monkeypatch.setattr(package.urllib.request, "urlopen", open_request)
    assert package.check_slack_tls("environment") == {
        "status": "passed", "ca_source": "environment", "tls_verification": True,
        "endpoint": "slack_api_test", "authenticated": False,
    }
