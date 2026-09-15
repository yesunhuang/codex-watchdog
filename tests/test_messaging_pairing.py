from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from codex_watchdog.messaging_device import device_label
from codex_watchdog.messaging_pairing import NoncePairing, pair_provider
from codex_watchdog.messaging_profile import MessagingError, PREFIX
from codex_watchdog.pairing_api import PairingApi, select_conversation
from codex_watchdog.storage import FileLock


def pairing(provider, label="012345abcdef"):
    now = [1700000000.0]
    p = NoncePairing(provider, "C12345678" if provider == "slack" else "oc_fixture00001", label,
                    bot_user="U87654321", clock=lambda: now[0], monotonic=lambda: now[0])
    return p, now


def message(p, identity=1):
    if p.provider == "slack":
        return dict(type="message", user="U12345678", ts="1700000000.%06d" % identity, text=p.nonce)
    return dict(sender=dict(sender_type="user", id_type="open_id", id="ou_fixture00001"),
                chat_id=p.chat, message_id="om_fixture0000%d" % identity, msg_type="text", deleted=False,
                create_time="1700000000000", updated=True, update_time="1700000000064",
                body=dict(content=json.dumps(dict(text=p.nonce))))


def test_device_hash_stable_distinguishes_duplicate_hostnames_and_shared_nodes(tmp_path):
    label = device_label(tmp_path / "one", hostname="shared-name")
    original = (tmp_path / "one/messaging-device.json").read_bytes()
    assert device_label(tmp_path / "one", hostname="SHARED-NAME") == label
    assert len(label) == 12 and int(label, 16) >= 0
    assert device_label(tmp_path / "two", hostname="shared-name") != label
    assert device_label(tmp_path / "one", hostname="second-node") != label
    assert (tmp_path / "one/messaging-device.json").read_bytes() == original
    assert b"shared-name" not in original


def test_device_identity_unknown_version_is_preserved(tmp_path):
    p = tmp_path / "messaging-device.json"
    p.write_text('{"schema_version": 9, "seed": "keep"}')
    before = p.read_bytes()
    with pytest.raises(ValueError): device_label(tmp_path)
    assert p.read_bytes() == before


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_two_devices_pair_on_same_app_without_consuming_each_others_messages(provider):
    a, _ = pairing(provider)
    b, _ = pairing(provider, "fedcba543210")
    assert a.nonce != b.nonce
    history = [message(a), message(b, 2)]
    api = SimpleNamespace(history=lambda *args: (history, None))
    for p in (a, b):
        assert p.poll(api)
        assert p.poll(api)  # Independent repeated reads, not a consumed queue.
        route = p.finish({PREFIX + "LARK_DOMAIN": "feishu", PREFIX + "LARK_APP_ID": "cli_fixture00001"})
        assert route["reply_mode"] == "poll" and route["device_label"] == p.label
        with pytest.raises(MessagingError): p.finish({})
    assert a.candidate[1] != b.candidate[1]


@pytest.mark.parametrize("provider", ["slack", "lark"])
@pytest.mark.parametrize("mutation", ["bot", "old", "future", "reply", "edited", "deleted", "altered", "bad_id", "nontext", "wrong_chat"])
def test_pairing_rejects_unsafe_or_unrelated_history(provider, mutation):
    p, _ = pairing(provider)
    m = message(p)
    if provider == "slack":
        if mutation == "bot": m["bot_id"] = "B12345678"
        elif mutation == "old": m["ts"] = "1699999999.000001"
        elif mutation == "future": m["ts"] = "1700000020.000001"
        elif mutation == "reply": m["thread_ts"] = "1700000000.000001"
        elif mutation == "edited": m["edited"] = dict(ts="1700000000.000002")
        elif mutation == "deleted": m["subtype"] = "message_deleted"
        elif mutation == "altered": m["text"] += "x"
        elif mutation == "bad_id": m["user"] = "bad"
        elif mutation == "nontext": m["type"] = "file"
        elif mutation == "wrong_chat": m["channel"] = "C99999999"
    else:
        if mutation == "bot": m["sender"]["sender_type"] = "app"
        elif mutation == "old": m["create_time"] = "1699999999999"
        elif mutation == "future": m["create_time"] = "1700000020000"
        elif mutation == "reply": m["parent_id"] = "om_parent000001"
        elif mutation == "edited": m["edited"] = True
        elif mutation == "deleted": m["deleted"] = True
        elif mutation == "altered": m["body"]["content"] = '{"text":"wrong"}'
        elif mutation == "bad_id": m["sender"]["id"] = "bad"
        elif mutation == "nontext": m["msg_type"] = "file"
        elif mutation == "wrong_chat": m["chat_id"] = "oc_other000001"
    assert not p.offer(m)
    with pytest.raises(MessagingError): p.finish({})


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_pairing_expiry_ambiguous_later_page_and_changed_confirmation(provider):
    p, now = pairing(provider)
    first, second = message(p), message(p, 2)
    api = SimpleNamespace(history=lambda chat, start, end, cursor: ([second], None) if cursor else ([first], "next"))
    with pytest.raises(MessagingError, match="ambiguous"): p.poll(api)
    p, now = pairing(provider)
    m = message(p)
    assert p.offer(m)
    if provider == "slack": m["text"] = "changed"
    else: m["body"]["content"] = '{"text":"changed"}'
    assert not p.offer(m)
    with pytest.raises(MessagingError): p.finish({})
    p, now = pairing(provider)
    now[0] += 180
    with pytest.raises(MessagingError): p.offer(message(p))


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_editing_first_observed_text_into_nonce_is_not_pairing(provider):
    p, _ = pairing(provider)
    good = message(p)
    bad = deepcopy(good)
    if provider == "slack": bad["text"] = "before"
    else: bad["body"]["content"] = '{"text":"before"}'
    assert not p.offer(bad)
    assert not p.offer(good)


def test_pairing_pagination_failure_never_finishes():
    p, _ = pairing("slack")
    api = SimpleNamespace(history=lambda *args: ([message(p)], "repeated"))
    with pytest.raises(MessagingError, match="pagination"): p.poll(api)
    assert p.candidate is None


def test_pairing_works_while_normal_runtime_listener_locks_are_held(tmp_path):
    history, output = [], []
    api = SimpleNamespace(provider="slack", bot_user="U87654321",
        conversations=lambda: [("C12345678", "Selected channel")], check_history=lambda *a: None,
        check_replies=lambda *a: None, history=lambda *a: (history, None))
    def show(text):
        output.append(text)
        if text.startswith("WATCHDOG-PAIR-"):
            import time
            history.append(dict(type="message", user="U12345678", ts="%.6f" % (time.time() + 0.001), text=text))
    answers = iter(["1", "yes"])
    with FileLock(tmp_path / "locks/slack-socket-mode.lock"), FileLock(tmp_path / "locks/slack-poll-listener.lock"):
        route = pair_provider("slack", {}, tmp_path, read=lambda _: next(answers), output=show,
                              api_factory=lambda *a: api)
    assert route["reply_mode"] == "poll" and route["allowed_user_ids"] == ["U12345678"]
    assert not any("stop" in line.lower() for line in output)


def test_slack_authenticated_discovery_history_and_capability_failure():
    calls = []
    class Client:
        def auth_test(self): return dict(ok=True, bot_id="B12345678", team_id="T12345678", user_id="U87654321")
        def users_conversations(self, **kw):
            calls.append(kw)
            return dict(ok=True, channels=[dict(id="C12345678", name="Selected")])
        def conversations_history(self, **kw): return dict(ok=True, messages=[])
        def conversations_replies(self, **kw): raise RuntimeError("private token must not escape")
    api = PairingApi("slack", {}, client=Client())
    assert select_conversation(api, read=lambda _: "1", output=lambda _: None) == "C12345678"
    assert calls[0]["types"] == "public_channel,private_channel"
    assert api.history("C12345678", 1, 2) == ([], None)
    with pytest.raises(MessagingError, match="^messaging_slack_api_unavailable_check_permissions_or_rate_limit$"):
        api.check_replies("C12345678", "1700000000.000001")


@pytest.mark.parametrize("provider,reason,chat", [
    ("slack", "messaging_slack_missing_scope", "C12345678"),
    ("lark", "messaging_lark_chat_list_error_99991672", "oc_fixture00001")])
def test_missing_optional_list_permission_asks_only_for_chat(provider, reason, chat):
    def failed(): raise MessagingError(reason)
    api = SimpleNamespace(provider=provider, conversations=failed)
    prompts = []
    def read(prompt): prompts.append(prompt); return chat
    assert select_conversation(api, read=read, output=lambda _: None) == chat
    assert len(prompts) == 1


def test_credentials_failure_does_not_offer_permission_fallback():
    def failed(): raise MessagingError("messaging_slack_invalid_auth")
    api = SimpleNamespace(provider="slack", conversations=failed)
    with pytest.raises(MessagingError, match="invalid_auth"):
        select_conversation(api, read=lambda _: pytest.fail("must not ask for chat"), output=lambda _: None)


def test_actual_slack_sdk_discovery_history_and_reply_capability():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from urllib.parse import parse_qs
    from slack_sdk import WebClient
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer xoxb-loopback-fixture"
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append((self.path, body))
            result = {
                "/auth.test": dict(ok=True, bot_id="B12345678", user_id="U87654321", team_id="T12345678"),
                "/users.conversations": dict(ok=True, channels=[dict(id="C12345678", name="Group")]),
                "/conversations.history": dict(ok=True, messages=[]),
                "/conversations.replies": dict(ok=True, messages=[]),
            }[self.path]
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        client = WebClient(token="xoxb-loopback-fixture", base_url="http://127.0.0.1:%d/" % server.server_port,
                           timeout=2, retry_handlers=[])
        api = PairingApi("slack", {}, client=client)
        assert api.conversations() == [("C12345678", "Group")]
        assert api.history("C12345678", 1700000000, 1700000001) == ([], None)
        api.check_replies("C12345678", "1700000000.000001")
        history = parse_qs(next(body.decode() for path, body in calls if path == "/conversations.history"))
        assert history["channel"] == ["C12345678"]
        assert history["oldest"] == ["1700000000"] and history["latest"] == ["1700000001"]
        assert len(calls) == 4  # No socket or provider write endpoint.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
