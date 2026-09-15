"""Read-only, authenticated discovery/history for bounded first-use pairing."""
from dataclasses import replace
import json
import re
import time

from .lark_transport import DOMAINS, LarkApi, LarkConfig, load_sdk, valid_id
from .messaging_profile import MessagingError, PREFIX


class PairingApi:
    def __init__(self, provider, values, *, client=None):
        self.provider = provider
        self.bot_user = None
        if provider == "slack":
            from slack_sdk import WebClient
            self.client = client if client is not None else WebClient(token=values[PREFIX + "SLACK_BOT_TOKEN"], timeout=10, retry_handlers=[])
            identity = self._slack("auth_test")
            self.bot_user = identity.get("user_id")
            if (not identity.get("bot_id") or not identity.get("team_id") or
                    not isinstance(self.bot_user, str) or not re.fullmatch(r"[UW][A-Z0-9]{8,}", self.bot_user)):
                raise MessagingError("messaging_slack_bot_identity_unavailable")
        else:
            self.config = LarkConfig(app_id=values[PREFIX + "LARK_APP_ID"],
                                    app_secret=values[PREFIX + "LARK_APP_SECRET"],
                                    domain=values[PREFIX + "LARK_DOMAIN"])
            self.client = client if client is not None else (load_sdk().Client.builder().app_id(self.config.app_id)
                           .app_secret(self.config.app_secret).domain(DOMAINS[self.config.domain]).timeout(10).build())

    def _slack(self, method, **params):
        try:
            result = getattr(self.client, method)(**params)
            if result.get("ok") is not True:
                raise ValueError()
            return result
        except Exception as error:
            # Do not print SDK exceptions, request headers or credential values.
            response = getattr(error, "response", {})
            code = response.get("error") if hasattr(response, "get") else None
            if code in ("missing_scope", "ratelimited", "invalid_auth", "token_revoked", "not_authed",
                        "no_permission", "not_allowed_token_type", "channel_not_found"):
                raise MessagingError("messaging_slack_" + code) from None
            raise MessagingError("messaging_slack_api_unavailable_check_permissions_or_rate_limit") from None

    def conversations(self):
        result, cursor, seen = [], None, set()
        # Only enumerate this bot's groups/channels; never scan their history.
        for _ in range(20):
            if self.provider == "slack":
                page = self._slack("users_conversations", types="public_channel,private_channel",
                                   exclude_archived=True, limit=100, **({"cursor": cursor} if cursor else {}))
                items = page.get("channels")
                more = page.get("response_metadata", {}).get("next_cursor") or None
            else:
                from lark_channel.api.im.v1.model.list_chat_request import ListChatRequest
                request = ListChatRequest.builder().page_size(100)
                if cursor:
                    request.page_token(cursor)
                try:
                    response = self.client.im.v1.chat.list(request.build())
                    if not response.success() or not 200 <= response.raw.status_code < 300:
                        if type(response.code) is int:
                            raise MessagingError("messaging_lark_chat_list_error_" + str(response.code))
                        raise ValueError()
                    page = json.loads(response.raw.content)["data"]
                    items = page.get("items")
                    more = page.get("page_token") if page.get("has_more") is True else None
                    if page.get("has_more") is True and not more:
                        raise ValueError()
                except MessagingError:
                    raise
                except Exception:
                    raise MessagingError("messaging_lark_chat_list_unavailable_check_permissions_or_rate_limit") from None
            if not isinstance(items, list):
                raise MessagingError("messaging_conversation_list_invalid")
            for item in items:
                if not isinstance(item, dict):
                    raise MessagingError("messaging_conversation_list_invalid")
                chat = item.get("id" if self.provider == "slack" else "chat_id")
                valid = (isinstance(chat, str) and re.fullmatch(r"[CG][A-Z0-9]{8,}", chat)
                         if self.provider == "slack" else valid_id(chat, "oc"))
                if not valid or not isinstance(item.get("name", ""), str):
                    raise MessagingError("messaging_conversation_list_invalid")
                result.append((chat, item.get("name") or "Unnamed conversation"))
            if not more:
                return list(dict.fromkeys(result))
            if not isinstance(more, str) or more in seen:
                raise MessagingError("messaging_conversation_pagination_invalid")
            seen.add(more)
            cursor = more
        raise MessagingError("messaging_conversation_list_too_large")

    def history(self, chat, start, end, cursor=None):
        if self.provider == "slack":
            page = self._slack("conversations_history", channel=chat, oldest=str(start), latest=str(end),
                               inclusive=True, limit=100, **({"cursor": cursor} if cursor else {}))
            more = page.get("response_metadata", {}).get("next_cursor") or None
            if page.get("has_more") and not more:
                raise MessagingError("messaging_history_pagination_invalid")
            return page.get("messages"), more
        api = LarkApi(replace(self.config, chat_id=chat), client=self.client)
        page = api.history(int(start), int(end) + 1, cursor)
        more = page.get("page_token") if page.get("has_more") is True else None
        if page.get("has_more") is True and not more:
            raise MessagingError("messaging_history_pagination_invalid")
        return page.get("items"), more

    def check_history(self, chat):
        # Verify the selected conversation's required API before showing a code.
        now = time.time()
        self.history(chat, now, now)

    def check_replies(self, chat, message_id):
        if self.provider == "slack":
            # Some app/token policies restrict thread history independently.
            # Never claim successful reply setup without checking this endpoint.
            self._slack("conversations_replies", channel=chat, ts=message_id, limit=1)


def select_conversation(api, *, read, output):
    try:
        conversations = api.conversations()
    except MessagingError as error:
        if str(error) not in ("messaging_slack_missing_scope", "messaging_lark_chat_list_error_99991672"):
            raise
        # Older apps may have message access without group-list access. Ask
        # only for the undiscoverable ID; the nonce still learns the human.
        output("This app cannot list conversations. Keep its permissions unchanged and supply the intended conversation ID, or cancel and grant list access.")
        chat = read("Channel ID (C.../G...): " if api.provider == "slack" else "Conversation ID (oc_...): ").strip()
        valid = (isinstance(chat, str) and re.fullmatch(r"[CG][A-Z0-9]{8,}", chat)
                 if api.provider == "slack" else valid_id(chat, "oc"))
        if not valid:
            raise MessagingError("messaging_conversation_invalid")
        return chat
    for index, (chat, name) in enumerate(conversations, 1):
        safe_name = "".join(c for c in name if c.isprintable())[:100]
        output(str(index) + ". " + safe_name + " [" + chat[-8:] + "]")
    if api.provider == "lark":
        output("0. Existing direct conversation (advanced: chat ID required; the group-list API cannot discover it)")
    if not conversations and api.provider == "slack":
        raise MessagingError("messaging_no_channels_invite_bot_then_retry")
    choice = read("Choose the conversation to pair: ").strip()
    if choice == "0" and api.provider == "lark":
        chat = read("Existing direct conversation chat ID (oc_...): ").strip()
        if not valid_id(chat, "oc"):
            raise MessagingError("messaging_conversation_invalid")
        return chat
    if not choice.isdigit() or not 1 <= int(choice) <= len(conversations):
        raise MessagingError("messaging_conversation_choice_invalid")
    return conversations[int(choice) - 1][0]
