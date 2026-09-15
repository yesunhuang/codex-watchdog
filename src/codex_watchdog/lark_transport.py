"""Feishu/Lark configuration and the official SDK boundary; no protocol clone."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
import re
import threading
from typing import Mapping, Optional
import uuid

from .models import sha256_text


SDK_VERSION = "1.4.0"
DOMAINS = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}


def valid_id(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r"_[A-Za-z0-9_-]{8,128}", value) is not None


class LarkTransportError(RuntimeError):
    """Only fixed, nonsecret error codes may cross the SDK boundary."""


@dataclass(frozen=True, repr=False)
class LarkConfig:
    app_id: Optional[str] = field(default=None, repr=False)
    app_secret: Optional[str] = field(default=None, repr=False)
    chat_id: Optional[str] = field(default=None, repr=False)
    allowed_user_ids: tuple[str, ...] = field(default=(), repr=False)
    domain: str = "feishu"
    reply_mode: str = "poll"

    def __post_init__(self):
        if self.domain not in DOMAINS:
            raise ValueError("Lark domain must be feishu or lark")
        if self.reply_mode not in ("poll", "socket"):
            raise ValueError("Lark reply mode must be poll or socket")

    @classmethod
    def from_environment(cls, environment: Optional[Mapping[str, str]] = None):
        source = os.environ if environment is None else environment
        def value(name):
            raw = source.get("CODEX_WATCHDOG_LARK_" + name)
            return raw.strip() if isinstance(raw, str) and raw.strip() else None
        users = tuple(part for part in re.split(r"[,;\s]+", value("ALLOWED_USER_IDS") or "") if part)
        return cls(value("APP_ID"), value("APP_SECRET"), value("CHAT_ID"), users,
                   (value("DOMAIN") or "feishu").lower(), (value("REPLY_MODE") or "poll").lower())

    @property
    def present(self):
        return bool(self.app_id or self.app_secret or self.chat_id or self.allowed_user_ids)

    @property
    def configured(self):
        return (valid_id(self.app_id, "cli") and isinstance(self.app_secret, str)
                and bool(self.app_secret.strip()) and valid_id(self.chat_id, "oc")
                and all(valid_id(user, "ou") for user in self.allowed_user_ids))

    @property
    def relay_configured(self):
        return self.configured and bool(self.allowed_user_ids)

    @property
    def scope(self):
        if not self.configured:
            raise LarkTransportError("lark_configuration_incomplete")
        return sha256_text("lark\0" + self.domain + "\0" + self.app_id)

    def __repr__(self):
        return f"LarkConfig(domain={self.domain!r}, configured={self.configured}, relay_configured={self.relay_configured})"


def load_sdk():
    try:
        import lark_channel as sdk
        from lark_channel.core.const import VERSION
        from lark_channel.core.log import logger
        from lark_channel.ws.pb.pbbp2_pb2 import Frame
        if VERSION != SDK_VERSION:
            raise LarkTransportError("lark_sdk_version_mismatch")
        # SDK/server exceptions and connection URLs may contain private data.
        logger.disabled = True
        for name in ("websockets", "websockets.client", "websockets.protocol"):
            logging.getLogger(name).disabled = True
        Frame().SerializePartialToString()  # Also verifies frozen protobuf imports.
        return sdk
    except LarkTransportError:
        raise
    except Exception:
        raise LarkTransportError("lark_sdk_unavailable") from None


class LarkApi:
    def __init__(self, config: LarkConfig, timeout=10.0, *, client=None):
        if not config.configured:
            raise LarkTransportError("lark_configuration_incomplete")
        sdk = load_sdk()
        self.config = config
        self.client = client if client is not None else (
            sdk.Client.builder().app_id(config.app_id).app_secret(config.app_secret)
            .domain(DOMAINS[config.domain]).timeout(timeout).build())

    def history(self, start, end, page_token=None):
        """Read one bounded page from the authenticated, configured conversation."""
        import json
        from lark_channel.api.im.v1.model.list_message_request import ListMessageRequest
        request = (ListMessageRequest.builder().container_id_type("chat")
                   .container_id(self.config.chat_id).start_time(str(start)).end_time(str(end))
                   .sort_type("ByCreateTimeAsc").page_size(50))
        if page_token is not None:
            request.page_token(page_token)
        try:
            response = self.client.im.v1.message.list(request.build())
            if response.raw.status_code == 429:
                raise LarkTransportError("lark_history_rate_limited")
            if not response.success() or not 200 <= response.raw.status_code < 300:
                raise LarkTransportError("lark_history_unavailable_check_permissions")
            # The SDK owns authentication and HTTP. Preserve the API's JSON
            # types for whole-page validation before any reply is admitted.
            return json.loads(response.raw.content)["data"]
        except LarkTransportError:
            raise
        except Exception:
            raise LarkTransportError("lark_history_failed_or_timed_out") from None

    def send(self, text, operation_id, *, reply_to=None):
        from lark_channel.api.im.v1.model.create_message_request import CreateMessageRequest
        from lark_channel.api.im.v1.model.create_message_request_body import CreateMessageRequestBody
        from lark_channel.api.im.v1.model.reply_message_request import ReplyMessageRequest
        from lark_channel.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody
        import json
        body = json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":"))
        message_uuid = uuid.uuid5(uuid.NAMESPACE_URL, self.config.scope + "\0" + operation_id).hex
        try:
            if reply_to is None:
                request = (CreateMessageRequest.builder().receive_id_type("chat_id")
                           .request_body(CreateMessageRequestBody.builder()
                                         .receive_id(self.config.chat_id).msg_type("text")
                                         .content(body).uuid(message_uuid).build()).build())
                response = self.client.im.v1.message.create(request)
            else:
                if not valid_id(reply_to, "om"):
                    raise LarkTransportError("lark_reply_address_invalid")
                request = (ReplyMessageRequest.builder().message_id(reply_to)
                           .request_body(ReplyMessageRequestBody.builder().msg_type("text")
                                         .content(body).uuid(message_uuid).reply_in_thread(True).build()).build())
                response = self.client.im.v1.message.reply(request)
            if not response.success() or not 200 <= response.raw.status_code < 300:
                raise LarkTransportError("lark_message_rejected")
            data = response.data
            if data.chat_id != self.config.chat_id or not valid_id(data.message_id, "om"):
                raise LarkTransportError("lark_response_identity_invalid")
            return {"chat_id": data.chat_id, "message_id": data.message_id}
        except LarkTransportError:
            raise
        except Exception:
            raise LarkTransportError("lark_message_failed_or_timed_out") from None


class LarkConnection:
    """Run the SDK's public background lifecycle beside the existing service."""

    def __init__(self, config, callback, timeout=10.0, *, channel=None):
        sdk = load_sdk()
        from lark_channel.channel.config import (
            InboundConfig, MediaCapabilities, NameCacheConfig, PolicyConfig,
            SecurityConfig, TransportConfig,
        )
        self.timeout = timeout
        self.channel = channel if channel is not None else sdk.FeishuChannel(
            app_id=config.app_id, app_secret=config.app_secret, domain=DOMAINS[config.domain],
            transport=TransportConfig(kind="ws", http_timeout_seconds=timeout,
                                      handshake_timeout_seconds=timeout),
            security=SecurityConfig(mode="strict", allow_local_insecure_ws=False),
            # Routing/allowlists/admission are checked against the raw event below.
            policy=PolicyConfig(dm_policy="disabled", group_policy="disabled"),
            inbound=InboundConfig(emit_raw_events=True, expand_merge_forward=False,
                                  fetch_interactive_card=False, reaction_notifications="off",
                                  media_capabilities=MediaCapabilities(False, False, False, False, False),
                                  name_cache=NameCacheConfig(enabled=False)),
        )
        async def received(data):
            await asyncio.to_thread(callback, data)
        self.channel.on("raw", received)
        self._thread = None
        self._loop = None
        self._stop = None
        self._ready = threading.Event()
        self._failed = False

    def start(self):
        if self._thread is not None:
            return
        self._ready.clear()
        self._failed = False
        def run():
            async def lifecycle():
                self._loop = asyncio.get_running_loop()
                self._loop.set_exception_handler(lambda _loop, _context: None)
                self._stop = asyncio.Event()
                try:
                    await self.channel.start_background(timeout=self.timeout)
                    self._ready.set()
                    await self._stop.wait()
                except Exception:
                    self._failed = True
                    self._ready.set()
                finally:
                    await self.channel.stop_background()
            try:
                asyncio.run(lifecycle())
            except Exception:
                self._failed = True
                self._ready.set()
        self._thread = threading.Thread(target=run, name="watchdog-lark", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.timeout + 6) or self._failed:
            self.close()
            raise LarkTransportError("lark_connection_failed")

    def close(self):
        thread = self._thread
        if thread is None:
            return
        if self._loop is not None and self._loop.is_running() and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        thread.join(self.timeout + 6)
        if thread.is_alive():
            raise LarkTransportError("lark_connection_shutdown_uncertain")
        self._thread = None
