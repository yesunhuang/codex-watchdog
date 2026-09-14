"""Bounded, authenticated confirmation-message pairing; never dispatches work."""
from __future__ import annotations

from contextlib import ExitStack
import json
import logging
import re
import secrets
import threading
import time

from .lark_transport import LarkConfig, LarkConnection, valid_id
from .messaging_profile import MessagingError, PREFIX
from .storage import FileLock


class NoncePairing:
    def __init__(self, provider, *, app_id=None, domain=None, team_id=None, bot_user=None,
                 lifetime=180, clock=time.time, monotonic=time.monotonic):
        self.provider, self.app_id, self.domain = provider, app_id, domain
        self.team_id, self.bot_user = team_id, bot_user
        self.clock, self.monotonic = clock, monotonic
        self.started, self.deadline = clock(), monotonic() + lifetime
        self.lifetime = lifetime
        self.nonce = "WATCHDOG-PAIR-" + secrets.token_hex(16)
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.candidate = None
        self.ambiguous = False
        self.consumed = False

    def _fresh(self, timestamp, divisor=1):
        try:
            value = float(timestamp) / divisor
            return self.started <= value <= min(self.clock() + 2, self.started + self.lifetime)
        except (ValueError, TypeError, OverflowError):
            return False

    def offer(self, body):
        with self.lock:
            if self.consumed or self.monotonic() >= self.deadline or not isinstance(body, dict):
                return False
            try:
                candidate = self._lark(body) if self.provider == "lark" else self._slack(body)
            except (ValueError, TypeError, AttributeError, KeyError):
                return False
            if candidate is None:
                return False
            if self.candidate is not None:
                # Provider retries of the same immutable event are ignored;
                # a second matching message/identity is ambiguous, never paired.
                if candidate != self.candidate:
                    self.ambiguous = True
                return False
            self.candidate = candidate
            self.ready.set()
            return True

    def _lark(self, body):
        header, event = body.get("header", {}), body.get("event", {})
        if (body.get("schema") != "2.0" or header.get("app_id") != self.app_id or
                header.get("event_type") != "im.message.receive_v1" or not header.get("event_id")):
            return None
        sender, msg = event.get("sender", {}), event.get("message", {})
        user = sender.get("sender_id", {}).get("open_id")
        chat, message_id = msg.get("chat_id"), msg.get("message_id")
        if (sender.get("sender_type") != "user" or msg.get("message_type") != "text" or
                msg.get("chat_type") not in ("p2p", "group") or msg.get("parent_id") or msg.get("root_id") or
                msg.get("deleted") or msg.get("edited") or msg.get("update_time") not in (None, "0", 0, msg.get("create_time")) or
                not self._fresh(msg.get("create_time"), 1000) or
                not all((valid_id(user, "ou"), valid_id(chat, "oc"), valid_id(message_id, "om")))):
            return None
        content = msg.get("content")
        if not isinstance(content, str) or len(content) > 1024 or json.loads(content).get("text") != self.nonce:
            return None
        return (chat, user, message_id)

    def _slack(self, body):
        event = body.get("event", {})
        authorizations = body.get("authorizations", [])
        if (body.get("type") != "event_callback" or body.get("team_id") != self.team_id or
                not self.app_id or body.get("api_app_id") != self.app_id or
                not body.get("event_id") or not any(isinstance(a, dict) and a.get("team_id") == self.team_id and
                    a.get("user_id") == self.bot_user and a.get("is_bot") is True for a in authorizations)):
            return None
        chat, user, stamp = event.get("channel", ""), event.get("user", ""), event.get("ts")
        if (event.get("type") != "message" or event.get("subtype") or event.get("bot_id") or
                event.get("app_id") or event.get("edited") or event.get("hidden") or event.get("thread_ts") or
                event.get("text") != self.nonce or user == self.bot_user or
                event.get("channel_type") not in ("channel", "group") or
                not re.fullmatch(r"[CG][A-Z0-9]{8,}", chat) or
                not re.fullmatch(r"[UW][A-Z0-9]{8,}", user) or not self._fresh(stamp)):
            return None
        return (chat, user, stamp)

    def slack_hello(self, body):
        with self.lock:
            app_id = body.get("connection_info", {}).get("app_id")
            if (body.get("type") != "hello" or not isinstance(app_id, str) or
                    not re.fullmatch(r"A[A-Z0-9]{8,}", app_id) or body.get("num_connections") != 1 or
                    self.app_id not in (None, app_id)):
                self.ambiguous = True
                return False
            self.app_id = app_id
            return True

    def finish(self):
        with self.lock:
            if self.consumed or self.monotonic() >= self.deadline or not self.candidate or self.ambiguous:
                raise MessagingError("messaging_pairing_expired_or_ambiguous")
            self.consumed = True
            chat, user, _ = self.candidate
            return dict(schema_version=1, **({"chat_id": chat, "domain": self.domain, "app_id": self.app_id}
                        if self.provider == "lark" else {"channel_id": chat}), allowed_user_ids=[user])


class SlackPairingConnection:
    def __init__(self, values):
        self.values = values
        self.handler = None

    def authenticate(self):
        from slack_sdk import WebClient
        self.client = WebClient(token=self.values[PREFIX + "SLACK_BOT_TOKEN"], timeout=10, retry_handlers=[])
        result = self.client.auth_test()
        team, user = result.get("team_id"), result.get("user_id")
        if not result.get("bot_id") or not team or not user:
            raise MessagingError("messaging_slack_bot_identity_unavailable")
        return team, user

    def start(self, pairing):
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        logger = logging.getLogger("codex_watchdog.pairing.slack")
        logger.disabled = True
        app = App(client=self.client, logger=logger)
        def receive(event, body):
            pairing.offer(body)
        app.event("message")(receive)
        self.handler = SocketModeHandler(app, self.values[PREFIX + "SLACK_APP_TOKEN"],
                                         logger=logger, auto_reconnect_enabled=False, concurrency=1)
        hello = threading.Event()
        def received(client, message, raw_message):
            if message.get("type") == "hello":
                pairing.slack_hello(message)
                hello.set()
        self.handler.client.message_listeners.append(received)
        # The SDK's default URL helper retries rate limits recursively. Setup
        # needs one bounded attempt, not a persistent reconnecting listener.
        def endpoint_once():
            return self.client.apps_connections_open(app_token=self.values[PREFIX + "SLACK_APP_TOKEN"])["url"]
        self.handler.client.issue_new_wss_url = endpoint_once
        self.handler.connect()
        if not hello.wait(10) or pairing.ambiguous:
            raise MessagingError("messaging_slack_app_or_exclusive_connection_unconfirmed")

    def close(self):
        if self.handler is not None:
            self.handler.close()


def pair_provider(provider, values, runtime, *, read=input, output=print):
    """Credentials are in memory; persist nothing until local confirmation."""
    with ExitStack() as stack:
        # Use the normal runtime listener locks. Do not consume a running
        # monitor's event stream or its incoming Codex messages.
        if provider == "lark":
            config = LarkConfig(app_id=values[PREFIX + "LARK_APP_ID"], app_secret=values[PREFIX + "LARK_APP_SECRET"],
                                domain=values[PREFIX + "LARK_DOMAIN"])
            from .models import sha256_text
            scope = sha256_text("lark\0" + config.domain + "\0" + config.app_id)
            stack.enter_context(FileLock(runtime / "locks" / ("lark-listener-" + scope + ".lock")))
            pairing = NoncePairing(provider, app_id=config.app_id, domain=config.domain)
            connection = LarkConnection(config, pairing.offer)
        else:
            stack.enter_context(FileLock(runtime / "locks" / "slack-socket-mode.lock"))
            stack.enter_context(FileLock(runtime / "locks" / "slack-poll-listener.lock"))
            connection = SlackPairingConnection(values)
            team, user = connection.authenticate()
            pairing = NoncePairing(provider, team_id=team, bot_user=user)
        stack.callback(connection.close)
        if provider == "lark":
            connection.start()
        else:
            connection.start(pairing)
        output("Send this exact text as a NEW plain-text message to the intended bot conversation (expires in 3 minutes):")
        if provider == "slack":
            output("Use a channel containing the bot; direct-message channels are not supported by the existing Slack relay.")
        output(pairing.nonce)
        while not pairing.ready.wait(min(0.25, max(0, pairing.deadline - pairing.monotonic()))):
            if pairing.monotonic() >= pairing.deadline:
                raise MessagingError("messaging_pairing_timed_out")
        # Release the network listener even if the local confirmation prompt is
        # left unanswered. finish() still checks expiry before saving anything.
        connection.close()
        chat, user, _ = pairing.candidate
        output("Received confirmation from " + user + " in " + chat + ".")
        if read("Is this the conversation and account you intended? Type yes: ").strip().lower() != "yes":
            raise MessagingError("messaging_pairing_cancelled")
        return pairing.finish()
