"""Authenticated Slack bot identities and bounded, explicit bot commands."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

from .models import MAX_PROMPT_CHARS


_IDS = {
    "team_id": re.compile(r"T[A-Z0-9]{8,}"),
    "user_id": re.compile(r"[UW][A-Z0-9]{8,}"),
    "bot_id": re.compile(r"B[A-Z0-9]{8,}"),
    "app_id": re.compile(r"A[A-Z0-9]{8,}"),
}
_MENTION = re.compile(r"<@([UW][A-Z0-9]{8,})(?:\|[^>\r\n]*)?>")
_HEADER = re.compile(r"!codex ([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(\n| -- )")
_RESERVED = frozenset(("add", "remove", "access", "bind", "unbind", "bot", "!codex"))
_TRANSIENT_ERRORS = frozenset((
    "ratelimited", "rate_limited", "internal_error", "fatal_error",
    "service_unavailable", "request_timeout",
))
_UNSAFE_CONTENT = frozenset((
    "edited", "deleted", "hidden", "deleted_ts", "message", "previous_message",
    "attachments", "files", "forwarded", "is_forwarded", "forwarded_from",
    "message_changed", "message_deleted",
))


def _valid_id(kind: str, value: Any) -> bool:
    return isinstance(value, str) and _IDS[kind].fullmatch(value) is not None


@dataclass(frozen=True)
class BotPrincipal:
    team_id: str
    user_id: str
    bot_id: str
    app_id: str

    def __post_init__(self) -> None:
        if any(not _valid_id(key, getattr(self, key)) for key in _IDS):
            raise ValueError("bot_principal_invalid")

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in _IDS}

    @classmethod
    def from_dict(cls, value: Any) -> "BotPrincipal":
        if not isinstance(value, dict) or set(value) != set(_IDS):
            raise ValueError("bot_principal_invalid")
        return cls(**value)


class BotVerificationDeferred(RuntimeError):
    """No admission occurred; the same provider message may be retried later."""

    def __init__(self) -> None:
        super().__init__("bot_verify_deferred")


def _response(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("bot_verify_malformed_response")
    if value.get("ok") is not True:
        error = value.get("error")
        if isinstance(error, str) and error in _TRANSIENT_ERRORS:
            raise BotVerificationDeferred()
        if error == "missing_scope":
            raise ValueError("bot_verify_missing_scope")
        raise ValueError("bot_verify_api_error")
    return value


def _call(api: Any, method: str, params: dict) -> dict:
    try:
        value = api(method, params)
    except BotVerificationDeferred:
        raise
    except HTTPError as exc:
        if exc.code == 429 or 500 <= exc.code <= 599:
            raise BotVerificationDeferred() from None
        raise ValueError("bot_verify_api_error") from None
    except (URLError, OSError):
        raise BotVerificationDeferred() from None
    except ValueError as exc:
        if str(exc) in ("bot_verify_missing_scope", "bot_verify_malformed_response"):
            raise ValueError(str(exc)) from None
        raise ValueError("bot_verify_api_error") from None
    except Exception:
        raise ValueError("bot_verify_api_error") from None
    return _response(value)


def bot_api_call(token: str, method: str, params: dict) -> dict:
    """Read only bounded identity metadata; never put tokens in URLs or errors."""
    permitted = {"auth.test": (), "users.info": ("user",), "bots.info": ("bot",)}
    if (not isinstance(token, str) or re.fullmatch(r"xoxb-[A-Za-z0-9-]+", token) is None
            or not isinstance(method, str) or method not in permitted or not isinstance(params, dict)
            or set(params) != set(permitted[method])):
        raise ValueError("bot_verify_api_error")
    if ((method == "users.info" and not _valid_id("user_id", params["user"]))
            or (method == "bots.info" and not _valid_id("bot_id", params["bot"]))):
        raise ValueError("bot_verify_api_error")
    url = "https://slack.com/api/" + method
    if params:
        url += "?" + urlencode(params)
    request = Request(url, data=b"" if method == "auth.test" else None,
                      headers={"Authorization": "Bearer " + token})

    def read(_method, _params):
        with urlopen(request, timeout=10) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("bot_verify_malformed_response")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError("bot_verify_malformed_response") from None

    return _call(read, method, params)


def _agrees(value: dict, key: str, expected: str) -> bool:
    """Supplied metadata, including null/empty values, must agree exactly."""
    return key not in value or value[key] == expected


class BotVerifier:
    def __init__(self, api: Any):
        self.api = api
        self._context: Optional[BotPrincipal] = None

    def _user(self, user_id: str, team_id: str) -> dict:
        if not _valid_id("user_id", user_id):
            raise ValueError("bot_verify_user_invalid")
        user = _call(self.api, "users.info", {"user": user_id}).get("user")
        if not isinstance(user, dict):
            raise ValueError("bot_verify_malformed_response")
        if user.get("id") != user_id or user.get("team_id") != team_id:
            raise ValueError("bot_verify_identity_mismatch")
        if user.get("deleted") is not False or user.get("suspended", False) is not False:
            raise ValueError("bot_verify_inactive")
        profile = user.get("profile", {})
        if not isinstance(profile, dict) or not _agrees(profile, "team", team_id):
            raise ValueError("bot_verify_identity_mismatch")
        return user

    def _bot(self, user_id: str, team_id: str) -> BotPrincipal:
        user = self._user(user_id, team_id)
        if user.get("is_bot") is not True:
            raise ValueError("bot_verify_not_bot")
        profile = user.get("profile", {})
        principal = BotPrincipal(team_id, user_id, profile.get("bot_id"), profile.get("api_app_id"))
        if (not _agrees(user, "bot_id", principal.bot_id)
                or not _agrees(user, "api_app_id", principal.app_id)):
            raise ValueError("bot_verify_identity_mismatch")
        bot = _call(self.api, "bots.info", {"bot": principal.bot_id}).get("bot")
        if not isinstance(bot, dict):
            raise ValueError("bot_verify_malformed_response")
        if (bot.get("id") != principal.bot_id or bot.get("user_id") != user_id
                or bot.get("app_id") != principal.app_id
                or not _agrees(bot, "team_id", team_id)):
            raise ValueError("bot_verify_identity_mismatch")
        if bot.get("deleted") is not False:
            raise ValueError("bot_verify_inactive")
        return principal

    def context(self) -> BotPrincipal:
        if self._context is None:
            auth = _call(self.api, "auth.test", {})
            if any(not _valid_id(key, auth.get(key)) for key in ("team_id", "user_id", "bot_id")):
                raise ValueError("bot_verify_context_invalid")
            principal = self._bot(auth["user_id"], auth["team_id"])
            if (principal.bot_id != auth["bot_id"]
                    or not _agrees(auth, "app_id", principal.app_id)
                    or not _agrees(auth, "api_app_id", principal.app_id)):
                raise ValueError("bot_verify_identity_mismatch")
            self._context = principal
        return self._context

    def bot(self, user_id: str) -> BotPrincipal:
        own = self.context()
        if user_id == own.user_id:
            raise ValueError("bot_verify_self")
        principal = self._bot(user_id, own.team_id)
        if principal.bot_id == own.bot_id or principal.app_id == own.app_id:
            raise ValueError("bot_verify_self")
        return principal

    def human_owner(self, user_id: str) -> str:
        own = self.context()
        user = self._user(user_id, own.team_id)
        if (user.get("is_bot") is not False or user.get("is_app_user") is not False
                or user.get("is_agentforce_bot", False) is not False
                or user.get("is_workflow_bot", False) is not False
                or user_id == own.user_id):
            raise ValueError("bot_verify_not_human")
        for metadata in (user, user.get("profile", {})):
            if any(key in metadata for key in ("bot_id", "api_app_id", "app_id")):
                raise ValueError("bot_verify_not_human")
        return user_id


def parse_bot_control(text: Any) -> Optional[tuple[str, Optional[str]]]:
    if not isinstance(text, str) or not text.strip() or text.strip().split()[0].lower() != "bot":
        return None
    if len(text) > 500 or "\n" in text or "\r" in text:
        raise ValueError("bot_command_invalid")
    parts = text.strip().split()
    if parts == ["bot", "access"]:
        return "access", None
    if len(parts) == 3 and parts[:2] in (["bot", "add"], ["bot", "remove"]):
        mention = _MENTION.fullmatch(parts[2])
        if mention is not None:
            return parts[1], mention.group(1)
    raise ValueError("bot_command_invalid")


def _plain_blocks(blocks: Any, text: str, *, allow_user_mentions: bool = False) -> bool:
    """Accept only generated rich-text sections that exactly reproduce text."""
    if not isinstance(blocks, list) or len(blocks) != 1:
        return False
    block = blocks[0]
    if (not isinstance(block, dict) or block.get("type") != "rich_text"
            or set(block) - {"type", "elements", "block_id"}
            or ("block_id" in block and not isinstance(block["block_id"], str))):
        return False
    sections = block.get("elements")
    if not isinstance(sections, list) or not sections:
        return False
    plain = []
    for section in sections:
        if (not isinstance(section, dict) or section.get("type") != "rich_text_section"
                or set(section) != {"type", "elements"}):
            return False
        elements = section["elements"]
        if not isinstance(elements, list) or not elements:
            return False
        words = []
        for element in elements:
            if (allow_user_mentions and isinstance(element, dict)
                    and element.get("type") == "user"
                    and not set(element) - {"type", "user_id", "from_llm"}
                    # Slack adds this non-visible marker to native mentions.
                    # https://docs.slack.dev/reference/block-kit/block-elements/user-element/
                    # Support the observed false shape only; it grants no authority.
                    and ("from_llm" not in element or element["from_llm"] is False)
                    and _valid_id("user_id", element.get("user_id"))):
                words.append("<@" + element["user_id"] + ">")
                continue
            if (not isinstance(element, dict) or element.get("type") != "text"
                    or not isinstance(element.get("text"), str)
                    or set(element) - {"type", "text", "style"}
                    or ("style" in element and element["style"] != {})):
                return False
            words.append(element["text"])
        plain.append("".join(words))
    rendered = "\n".join(plain)
    escaped = rendered.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text in (rendered, escaped)


def parse_bot_control_event(event: Any) -> tuple[str, Optional[str]]:
    """Controls must be original visible human text, with native mentions only."""
    if (not isinstance(event, dict) or event.get("type") != "message"
            or event.get("subtype") is not None or _UNSAFE_CONTENT.intersection(event)):
        raise ValueError("bot_command_modified")
    command = parse_bot_control(event.get("text"))
    if command is None or ("blocks" in event and not _plain_blocks(
            event["blocks"], event["text"], allow_user_mentions=True)):
        raise ValueError("bot_command_content_invalid")
    return command


def parse_bot_instruction(event: Any) -> Optional[tuple[str, str]]:
    if not isinstance(event, dict) or event.get("type") != "message":
        return None
    if event.get("subtype") not in (None, "bot_message"):
        return None
    if _UNSAFE_CONTENT.intersection(event):
        return None
    text = event.get("text")
    # The inline header is 47 characters; the legacy LF header is 44.
    if not isinstance(text, str) or len(text) > MAX_PROMPT_CHARS + 47 or "\r" in text:
        return None
    header = _HEADER.match(text)
    if header is None:
        return None
    request_id = header.group(1)
    if str(uuid.UUID(request_id)) != request_id:
        return None
    prompt = text[header.end():]
    if header.group(2) == " -- " and "\n" in prompt:
        return None
    stripped = prompt.strip()
    if not stripped or len(prompt) > MAX_PROMPT_CHARS:
        return None
    first_word = stripped.split()[0].lower()
    if first_word in _RESERVED or first_word.startswith("wd-bind-"):
        return None
    if (stripped.startswith((">", "&gt;", "`", "~~~"))
            or re.match(r"(?i)forwarded(?:\s+message)?(?:\s|:)", stripped)):
        return None
    if "blocks" in event and not _plain_blocks(event["blocks"], text):
        return None
    return request_id, prompt


def event_principal(event: Any, trusted_team_id: Any, verified: BotPrincipal) -> bool:
    """Compare inner sender metadata; receiving socket app IDs belong elsewhere."""
    if (not isinstance(event, dict) or not isinstance(verified, BotPrincipal)
            or trusted_team_id != verified.team_id or event.get("type") != "message"
            or event.get("subtype") not in (None, "bot_message")
            or event.get("user") != verified.user_id or event.get("bot_id") != verified.bot_id):
        return False
    if any(not _agrees(event, key, expected) for key, expected in (
        ("team", verified.team_id), ("team_id", verified.team_id),
        ("app_id", verified.app_id), ("api_app_id", verified.app_id),
    )):
        return False
    if "bot_profile" in event:
        profile = event["bot_profile"]
        if (not isinstance(profile, dict)
                or any(not _agrees(profile, key, expected) for key, expected in (
                    ("id", verified.bot_id), ("bot_id", verified.bot_id),
                    ("user_id", verified.user_id), ("app_id", verified.app_id),
                    ("api_app_id", verified.app_id), ("team_id", verified.team_id),
                    ("team", verified.team_id),
                )) or ("deleted" in profile and profile["deleted"] is not False)):
            return False
    return True
