"""Actual active-ticket polling under scoped faults and unrelated history."""

import json
import sqlite3
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from codex_watchdog.notifications import NotificationConfig
from codex_watchdog.reply_tickets import ReplyTickets
from codex_watchdog.slack_mapping import SlackRelayTarget, SlackThreadStore
from codex_watchdog.slack_poll import SlackReplyPoller
from codex_watchdog.slack_relay import SlackReplyRelay, SlackReplyResult
from codex_watchdog.storage import InstructionStore


CHANNEL = "C12345678"
USER = "U12345678"


class PollRig:
    def __init__(self, root, *, sessions=1, tickets=2):
        self.deliveries = []
        self.calls = []
        self.relay = SlackReplyRelay.from_notification_config(root, NotificationConfig(
            slack_bot_token="xoxb-fixture", slack_channel_id=CHANNEL,
            slack_allowed_user_ids=(USER,), slack_reply_mode="poll"),
            queue_dispatcher=SimpleNamespace(dispatch=self.dispatch), remote_ssh_adapter=None)
        self.targets = [SlackRelayTarget("workspace", f"11111111-2222-4333-8444-{i:012d}",
                                        "process_local") for i in range(sessions)]
        self.recorded = []
        for i, target in enumerate(self.targets):
            for j in range(tickets):
                parent = f"178911{1600 + i * 10 + j}.000001"
                self.relay.thread_store.record_thread(CHANNEL, parent, target, f"{i * 10 + j:064x}")
                self.recorded.append(parent)
        self.pages = {}

    def dispatch(self, *args):
        self.deliveries.append(args)
        return SimpleNamespace(status="enqueued")

    def mappings(self):
        return self.relay.thread_store.poll_mappings()

    def parents(self):
        return [value["thread_ts"] for _, value in sorted(self.mappings().items())]

    def key(self, parent):
        return self.relay.thread_store.thread_key(CHANNEL, parent)

    def message(self, parent, *, index=0, user=USER):
        return dict(type="message", ts=f"1789112244.{index:06d}", thread_ts=parent,
                    user=user, text="Run the fixture instruction.")

    def api(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "conversations.replies":
            return dict(messages=self.pages.get(params["ts"], []))
        assert method == "chat.postMessage"
        return dict(ok=True, channel=CHANNEL, ts="1789113000.000001")

    def reads(self):
        return [params["ts"] for method, params in self.calls if method == "conversations.replies"]


@pytest.mark.parametrize("fault", ["api", "page", "handler", "ack"])
def test_scoped_exception_rotates_before_restart_without_losing_unread_reply(tmp_path, monkeypatch, fault):
    rig = PollRig(tmp_path)
    bad, healthy = rig.parents()
    unread = "1789112000.000001"
    rig.pages[healthy] = [rig.message(healthy)]
    original_handler = rig.relay.handle_polled_message
    original_ack = rig.relay.acknowledge

    def api(method, params):
        value = rig.api(method, params)
        if method == "conversations.replies" and params["ts"] == bad:
            if fault == "api":
                raise OSError("fixture inaccessible parent")
            if fault == "page":
                return dict(messages=None)
            # ACK errors follow a processed, unauthorized message, so that
            # parent remains active; the read cursor may advance only that far.
            return dict(messages=[rig.message(bad, index=len(rig.reads()), user="U99999999")])
        return value

    def handle(event):
        if fault == "handler" and event["thread_ts"] == bad:
            raise OSError("fixture handler fault")
        return original_handler(event)

    def acknowledge(event, result, client):
        if fault == "ack" and event["thread_ts"] == bad:
            raise OSError("fixture acknowledgement fault")
        return original_ack(event, result, client)

    monkeypatch.setattr(rig.relay, "handle_polled_message", handle)
    monkeypatch.setattr(rig.relay, "acknowledge", acknowledge)
    poller = SlackReplyPoller(rig.relay, api=api)
    InstructionStore._atomic_json(poller.path, dict(schema_version=1, after=None,
        threads={rig.key(bad): unread}, extension={"preserved": True}))
    for _ in range(4):
        # Every attempt uses a new poller, including after scoped failures.
        try:
            SlackReplyPoller(rig.relay, api=api).poll_once()
        except (OSError, ValueError):
            pass
    assert rig.reads()[:2] == [bad, healthy]
    assert len(rig.deliveries) == 1
    state = json.loads(poller.path.read_text())
    assert state["extension"] == {"preserved": True}
    assert state["threads"][rig.key(bad)] == (
        "1789112244.000004" if fault == "ack" else unread)
    assert rig.key(healthy) not in rig.mappings()


def test_schedule_write_failure_prevents_provider_attempt_and_keeps_unread_state(tmp_path, monkeypatch):
    rig = PollRig(tmp_path)
    poller = SlackReplyPoller(rig.relay, api=rig.api)
    before = dict(schema_version=1, after=None, threads={}, extension=["keep"])
    InstructionStore._atomic_json(poller.path, before)
    atomic = InstructionStore._atomic_json

    def write(path, value):
        if path == poller.path:
            raise OSError("fixture scheduler disk failure")
        return atomic(path, value)

    monkeypatch.setattr(InstructionStore, "_atomic_json", write)
    with pytest.raises(OSError, match="scheduler"):
        poller.poll_once()
    assert rig.calls == [] and rig.deliveries == []
    assert json.loads(poller.path.read_text()) == before


def test_deferred_parent_rotates_and_retries_the_exact_unread_message(tmp_path, monkeypatch):
    rig = PollRig(tmp_path)
    deferred, healthy = rig.parents()
    rig.pages[deferred] = [rig.message(deferred)]
    handler = rig.relay.handle_polled_message
    monkeypatch.setattr(rig.relay, "handle_polled_message", lambda event: SlackReplyResult("deferred"))
    for _ in range(4):
        SlackReplyPoller(rig.relay, api=rig.api).poll_once()
    assert rig.reads() == [deferred, healthy, deferred, healthy]
    poller = SlackReplyPoller(rig.relay, api=rig.api)
    assert json.loads(poller.path.read_text())["threads"] == {}
    assert rig.deliveries == []
    monkeypatch.setattr(rig.relay, "handle_polled_message", handler)
    assert poller.poll_once()[0]["status"] == "queued"
    assert len(rig.deliveries) == 1
    assert [params["oldest"] for method, params in rig.calls
            if method == "conversations.replies" and params["ts"] == deferred] == [deferred] * 3


def test_handler_fault_preserves_last_processed_reply_and_retries_first_unread(tmp_path, monkeypatch):
    rig = PollRig(tmp_path)
    bad, healthy = rig.parents()
    processed = rig.message(bad, user="U99999999")
    unread = rig.message(bad, index=1)
    rig.pages[bad] = [processed, unread, rig.message(bad, index=2)]
    original = rig.relay.handle_polled_message

    def handle(event):
        if event["ts"] == unread["ts"]:
            raise OSError("fixture first unread handler")
        return original(event)

    monkeypatch.setattr(rig.relay, "handle_polled_message", handle)
    poller = SlackReplyPoller(rig.relay, api=rig.api)
    with pytest.raises(OSError, match="first unread"):
        poller.poll_once()
    assert json.loads(poller.path.read_text())["threads"][rig.key(bad)] == processed["ts"]
    assert rig.deliveries == []
    assert SlackReplyPoller(rig.relay, api=rig.api).poll_once() == []
    assert rig.reads() == [bad, healthy]
    monkeypatch.setattr(rig.relay, "handle_polled_message", original)
    assert SlackReplyPoller(rig.relay, api=rig.api).poll_once()[0]["status"] == "queued"
    assert len(rig.deliveries) == 1
    assert json.loads(poller.path.read_text())["threads"][rig.key(bad)] == unread["ts"]
    assert [params["oldest"] for method, params in rig.calls
            if method == "conversations.replies" and params["ts"] == bad] == [bad, processed["ts"]]


@pytest.mark.parametrize("code,retry_after,delay", [
    (None, None, 10), (500, None, 10), (429, "90", 90),
    (429, "2", 10), (429, "invalid", 60), (429, None, 60),
])
def test_run_rotates_after_fault_but_respects_provider_wide_backoff(
        tmp_path, monkeypatch, code, retry_after, delay):
    from codex_watchdog import slack_poll

    rig = PollRig(tmp_path)
    bad, healthy = rig.parents()
    rig.pages[healthy] = [rig.message(healthy)]
    clock = [0.0]
    attempts = []

    def api(method, params):
        value = rig.api(method, params)
        if method == "conversations.replies":
            attempts.append((clock[0], params["ts"]))
            if params["ts"] == bad:
                if code is None:
                    raise OSError("fixture network failure")
                headers = {} if retry_after is None else {"Retry-After": retry_after}
                raise HTTPError("https://fixture.invalid", code, "fixture", headers, None)
        return value

    class Ticks:
        def is_set(self):
            return clock[0] > delay

        def wait(self, seconds):
            assert seconds == 1
            clock[0] += seconds

    poller = SlackReplyPoller(rig.relay, api=api)
    poller.stop = Ticks()
    monkeypatch.setattr(slack_poll.time, "monotonic", lambda: clock[0])
    poller._run()
    assert attempts == [(0, bad), (delay, healthy)]
    assert poller.next_poll == delay + 10
    assert len(rig.deliveries) == 1
    state = json.loads(poller.path.read_text())
    assert rig.key(bad) not in state["threads"]
    assert state["after"] == rig.key(healthy)


def test_full_noisy_page_consumes_one_api_page_then_other_active_parent_progresses(tmp_path):
    rig = PollRig(tmp_path)
    noisy, healthy = rig.parents()
    rig.pages[noisy] = [rig.message(noisy, index=i, user="U99999999") for i in range(100)]
    rig.pages[healthy] = [rig.message(healthy, index=101)]

    def api(method, params):
        value = rig.api(method, params)
        if method == "conversations.replies":
            assert params["limit"] == 100 and params["inclusive"] == "false"
            value["has_more"] = True
        return value

    poller = SlackReplyPoller(rig.relay, api=api)
    assert len(poller.poll_once()) == 100
    assert rig.reads() == [noisy] and rig.deliveries == []
    assert poller.poll_once()[0]["status"] == "queued"
    assert rig.reads() == [noisy, healthy]
    assert len(rig.deliveries) == 1
    assert json.loads(poller.path.read_text())["threads"][rig.key(noisy)] == "1789112244.000099"


@pytest.mark.parametrize("delivery_status", ["enqueued", "uncertain"])
def test_ack_exception_after_admission_never_replays_task_or_closed_parent(tmp_path, delivery_status):
    rig = PollRig(tmp_path)
    first, healthy = rig.parents()
    rig.relay.queue_dispatcher.dispatch = lambda *args: (
        rig.deliveries.append(args) or SimpleNamespace(status=delivery_status))
    rig.pages[first] = [rig.message(first), rig.message(first, index=1)]
    rig.pages[healthy] = [rig.message(healthy, index=2)]

    def api(method, params):
        result = rig.api(method, params)
        if method == "chat.postMessage" and params["thread_ts"] == first:
            raise OSError("fixture uncertain acknowledgement")
        return result

    poller = SlackReplyPoller(rig.relay, api=api)
    with pytest.raises(OSError, match="acknowledgement"):
        poller.poll_once()
    assert len(rig.deliveries) == 1
    assert json.loads(poller.path.read_text())["threads"][rig.key(first)] == rig.message(first)["ts"]
    # Even total cursor loss cannot reopen an admitted ticket or resend its ACK.
    poller.path.unlink()
    restarted = SlackReplyPoller(rig.relay, api=api)
    assert restarted.poll_once()[0]["status"] in ("queued", "uncertain")
    assert restarted.poll_once() == []
    assert rig.reads() == [first, healthy]
    assert len(rig.deliveries) == 2
    assert len([1 for method, params in rig.calls
                if method == "chat.postMessage" and params["thread_ts"] == first]) == 1
    journal = rig.relay.thread_store.journal
    with journal.transaction() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='events'").fetchone()[0] == 2


def test_four_ticket_cap_is_per_session_and_transport_scope_stays_separate(tmp_path):
    rig = PollRig(tmp_path, sessions=3, tickets=5)
    active = rig.mappings()
    assert len(active) == 12  # Three sessions; no global four-session cap.
    for target in rig.targets:
        assert sum(value["target"]["thread_id"] == target.thread_id for value in active.values()) == 4
    evicted = set(rig.recorded) - set(rig.parents())
    assert len(evicted) == 3

    # A socket ticket shares Slack's per-session cap but is not polled here.
    socket = SlackThreadStore(tmp_path)
    socket_parent = "1789111900.000001"
    socket.record_thread(CHANNEL, socket_parent, rig.targets[0], "f" * 64)
    assert len(rig.mappings()) == 11
    closed = rig.parents()[0]
    journal = rig.relay.thread_store.journal
    with journal.transaction() as db:
        assert journal.claim(db, rig.key(closed))
    active_parents = rig.parents()
    assert len(active_parents) == 10
    bad = active_parents[0]
    poller = SlackReplyPoller(rig.relay, api=rig.api)
    InstructionStore._atomic_json(poller.path, dict(schema_version=1, after=None,
        threads={rig.key(parent): "1789112000.000001" for parent in rig.recorded}))

    def api(method, params):
        value = rig.api(method, params)
        if params["ts"] == bad:
            raise OSError("fixture scoped failure")
        return value

    for _ in range(20):
        try:
            SlackReplyPoller(rig.relay, api=api).poll_once()
        except OSError:
            pass
    assert rig.reads() == active_parents * 2
    assert not set(rig.reads()) & (evicted | {closed, socket_parent})
    assert set(json.loads(poller.path.read_text())["threads"]) == set(rig.mappings())
    assert rig.deliveries == []


def test_history_growth_keeps_fixed_active_discovery_and_index_work_bounded(tmp_path, monkeypatch, record_property):
    measurements = []
    original_connect = sqlite3.connect
    original_active = ReplyTickets.active
    for history_count in (0, 1_000, 10_000, 100_000):
        rig = PollRig(tmp_path / str(history_count), tickets=4)
        parents = rig.parents()
        bad, healthy = parents[0], parents[-1]
        rig.pages[healthy] = [rig.message(healthy)]
        journal = rig.relay.thread_store.journal
        # Grow real schema-2 audit rows, outside the measured polling hot path.
        with journal.transaction() as db:
            db.executemany("INSERT INTO records(namespace,kind,key,value,thread_id,active,created_at) "
                "VALUES(?,?,?,?,?,0,?)", ((journal.namespace, "threads", f"{1_000_000 + i:064x}",
                json.dumps(dict(channel_id=CHANNEL, thread_ts=f"1700{i:06d}.000001",
                    target=rig.targets[0].to_dict(), event_fingerprint="a" * 64,
                    created_at="2020-01-01T00:00:00Z")), rig.targets[0].thread_id,
                    "2020-01-01T00:00:00Z") for i in range(history_count)))
        statements = []
        vm_steps = [0]
        active_steps = []

        def progress():
            vm_steps[0] += 1
            return 0

        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            assert str(args[0]) == str(journal.database)
            db.set_trace_callback(statements.append)
            db.set_progress_handler(progress, 1)
            return db

        def active(store, db):
            before = vm_steps[0]
            result = original_active(store, db)
            active_steps.append(vm_steps[0] - before)
            return result

        def api(method, params):
            value = rig.api(method, params)
            if method == "conversations.replies" and params["ts"] == bad:
                raise OSError("fixture inaccessible parent")
            return value

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            patch.setattr(ReplyTickets, "active", active)
            started = time.perf_counter()
            for _ in range(4):
                try:
                    SlackReplyPoller(rig.relay, api=api).poll_once()
                except OSError:
                    pass
            seconds = time.perf_counter() - started
        assert rig.reads() == parents
        assert len(rig.deliveries) == 1
        assert len([method for method, _ in rig.calls if method == "chat.postMessage"]) == 1
        # Explain the queries actually executed, not substitute benchmark SQL.
        selects = sorted({sql for sql in statements if sql.startswith("SELECT ")})
        with original_connect(str(journal.database)) as db:
            plans = sorted({row[3] for sql in selects
                            for row in db.execute("EXPLAIN QUERY PLAN " + sql)})
        assert any("namespace_active_tickets" in plan for plan in plans)
        assert any("sqlite_autoindex_records_1" in plan for plan in plans)
        assert all("SCAN records" not in plan for plan in plans)
        measurements.append(dict(history_rows=history_count, active_parents=4, discovery_tick=4,
            read_pages=len(rig.reads()), acknowledgement_attempts=1, synthetic_dispatches=1,
            sqlite_statements=len(statements), sqlite_vm_steps=vm_steps[0], active_vm_steps=active_steps,
            query_plans=plans, fixture_poll_seconds=seconds))
    for field in ("discovery_tick", "read_pages", "acknowledgement_attempts", "synthetic_dispatches",
                  "sqlite_statements", "sqlite_vm_steps", "active_vm_steps", "query_plans"):
        assert all(value[field] == measurements[0][field] for value in measurements), (field, measurements)
    record_property("history_growth", json.dumps(measurements, sort_keys=True))
