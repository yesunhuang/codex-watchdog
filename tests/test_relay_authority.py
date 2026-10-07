"""Real shared authority journals survive native-node changes without replay."""
from pathlib import Path, PureWindowsPath
import json
import os
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.control_context import acting_as, current_effect
from codex_watchdog.lark_mapping import LarkThreadStore
from codex_watchdog.linux_relay_migration import initialize_empty_authority
from codex_watchdog.models import sha256_text
from codex_watchdog.node_observation import NodeObservation
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.onebot_relay import OneBotThreadStore
from codex_watchdog.relay import RelayTarget
from codex_watchdog.relay_authority import SessionRelayAuthority, bind_service_authority
from codex_watchdog.relay_native_completion import completion_progress
from codex_watchdog.slack_bot_acl import BotRequestReused
from codex_watchdog.slack_poll import SlackPollingThreadStore, SlackReplyPoller
from codex_watchdog.slack_relay import SlackReplyRelay
from test_linux_node import THREAD, nodes
from test_slack_bot_acl import BOT, CHANNEL, Harness


LOGIN6 = "cluster-login6.example"
LOGIN3 = "cluster-login3.example"
LOGIN4 = "cluster-login4.example"


def _stamp(home):
    path = home / "sessions/thread.jsonl"
    stat = path.stat()
    return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _admit(authority, home):
    with authority.observation.guard(rollout_stamp=_stamp(home)) as admitted:
        if admitted:
            authority.activate()
        return admitted


def _node(nodes, node=LOGIN6, *, thread=THREAD, activate=True, boot=None,
          clock=100, instance=None):
    home, repo, host, _, pid = nodes
    host[0], pid[0] = node, 123
    root = cs.control_node_directory(home)
    cs.control_atomic_json(root / "node.json", dict(schema_version=1, node=node, codex_home=str(home)))
    with sqlite3.connect(home / "state_5.sqlite") as db:
        if db.execute("SELECT 1 FROM threads WHERE id=?", (thread,)).fetchone() is None:
            db.execute("INSERT INTO threads VALUES (?,?,'vscode','user',0,?)",
                       (thread, str(repo), str(home / "sessions/thread.jsonl")))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE IF NOT EXISTS queued_items(id,thread_id,payload_json)")
        db.execute("CREATE TABLE IF NOT EXISTS queued_thread_revisions(thread_id,revision)")
    store = cs.ControlStore(home, thread, repo, clock=lambda: clock, boot_id=boot or "boot-" + node)
    store.enroll_node()
    token = store.claim_remote(instance or "dog-" + node, node, "vscode", host_observer=True)
    observation = NodeObservation(store, token)
    authority = SessionRelayAuthority(store, token, observation, "workspace-" + thread)
    cluster = authority.root / "cluster.json"
    if not cluster.exists():
        cs.control_atomic_json(cluster, dict(schema_version=1, codex_home=str(home),
            state="ready", storage="cluster_provider_journals_v1", plan_sha256="a" * 64))
    if not authority.manifest.exists():
        cs.control_atomic_json(authority.manifest, dict(schema_version=1, thread_id=thread,
            repo_path=str(repo), state="ready", storage="cluster_provider_journals_v1"))
    observation.relay_handoff_safe = authority.handoff_safe
    if activate:
        assert _admit(authority, home), observation.reason
    local_runtime = Path(store.read()["runtime_path"])
    inbox = SlackPollingThreadStore(local_runtime)
    inbox.relay_authority = authority
    return authority, Harness(inbox)


def _target(authority):
    route = authority.store.read()["remote_target"]
    # The immutable envelope names a Linux repository. Windows fixture files
    # remain native paths in ControlStore; they are not valid remote paths.
    remote_repo = ("/fixture/project" if PureWindowsPath(route["repo_path"]).is_absolute()
                   else Path(route["repo_path"]).as_posix())
    return RelayTarget(authority.workspace_id, authority.thread_id, "remote_ssh",
                       route["authority"], remote_repo, route["storage_key"])


def _park(nodes, authority):
    home, _, host, _, pid = nodes
    host[0], pid[0] = authority.observation.node, None
    assert _admit(authority, home)


def _grant(harness, target):
    result, event = harness.control(target=target)
    assert result["status"] == "added"
    harness.store.finish_reply(event["event_key"], state_value="delivered", delivery_status="control_added")


def _active(store):
    with store.journal.transaction() as db:
        return store.journal.active(db)


def _rows(database):
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT namespace,kind,key,value,thread_id,active FROM records "
                          "ORDER BY namespace,kind,key").fetchall()


def test_login6_to_login3_to_third_preserves_principal_claim_cursor_and_old_envelope(nodes):
    home = nodes[0]
    first, six = _node(nodes)
    immutable = _target(first)
    _grant(six, immutable)
    source = six.source(immutable)
    request = str(uuid.uuid4())
    args = six.admission(source, target=immutable, request=request)
    assert six.store.claim_reply(**args) == (True, None)
    # Model this fixture's proven-ended dispatch, not merely queue admission.
    six.store.finish_reply(args["event_key"], state_value="delivered", delivery_status="completed")
    cursor = {"schema_version": 1, "after": source[0], "threads": {source[0]: "1791389141.470039"},
              "future_choice": {"keep": True}}
    first.write_cursor("slack", "poll", cursor)
    next_source = six.source(immutable)
    grant_before = six.acl.principals(first.thread_id)
    owner6 = first.store.path.read_bytes()
    native_db = (home / "state_5.sqlite").read_bytes()
    _park(nodes, first)
    second, three = _node(nodes, LOGIN3)
    three.sequence = six.sequence
    assert second.expected["relay_epoch"] == first.expected["relay_epoch"] + 1
    assert three.acl.principals(second.thread_id) == grant_before == [BOT]
    assert three.acl.known_user(BOT.user_id)
    assert three.store.lookup_thread(source[1], source[2]).target == immutable
    assert second.read_cursor("slack", "poll", {}) == cursor
    assert three.store.claim_reply(**args) == (False, "completed")
    assert three.store.claim_reply(**dict(args, event_key="changed-physical-envelope")) == (False, "completed")
    with pytest.raises(BotRequestReused):
        three.store.claim_reply(**three.admission(next_source, target=immutable,
                                                 request=request, suffix="different"))
    fresh = three.admission(next_source, target=immutable, request=str(uuid.uuid4()), suffix="fresh")
    assert three.store.claim_reply(**fresh) == (True, None)
    three.store.finish_reply(fresh["event_key"], state_value="delivered", delivery_status="completed")
    later_cursor = dict(cursor, threads={source[0]: "1791389142.470040"})
    second.write_cursor("slack", "poll", later_cursor)
    _park(nodes, second)
    third, four = _node(nodes, LOGIN4)
    assert third.expected["relay_epoch"] == second.expected["relay_epoch"] + 1
    assert four.acl.principals(third.thread_id) == [BOT]
    assert third.read_cursor("slack", "poll", {}) == later_cursor
    assert four.store.lookup_thread(source[1], source[2]).target == immutable
    assert four.store.claim_reply(**args) == (False, "completed")
    assert first.store.path.read_bytes() == owner6
    assert (home / "state_5.sqlite").read_bytes() == native_db
    assert first.store.read()["native_boot_id"] != second.store.read()["native_boot_id"]
    assert first.store.directory != second.store.directory != third.store.directory


@pytest.mark.parametrize("native_repo", [r"C:\Users\fixture\project", r"\\server\share\project"])
def test_windows_fixture_repo_is_not_used_as_an_immutable_remote_path(native_repo):
    route = dict(authority="ssh-remote+" + LOGIN6, repo_path=native_repo, storage_key="a" * 32)
    authority = SimpleNamespace(workspace_id="workspace", thread_id=THREAD,
                                store=SimpleNamespace(read=lambda: dict(remote_target=route)))
    target = _target(authority)
    assert target.remote_repo_path == "/fixture/project"
    assert RelayTarget.from_dict(target.to_dict()) == target
    with pytest.raises(ValueError, match="routing is invalid"):
        RelayTarget("workspace", THREAD, "remote_ssh", route["authority"], native_repo, route["storage_key"])


@pytest.mark.skipif(os.name == "nt", reason="Linux executor resolution uses native POSIX repository paths")
def test_linux_native_handoff_resolves_immutable_target_at_each_current_executor(nodes):
    current, _ = _node(nodes)
    immutable = _target(current)
    for node in (LOGIN6, LOGIN3, LOGIN4):
        if node != LOGIN6:
            _park(nodes, current)
            current, _ = _node(nodes, node)
        resolved = current.resolve_target(immutable)
        assert resolved.authority == "ssh-remote+" + node
        assert resolved.repo_path == immutable.remote_repo_path


def test_stale_claim_and_cursor_write_are_fenced_before_any_durable_mutation(nodes):
    first, six = _node(nodes)
    immutable = _target(first)
    _grant(six, immutable)
    source = six.source(immutable)
    args = six.admission(source, target=immutable, request=str(uuid.uuid4()))
    first.write_cursor("slack", "poll", {"threads": {source[0]: source[2]}})
    _park(nodes, first)
    second, three = _node(nodes, LOGIN3)
    before = _rows(three.store.journal.database)
    with pytest.raises(cs.ControlBusy, match="stale_epoch"):
        six.store.claim_reply(**args)
    with pytest.raises(cs.ControlBusy, match="stale_epoch"):
        first.write_cursor("slack", "poll", {"threads": {source[0]: "9999999999.999999"}})
    with pytest.raises(cs.ControlBusy, match="stale_epoch"):
        first.resolve_target(immutable)
    assert _rows(three.store.journal.database) == before
    assert second.read_cursor("slack", "poll", {}) == {"threads": {source[0]: source[2]}}


def test_unrelated_sessions_have_separate_ticket_cursor_budgets_but_global_uuid_reservations(nodes):
    first, one = _node(nodes)
    one_target = _target(first)
    _grant(one, one_target)
    source = one.source(one_target)
    request = str(uuid.uuid4())
    claim = one.admission(source, target=one_target, request=request)
    assert one.store.claim_reply(**claim) == (True, None)
    one.store.finish_reply(claim["event_key"], state_value="delivered", delivery_status="enqueued")
    for _ in range(4):
        one.source(one_target)
    first.write_cursor("slack", "poll", {"value": "session-one"})
    original = _active(one.store)
    other_thread = "22222222-3333-4444-8555-666666666666"
    second, two = _node(nodes, thread=other_thread)
    two.sequence = one.sequence + 100
    other_target = _target(second)
    _grant(two, other_target)
    second.write_cursor("slack", "poll", {"value": "session-two"})
    for _ in range(5):
        two.source(other_target)
    assert len(_active(two.store)) == 4
    assert _active(one.store) == original and len(original) == 4
    assert first.read_cursor("slack", "poll", {}) == {"value": "session-one"}
    assert second.read_cursor("slack", "poll", {}) == {"value": "session-two"}
    assert two.store.lookup_thread(source[1], source[2]) is None
    cross = two.source(other_target)
    before = _rows(two.store.journal.database)
    with pytest.raises(BotRequestReused):
        two.store.claim_reply(**two.admission(cross, target=other_target, request=request, suffix="cross"))
    assert _rows(two.store.journal.database) == before


def test_paused_token_cannot_modify_shared_claims_or_cursors(nodes):
    authority, harness = _node(nodes)
    target = _target(authority)
    _grant(harness, target)
    source = harness.source(target)
    args = harness.admission(source, target=target, request=str(uuid.uuid4()))
    before = _rows(harness.store.journal.database)
    with authority.store.guard(authority.token) as value:
        value["auto_paused"] = True
        cs.control_atomic_json(authority.store.path, value)
    with pytest.raises(cs.ControlBusy, match="paused"):
        harness.store.claim_reply(**args)
    with pytest.raises(cs.ControlBusy, match="paused"):
        authority.write_cursor("slack", "poll", {"advance": True})
    assert _rows(harness.store.journal.database) == before


@pytest.mark.parametrize("outcome", ["dispatching", "uncertain", "enqueued", "consumed_or_started", "started"])
def test_pending_portable_dispatch_blocks_handoff_without_reusing_native_state(nodes, outcome):
    home = nodes[0]
    first, six = _node(nodes)
    target = _target(first)
    _grant(six, target)
    source = six.source(target)
    args = six.admission(source, target=target, request=str(uuid.uuid4()))
    assert six.store.claim_reply(**args) == (True, None)
    if outcome == "uncertain":
        six.store.finish_reply(args["event_key"], state_value="uncertain", delivery_status="exception")
    elif outcome != "dispatching":
        six.store.finish_reply(args["event_key"], state_value="delivered", delivery_status=outcome)
    assert not first.handoff_safe()
    _park(nodes, first)
    observation_before = first.observation.path.read_bytes()
    owner_before = first.store.path.read_bytes()
    native_db = (home / "state_5.sqlite").read_bytes()
    second, _ = _node(nodes, LOGIN3, activate=False)
    assert not _admit(second, home)
    assert second.observation.reason == "control_node_relay_pending"
    assert first.observation.path.read_bytes() == observation_before
    assert first.store.path.read_bytes() == owner_before
    assert (home / "state_5.sqlite").read_bytes() == native_db
    assert second.expected is None


def test_existing_native_effect_composes_nested_authority_journal_locks(nodes):
    authority, harness = _node(nodes)
    with acting_as(authority.store, authority.token), current_effect():
        with authority.guard(), authority.guard():
            authority.write_cursor("slack", "nested", {"value": "kept"})
            assert authority.read_cursor("slack", "nested", {}) == {"value": "kept"}
            assert _active(harness.store) == {}


def test_slack_provider_fetch_cannot_advance_reply_after_native_handoff(nodes):
    first, six = _node(nodes)
    immutable = _target(first)
    source = six.source(immutable)
    _park(nodes, first)
    dispatches = []
    relay = SlackReplyRelay(Path(first.store.read()["runtime_path"]), bot_token="xoxb-fixture",
        app_token="xapp-fixture", channel_id=CHANNEL, allowed_user_ids=("U00000009",),
        thread_store=six.store, reply_mode="poll", remote_ssh_adapter=None,
        queue_dispatcher=SimpleNamespace(dispatch=lambda *args: dispatches.append(args)))
    relay.relay_authority = first
    selected = []
    def api(method, params):
        assert method == "conversations.replies"
        selected.append(_node(nodes, LOGIN3)[0])
        return {"messages": [{"type": "message", "user": "U00000009", "thread_ts": source[2],
                              "ts": "1760000010.000001", "text": "fixture fresh command"}]}
    poller = SlackReplyPoller(relay, api=api)
    with pytest.raises(cs.ControlBusy, match="stale_epoch"):
        poller.poll_once()
    current = selected[0].read_cursor("slack", "slack-active", {})
    assert current["threads"] == {}
    assert current["after"] == source[0]  # Scheduling rotates; unread reply did not advance.
    assert dispatches == []


def test_notifier_suppression_survives_restart_and_node_handoff(nodes):
    calls = []
    def api(token, method, payload, timeout):
        calls.append(payload)
        return {"ok": True, "channel": CHANNEL, "ts": "1760000010.000001"}
    config = NotificationConfig(slack_bot_token="xoxb-fixture", slack_app_token="xapp-fixture",
                                slack_channel_id=CHANNEL, slack_allowed_user_ids=("U00000009",),
                                slack_reply_mode="poll")
    first, _ = _node(nodes)
    def notifier(authority):
        runtime = Path(authority.store.read()["runtime_path"])
        instance = EnvironmentNotifier(runtime, config, slack_api_post=api)
        bind_service_authority(SimpleNamespace(notifier=instance), authority)
        return instance
    event = NotificationEvent(first.workspace_id, "codex_completed", "finished-same-turn", "done", "fixture result",
                              relay_target=_target(first))
    first_notifier = notifier(first)
    assert first_notifier.notify(event).status == "sent"
    assert notifier(first).notify(event).status == "suppressed"
    _park(nodes, first)
    second, _ = _node(nodes, LOGIN3)
    second_notifier = notifier(second)
    assert second_notifier.state_path == first_notifier.state_path
    assert second_notifier.notify(event).status == "suppressed"
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["lark", "onebot"])
def test_provider_journal_identity_and_unknown_fields_survive_handoff(nodes, provider):
    first, _ = _node(nodes)
    klass = LarkThreadStore if provider == "lark" else OneBotThreadStore
    scope = sha256_text(provider + "-fixture")
    def inbox(authority):
        store = klass(Path(authority.store.read()["runtime_path"]), scope)
        store.relay_authority = authority
        return store
    old = inbox(first)
    target = _target(first)
    chat, message = ("oc_fixture000001", "om_fixture000001") if provider == "lark" else ("private:12345", "-111")
    fingerprint = sha256_text(provider + "-notification")
    assert old.prepare_notification(fingerprint, sha256_text("fixture payload")) is None
    old.finish_notification(fingerprint, chat, message, target=target)
    first.write_cursor(provider, "active", {"parents": {"fixture": {"after": 123}}, "unknown": {"keep": True}})
    manifest_before = cs.control_read_json(first.manifest)
    manifest_before["future_profile"] = {"keep": True}
    cs.control_atomic_json(first.manifest, manifest_before)
    _park(nodes, first)
    second, _ = _node(nodes, LOGIN3)
    current = inbox(second)
    assert current.lookup_thread(chat, message).target == target
    assert current.prepare_notification(fingerprint, sha256_text("fixture payload"))["state"] == "sent"
    assert second.read_cursor(provider, "active", {})["unknown"] == {"keep": True}
    assert cs.control_read_json(second.manifest) == manifest_before
    assert current.journal.database == old.journal.database


def _admitted_native_fixture(nodes):
    authority, harness = _node(nodes)
    target = _target(authority)
    _grant(harness, target)
    source = harness.source(target)
    args = harness.admission(source, target=target, request=str(uuid.uuid4()))
    assert harness.store.claim_reply(**args) == (True, None)
    return authority, harness, args


def _completed_native_receipt(authority, args, *, thread=None, instruction=None, text=None):
    thread = thread or authority.thread_id
    instruction = instruction or args["instruction_id"]
    text = args["text"] if text is None else text
    path = authority.store.codex_home / "sessions" / "receipt-proof.jsonl"
    turn = "44444444-4444-4444-8444-444444444444"
    digest = sha256_text(text)
    marker = "[CODEX_WATCHDOG_WAKE id={} sha256={}]\n".format(instruction, digest)
    events = [dict(type="event_msg", payload=dict(type="item_completed", thread_id=thread,
                  turn_id=turn, item=dict(type="UserMessage", content=[dict(type="text", text=marker + text)]))),
              dict(type="event_msg", payload=dict(type="task_complete", thread_id=thread, turn_id=turn))]
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return dict(thread_id=thread, instruction_id=instruction, prompt_sha256=digest,
                rollout_path=str(path), rollout_baseline_offset=0,
                queue_message_id="33333333-3333-4333-8333-333333333333", state="enqueued")


@pytest.mark.parametrize("mismatch", ["thread", "instruction", "prompt", "unadmitted"])
def test_native_receipt_is_bound_to_exact_admitted_event_before_storage(nodes, mismatch):
    authority, harness, args = _admitted_native_fixture(nodes)
    changes = {}
    if mismatch == "thread":
        changes["thread"] = "22222222-2222-4222-8222-222222222222"
    elif mismatch == "instruction":
        changes["instruction"] = "slackbot:" + "b" * 40
    elif mismatch == "prompt":
        changes["text"] = "A completed but different admitted command."
    evidence = _completed_native_receipt(authority, args, **changes)
    key = "never-admitted-event" if mismatch == "unadmitted" else args["event_key"]
    before = _rows(harness.store.journal.database)
    with pytest.raises(cs.ControlError, match="native_receipt"):
        authority.record_native_delivery(harness.store, key, evidence)
    assert _rows(harness.store.journal.database) == before
    assert not authority.handoff_safe()


def test_exact_native_receipt_reconciles_original_pending_barrier_without_replay(nodes):
    authority, harness, args = _admitted_native_fixture(nodes)
    evidence = _completed_native_receipt(authority, args)
    authority.record_native_delivery(harness.store, args["event_key"], evidence)
    harness.store.finish_reply(args["event_key"], state_value="delivered", delivery_status="enqueued")
    assert not authority.handoff_safe()
    authority.reconcile_completions()
    assert authority.handoff_safe()
    original = harness.store.lookup_reply(args["event_key"])
    assert original["native_completed"] is True
    assert original["instruction_id"] == args["instruction_id"]
    assert harness.store.claim_reply(**args) == (False, "enqueued")


@pytest.mark.parametrize("mismatch", ["thread", "instruction", "prompt"])
def test_persisted_native_proof_is_revalidated_against_original_pending_event(nodes, mismatch):
    authority, harness, args = _admitted_native_fixture(nodes)
    changes = (dict(thread="22222222-2222-4222-8222-222222222222") if mismatch == "thread" else
               dict(instruction="slackbot:" + "b" * 40) if mismatch == "instruction" else
               dict(text="Different completed native command."))
    evidence = _completed_native_receipt(authority, args, **changes)
    progress, _ = completion_progress(evidence, budget=0)
    journal = harness.store.journal
    with journal.transaction() as db:
        journal.put(db, "relay_native_receipts", sha256_text(args["event_key"]),
                    dict(thread_id=authority.thread_id, receipt=evidence, progress=progress))
    harness.store.finish_reply(args["event_key"], state_value="delivered", delivery_status="enqueued")
    try:
        authority.reconcile_completions()
    except cs.ControlError:
        pass  # A specific validation error is also a safe refusal.
    assert not authority.handoff_safe()
    assert harness.store.lookup_reply(args["event_key"]).get("native_completed") is not True


def test_first_relay_epoch_preserves_same_node_selected_observer_with_pending_work(nodes):
    authority, harness, args = _admitted_native_fixture(nodes)
    harness.store.finish_reply(args["event_key"], state_value="delivered", delivery_status="enqueued")
    old = cs.control_read_json(authority.observation.path)
    old.pop("relay_epoch")
    old.pop("relay_owner")
    cs.control_atomic_json(authority.observation.path, old)
    authority.expected = None
    # This models the selected pre-upgrade observer, with a live exact native
    # writer and unchanged node/boot, gaining relay fencing for the first time.
    assert _admit(authority, nodes[0])
    assert authority.expected["relay_epoch"] == 1
    assert not authority.handoff_safe()  # First activation did not clear work.
    _park(nodes, authority)
    replacement, _ = _node(nodes, LOGIN3, activate=False)
    assert not _admit(replacement, nodes[0])
    assert replacement.observation.reason == "control_node_relay_pending"


def test_fresh_native_session_becomes_ready_after_completed_cluster_migration(nodes):
    authority, harness = _node(nodes, activate=False)
    authority.manifest.unlink()
    cs.control_atomic_json(authority.root / "cluster.json", dict(schema_version=1,
        codex_home=str(nodes[0]), state="ready", storage="cluster_provider_journals_v1",
        plan_sha256="a" * 64))
    assert _admit(authority, nodes[0])
    saved = cs.control_read_json(authority.manifest)
    assert saved["thread_id"] == authority.thread_id
    assert saved["repo_path"] == authority.repo_path and saved["state"] == "ready"
    assert _active(harness.store) == {}


def test_same_boot_native_controller_restart_can_reconcile_without_changing_executor(nodes):
    first, six, args = _admitted_native_fixture(nodes)
    evidence = _completed_native_receipt(first, args)
    first.record_native_delivery(six.store, args["event_key"], evidence)
    six.store.finish_reply(args["event_key"], state_value="delivered", delivery_status="enqueued")
    with sqlite3.connect(nodes[0] / "queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES (?,?,?)",
                   (evidence["queue_message_id"], first.thread_id, "{}"))
    queue_before = (nodes[0] / "queue_1.sqlite").read_bytes()
    before = _rows(six.store.journal.database)
    resumed, current = _node(nodes, LOGIN6, activate=False, clock=200, instance="dog-restarted")
    assert resumed.token["epoch"] > first.token["epoch"]
    assert _admit(resumed, nodes[0])
    assert resumed.expected["relay_epoch"] == first.expected["relay_epoch"] + 1
    assert _rows(current.store.journal.database) == before
    assert (nodes[0] / "queue_1.sqlite").read_bytes() == queue_before
    resumed.reconcile_completions()
    assert not resumed.handoff_safe()  # Completion cannot remove a queued row.
    assert current.store.lookup_reply(args["event_key"]).get("native_completed") is not True
    with pytest.raises(cs.ControlBusy, match="stale_epoch"):
        first.write_cursor("slack", "old-instance", {"advance": True})


@pytest.mark.parametrize("changed", ["boot", "executor"])
def test_boot_or_executor_change_cannot_recover_an_unresolved_relay_as_same_owner(nodes, changed):
    first, harness, args = _admitted_native_fixture(nodes)
    harness.store.finish_reply(args["event_key"], state_value="delivered", delivery_status="enqueued")
    if changed == "executor":
        _park(nodes, first)
    before = first.observation.path.read_bytes()
    replacement, _ = _node(nodes, LOGIN3 if changed == "executor" else LOGIN6,
        activate=False, clock=200, instance="dog-different",
        boot="boot-different" if changed == "boot" else None)
    assert not _admit(replacement, nodes[0])
    assert replacement.observation.reason == "control_node_relay_pending"
    assert first.observation.path.read_bytes() == before
    assert replacement.expected is None


@pytest.mark.parametrize("linked", ["authority", "sessions"])
def test_enrollment_rejects_symlinked_authority_paths_before_writing(nodes, linked):
    authority, _ = _node(nodes, activate=False)
    with authority.observation.guard(rollout_stamp=_stamp(nodes[0])) as admitted:
        assert admitted
    authority.manifest.unlink()
    escaped = nodes[0].parent / "unrelated-directory"
    escaped.mkdir()
    try:
        if linked == "authority":
            (escaped / "cluster.json").write_bytes((authority.root / "cluster.json").read_bytes())
            authority.root.rename(authority.root.with_name("saved-relay-authority"))
            authority.root.symlink_to(escaped, target_is_directory=True)
        else:
            authority.manifest.parent.rmdir()
            authority.manifest.parent.symlink_to(escaped, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows directory symlink privilege is unavailable (WinError 1314)")
        raise
    before = {str(path.relative_to(escaped)): path.read_bytes()
              for path in escaped.rglob("*") if path.is_file()}
    with pytest.raises(cs.ControlError, match="authority_path_invalid"):
        authority.activate()
    after = {str(path.relative_to(escaped)): path.read_bytes()
             for path in escaped.rglob("*") if path.is_file()}
    assert after == before


def _event_rows(authority, journal, start, stop, *, active):
    for number in range(start, stop):
        key = ("{:064x}".format(number) if active else "history-{:064x}".format(number))
        body = dict(instruction_id="slackbot:stress-" + str(number),
                    text_sha256=sha256_text("bounded fixture"),
                    state="uncertain" if active else "delivered",
                    delivery_status="uncertain" if active else "completed",
                    native_completed=not active)
        yield (journal.namespace, "events", key, json.dumps(body), None,
               authority.thread_id, int(active), "2026-10-07T16:00:00Z")


def _grow_history(authority, provider, start, stop, completed):
    journal = authority.journal(provider)
    with journal.transaction() as db:
        db.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)",
                       _event_rows(authority, journal, start, stop, active=True))
        db.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)",
                       _event_rows(authority, journal, 0, completed, active=False))
    return journal


def _observe_pending_queries(monkeypatch):
    queries = []
    connect = sqlite3.connect
    class Cursor(sqlite3.Cursor):
        def execute(self, statement, parameters=()):
            self.observed = None
            if statement.startswith("SELECT namespace,key,value FROM records INDEXED BY session_relay_pending"):
                plan = self.connection.cursor().execute("EXPLAIN QUERY PLAN " + statement, parameters).fetchall()
                self.observed = dict(sql=statement, parameters=parameters, plan=plan, steps=0)
                queries.append(self.observed)
                def counted():
                    self.observed["steps"] += 1
                    return 0
                self.connection.set_progress_handler(counted, 1)
            return super().execute(statement, parameters)
        def fetchall(self):
            try:
                rows = super().fetchall()
                if self.observed is not None:
                    self.observed["rows"] = len(rows)
                return rows
            finally:
                if self.observed is not None:
                    self.connection.set_progress_handler(None, 0)
    class Connection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            return self.cursor(factory=Cursor).execute(statement, parameters)
    def observed_connect(*args, **kwargs):
        kwargs["factory"] = Connection
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    return queries


def _bounded_query_plans(queries):
    assert 1 <= len(queries) <= 2
    assert sum(query["rows"] for query in queries) <= 4
    for query in queries:
        description = " ".join(row[3] for row in query["plan"])
        assert "SEARCH records USING INDEX session_relay_pending" in description
        assert "namespace,key" in description.replace(" ", "")
        assert "SCAN" not in description and "TEMP B-TREE" not in description
        assert query["steps"] <= 256


@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_reconcile_index_seeks_and_operation_counts_remain_bounded_as_history_grows(nodes, monkeypatch, provider):
    authority, _ = _node(nodes)
    journal = _grow_history(authority, provider, 0, 16, 0)
    queries = _observe_pending_queries(monkeypatch)
    authority.reconcile_completions()
    _bounded_query_plans(queries)
    baseline_steps = sum(query["steps"] for query in queries)
    queries.clear()
    _grow_history(authority, provider, 16, 10000, 10000)
    authority.reconcile_completions()
    _bounded_query_plans(queries)
    assert sum(query["steps"] for query in queries) <= baseline_steps + 64
    with journal.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='events' AND active=1").fetchone() == (10000,)
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='events' AND active=0").fetchone() == (10000,)


@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
def test_reconcile_rotates_over_missing_and_malformed_proofs_without_history_scan(nodes, monkeypatch, provider):
    authority, _ = _node(nodes)
    journal = _grow_history(authority, provider, 0, 10000, 10000)
    keys = ["{:064x}".format(number) for number in range(3)]
    instruction = "slackbot:stress-2"
    evidence = _completed_native_receipt(authority,
        dict(instruction_id=instruction, text="bounded fixture"))
    progress, _ = completion_progress(evidence, budget=0)
    with journal.transaction() as db:
        # Start near the tail: one missing proof, then a bounded wrap across a
        # malformed proof and wrong binding before reaching a healthy receipt.
        journal.put(db, "relay_reconcile_cursor", authority.thread_id,
                    dict(after=[journal.namespace, "{:064x}".format(9998)], thread_id=authority.thread_id))
        journal.put(db, "relay_native_receipts", keys[0], dict(receipt=dict(
            thread_id=authority.thread_id, instruction_id="slackbot:stress-0",
            prompt_sha256=sha256_text("bounded fixture"))))
        journal.put(db, "relay_native_receipts", keys[1], dict(receipt=dict(
            thread_id=authority.thread_id, instruction_id="slackbot:different",
            prompt_sha256=sha256_text("bounded fixture"))))
        journal.put(db, "relay_native_receipts", keys[2],
                    dict(thread_id=authority.thread_id, receipt=evidence, progress=progress))
        historic = journal.get(db, "events", "history-" + "{:064x}".format(9999))
    queries = _observe_pending_queries(monkeypatch)
    authority.reconcile_completions()
    _bounded_query_plans(queries)
    assert [query["rows"] for query in queries] == [1, 3]
    with journal.transaction() as db:
        assert journal.get(db, "events", keys[2])["native_completed"] is True
        assert journal.get(db, "events", keys[0]).get("native_completed") is False
        assert journal.get(db, "events", keys[1]).get("native_completed") is False
        assert journal.get(db, "events", "history-" + "{:064x}".format(9999)) == historic
        assert journal.get(db, "relay_reconcile_cursor", authority.thread_id)["after"] == [journal.namespace, keys[2]]
    queries.clear()
    authority.reconcile_completions()
    _bounded_query_plans(queries)
    with journal.transaction() as db:
        assert journal.get(db, "relay_reconcile_cursor", authority.thread_id)["after"] == [
            journal.namespace, "{:064x}".format(6)]


@pytest.mark.parametrize("provider", ["slack", "lark", "onebot"])
@pytest.mark.parametrize("version", [1, 2])
def test_legacy_json_without_provider_database_cannot_take_fresh_authority_shortcut(nodes, provider, version):
    authority, _ = _node(nodes, activate=False)
    (authority.root / "cluster.json").unlink()
    authority.manifest.unlink()
    runtime = authority.store.directory.parent.parent / "runtime"
    directory = runtime / provider
    if provider != "slack":
        directory /= sha256_text(provider + "-fixture")
    directory.mkdir(parents=True)
    source = directory / "relay-state.json"
    content = (dict(schema_version=1, threads={}, events={"original-effect": dict(state="uncertain")})
               if version == 1 else dict(schema_version=2, storage="reply-tickets.sqlite3",
                    active_scope="provider_session", active_limit=4))
    source.write_text(json.dumps(content))
    before = source.read_bytes()
    assert not (runtime / provider / "reply-tickets.sqlite3").exists()
    with pytest.raises(cs.ControlError, match="relay_migration_legacy_or_missing_journal"):
        initialize_empty_authority(nodes[0])
    assert source.read_bytes() == before
    assert not (authority.root / "cluster.json").exists()
    assert not authority.runtime.exists()


def test_missing_migration_retries_reject_existing_database_without_history_reads(nodes, monkeypatch):
    from codex_watchdog import linux_relay_migration as migration
    authority, _ = _node(nodes, activate=False)
    (authority.root / "cluster.json").unlink()
    authority.manifest.unlink()
    runtime = authority.store.directory.parent.parent / "runtime"
    legacy = SlackPollingThreadStore(runtime)
    legacy.record_thread(CHANNEL, "1791389141.470039", _target(authority), "a" * 64)
    assert legacy.journal.database.exists()
    monkeypatch.setattr(migration, "read_snapshot",
        lambda *args, **kwargs: pytest.fail("missing-migration startup read historical journal rows"))
    for _ in range(3):  # Same bounded refusal on repeated automatic cycles.
        with pytest.raises(cs.ControlError, match="migration_required"):
            migration.initialize_empty_authority(nodes[0])
    assert not (authority.root / "cluster.json").exists()
