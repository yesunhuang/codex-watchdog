"""Add OneBot through the existing current-user configuration/secret boundaries."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import getpass
import json
import os
from pathlib import Path
import secrets
import time

from .messaging_profile import (
    PREFIX, SELECTOR, TRANSPORTS, MessagingError, SecretStore, config_directory,
    identity_keys, json_bytes, linux_environment, private_directory, read_object,
    slack_complete, lark_complete, write_new,
)
from .onebot_relay import human_message
from .onebot_transport import OneBotConfig, OneBotError, connect_once, valid_endpoint
from .storage import FileLock

SERVICE = "org.localcodexwatchdog.onebot"
ACCOUNT = "onebot-access-token"
PROFILE = "onebot-relay.json"
ENVIRONMENT = "onebot-notifications.env"


def route_environment(route):
    if (type(route.get("schema_version")) is not int or route["schema_version"] != 1
            or type(route.get("protocol_version")) is not int or route["protocol_version"] != 11
            or not isinstance(route.get("allowed_user_ids"), list)
            or not all(isinstance(user, str) for user in route["allowed_user_ids"])):
        raise MessagingError("onebot_saved_profile_invalid")
    values = {"WS_URL": route.get("ws_url"), "SELF_ID": route.get("self_id"),
              "CHAT_TYPE": route.get("chat_type"), "CHAT_ID": route.get("chat_id"),
              "ALLOWED_USER_IDS": ",".join(route["allowed_user_ids"])}
    probe = {PREFIX + "ONEBOT_" + key: value for key, value in values.items()}
    probe[PREFIX + "ONEBOT_ACCESS_TOKEN"] = "probe"
    if not OneBotConfig.from_environment(probe).configured:
        raise MessagingError("onebot_saved_profile_invalid")
    return {PREFIX + "ONEBOT_" + key: value for key, value in values.items()}


def with_onebot(env):
    current = env.get(SELECTOR)
    if current not in TRANSPORTS:
        current = "lark" if lark_complete(env) and not slack_complete(env) else "slack" if slack_complete(env) else "onebot"
    wanted = set(TRANSPORTS[current]) | {"onebot"}
    return next(name for name, providers in TRANSPORTS.items() if set(providers) == wanted)


def load_onebot(environment, root, store, *, secrets=True, selector_explicit=False):
    env = dict(environment)
    explicit = identity_keys(env, "onebot")
    path = root / PROFILE
    if explicit:
        if not OneBotConfig.from_environment(env).configured:
            raise MessagingError("messaging_partial_explicit_onebot_environment")
        if not selector_explicit:
            env[SELECTOR] = with_onebot(env)
        return env
    if not path.exists():
        return env
    route = read_object(path)
    values = route_environment(route)
    if store.platform.startswith("linux"):
        stored = linux_environment(root / ENVIRONMENT)
        if set(stored) != {PREFIX + "ONEBOT_ACCESS_TOKEN"} or not stored[PREFIX + "ONEBOT_ACCESS_TOKEN"]:
            raise MessagingError("onebot_saved_credential_invalid")
        token = stored[PREFIX + "ONEBOT_ACCESS_TOKEN"] if secrets else "probe"
    else:
        if not store.has(SERVICE, ACCOUNT):
            raise MessagingError("onebot_saved_profile_incomplete")
        token = store.get(SERVICE, ACCOUNT, username=route["self_id"]) if secrets else "probe"
    values[PREFIX + "ONEBOT_ACCESS_TOKEN"] = token
    env.update(values)
    if not selector_explicit:
        selection = route.get("interactive_transport")
        if selection not in TRANSPORTS or "onebot" not in TRANSPORTS[selection]:
            raise MessagingError("onebot_saved_selection_invalid")
        env[SELECTOR] = selection
    return env


def pair_onebot(url, token, *, output=print, timeout=600):
    nonce = "PAIR_CODEX_ONEBOT_" + secrets.token_hex(16)
    began = int(time.time())
    async def collect():
        async with connect_once(url, token) as (connection, self_id):
            output("Send this exact confirmation as a new message to your QQ bot or intended group:")
            output(nonce)
            output("Waiting up to 10 minutes; the chat and authorized human will be learned automatically.")
            async for payload in connection.events():
                value = human_message(payload, self_id)
                stamp = payload.get("time") if isinstance(payload, dict) else None
                if (value is not None and value["text"] == nonce and value["reply_to"] is None
                        and type(stamp) is int and began - 2 <= stamp <= time.time() + 30):
                    kind, chat = value["chat_id"].split(":")
                    return dict(schema_version=1, protocol_version=11, ws_url=url, self_id=self_id,
                                chat_type=kind, chat_id=chat, allowed_user_ids=[value["user_id"]])
            raise OneBotError("onebot_pairing_connection_lost")
    try:
        return asyncio.run(asyncio.wait_for(collect(), timeout=timeout))
    except OneBotError:
        raise
    except Exception:
        raise OneBotError("onebot_pairing_failed_or_expired") from None


def setup_onebot(*, environment=None, root=None, runtime=None, store=None, check=False,
                 is_interactive=None, read=None, secret=None, output=print,
                 pair=None, guarded=False):
    from .messaging_setup import interactive
    from .messaging_profile import load_saved
    env = dict(os.environ if environment is None else environment)
    root = config_directory(env) if root is None else Path(root)
    store = store or SecretStore(root, environment=env)
    read, secret = read or input, secret or getpass.getpass
    pair = pair or pair_onebot
    try:
        loaded = load_onebot(env, root, store, secrets=False, selector_explicit=SELECTOR in env)
        configured = OneBotConfig.from_environment(loaded).relay_configured
        partial = (any(identity_keys(env, "onebot")) or (root / PROFILE).exists()
                   or (root / ENVIRONMENT).exists() or store.has(SERVICE, ACCOUNT))
        if check:
            output(json.dumps(dict(schema_version=1, provider="onebot", configured=configured,
                                   existing_or_partial=bool(partial and not configured)), sort_keys=True))
            return 0 if configured or not partial else 1
        if configured:
            output("OneBot is configured; existing pairings and credentials are unchanged.")
            return 0
        if partial:
            raise MessagingError("onebot_partial_setup_preserved_requires_review")
        if not (interactive() if is_interactive is None else is_interactive):
            raise MessagingError("onebot_setup_requires_interactive_terminal")
        private_directory(root)
        with nullcontext() if guarded else FileLock(root / "setup.lock"):
            if (root / PROFILE).exists() or (root / ENVIRONMENT).exists() or store.has(SERVICE, ACCOUNT):
                raise MessagingError("onebot_setup_changed_concurrently")
            # Reuse the current provider selection without copying its secrets.
            prior = load_saved(env, root, store, secrets=False)
            url = read("OneBot 11 WebSocket address (ws:// or wss://, no token in URL): ").strip()
            token = secret("OneBot access token (hidden): ").strip()
            if not valid_endpoint(url) or not token or any(ord(c) < 32 for c in token):
                raise MessagingError("onebot_setup_credentials_invalid")
            route = pair(url, token, output=output)
            if route.get("ws_url") != url:
                raise MessagingError("onebot_pairing_endpoint_mismatch")
            route_environment(route)
            route = dict(route, interactive_transport=with_onebot(prior))
            if (root / PROFILE).exists() or (root / ENVIRONMENT).exists() or store.has(SERVICE, ACCOUNT):
                raise MessagingError("onebot_setup_changed_concurrently")
            if store.platform.startswith("linux"):
                write_new(root / ENVIRONMENT, ("# OneBot credential; schema_version=1\n" +
                    PREFIX + "ONEBOT_ACCESS_TOKEN=" + json.dumps(token) + "\n").encode())
            else:
                store.put(SERVICE, ACCOUNT, token, username=route["self_id"])
            write_new(root / PROFILE, json_bytes(route))
            output("OneBot pairing saved. Quote/reply to a WatchDog message to target its exact Codex thread.")
            return 0
    except (KeyboardInterrupt, EOFError):
        output("OneBot setup cancelled. Existing provider settings were preserved.")
        return 130
    except Exception as error:
        reason = str(error) if isinstance(error, (MessagingError, OneBotError)) else "onebot_setup_failed"
        output(reason + ". Existing provider settings were preserved.")
        return 1
