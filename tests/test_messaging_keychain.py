"""Mac credential writes retain the existing security-tool reader identity."""
import json
import os
import subprocess
import sys

import pytest

from codex_watchdog import messaging_profile as profile


def test_keychain_write_uses_one_stdin_command_without_secret_argv(tmp_path, monkeypatch):
    value = "fixture 'quotes' \"double\" \\ $() `literal`"
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[1:] == ["-q", "-i"]:
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[-1] == "-w":
            return subprocess.CompletedProcess(args, 0, value.encode() + b"\n", b"")
        return subprocess.CompletedProcess(args, 44, b"", b"")
    monkeypatch.setattr(profile.subprocess, "run", run)
    store = profile.SecretStore(tmp_path, platform="darwin", environment={})
    store.put("fixture-service", "fixture-account", value)
    written = [kwargs["input"] for args, kwargs in calls if args[1:] == ["-q", "-i"]]
    assert written == [("add-generic-password -a \"fixture-account\" -s \"fixture-service\" -w " +
                        json.dumps(value, ensure_ascii=False) + "\n").encode()]
    assert all(value not in " ".join(args) and not kwargs.get("shell") for args, kwargs in calls)
    assert b"-U" not in written[0] and b"-A" not in written[0]


@pytest.mark.parametrize("value", ["line\nbreak", "carriage\rreturn", "null\x00byte", "delete\x7fbyte", "x" * 4096])
def test_keychain_write_rejects_command_boundaries_and_truncation(tmp_path, monkeypatch, value):
    store = profile.SecretStore(tmp_path, platform="darwin", environment={})
    monkeypatch.setattr(store, "has", lambda *args: False)
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid credential must not reach the native tool")
    monkeypatch.setattr(store, "_security", forbidden)
    with pytest.raises(profile.MessagingError, match="messaging_credential_invalid"):
        store.put("fixture-service", "fixture-account", value)


def test_keychain_write_failure_is_not_reported_as_saved(tmp_path, monkeypatch):
    store = profile.SecretStore(tmp_path, platform="darwin", environment={})
    monkeypatch.setattr(store, "has", lambda *args: False)
    monkeypatch.setattr(store, "_security", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, b"", b""))
    with pytest.raises(profile.MessagingError, match="messaging_keychain_store_failed"):
        store.put("fixture-service", "fixture-account", "fixture-only")


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS Keychain fixture")
def test_native_keychain_write_read_and_duplicate_preservation(tmp_path):
    keychain = tmp_path / "fixture.keychain-db"
    environment = dict(os.environ, PACKAGE_TEST_KEYCHAIN=str(keychain))
    security = "/usr/bin/security"
    def command(*args, accepted=(0,)):
        result = subprocess.run([security, *map(str, args)], env=environment, capture_output=True, timeout=15)
        assert result.returncode in accepted, "disposable Keychain fixture command failed"
        return result.returncode, result.stdout

    prior_search = command("list-keychains", "-d", "user")
    # The isolated HOME may have no default Keychain; preserve that too.
    prior_default = command("default-keychain", "-d", "user", accepted=(0, 1))
    command("create-keychain", "-p", "onboarding-fixture-only", keychain)
    try:
        command("unlock-keychain", "-p", "onboarding-fixture-only", keychain)
        wrapper = tmp_path / "fixture-security"
        # Keep the production command and stdin bytes; only target the
        # disposable Keychain. No shell evaluation of the input takes place.
        wrapper.write_text('''#!/bin/sh
if [ "$1" = "-q" ] && [ "$2" = "-i" ]; then
    IFS= read -r command
    printf '%s "%s"\\n' "$command" "$PACKAGE_TEST_KEYCHAIN" | /usr/bin/security "$@"
else
    exec /usr/bin/security "$@" "$PACKAGE_TEST_KEYCHAIN"
fi
''')
        wrapper.chmod(0o700)
        environment["CODEX_WATCHDOG_MACOS_SECURITY_BIN"] = str(wrapper)
        store = profile.SecretStore(tmp_path, environment=environment)
        service, account = "org.localcodexwatchdog.fixture", "onboarding-fixture"
        value = "fixture 'quotes' \"double\" \\ $() `literal`"
        assert not store.has(service, account)
        store.put(service, account, value)
        assert store.has(service, account)
        assert store.get(service, account) == value
        with pytest.raises(profile.MessagingError, match="messaging_credential_already_exists"):
            store.put(service, account, "replacement")
        assert store.get(service, account) == value
    finally:
        command("delete-keychain", keychain)
    assert command("list-keychains", "-d", "user") == prior_search
    assert command("default-keychain", "-d", "user", accepted=(0, 1)) == prior_default
