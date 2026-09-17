"""OneBot 11 client policy around the attributed upstream connection module.

The backend owns QQ. No outbound action is retried after uncertain delivery.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from .models import sha256_text, utc_now
from .storage import InstructionStore


class OneBotError(RuntimeError):
    """Fixed diagnostics only; provider exceptions can contain private data."""


def identifier(value, *, message=False):
    """Canonical OneBot integers; booleans and lossy coercions are not IDs."""
    if type(value) is int:
        value = str(value)
    pattern = r"(?:0|-?[1-9][0-9]{0,18})" if message else r"[1-9][0-9]{0,18}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        return None
    return value if -(2**63) <= int(value) < 2**63 else None


def valid_endpoint(value):
    try:
        parts = urlsplit(value)
        return bool(isinstance(value, str) and value == value.strip()
                    and not any(ord(c) <= 32 for c in value)
                    and parts.scheme in ("ws", "wss") and parts.hostname
                    and not parts.username and not parts.password
                    and not parts.query and not parts.fragment
                    and (parts.port is None or 1 <= parts.port <= 65535))
    except (TypeError, ValueError, AttributeError):
        return False


@dataclass(frozen=True, repr=False)
class OneBotConfig:
    ws_url: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
    self_id: str | None = None
    chat_type: str | None = None
    chat_id: str | None = None
    allowed_user_ids: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls, environment=None):
        env = os.environ if environment is None else environment
        def get(key):
            value = env.get("CODEX_WATCHDOG_ONEBOT_" + key)
            return value.strip() if isinstance(value, str) and value.strip() else None
        users = tuple(v for v in re.split(r"[,;\s]+", get("ALLOWED_USER_IDS") or "") if v)
        return cls(get("WS_URL"), get("ACCESS_TOKEN"), get("SELF_ID"),
                   get("CHAT_TYPE"), get("CHAT_ID"), users)

    @property
    def present(self):
        return any((self.ws_url, self.access_token, self.self_id, self.chat_type,
                    self.chat_id, self.allowed_user_ids))

    @property
    def configured(self):
        return bool(valid_endpoint(self.ws_url) and isinstance(self.access_token, str)
                    and self.access_token.strip() and not any(ord(c) < 32 for c in self.access_token)
                    and isinstance(self.self_id, str) and identifier(self.self_id)
                    and self.chat_type in ("private", "group")
                    and isinstance(self.chat_id, str) and identifier(self.chat_id)
                    and all(isinstance(user, str) and identifier(user) for user in self.allowed_user_ids))

    @property
    def relay_configured(self):
        return self.configured and bool(self.allowed_user_ids)

    @property
    def scope(self):
        if not self.configured:
            raise OneBotError("onebot_configuration_incomplete")
        return sha256_text("onebot11\0" + self.self_id)

    @property
    def destination(self):
        if not self.configured:
            raise OneBotError("onebot_configuration_incomplete")
        return self.chat_type + ":" + self.chat_id

    def __repr__(self):
        return f"OneBotConfig(configured={self.configured}, relay_configured={self.relay_configured})"


def websocket_options(token, timeout):
    # Pass authentication in headers, never in URLs or logs. Proxy behavior is
    # explicit for the operator-selected backend; no process environment edits.
    logger = logging.getLogger("codex_watchdog.onebot.websocket")
    logger.disabled = True
    logging.getLogger("napcat.connection").disabled = True
    return dict(additional_headers={"Authorization": "Bearer " + token},
                open_timeout=timeout, close_timeout=2, ping_interval=20,
                ping_timeout=20, max_size=1024 * 1024, max_queue=16,
                proxy=None, logger=logger)


async def action(connection, name, params=None, timeout=10.0):
    response = await connection.send({"action": name, "params": params or {}}, timeout=timeout)
    if (not isinstance(response, dict) or response.get("status") != "ok"
            or type(response.get("retcode")) is not int or response["retcode"] != 0
            or not isinstance(response.get("data"), dict)):
        raise OneBotError("onebot_action_rejected")
    return response["data"]


async def authenticate(connection, expected=None, timeout=10.0):
    result = await action(connection, "get_login_info", timeout=timeout)
    actual = identifier(result.get("user_id"))
    if actual is None or (expected is not None and actual != expected):
        raise OneBotError("onebot_backend_identity_mismatch")
    return actual


@asynccontextmanager
async def connect_once(url, token, expected=None, timeout=10.0):
    from websockets.asyncio.client import connect
    from ._vendor.napcat_sdk.connection import Connection
    async with connect(url, **websocket_options(token, timeout)) as ws:
        async with Connection(ws) as connection:
            actual = await authenticate(connection, expected, timeout)
            yield connection, actual


def check_configuration(config, *, connect=False):
    output = dict(schema_version=1, protocol_version=11, client_version=None,
                  notification_configured=config.configured, relay_configured=config.relay_configured,
                  backend_verified=False, error=None)
    try:
        import websockets
        from ._vendor.napcat_sdk.connection import Connection  # noqa: F401
        output["client_version"] = websockets.__version__
        if websockets.__version__ != "15.0.1":
            raise OneBotError("onebot_client_version_unsupported")
        if connect:
            if not config.configured:
                raise OneBotError("onebot_configuration_incomplete")
            async def verify():
                async with connect_once(config.ws_url, config.access_token, config.self_id):
                    pass
            asyncio.run(verify())
            output["backend_verified"] = True
    except Exception as error:
        output["error"] = str(error) if isinstance(error, OneBotError) else "onebot_check_failed"
    return output


class OneBotApi:
    def __init__(self, config, timeout=10.0):
        if not config.configured:
            raise OneBotError("onebot_configuration_incomplete")
        self.config, self.timeout = config, timeout

    def send(self, text, operation_id, *, reply_to=None):
        if not isinstance(text, str) or not text.strip():
            raise OneBotError("onebot_message_invalid")
        async def perform():
            config = self.config
            async with connect_once(config.ws_url, config.access_token, config.self_id, self.timeout) as (connection, _):
                segments = [{"type": "text", "data": {"text": text}}]
                if reply_to is not None:
                    if identifier(reply_to, message=True) is None:
                        raise OneBotError("onebot_reply_address_invalid")
                    segments.insert(0, {"type": "reply", "data": {"id": reply_to}})
                params = {"message_type": config.chat_type, "message": segments,
                          ("group_id" if config.chat_type == "group" else "user_id"): int(config.chat_id)}
                result = await action(connection, "send_msg", params, self.timeout)
                message_id = identifier(result.get("message_id"), message=True)
                if message_id is None:
                    raise OneBotError("onebot_response_identity_invalid")
                return {"chat_id": config.destination, "message_id": message_id}
        try:
            # operation_id is fenced by the durable WatchDog ledger. OneBot has
            # no standard idempotency key; never retry a send or invent one.
            return asyncio.run(perform())
        except OneBotError:
            raise
        except Exception:
            raise OneBotError("onebot_message_outcome_uncertain") from None


class OneBotConnection:
    """An independently reconnecting observer; actions are never replayed."""
    def __init__(self, config, callback, timeout=10.0, *, runtime=None):
        if not config.configured:
            raise OneBotError("onebot_configuration_incomplete")
        self.config, self.callback, self.timeout = config, callback, timeout
        self.runtime = Path(runtime) if runtime is not None else None
        self._thread = self._loop = self._task = None
        self._stopping = threading.Event()
        self._last_health = None

    def _health(self, status, reason=None):
        if self.runtime is not None:
            now = time.monotonic()
            if self._last_health is not None and self._last_health[:2] == (status, reason) and now - self._last_health[2] < 10:
                return
            try:
                InstructionStore._atomic_json(self.runtime / "onebot" / self.config.scope / "health.json",
                    dict(schema_version=1, status=status, reason=reason, observed_at=utc_now()))
                self._last_health = (status, reason, now)
            except OSError:
                pass

    async def _observe(self):
        from websockets.asyncio.client import connect
        from ._vendor.napcat_sdk.connection import Connection
        if self._stopping.is_set():
            return
        class ObservedConnection(Connection):
            dropped = 0

            def _dispatch(self, queues, item):
                if isinstance(item, dict) and any(queue.full() for queue in queues):
                    self.dropped += 1
                super()._dispatch(queues, item)
        config = self.config
        # Upstream owns handshake retry/backoff. Authentication/read failures
        # after a handshake remain observation-only retries with a finite delay.
        async for ws in connect(config.ws_url, process_exception=lambda error: None,
                                **websocket_options(config.access_token, self.timeout)):
            if self._stopping.is_set():
                await ws.close()
                return
            try:
                async with ObservedConnection(ws) as connection:
                    await authenticate(connection, config.self_id, self.timeout)
                    self._health("connected")
                    dropped = 0
                    async for payload in connection.events():
                        if self._stopping.is_set():
                            return
                        try:
                            await asyncio.to_thread(self.callback, payload)
                        except Exception:
                            self._health("recovering", "onebot_event_processing_failed")
                        else:
                            if connection.dropped != dropped:
                                self._health("recovering", "onebot_event_backlog_dropped")
                                dropped = connection.dropped
                            else:
                                self._health("connected")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            self._health("recovering", "onebot_connection_lost")
            await asyncio.sleep(1)

    def start(self):
        if self._thread is not None:
            return
        self._stopping.clear()
        self._health("recovering", "onebot_connecting")
        def run():
            async def main():
                self._loop = asyncio.get_running_loop()
                self._loop.set_exception_handler(lambda loop, context: None)
                self._task = asyncio.current_task()
                try:
                    await self._observe()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    self._health("recovering", "onebot_transport_unavailable")
                finally:
                    if self._stopping.is_set():
                        self._health("stopped")
            asyncio.run(main())
        self._thread = threading.Thread(target=run, name="watchdog-onebot", daemon=True)
        self._thread.start()

    def close(self):
        if self._thread is None:
            return
        self._stopping.set()
        if self._loop is not None and self._loop.is_running() and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        self._thread.join(self.timeout + 5)
        if self._thread.is_alive():
            raise OneBotError("onebot_connection_shutdown_uncertain")
        self._thread = self._loop = self._task = None
