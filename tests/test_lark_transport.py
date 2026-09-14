"""Real SDK against an isolated HTTP/WebSocket provider; never uses account secrets."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Queue
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
import uuid

import pytest

from codex_watchdog.lark_transport import LarkApi, LarkConfig, LarkConnection, LarkTransportError, load_sdk
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.lark_relay import LarkReplyRelay
from codex_watchdog.relay import RelayTarget


CHAT = "oc_loopback000001"
USER = "ou_loopback000001"
ROOT_MESSAGE = "om_notification01"
THREAD = "11111111-2222-4333-8444-555555555555"


class FakeProvider:
    def __init__(self):
        self.config = LarkConfig("cli_" + uuid.uuid4().hex, "loopback-secret", CHAT, (USER,))
        self.posts = []
        self.failure = None
        self.delay = 0
        self.message_chat = CHAT
        self.connections = Queue()
        self.frames = Queue()
        self.responses = Queue()
        self.token = "loopback-bearer-token"
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond({"code": 0, "bot": {"open_id": "ou_loopbackbot01", "app_name": "fixture"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                provider.posts.append((self.path, dict(self.headers), body))
                if self.path == "/callback/ws/endpoint":
                    assert body == {"AppID": provider.config.app_id, "AppSecret": provider.config.app_secret}
                    self.respond({"code": 0, "data": {"URL": provider.ws_url,
                        "ClientConfig": {"PingInterval": 60, "ReconnectInterval": 1,
                                         "ReconnectNonce": 1, "ReconnectCount": -1}}})
                elif self.path.startswith("/open-apis/auth/"):
                    assert body == {"app_id": provider.config.app_id, "app_secret": provider.config.app_secret}
                    self.respond({"code": 0, "expire": 7200, "tenant_access_token": provider.token})
                else:
                    assert self.headers.get("Authorization") == "Bearer " + provider.token
                    if provider.delay:
                        time.sleep(provider.delay)
                    if provider.failure:
                        self.respond({"code": 999, "msg": "raw-secret-provider-error"}, provider.failure)
                    else:
                        message = "om_ack0000000001" if self.path.endswith("/reply") else ROOT_MESSAGE
                        self.respond({"code": 0, "data": {"chat_id": provider.message_chat, "message_id": message}})

            def respond(self, data, status=200):
                body = json.dumps(data).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        from websockets.sync.server import serve
        from websockets.exceptions import ConnectionClosed
        def websocket(connection):
            assert connection.request.path == "/events?device_id=fixture&service_id=1&ticket=loopback"
            self.connections.put(connection)
            try:
                for data in connection:
                    self.frames.put(data)
            except ConnectionClosed:
                pass
        self.ws = serve(websocket, "127.0.0.1", 0)
        self.ws_url = "ws://127.0.0.1:%d/events?device_id=fixture&service_id=1&ticket=loopback" % self.ws.socket.getsockname()[1]
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.domain = "http://127.0.0.1:%d" % self.http.server_address[1]
        self.threads = [threading.Thread(target=self.ws.serve_forever, daemon=True),
                        threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.05), daemon=True)]
        for thread in self.threads:
            thread.start()

    def api(self, timeout=2):
        client = (load_sdk().Client.builder().app_id(self.config.app_id).app_secret(self.config.app_secret)
                  .domain(self.domain).timeout(timeout).trust_env_proxy(False).build())
        return LarkApi(self.config, timeout, client=client)

    def channel(self):
        from lark_channel.channel.config import TransportConfig, SecurityConfig, PolicyConfig, InboundConfig, NameCacheConfig
        return load_sdk().FeishuChannel(app_id=self.config.app_id, app_secret=self.config.app_secret,
            domain=self.domain, transport=TransportConfig(kind="ws", trust_env_proxy=False,
                http_timeout_seconds=2, handshake_timeout_seconds=2),
            security=SecurityConfig(mode="strict", allow_local_insecure_ws=True),
            policy=PolicyConfig(dm_policy="disabled", group_policy="disabled"),
            inbound=InboundConfig(emit_raw_events=True, name_cache=NameCacheConfig(enabled=False)))

    def event(self, text="  exact reply\n\u98de\u4e66 \u2603  "):
        return {"schema": "2.0", "header": {"event_id": "loopback-event0001", "app_id": self.config.app_id,
            "event_type": "im.message.receive_v1", "create_time": "1789084800000", "tenant_key": "fixture"},
            "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": USER}},
                "message": {"message_id": "om_reply00000001", "chat_id": CHAT, "chat_type": "group",
                    "message_type": "text", "root_id": ROOT_MESSAGE, "parent_id": ROOT_MESSAGE,
                    "content": json.dumps({"text": text})}}}

    def emit(self, connection, payload):
        from lark_channel.ws.pb.pbbp2_pb2 import Frame
        frame = Frame(SeqID=1, LogID=1, service=1, method=1, payload=json.dumps(payload).encode())
        for key, value in {"type": "event", "message_id": uuid.uuid4().hex, "sum": "1", "seq": "0", "trace_id": "fixture"}.items():
            header = frame.headers.add()
            header.key, header.value = key, value
        connection.send(frame.SerializeToString())

    def close(self):
        self.ws.shutdown()
        self.http.shutdown()
        self.http.server_close()
        for thread in self.threads:
            thread.join(3)


@pytest.fixture
def provider():
    instance = FakeProvider()
    try:
        yield instance
    finally:
        instance.close()


def test_setup_pairs_through_real_sdk_without_posting_or_codex_mapping(provider, tmp_path, monkeypatch):
    from codex_watchdog import messaging_pairing
    from codex_watchdog.messaging_profile import PREFIX
    from codex_watchdog.storage import FileLock
    def connection(config, callback):
        return LarkConnection(config, callback, timeout=2, channel=provider.channel())
    monkeypatch.setattr(messaging_pairing, "LarkConnection", connection)
    output = []
    def emit_confirmation(text):
        output.append(text)
        if text.startswith("WATCHDOG-PAIR-"):
            socket = provider.connections.get(timeout=5)
            payload = provider.event(text)
            payload["event"]["message"].pop("root_id")
            payload["event"]["message"].pop("parent_id")
            payload["event"]["message"]["create_time"] = str(int(time.time() * 1000))
            provider.emit(socket, payload)
    values = {PREFIX + "LARK_" + k: v for k, v in dict(APP_ID=provider.config.app_id,
        APP_SECRET=provider.config.app_secret, DOMAIN="feishu").items()}
    paired = messaging_pairing.pair_provider("lark", values, tmp_path, read=lambda _: "yes", output=emit_confirmation)
    assert paired["chat_id"] == CHAT and paired["allowed_user_ids"] == [USER]
    assert not any("/im/v1/messages" in path for path, _, _ in provider.posts)
    assert not (tmp_path / "lark").exists() and not (tmp_path / "inbox").exists()
    with FileLock(tmp_path / "locks" / ("lark-listener-" + provider.config.scope + ".lock")):
        pass  # Pairing releases the normal listener lock.


def test_real_sdk_auth_create_reply_and_stable_idempotency_uuid(provider):
    api = provider.api()
    assert api.send("  literal \u2603\n", "operation") == {"chat_id": CHAT, "message_id": ROOT_MESSAGE}
    api.send("again", "operation")
    api.send("ack", "ack-operation", reply_to=ROOT_MESSAGE)
    messages = [post for post in provider.posts if "/im/v1/messages" in post[0]]
    assert len(messages) == 3
    assert parse_qs(urlparse(messages[0][0]).query) == {"receive_id_type": ["chat_id"]}
    assert messages[0][2]["receive_id"] == CHAT
    assert json.loads(messages[0][2]["content"]) == {"text": "  literal \u2603\n"}
    assert messages[0][2]["uuid"] == messages[1][2]["uuid"] != messages[2][2]["uuid"]
    assert messages[2][0] == "/open-apis/im/v1/messages/" + ROOT_MESSAGE + "/reply"
    assert messages[2][2]["reply_in_thread"] is True
    assert len([p for p in provider.posts if "/auth/" in p[0]]) == 1


@pytest.mark.parametrize("failure", [200, 401, 429, 500, "timeout", "identity"])
def test_actual_http_failures_are_safe_and_do_not_resend_uncertain_notification(provider, tmp_path, failure):
    if failure == "timeout": provider.delay = 0.2
    elif failure == "identity": provider.message_chat = "oc_wrongchat0001"
    else: provider.failure = failure
    notifier = EnvironmentNotifier(tmp_path, NotificationConfig(lark=provider.config), lark_api=provider.api(timeout=0.05))
    event = NotificationEvent("workspace", "waiting", "notification", "Waiting", "Body")
    result = notifier.notify(event)
    assert result.status != "sent"
    notifier.notify(event)
    messages = [p for p in provider.posts if "/im/v1/messages" in p[0]]
    assert len(messages) == 1
    saved = "".join(path.read_text() for path in tmp_path.rglob("*.json"))
    assert "raw-secret-provider-error" not in saved and provider.config.app_secret not in saved


def test_sdk_socket_notification_reply_reconnect_duplicate_and_collision(provider, tmp_path):
    calls, completed = [], Queue()
    def factory(config, callback, timeout):
        def received(payload):
            callback(payload)
            completed.put(payload)
        return LarkConnection(config, received, timeout, channel=provider.channel())
    queue = SimpleNamespace(dispatch=lambda *args: (calls.append(args) or SimpleNamespace(status="enqueued")))
    relay = LarkReplyRelay(tmp_path, provider.config, queue_dispatcher=queue, remote_ssh_adapter=None,
                          timeout=2, api=provider.api(), connection_factory=factory)
    notifier = EnvironmentNotifier(tmp_path, NotificationConfig(lark=provider.config), lark_api=provider.api())
    event = NotificationEvent("workspace", "waiting", "notification", "Waiting", "Reply",
                               RelayTarget("workspace", THREAD, "process_local"))
    assert notifier.notify(event).status == "sent"
    try:
        relay.start()
        socket = provider.connections.get(timeout=5)
        payload = provider.event()
        provider.emit(socket, payload)
        completed.get(timeout=5)
        assert len(calls) == 1 and calls[0][0] == THREAD
        assert calls[0][2] == json.loads(payload["event"]["message"]["content"])["text"]
        socket.close()
        socket = provider.connections.get(timeout=5)  # SDK owns authenticated reconnect.
        for altered in (payload, provider.event(text="event identity collision")):
            provider.emit(socket, altered)
            completed.get(timeout=5)
        untrusted = provider.event()
        untrusted["header"]["event_id"] = "loopback-event0002"
        untrusted["event"]["sender"]["sender_id"]["open_id"] = "ou_intruder00001"
        provider.emit(socket, untrusted)
        completed.get(timeout=5)
        assert len(calls) == 1
        assert len([p for p in provider.posts if p[0].endswith("/reply")]) == 1
        assert len([p for p in provider.posts if p[0] == "/callback/ws/endpoint"]) == 2
    finally:
        relay.close()
    assert relay._connection is None and relay._listener_lock is None


def test_lifecycle_failure_redaction_and_restart():
    class Channel:
        def __init__(self): self.fail, self.started, self.stopped = True, 0, 0
        def on(self, *args): pass
        async def start_background(self, **kwargs):
            self.started += 1
            if self.fail: raise RuntimeError("raw-private-endpoint-and-token")
        async def stop_background(self): self.stopped += 1
    channel = Channel()
    connection = LarkConnection(None, lambda _: None, timeout=0.1, channel=channel)
    with pytest.raises(LarkTransportError, match="^lark_connection_failed$"):
        connection.start()
    channel.fail = False
    connection.start()
    connection.start()
    connection.close()
    connection.close()
    assert channel.started == channel.stopped == 2


def test_provider_selection_retains_existing_slack_and_redacts_credentials():
    lark = {"CODEX_WATCHDOG_LARK_APP_ID": "cli_fixture000001", "CODEX_WATCHDOG_LARK_APP_SECRET": "private-secret",
            "CODEX_WATCHDOG_LARK_CHAT_ID": CHAT, "CODEX_WATCHDOG_LARK_ALLOWED_USER_IDS": USER}
    config = NotificationConfig.from_environment(lark)
    assert config.selected_interactive_transport == "lark" and config.interactive_relay_configured
    both = {**lark, "CODEX_WATCHDOG_SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/fixture/fixture/fixture"}
    assert NotificationConfig.from_environment(both).selected_interactive_transport == "slack"
    both["CODEX_WATCHDOG_INTERACTIVE_TRANSPORT"] = "lark"
    assert NotificationConfig.from_environment(both).selected_interactive_transport == "lark"
    assert "private-secret" not in repr(config) + repr(config.lark)
    assert LarkConfig.from_environment({**lark, "CODEX_WATCHDOG_LARK_DOMAIN": "lark"}).scope != config.lark.scope
    with pytest.raises(ValueError):
        LarkConfig.from_environment({**lark, "CODEX_WATCHDOG_LARK_DOMAIN": "https://attacker.invalid"})


def test_lark_credentials_are_not_inherited_by_codex(monkeypatch, tmp_path):
    from codex_watchdog.process_environment import codex_process_environment
    monkeypatch.setenv("CODEX_WATCHDOG_LARK_APP_SECRET", "provider-private")
    monkeypatch.setenv("CODEX_WATCHDOG_LARK_CHAT_ID", CHAT)
    monkeypatch.setenv("UNRELATED_USER_SETTING", "retained")
    child = codex_process_environment(tmp_path)
    assert not any(name.startswith("CODEX_WATCHDOG_LARK_") for name in child)
    assert child["UNRELATED_USER_SETTING"] == "retained"
    import os
    assert os.environ["CODEX_WATCHDOG_LARK_APP_SECRET"] == "provider-private"
