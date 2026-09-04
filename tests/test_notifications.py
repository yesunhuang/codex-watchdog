from __future__ import annotations

from email.message import EmailMessage
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from codex_watchdog.notifications import (
    EnvironmentNotifier,
    NotificationConfig,
    NotificationEvent,
    notification_workspace_label,
)
from codex_watchdog.slack_mapping import SlackRelayTarget, SlackThreadStore


WEBHOOK = "https://hooks.slack.invalid/services/secret/path"
PASSWORD = "smtp-secret-password"
ACCESS_TOKEN = "outlook-access-token-secret"
OUTLOOK_CLIENT_ID = "11111111-2222-4333-8444-555555555555"
SLACK_BOT_TOKEN = "xoxb-test-secret"
SLACK_APP_TOKEN = "xapp-test-secret"
SLACK_CHANNEL = "C12345678"
SLACK_USER = "U12345678"


def test_notification_workspace_label_prefers_repo_name_and_supports_locality(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "ProjectAlpha"

    assert notification_workspace_label("vscode-internal", repo) == "ProjectAlpha"
    assert (
        notification_workspace_label("vscode-internal", repo, locality="hpc-login")
        == "ProjectAlpha @ hpc-login"
    )


def test_notification_workspace_label_uses_internal_id_only_as_fallback() -> None:
    assert notification_workspace_label("vscode-internal", None) == "vscode-internal"
    assert notification_workspace_label("vscode-internal", Path()) == "vscode-internal"


def event(transition: str = "git-state-1", event_type: str = "git_attention"):
    return NotificationEvent(
        workspace_id="local-watchdog",
        event_type=event_type,
        transition_fingerprint=transition,
        subject="[Codex Watchdog] local-watchdog needs attention",
        message="Untracked files were left untouched.",
    )


class FakeSmtp:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Any]] = []
        self.message: Optional[EmailMessage] = None
        self.refused: Dict[str, Any] = {}

    def ehlo(self) -> None:
        self.calls.append(("ehlo", None))

    def starttls(self, *, context: Any) -> None:
        self.calls.append(("starttls", context))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", (username, password)))

    def auth(self, mechanism: str, authobject) -> None:
        self.calls.append(("auth", (mechanism, authobject(None))))

    def send_message(self, message: EmailMessage) -> Dict[str, Any]:
        self.calls.append(("send_message", None))
        self.message = message
        return self.refused

    def quit(self) -> None:
        self.calls.append(("quit", None))


def test_slack_delivery_is_primary_and_identical_event_is_persistently_suppressed(
    tmp_path: Path,
) -> None:
    calls = []

    def post(url: str, payload: bytes, timeout: float) -> int:
        calls.append((url, json.loads(payload), timeout))
        return 200

    config = NotificationConfig(slack_webhook_url=WEBHOOK, timeout_seconds=3)
    notifier = EnvironmentNotifier(tmp_path, config, http_post=post)

    first = notifier.notify(event())
    second = EnvironmentNotifier(tmp_path, config, http_post=post).notify(event())

    assert first.status == "sent"
    assert first.channel == "slack"
    assert first.attempted_channels == ("slack",)
    assert second.status == "suppressed"
    assert second.duplicate is True
    assert len(calls) == 1
    assert calls[0][0] == WEBHOOK
    assert calls[0][1] == {
        "text": "[Codex Watchdog] local-watchdog needs attention\n"
        "Untracked files were left untouched."
    }

    durable = notifier.state_path.read_text(encoding="utf-8")
    assert WEBHOOK not in durable
    assert "Untracked files" not in durable
    assert "local-watchdog" not in durable


def test_slack_bot_delivery_persists_exact_reply_thread_without_message_text(
    tmp_path: Path,
) -> None:
    calls = []

    def api_post(token: str, method: str, payload: Dict[str, Any], timeout: float):
        calls.append((token, method, payload, timeout))
        return {"ok": True, "channel": SLACK_CHANNEL, "ts": "1760000000.000100"}

    target = SlackRelayTarget(
        workspace_id="local-watchdog",
        thread_id="11111111-2222-4333-8444-555555555555",
        execution_locality="process_local",
    )
    relay_event = NotificationEvent(
        workspace_id="local-watchdog",
        event_type="codex_parked",
        transition_fingerprint="relay-transition",
        subject="[Codex Watchdog] stopped",
        message="Sensitive output for the operator.",
        relay_target=target,
    )
    config = NotificationConfig(
        slack_bot_token=SLACK_BOT_TOKEN,
        slack_app_token=SLACK_APP_TOKEN,
        slack_channel_id=SLACK_CHANNEL,
        slack_allowed_user_ids=(SLACK_USER,),
        timeout_seconds=4,
    )
    store = SlackThreadStore(tmp_path)
    result = EnvironmentNotifier(
        tmp_path, config, slack_api_post=api_post, slack_thread_store=store,
    ).notify(relay_event)

    assert result.status == "sent"
    assert result.channel == "slack"
    assert calls[0][0:2] == (SLACK_BOT_TOKEN, "chat.postMessage")
    assert calls[0][2]["channel"] == SLACK_CHANNEL
    assert "Reply in this Slack thread" in calls[0][2]["text"]
    mapping = store.lookup_thread(SLACK_CHANNEL, "1760000000.000100")
    assert mapping is not None
    assert mapping.target == target
    assert store.has_notification_mapping(relay_event.event_fingerprint()) is True
    durable = store.path.read_text(encoding="utf-8")
    for secret in (
        SLACK_BOT_TOKEN,
        SLACK_APP_TOKEN,
        "Sensitive output for the operator.",
    ):
        assert secret not in durable


def test_partial_slack_relay_configuration_fails_closed() -> None:
    config = NotificationConfig.from_environment(
        {
            "CODEX_WATCHDOG_SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
            "CODEX_WATCHDOG_SLACK_CHANNEL_ID": SLACK_CHANNEL,
        }
    )

    assert config.slack_configured is False
    assert config.slack_relay_configured is False
    assert config.configuration_issues == ("slack_relay_configuration_incomplete",)
    rendered = repr(config)
    assert SLACK_BOT_TOKEN not in rendered

    direct_message = NotificationConfig(
        slack_bot_token=SLACK_BOT_TOKEN,
        slack_app_token=SLACK_APP_TOKEN,
        slack_channel_id="D12345678",
        slack_allowed_user_ids=(SLACK_USER,),
    )
    assert direct_message.slack_relay_configured is False


def test_distinct_transition_bypasses_dedupe_immediately(tmp_path: Path) -> None:
    calls = []

    def post(_url: str, _payload: bytes, _timeout: float) -> int:
        calls.append("sent")
        return 204

    notifier = EnvironmentNotifier(
        tmp_path, NotificationConfig(slack_webhook_url=WEBHOOK), http_post=post
    )

    first = notifier.notify(event("state-a"))
    second = notifier.notify(event("state-b"))

    assert first.event_fingerprint != second.event_fingerprint
    assert second.status == "sent"
    assert calls == ["sent", "sent"]


def test_dedupe_is_independent_per_workspace_event_stream(tmp_path: Path) -> None:
    calls = []

    def post(_url: str, _payload: bytes, _timeout: float) -> int:
        calls.append("sent")
        return 200

    notifier = EnvironmentNotifier(
        tmp_path, NotificationConfig(slack_webhook_url=WEBHOOK), http_post=post
    )

    assert notifier.notify(event(event_type="git_attention")).status == "sent"
    assert notifier.notify(event(event_type="codex_parked")).status == "sent"
    assert notifier.notify(event(event_type="git_attention")).status == "suppressed"
    assert calls == ["sent", "sent"]


def test_failed_slack_falls_back_to_authenticated_starttls_smtp(
    tmp_path: Path,
) -> None:
    smtp = FakeSmtp()
    factory_calls = []

    def post(_url: str, _payload: bytes, _timeout: float) -> int:
        raise RuntimeError(f"failed webhook {WEBHOOK}")

    def factory(host: str, port: int, timeout: float, use_ssl: bool) -> FakeSmtp:
        factory_calls.append((host, port, timeout, use_ssl))
        return smtp

    config = NotificationConfig(
        slack_webhook_url=WEBHOOK,
        smtp_host="smtp.invalid",
        smtp_username="watchdog-user",
        smtp_password=PASSWORD,
        smtp_sender="watchdog@example.invalid",
        smtp_recipients=("owner@example.invalid",),
        smtp_security="starttls",
        timeout_seconds=4,
    )
    result = EnvironmentNotifier(
        tmp_path, config, http_post=post, smtp_factory=factory
    ).notify(event())

    assert result.status == "sent_fallback"
    assert result.channel == "smtp"
    assert result.attempted_channels == ("slack", "smtp")
    assert result.error_sha256 is not None
    assert factory_calls == [("smtp.invalid", 587, 4, False)]
    assert [call[0] for call in smtp.calls] == [
        "ehlo",
        "starttls",
        "ehlo",
        "login",
        "send_message",
        "quit",
    ]
    assert smtp.calls[3][1] == ("watchdog-user", PASSWORD)
    assert smtp.message is not None
    assert smtp.message["Subject"] == event().subject
    assert smtp.message.get_content().strip() == event().message
    persisted = (tmp_path / "notifications" / "last-events.json").read_text(
        encoding="utf-8"
    )
    result_output = json.dumps(result.to_dict())
    for secret in (WEBHOOK, PASSWORD, "owner@example.invalid"):
        assert secret not in persisted
        assert secret not in result_output


def test_smtp_only_supports_ssl_without_starttls(tmp_path: Path) -> None:
    smtp = FakeSmtp()
    factory_calls = []

    def factory(host: str, port: int, timeout: float, use_ssl: bool) -> FakeSmtp:
        factory_calls.append((host, port, timeout, use_ssl))
        return smtp

    config = NotificationConfig(
        smtp_host="smtp.invalid",
        smtp_port=465,
        smtp_sender="watchdog@example.invalid",
        smtp_recipients=("owner@example.invalid",),
        smtp_security="ssl",
    )
    result = EnvironmentNotifier(tmp_path, config, smtp_factory=factory).notify(event())

    assert result.status == "sent"
    assert result.channel == "smtp"
    assert factory_calls == [("smtp.invalid", 465, 10.0, True)]
    assert [call[0] for call in smtp.calls] == ["send_message", "quit"]


def test_personal_outlook_uses_silent_oauth_token_and_exact_xoauth2_shape(
    tmp_path: Path,
) -> None:
    smtp = FakeSmtp()
    token_calls = []

    def factory(_host: str, _port: int, _timeout: float, _ssl: bool) -> FakeSmtp:
        return smtp

    def access_token(client_id: str, username: str, timeout: float) -> str:
        token_calls.append((client_id, username, timeout))
        return ACCESS_TOKEN

    config = NotificationConfig(
        smtp_host="smtp-mail.outlook.com",
        smtp_username="watchdog@outlook.com",
        smtp_sender="watchdog@outlook.com",
        smtp_recipients=("owner@example.invalid",),
        smtp_security="starttls",
        smtp_auth="outlook_oauth2",
        outlook_client_id=OUTLOOK_CLIENT_ID,
        timeout_seconds=7,
    )
    result = EnvironmentNotifier(
        tmp_path, config, smtp_factory=factory, outlook_access_token=access_token,
    ).notify(event())

    assert result.status == "sent"
    assert result.channel == "smtp"
    assert token_calls == [(OUTLOOK_CLIENT_ID, "watchdog@outlook.com", 7)]
    assert [call[0] for call in smtp.calls] == [
        "ehlo",
        "starttls",
        "ehlo",
        "auth",
        "send_message",
        "quit",
    ]
    assert smtp.calls[3][1] == (
        "XOAUTH2",
        "user=watchdog@outlook.com\x01" f"auth=Bearer {ACCESS_TOKEN}\x01\x01",
    )
    rendered = json.dumps(result.to_dict())
    assert ACCESS_TOKEN not in rendered
    assert "watchdog@outlook.com" not in rendered


def test_outlook_oauth_configuration_fails_closed_before_token_access(
    tmp_path: Path,
) -> None:
    token_calls = []

    def unexpected_token(*args) -> str:
        token_calls.append(args)
        return ACCESS_TOKEN

    notifier = EnvironmentNotifier(
        tmp_path,
        environment={
            "CODEX_WATCHDOG_SMTP_HOST": "smtp-mail.outlook.com",
            "CODEX_WATCHDOG_SMTP_USERNAME": "watchdog@outlook.com",
            "CODEX_WATCHDOG_SMTP_PASSWORD": PASSWORD,
            "CODEX_WATCHDOG_SMTP_FROM": "watchdog@outlook.com",
            "CODEX_WATCHDOG_SMTP_TO": "owner@example.invalid",
            "CODEX_WATCHDOG_SMTP_SECURITY": "starttls",
            "CODEX_WATCHDOG_SMTP_AUTH": "outlook_oauth2",
            "CODEX_WATCHDOG_OUTLOOK_CLIENT_ID": OUTLOOK_CLIENT_ID,
        },
        outlook_access_token=unexpected_token,
    )

    result = notifier.notify(event())

    assert result.status == "audit_only"
    assert result.configuration_issues == ("smtp_configuration_incomplete",)
    assert token_calls == []
    assert PASSWORD not in json.dumps(result.to_dict())


def test_slack_success_does_not_load_outlook_token(tmp_path: Path) -> None:
    def post(_url: str, _payload: bytes, _timeout: float) -> int:
        return 200

    def unexpected_token(*_args) -> str:
        raise AssertionError("Slack success must not load an Outlook token")

    config = NotificationConfig(
        slack_webhook_url=WEBHOOK,
        smtp_host="smtp-mail.outlook.com",
        smtp_username="watchdog@outlook.com",
        smtp_sender="watchdog@outlook.com",
        smtp_recipients=("owner@example.invalid",),
        smtp_auth="outlook_oauth2",
        outlook_client_id=OUTLOOK_CLIENT_ID,
    )
    result = EnvironmentNotifier(
        tmp_path, config, http_post=post, outlook_access_token=unexpected_token,
    ).notify(event())

    assert result.status == "sent"
    assert result.channel == "slack"
    assert result.attempted_channels == ("slack",)


def test_unconfigured_notifier_returns_local_audit_result_and_dedupes(
    tmp_path: Path,
) -> None:
    notifier = EnvironmentNotifier(tmp_path, environment={})

    first = notifier.notify(event())
    second = notifier.notify(event())

    assert first.status == "audit_only"
    assert first.channel == "local_audit"
    assert first.attempted_channels == ()
    assert first.state_persisted is True
    assert second.status == "suppressed"


def test_windows_message_is_opt_in_and_uses_injected_argv_runner(
    tmp_path: Path,
) -> None:
    calls: List[Tuple[Sequence[str], float]] = []

    def runner(argv: Sequence[str], timeout: float) -> int:
        calls.append((argv, timeout))
        return 0

    notifier = EnvironmentNotifier(
        tmp_path,
        environment={
            "CODEX_WATCHDOG_WINDOWS_MSG": "1",
            "USERNAME": "interactive-user",
            "CODEX_WATCHDOG_NOTIFICATION_TIMEOUT_SECONDS": "2.5",
        },
        message_runner=runner,
    )
    result = notifier.notify(event())

    assert result.status == "sent"
    assert result.channel == "windows_msg"
    assert result.attempted_channels == ("windows_msg",)
    assert calls == [
        (
            (
                r"C:\Windows\System32\msg.exe",
                "interactive-user",
                "/TIME:60",
                f"{event().subject}\n{event().message}",
            ),
            2.5,
        )
    ]
    durable = notifier.state_path.read_text(encoding="utf-8")
    assert "interactive-user" not in durable
    assert event().message not in durable


def test_windows_message_follows_failed_slack_and_smtp(tmp_path: Path) -> None:
    smtp = FakeSmtp()
    smtp.refused = {"owner@example.invalid": (550, b"rejected")}
    message_calls = []

    def post(_url: str, _payload: bytes, _timeout: float) -> int:
        return 500

    def factory(_host: str, _port: int, _timeout: float, _ssl: bool) -> FakeSmtp:
        return smtp

    def runner(argv: Sequence[str], _timeout: float) -> int:
        message_calls.append(tuple(argv))
        return 0

    config = NotificationConfig(
        slack_webhook_url=WEBHOOK,
        smtp_host="smtp.invalid",
        smtp_sender="watchdog@example.invalid",
        smtp_recipients=("owner@example.invalid",),
        windows_message_enabled=True,
        windows_message_target="interactive-user",
    )
    result = EnvironmentNotifier(
        tmp_path, config, http_post=post, smtp_factory=factory, message_runner=runner,
    ).notify(event())

    assert result.status == "sent_fallback"
    assert result.channel == "windows_msg"
    assert result.attempted_channels == ("slack", "smtp", "windows_msg")
    assert result.error_sha256 is not None
    assert len(message_calls) == 1


def test_all_configured_delivery_failures_are_hash_only_and_retried(
    tmp_path: Path,
) -> None:
    calls = []

    def post(_url: str, _payload: bytes, _timeout: float) -> int:
        calls.append("slack")
        raise RuntimeError(f"raw secret: {WEBHOOK}")

    def runner(_argv: Sequence[str], _timeout: float) -> int:
        calls.append("windows")
        return 1

    config = NotificationConfig(
        slack_webhook_url=WEBHOOK,
        windows_message_enabled=True,
        windows_message_target="interactive-user",
    )
    notifier = EnvironmentNotifier(
        tmp_path, config, http_post=post, message_runner=runner
    )

    first = notifier.notify(event())
    second = notifier.notify(event())

    assert first.status == "delivery_failed"
    assert first.channel == "local_audit"
    assert first.error_sha256 is not None
    assert WEBHOOK not in json.dumps(first.to_dict())
    assert first.state_persisted is False
    assert second.status == "delivery_failed"
    assert second.duplicate is False
    assert second.state_persisted is False
    assert calls == ["slack", "windows", "slack", "windows"]


def test_environment_configuration_is_explicit_and_repr_redacts_values() -> None:
    config = NotificationConfig.from_environment(
        {
            "CODEX_WATCHDOG_SLACK_WEBHOOK_URL": WEBHOOK,
            "CODEX_WATCHDOG_SMTP_HOST": "smtp.invalid",
            "CODEX_WATCHDOG_SMTP_PORT": "2525",
            "CODEX_WATCHDOG_SMTP_USERNAME": "watchdog-user",
            "CODEX_WATCHDOG_SMTP_PASSWORD": PASSWORD,
            "CODEX_WATCHDOG_SMTP_FROM": "watchdog@example.invalid",
            "CODEX_WATCHDOG_SMTP_TO": "one@example.invalid; two@example.invalid",
            "CODEX_WATCHDOG_SMTP_SECURITY": "plain",
            "CODEX_WATCHDOG_WINDOWS_MSG": "off",
        }
    )

    assert config.slack_configured is True
    assert config.smtp_configured is True
    assert config.smtp_port == 2525
    assert config.smtp_recipients == ("one@example.invalid", "two@example.invalid",)
    rendered = repr(config)
    for secret in (WEBHOOK, PASSWORD, "smtp.invalid", "watchdog-user"):
        assert secret not in rendered


def test_incomplete_smtp_configuration_is_reported_without_attempt(
    tmp_path: Path,
) -> None:
    notifier = EnvironmentNotifier(
        tmp_path,
        environment={
            "CODEX_WATCHDOG_SMTP_HOST": "smtp.invalid",
            "CODEX_WATCHDOG_SMTP_PASSWORD": PASSWORD,
        },
    )

    result = notifier.notify(event())

    assert result.status == "audit_only"
    assert result.configuration_issues == ("smtp_configuration_incomplete",)
    assert PASSWORD not in json.dumps(result.to_dict())


def test_malformed_state_fails_closed_without_delivery(tmp_path: Path) -> None:
    state = tmp_path / "notifications" / "last-events.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"schema_version": 99, "last_events": {}}', encoding="utf-8")
    calls = []

    def post(_url: str, _payload: bytes, _timeout: float) -> int:
        calls.append("unexpected")
        return 200

    result = EnvironmentNotifier(
        tmp_path, NotificationConfig(slack_webhook_url=WEBHOOK), http_post=post
    ).notify(event())

    assert result.status == "state_error"
    assert result.channel == "local_audit"
    assert result.state_persisted is False
    assert result.error_sha256 is not None
    assert calls == []


def test_state_write_failure_reports_hash_only_after_delivery(tmp_path: Path) -> None:
    def post(_url: str, _payload: bytes, _timeout: float) -> int:
        return 200

    def fail_write(_path: Path, _value: Dict[str, Any]) -> None:
        raise OSError(f"cannot persist {WEBHOOK}")

    result = EnvironmentNotifier(
        tmp_path,
        NotificationConfig(slack_webhook_url=WEBHOOK),
        http_post=post,
        atomic_writer=fail_write,
    ).notify(event())

    assert result.status == "state_error"
    assert result.channel == "slack"
    assert result.state_persisted is False
    assert result.error_sha256 is not None
    assert WEBHOOK not in json.dumps(result.to_dict())
