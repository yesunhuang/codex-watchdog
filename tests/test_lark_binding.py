import json

import pytest

from codex_watchdog.lark_binding import LarkBindingDiscovery
from codex_watchdog.lark_poll import LarkReplyPoller
from codex_watchdog.lark_transport import LarkTransportError
from test_destination_binding import setup, begin, event


def history_message(env, token, chat=None, mid="om_challenge0001"):
    return dict(chat_id=chat or env.dest, message_id=mid, create_time=str(int(env.clock[0]*1000)),
        msg_type="text", deleted=False, updated=True, update_time=str(int(env.clock[0]*1000)+10),
        sender=dict(id=env.user,id_type="open_id",sender_type="user"),
        body=dict(content=json.dumps(dict(text=token))))


def fixture(root):
    env = setup(root, "lark")
    env.history = {}
    env.queries = []
    env.listings = []
    def conversations():
        env.listings.append(True)
        return [(env.default,"Default"),(env.dest,"Destination")]
    def history(start,end,page_token=None,*,destination=None):
        chat = destination or env.default
        env.queries.append((chat,start,end,page_token))
        return dict(items=env.history.get(chat,[]),has_more=False)
    env.api.conversations = conversations
    env.api.history = history
    env.discovery = LarkBindingDiscovery(env.relay,env.api,clock=lambda:env.clock[0])
    return env


def test_no_pending_challenge_does_not_browse_conversations(tmp_path):
    env=fixture(tmp_path)
    assert env.discovery.poll_once() == []
    assert env.queries == env.listings == []


def test_poll_finds_same_user_in_new_chat_then_stops_discovery(tmp_path):
    env=fixture(tmp_path)
    token=begin(env)
    env.clock[0]+=1
    env.history[env.dest]=[history_message(env,token)]
    env.clock[0]+=3
    results=env.discovery.poll_once()
    assert results[0]["status"] == "route_bound"
    assert env.relay.binding.store.routes.destination(env.target.thread_id) == env.dest
    assert not env.queued
    before=len(env.queries),len(env.listings)
    assert env.discovery.poll_once() == []
    assert (len(env.queries),len(env.listings)) == before


def test_ambiguous_destinations_never_pick_first_and_survive_restart(tmp_path):
    env=fixture(tmp_path)
    token=begin(env)
    env.clock[0]+=1
    env.history[env.default]=[history_message(env,token,env.default)]
    env.history[env.dest]=[history_message(env,token,mid="om_challenge0002")]
    env.clock[0]+=3
    assert env.discovery.poll_once()[0]["status"] == "rejected_ambiguous_route_challenge"
    assert env.relay.binding.store.routes.destination(env.target.thread_id) is None
    env.history[env.default]=[]
    restarted=LarkBindingDiscovery(env.relay,env.api,clock=lambda:env.clock[0])
    assert restarted.poll_once()[0]["status"] == "rejected_ambiguous_route_challenge"
    assert not env.queued and len(env.api.calls)==1


def test_changed_message_after_first_observation_is_not_confirmation(tmp_path):
    env=fixture(tmp_path)
    token=begin(env)
    env.clock[0]+=1
    message=history_message(env,"unrelated text")
    env.history[env.dest]=[message]
    env.clock[0]+=3
    assert env.discovery.poll_once()==[]
    message["body"]["content"]=json.dumps(dict(text=token))
    restarted=LarkBindingDiscovery(env.relay,env.api,clock=lambda:env.clock[0])
    assert restarted.poll_once()==[]
    assert env.relay.binding.store.routes.destination(env.target.thread_id) is None


def test_discovery_history_is_bounded_and_partial_page_cannot_bind(tmp_path):
    env=fixture(tmp_path)
    token=begin(env)
    env.clock[0]+=1
    message=history_message(env,token)
    env.clock[0]+=3
    calls=[]
    env.api.conversations=lambda:[(env.dest,"Destination")]
    def pages(*args,**kwargs):
        chat=kwargs["destination"]
        if chat==env.default:return dict(items=[],has_more=False)
        calls.append(args)
        return dict(items=[message],has_more=True,page_token=str(len(calls)))
    env.api.history=pages
    with pytest.raises(LarkTransportError,match="limit_or_cursor"):
        for _ in range(3): env.discovery.poll_once()
    assert len(calls)==4 and env.relay.binding.store.routes.destination(env.target.thread_id) is None


def test_changed_candidate_is_removed_and_cannot_be_restored_after_restart(tmp_path):
    env = fixture(tmp_path)
    token = begin(env)
    env.clock[0] += 1
    message = history_message(env, token)
    matches = {}
    env.discovery._offer(message, matches)
    message["body"]["content"] = json.dumps(dict(text="edited"))
    env.discovery._offer(message, matches)
    assert env.discovery._finish(matches) == []
    message["body"]["content"] = json.dumps(dict(text=token))
    env.history[env.dest] = [message]
    env.clock[0] += 3
    restarted = LarkBindingDiscovery(env.relay, env.api, clock=lambda: env.clock[0])
    assert restarted.poll_once() == []
    assert env.relay.binding.store.routes.destination(env.target.thread_id) is None


def test_bound_chat_reply_is_polled_with_persistent_cursor(tmp_path):
    env=fixture(tmp_path)
    token=begin(env)
    env.clock[0]+=1
    env.history[env.dest]=[history_message(env,token)]
    env.clock[0]+=3
    assert env.discovery.poll_once()[0]["status"]=="route_bound"
    env.clock[0]+=1
    hello="om_sent000000000002"
    native_roots = {}
    lookups = []
    def thread_for_root(root, *, destination=None):
        chat = destination or env.default
        assert env.relay.thread_store.lookup_thread(chat, root) is not None
        tid = "omt_" + root.removeprefix("om_")
        native_roots[tid] = (chat, root)
        lookups.append((chat, root))
        return tid
    def thread_history(tid, page_token=None):
        assert page_token is None
        chat, root = native_roots[tid]
        return dict(items=[item for item in env.history.get(chat, [])
                          if item.get("root_id") == root], has_more=False)
    env.api.thread_for_root = thread_for_root
    env.api.thread_history = thread_history
    poller=LarkReplyPoller(env.relay,api=env.api,clock=lambda:env.clock[0])
    env.clock[0]+=1  # Fresh native reply follows the first-switch baseline.
    reply=history_message(env,"continue",mid="om_newreply0001")
    reply.update(root_id=hello,parent_id=hello,updated=False,update_time=reply["create_time"])
    env.history[env.dest]=[reply]
    env.clock[0]+=3
    history_reads = len(env.queries)
    poller.poll_once()
    assert len(env.queued)==1 and env.queued[0][0]==env.target.thread_id
    assert env.api.calls[-1]["destination"]==env.dest
    assert (env.dest, hello) in lookups and len(env.queries) == history_reads
    restarted=LarkReplyPoller(env.relay,api=env.api,clock=lambda:env.clock[0]+10)
    restarted.poll_once()
    assert len(env.queued)==1
