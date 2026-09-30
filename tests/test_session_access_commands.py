"""Bounded mention parsing and exact human identity verification for exact-session access commands."""
from types import SimpleNamespace
from urllib.error import HTTPError
import uuid

import pytest

from codex_watchdog.session_access_commands import (
    reserved_control,
    parse_slack_access,
    parse_lark_access,
    parse_onebot_access,
    verify_slack_user,
    slack_user_get,
)
from codex_watchdog.lark_transport import LarkApi, LarkConfig, LarkTransportError

# ── Fixtures ─────────────────────────────────────────────────────────────────

USER = "U12345678"
USER2 = "U99999999"
OU = "ou_abc123def456gh"
OU2 = "ou_xyz999aaa111bb"
BOT_QQ = "12345"
USER_QQ = "54321"
OTHER_QQ = "99999"

LARK_CHAT = "oc_test00000000001"
LARK_USER = "ou_test0000000001"


def lark_config():
    return LarkConfig("cli_" + uuid.uuid4().hex[:18], "secret", LARK_CHAT, (LARK_USER,))


def make_lark_api(get_fn):
    """Build a LarkApi whose contact.v3.user.get is replaced by get_fn."""
    config = lark_config()
    from codex_watchdog.lark_transport import load_sdk
    sdk = load_sdk()
    real_client = (sdk.Client.builder()
                   .app_id(config.app_id)
                   .app_secret(config.app_secret)
                   .domain("https://open.feishu.cn")
                   .timeout(2)
                   .build())
    real_client.contact = SimpleNamespace(
        v3=SimpleNamespace(
            user=SimpleNamespace(get=get_fn)
        )
    )
    return LarkApi(config, client=real_client)


def fake_user_response(open_id, *, status_code=200, success=True, wrong_id=False,
                       frozen=False, resigned=False, missing_data=False):
    status = None
    if frozen or resigned:
        status = SimpleNamespace(is_frozen=frozen, is_resigned=resigned)
    if missing_data:
        data = None
    else:
        user = SimpleNamespace(open_id=(OU2 if wrong_id else open_id), status=status)
        data = SimpleNamespace(user=user)
    raw = SimpleNamespace(status_code=status_code)
    return SimpleNamespace(success=lambda: success, data=data, raw=raw)


# ── reserved_control ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "add U12345678",
    "Add U12345678",
    "ADD U12345678",
    "  add something",
    "remove @user",
    "access",
    "ACCESS now",
    "bind #channel",
    "unbind",
    "UNBIND extra",
    "WD-BIND-session123",
    "wd-bind-foo",
    "WD-BIND-",          # bare prefix still reserved
    "WD-BIND-malformed garbage",
])
def test_reserved_control_detects_reserved_text(text):
    assert reserved_control(text) is True


@pytest.mark.parametrize("text", [
    "hello world",
    "admins add",
    "binder something",
    "accessor",
    "removing user",
    "",
    "   ",
    "WD-UNBIND-something",   # does not start with wd-bind-
    "addme",
])
def test_reserved_control_ignores_ordinary_text(text):
    assert reserved_control(text) is False


def test_reserved_control_non_string_is_false():
    assert reserved_control(None) is False
    assert reserved_control(42) is False
    assert reserved_control([]) is False


# ── parse_slack_access ────────────────────────────────────────────────────────

def test_parse_slack_access_add_mention():
    assert parse_slack_access("add <@U12345678>") == ("add", USER)


def test_parse_slack_access_add_mention_with_label():
    # Display label in native mention must be accepted but ignored.
    assert parse_slack_access("add <@U12345678|alice>") == ("add", USER)


def test_parse_slack_access_remove_mention():
    assert parse_slack_access("remove <@U99999999>") == ("remove", USER2)


def test_parse_slack_access_remove_mention_with_label():
    assert parse_slack_access("remove <@U99999999|bob>") == ("remove", USER2)


def test_parse_slack_access_access_no_args():
    assert parse_slack_access("access") == ("access", None)
    assert parse_slack_access("  access  ") == ("access", None)


def test_parse_slack_access_ordinary_text_returns_none():
    assert parse_slack_access("hello world") is None
    assert parse_slack_access("") is None
    assert parse_slack_access("   ") is None
    assert parse_slack_access("bind #channel") is None
    assert parse_slack_access("unbind") is None


def test_parse_slack_access_non_string_returns_none():
    assert parse_slack_access(None) is None
    assert parse_slack_access(42) is None


def test_parse_slack_access_case_insensitive_command():
    assert parse_slack_access("ADD <@U12345678>") == ("add", USER)
    assert parse_slack_access("Remove <@U99999999>") == ("remove", USER2)
    assert parse_slack_access("ACCESS") == ("access", None)


def test_parse_slack_access_multiline_raises():
    with pytest.raises(ValueError, match="access_command_multiline"):
        parse_slack_access("add\n<@U12345678>")


def test_parse_slack_access_oversize_raises():
    with pytest.raises(ValueError, match="access_command_oversize"):
        parse_slack_access("add " + "a" * 500)


def test_parse_slack_access_unexpected_args_raises():
    with pytest.raises(ValueError, match="access_command_unexpected_args"):
        parse_slack_access("access extra")


def test_parse_slack_access_missing_target_raises():
    with pytest.raises(ValueError, match="access_command_missing_target"):
        parse_slack_access("add")


def test_parse_slack_access_extra_targets_raises():
    with pytest.raises(ValueError, match="access_command_extra_targets"):
        parse_slack_access("add <@U12345678> <@U99999999>")


def test_parse_slack_access_invalid_mention_format_raises():
    # Raw opaque ID without <@...> wrapper must be rejected.
    with pytest.raises(ValueError, match="access_command_invalid_mention"):
        parse_slack_access("add U12345678")
    with pytest.raises(ValueError, match="access_command_invalid_mention"):
        parse_slack_access("add @alice")
    with pytest.raises(ValueError, match="access_command_invalid_mention"):
        parse_slack_access("add <#C12345678>")


def test_parse_slack_access_existing_admin_mention_allowed():
    # Parsers allow mention of any valid user ID; admin check is at integration layer.
    assert parse_slack_access("add <@U12345678>") == ("add", "U12345678")


# ── parse_lark_access ─────────────────────────────────────────────────────────

def socket_mention(key="@_user_1", open_id=OU, extra_id_fields=None, id_type=None):
    id_dict = {"open_id": open_id}
    if extra_id_fields:
        id_dict.update(extra_id_fields)
    m = {"key": key, "id": id_dict, "name": "Test User"}
    if id_type is not None:
        m["id_type"] = id_type
    return m


def history_mention(key="@_user_1", open_id=OU, id_type="open_id"):
    return {"key": key, "id": open_id, "id_type": id_type, "name": "Test User"}


def test_parse_lark_access_add_socket_form():
    result = parse_lark_access("add @_user_1", [socket_mention()])
    assert result == ("add", OU)


def test_parse_lark_access_add_history_form():
    result = parse_lark_access("add @_user_1", [history_mention()])
    assert result == ("add", OU)


def test_parse_lark_access_remove_socket_form():
    result = parse_lark_access("remove @_user_1", [socket_mention()])
    assert result == ("remove", OU)


def test_parse_lark_access_remove_history_form():
    result = parse_lark_access("remove @_user_1", [history_mention()])
    assert result == ("remove", OU)


def test_parse_lark_access_access_no_mentions():
    assert parse_lark_access("access", []) == ("access", None)
    assert parse_lark_access("  access  ", []) == ("access", None)


def test_parse_lark_access_ordinary_text_returns_none():
    assert parse_lark_access("hello", []) is None
    assert parse_lark_access("bind foo", []) is None
    assert parse_lark_access("", []) is None


def test_parse_lark_access_non_string_returns_none():
    assert parse_lark_access(None, []) is None


def test_parse_lark_access_access_with_mentions_raises():
    with pytest.raises(ValueError, match="access_command_unexpected_mentions"):
        parse_lark_access("access", [socket_mention()])


def test_parse_lark_access_access_with_extra_args_raises():
    with pytest.raises(ValueError, match="access_command_unexpected_args"):
        parse_lark_access("access extra", [])


def test_parse_lark_access_missing_mention_raises():
    with pytest.raises(ValueError, match="access_command_missing_mention"):
        parse_lark_access("add @_user_1", [])


def test_parse_lark_access_multiple_mentions_raises():
    with pytest.raises(ValueError, match="access_command_multiple_mentions"):
        parse_lark_access("add @_user_1", [socket_mention("@_user_1"), socket_mention("@_user_2", OU2)])


def test_parse_lark_access_duplicate_key_raises():
    with pytest.raises(ValueError, match="access_command_duplicate_mention_key"):
        parse_lark_access("add @_user_1", [socket_mention("@_user_1"), socket_mention("@_user_1", OU2)])


def test_parse_lark_access_bare_placeholder_no_metadata_raises():
    # Text has placeholder but no mention metadata supplied.
    with pytest.raises(ValueError, match="access_command_missing_mention"):
        parse_lark_access("add @_user_1", [])


def test_parse_lark_access_placeholder_mismatch_raises():
    # Mention key differs from text placeholder.
    with pytest.raises(ValueError, match="access_command_extra_content"):
        parse_lark_access("add @_user_1", [socket_mention("@_user_2", OU)])


def test_parse_lark_access_extra_text_after_placeholder_raises():
    with pytest.raises(ValueError, match="access_command_extra_content"):
        parse_lark_access("add @_user_1 extra", [socket_mention()])


def test_parse_lark_access_wrong_id_type_raises():
    with pytest.raises(ValueError, match="access_command_wrong_id_type"):
        parse_lark_access("add @_user_1", [history_mention(id_type="user_id")])


def test_parse_lark_access_conflicting_evidence_raises():
    # Socket form (id is dict) should not also have top-level id_type.
    with pytest.raises(ValueError, match="access_command_conflicting_mention_form"):
        parse_lark_access("add @_user_1", [socket_mention(id_type="open_id")])


def test_parse_lark_access_bot_mention_app_id_raises():
    with pytest.raises(ValueError, match="access_command_bot_mention"):
        parse_lark_access("add @_user_1", [socket_mention(extra_id_fields={"app_id": "cli_bot"})])


def test_parse_lark_access_invalid_open_id_format_raises():
    # open_id that doesn't match ou_ pattern must be rejected.
    with pytest.raises(ValueError, match="access_command_invalid_user_id"):
        parse_lark_access("add @_user_1", [history_mention(open_id="invalid_id_here")])
    with pytest.raises(ValueError, match="access_command_invalid_user_id"):
        parse_lark_access("add @_user_1", [history_mention(open_id="app_abc123def456")])


def test_parse_lark_access_malformed_mention_none_id_raises():
    bad = {"key": "@_user_1", "id": None, "name": "bad"}
    with pytest.raises(ValueError, match="access_command_malformed_mention"):
        parse_lark_access("add @_user_1", [bad])


def test_parse_lark_access_existing_admin_mention_allowed():
    # Parser allows any valid open_id; admin/ACL check is at integration layer.
    assert parse_lark_access("add @_user_1", [socket_mention()]) == ("add", OU)


def test_parse_lark_access_multiline_raises():
    with pytest.raises(ValueError, match="access_command_multiline"):
        parse_lark_access("add\n@_user_1", [socket_mention()])


# ── parse_onebot_access ───────────────────────────────────────────────────────

def text_seg(content):
    return {"type": "text", "data": {"text": content}}


def at_seg(qq):
    return {"type": "at", "data": {"qq": qq}}


def reply_seg(msg_id="100"):
    return {"type": "reply", "data": {"id": msg_id}}


SELF_ID = BOT_QQ
TARGET_ID = USER_QQ


def test_parse_onebot_access_add_no_envelope():
    segs = [text_seg("add "), at_seg(TARGET_ID)]
    assert parse_onebot_access(segs, SELF_ID) == ("add", TARGET_ID)


def test_parse_onebot_access_add_with_reply_envelope():
    segs = [reply_seg(), text_seg("add "), at_seg(TARGET_ID)]
    assert parse_onebot_access(segs, SELF_ID) == ("add", TARGET_ID)


def test_parse_onebot_access_add_with_full_envelope():
    segs = [reply_seg(), at_seg(SELF_ID), text_seg("add "), at_seg(TARGET_ID)]
    assert parse_onebot_access(segs, SELF_ID) == ("add", TARGET_ID)


def test_parse_onebot_access_remove_no_envelope():
    segs = [text_seg("remove "), at_seg(TARGET_ID)]
    assert parse_onebot_access(segs, SELF_ID) == ("remove", TARGET_ID)


def test_parse_onebot_access_access_no_envelope():
    segs = [text_seg("access")]
    assert parse_onebot_access(segs, SELF_ID) == ("access", None)


def test_parse_onebot_access_access_with_envelope():
    segs = [reply_seg(), at_seg(SELF_ID), text_seg("access")]
    assert parse_onebot_access(segs, SELF_ID) == ("access", None)


def test_parse_onebot_access_ordinary_text_returns_none():
    assert parse_onebot_access([text_seg("hello world")], SELF_ID) is None
    assert parse_onebot_access([text_seg("bind me")], SELF_ID) is None
    assert parse_onebot_access([], SELF_ID) is None


def test_parse_onebot_access_non_list_returns_none():
    assert parse_onebot_access(None, SELF_ID) is None
    assert parse_onebot_access("add 123", SELF_ID) is None


def test_parse_onebot_access_integer_qq():
    # Provider delivers qq as an integer in JSON.
    segs = [text_seg("add "), at_seg(int(TARGET_ID))]
    assert parse_onebot_access(segs, SELF_ID) == ("add", TARGET_ID)


def test_parse_onebot_access_bot_self_target_rejected():
    segs = [text_seg("add "), at_seg(SELF_ID)]
    with pytest.raises(ValueError, match="access_command_self_target"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_bot_self_in_envelope_not_target():
    # Bot-self at immediately after reply is envelope, not target.
    # Without a target at after that, add raises missing_target.
    segs = [reply_seg(), at_seg(SELF_ID), text_seg("add")]
    with pytest.raises(ValueError, match="access_command_missing_target"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_at_all_rejected():
    segs = [text_seg("add "), at_seg("all")]
    with pytest.raises(ValueError, match="access_command_all_target"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_bool_qq_rejected():
    segs = [text_seg("add "), at_seg(True)]
    with pytest.raises(ValueError, match="access_command_invalid_at_id"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_float_qq_rejected():
    segs = [text_seg("add "), at_seg(1.0)]
    with pytest.raises(ValueError, match="access_command_invalid_at_id"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_zero_qq_rejected():
    # 0 is not a canonical user ID.
    segs = [text_seg("add "), at_seg("0")]
    with pytest.raises(ValueError, match="access_command_invalid_at_id"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_extra_targets_rejected():
    segs = [text_seg("add "), at_seg(TARGET_ID), at_seg(OTHER_QQ)]
    with pytest.raises(ValueError, match="access_command_extra_targets"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_missing_target_raises():
    segs = [text_seg("add")]
    with pytest.raises(ValueError, match="access_command_missing_target"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_extra_text_before_target_raises():
    segs = [text_seg("add extra "), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_extra_content_before_target"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_extra_text_after_target_raises():
    segs = [text_seg("add "), at_seg(TARGET_ID), text_seg(" extra")]
    with pytest.raises(ValueError, match="access_command_extra_content_after_target"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_access_with_at_raises():
    segs = [text_seg("access "), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_unexpected_at"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_access_with_extra_text_raises():
    segs = [text_seg("access extra")]
    with pytest.raises(ValueError, match="access_command_unexpected_args"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_unexpected_reply_in_body_raises():
    segs = [text_seg("add "), reply_seg(), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_unexpected_reply"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_unknown_segment_raises():
    segs = [{"type": "image", "data": {}}, text_seg("add "), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_unsupported_segment"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_raw_opaque_id_in_text_raises():
    # Raw number in text (no at segment) must raise, not silently parse.
    segs = [text_seg("add 54321")]
    with pytest.raises(ValueError, match="access_command_missing_target"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_existing_admin_mention_allowed():
    # Parser allows any valid user; admin check is at integration layer.
    segs = [text_seg("add "), at_seg(TARGET_ID)]
    assert parse_onebot_access(segs, SELF_ID) == ("add", TARGET_ID)


# ── verify_slack_user ─────────────────────────────────────────────────────────

def slack_api(user_id, *, bot=False, app=False, deleted=False, wrong_id=False, ok=True):
    def api(method, params):
        assert method == "users.info"
        assert params == {"user": user_id}
        if not ok:
            return {"ok": False, "error": "user_not_found"}
        uid = USER2 if wrong_id else user_id
        return {"ok": True, "user": {"id": uid, "is_bot": bot, "is_app_user": app, "deleted": deleted}}
    return api


def test_verify_slack_user_valid_human():
    assert verify_slack_user(USER, slack_api(USER)) == USER


def test_verify_slack_user_uslackbot_rejected_before_api():
    calls = []
    def api(method, params):
        calls.append((method, params))
        return {"ok": True, "user": {"id": "USLACKBOT", "is_bot": True, "is_app_user": False, "deleted": False}}
    with pytest.raises(ValueError, match="access_verify_bot_user"):
        verify_slack_user("USLACKBOT", api)
    assert not calls  # must not call API at all


def test_verify_slack_user_is_bot_rejected():
    with pytest.raises(ValueError, match="access_verify_bot_user"):
        verify_slack_user(USER, slack_api(USER, bot=True))


def test_verify_slack_user_is_app_user_rejected():
    with pytest.raises(ValueError, match="access_verify_app_user"):
        verify_slack_user(USER, slack_api(USER, app=True))


def test_verify_slack_user_deleted_rejected():
    with pytest.raises(ValueError, match="access_verify_deleted_user"):
        verify_slack_user(USER, slack_api(USER, deleted=True))


def test_verify_slack_user_id_mismatch_rejected():
    with pytest.raises(ValueError, match="access_verify_id_mismatch"):
        verify_slack_user(USER, slack_api(USER, wrong_id=True))


def test_verify_slack_user_missing_field_fails_closed():
    # If is_bot is missing, fail closed (treat as bot).
    def api(method, params):
        return {"ok": True, "user": {"id": USER, "is_app_user": False, "deleted": False}}
    with pytest.raises(ValueError, match="access_verify_bot_user"):
        verify_slack_user(USER, api)


def test_verify_slack_user_api_not_ok_raises():
    with pytest.raises(ValueError, match="access_verify_api_error"):
        verify_slack_user(USER, slack_api(USER, ok=False))


def test_verify_slack_user_missing_scope_raises():
    def api(method, params):
        return {"ok": False, "error": "missing_scope"}
    with pytest.raises(ValueError, match="access_verify_missing_scope"):
        verify_slack_user(USER, api)


def test_verify_slack_user_api_exception_is_redacted():
    def api(method, params):
        raise RuntimeError("secret-provider-url-and-token")
    with pytest.raises(ValueError, match="access_verify_api_error") as exc_info:
        verify_slack_user(USER, api)
    assert "secret" not in str(exc_info.value)


def test_verify_slack_user_valueerror_secret_is_redacted():
    # A non-allowlisted ValueError from the provider API must never leak its message text.
    def api(method, params):
        raise ValueError("sentinel-secret-credential-abc123")
    with pytest.raises(ValueError, match="access_verify_api_error") as exc_info:
        verify_slack_user(USER, api)
    assert "sentinel" not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


def test_verify_slack_user_429_propagates_unchanged():
    def api(method, params):
        raise HTTPError("https://slack.com/api/users.info", 429, "Too Many Requests", {}, None)
    with pytest.raises(HTTPError) as exc_info:
        verify_slack_user(USER, api)
    assert exc_info.value.code == 429


def test_verify_slack_user_invalid_format_rejected():
    with pytest.raises(ValueError, match="access_verify_invalid_user_id"):
        verify_slack_user("invalid", lambda *a: {})
    with pytest.raises(ValueError, match="access_verify_invalid_user_id"):
        verify_slack_user("", lambda *a: {})


def test_verify_slack_user_only_calls_users_info():
    calls = []
    def api(method, params):
        calls.append(method)
        return {"ok": True, "user": {"id": USER, "is_bot": False, "is_app_user": False, "deleted": False}}
    verify_slack_user(USER, api)
    assert calls == ["users.info"]


# ── slack_user_get ────────────────────────────────────────────────────────────

def test_slack_user_get_rejects_non_users_info():
    with pytest.raises(ValueError, match="access_verify_api_error"):
        slack_user_get("token", "users.list", {"limit": "200"})
    with pytest.raises(ValueError, match="access_verify_api_error"):
        slack_user_get("token", "conversations.info", {"channel": "C12345678"})


# ── LarkApi.verify_user ───────────────────────────────────────────────────────

def test_lark_api_verify_user_valid():
    api = make_lark_api(lambda req: fake_user_response(OU))
    assert api.verify_user(OU) == OU


def test_lark_api_verify_user_id_mismatch_raises():
    api = make_lark_api(lambda req: fake_user_response(OU, wrong_id=True))
    with pytest.raises(LarkTransportError, match="^lark_user_id_mismatch$"):
        api.verify_user(OU)


def test_lark_api_verify_user_frozen_raises():
    api = make_lark_api(lambda req: fake_user_response(OU, frozen=True))
    with pytest.raises(LarkTransportError, match="^lark_user_inactive$"):
        api.verify_user(OU)


@pytest.mark.parametrize("status", [{}, [], "active", False, SimpleNamespace()])
def test_lark_api_verify_user_malformed_status_container(status):
    response = fake_user_response(OU)
    response.data.user.status = status
    api = make_lark_api(lambda request: response)
    with pytest.raises(LarkTransportError, match="^lark_user_verify_malformed$"):
        api.verify_user(OU)


def test_lark_api_verify_user_resigned_raises():
    api = make_lark_api(lambda req: fake_user_response(OU, resigned=True))
    with pytest.raises(LarkTransportError, match="^lark_user_inactive$"):
        api.verify_user(OU)


def test_lark_api_verify_user_failed_response_raises():
    api = make_lark_api(lambda req: fake_user_response(OU, success=False, status_code=403))
    with pytest.raises(LarkTransportError, match="^lark_user_verify_failed_check_permissions$"):
        api.verify_user(OU)


def test_lark_api_verify_user_missing_data_raises():
    api = make_lark_api(lambda req: fake_user_response(OU, missing_data=True))
    with pytest.raises(LarkTransportError, match="^lark_user_verify_malformed$"):
        api.verify_user(OU)


def test_lark_api_verify_user_invalid_id_format_raises():
    api = make_lark_api(lambda req: fake_user_response(OU))
    with pytest.raises(LarkTransportError, match="^lark_user_id_invalid$"):
        api.verify_user("not-a-valid-ou-id")


def test_lark_api_verify_user_rate_limited_raises():
    raw = SimpleNamespace(status_code=429)
    resp = SimpleNamespace(success=lambda: False, data=None, raw=raw)
    api = make_lark_api(lambda req: resp)
    with pytest.raises(LarkTransportError, match="^lark_user_rate_limited$"):
        api.verify_user(OU)


def test_lark_api_verify_user_exception_is_redacted():
    def get_raises(req):
        raise RuntimeError("secret-provider-token-and-url")
    api = make_lark_api(get_raises)
    with pytest.raises(LarkTransportError, match="^lark_user_verify_failed$") as exc_info:
        api.verify_user(OU)
    assert "secret" not in str(exc_info.value)


def test_lark_api_verify_user_status_none_is_allowed():
    # A user with no status object (not frozen/resigned) should pass.
    api = make_lark_api(lambda req: fake_user_response(OU))
    assert api.verify_user(OU) == OU


def test_lark_api_verify_user_truthy_non_true_success_rejected():
    # success() returning 1 or any truthy non-True value must be rejected (is True required).
    raw = SimpleNamespace(status_code=200)
    user = SimpleNamespace(open_id=OU, status=None)
    data = SimpleNamespace(user=user)
    resp = SimpleNamespace(success=lambda: 1, data=data, raw=raw)
    api = make_lark_api(lambda req: resp)
    with pytest.raises(LarkTransportError, match="^lark_user_verify_failed_check_permissions$"):
        api.verify_user(OU)


def test_lark_api_verify_user_exited_raises():
    status = SimpleNamespace(is_frozen=False, is_resigned=False, is_exited=True)
    raw = SimpleNamespace(status_code=200)
    user = SimpleNamespace(open_id=OU, status=status)
    data = SimpleNamespace(user=user)
    resp = SimpleNamespace(success=lambda: True, data=data, raw=raw)
    api = make_lark_api(lambda req: resp)
    with pytest.raises(LarkTransportError, match="^lark_user_inactive$"):
        api.verify_user(OU)


def test_lark_api_verify_user_not_activated_raises():
    status = SimpleNamespace(is_frozen=False, is_resigned=False, is_exited=False, is_activated=False)
    raw = SimpleNamespace(status_code=200)
    user = SimpleNamespace(open_id=OU, status=status)
    data = SimpleNamespace(user=user)
    resp = SimpleNamespace(success=lambda: True, data=data, raw=raw)
    api = make_lark_api(lambda req: resp)
    with pytest.raises(LarkTransportError, match="^lark_user_inactive$"):
        api.verify_user(OU)


def test_lark_api_verify_user_unjoined_raises():
    status = SimpleNamespace(is_frozen=False, is_resigned=False, is_exited=False, is_unjoin=True)
    raw = SimpleNamespace(status_code=200)
    user = SimpleNamespace(open_id=OU, status=status)
    data = SimpleNamespace(user=user)
    resp = SimpleNamespace(success=lambda: True, data=data, raw=raw)
    api = make_lark_api(lambda req: resp)
    with pytest.raises(LarkTransportError, match="^lark_user_inactive$"):
        api.verify_user(OU)


def test_lark_api_verify_user_bool_status_code_rejected():
    raw = SimpleNamespace(status_code=True)
    user = SimpleNamespace(open_id=OU, status=None)
    data = SimpleNamespace(user=user)
    resp = SimpleNamespace(success=lambda: True, data=data, raw=raw)
    api = make_lark_api(lambda req: resp)
    with pytest.raises(LarkTransportError, match="^lark_user_verify_failed_check_permissions$"):
        api.verify_user(OU)


def test_lark_api_verify_user_float_status_code_rejected():
    raw = SimpleNamespace(status_code=200.0)
    user = SimpleNamespace(open_id=OU, status=None)
    data = SimpleNamespace(user=user)
    resp = SimpleNamespace(success=lambda: True, data=data, raw=raw)
    api = make_lark_api(lambda req: resp)
    with pytest.raises(LarkTransportError, match="^lark_user_verify_failed_check_permissions$"):
        api.verify_user(OU)


def test_lark_api_verify_user_malformed_status_field_fails_closed():
    # Non-bool status field present → fail closed as malformed, not inactive.
    status = SimpleNamespace(is_frozen="yes", is_resigned=False, is_exited=False, is_unjoin=False)
    raw = SimpleNamespace(status_code=200)
    user = SimpleNamespace(open_id=OU, status=status)
    data = SimpleNamespace(user=user)
    resp = SimpleNamespace(success=lambda: True, data=data, raw=raw)
    api = make_lark_api(lambda req: resp)
    with pytest.raises(LarkTransportError, match="^lark_user_verify_malformed$"):
        api.verify_user(OU)


def test_lark_api_verify_user_all_none_status_fields_allowed():
    # Correctly typed status with all None fields is acceptable (SDK default).
    status = SimpleNamespace(is_frozen=None, is_resigned=None, is_exited=None,
                             is_unjoin=None, is_activated=None)
    raw = SimpleNamespace(status_code=200)
    user = SimpleNamespace(open_id=OU, status=status)
    data = SimpleNamespace(user=user)
    resp = SimpleNamespace(success=lambda: True, data=data, raw=raw)
    api = make_lark_api(lambda req: resp)
    assert api.verify_user(OU) == OU


# ── verify_slack_user: helper -> verifier path ────────────────────────────────

def test_verify_slack_user_missing_scope_from_helper_propagates():
    # When api (e.g. slack_user_get) raises ValueError("access_verify_missing_scope"),
    # verify_slack_user must re-raise it unchanged rather than replacing with api_error.
    def api(method, params):
        raise ValueError("access_verify_missing_scope")
    with pytest.raises(ValueError, match="^access_verify_missing_scope$"):
        verify_slack_user(USER, api)


def test_verify_slack_user_bot_id_marker_rejected():
    def api(method, params):
        return {"ok": True, "user": {"id": USER, "is_bot": False, "is_app_user": False,
                                     "deleted": False, "bot_id": "B12345678"}}
    with pytest.raises(ValueError, match="access_verify_bot_user"):
        verify_slack_user(USER, api)


def test_verify_slack_user_api_app_id_marker_rejected():
    def api(method, params):
        return {"ok": True, "user": {"id": USER, "is_bot": False, "is_app_user": False,
                                     "deleted": False, "api_app_id": "A12345678"}}
    with pytest.raises(ValueError, match="access_verify_app_user"):
        verify_slack_user(USER, api)


def test_verify_slack_user_agentforce_bot_marker_rejected():
    def api(method, params):
        return {"ok": True, "user": {"id": USER, "is_bot": False, "is_app_user": False,
                                     "deleted": False, "is_agentforce_bot": True}}
    with pytest.raises(ValueError, match="access_verify_bot_user"):
        verify_slack_user(USER, api)


def test_verify_slack_user_contradictory_markers_rejected_even_when_is_bot_false():
    # is_bot=False but bot_id present: must still be rejected.
    def api(method, params):
        return {"ok": True, "user": {"id": USER, "is_bot": False, "is_app_user": False,
                                     "deleted": False, "bot_id": "B99999999"}}
    with pytest.raises(ValueError, match="access_verify_bot_user"):
        verify_slack_user(USER, api)


# ── parse_onebot_access: case-insensitive commands ────────────────────────────

def test_parse_onebot_access_uppercase_add():
    segs = [text_seg("ADD "), at_seg(TARGET_ID)]
    assert parse_onebot_access(segs, SELF_ID) == ("add", TARGET_ID)


def test_parse_onebot_access_mixed_case_remove():
    segs = [text_seg("Remove "), at_seg(TARGET_ID)]
    assert parse_onebot_access(segs, SELF_ID) == ("remove", TARGET_ID)


def test_parse_onebot_access_uppercase_access():
    segs = [text_seg("ACCESS")]
    assert parse_onebot_access(segs, SELF_ID) == ("access", None)


def test_parse_onebot_access_uppercase_access_with_envelope():
    segs = [reply_seg(), at_seg(SELF_ID), text_seg("ACCESS")]
    assert parse_onebot_access(segs, SELF_ID) == ("access", None)


# ── parse_onebot_access: malformed leading reply ──────────────────────────────

def test_parse_onebot_access_malformed_reply_no_id_raises():
    segs = [{"type": "reply", "data": {}}, text_seg("add "), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_malformed_segment"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_reply_integer_id_accepted():
    # Integer message IDs are valid per OneBot protocol; identifier(message=True) accepts them.
    segs = [{"type": "reply", "data": {"id": 100}}, text_seg("add "), at_seg(TARGET_ID)]
    assert parse_onebot_access(segs, SELF_ID) == ("add", TARGET_ID)


def test_parse_onebot_access_malformed_reply_bool_id_raises():
    segs = [{"type": "reply", "data": {"id": True}}, text_seg("add "), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_malformed_segment"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_malformed_reply_float_id_raises():
    segs = [{"type": "reply", "data": {"id": 1.0}}, text_seg("add "), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_malformed_segment"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_malformed_reply_invalid_string_id_raises():
    segs = [{"type": "reply", "data": {"id": "not-a-valid-id"}}, text_seg("add "), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_malformed_segment"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_malformed_reply_empty_id_raises():
    segs = [{"type": "reply", "data": {"id": ""}}, text_seg("add "), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_malformed_segment"):
        parse_onebot_access(segs, SELF_ID)


def test_parse_onebot_access_malformed_reply_no_data_raises():
    segs = [{"type": "reply", "data": None}, text_seg("add "), at_seg(TARGET_ID)]
    with pytest.raises(ValueError, match="access_command_malformed_segment"):
        parse_onebot_access(segs, SELF_ID)


# ── parse_lark_access: placeholder key format ─────────────────────────────────

def test_parse_lark_access_invalid_key_format_raises():
    bad = {"key": "not_a_placeholder", "id": OU, "id_type": "open_id", "name": "bad"}
    with pytest.raises(ValueError, match="access_command_malformed_mention"):
        parse_lark_access("add not_a_placeholder", [bad])


def test_parse_lark_access_empty_key_raises():
    bad = {"key": "", "id": OU, "id_type": "open_id", "name": "bad"}
    with pytest.raises(ValueError, match="access_command_malformed_mention"):
        parse_lark_access("add @_user_1", [bad])


def test_parse_lark_access_at_sign_only_key_raises():
    bad = {"key": "@", "id": OU, "id_type": "open_id", "name": "bad"}
    with pytest.raises(ValueError, match="access_command_malformed_mention"):
        parse_lark_access("add @", [bad])


# ── parse_lark_access: access with malformed mentions ─────────────────────────

def test_parse_lark_access_access_with_empty_dict_mentions_raises():
    # {} is not a valid mentions value; only None or [] are accepted for access.
    with pytest.raises(ValueError, match="access_command_unexpected_mentions"):
        parse_lark_access("access", {})


def test_parse_lark_access_access_with_none_mentions_allowed():
    assert parse_lark_access("access", None) == ("access", None)
