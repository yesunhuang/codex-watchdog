from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from codex_watchdog.outlook_oauth import (
    OUTLOOK_AUTHORITY,
    OUTLOOK_SMTP_SCOPES,
    OutlookDeviceCodePrompt,
    OutlookOAuthError,
    OutlookOAuthTokenProvider,
    _TimeoutHttpClient,
)


CLIENT_ID = "11111111-2222-4333-8444-555555555555"
USERNAME = "watchdog-owner@outlook.com"
ACCESS_TOKEN = "opaque-access-token"
REFRESH_TOKEN = "opaque-refresh-token"
DEVICE_CODE = "secret-device-code"


class FakePersistence:
    def __init__(self, encrypted: bool = True) -> None:
        self.is_encrypted = encrypted


class FakeClient:
    def __init__(self) -> None:
        self.accounts: Any = [{"username": USERNAME, "home_account_id": "account-1"}]
        self.silent_result: Any = {"access_token": ACCESS_TOKEN}
        self.device_flow: Any = {
            "verification_uri": "https://microsoft.com/devicelogin",
            "user_code": "ABCD-EFGH",
            "expires_in": 900,
            "device_code": DEVICE_CODE,
            "message": f"untrusted message containing {DEVICE_CODE}",
        }
        self.device_result: Any = {
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
        }
        self.calls: List[Any] = []

    def get_accounts(self, username: str) -> Any:
        self.calls.append(("get_accounts", username))
        return self.accounts

    def acquire_token_silent(
        self, *, scopes: List[str], account: Dict[str, Any]
    ) -> Any:
        self.calls.append(("acquire_token_silent", scopes, account))
        return self.silent_result

    def initiate_device_flow(self, *, scopes: List[str]) -> Any:
        self.calls.append(("initiate_device_flow", scopes))
        return self.device_flow

    def acquire_token_by_device_flow(self, flow: Dict[str, Any]) -> Any:
        self.calls.append(("acquire_token_by_device_flow", flow))
        return self.device_result


class Harness:
    def __init__(
        self, tmp_path: Path, *, encrypted: bool = True, client: Optional[Any] = None
    ) -> None:
        self.cache_path = tmp_path / "credentials" / "tokens.bin"
        self.persistence = FakePersistence(encrypted)
        self.token_cache = object()
        self.http_client = object()
        self.client = FakeClient() if client is None else client
        self.calls: List[Any] = []

    def persistence_factory(self, location: str) -> FakePersistence:
        self.calls.append(("persistence", location))
        return self.persistence

    def token_cache_factory(self, persistence: Any) -> Any:
        self.calls.append(("token_cache", persistence))
        return self.token_cache

    def http_client_factory(self, timeout: float) -> Any:
        self.calls.append(("http_client", timeout))
        return self.http_client

    def public_client_factory(self, **kwargs: Any) -> Any:
        self.calls.append(("public_client", kwargs))
        return self.client

    def provider(self, **kwargs: Any) -> OutlookOAuthTokenProvider:
        return OutlookOAuthTokenProvider(
            CLIENT_ID,
            USERNAME,
            cache_path=self.cache_path,
            persistence_factory=self.persistence_factory,
            token_cache_factory=self.token_cache_factory,
            public_client_factory=self.public_client_factory,
            http_client_factory=self.http_client_factory,
            **kwargs,
        )


def test_default_cache_path_is_user_local_and_hides_account_identity(
    tmp_path: Path,
) -> None:
    provider = OutlookOAuthTokenProvider(
        CLIENT_ID, USERNAME, environment={"LOCALAPPDATA": str(tmp_path)},
    )

    digest = hashlib.sha256(CLIENT_ID.encode("utf-8")).hexdigest()
    assert (
        provider.cache_path
        == (tmp_path / "CodexWatchdog" / "oauth" / f"outlook-{digest}.bin").resolve()
    )
    assert USERNAME not in str(provider.cache_path)


@pytest.mark.parametrize(
    ("client_id", "username", "timeout", "code"),
    [
        ("", USERNAME, 10, "outlook_client_id_invalid"),
        (CLIENT_ID, "owner\n@example.com", 10, "outlook_username_invalid"),
        (CLIENT_ID, USERNAME, 0, "oauth_timeout_invalid"),
        (CLIENT_ID, USERNAME, True, "oauth_timeout_invalid"),
    ],
)
def test_configuration_validation_is_stable_and_privacy_safe(
    tmp_path: Path, client_id: str, username: str, timeout: Any, code: str
) -> None:
    with pytest.raises(OutlookOAuthError) as captured:
        OutlookOAuthTokenProvider(
            client_id,
            username,
            cache_path=tmp_path / "cache.bin",
            timeout_seconds=timeout,
        )

    assert captured.value.code == code
    assert str(captured.value) == code
    assert USERNAME not in str(captured.value)


def test_silent_token_uses_fixed_authority_scope_and_encrypted_cache(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    provider = harness.provider(timeout_seconds=4.5)

    assert provider.get_access_token() == ACCESS_TOKEN
    assert harness.calls[0] == ("persistence", str(harness.cache_path.resolve()))
    assert harness.calls[1] == ("token_cache", harness.persistence)
    assert harness.calls[2] == ("http_client", 4.5)
    public_call = harness.calls[3]
    assert public_call[0] == "public_client"
    assert public_call[1] == {
        "client_id": CLIENT_ID,
        "authority": OUTLOOK_AUTHORITY,
        "token_cache": harness.token_cache,
        "http_client": harness.http_client,
    }
    assert harness.client.calls == [
        ("get_accounts", USERNAME),
        (
            "acquire_token_silent",
            list(OUTLOOK_SMTP_SCOPES),
            harness.client.accounts[0],
        ),
    ]
    assert "offline_access" not in OUTLOOK_SMTP_SCOPES


def test_provider_initializes_client_once_and_refreshes_silently_each_time(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    provider = harness.provider()

    assert provider.get_access_token() == ACCESS_TOKEN
    harness.client.silent_result = {"access_token": "refreshed-access-token"}
    assert provider.get_access_token() == "refreshed-access-token"

    assert [call[0] for call in harness.calls] == [
        "persistence",
        "token_cache",
        "http_client",
        "public_client",
    ]
    assert [call[0] for call in harness.client.calls] == [
        "get_accounts",
        "acquire_token_silent",
        "get_accounts",
        "acquire_token_silent",
    ]


def test_unencrypted_persistence_fails_closed_without_plaintext_fallback(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, encrypted=False)

    with pytest.raises(OutlookOAuthError) as captured:
        harness.provider().get_access_token()

    assert captured.value.code == "encrypted_cache_required"
    assert harness.calls == [("persistence", str(harness.cache_path.resolve()))]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI acceptance")
def test_real_msal_extensions_cache_is_dpapi_encrypted_at_rest(tmp_path: Path) -> None:
    try:
        from msal_extensions import build_encrypted_persistence
    except (ImportError, ModuleNotFoundError):
        pytest.skip("msal-extensions is unavailable")

    cache_path = tmp_path / "real-dpapi-cache.bin"
    try:
        persistence = build_encrypted_persistence(str(cache_path))
    except Exception as exc:
        pytest.skip(f"platform encryption unavailable: {type(exc).__name__}")

    payload = json.dumps(
        {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}, sort_keys=True,
    )
    persistence.save(payload)

    raw = cache_path.read_bytes()
    assert persistence.is_encrypted is True
    assert ACCESS_TOKEN.encode("ascii") not in raw
    assert REFRESH_TOKEN.encode("ascii") not in raw
    assert persistence.load() == payload


def test_persistence_failure_does_not_leak_underlying_secret(tmp_path: Path) -> None:
    secret = REFRESH_TOKEN
    calls = []

    def fail(location: str) -> Any:
        calls.append(location)
        raise RuntimeError(f"could not save {secret}")

    provider = OutlookOAuthTokenProvider(
        CLIENT_ID,
        USERNAME,
        cache_path=tmp_path / "cache.bin",
        persistence_factory=fail,
    )

    with pytest.raises(OutlookOAuthError) as captured:
        provider.get_access_token()

    assert captured.value.code == "encrypted_cache_unavailable"
    assert secret not in str(captured.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("accounts", "code"),
    [
        ([], "reauthentication_required"),
        (
            [{"username": USERNAME}, {"username": USERNAME},],
            "outlook_account_ambiguous",
        ),
        ([{"username": "different@outlook.com"}], "reauthentication_required"),
    ],
)
def test_missing_ambiguous_or_wrong_cached_account_fails_before_token_request(
    tmp_path: Path, accounts: List[Dict[str, Any]], code: str
) -> None:
    harness = Harness(tmp_path)
    harness.client.accounts = accounts

    with pytest.raises(OutlookOAuthError) as captured:
        harness.provider().get_access_token()

    assert captured.value.code == code
    assert harness.client.calls == [("get_accounts", USERNAME)]


def test_silent_failure_response_is_not_rendered_or_persisted(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.client.silent_result = {
        "error": "invalid_grant",
        "error_description": f"server included {REFRESH_TOKEN}",
    }

    with pytest.raises(OutlookOAuthError) as captured:
        harness.provider().get_access_token()

    assert captured.value.code == "token_acquisition_failed"
    assert REFRESH_TOKEN not in str(captured.value)
    assert not harness.cache_path.exists()


@pytest.mark.parametrize(
    "invalid_token", ["", "token with spaces", "token\r\nvalue", "token\x01value", 123],
)
def test_malformed_access_token_fails_closed(
    tmp_path: Path, invalid_token: Any
) -> None:
    harness = Harness(tmp_path)
    harness.client.silent_result = {"access_token": invalid_token}

    with pytest.raises(OutlookOAuthError) as captured:
        harness.provider().get_access_token()

    assert captured.value.code == "reauthentication_required"


def test_device_login_displays_only_safe_prompt_and_returns_no_tokens(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    displayed = []

    result = harness.provider().login_device_code(displayed.append)

    assert displayed == [
        OutlookDeviceCodePrompt(
            verification_uri="https://microsoft.com/devicelogin",
            user_code="ABCD-EFGH",
            expires_in_seconds=900,
        )
    ]
    assert result.to_dict() == {"status": "authenticated"}
    rendered = json.dumps(result.to_dict()) + repr(displayed)
    assert DEVICE_CODE not in rendered
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
    assert harness.client.calls == [
        ("initiate_device_flow", list(OUTLOOK_SMTP_SCOPES)),
        ("acquire_token_by_device_flow", harness.client.device_flow),
        ("get_accounts", USERNAME),
    ]


@pytest.mark.parametrize(
    "flow",
    [
        {},
        {
            "verification_uri": "http://microsoft.com/devicelogin",
            "user_code": "ABCD-EFGH",
            "expires_in": 900,
            "device_code": DEVICE_CODE,
        },
        {
            "verification_uri": "https://evil.example/devicelogin",
            "user_code": "ABCD-EFGH",
            "expires_in": 900,
            "device_code": DEVICE_CODE,
        },
        {
            "verification_uri": "https://microsoft.com/devicelogin",
            "user_code": "BAD\nCODE",
            "expires_in": 900,
            "device_code": DEVICE_CODE,
        },
    ],
)
def test_malformed_device_flow_is_never_displayed(
    tmp_path: Path, flow: Dict[str, Any]
) -> None:
    harness = Harness(tmp_path)
    harness.client.device_flow = flow
    displayed = []

    with pytest.raises(OutlookOAuthError) as captured:
        harness.provider().login_device_code(displayed.append)

    assert captured.value.code == "device_flow_initialization_failed"
    assert displayed == []
    assert DEVICE_CODE not in str(captured.value)


def test_device_login_failure_and_account_mismatch_are_privacy_safe(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.client.device_result = {
        "error": "authorization_declined",
        "error_description": f"contains {DEVICE_CODE}",
    }

    with pytest.raises(OutlookOAuthError) as captured:
        harness.provider().login_device_code(lambda _prompt: None)

    assert captured.value.code == "device_login_failed"
    assert DEVICE_CODE not in str(captured.value)

    second = Harness(tmp_path / "second")
    second.client.accounts = []
    with pytest.raises(OutlookOAuthError) as mismatch:
        second.provider().login_device_code(lambda _prompt: None)
    assert mismatch.value.code == "authenticated_account_mismatch"


def test_device_prompt_callback_failure_is_sanitized_and_stops_polling(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)

    def fail(_prompt: OutlookDeviceCodePrompt) -> None:
        raise RuntimeError(f"display leaked {DEVICE_CODE}")

    with pytest.raises(OutlookOAuthError) as captured:
        harness.provider().login_device_code(fail)

    assert captured.value.code == "device_prompt_failed"
    assert DEVICE_CODE not in str(captured.value)
    assert [call[0] for call in harness.client.calls] == ["initiate_device_flow"]


def test_timeout_http_client_bounds_default_and_preserves_tighter_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests_calls = []

    class FakeSession:
        def get(self, url: str, **kwargs: Any) -> str:
            requests_calls.append(("get", url, kwargs))
            return "get-response"

        def post(self, url: str, **kwargs: Any) -> str:
            requests_calls.append(("post", url, kwargs))
            return "post-response"

    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(Session=FakeSession))
    client = _TimeoutHttpClient(7.5)

    assert client.get("https://login.microsoftonline.com/metadata") == "get-response"
    assert (
        client.post("https://login.microsoftonline.com/token", timeout=2)
        == "post-response"
    )
    assert (
        client.post("https://login.microsoftonline.com/token", timeout=120)
        == "post-response"
    )
    assert requests_calls == [
        ("get", "https://login.microsoftonline.com/metadata", {"timeout": 7.5},),
        ("post", "https://login.microsoftonline.com/token", {"timeout": 2},),
        ("post", "https://login.microsoftonline.com/token", {"timeout": 7.5},),
    ]
