from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Sequence
from urllib.parse import urlparse


OUTLOOK_AUTHORITY = "https://login.microsoftonline.com/consumers"
OUTLOOK_SMTP_SCOPES = ("https://outlook.office.com/SMTP.Send",)


PersistenceFactory = Callable[[str], Any]
TokenCacheFactory = Callable[[Any], Any]
PublicClientFactory = Callable[..., Any]
HttpClientFactory = Callable[[float], Any]
DeviceCodeDisplay = Callable[["OutlookDeviceCodePrompt"], None]


class OutlookOAuthError(RuntimeError):
    """A privacy-safe Outlook OAuth failure with a stable machine code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OutlookDeviceCodePrompt:
    """The short-lived, deliberately displayed portion of a device flow."""

    verification_uri: str
    user_code: str
    expires_in_seconds: int


@dataclass(frozen=True)
class OutlookOAuthLoginResult:
    status: str = "authenticated"

    def to_dict(self) -> Dict[str, str]:
        return {"status": self.status}


def _require_clean_ascii(value: str, *, code: str) -> str:
    if not isinstance(value, str):
        raise OutlookOAuthError(code)
    stripped = value.strip()
    if (
        not stripped
        or not stripped.isascii()
        or any(
            ord(character) < 32 or 127 <= ord(character) <= 159
            for character in stripped
        )
    ):
        raise OutlookOAuthError(code)
    return stripped


def _default_cache_path(
    client_id: str, environment: Optional[Mapping[str, str]] = None
) -> Path:
    source = os.environ if environment is None else environment
    if os.name == "nt":
        local_app_data = source.get("LOCALAPPDATA")
        if not isinstance(local_app_data, str) or not local_app_data.strip():
            raise OutlookOAuthError("local_app_data_unavailable")
        base = Path(local_app_data.strip()) / "CodexWatchdog"
    else:
        xdg_data_home = source.get("XDG_DATA_HOME")
        if isinstance(xdg_data_home, str) and xdg_data_home.strip():
            base = Path(xdg_data_home.strip()) / "codex-watchdog"
        else:
            base = Path.home() / ".local" / "share" / "codex-watchdog"
    client_digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()
    return base / "oauth" / f"outlook-{client_digest}.bin"


def _default_persistence_factory(location: str) -> Any:
    try:
        from msal_extensions import build_encrypted_persistence
    except (ImportError, ModuleNotFoundError):
        raise OutlookOAuthError("oauth_dependencies_unavailable") from None
    try:
        return build_encrypted_persistence(location)
    except Exception:
        raise OutlookOAuthError("encrypted_cache_unavailable") from None


def _default_token_cache_factory(persistence: Any) -> Any:
    try:
        from msal_extensions import PersistedTokenCache
    except (ImportError, ModuleNotFoundError):
        raise OutlookOAuthError("oauth_dependencies_unavailable") from None
    try:
        return PersistedTokenCache(persistence)
    except Exception:
        raise OutlookOAuthError("encrypted_cache_unavailable") from None


class _TimeoutHttpClient:
    """MSAL-compatible HTTP client that applies a finite default timeout."""

    def __init__(self, timeout_seconds: float) -> None:
        try:
            import requests
        except (ImportError, ModuleNotFoundError):
            raise OutlookOAuthError("oauth_dependencies_unavailable") from None
        self._session = requests.Session()
        self._timeout_seconds = timeout_seconds

    def get(self, url: str, **kwargs: Any) -> Any:
        kwargs["timeout"] = self._bounded_timeout(kwargs.get("timeout"))
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        kwargs["timeout"] = self._bounded_timeout(kwargs.get("timeout"))
        return self._session.post(url, **kwargs)

    def _bounded_timeout(self, requested: Any) -> float:
        if isinstance(requested, bool):
            return self._timeout_seconds
        try:
            parsed = float(requested)
        except (TypeError, ValueError):
            return self._timeout_seconds
        if parsed <= 0:
            return self._timeout_seconds
        return min(parsed, self._timeout_seconds)


def _default_http_client_factory(timeout_seconds: float) -> Any:
    return _TimeoutHttpClient(timeout_seconds)


def _default_public_client_factory(**kwargs: Any) -> Any:
    try:
        import msal
    except (ImportError, ModuleNotFoundError):
        raise OutlookOAuthError("oauth_dependencies_unavailable") from None
    try:
        return msal.PublicClientApplication(**kwargs)
    except Exception:
        raise OutlookOAuthError("oauth_client_initialization_failed") from None


class OutlookOAuthTokenProvider:
    """Acquire Outlook SMTP tokens without interactive work on notification paths."""

    def __init__(
        self,
        client_id: str,
        username: str,
        *,
        cache_path: Optional[Path] = None,
        environment: Optional[Mapping[str, str]] = None,
        timeout_seconds: float = 10.0,
        persistence_factory: Optional[PersistenceFactory] = None,
        token_cache_factory: Optional[TokenCacheFactory] = None,
        public_client_factory: Optional[PublicClientFactory] = None,
        http_client_factory: Optional[HttpClientFactory] = None,
    ) -> None:
        self.client_id = _require_clean_ascii(
            client_id, code="outlook_client_id_invalid"
        )
        self.username = _require_clean_ascii(username, code="outlook_username_invalid")
        if isinstance(timeout_seconds, bool):
            raise OutlookOAuthError("oauth_timeout_invalid")
        try:
            parsed_timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            raise OutlookOAuthError("oauth_timeout_invalid") from None
        if parsed_timeout <= 0:
            raise OutlookOAuthError("oauth_timeout_invalid")

        selected_cache_path = (
            Path(cache_path)
            if cache_path is not None
            else _default_cache_path(self.client_id, environment)
        )
        self.cache_path = selected_cache_path.expanduser().resolve()
        self.timeout_seconds = parsed_timeout
        self._persistence_factory = (
            persistence_factory
            if persistence_factory is not None
            else _default_persistence_factory
        )
        self._token_cache_factory = (
            token_cache_factory
            if token_cache_factory is not None
            else _default_token_cache_factory
        )
        self._public_client_factory = (
            public_client_factory
            if public_client_factory is not None
            else _default_public_client_factory
        )
        self._http_client_factory = (
            http_client_factory
            if http_client_factory is not None
            else _default_http_client_factory
        )
        self._client: Any = None
        self._lock = threading.RLock()

    def get_access_token(self) -> str:
        """Return a cached/refreshed token, never starting an interactive flow."""

        with self._lock:
            client = self._get_client()
            account = self._select_account(
                client, missing_code="reauthentication_required"
            )
            try:
                result = client.acquire_token_silent(
                    scopes=list(OUTLOOK_SMTP_SCOPES), account=account
                )
            except Exception:
                raise OutlookOAuthError("token_acquisition_failed") from None
            if not isinstance(result, dict):
                raise OutlookOAuthError("reauthentication_required")
            token = result.get("access_token")
            if not self._valid_token(token):
                if result.get("error"):
                    raise OutlookOAuthError("token_acquisition_failed")
                raise OutlookOAuthError("reauthentication_required")
            return token

    def login_device_code(self, display: DeviceCodeDisplay) -> OutlookOAuthLoginResult:
        """Run the one explicit interactive bootstrap and retain only MSAL's cache."""

        if not callable(display):
            raise OutlookOAuthError("device_prompt_invalid")
        with self._lock:
            client = self._get_client()
            try:
                flow = client.initiate_device_flow(scopes=list(OUTLOOK_SMTP_SCOPES))
            except Exception:
                raise OutlookOAuthError("device_flow_initialization_failed") from None
            prompt = self._device_prompt(flow)
            try:
                display(prompt)
            except Exception:
                raise OutlookOAuthError("device_prompt_failed") from None
            try:
                result = client.acquire_token_by_device_flow(flow)
            except Exception:
                raise OutlookOAuthError("device_login_failed") from None
            if not isinstance(result, dict) or not self._valid_token(
                result.get("access_token")
            ):
                raise OutlookOAuthError("device_login_failed")
            self._select_account(client, missing_code="authenticated_account_mismatch")
            return OutlookOAuthLoginResult()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise OutlookOAuthError("encrypted_cache_unavailable") from None
        try:
            persistence = self._persistence_factory(str(self.cache_path))
        except OutlookOAuthError:
            raise
        except Exception:
            raise OutlookOAuthError("encrypted_cache_unavailable") from None
        try:
            encrypted = persistence.is_encrypted
        except Exception:
            raise OutlookOAuthError("encrypted_cache_unavailable") from None
        if encrypted is not True:
            raise OutlookOAuthError("encrypted_cache_required")
        try:
            token_cache = self._token_cache_factory(persistence)
        except OutlookOAuthError:
            raise
        except Exception:
            raise OutlookOAuthError("encrypted_cache_unavailable") from None
        try:
            http_client = self._http_client_factory(self.timeout_seconds)
        except OutlookOAuthError:
            raise
        except Exception:
            raise OutlookOAuthError("oauth_client_initialization_failed") from None
        try:
            client = self._public_client_factory(
                client_id=self.client_id,
                authority=OUTLOOK_AUTHORITY,
                token_cache=token_cache,
                http_client=http_client,
            )
        except OutlookOAuthError:
            raise
        except Exception:
            raise OutlookOAuthError("oauth_client_initialization_failed") from None
        self._client = client
        return client

    def _select_account(self, client: Any, *, missing_code: str) -> Dict[str, Any]:
        try:
            accounts = client.get_accounts(username=self.username)
        except Exception:
            raise OutlookOAuthError("token_cache_unavailable") from None
        if not isinstance(accounts, Sequence) or isinstance(accounts, (str, bytes)):
            raise OutlookOAuthError("token_cache_unavailable")
        if not accounts:
            raise OutlookOAuthError(missing_code)
        if len(accounts) != 1:
            raise OutlookOAuthError("outlook_account_ambiguous")
        account = accounts[0]
        if not isinstance(account, dict):
            raise OutlookOAuthError("token_cache_unavailable")
        account_username = account.get("username")
        if (
            not isinstance(account_username, str)
            or account_username.casefold() != self.username.casefold()
        ):
            raise OutlookOAuthError(missing_code)
        return account

    @staticmethod
    def _valid_token(token: Any) -> bool:
        return (
            isinstance(token, str)
            and bool(token)
            and token.isascii()
            and not any(
                character.isspace()
                or ord(character) < 32
                or 127 <= ord(character) <= 159
                for character in token
            )
        )

    @staticmethod
    def _device_prompt(flow: Any) -> OutlookDeviceCodePrompt:
        if not isinstance(flow, dict):
            raise OutlookOAuthError("device_flow_initialization_failed")
        verification_uri = flow.get("verification_uri")
        user_code = flow.get("user_code")
        expires_in = flow.get("expires_in")
        if not isinstance(verification_uri, str) or not verification_uri:
            raise OutlookOAuthError("device_flow_initialization_failed")
        parsed_uri = urlparse(verification_uri)
        hostname = (parsed_uri.hostname or "").lower()
        if (
            parsed_uri.scheme.lower() != "https"
            or (hostname != "microsoft.com" and not hostname.endswith(".microsoft.com"))
            or parsed_uri.username is not None
            or parsed_uri.password is not None
        ):
            raise OutlookOAuthError("device_flow_initialization_failed")
        try:
            clean_code = _require_clean_ascii(
                user_code, code="device_flow_initialization_failed"
            )
        except OutlookOAuthError:
            raise OutlookOAuthError("device_flow_initialization_failed") from None
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in <= 0
        ):
            raise OutlookOAuthError("device_flow_initialization_failed")
        return OutlookDeviceCodePrompt(
            verification_uri=verification_uri,
            user_code=clean_code,
            expires_in_seconds=expires_in,
        )
