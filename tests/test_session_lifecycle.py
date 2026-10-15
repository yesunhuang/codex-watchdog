from types import SimpleNamespace

import pytest

from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
from codex_watchdog.session_activity import TTL_SECONDS
from codex_watchdog.session_lifecycle import SessionLifecycle
from codex_watchdog.workspace_registry import TrackedWorkspace

THREAD = "11111111-1111-4111-8111-111111111111"


def setup_case(tmp_path, monkeypatch, *, error=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = TrackedWorkspace.create("fixture", repo, THREAD)
    clock = [1800000000.0]
    config = SimpleNamespace(interactive_providers=("slack",), slack_relay_configured=True,
                             slack_channel_id="C12345678")
    calls = []
    def notify(event):
        calls.append(event)
        if error:
            raise TimeoutError("uncertain provider")
        return SimpleNamespace(to_dict=lambda: dict(status="sent"))
    notifier = SimpleNamespace(config=config, slack_thread_store=object(), notify=notify)
    monkeypatch.setattr("codex_watchdog.session_lifecycle.SessionRoutes.destination", lambda *args: "C87654321")
    lifecycle = SessionLifecycle(tmp_path, notifier, clock=lambda: clock[0])
    return workspace, clock, calls, notifier, lifecycle


def signals(stamp, *, turn=False, queue=False):
    return dict(exact=True, genuine_at=stamp, turn_active=turn, queue_active=queue)


def test_verified_new_session_claims_one_hello_without_waking_codex(tmp_path, monkeypatch):
    workspace, clock, calls, notifier, lifecycle = setup_case(tmp_path, monkeypatch)
    refused = lifecycle.observe(workspace, signals(clock[0]))
    assert refused["status"] == "UNKNOWN" and calls == []
    state = lifecycle.observe(workspace, signals(clock[0]), verified_owner=True)
    assert state["status"] == "ACTIVE" and len(calls) == 1
    event = calls[0]
    assert event.relay_target.thread_id == THREAD and event.provider_filter == ("slack",)
    assert event.event_type == "session_hello"
    # Reconstruct after restart/handoff and observe the identical evidence.
    other_node = SessionLifecycle(tmp_path, notifier, clock=lambda: clock[0])
    other_node.observe(workspace, signals(clock[0]), verified_owner=True)
    assert len(calls) == 1


def test_failed_hello_is_terminal_but_reactivation_makes_new_generation(tmp_path, monkeypatch):
    workspace, clock, calls, notifier, lifecycle = setup_case(tmp_path, monkeypatch, error=True)
    state = lifecycle.observe(workspace, signals(clock[0]), verified_owner=True)
    assert state["status"] == "ACTIVE" and state["hello_error"]
    lifecycle.observe(workspace, signals(clock[0]), verified_owner=True)
    assert len(calls) == 1
    clock[0] += TTL_SECONDS
    state = lifecycle.observe(workspace, signals(None), verified_owner=True)
    assert state["status"] == "DORMANT"
    assert not lifecycle.is_active(THREAD)
    assert len(calls) == 1
    # A queue flag/notification/old event cannot reactivate; a fresh user can.
    lifecycle.observe(workspace, signals(None, queue=True), verified_owner=True)
    assert not lifecycle.is_active(THREAD)
    state = lifecycle.observe(workspace, signals(clock[0]), verified_owner=True)
    assert state["status"] == "ACTIVE" and state["generation"] == 2
    assert len(calls) == 2


def test_migration_and_busy_flags_do_not_spam_hello(tmp_path, monkeypatch):
    workspace, clock, calls, notifier, lifecycle = setup_case(tmp_path, monkeypatch)
    lifecycle.observe(workspace, signals(clock[0]), verified_owner=True, legacy=True)
    assert calls == []
    clock[0] += TTL_SECONDS + 1
    state = lifecycle.observe(workspace, signals(None, turn=True), verified_owner=True)
    assert state["status"] == "ACTIVE" and calls == []
    assert state["last_activity_at"] == clock[0] - TTL_SECONDS - 1


def test_activation_provider_filter_never_uses_fallback_or_retries(tmp_path, monkeypatch):
    config = NotificationConfig(slack_bot_token="xoxb-fixture", slack_channel_id="C12345678",
                                slack_allowed_user_ids=("U12345678",), slack_reply_mode="poll")
    notifier = EnvironmentNotifier(tmp_path, config)
    calls = []
    def failed(event):
        calls.append("slack")
        raise TimeoutError("provider uncertain")
    monkeypatch.setattr(notifier, "_send_slack", failed)
    monkeypatch.setattr(notifier, "_send_smtp", lambda *args: pytest.fail("invented fallback"))
    from codex_watchdog.notifications import NotificationEvent
    event = NotificationEvent("fixture", "session_hello", "generation1", "Hello", "Monitoring active",
                              provider_filter=("slack",))
    first = notifier.notify(event)
    assert first.status == "delivery_failed" and calls == ["slack"]
    assert notifier.notify(event).status == "suppressed" and calls == ["slack"]
