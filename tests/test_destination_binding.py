"""Controls are provider-authenticated, ticket-scoped, and never Codex prompts."""
from dataclasses import replace
import json
import time
from types import SimpleNamespace

import pytest

from codex_watchdog.lark_relay import LarkReplyRelay
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.onebot_relay import OneBotReplyRelay
from codex_watchdog.onebot_transport import OneBotConfig
from codex_watchdog.relay import RelayTarget

THREAD = "11111111-2222-4333-8444-555555555555"
OTHER = "22222222-2222-4333-8444-555555555555"


class Provider:
    def __init__(self, kind):
        self.kind, self.calls, self.error = kind, [], False

    def send(self, text, operation_id, *, destination=None, reply_to=None):
        self.calls.append(dict(text=text, operation=operation_id, destination=destination, reply_to=reply_to))
        if self.error:
            raise RuntimeError("synthetic timeout after send")
        mid = "om_sent%012d" % len(self.calls) if self.kind == "lark" else str(700 + len(self.calls))
        return dict(chat_id=destination, message_id=mid)


def setup(root, kind):
    api, queued, clock = Provider(kind), [], [time.time() + 5]
    queue = SimpleNamespace(dispatch=lambda *args: queued.append(args) or SimpleNamespace(status="enqueued"))
    if kind == "lark":
        config = LarkConfig("cli_fixture000001", "synthetic", "oc_default000001", ("ou_fixture000001",))
        relay = LarkReplyRelay(root, config, queue_dispatcher=queue, remote_ssh_adapter=None,
                              api=api, clock=lambda: clock[0])
        default, dest, user, parent = config.chat_id, "oc_destination001", config.allowed_user_ids[0], "om_parent0000001"
    else:
        config = OneBotConfig("ws://127.0.0.1:12345", "synthetic", "12345", "group", "67890", ("54321",))
        relay = OneBotReplyRelay(root, config, queue_dispatcher=queue, remote_ssh_adapter=None,
                                api=api, clock=lambda: clock[0])
        default, dest, user, parent = config.destination, "group:98765", "54321", "101"
    target = RelayTarget("workspace", THREAD, "process_local")
    record(relay, default, parent, target, "a" * 64)
    return SimpleNamespace(relay=relay, api=api, queued=queued, clock=clock, kind=kind,
                           default=default, dest=dest, user=user, parent=parent, target=target)


def record(relay, chat, parent, target, fp):
    relay.thread_store.prepare_notification(fp, fp)
    relay.thread_store.finish_notification(fp, chat, parent, target)


def event(env, text, *, chat=None, parent="default", number=1, user=None):
    chat = chat or env.default
    parent = env.parent if parent == "default" else parent
    user = user or env.user
    if env.kind == "lark":
        return dict(schema="2.0", header=dict(app_id=env.relay.config.app_id,
                    event_type="im.message.receive_v1", event_id="event-%012d" % number),
            event=dict(sender=dict(sender_type="user", sender_id=dict(open_id=user)),
                message=dict(chat_id=chat, message_id="om_reply%012d" % number,
                    root_id=parent, parent_id=parent, message_type="text",
                    create_time=str(int(env.clock[0] * 1000)), content=json.dumps(dict(text=text)))))
    kind, chat_id = chat.split(":")
    value = dict(post_type="message", message_type=kind, self_id=12345,
        time=int(env.clock[0]), user_id=int(user), sender=dict(user_id=int(user)), message_id=200+number,
        message=([dict(type="reply", data=dict(id=parent))] if parent else []) + [dict(type="text",data=dict(text=text))])
    if kind == "group": value["group_id"] = int(chat_id)
    return value


def begin(env):
    result = env.relay.handle_event(event(env, "bind"))
    assert result.status == "route_pending"
    assert not env.queued
    return env.api.calls[-1]["text"].splitlines()[-1]


def finish(env, token):
    env.clock[0] += 2
    return env.relay.handle_event(event(env, token, chat=env.dest, parent=None, number=2))


def test_onebot_confirmation_in_same_timestamp_second(tmp_path):
    env = setup(tmp_path, "onebot")
    env.clock[0] = int(env.clock[0]) + 0.75
    token = begin(env)
    # Provider events truncate milliseconds, including a quick confirmation.
    result = env.relay.handle_event(event(env, token, chat=env.dest, parent=None, number=2))
    assert result.status == "route_bound"
    assert env.relay.binding.store.routes.destination(THREAD) == env.dest
    assert not env.queued


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_bind_hello_reply_unbind_exact_session(tmp_path, kind):
    env = setup(tmp_path, kind)
    token = begin(env)
    assert env.relay.binding.store.routes.destination(THREAD) is None
    assert finish(env, token).status == "route_bound"
    assert env.relay.binding.store.routes.destination(THREAD) == env.dest
    assert env.relay.binding.store.routes.destination(OTHER) is None
    assert len(env.api.calls) == 2 and env.api.calls[-1]["destination"] == env.dest
    hello = "om_sent000000000002" if kind == "lark" else "702"
    env.clock[0] += 1
    wrong = "oc_another000001" if kind == "lark" else "group:88888"
    assert env.relay.handle_event(event(env, "hello", chat=wrong, parent=hello, number=3)).status == "ignored_chat"
    payload = event(env, "continue exactly", chat=env.dest, parent=hello, number=4)
    assert env.relay.handle_event(payload).status == "queued"
    assert env.relay.handle_event(payload).duplicate
    assert len(env.queued) == 1 and env.queued[0][0] == THREAD
    assert env.queued[0][2] == "continue exactly"
    # A closed hello cannot issue unbind or another command.
    assert env.relay.handle_event(event(env, "unbind", chat=env.dest, parent=hello, number=5)).status != "route_unbound"
    new_parent = "om_parent0000002" if kind == "lark" else "102"
    record(env.relay, env.dest, new_parent, env.target, "b" * 64)
    env.clock[0] += 1
    assert env.relay.handle_event(event(env, "unbind", chat=env.dest, parent=new_parent, number=6)).status == "route_unbound"
    assert env.relay.binding.store.routes.destination(THREAD) is None
    assert len(env.queued) == 1


@pytest.mark.parametrize("kind", ["lark", "onebot"])
@pytest.mark.parametrize("change", ["user", "bot", "modified", "expired", "reply", "missing_time"])
def test_challenge_rejections_never_queue_or_move(tmp_path, kind, change):
    env = setup(tmp_path, kind)
    token = begin(env)
    env.clock[0] += 2
    if change == "expired": env.clock[0] += 400
    payload = event(env, token + (" " if change == "modified" else ""), chat=env.dest,
                    parent=env.parent if change == "reply" else None, number=2,
                    user=("ou_intruder0001" if kind == "lark" else "99999") if change == "user" else None)
    if change == "bot":
        if kind == "lark": payload["header"]["app_id"] = "cli_otherbot0001"
        else: payload["self_id"] = 99999
    if change == "missing_time":
        if kind == "lark": payload["event"]["message"].pop("create_time")
        else: payload.pop("time")
    assert env.relay.handle_event(payload).status != "route_bound"
    assert env.relay.binding.store.routes.destination(THREAD) is None
    assert not env.queued and len(env.api.calls) == 1


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_uncertain_hello_not_retried_after_restart(tmp_path, kind):
    env = setup(tmp_path, kind)
    token = begin(env)
    env.api.error = True
    assert finish(env, token).status == "route_hello_uncertain"
    assert env.relay.binding.store.routes.destination(THREAD) == env.dest
    env.api.error = False
    relay_type = type(env.relay)
    env.relay = relay_type(tmp_path, env.relay.config, queue_dispatcher=env.relay.queue_dispatcher,
        remote_ssh_adapter=None, api=env.api, clock=lambda: env.clock[0])
    assert finish(env, token).status != "route_bound"
    assert len(env.api.calls) == 2 and not env.queued


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_reserved_invalid_controls_never_become_prompts(tmp_path, kind):
    env = setup(tmp_path, kind)
    for n, text in enumerate(("bind argument", "unbind extra", "WD-BIND-BAD", "bind\nextra"), 1):
        env.relay.handle_event(event(env, text, number=n))
    assert not env.queued and not env.api.calls


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_bound_reply_preserves_control_handoff_path(tmp_path, kind):
    env = setup(tmp_path, kind)
    assert finish(env, begin(env)).status == "route_bound"
    seen = []
    from codex_watchdog.relay import ReplyResult
    def fenced(mapping, *args, **kwargs):
        seen.append(mapping.target)
        return ReplyResult("deferred", mapping.target.workspace_id, delivery_status="control_stale_epoch")
    env.relay._controlled_reply = fenced
    hello = "om_sent000000000002" if kind == "lark" else "702"
    result = env.relay.handle_event(event(env, "go", chat=env.dest, parent=hello, number=7))
    assert result.delivery_status == "control_stale_epoch"
    assert seen == [env.target] and not env.queued


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_busy_before_control_admission_is_retryable(tmp_path, kind):
    from codex_watchdog.storage import FileLock
    env = setup(tmp_path, kind)
    with FileLock(env.relay.thread_store.lock_path):
        assert env.relay.handle_event(event(env, "bind")).status == "deferred"
    token = begin(env)
    env.clock[0] += 2
    payload = event(env, token, chat=env.dest, parent=None, number=2)
    with FileLock(env.relay.thread_store.lock_path):
        assert env.relay.handle_event(payload).status == "deferred"
    assert env.relay.handle_event(payload).status == "route_bound"
    assert not env.queued
