from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import subprocess
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
import urllib.request
import uuid

from .models import sha256_text, utc_now
from .slack_mapping import (
    SlackRelayTarget,
    SlackThreadStore,
    valid_slack_channel_id,
    valid_slack_timestamp,
    valid_slack_user_id,
)
from .storage import FileLock, InstructionStore


NOTIFICATION_STATE_SCHEMA_VERSION = 1

SLACK_WEBHOOK_ENV = "CODEX_WATCHDOG_SLACK_WEBHOOK_URL"
SLACK_BOT_TOKEN_ENV = "CODEX_WATCHDOG_SLACK_BOT_TOKEN"
SLACK_APP_TOKEN_ENV = "CODEX_WATCHDOG_SLACK_APP_TOKEN"
SLACK_CHANNEL_ID_ENV = "CODEX_WATCHDOG_SLACK_CHANNEL_ID"
SLACK_ALLOWED_USER_IDS_ENV = "CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS"
SMTP_HOST_ENV = "CODEX_WATCHDOG_SMTP_HOST"
SMTP_PORT_ENV = "CODEX_WATCHDOG_SMTP_PORT"
SMTP_USERNAME_ENV = "CODEX_WATCHDOG_SMTP_USERNAME"
SMTP_PASSWORD_ENV = "CODEX_WATCHDOG_SMTP_PASSWORD"
SMTP_FROM_ENV = "CODEX_WATCHDOG_SMTP_FROM"
SMTP_TO_ENV = "CODEX_WATCHDOG_SMTP_TO"
SMTP_SECURITY_ENV = "CODEX_WATCHDOG_SMTP_SECURITY"
SMTP_AUTH_ENV = "CODEX_WATCHDOG_SMTP_AUTH"
OUTLOOK_CLIENT_ID_ENV = "CODEX_WATCHDOG_OUTLOOK_CLIENT_ID"
WINDOWS_MSG_ENV = "CODEX_WATCHDOG_WINDOWS_MSG"
WINDOWS_MSG_TARGET_ENV = "CODEX_WATCHDOG_WINDOWS_MSG_TARGET"
NOTIFICATION_TIMEOUT_ENV = "CODEX_WATCHDOG_NOTIFICATION_TIMEOUT_SECONDS"

_SMTP_SECURITY_VALUES = frozenset({"plain", "starttls", "ssl"})
_SMTP_AUTH_VALUES = frozenset({"password", "outlook_oauth2"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
_OUTLOOK_SMTP_HOST = "smtp-mail.outlook.com"

HttpPost = Callable[[str, bytes, float], int]
SlackApiPost = Callable[[str, str, Dict[str, Any], float], Dict[str, Any]]
SmtpFactory = Callable[[str, int, float, bool], Any]
MessageRunner = Callable[[Sequence[str], float], int]
AtomicWriter = Callable[[Path, Dict[str, Any]], None]
OutlookAccessToken = Callable[[str, str, float], str]


def _notification_label_component(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or any(
        ord(character) < 32 or ord(character) == 127 for character in candidate
    ):
        return None
    return candidate


def notification_workspace_label(
    workspace_id: str, repo_root: Optional[Path], *, locality: Optional[str] = None,
) -> str:
    """Return a safe presentation label without changing durable identity."""

    fallback = _notification_label_component(workspace_id)
    if fallback is None:
        raise ValueError("workspace id must provide a safe notification fallback")
    try:
        repo_label = (
            _notification_label_component(Path(repo_root).name)
            if repo_root is not None
            else None
        )
    except (OSError, TypeError, ValueError):
        repo_label = None
    label = repo_label or fallback
    locality_label = _notification_label_component(locality)
    return f"{label} @ {locality_label}" if locality_label is not None else label


def _optional_environment_value(
    environment: Mapping[str, str], name: str
) -> Optional[str]:
    value = environment.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _parse_positive_float(value: Optional[str], *, default: float, name: str) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def _parse_port(value: Optional[str], *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{SMTP_PORT_ENV} must be an integer from 1 to 65535") from exc
    if not 1 <= parsed <= 65535:
        raise ValueError(f"{SMTP_PORT_ENV} must be an integer from 1 to 65535")
    return parsed


def _parse_opt_in(value: Optional[str], *, name: str) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be an explicit true or false value")


def _split_recipients(value: Optional[str]) -> Tuple[str, ...]:
    if value is None:
        return ()
    return tuple(part.strip() for part in re.split(r"[,;]", value) if part.strip())


def _split_slack_user_ids(value: Optional[str]) -> Tuple[str, ...]:
    if value is None:
        return ()
    return tuple(
        dict.fromkeys(
            part.strip() for part in re.split(r"[,;\s]", value) if part.strip()
        )
    )


def _is_canonical_uuid(value: Optional[str]) -> bool:
    if value is None:
        return False
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        return False
    return str(parsed) == value.lower()


@dataclass(frozen=True, repr=False)
class NotificationConfig:
    """Environment-derived transports; repr deliberately excludes all values."""

    slack_webhook_url: Optional[str] = field(default=None, repr=False)
    slack_bot_token: Optional[str] = field(default=None, repr=False)
    slack_app_token: Optional[str] = field(default=None, repr=False)
    slack_channel_id: Optional[str] = field(default=None, repr=False)
    slack_allowed_user_ids: Tuple[str, ...] = field(default=(), repr=False)
    smtp_host: Optional[str] = field(default=None, repr=False)
    smtp_port: int = field(default=587, repr=False)
    smtp_username: Optional[str] = field(default=None, repr=False)
    smtp_password: Optional[str] = field(default=None, repr=False)
    smtp_sender: Optional[str] = field(default=None, repr=False)
    smtp_recipients: Tuple[str, ...] = field(default=(), repr=False)
    smtp_security: str = field(default="starttls", repr=False)
    smtp_auth: str = field(default="password", repr=False)
    outlook_client_id: Optional[str] = field(default=None, repr=False)
    windows_message_enabled: bool = field(default=False, repr=False)
    windows_message_target: Optional[str] = field(default=None, repr=False)
    timeout_seconds: float = field(default=10.0, repr=False)

    def __post_init__(self) -> None:
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP port must be from 1 to 65535")
        if self.smtp_security not in _SMTP_SECURITY_VALUES:
            raise ValueError("SMTP security must be plain, starttls, or ssl")
        if self.smtp_auth not in _SMTP_AUTH_VALUES:
            raise ValueError("SMTP auth must be password or outlook_oauth2")
        if self.timeout_seconds <= 0:
            raise ValueError("notification timeout must be positive")

    @classmethod
    def from_environment(
        cls, environment: Optional[Mapping[str, str]] = None
    ) -> "NotificationConfig":
        source = os.environ if environment is None else environment
        security = (
            _optional_environment_value(source, SMTP_SECURITY_ENV) or "starttls"
        ).lower()
        if security not in _SMTP_SECURITY_VALUES:
            raise ValueError(f"{SMTP_SECURITY_ENV} must be plain, starttls, or ssl")
        auth = (
            _optional_environment_value(source, SMTP_AUTH_ENV) or "password"
        ).lower()
        if auth not in _SMTP_AUTH_VALUES:
            raise ValueError(f"{SMTP_AUTH_ENV} must be password or outlook_oauth2")
        default_port = 465 if security == "ssl" else 587
        windows_enabled = _parse_opt_in(
            _optional_environment_value(source, WINDOWS_MSG_ENV), name=WINDOWS_MSG_ENV,
        )
        windows_target = _optional_environment_value(source, WINDOWS_MSG_TARGET_ENV)
        if windows_target is None and windows_enabled:
            windows_target = _optional_environment_value(source, "USERNAME")
        return cls(
            slack_webhook_url=_optional_environment_value(source, SLACK_WEBHOOK_ENV),
            slack_bot_token=_optional_environment_value(source, SLACK_BOT_TOKEN_ENV),
            slack_app_token=_optional_environment_value(source, SLACK_APP_TOKEN_ENV),
            slack_channel_id=_optional_environment_value(source, SLACK_CHANNEL_ID_ENV),
            slack_allowed_user_ids=_split_slack_user_ids(
                _optional_environment_value(source, SLACK_ALLOWED_USER_IDS_ENV)
            ),
            smtp_host=_optional_environment_value(source, SMTP_HOST_ENV),
            smtp_port=_parse_port(
                _optional_environment_value(source, SMTP_PORT_ENV),
                default=default_port,
            ),
            smtp_username=_optional_environment_value(source, SMTP_USERNAME_ENV),
            smtp_password=_optional_environment_value(source, SMTP_PASSWORD_ENV),
            smtp_sender=_optional_environment_value(source, SMTP_FROM_ENV),
            smtp_recipients=_split_recipients(
                _optional_environment_value(source, SMTP_TO_ENV)
            ),
            smtp_security=security,
            smtp_auth=auth,
            outlook_client_id=_optional_environment_value(
                source, OUTLOOK_CLIENT_ID_ENV
            ),
            windows_message_enabled=windows_enabled,
            windows_message_target=windows_target,
            timeout_seconds=_parse_positive_float(
                _optional_environment_value(source, NOTIFICATION_TIMEOUT_ENV),
                default=10.0,
                name=NOTIFICATION_TIMEOUT_ENV,
            ),
        )

    @property
    def slack_configured(self) -> bool:
        return self.slack_webhook_url is not None or self.slack_relay_configured

    @property
    def slack_relay_configured(self) -> bool:
        return (
            isinstance(self.slack_bot_token, str)
            and self.slack_bot_token.startswith("xoxb-")
            and isinstance(self.slack_app_token, str)
            and self.slack_app_token.startswith("xapp-")
            and valid_slack_channel_id(self.slack_channel_id)
            and bool(self.slack_allowed_user_ids)
            and all(valid_slack_user_id(value) for value in self.slack_allowed_user_ids)
        )

    @property
    def smtp_configured(self) -> bool:
        base_configured = (
            self.smtp_host is not None
            and self.smtp_sender is not None
            and bool(self.smtp_recipients)
        )
        if self.smtp_auth == "outlook_oauth2":
            return (
                base_configured
                and self.smtp_host.lower() == _OUTLOOK_SMTP_HOST
                and self.smtp_port == 587
                and self.smtp_security == "starttls"
                and self.smtp_username is not None
                and self.smtp_password is None
                and self.smtp_sender.casefold() == self.smtp_username.casefold()
                and _is_canonical_uuid(self.outlook_client_id)
            )
        credentials_complete = (self.smtp_username is None) == (
            self.smtp_password is None
        )
        return (
            base_configured and credentials_complete and self.outlook_client_id is None
        )

    @property
    def windows_message_configured(self) -> bool:
        return self.windows_message_enabled and self.windows_message_target is not None

    @property
    def configuration_issues(self) -> Tuple[str, ...]:
        issues = []
        relay_values_present = any(
            (
                self.slack_bot_token,
                self.slack_app_token,
                self.slack_channel_id,
                self.slack_allowed_user_ids,
            )
        )
        if relay_values_present and not self.slack_relay_configured:
            issues.append("slack_relay_configuration_incomplete")
        smtp_values_present = self.smtp_auth == "outlook_oauth2" or any(
            (
                self.smtp_host,
                self.smtp_username,
                self.smtp_password,
                self.smtp_sender,
                self.smtp_recipients,
                self.outlook_client_id,
            )
        )
        if smtp_values_present and not self.smtp_configured:
            issues.append("smtp_configuration_incomplete")
        if self.windows_message_enabled and self.windows_message_target is None:
            issues.append("windows_msg_target_missing")
        return tuple(issues)

    def __repr__(self) -> str:
        return (
            "NotificationConfig("
            f"slack_configured={self.slack_configured!r}, "
            f"slack_relay_configured={self.slack_relay_configured!r}, "
            f"smtp_configured={self.smtp_configured!r}, "
            f"windows_message_configured={self.windows_message_configured!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )


@dataclass(frozen=True)
class NotificationEvent:
    workspace_id: str
    event_type: str
    transition_fingerprint: str
    subject: str
    message: str
    relay_target: Optional[SlackRelayTarget] = None

    def __post_init__(self) -> None:
        for name in ("workspace_id", "event_type", "transition_fingerprint", "subject"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"notification {name} must be a non-empty string")
        if not isinstance(self.message, str):
            raise ValueError("notification message must be a string")
        if (
            self.relay_target is not None
            and self.relay_target.workspace_id != self.workspace_id
        ):
            raise ValueError("notification relay target workspace does not match")

    def dedupe_key(self) -> str:
        stable = json.dumps(
            {"event_type": self.event_type, "workspace_id": self.workspace_id,},
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256_text(stable)

    def event_fingerprint(self) -> str:
        stable = json.dumps(
            {
                "event_type": self.event_type,
                "transition_fingerprint": self.transition_fingerprint,
                "workspace_id": self.workspace_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256_text(stable)


@dataclass(frozen=True)
class NotificationResult:
    status: str
    channel: Optional[str]
    event_fingerprint: str
    duplicate: bool
    attempted_channels: Tuple[str, ...]
    configuration_issues: Tuple[str, ...]
    state_path: Path
    state_persisted: bool
    error_sha256: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "channel": self.channel,
            "event_fingerprint": self.event_fingerprint,
            "duplicate": self.duplicate,
            "attempted_channels": list(self.attempted_channels),
            "configuration_issues": list(self.configuration_issues),
            "state_path": str(self.state_path),
            "state_persisted": self.state_persisted,
            "error_sha256": self.error_sha256,
        }


@dataclass(frozen=True)
class _DeliveryResult:
    status: str
    channel: str
    attempted_channels: Tuple[str, ...]
    error_sha256: Optional[str]


def _default_http_post(url: str, payload: bytes, timeout: float) -> int:
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.getcode())


def _default_slack_api_post(
    token: str, method: str, payload: Dict[str, Any], timeout: float
) -> Dict[str, Any]:
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(response.getcode())
        body = response.read()
    if not 200 <= status < 300:
        raise RuntimeError("Slack API returned a non-success HTTP status")
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Slack API returned malformed JSON") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        error = result.get("error") if isinstance(result, dict) else None
        safe_error = (
            error if isinstance(error, str) and len(error) <= 100 else "unknown"
        )
        raise RuntimeError(f"Slack API rejected chat.postMessage: {safe_error}")
    return result


def _default_smtp_factory(host: str, port: int, timeout: float, use_ssl: bool) -> Any:
    smtp_type = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    return smtp_type(host=host, port=port, timeout=timeout)


def _default_outlook_access_token(
    client_id: str, username: str, timeout_seconds: float
) -> str:
    from .outlook_oauth import OutlookOAuthTokenProvider

    return OutlookOAuthTokenProvider(
        client_id=client_id, username=username, timeout_seconds=timeout_seconds,
    ).get_access_token()


def _default_message_runner(argv: Sequence[str], timeout: float) -> int:
    completed = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        timeout=timeout,
        check=False,
    )
    return int(completed.returncode)


class EnvironmentNotifier:
    """Deliver transition notifications and atomically suppress exact repeats."""

    def __init__(
        self,
        runtime: Path,
        config: Optional[NotificationConfig] = None,
        *,
        environment: Optional[Mapping[str, str]] = None,
        http_post: Optional[HttpPost] = None,
        slack_api_post: Optional[SlackApiPost] = None,
        smtp_factory: Optional[SmtpFactory] = None,
        outlook_access_token: Optional[OutlookAccessToken] = None,
        message_runner: Optional[MessageRunner] = None,
        atomic_writer: Optional[AtomicWriter] = None,
        slack_thread_store: Optional[SlackThreadStore] = None,
    ) -> None:
        if config is not None and environment is not None:
            raise ValueError("provide notification config or environment, not both")
        self.runtime = Path(runtime)
        self.config = (
            config
            if config is not None
            else NotificationConfig.from_environment(environment)
        )
        self.http_post = http_post if http_post is not None else _default_http_post
        self.slack_api_post = (
            slack_api_post if slack_api_post is not None else _default_slack_api_post
        )
        self.smtp_factory = (
            smtp_factory if smtp_factory is not None else _default_smtp_factory
        )
        self.outlook_access_token = (
            outlook_access_token
            if outlook_access_token is not None
            else _default_outlook_access_token
        )
        self.message_runner = (
            message_runner if message_runner is not None else _default_message_runner
        )
        self.atomic_writer = (
            atomic_writer
            if atomic_writer is not None
            else InstructionStore._atomic_json
        )
        self.state_path = self.runtime / "notifications" / "last-events.json"
        self.lock_path = self.runtime / "locks" / "notifications.lock"
        self.slack_thread_store = (
            slack_thread_store
            if slack_thread_store is not None
            else SlackThreadStore(runtime)
        )

    def notify(self, event: NotificationEvent) -> NotificationResult:
        fingerprint = event.event_fingerprint()
        dedupe_key = event.dedupe_key()
        with FileLock(self.lock_path):
            try:
                state = self._read_state()
            except Exception as exc:
                return NotificationResult(
                    status="state_error",
                    channel="local_audit",
                    event_fingerprint=fingerprint,
                    duplicate=False,
                    attempted_channels=(),
                    configuration_issues=self.config.configuration_issues,
                    state_path=self.state_path,
                    state_persisted=False,
                    error_sha256=self._error_digest("state_read_failed", exc),
                )

            previous = state["last_events"].get(dedupe_key)
            if previous is not None and previous["fingerprint"] == fingerprint:
                return NotificationResult(
                    status="suppressed",
                    channel=None,
                    event_fingerprint=fingerprint,
                    duplicate=True,
                    attempted_channels=(),
                    configuration_issues=self.config.configuration_issues,
                    state_path=self.state_path,
                    state_persisted=True,
                )

            delivery = self._deliver(event)
            if delivery.status == "delivery_failed":
                return NotificationResult(
                    status=delivery.status,
                    channel=delivery.channel,
                    event_fingerprint=fingerprint,
                    duplicate=False,
                    attempted_channels=delivery.attempted_channels,
                    configuration_issues=self.config.configuration_issues,
                    state_path=self.state_path,
                    state_persisted=False,
                    error_sha256=delivery.error_sha256,
                )
            state["last_events"][dedupe_key] = {
                "fingerprint": fingerprint,
                "recorded_at": utc_now(),
            }
            try:
                self.atomic_writer(self.state_path, state)
            except Exception as exc:
                state_error = self._error_digest("state_write_failed", exc)
                combined_error = self._combine_error_digests(
                    tuple(
                        value
                        for value in (delivery.error_sha256, state_error)
                        if value is not None
                    )
                )
                return NotificationResult(
                    status="state_error",
                    channel=delivery.channel,
                    event_fingerprint=fingerprint,
                    duplicate=False,
                    attempted_channels=delivery.attempted_channels,
                    configuration_issues=self.config.configuration_issues,
                    state_path=self.state_path,
                    state_persisted=False,
                    error_sha256=combined_error,
                )
            return NotificationResult(
                status=delivery.status,
                channel=delivery.channel,
                event_fingerprint=fingerprint,
                duplicate=False,
                attempted_channels=delivery.attempted_channels,
                configuration_issues=self.config.configuration_issues,
                state_path=self.state_path,
                state_persisted=True,
                error_sha256=delivery.error_sha256,
            )

    def _deliver(self, event: NotificationEvent) -> _DeliveryResult:
        attempts = []
        failures = []

        if self.config.slack_configured:
            attempts.append("slack")
            try:
                self._send_slack(event)
            except Exception as exc:
                failures.append(self._error_digest("slack_failed", exc))
            else:
                return _DeliveryResult("sent", "slack", tuple(attempts), None)

        if self.config.smtp_configured:
            attempts.append("smtp")
            try:
                self._send_smtp(event)
            except Exception as exc:
                failures.append(self._error_digest("smtp_failed", exc))
            else:
                status = "sent_fallback" if failures else "sent"
                return _DeliveryResult(
                    status,
                    "smtp",
                    tuple(attempts),
                    self._combine_error_digests(tuple(failures)),
                )

        if self.config.windows_message_configured:
            attempts.append("windows_msg")
            try:
                self._send_windows_message(event)
            except Exception as exc:
                failures.append(self._error_digest("windows_msg_failed", exc))
            else:
                status = "sent_fallback" if failures else "sent"
                return _DeliveryResult(
                    status,
                    "windows_msg",
                    tuple(attempts),
                    self._combine_error_digests(tuple(failures)),
                )

        if not attempts:
            return _DeliveryResult("audit_only", "local_audit", (), None)
        return _DeliveryResult(
            "delivery_failed",
            "local_audit",
            tuple(attempts),
            self._combine_error_digests(tuple(failures)),
        )

    def _send_slack(self, event: NotificationEvent) -> None:
        text = f"{event.subject.strip()}\n{event.message}"
        if self.config.slack_relay_configured:
            assert self.config.slack_bot_token is not None
            assert self.config.slack_channel_id is not None
            relay_text = text
            if event.relay_target is not None:
                relay_text += (
                    "\n\n_Reply in this Slack thread to send text to this exact "
                    "existing Codex thread._"
                )
            try:
                result = self.slack_api_post(
                    self.config.slack_bot_token,
                    "chat.postMessage",
                    {
                        "channel": self.config.slack_channel_id,
                        "text": relay_text,
                        "unfurl_links": False,
                        "unfurl_media": False,
                    },
                    self.config.timeout_seconds,
                )
            except Exception:
                if self.config.slack_webhook_url is None:
                    raise
                self._send_slack_webhook(
                    text + "\n\n_Slack quick-reply mapping is unavailable for this "
                    "fallback message._"
                )
                return
            channel_id = result.get("channel")
            thread_ts = result.get("ts")
            if (
                channel_id != self.config.slack_channel_id
                or not valid_slack_channel_id(channel_id)
                or not valid_slack_timestamp(thread_ts)
            ):
                raise RuntimeError(
                    "Slack chat.postMessage response identity is invalid"
                )
            if event.relay_target is not None:
                self.slack_thread_store.record_thread(
                    channel_id,
                    thread_ts,
                    event.relay_target,
                    event.event_fingerprint(),
                )
            return
        assert self.config.slack_webhook_url is not None
        self._send_slack_webhook(text)

    def _send_slack_webhook(self, text: str) -> None:
        assert self.config.slack_webhook_url is not None
        payload = json.dumps(
            {"text": text}, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        status = self.http_post(
            self.config.slack_webhook_url, payload, self.config.timeout_seconds
        )
        if not 200 <= status < 300:
            raise RuntimeError("Slack webhook returned a non-success status")

    def _send_smtp(self, event: NotificationEvent) -> None:
        assert self.config.smtp_host is not None
        assert self.config.smtp_sender is not None
        message = EmailMessage()
        message["Subject"] = self._email_subject(event.subject)
        message["From"] = self.config.smtp_sender
        message["To"] = ", ".join(self.config.smtp_recipients)
        message.set_content(event.message)

        client = self.smtp_factory(
            self.config.smtp_host,
            self.config.smtp_port,
            self.config.timeout_seconds,
            self.config.smtp_security == "ssl",
        )
        try:
            if self.config.smtp_security == "starttls":
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if self.config.smtp_auth == "outlook_oauth2":
                assert self.config.smtp_username is not None
                assert self.config.outlook_client_id is not None
                access_token = self.outlook_access_token(
                    self.config.outlook_client_id,
                    self.config.smtp_username,
                    self.config.timeout_seconds,
                )
                if not isinstance(access_token, str) or not access_token:
                    raise RuntimeError("Outlook OAuth access token is unavailable")
                auth_string = (
                    f"user={self.config.smtp_username}\x01"
                    f"auth=Bearer {access_token}\x01\x01"
                )
                client.auth("XOAUTH2", lambda _challenge=None: auth_string)
            elif self.config.smtp_username is not None:
                assert self.config.smtp_password is not None
                client.login(self.config.smtp_username, self.config.smtp_password)
            refused = client.send_message(message)
            if refused:
                raise RuntimeError("SMTP refused one or more recipients")
        finally:
            try:
                client.quit()
            except Exception:
                pass

    def _send_windows_message(self, event: NotificationEvent) -> None:
        assert self.config.windows_message_target is not None
        argv = (
            r"C:\Windows\System32\msg.exe",
            self.config.windows_message_target,
            "/TIME:60",
            f"{event.subject.strip()}\n{event.message}",
        )
        return_code = self.message_runner(argv, self.config.timeout_seconds)
        if return_code != 0:
            raise RuntimeError("Windows message command returned a non-success status")

    def _read_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema_version": NOTIFICATION_STATE_SCHEMA_VERSION,
                "last_events": {},
            }
        with self.state_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if (
            not isinstance(value, dict)
            or frozenset(value) != frozenset({"schema_version", "last_events"})
            or value["schema_version"] != NOTIFICATION_STATE_SCHEMA_VERSION
            or not isinstance(value["last_events"], dict)
        ):
            raise ValueError("notification state is malformed")
        for key, entry in value["last_events"].items():
            if (
                not isinstance(key, str)
                or len(key) != 64
                or not isinstance(entry, dict)
                or frozenset(entry) != frozenset({"fingerprint", "recorded_at"})
                or not isinstance(entry["fingerprint"], str)
                or len(entry["fingerprint"]) != 64
                or not isinstance(entry["recorded_at"], str)
                or not entry["recorded_at"]
            ):
                raise ValueError("notification state is malformed")
        return value

    @staticmethod
    def _email_subject(value: str) -> str:
        return " ".join(value.replace("\r", " ").replace("\n", " ").split())

    @staticmethod
    def _error_digest(code: str, error: Exception) -> str:
        error_type = f"{type(error).__module__}.{type(error).__qualname__}"
        try:
            detail = str(error)
        except Exception:
            detail = ""
        return sha256_text(f"{code}\0{error_type}\0{detail}")

    @staticmethod
    def _combine_error_digests(values: Tuple[str, ...]) -> Optional[str]:
        if not values:
            return None
        return sha256_text(json.dumps(values, separators=(",", ":")))
