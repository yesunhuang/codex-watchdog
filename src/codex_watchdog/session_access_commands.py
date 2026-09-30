"""Bounded native mention parsing and exact human identity verification for exact-session access commands."""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .onebot_transport import identifier as _onebot_identifier
from .slack_mapping import valid_slack_user_id

_MAX_COMMAND_CHARS = 500
_SLACK_API_BASE = "https://slack.com/api/"
_RESERVED_WORDS = frozenset(("add", "remove", "access", "bind", "unbind"))
_ACCESS_OPS = frozenset(("add", "remove", "access"))
_WD_BIND_PREFIX = "wd-bind-"
_ALLOWED_USER_INFO_METHODS = frozenset(("users.info",))
_SLACK_BOT_IDENTITY = "USLACKBOT"

# <@U12345678> or <@U12345678|display-label> – display label allowed but ignored
_SLACK_USER_MENTION_RE = re.compile(r"^<@([UW][A-Z0-9]{8,})(?:\|[^>\n]*)?>$")
_LARK_PLACEHOLDER_RE = re.compile(r"^@_user_\d+$")


def reserved_control(text: str) -> bool:
    """True if text starts with a reserved access/routing word or WD-BIND- prefix."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    first_word = stripped.split()[0].lower()
    return first_word in _RESERVED_WORDS or first_word.startswith(_WD_BIND_PREFIX)


def parse_slack_access(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """Parse an exact-session Slack access command.

    Returns (operation, user_id) for add/remove, ("access", None) for access,
    or None for ordinary text.  Raises ValueError with a fixed code for any
    reserved-word command that does not meet the strict structure requirements.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    first_word = stripped.split()[0].lower()
    if first_word not in _ACCESS_OPS:
        return None

    # Reserved prefix confirmed – validate strictly from here; never fall through.
    if "\n" in text or "\r" in text:
        raise ValueError("access_command_multiline")
    if len(text) > _MAX_COMMAND_CHARS:
        raise ValueError("access_command_oversize")

    parts = stripped.split()
    command = parts[0].lower()

    if command == "access":
        if len(parts) > 1:
            raise ValueError("access_command_unexpected_args")
        return ("access", None)

    # add or remove
    if len(parts) < 2:
        raise ValueError("access_command_missing_target")
    if len(parts) > 2:
        raise ValueError("access_command_extra_targets")

    m = _SLACK_USER_MENTION_RE.fullmatch(parts[1])
    if not m:
        raise ValueError("access_command_invalid_mention")

    return (command, m.group(1))


def parse_lark_access(
    text: str, mentions: list
) -> Optional[Tuple[str, Optional[str]]]:
    """Parse an exact-session Lark access command.

    mentions is a list of raw dicts from either socket (id is a dict with open_id)
    or history (id is a string, id_type is 'open_id') events.  Returns
    (operation, open_id) for add/remove, ("access", None) for access, or None for
    ordinary text.  Raises ValueError for any reserved-word command that fails
    structure or identity requirements.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    first_word = stripped.split()[0].lower()
    if first_word not in _ACCESS_OPS:
        return None

    # Reserved prefix confirmed.
    if "\n" in text or "\r" in text:
        raise ValueError("access_command_multiline")
    if len(text) > _MAX_COMMAND_CHARS:
        raise ValueError("access_command_oversize")

    parts = stripped.split()
    command = parts[0].lower()

    if command == "access":
        if len(parts) > 1:
            raise ValueError("access_command_unexpected_args")
        if mentions is not None and (not isinstance(mentions, list) or mentions):
            raise ValueError("access_command_unexpected_mentions")
        return ("access", None)

    # add or remove
    if not isinstance(mentions, list) or not mentions:
        raise ValueError("access_command_missing_mention")

    # Validate and deduplicate keys
    seen_keys: set = set()
    for m in mentions:
        if not isinstance(m, dict):
            raise ValueError("access_command_malformed_mention")
        k = m.get("key")
        if not isinstance(k, str) or not _LARK_PLACEHOLDER_RE.fullmatch(k):
            raise ValueError("access_command_malformed_mention")
        if k in seen_keys:
            raise ValueError("access_command_duplicate_mention_key")
        seen_keys.add(k)

    if len(mentions) != 1:
        raise ValueError("access_command_multiple_mentions")

    mention = mentions[0]
    key = mention["key"]

    # The placeholder must appear as the sole argument after the command word.
    text_rest = stripped[len(parts[0]):].strip()
    if text_rest != key:
        raise ValueError("access_command_extra_content")

    mention_id = mention.get("id")
    id_type = mention.get("id_type")

    if isinstance(mention_id, dict):
        # Socket event form: id is a nested object; id_type must NOT be present.
        if id_type is not None:
            raise ValueError("access_command_conflicting_mention_form")
        if "app_id" in mention_id:
            raise ValueError("access_command_bot_mention")
        open_id = mention_id.get("open_id")
        if not isinstance(open_id, str):
            raise ValueError("access_command_malformed_mention")
    elif isinstance(mention_id, str):
        # History poll form: id is the open_id string; id_type must be 'open_id'.
        if id_type != "open_id":
            raise ValueError("access_command_wrong_id_type")
        open_id = mention_id
    else:
        raise ValueError("access_command_malformed_mention")

    # Require a real contact user open_id (ou_ prefix and valid character set).
    from .lark_transport import valid_id
    if not valid_id(open_id, "ou"):
        raise ValueError("access_command_invalid_user_id")

    return (command, open_id)


def _validate_onebot_qq(qq) -> str:
    """Return the canonical OneBot user qq string, or raise ValueError."""
    if isinstance(qq, bool) or isinstance(qq, float):
        raise ValueError("access_command_invalid_at_id")
    if isinstance(qq, int):
        val = _onebot_identifier(str(qq))
        if val is None:
            raise ValueError("access_command_invalid_at_id")
        return val
    if isinstance(qq, str):
        if qq == "all":
            raise ValueError("access_command_all_target")
        val = _onebot_identifier(qq)
        if val is None:
            raise ValueError("access_command_invalid_at_id")
        return val
    raise ValueError("access_command_invalid_at_id")


def parse_onebot_access(
    segments: list, self_id: str
) -> Optional[Tuple[str, Optional[str]]]:
    """Parse an exact-session OneBot 11 access command from array-form segments.

    Accepts structured text/reply/at segments only.  A leading reply and at most
    one immediately following bot-self at may be silently consumed as envelope.
    Returns (operation, user_id) for add/remove, ("access", None) for access, or
    None for ordinary text.  Raises ValueError for reserved-word commands that do
    not meet the strict requirements.
    """
    if not isinstance(segments, list) or not isinstance(self_id, str):
        return None

    idx = 0

    # Consume optional leading reply envelope.
    has_reply = False
    if idx < len(segments):
        seg = segments[idx]
        if isinstance(seg, dict) and seg.get("type") == "reply":
            data = seg.get("data")
            if not isinstance(data, dict):
                raise ValueError("access_command_malformed_segment")
            if _onebot_identifier(data.get("id"), message=True) is None:
                raise ValueError("access_command_malformed_segment")
            has_reply = True
            idx += 1

    # If there was a reply, optionally consume an immediately following bot-self at.
    if has_reply and idx < len(segments):
        seg = segments[idx]
        if isinstance(seg, dict) and seg.get("type") == "at":
            data = seg.get("data")
            if isinstance(data, dict):
                qq = data.get("qq")
                try:
                    qq_str = _validate_onebot_qq(qq)
                except ValueError:
                    qq_str = None
                if qq_str is not None and qq_str == self_id:
                    idx += 1

    remaining = segments[idx:]

    text_before: list = []
    text_after: list = []
    at_segments: list = []
    at_seen = False

    for seg in remaining:
        if not isinstance(seg, dict):
            raise ValueError("access_command_malformed_segment")
        stype = seg.get("type")
        data = seg.get("data")
        if not isinstance(data, dict):
            data = {}

        if stype == "text":
            content = data.get("text", "")
            if not isinstance(content, str):
                raise ValueError("access_command_malformed_segment")
            if at_seen:
                text_after.append(content)
            else:
                text_before.append(content)
        elif stype == "at":
            at_seen = True
            qq = data.get("qq")
            qq_str = _validate_onebot_qq(qq)
            at_segments.append(qq_str)
        elif stype == "reply":
            raise ValueError("access_command_unexpected_reply")
        else:
            raise ValueError("access_command_unsupported_segment")

    full_before = "".join(text_before)
    stripped_before = full_before.strip()

    if not stripped_before:
        return None

    first_word = stripped_before.split()[0].lower()
    if first_word not in _ACCESS_OPS:
        return None

    command = first_word

    # Reserved command found – validate strictly.
    if "\n" in full_before or "\r" in full_before:
        raise ValueError("access_command_multiline")
    if len(full_before) > _MAX_COMMAND_CHARS:
        raise ValueError("access_command_oversize")

    if command == "access":
        if at_segments:
            raise ValueError("access_command_unexpected_at")
        full_text = full_before + "".join(text_after)
        if full_text.strip().lower() != "access":
            raise ValueError("access_command_unexpected_args")
        return ("access", None)

    # add or remove: exactly one at target, command word only before it, whitespace only after.
    if not at_segments:
        raise ValueError("access_command_missing_target")
    if len(at_segments) > 1:
        raise ValueError("access_command_extra_targets")

    if stripped_before.lower() != command:
        raise ValueError("access_command_extra_content_before_target")

    after_text = "".join(text_after)
    if after_text.strip():
        raise ValueError("access_command_extra_content_after_target")

    target_qq = at_segments[0]

    if target_qq == self_id:
        raise ValueError("access_command_self_target")

    return (command, target_qq)


def verify_slack_user(user_id: str, api) -> str:
    """Verify user_id is an active, non-bot Slack human.

    api(method, params) is injectable.  Calls ONLY users.info.  Raises ValueError
    with a fixed safe code on any failure.  HTTP 429 propagates unchanged for
    the poller's retry logic.
    """
    if not valid_slack_user_id(user_id):
        raise ValueError("access_verify_invalid_user_id")
    if user_id == _SLACK_BOT_IDENTITY:
        raise ValueError("access_verify_bot_user")

    try:
        response = api("users.info", {"user": user_id})
    except HTTPError as exc:
        if exc.code == 429:
            raise
        raise ValueError("access_verify_api_error") from None
    except ValueError as exc:
        if str(exc) == "access_verify_missing_scope":
            raise
        raise ValueError("access_verify_api_error") from None
    except Exception:
        raise ValueError("access_verify_api_error") from None

    if not isinstance(response, dict):
        raise ValueError("access_verify_malformed_response")
    if response.get("ok") is not True:
        if response.get("error") == "missing_scope":
            raise ValueError("access_verify_missing_scope")
        raise ValueError("access_verify_api_error")

    user = response.get("user")
    if not isinstance(user, dict):
        raise ValueError("access_verify_malformed_response")

    if user.get("id") != user_id:
        raise ValueError("access_verify_id_mismatch")
    if user.get("is_bot") is not False:
        raise ValueError("access_verify_bot_user")
    if user.get("is_app_user") is not False:
        raise ValueError("access_verify_app_user")
    if user.get("deleted") is not False:
        raise ValueError("access_verify_deleted_user")
    if user.get("bot_id") is not None:
        raise ValueError("access_verify_bot_user")
    if user.get("api_app_id") is not None:
        raise ValueError("access_verify_app_user")
    if user.get("is_agentforce_bot") is True:
        raise ValueError("access_verify_bot_user")

    return user_id


def slack_user_get(token: str, method: str, params: dict) -> dict:
    """Narrow authenticated users.info HTTP GET helper.

    Token is sent in the Authorization header only.  Only users.info is permitted.
    Raises HTTPError(429) unchanged; all other errors raise ValueError with a
    fixed safe code.
    """
    if method not in _ALLOWED_USER_INFO_METHODS:
        raise ValueError("access_verify_api_error")
    encoded = {k: (v if isinstance(v, str) else str(v)) for k, v in params.items()}
    url = _SLACK_API_BASE + method + "?" + urlencode(encoded)
    req = Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except HTTPError as exc:
        if exc.code == 429:
            raise
        raise ValueError("access_verify_api_error") from None
    except OSError:
        raise ValueError("access_verify_api_error") from None
    try:
        data = json.loads(raw)
    except Exception:
        raise ValueError("access_verify_api_error") from None
    if not isinstance(data, dict):
        raise ValueError("access_verify_api_error")
    if data.get("ok") is not True:
        if data.get("error") == "missing_scope":
            raise ValueError("access_verify_missing_scope")
        raise ValueError("access_verify_api_error")
    return data
