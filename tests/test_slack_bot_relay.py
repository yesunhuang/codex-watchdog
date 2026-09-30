"""Real Slack socket/poll admission with synthetic authenticated principals."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_watchdog.models import sha256_text
from codex_watchdog.relay import RelayTarget
from codex_watchdog.slack_poll import SlackReplyPoller
from codex_watchdog.slack_relay import SlackReplyRelay


TEAM, OTHER_TEAM = "T12345678", "T87654321"
CHANNEL, OTHER_CHANNEL = "C12345678", "C87654321"
ADMIN, DELEGATE = "U12345678", "U23456789"
BOT_USER, BOT_ID, BOT_APP = "U34567890", "B34567890", "A34567890"
OWN_USER, OWN_BOT, OWN_APP = "U45678901", "B45678901", "A45678901"
REQUEST = "a1111111-2222-4333-8444-555555555555"
PROMPT = "Continue the fixture task"
TARGET = RelayTarget("fixture-workspace", "11111111-2222-4333-8444-555555555555", "process_local")
OTHER_TARGET = replace(TARGET, thread_id="22222222-2222-4333-8444-555555555555")
REMOTE = replace(TARGET, execution_locality="remote_ssh", remote_authority="ssh-remote+fixture",
                 remote_repo_path="/srv/fixture", remote_storage_key="a" * 32)


class Metadata:
    def __init__(self):
        self.calls, self.fail_once = [], None
        self.users = {user: dict(id=user, team_id=TEAM, deleted=False, is_bot=False,
                                is_app_user=False, profile={}) for user in (ADMIN, DELEGATE)}
        self.bots = {}
        for user, bot, app in ((BOT_USER, BOT_ID, BOT_APP), (OWN_USER, OWN_BOT, OWN_APP)):
            self.users[user] = dict(id=user, team_id=TEAM, deleted=False, is_bot=True,
                is_app_user=False, profile=dict(bot_id=bot, api_app_id=app, team=TEAM))
            self.bots[bot] = dict(id=bot, user_id=user, app_id=app, team_id=TEAM, deleted=False)

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        if self.fail_once is not None:
            error, self.fail_once = self.fail_once, None
            raise error
        if method == "auth.test":
            return dict(ok=True, team_id=TEAM, user_id=OWN_USER, bot_id=OWN_BOT, app_id=OWN_APP)
        if method == "users.info":
            return dict(ok=True, user=deepcopy(self.users[params["user"]]))
        if method == "bots.info":
            return dict(ok=True, bot=deepcopy(self.bots[params["bot"]]))
        raise AssertionError("unexpected metadata method: " + method)


class Rig:
    def __init__(self, path, mode):
        self.path, self.mode = path, mode
        self.api = Metadata()
        self.calls, self.remote_calls, self.acks, self.sent = [], [], [], []
        self.history = {}
        self.parents = self.messages = 0
        self.admins = (ADMIN,)
        self.outcome = "enqueued"
        self.restart()

    def restart(self):
        def dispatch(*args):
            self.calls.append(args)
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return SimpleNamespace(status=self.outcome)

        def probe(*args, **kwargs):
            self.remote_calls.append((args, kwargs))
            return dict(status="ok", wake=dict(state="enqueued"))

        self.relay = SlackReplyRelay(self.path, bot_token="xoxb-synthetic", app_token="xapp-synthetic",
            channel_id=CHANNEL, allowed_user_ids=self.admins, reply_mode=self.mode,
            queue_dispatcher=SimpleNamespace(dispatch=dispatch),
            remote_ssh_adapter=SimpleNamespace(probe=probe), bot_api=self.api,
            access_api=self.api, access_sender=self.send)
        self.controlled = Mock(wraps=self.relay._controlled_reply)
        self.dispatch = Mock(wraps=self.relay._dispatch)
        self.relay._controlled_reply, self.relay._dispatch = self.controlled, self.dispatch
        self.poller = SlackReplyPoller(self.relay, api=self.provider)

    @property
    def store(self):
        return self.relay.thread_store

    def send(self, text, operation_id, destination):
        address = f"1789111750.{len(self.sent) + 1:06d}"
        self.sent.append(dict(text=text, operation_id=operation_id, channel=destination, ts=address))
        return dict(chat_id=destination, message_id=address)

    def provider(self, method, params):
        if method == "conversations.replies":
            return dict(ok=True, messages=deepcopy(self.history.get((params["channel"], params["ts"]), [])))
        assert method == "chat.postMessage"
        self.acks.append(dict(params))
        return dict(ok=True)

    def record(self, target=TARGET, channel=CHANNEL):
        self.parents += 1
        parent = f"1789111700.{self.parents:06d}"
        self.store.record_thread(channel, parent, target, sha256_text("notification:" + parent))
        return parent

    def event(self, parent, *, text=None, user=BOT_USER, bot=True, channel=CHANNEL):
        self.messages += 1
        event = dict(type="message", channel=channel, thread_ts=parent,
            ts=f"1789111800.{self.messages:06d}", user=user,
            text=f"!codex {REQUEST}\n{PROMPT}" if text is None else text,
            client_msg_id=f"fixture-client-{self.messages}")
        if bot:
            event.update(subtype="bot_message", bot_id=BOT_ID, app_id=BOT_APP, team=TEAM,
                bot_profile=dict(id=BOT_ID, app_id=BOT_APP, team_id=TEAM))
        return event

    def socket(self, event, *, body=None):
        envelope = dict(team_id=TEAM, api_app_id=OWN_APP, event_id="Ev" + event["client_msg_id"])
        if body is not None:
            envelope.update(body)
        result = self.relay._handle_bolt_message(event, envelope,
            SimpleNamespace(chat_postMessage=lambda **params: self.acks.append(params)))
        return result.to_dict()

    def deliver(self, event, *, body=None):
        if self.mode == "socket":
            return [self.socket(event, body=body)]
        self.history[(event["channel"], event["thread_ts"])] = [deepcopy(event)]
        # Poll each active mapped parent at most once; no listener is started.
        for _ in range(4):
            results = self.poller.poll_once()
            if results:
                return results
        return []

    def control(self, operation="add", *, source=None, target=TARGET, user=ADMIN, via_socket=False):
        source = source or self.record(target)
        text = "bot access" if operation == "access" else f"bot {operation} <@{BOT_USER}>"
        event = self.event(source, text=text, user=user, bot=False)
        result = [self.socket(event)] if via_socket else self.deliver(event)
        return event, result

    def grant(self, target=TARGET):
        _, result = self.control(target=target)
        assert result[0]["status"] == "bot_access_applied", result
        assert result[0]["delivery_status"] == "added"
        source = self.sent[-1]["ts"]
        assert self.store.lookup_thread(CHANNEL, source).target == target
        return source

    def state(self, source, channel=CHANNEL):
        journal = self.store.journal
        key = self.store.thread_key(channel, source)
        with journal.transaction() as db:
            active = journal.active(db)
            assert len(active) <= 4
            return dict(entry=journal.get(db, "threads", key), active=key in active,
                receipts=db.execute("SELECT kind,key,value FROM records WHERE namespace=? "
                    "AND kind IN ('events','messages') ORDER BY kind,key", (journal.namespace,)).fetchall(),
                calls=len(self.calls), remote=len(self.remote_calls), controlled=self.controlled.call_count,
                dispatch=self.dispatch.call_count, acks=len(self.acks))

    def assert_inert(self, source, before, *, channel=CHANNEL, ack=True):
        after = self.state(source, channel)
        if not ack:
            before, after = dict(before), dict(after)
            before.pop("acks"); after.pop("acks")
        assert after == before
        assert after["active"]


@pytest.fixture(params=("socket", "poll"))
def rig(request, tmp_path):
    return Rig(tmp_path, request.param)


def plain_instruction_blocks(text):
    return [dict(type="rich_text", block_id="fixture-command", elements=[
        dict(type="rich_text_section", elements=[dict(type="text", text=text)])])]


def instruction_event(rig, source, *, inline=False, user=BOT_USER):
    event = rig.event(source, user=user)
    if inline:
        event["text"] = f"!codex {REQUEST} -- {PROMPT}"
        event["blocks"] = plain_instruction_blocks(event["text"])
    return event


def journal_rows(rig):
    with rig.store.journal.transaction() as db:
        return db.execute("SELECT namespace,kind,key,value,active FROM records "
                          "ORDER BY namespace,kind,key").fetchall()


@pytest.mark.parametrize("inline", (False, True), ids=("legacy", "inline"))
def test_grant_explicit_instruction_exactly_once_and_no_bot_ack(rig, inline):
    source = rig.grant()
    event = instruction_event(rig, source, inline=inline)
    before_ack = len(rig.acks)
    result = rig.deliver(event)
    assert result[0]["status"] == "bot_queued", result
    assert rig.calls[0][0] == TARGET.thread_id and rig.calls[0][2] == PROMPT
    assert not rig.state(source)["active"]
    rig.restart()
    rig.deliver(event, body=dict(event_id="Ev-changed-delivery"))
    assert len(rig.calls) == 1 and len(rig.acks) == before_ack


def test_inline_instruction_without_blocks_queues_only_its_plain_body(rig):
    source = rig.grant()
    event = instruction_event(rig, source, inline=True)
    event.pop("blocks")
    assert rig.deliver(event)[0]["status"] == "bot_queued"
    assert len(rig.calls) == 1
    assert rig.calls[0][0] == TARGET.thread_id and rig.calls[0][2] == PROMPT
    assert rig.acks == rig.remote_calls == []


@pytest.mark.parametrize("inline_first", (False, True))
def test_inline_and_legacy_headers_share_logical_request_reservation(rig, inline_first):
    source = rig.grant()
    assert rig.deliver(instruction_event(rig, source, inline=inline_first))[0]["status"] == "bot_queued"
    rig.restart()
    second = rig.record()
    before, rows = rig.state(second), journal_rows(rig)
    rig.deliver(instruction_event(rig, second, inline=not inline_first))
    rig.assert_inert(second, before)
    assert journal_rows(rig) == rows and len(rig.calls) == 1


@pytest.mark.parametrize("attempt", ("soft-line", "explicit-block-line", "paragraph"))
def test_observed_flattened_newline_shapes_remain_unadmitted(rig, attempt):
    source = rig.grant()
    line_count = 2 if attempt == "paragraph" else 1
    event = rig.event(source, text=f"!codex {REQUEST}" + " " * line_count + PROMPT)
    event["blocks"] = plain_instruction_blocks(f"!codex {REQUEST}" + "\n" * line_count + PROMPT)
    event["blocks"][0]["block_id"] = "fixture-" + attempt
    before, rows, api_calls = rig.state(source), journal_rows(rig), len(rig.api.calls)
    result = rig.deliver(event)
    assert result[0]["status"] == "ignored_bot_or_subtype"
    assert result[0]["delivery_status"] == "bot_not_instruction"
    rig.assert_inert(source, before)
    assert journal_rows(rig) == rows and len(rig.api.calls) == api_calls


@pytest.mark.parametrize("form", ("styled", "quoted", "block-mismatch", "multiline-body"))
def test_inline_instruction_does_not_relax_visible_plain_content(rig, form):
    source = rig.grant()
    event = instruction_event(rig, source, inline=True)
    section = event["blocks"][0]["elements"][0]
    if form == "styled":
        section["elements"][0]["style"] = {"bold": True}
    elif form == "quoted":
        section["type"] = "rich_text_quote"
    elif form == "block-mismatch":
        section["elements"][0]["text"] += " changed visible instruction"
    else:
        event["text"] += "\nAnother line"
        event["blocks"] = plain_instruction_blocks(event["text"])
    before, rows = rig.state(source), journal_rows(rig)
    rig.deliver(event)
    rig.assert_inert(source, before)
    assert journal_rows(rig) == rows


@pytest.mark.parametrize("prompt", ("bot access", f"!codex {REQUEST} -- Again",
                                    "WD-BIND-fixture", "> quoted instruction"))
def test_inline_instruction_keeps_reserved_and_loop_content_inert(rig, prompt):
    source = rig.grant()
    event = rig.event(source, text=f"!codex {REQUEST} -- {prompt}")
    event["blocks"] = plain_instruction_blocks(event["text"])
    before, rows = rig.state(source), journal_rows(rig)
    rig.deliver(event)
    rig.assert_inert(source, before)
    assert journal_rows(rig) == rows


def test_modern_bot_shape_without_subtype_uses_verified_sender_app(rig):
    source = rig.grant()
    event = rig.event(source)
    event.pop("subtype")
    assert BOT_APP != OWN_APP
    assert rig.deliver(event)[0]["status"] == "bot_queued"
    assert len(rig.calls) == 1


@pytest.mark.parametrize("state", ("absent", "revoked", "other-session", "bot-on-admin-list"))
@pytest.mark.parametrize("inline", (False, True), ids=("legacy", "inline"))
def test_grant_is_required_current_and_exact_session(rig, state, inline):
    if state == "absent" or state == "bot-on-admin-list":
        source = rig.record()
    else:
        source = rig.grant()
        if state == "revoked":
            _, result = rig.control("remove")
            assert result[0]["delivery_status"] == "removed"
            rig.restart()
        else:
            source = rig.record(OTHER_TARGET)
    if state == "bot-on-admin-list":
        rig.admins = (ADMIN, BOT_USER)
        rig.restart()
    before = rig.state(source)
    rig.deliver(instruction_event(rig, source, inline=inline))
    rig.assert_inert(source, before)


def test_known_revoked_bot_without_markers_cannot_become_human_admin(rig):
    source = rig.grant()
    rig.control("remove")
    rig.admins = (ADMIN, BOT_USER)
    rig.restart()
    before = rig.state(source)
    result = rig.deliver(rig.event(source, bot=False, text="Continue as a human"))
    assert result[0]["status"] == "bot_ignored"
    rig.assert_inert(source, before)


def test_app_only_sender_marker_cannot_use_human_admin_lane_before_any_grant(rig):
    rig.admins = (ADMIN, BOT_USER)
    rig.restart()
    source = rig.record()
    event = rig.event(source, bot=False, text="Unwrapped fixture instruction")
    event["api_app_id"] = BOT_APP
    before = rig.state(source)
    rig.deliver(event)
    rig.assert_inert(source, before)
    assert rig.sent == []


@pytest.mark.parametrize("field,value", (("team_id", OTHER_TEAM), ("team", OTHER_TEAM),
                                         ("team_id", None), ("team_id", True)))
def test_human_bot_control_rejects_every_conflicting_inner_team_field(rig, field, value):
    source = rig.record()
    event = rig.event(source, bot=False, user=ADMIN, text=f"bot add <@{BOT_USER}>")
    event[field] = value
    before = rig.state(source)
    rig.deliver(event)
    rig.assert_inert(source, before)
    assert rig.sent == []
    assert rig.relay._get_bot_access().principals(TARGET.thread_id) == []


@pytest.mark.parametrize("form", ("quote", "preformatted", "attachments", "forwarded",
                                  "is_forwarded", "edited", "deleted"))
def test_quoted_forwarded_and_modified_human_bot_controls_cannot_grant(rig, form):
    source = rig.record()
    event = rig.event(source, bot=False, user=ADMIN, text=f"bot add <@{BOT_USER}>")
    if form in ("quote", "preformatted"):
        event["blocks"] = [dict(type="rich_text", elements=[dict(type="rich_text_" + form,
            elements=[dict(type="text", text=event["text"])])])]
    elif form == "attachments":
        event[form] = [dict(text=event["text"])]
    elif form in ("forwarded", "is_forwarded"):
        event[form] = True
    else:
        event[form] = dict(user=ADMIN, ts=event["ts"])
    before = rig.state(source)
    rig.deliver(event)
    rig.assert_inert(source, before)
    assert rig.sent == []
    assert rig.relay._get_bot_access().principals(TARGET.thread_id) == []


def test_plain_rich_text_human_control_with_native_user_mention_grants(rig):
    source = rig.record()
    event = rig.event(source, bot=False, user=ADMIN, text=f"bot add <@{BOT_USER}>")
    event.update(team=TEAM, team_id=TEAM, blocks=[dict(type="rich_text", block_id="fixture-block",
        elements=[dict(type="rich_text_section", elements=[
            dict(type="text", text="bot add "), dict(type="user", user_id=BOT_USER)])])])
    result = rig.deliver(event)
    assert (result[0]["status"], result[0]["delivery_status"]) == ("bot_access_applied", "added")
    assert len(rig.sent) == 1 and rig.calls == []
    assert rig.relay._get_bot_access().principals(TARGET.thread_id)[0].user_id == BOT_USER


def native_human_bot_control(rig, source):
    """Sanitized native Slack event shape; every identity remains synthetic."""
    event = rig.event(source, bot=False, user=ADMIN, text=f"bot add <@{BOT_USER}>")
    event.update(team=TEAM, parent_user_id=OWN_USER, blocks=[
        dict(type="rich_text", block_id="fixture-native-block", elements=[
            dict(type="rich_text_section", elements=[
                dict(type="text", text="bot add "),
                dict(type="user", user_id=BOT_USER, from_llm=False),
            ]),
        ]),
    ])
    return event


def test_native_human_mention_metadata_grants_with_one_mapped_confirmation(rig):
    source = rig.record()
    event = native_human_bot_control(rig, source)
    result = rig.deliver(event)
    assert (result[0]["status"], result[0]["delivery_status"]) == ("bot_access_applied", "added")
    assert ("auth.test", {}) in rig.api.calls
    assert ("users.info", {"user": ADMIN}) in rig.api.calls
    assert ("users.info", {"user": BOT_USER}) in rig.api.calls
    assert ("bots.info", {"bot": BOT_ID}) in rig.api.calls
    principals = rig.relay._get_bot_access().principals(TARGET.thread_id)
    assert [principal.to_dict() for principal in principals] == [dict(
        team_id=TEAM, user_id=BOT_USER, bot_id=BOT_ID, app_id=BOT_APP)]
    assert len(rig.sent) == 1 and rig.calls == rig.remote_calls == rig.acks == []
    confirmation = rig.sent[0]["ts"]
    assert confirmation != source and rig.store.lookup_thread(CHANNEL, confirmation).target == TARGET
    assert not rig.state(source)["active"] and rig.state(confirmation)["active"]
    rig.restart()
    rig.deliver(event, body=dict(event_id="Ev-native-control-redelivery"))
    assert len(rig.sent) == 1 and rig.calls == rig.remote_calls == rig.acks == []
    assert rig.relay._get_bot_access().principals(TARGET.thread_id) == principals
    assert rig.state(confirmation)["active"]


@pytest.mark.parametrize("change", (
    "mismatched-mention", "quote", "unknown-mention-field", "unknown-text-field",
    "unknown-section-field", "from-llm-true", "from-llm-zero", "from-llm-null",
    "from-llm-string", "from-llm-object",
))
def test_native_human_mention_metadata_does_not_relax_content_checks(rig, change):
    source = rig.record()
    event = native_human_bot_control(rig, source)
    section = event["blocks"][0]["elements"][0]
    mention = section["elements"][1]
    if change == "mismatched-mention":
        mention["user_id"] = DELEGATE
    elif change == "quote":
        section["type"] = "rich_text_quote"
    elif change == "unknown-mention-field":
        mention["unknown_metadata"] = False
    elif change == "unknown-text-field":
        section["elements"][0]["from_llm"] = False
    elif change == "unknown-section-field":
        section["from_llm"] = False
    else:
        mention["from_llm"] = {"from-llm-true": True, "from-llm-zero": 0,
            "from-llm-null": None, "from-llm-string": "false", "from-llm-object": {}}[change]
    before = rig.state(source)
    result = rig.deliver(event)
    assert result and result[0]["status"] != "bot_access_applied"
    rig.assert_inert(source, before)
    assert rig.sent == []
    assert rig.relay._get_bot_access().principals(TARGET.thread_id) == []


@pytest.mark.parametrize("change", ("bot", "app", "team", "profile-bot", "profile-app",
    "profile-team", "profile-type", "missing-user", "missing-bot", "edited", "deleted", "subtype"))
def test_mismatched_or_modified_bot_envelope_is_inert(rig, change):
    source = rig.grant()
    event = rig.event(source)
    if change == "bot": event["bot_id"] = "B99999999"
    elif change == "app": event["app_id"] = "A99999999"
    elif change == "team": event["team"] = OTHER_TEAM
    elif change == "profile-bot": event["bot_profile"]["id"] = "B99999999"
    elif change == "profile-app": event["bot_profile"]["app_id"] = "A99999999"
    elif change == "profile-team": event["bot_profile"]["team_id"] = OTHER_TEAM
    elif change == "profile-type": event["bot_profile"] = []
    elif change == "missing-user": event.pop("user")
    elif change == "missing-bot": event.pop("bot_id")
    elif change == "subtype": event["subtype"] = "message_changed"
    else: event[change] = {"ts": event["ts"]}
    before = rig.state(source)
    rig.deliver(event)
    rig.assert_inert(source, before)


@pytest.mark.parametrize("text", (
    "Ordinary bot chatter", f"> !codex {REQUEST}\n{PROMPT}",
    f"```\n!codex {REQUEST}\n{PROMPT}\n```", f"Report:\n!codex {REQUEST}\n{PROMPT}",
    f"!codex {REQUEST.upper()}\n{PROMPT}", f"!codex {REQUEST}\n",
    f"!codex {REQUEST}\nbot add <@{ADMIN}>", f"!codex {REQUEST}\nadd <@{ADMIN}>",
    f"!codex {REQUEST}\nremove <@{ADMIN}>", f"!codex {REQUEST}\naccess",
    f"!codex {REQUEST}\nbind <#{OTHER_CHANNEL}>", f"!codex {REQUEST}\nunbind",
    f"!codex {REQUEST}\nWD-BIND-fixture", "WatchDog host: fixture\nQueued for the exact existing Codex thread.",
))
def test_chatter_quotations_reports_and_admin_bodies_never_dispatch(rig, text):
    source = rig.grant()
    before = rig.state(source)
    rig.deliver(rig.event(source, text=text))
    rig.assert_inert(source, before)


@pytest.mark.parametrize("inline", (False, True), ids=("legacy", "inline"))
def test_watchdog_self_output_is_inert_even_with_command_text(rig, inline):
    source = rig.grant()
    event = instruction_event(rig, source, user=OWN_USER, inline=inline)
    event.update(bot_id=OWN_BOT, app_id=OWN_APP,
                 bot_profile=dict(id=OWN_BOT, app_id=OWN_APP, team_id=TEAM))
    before = rig.state(source)
    rig.deliver(event)
    rig.assert_inert(source, before)


@pytest.mark.parametrize("owner", ("nonadmin", "bot", "wrong-team", "deleted"))
def test_only_authenticated_same_team_human_owner_can_grant(rig, owner):
    source = rig.record()
    user = DELEGATE if owner == "nonadmin" else ADMIN
    if owner == "bot": rig.api.users[ADMIN]["is_bot"] = True
    if owner == "wrong-team": rig.api.users[ADMIN]["team_id"] = OTHER_TEAM
    if owner == "deleted": rig.api.users[ADMIN]["deleted"] = True
    before = rig.state(source)
    rig.control(source=source, user=user)
    rig.assert_inert(source, before, ack=False)
    assert rig.sent == []


def test_bot_cannot_grant_onward_even_when_on_human_admin_list(rig):
    source = rig.grant()
    rig.admins = (ADMIN, BOT_USER)
    rig.restart()
    before = rig.state(source)
    sent = len(rig.sent)
    rig.deliver(rig.event(source, text=f"bot add <@{DELEGATE}>"))
    rig.assert_inert(source, before)
    assert len(rig.sent) == sent


def test_metadata_timeout_defers_without_receipt_ack_or_poll_cursor_advance(rig):
    source = rig.grant()
    rig.restart()
    event = rig.event(source)
    before = rig.state(source)
    rig.api.fail_once = TimeoutError("synthetic timeout, never expose provider detail")
    result = rig.deliver(event)
    assert result[0]["status"] == "deferred"
    rig.assert_inert(source, before)
    if rig.mode == "poll":
        cursors = json.loads(rig.poller.path.read_text())["threads"]
        assert rig.store.thread_key(CHANNEL, source) not in cursors
    assert rig.deliver(event)[0]["status"] == "bot_queued"
    assert len(rig.calls) == 1


@pytest.mark.parametrize("uncertain", (False, True))
@pytest.mark.parametrize("inline", (False, True), ids=("legacy", "inline"))
def test_logical_request_cannot_move_to_new_message_or_parent_after_restart(rig, uncertain, inline):
    source = rig.grant()
    if uncertain:
        rig.outcome = TimeoutError("synthetic queue uncertainty")
    first = instruction_event(rig, source, inline=inline)
    result = rig.deliver(first)
    assert result[0]["status"] == ("bot_uncertain" if uncertain else "bot_queued")
    assert not rig.state(source)["active"]
    rig.restart()
    rig.deliver(first, body=dict(event_id="Ev-retry-after-restart"))
    second = rig.record()
    before = rig.state(second)
    rig.deliver(instruction_event(rig, second, inline=inline))
    rig.assert_inert(second, before)
    assert len(rig.calls) == 1


@pytest.mark.parametrize("flow", ("ordinary", "access", "bot-control", "unbind"))
@pytest.mark.parametrize("bot_first", (False, True))
def test_bot_and_other_flows_share_physical_message_collision_guards(rig, flow, bot_first):
    source = rig.grant()
    other = rig.record()
    text = {"ordinary": PROMPT, "access": "access", "bot-control": "bot access", "unbind": "unbind"}[flow]
    first = rig.event(source, bot=bot_first, user=BOT_USER if bot_first else ADMIN,
                      text=None if bot_first else text)
    initial = rig.deliver(first)
    assert initial and initial[0]["status"] in (
        "bot_queued", "queued", "access_applied", "bot_access_applied", "route_unbound"), initial
    second = rig.event(other, bot=not bot_first, user=ADMIN if bot_first else BOT_USER,
                       text=text if bot_first else None)
    second["ts"] = first["ts"]
    before = rig.state(other)
    rig.deliver(second, body=dict(event_id="Ev-cross-flow-redelivery"))
    rig.assert_inert(other, before, ack=not bot_first)


def test_bot_control_confirmation_is_once_and_new_exact_mapping(rig):
    source = rig.record()
    event, result = rig.control(source=source)
    assert result[0]["status"] == "bot_access_applied"
    assert len(rig.sent) == 1
    confirmation = rig.sent[0]["ts"]
    assert confirmation != source and rig.store.lookup_thread(CHANNEL, confirmation).target == TARGET
    assert not rig.state(source)["active"] and rig.state(confirmation)["active"]
    rig.restart()
    rig.deliver(event, body=dict(event_id="Ev-new-delivery-for-grant"))
    assert len(rig.sent) == 1 and rig.calls == []
    _, result = rig.control("access", source=confirmation)
    assert result[0]["delivery_status"] == "access"


@pytest.mark.parametrize("change", ("revoke", "remote-authority", "remote-repo", "remote-storage"))
def test_current_grant_and_full_target_rechecked_inside_real_claim(rig, monkeypatch, change):
    source = rig.grant(REMOTE)
    event = rig.event(source)
    original_lookup, original_claim = rig.store.lookup_thread, rig.store.claim_reply
    captured, outcomes, expected = [], [], []

    def lookup(*args, **kwargs):
        mapping = original_lookup(*args, **kwargs)
        captured.append(mapping)
        return mapping

    def claim(**kwargs):
        if kwargs["text"] != PROMPT:
            return original_claim(**kwargs)  # The intervening real revoke claims its own ticket.
        assert captured[0].target == REMOTE
        if change == "revoke":
            _, result = rig.control("remove", target=REMOTE, via_socket=True)
            assert result[0]["delivery_status"] == "removed"
        else:
            field, value = {"remote-authority": ("remote_authority", "ssh-remote+changed"),
                "remote-repo": ("remote_repo_path", "/srv/changed"),
                "remote-storage": ("remote_storage_key", "b" * 32)}[change]
            journal = rig.store.journal
            with journal.transaction() as db:
                key = rig.store.thread_key(CHANNEL, source)
                entry = journal.get(db, "threads", key)
                journal.put(db, "threads", key, dict(entry, target=replace(REMOTE, **{field: value}).to_dict()))
        expected.append(rig.state(source))
        result = original_claim(**kwargs)
        outcomes.append(result)
        return result

    monkeypatch.setattr(rig.store, "lookup_thread", lookup)
    monkeypatch.setattr(rig.store, "claim_reply", claim)
    rig.deliver(event)
    assert len(outcomes) == 1 and outcomes[0][0] is False
    rig.assert_inert(source, expected[0])


@pytest.mark.parametrize("field", ("team_id", "api_app_id"))
@pytest.mark.parametrize("value", (None, "mismatch"))
def test_socket_outer_receiving_context_is_required(tmp_path, field, value):
    rig = Rig(tmp_path, "socket")
    source = rig.grant()
    before = rig.state(source)
    rig.deliver(rig.event(source), body={field: value})
    rig.assert_inert(source, before)


def test_socket_unmapped_channel_cannot_reuse_authorized_parent(tmp_path):
    rig = Rig(tmp_path, "socket")
    source = rig.grant()
    before = rig.state(source)
    rig.deliver(rig.event(source, channel=OTHER_CHANNEL))
    rig.assert_inert(source, before)


def test_poll_destination_comes_from_mapped_request_not_message_channel(tmp_path):
    rig = Rig(tmp_path, "poll")
    source = rig.grant()
    forged = rig.event(source, channel=OTHER_CHANNEL)
    rig.history[(CHANNEL, source)] = [forged]
    results = rig.poller.poll_once()
    assert results[0]["status"] == "bot_queued"
    assert rig.calls[0][0] == TARGET.thread_id and rig.acks == []


def test_poll_validates_entire_page_before_any_bot_claim(tmp_path):
    rig = Rig(tmp_path, "poll")
    source = rig.grant()
    before = rig.state(source)
    rig.history[(CHANNEL, source)] = [rig.event(source), rig.event("1789111000.000001")]
    with pytest.raises(ValueError, match="identity"):
        rig.poller.poll_once()
    rig.assert_inert(source, before)
