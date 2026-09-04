from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_watchdog import cli
from codex_watchdog import notifications
from codex_watchdog import outlook_oauth
from codex_watchdog.models import sha256_text
from codex_watchdog.service import RunOnceService
from codex_watchdog.workspace_registry import WorkspaceRegistry


SESSION = "11111111-2222-4333-8444-555555555555"


def clear_notification_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        notifications.SLACK_WEBHOOK_ENV,
        notifications.SLACK_BOT_TOKEN_ENV,
        notifications.SLACK_APP_TOKEN_ENV,
        notifications.SLACK_CHANNEL_ID_ENV,
        notifications.SLACK_ALLOWED_USER_IDS_ENV,
        notifications.SMTP_HOST_ENV,
        notifications.SMTP_PORT_ENV,
        notifications.SMTP_USERNAME_ENV,
        notifications.SMTP_PASSWORD_ENV,
        notifications.SMTP_FROM_ENV,
        notifications.SMTP_TO_ENV,
        notifications.SMTP_SECURITY_ENV,
        notifications.SMTP_AUTH_ENV,
        notifications.OUTLOOK_CLIENT_ID_ENV,
        notifications.WINDOWS_MSG_ENV,
        notifications.WINDOWS_MSG_TARGET_ENV,
        notifications.NOTIFICATION_TIMEOUT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def test_service_once_persists_git_blocker_without_initializing_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "not-a-git-repository"
    repo.mkdir()
    WorkspaceRegistry(runtime).add("workspace-1", repo, SESSION)

    def unexpected_dispatcher(*_args, **_kwargs):
        raise AssertionError("service-once must not initialize queue state")

    monkeypatch.setattr(cli, "QueueWakeDispatcher", unexpected_dispatcher)

    code = cli.main(["--runtime", str(runtime), "service-once", "--manual-only"])

    output = capsys.readouterr()
    value = json.loads(output.out)
    assert code == 0
    assert output.err == ""
    assert value["status"] == "ok"
    assert value["workspace_count"] == 1
    assert value["workspaces"][0]["status"] == "persisted"
    assert value["workspaces"][0]["git_status"] == "blocked"
    # pytest's configured tmp_path is nested beneath this checkout, so Git can
    # discover the parent repository; the exact-root guard must reject it.
    assert value["workspaces"][0]["blockers"] == ["repo_root_mismatch"]
    assert (
        RunOnceService(runtime, registry=WorkspaceRegistry(runtime))
        .observation_path("workspace-1")
        .is_file()
    )


def test_run_once_wires_runtime_codex_home_and_stop_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = []

    class FakeCycle:
        ok = True

        @staticmethod
        def to_dict():
            return {"status": "completed", "workspace_count": 0}

    class FakeMvpService:
        def __init__(self, runtime, **kwargs):
            created.append((runtime, kwargs))

        def run_once(self):
            return FakeCycle()

    monkeypatch.setattr(cli, "MvpWatchdogService", FakeMvpService)
    runtime = tmp_path / "runtime"
    codex_home = tmp_path / "codex-home"

    code = cli.main(
        [
            "--runtime",
            str(runtime),
            "--codex-home",
            str(codex_home),
            "run",
            "--once",
            "--replay-latest-stop",
        ]
    )

    assert code == 0
    assert created == [
        (
            runtime.resolve(),
            {"codex_home": codex_home.resolve(), "replay_latest_stop": True,},
        )
    ]
    assert json.loads(capsys.readouterr().out) == {
        "status": "completed",
        "workspace_count": 0,
    }


def test_run_foreground_passes_interval_and_jsonl_emitter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class FakeMvpService:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *, interval_seconds, emit):
            calls.append(interval_seconds)
            emit({"cycle_id": "cycle-1", "status": "completed"})
            return 0

    monkeypatch.setattr(cli, "MvpWatchdogService", FakeMvpService)

    code = cli.main(
        ["--runtime", str(tmp_path / "runtime"), "run", "--interval", "12.5"]
    )

    assert code == 0
    assert calls == [12.5]
    assert json.loads(capsys.readouterr().out) == {
        "cycle_id": "cycle-1",
        "status": "completed",
    }


def test_run_manual_only_disables_automatic_discovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = []

    class FakeCycle:
        ok = True

        @staticmethod
        def to_dict():
            return {"status": "completed", "workspace_count": 0}

    class FakeMvpService:
        def __init__(self, runtime, **kwargs):
            created.append((runtime, kwargs))

        @staticmethod
        def run_once():
            return FakeCycle()

    monkeypatch.setattr(cli, "MvpWatchdogService", FakeMvpService)
    runtime = tmp_path / "runtime"

    code = cli.main(["--runtime", str(runtime), "run", "--once", "--manual-only"])

    assert code == 0
    assert created == [
        (
            runtime.resolve(),
            {"codex_home": None, "replay_latest_stop": False, "auto_discovery": False,},
        )
    ]
    assert json.loads(capsys.readouterr().out) == {
        "status": "completed",
        "workspace_count": 0,
    }


def test_workspace_discover_is_read_only_and_does_not_initialize_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class FakeSnapshot:
        status = "partial"

        @staticmethod
        def to_dict():
            return {
                "schema_version": 1,
                "status": "partial",
                "window_count": 2,
                "tracked_workspace_count": 1,
                "issues": ["remote_agent_required"],
            }

    class FakeCatalog:
        def __init__(self, runtime, **kwargs):
            calls.append((runtime, kwargs))

        @staticmethod
        def snapshot(*, persist):
            assert persist is False
            return FakeSnapshot()

    def unexpected_dispatcher(*_args, **_kwargs):
        raise AssertionError("workspace discovery must not initialize queue state")

    monkeypatch.setattr(cli, "EffectiveWorkspaceCatalog", FakeCatalog)
    monkeypatch.setattr(cli, "QueueWakeDispatcher", unexpected_dispatcher)
    runtime = tmp_path / "runtime"
    codex_home = tmp_path / "codex-home"

    code = cli.main(
        [
            "--runtime",
            str(runtime),
            "--codex-home",
            str(codex_home),
            "workspace-discover",
            "--exclude",
            "scratch",
            "--exclude",
            str(tmp_path / "ignored-repo"),
        ]
    )

    assert code == 0
    assert calls == [
        (
            runtime.resolve(),
            {
                "codex_home": codex_home.resolve(),
                "exclude": ["scratch", str(tmp_path / "ignored-repo")],
            },
        )
    ]
    assert json.loads(capsys.readouterr().out) == FakeSnapshot.to_dict()


def test_service_once_exits_nonzero_when_automatic_discovery_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class ErrorCatalog:
        def __init__(self, runtime, **_kwargs):
            self.path = runtime / "service" / "workspace-discovery.json"
            self.last_snapshot = None

        def list_workspaces(self):
            self.last_snapshot = SimpleNamespace(
                status="error",
                windows=(),
                effective_workspaces=(),
                issues=("vscode_live_status_unavailable",),
            )
            return []

    monkeypatch.setattr(cli, "EffectiveWorkspaceCatalog", ErrorCatalog)

    code = cli.main(["--runtime", str(tmp_path / "runtime"), "service-once"])

    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert result["status"] == "failed"
    assert result["discovery"]["status"] == "error"


def test_workspace_remove_clears_manual_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    registered = WorkspaceRegistry(runtime).add("manual", repo, SESSION).workspace

    code = cli.main(
        ["--runtime", str(runtime), "workspace-remove", "--workspace", "manual"]
    )

    assert code == 0
    assert WorkspaceRegistry(runtime).list_workspaces() == []
    assert json.loads(capsys.readouterr().out) == {
        "status": "removed",
        "workspace": registered.to_dict(),
    }


def test_notify_test_wires_fixed_privacy_safe_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = []
    notified = []

    class FakeResult:
        status = "sent_fallback"

        @staticmethod
        def to_dict():
            return {
                "status": "sent_fallback",
                "channel": "smtp",
                "event_fingerprint": "f" * 64,
            }

    class FakeNotifier:
        def __init__(self, runtime):
            created.append(runtime)

        def notify(self, event):
            notified.append(event)
            return FakeResult()

    monkeypatch.setattr(cli, "EnvironmentNotifier", FakeNotifier)
    runtime = tmp_path / "runtime"
    test_id = "cpx06-notify-001"

    code = cli.main(
        [
            "--runtime",
            str(runtime),
            "notify-test",
            "--id",
            test_id,
            "--workspace",
            "watchdog-dev",
        ]
    )

    output = capsys.readouterr()
    value = json.loads(output.out)
    assert code == 0
    assert output.err == ""
    assert created == [runtime.resolve()]
    assert len(notified) == 1
    event = notified[0]
    assert event.workspace_id == "watchdog-dev"
    assert event.event_type == "notification_test"
    assert event.transition_fingerprint == sha256_text(f"notification_test\0{test_id}")
    assert event.subject == "[Codex Watchdog TEST] Notification delivery check"
    assert event.message == (
        "This is a direct Codex Watchdog test notification. No action is required."
    )
    assert test_id not in event.subject
    assert test_id not in event.message
    assert test_id not in output.out
    assert value == {
        "status": "sent_fallback",
        "channel": "smtp",
        "event_fingerprint": "f" * 64,
    }


def test_slack_relay_test_maps_one_auto_discovered_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = SimpleNamespace(
        workspace_id="vscode-exact", repo_root=repo.resolve(), session_id=SESSION,
    )
    notified = []

    class FakeNotifier:
        config = SimpleNamespace(slack_relay_configured=True)
        slack_thread_store = SimpleNamespace(
            has_notification_mapping=lambda _fingerprint: True
        )

        def __init__(self, runtime):
            assert runtime == (tmp_path / "runtime").resolve()

        def notify(self, event):
            notified.append(event)
            return SimpleNamespace(
                status="sent",
                channel="slack",
                to_dict=lambda: {"status": "sent", "channel": "slack"},
            )

    class FakeCatalog:
        def __init__(self, runtime, **kwargs):
            assert runtime == (tmp_path / "runtime").resolve()
            assert kwargs == {"codex_home": None}

        @staticmethod
        def list_workspaces():
            return [tracked]

    monkeypatch.setattr(cli, "EnvironmentNotifier", FakeNotifier)
    monkeypatch.setattr(cli, "EffectiveWorkspaceCatalog", FakeCatalog)

    code = cli.main(
        [
            "--runtime",
            str(tmp_path / "runtime"),
            "slack-relay-test",
            "--id",
            "relay-live-001",
            "--workspace",
            "repo",
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "sent",
        "channel": "slack",
        "relay_mapping": "created",
    }
    assert len(notified) == 1
    event = notified[0]
    assert event.relay_target.workspace_id == "vscode-exact"
    assert event.relay_target.thread_id == SESSION
    assert event.relay_target.execution_locality == "process_local"
    assert "relay-live-001" in event.subject


def test_slack_relay_test_fails_when_webhook_fallback_has_no_mapping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = SimpleNamespace(
        workspace_id="vscode-exact", repo_root=repo.resolve(), session_id=SESSION,
    )

    class FakeNotifier:
        config = SimpleNamespace(slack_relay_configured=True)
        slack_thread_store = SimpleNamespace(
            has_notification_mapping=lambda _fingerprint: False
        )

        def __init__(self, _runtime):
            pass

        @staticmethod
        def notify(_event):
            return SimpleNamespace(
                status="sent",
                channel="slack",
                to_dict=lambda: {"status": "sent", "channel": "slack"},
            )

    class FakeCatalog:
        def __init__(self, _runtime, **_kwargs):
            pass

        @staticmethod
        def list_workspaces():
            return [tracked]

    monkeypatch.setattr(cli, "EnvironmentNotifier", FakeNotifier)
    monkeypatch.setattr(cli, "EffectiveWorkspaceCatalog", FakeCatalog)

    code = cli.main(
        [
            "--runtime",
            str(tmp_path / "runtime"),
            "slack-relay-test",
            "--id",
            "relay-fallback-001",
            "--workspace",
            "repo",
        ]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out)["relay_mapping"] == "missing"


def test_notify_test_distinct_ids_bypass_debounce(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_notification_environment(monkeypatch)
    monkeypatch.setenv(notifications.WINDOWS_MSG_ENV, "1")
    monkeypatch.setenv("USERNAME", "test-user")
    calls = []

    def message_runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        return 0

    monkeypatch.setattr(notifications, "_default_message_runner", message_runner)
    runtime = tmp_path / "runtime"

    first_code = cli.main(
        ["--runtime", str(runtime), "notify-test", "--id", "delivery-001"]
    )
    first = json.loads(capsys.readouterr().out)
    duplicate_code = cli.main(
        ["--runtime", str(runtime), "notify-test", "--id", "delivery-001"]
    )
    duplicate = json.loads(capsys.readouterr().out)
    distinct_code = cli.main(
        ["--runtime", str(runtime), "notify-test", "--id", "delivery-002"]
    )
    distinct = json.loads(capsys.readouterr().out)

    assert (first_code, duplicate_code, distinct_code) == (0, 1, 0)
    assert first["status"] == "sent"
    assert duplicate["status"] == "suppressed"
    assert distinct["status"] == "sent"
    assert first["event_fingerprint"] != distinct["event_fingerprint"]
    assert len(calls) == 2
    durable = (runtime / "notifications" / "last-events.json").read_text(
        encoding="utf-8"
    )
    assert "delivery-001" not in durable
    assert "delivery-002" not in durable


def test_notify_test_audit_only_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_notification_environment(monkeypatch)

    code = cli.main(
        [
            "--runtime",
            str(tmp_path / "runtime"),
            "notify-test",
            "--id",
            "audit-only-001",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert result["status"] == "audit_only"
    assert result["channel"] == "local_audit"


def test_outlook_login_displays_only_device_prompt_and_safe_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_notification_environment(monkeypatch)
    environment = {
        notifications.SMTP_HOST_ENV: "smtp-mail.outlook.com",
        notifications.SMTP_PORT_ENV: "587",
        notifications.SMTP_USERNAME_ENV: "watchdog@outlook.com",
        notifications.SMTP_FROM_ENV: "watchdog@outlook.com",
        notifications.SMTP_TO_ENV: "owner@example.invalid",
        notifications.SMTP_SECURITY_ENV: "starttls",
        notifications.SMTP_AUTH_ENV: "outlook_oauth2",
        notifications.OUTLOOK_CLIENT_ID_ENV: SESSION,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    created = []

    class FakeProvider:
        def __init__(self, **kwargs):
            created.append(kwargs)

        @staticmethod
        def login_device_code(display):
            display(
                outlook_oauth.OutlookDeviceCodePrompt(
                    verification_uri="https://microsoft.com/devicelogin",
                    user_code="ABCD-EFGH",
                    expires_in_seconds=900,
                )
            )
            return outlook_oauth.OutlookOAuthLoginResult()

    monkeypatch.setattr(outlook_oauth, "OutlookOAuthTokenProvider", FakeProvider)

    code = cli.main(["--runtime", str(tmp_path / "runtime"), "outlook-login"])

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert lines == [
        {
            "status": "authorization_required",
            "verification_uri": "https://microsoft.com/devicelogin",
            "user_code": "ABCD-EFGH",
            "expires_in_seconds": 900,
        },
        {"status": "authenticated"},
    ]
    assert created == [
        {
            "client_id": SESSION,
            "username": "watchdog@outlook.com",
            "timeout_seconds": 10.0,
        }
    ]
    rendered = json.dumps(lines)
    assert "watchdog@outlook.com" not in rendered
    assert SESSION not in rendered
