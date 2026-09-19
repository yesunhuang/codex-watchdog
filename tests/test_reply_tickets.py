"""Product-level ticket bounds, atomic claims, migration and indexed history."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.lark_relay import LarkReplyRelay
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.models import sha256_text
from codex_watchdog.onebot_relay import OneBotThreadStore, OneBotReplyRelay
from codex_watchdog.onebot_transport import OneBotConfig
from codex_watchdog.relay import RelayTarget
from codex_watchdog.slack_mapping import SlackThreadStore
from codex_watchdog.slack_poll import SlackPollingThreadStore, SlackReplyPoller
from codex_watchdog.slack_relay import SlackReplyRelay
from codex_watchdog.storage import FileLock, StoreBusyError

CHANNEL, USER = "C12345678", "U12345678"
CHAT, LUSER = "oc_fixture000001", "ou_fixture000001"
LCFG = LarkConfig("cli_fixture000001", "fixture", CHAT, (LUSER,))
QCFG = OneBotConfig("ws://127.0.0.1:3001", "fixture", "12345", "private", "54321", ("54321",))


def target(n):
    return RelayTarget("workspace-" + str(n), "11111111-2222-4333-8444-{:012d}".format(n), "process_local")


def fixture(runtime, provider, *, outcome="enqueued"):
    calls = []
    def dispatch(*args):
        calls.append(args)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(status=outcome)
    queue = SimpleNamespace(dispatch=dispatch)
    if provider.startswith("slack"):
        relay = SlackReplyRelay(runtime, bot_token="xoxb-fixture", app_token="xapp-fixture",
            channel_id=CHANNEL, allowed_user_ids=(USER,), queue_dispatcher=queue,
            remote_ssh_adapter=None, reply_mode="poll" if provider == "slack-poll" else "socket")
    elif provider == "lark":
        relay = LarkReplyRelay(runtime, LCFG, queue_dispatcher=queue, remote_ssh_adapter=None)
    else:
        relay = OneBotReplyRelay(runtime, QCFG, queue_dispatcher=queue, remote_ssh_adapter=None)
    return relay, calls


def parent(provider, n):
    return "1789111600.{:06d}".format(n) if provider.startswith("slack") else (
        "om_notice{:08d}".format(n) if provider == "lark" else str(-n))


def record(store, provider, n):
    fingerprint = sha256_text("notice:" + str(n))
    if provider.startswith("slack"):
        store.record_thread(CHANNEL, parent(provider, n), target(n), fingerprint)
    else:
        store.prepare_notification(fingerprint, sha256_text("payload:" + str(n)))
        store.finish_notification(fingerprint, CHAT if provider == "lark" else QCFG.destination,
                                  parent(provider, n), target(n))


def reply(relay, provider, n, serial=1):
    if provider.startswith("slack"):
        return relay.handle_message(dict(type="message", user=USER, channel=CHANNEL,
            thread_ts=parent(provider, n), ts="1789111700.{:06d}".format(serial),
            text="reply " + str(serial)))
    if provider == "lark":
        return relay.handle_event(dict(schema="2.0", header=dict(event_type="im.message.receive_v1",
            app_id=LCFG.app_id, event_id="fixture-event-{:08d}".format(serial)), event=dict(
            sender=dict(sender_type="user", sender_id=dict(open_id=LUSER)), message=dict(
                chat_id=CHAT, message_id="om_reply{:08d}".format(serial), message_type="text",
                root_id=parent(provider, n), parent_id=parent(provider, n),
                content=json.dumps(dict(text="reply " + str(serial)))))))
    return relay.handle_event(dict(post_type="message", self_id=12345, message_type="private",
        user_id=54321, sender=dict(user_id=54321), message_id=serial,
        message=[dict(type="reply", data=dict(id=parent(provider, n))),
                 dict(type="text", data=dict(text="reply " + str(serial)))]))


def active(store):
    journal = store.journal
    with journal.transaction() as db:
        return journal.active(db)


@pytest.mark.parametrize("provider", ["slack-poll", "slack-socket", "lark", "onebot"])
def test_fifth_retires_oldest_and_each_exact_thread_wakes_only_once(tmp_path, provider):
    relay, calls = fixture(tmp_path, provider)
    for n in range(1, 6):
        record(relay.thread_store, provider, n)
    assert len(active(relay.thread_store)) == 4
    reply(relay, provider, 1)
    assert not calls
    for n in range(2, 6):
        assert reply(relay, provider, n, n).status == "queued"
        reply(relay, provider, n, n + 100)
    assert [call[0] for call in calls] == [target(n).thread_id for n in range(2, 6)]
    assert active(relay.thread_store) == {}
    restarted, retry_calls = fixture(tmp_path, provider)
    for n in range(1, 6):
        record(restarted.thread_store, provider, n)  # Refresh can't reopen a closed ticket.
        reply(restarted, provider, n, n + 200)
    assert not retry_calls and active(restarted.thread_store) == {}


@pytest.mark.parametrize("provider", ["slack-poll", "slack-socket", "lark", "onebot"])
def test_concurrent_different_replies_can_claim_only_once(tmp_path, provider):
    relay, calls = fixture(tmp_path, provider)
    record(relay.thread_store, provider, 1)
    def attempt(i):
        try:
            return reply(relay, provider, 1, i)
        except StoreBusyError:
            return None  # A store busy before claim is safe to redeliver.
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(1, 17)))
    assert len(calls) == 1 and active(relay.thread_store) == {}


@pytest.mark.parametrize("provider", ["slack-poll", "lark", "onebot"])
def test_uncertain_and_interrupted_claims_close_immediately(tmp_path, provider):
    relay, calls = fixture(tmp_path, provider, outcome=TimeoutError("private detail"))
    record(relay.thread_store, provider, 1)
    assert reply(relay, provider, 1).status == "uncertain"
    assert not active(relay.thread_store)
    restarted, again = fixture(tmp_path, provider)
    reply(restarted, provider, 1, 2)
    assert len(calls) == 1 and not again
    journal = relay.thread_store.journal
    with journal.transaction() as db:
        assert "private detail" not in str(db.execute("SELECT value FROM records").fetchall())


def test_crash_after_claim_before_delivery_never_reopens(tmp_path):
    store = SlackPollingThreadStore(tmp_path)
    record(store, "slack-poll", 1)
    assert store.claim_reply(event_key="first", channel_id=CHANNEL, thread_ts=parent("slack", 1),
        instruction_id="first", text="literal")[0]
    restarted = SlackPollingThreadStore(tmp_path)
    assert not active(restarted)
    assert not restarted.claim_reply(event_key="second", channel_id=CHANNEL,
        thread_ts=parent("slack", 1), instruction_id="second", text="different")[0]
    assert restarted.lookup_reply("first")["state"] == "dispatching"


def test_provider_bound_shared_by_scopes_and_slack_transports(tmp_path):
    first, second = SlackThreadStore(tmp_path), SlackPollingThreadStore(tmp_path)
    for n in range(1, 4):
        record(first, "slack-socket", n)
        record(second, "slack-poll", n + 10)
    assert len(active(first)) + len(active(second)) == 4
    a, b = LarkThreadStore(tmp_path, "a" * 64), LarkThreadStore(tmp_path, "b" * 64)
    for n in range(1, 4):
        record(a, "lark", n)
        record(b, "lark", n + 10)
    assert len(active(a)) + len(active(b)) == 4
    assert len(active(first)) + len(active(second)) == 4  # Different provider budget.


@pytest.mark.parametrize("provider", ["slack-poll", "lark", "onebot"])
def test_legacy_migration_preserves_backup_and_retires_unknown_history(tmp_path, provider):
    relay, calls = fixture(tmp_path, provider)
    store = relay.thread_store
    if provider.startswith("slack"):
        key = store.thread_key(CHANNEL, parent(provider, 1))
        entry = dict(channel_id=CHANNEL, thread_ts=parent(provider, 1))
        legacy = dict(schema_version=1, threads={}, events={})
    else:
        chat = CHAT if provider == "lark" else QCFG.destination
        key = store._address(chat, parent(provider, 1))
        entry = dict(chat_id=chat, message_id=parent(provider, 1))
        legacy = dict(schema_version=1, scope=store.scope, threads={}, events={}, messages={}, notifications={})
    entry.update(target=target(1).to_dict(), event_fingerprint="a" * 64, created_at="2026-09-18T00:00:00Z")
    legacy["threads"][key] = entry
    store.path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(legacy).encode()
    store.path.write_bytes(original)
    assert not active(store)
    assert store.path.with_name(store.path.name + ".v1-backup").read_bytes() == original
    assert store.has_notification_mapping("a" * 64)  # Retain outbound dedup evidence.
    reply(relay, provider, 1)
    assert calls == []
    record(store, provider, 2)
    assert reply(relay, provider, 2, 2).status == "queued"
    restarted, _ = fixture(tmp_path, provider)
    assert active(restarted.thread_store) == {}


def test_thousands_of_history_entries_do_not_enter_polling_or_cursor_set(tmp_path):
    relay, calls = fixture(tmp_path, "slack-poll")
    store, api_calls = relay.thread_store, []
    journal = store.journal
    with journal.transaction() as db:
        for n in range(10000):
            journal.put(db, "threads", sha256_text(str(n)), dict(channel_id=CHANNEL,
                thread_ts="1780000000.{:06d}".format(n), target=target(1).to_dict(),
                event_fingerprint=sha256_text(str(n)), created_at="2026-01-01T00:00:00Z"))
    for n in range(1, 5):
        record(store, "slack-poll", n)
    poller = SlackReplyPoller(relay, api=lambda method, params:
        (api_calls.append(params["ts"]) or dict(messages=[])))
    poller.path.write_text(json.dumps(dict(schema_version=1, after=None,
        threads={sha256_text(str(n)): "1780000001.000001" for n in range(10000)})))
    for _ in range(4):
        poller.poll_once()
    assert set(api_calls) == {parent("slack", n) for n in range(1, 5)}
    assert json.loads(poller.path.read_text())["threads"] == {}
    with journal.transaction() as db:
        plan = str(db.execute("EXPLAIN QUERY PLAN SELECT * FROM records WHERE active=1").fetchall())
        assert "active_tickets" in plan
    assert len(store.poll_mappings()) == 4 and not calls


def test_legacy_import_is_closed_and_refresh_cannot_reopen(tmp_path):
    store = LarkThreadStore(tmp_path, LCFG.scope)
    envelope = dict(provider="lark", scope=LCFG.scope, chat_id=CHAT,
                    message_id=parent("lark", 1), event_fingerprint="a" * 64)
    store.cache_mappings([envelope], target(1))
    assert not active(store)
    envelope.update(ticket_schema=1, created_at="2026-09-18T00:00:00Z")
    store.cache_mappings([envelope], target(1))
    assert not active(store)


def test_busy_before_claim_can_retry_without_admission(tmp_path):
    relay, calls = fixture(tmp_path, "slack-poll")
    record(relay.thread_store, "slack-poll", 1)
    with FileLock(relay.thread_store.lock_path):
        assert reply(relay, "slack-poll", 1).status == "deferred"
    assert not calls
    assert reply(relay, "slack-poll", 1).status == "queued"


def test_crash_after_migration_commit_repairs_marker_without_reimport(tmp_path, monkeypatch):
    from codex_watchdog.storage import InstructionStore
    store = SlackPollingThreadStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    legacy = dict(schema_version=1, threads={}, events={})
    store.path.write_text(json.dumps(legacy))
    write = InstructionStore._atomic_json
    def fail_marker(path, value):
        if path == store.path:
            raise OSError("fixture interrupted marker publication")
        return write(path, value)
    monkeypatch.setattr(InstructionStore, "_atomic_json", fail_marker)
    with pytest.raises(OSError):
        active(store)
    monkeypatch.setattr(InstructionStore, "_atomic_json", write)
    assert active(store) == {}
    assert json.loads(store.path.read_text())["schema_version"] == 2
    record(store, "slack-poll", 1)
    assert len(active(store)) == 1


def test_migrated_reply_receipt_stays_terminal(tmp_path):
    store = SlackThreadStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    entry = dict(thread_key="a" * 64, instruction_id="slack:old", text_sha256="b" * 64,
        text_chars=4, state="uncertain", delivery_status="exception", created_at="2026-01-01T00:00:00Z",
        updated_at=None, error_sha256=None)
    store.path.write_text(json.dumps(dict(schema_version=1, threads={}, events={sha256_text("old"): entry})))
    assert store.lookup_reply("old") == entry
    assert not active(store)


def test_poll_rate_limit_keeps_backoff_and_does_not_advance_cursor(tmp_path, monkeypatch):
    from urllib.error import HTTPError
    import codex_watchdog.slack_poll as polling
    relay, _ = fixture(tmp_path, "slack-poll")
    record(relay.thread_store, "slack-poll", 1)
    def limited(*args):
        raise HTTPError("https://slack.com/api/conversations.replies", 429, "limited",
                        {"Retry-After": "90"}, None)
    poller = SlackReplyPoller(relay, api=limited)
    monkeypatch.setattr(polling.time, "monotonic", lambda: 100)
    class OneTick:
        done = False
        def is_set(self): return self.done
        def wait(self, seconds): self.done = True
    poller.stop = OneTick()
    poller._run()
    assert poller.next_poll == 190
    assert not poller.path.exists()
    assert json.loads(poller.health_path.read_text())["retry_seconds"] == 90
