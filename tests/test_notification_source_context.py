"""Notifier -> finite receipt -> fake transport; no native/provider side effects."""
from contextlib import nullcontext
from dataclasses import replace
import sqlite3

import pytest

from codex_watchdog import notification_receipt_retention as finite
from codex_watchdog.notification_context import source_time
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.relay import RelayTarget
from codex_watchdog.models import sha256_text
from codex_watchdog.session_routes import SessionRoutes

SID = "11111111-2222-4333-8444-555555555555"
WORKSPACE = "test-workspace"
START = 1792008000.0


def setup_notifier(tmp_path, *, both=False):
    current = [START + 5]
    calls = []
    config = NotificationConfig(slack_bot_token="xoxb-fixture", slack_app_token="xapp-fixture",
        slack_channel_id="C12345678",
        slack_allowed_user_ids=("U12345678",), interactive_transport="both" if both else "slack",
        lark=LarkConfig("cli_" + "a" * 20, "fixture-secret", "oc_fixture000000001",
                        ("ou_fixture0000001",)) if both else LarkConfig())

    def post(token, method, payload, timeout):
        calls.append(dict(payload))
        return dict(channel=payload["channel"], ts=str(int(current[0])) + ".000001")

    notifier = EnvironmentNotifier(tmp_path, config, slack_api_post=post, clock=lambda: current[0])
    notifier.slack_thread_store.clock = lambda: current[0]
    if notifier.lark_thread_store is not None:
        notifier.lark_thread_store.clock = lambda: current[0]
    scopes = [(SID, sha256_text(WORKSPACE), p) for p in ("slack", "lark", "smtp", "windows_msg")]
    finite.prepare_notification_receipts(tmp_path, scopes=scopes, contexts=(),
        sender_fence=nullcontext, clock=lambda: START)
    return notifier, calls, current, scopes


def event(fingerprint="original", born=START+1):
    return NotificationEvent(WORKSPACE, "codex_parked", fingerprint, "Subject", "Output",
        RelayTarget(WORKSPACE, SID, "process_local"), source_born=born)


def test_finite_event_birth_is_separate_from_new_ticket_creation(tmp_path):
    notifier, calls, current, scopes = setup_notifier(tmp_path)
    original = event()
    assert notifier.notify(original).status == "sent"
    with sqlite3.connect(tmp_path / "notifications/dual-deliveries.sqlite3") as db:
        assert db.execute("SELECT born,expires FROM retention").fetchall() == [(START+1,START+1+finite.TTL)]
    mapping = notifier.slack_thread_store.notification_mappings(original.event_fingerprint())[0]
    assert source_time(mapping["created_at"]) == START+5
    assert len(calls) == 1
    current[0] += 10
    assert notifier.notify(original).duplicate
    assert len(calls) == 1


def test_expired_original_callback_cannot_rebirth_after_gc_or_restart(tmp_path):
    notifier, calls, current, scopes = setup_notifier(tmp_path)
    original = event()
    assert notifier.notify(original).status == "sent"
    current[0] = START + finite.TTL + 10
    finite.maintain_notification_receipts(tmp_path, scopes=scopes, complete=True,
        sender_fence=nullcontext, clock=lambda: current[0])
    newer = event("newer", current[0])
    assert notifier.notify(newer).status == "sent"
    # Fresh notifier instance/callback and changed current fingerprint cannot
    # manufacture source birth from the new claim/ticket clock.
    restarted = EnvironmentNotifier(tmp_path, notifier.config, slack_api_post=notifier.slack_api_post,
                                   clock=lambda: current[0])
    restarted.slack_thread_store.clock = lambda: current[0]
    assert restarted.notify(original).attempted_channels == ()
    assert len(calls) == 2
    with sqlite3.connect(tmp_path / "notifications/dual-deliveries.sqlite3") as db:
        assert db.execute("SELECT fingerprint FROM retention WHERE fingerprint=?",
                          (original.event_fingerprint(),)).fetchone() is None


def test_delayed_output_creates_fresh_ticket_without_refreshing_event_age(tmp_path):
    notifier, calls, current, scopes = setup_notifier(tmp_path)
    current[0] = START + finite.TTL - 1
    original = event()
    assert notifier.notify(original).status == "sent"
    parent = notifier.slack_thread_store.notification_mappings(original.event_fingerprint())[0]
    assert source_time(parent["created_at"]) == current[0]
    current[0] += 3
    finite.maintain_notification_receipts(tmp_path, scopes=scopes, complete=True,
        sender_fence=nullcontext, clock=lambda: current[0])
    with sqlite3.connect(tmp_path / "notifications/dual-deliveries.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM retention").fetchone()[0] == 0
    # The new reply parent is only three seconds old, although the notification
    # event's source is now beyond its finite history horizon.
    assert notifier.slack_thread_store.lookup_thread(parent["channel_id"],parent["thread_ts"]) is not None
    assert notifier.slack_thread_store.notification_mappings(original.event_fingerprint())[0]["created_at"] == parent["created_at"]


def test_missing_original_time_refuses_finite_send_without_route_lookup(tmp_path, monkeypatch):
    notifier, calls, current, scopes = setup_notifier(tmp_path)
    monkeypatch.setattr(SessionRoutes, "destination", lambda *args: pytest.fail("missing source read routes"))
    result = notifier.notify(event(born=None))
    assert result.attempted_channels == ()
    assert not calls


def test_claimed_route_change_is_uncertain_without_external_attempt(tmp_path, monkeypatch):
    notifier, calls, current, scopes = setup_notifier(tmp_path)
    send = notifier._send_slack

    def changed(event, *, expected_identity):
        notifier.config = replace(notifier.config, slack_channel_id="C87654321")
        return send(event, expected_identity=expected_identity)

    monkeypatch.setattr(notifier, "_send_slack", changed)
    result = notifier.notify(event())
    assert result.provider_outcomes == {"slack": "uncertain"}
    assert not calls
    with sqlite3.connect(tmp_path / "notifications/dual-deliveries.sqlite3") as db:
        assert db.execute("SELECT slack FROM receipts").fetchone()[0] == "uncertain"


def test_bad_lark_route_does_not_block_independent_slack_claim(tmp_path):
    notifier, calls, current, scopes = setup_notifier(tmp_path, both=True)
    routes = SessionRoutes(notifier.lark_thread_store, "lark", notifier.config.lark.scope)
    journal = notifier.lark_thread_store.journal
    with journal.transaction() as db:
        journal.put(db, "session_routes", routes._route_key(SID), dict(schema_version=1, provider="lark",
            scope=notifier.config.lark.scope, thread_id=SID, destination="bad-destination",
            last_command_ts="1792008000000", created_at="2026-01-01T00:00:00Z"))
    result = notifier.notify(event())
    assert result.provider_outcomes == {"slack": "sent", "lark": "failed"}
    assert len(calls) == 1


def test_dual_fanout_preserves_source_birth_and_separate_parent_creation(tmp_path):
    notifier, calls, current, scopes = setup_notifier(tmp_path, both=True)
    lark_calls = []

    class Api:
        def send(self, text, fingerprint, **kwargs):
            lark_calls.append((text, fingerprint, kwargs))
            return dict(chat_id=notifier.config.lark.chat_id, message_id="om_fixture00000001")

    notifier.lark_api = Api()
    original = event()
    result = notifier.notify(original)
    assert result.status == "sent" and result.provider_outcomes == {"slack": "sent", "lark": "sent"}
    assert len(calls) == len(lark_calls) == 1
    assert source_time(notifier.lark_thread_store.notification_mappings(original.event_fingerprint())[0]["created_at"]) == START+5
    with sqlite3.connect(tmp_path / "notifications/dual-deliveries.sqlite3") as db:
        assert db.execute("SELECT born FROM retention").fetchall() == [(START+1,), (START+1,)]


def test_fallback_claim_uses_exact_source_session_and_birth(tmp_path):
    scopes = [(SID, sha256_text(WORKSPACE), "windows_msg")]
    finite.prepare_notification_receipts(tmp_path, scopes=scopes, contexts=(),
        sender_fence=nullcontext, clock=lambda: START)
    calls = []
    config = NotificationConfig(windows_message_enabled=True, windows_message_target="fixture-user")
    notifier = EnvironmentNotifier(tmp_path, config,
        message_runner=lambda args, timeout: calls.append(args) or 0, clock=lambda: START+5)
    original = replace(event(), relay_target=None, source_thread_id=SID)
    assert notifier.notify(original).status == "sent"
    with sqlite3.connect(tmp_path / "notifications/fallback-deliveries.sqlite3") as db:
        assert db.execute("SELECT thread_id,born FROM retention").fetchone() == (SID,START+1)
    assert len(calls) == 1


def test_notification_context_contains_no_credentials(tmp_path):
    import json
    notifier, calls, current, scopes = setup_notifier(tmp_path, both=True)
    context = json.dumps(notifier.receipt_context(event()))
    assert "xoxb-fixture" not in context and "xapp-fixture" not in context and "fixture-secret" not in context
    assert "oc_fixture" not in context and "C12345678" not in context


def test_lark_delayed_send_cannot_extend_original_parent_time(tmp_path):
    from codex_watchdog.lark_mapping import LarkThreadStore
    from codex_watchdog.lark_transport import LarkTransportError
    from datetime import datetime, timezone
    store = LarkThreadStore(tmp_path, sha256_text("fixture"), clock=lambda: START+finite.TTL+1)
    born = datetime.fromtimestamp(START, timezone.utc).isoformat().replace("+00:00", "Z")
    with pytest.raises(LarkTransportError, match="ticket_retired"):
        store.prepare_notification(sha256_text("old"), sha256_text("payload"), created_at=born, thread_id=SID)
    with store.journal.transaction() as db:
        assert store.journal.get(db, "notifications", sha256_text("old")) is None


@pytest.mark.parametrize("value", [True, float("nan"), -1, "2026-10-14T00:00:00", "bad"])
def test_unverified_source_times_are_not_original_birth(value):
    assert source_time(value) is None


def test_z_source_time_preserves_original_utc_instant():
    assert source_time("2026-10-14T01:02:03Z") == source_time("2026-10-14T01:02:03+00:00")
