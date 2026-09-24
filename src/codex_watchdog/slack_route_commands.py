"""Parse and resolve Slack route-control commands (bind/unbind).

parse_route_command: classify text as a routing command or ordinary message.
resolve_destination: validate and return an immutable Slack channel ID.
slack_conversations_get: authenticated GET helper for conversations.info/list.
"""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .slack_mapping import valid_slack_channel_id

_MAX_COMMAND_CHARS = 500
_SLACK_API_BASE = "https://slack.com/api/"
_ALLOWED_GET_METHODS = frozenset(("conversations.info", "conversations.list"))

# <#CXXXXXXXX|optional-name> or <#CXXXXXXXX>
_MENTION_RE = re.compile(r"^<#([CG][A-Z0-9]{8,})(?:\|[^>]*)?>$")
# #channelname  (1–80 chars, letters/digits/hyphens/underscores)
_HASH_NAME_RE = re.compile(r"^#([A-Za-z0-9][A-Za-z0-9_-]{0,79})$")

_RESERVED = frozenset(("bind", "unbind"))


def slack_conversations_get(token: str, method: str, params: dict) -> dict:
    """Authenticated GET for conversations.info and conversations.list only.

    Booleans in params are encoded as lowercase strings. Token is sent in the
    Authorization header; no request body is sent. Raises HTTPError(429) as-is
    for the caller's retry logic. On missing_scope raises
    ValueError('route_destination_missing_scope'); all other non-ok or
    network errors raise ValueError('route_destination_api_error') without
    including provider details.
    """
    if method not in _ALLOWED_GET_METHODS:
        raise ValueError("route_destination_api_error")
    encoded: dict = {}
    for k, v in params.items():
        if isinstance(v, bool):
            encoded[k] = "true" if v else "false"
        elif not isinstance(v, str):
            encoded[k] = str(v)
        else:
            encoded[k] = v
    url = _SLACK_API_BASE + method + "?" + urlencode(encoded)
    req = Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except HTTPError as exc:
        if exc.code == 429:
            raise
        raise ValueError("route_destination_api_error") from None
    except OSError:
        raise ValueError("route_destination_api_error") from None
    try:
        data = json.loads(raw)
    except Exception:
        raise ValueError("route_destination_api_error") from None
    if not isinstance(data, dict):
        raise ValueError("route_destination_api_error")
    if data.get("ok") is not True:
        if data.get("error") == "missing_scope":
            raise ValueError("route_destination_missing_scope")
        raise ValueError("route_destination_api_error")
    return data


def parse_route_command(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """Return (operation, argument) for a routing command, or None for ordinary text.

    Raises ValueError for a reserved command prefix with invalid syntax so the
    message never falls through to the ordinary Codex wake path.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    first_word = stripped.split()[0].lower()
    if first_word not in _RESERVED:
        return None

    # Reserved prefix found — validate strictly from here
    if "\n" in text or "\r" in text:
        raise ValueError("route_command_multiline")
    if len(text) > _MAX_COMMAND_CHARS:
        raise ValueError("route_command_oversize")

    parts = stripped.split()
    command = parts[0].lower()

    if command == "unbind":
        if len(parts) > 1:
            raise ValueError("route_command_unbind_extra_args")
        return ("unbind", None)

    # bind
    if len(parts) < 2:
        raise ValueError("route_command_bind_missing_arg")
    if len(parts) > 2:
        raise ValueError("route_command_bind_extra_args")
    arg = parts[1]
    if _MENTION_RE.fullmatch(arg) or _HASH_NAME_RE.fullmatch(arg):
        return ("bind", arg)
    raise ValueError("route_command_bind_invalid_arg")


def resolve_destination(argument: str, api) -> str:
    """Resolve a bind argument to an immutable Slack channel ID.

    api(method, params) -> dict must be fixture-injectable.  Raises ValueError
    on any API error, ambiguity, inaccessibility, or malformed response; never
    writes a route on failure.
    """
    m = _MENTION_RE.fullmatch(argument)
    if m:
        # Prefer the structured ID; never derive identity from the display name
        return _verify_channel(m.group(1), api)
    m = _HASH_NAME_RE.fullmatch(argument)
    if m:
        return _resolve_by_name(m.group(1).lower(), api)
    raise ValueError("route_command_arg_invalid")


def _verify_channel(channel_id: str, api) -> str:
    response = api("conversations.info", {"channel": channel_id})
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise ValueError("route_destination_api_error")
    channel = response.get("channel")
    if not isinstance(channel, dict):
        raise ValueError("route_destination_malformed")
    if channel.get("id") != channel_id:
        raise ValueError("route_destination_id_mismatch")
    if channel.get("is_member") is not True:
        raise ValueError("route_destination_not_member")
    if any(channel.get(flag, False) is not False
           for flag in ("is_archived", "is_read_only", "is_frozen")):
        raise ValueError("route_destination_inaccessible")
    return channel_id


def _resolve_by_name(name: str, api) -> str:
    cursor: Optional[str] = None
    matches: list[str] = []
    for _ in range(3):
        params: dict = {
            "exclude_archived": True,
            "types": "public_channel,private_channel",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        response = api("conversations.list", params)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ValueError("route_destination_api_error")
        channels = response.get("channels")
        if not isinstance(channels, list):
            raise ValueError("route_destination_malformed")
        for ch in channels:
            if (not isinstance(ch, dict) or not isinstance(ch.get("name"), str)
                    or not valid_slack_channel_id(ch.get("id"))):
                raise ValueError("route_destination_malformed")
            if ch.get("name", "").lower() == name:
                cid = ch.get("id")
                if cid:
                    matches.append(cid)
        meta = response.get("response_metadata")
        if meta is not None and not isinstance(meta, dict):
            raise ValueError("route_destination_malformed")
        next_cursor = (meta or {}).get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("route_destination_malformed")
        if not next_cursor:
            if response.get("has_more"):
                raise ValueError("route_destination_malformed")
            break
        cursor = next_cursor
    else:
        # All 3 pages exhausted but more pages remain
        raise ValueError("route_destination_name_exhausted")

    if not matches:
        raise ValueError("route_destination_name_not_found")
    if len(matches) > 1:
        raise ValueError("route_destination_name_ambiguous")
    channel_id = matches[0]
    if not valid_slack_channel_id(channel_id):
        raise ValueError("route_destination_id_invalid")
    return _verify_channel(channel_id, api)
