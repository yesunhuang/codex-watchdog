"""Tests for Slack route command parsing and destination resolution."""
import json as _json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from codex_watchdog.slack_route_commands import (
    parse_route_command,
    resolve_destination,
    slack_conversations_get,
)

CHANNEL_ID = "C12345678"
CHANNEL_ID2 = "C99999999"


def test_access_check_accepts_absent_optional_restriction_flags():
    assert resolve_destination("<#C12345678>", lambda *args: {
        "ok": True, "channel": {"id": CHANNEL_ID, "is_member": True,
                                  "is_archived": False},
    }) == CHANNEL_ID


@pytest.mark.parametrize("metadata", [{"next_cursor": 7}, {"next_cursor": True}])
def test_malformed_cursor_cannot_hide_additional_name_matches(metadata):
    with pytest.raises(ValueError, match="malformed"):
        resolve_destination("#target", lambda *args: {
            "ok": True, "channels": [{"id": CHANNEL_ID, "name": "target"}],
            "response_metadata": metadata,
        })

# ── parse_route_command ──────────────────────────────────────────────────────

def test_non_command_returns_none():
    assert parse_route_command("hello world") is None
    assert parse_route_command("") is None
    assert parse_route_command("   ") is None
    assert parse_route_command("binder something") is None
    assert parse_route_command("unbound") is None


def test_bind_structured_mention():
    result = parse_route_command("bind <#C12345678|general>")
    assert result == ("bind", "<#C12345678|general>")


def test_bind_structured_mention_no_name():
    result = parse_route_command("bind <#C12345678>")
    assert result == ("bind", "<#C12345678>")


def test_bind_hash_name():
    result = parse_route_command("bind #general")
    assert result == ("bind", "#general")


def test_unbind_returns_none_argument():
    result = parse_route_command("unbind")
    assert result == ("unbind", None)


def test_case_insensitive_bind():
    assert parse_route_command("BIND <#C12345678|general>") == ("bind", "<#C12345678|general>")
    assert parse_route_command("Bind <#C12345678|general>") == ("bind", "<#C12345678|general>")


def test_case_insensitive_unbind():
    assert parse_route_command("UNBIND") == ("unbind", None)
    assert parse_route_command("Unbind") == ("unbind", None)


def test_surrounding_whitespace_accepted():
    assert parse_route_command("  bind <#C12345678|general>  ") == ("bind", "<#C12345678|general>")
    assert parse_route_command("  unbind  ") == ("unbind", None)


def test_bind_missing_arg_raises():
    with pytest.raises(ValueError, match="route_command_bind_missing_arg"):
        parse_route_command("bind")


def test_bind_extra_args_raises():
    with pytest.raises(ValueError, match="route_command_bind_extra_args"):
        parse_route_command("bind <#C12345678|general> extra")


def test_unbind_extra_args_raises():
    with pytest.raises(ValueError, match="route_command_unbind_extra_args"):
        parse_route_command("unbind #general")


def test_bind_invalid_arg_raises():
    with pytest.raises(ValueError, match="route_command_bind_invalid_arg"):
        parse_route_command("bind not-a-channel")


def test_bind_bare_channel_id_no_hash_raises():
    with pytest.raises(ValueError, match="route_command_bind_invalid_arg"):
        parse_route_command("bind C12345678")


def test_multiline_bind_raises():
    with pytest.raises(ValueError, match="route_command_multiline"):
        parse_route_command("bind <#C12345678|general>\nextra line")


def test_multiline_unbind_raises():
    with pytest.raises(ValueError, match="route_command_multiline"):
        parse_route_command("unbind\nsomething")


def test_oversize_bind_raises():
    long_text = "bind " + "#" + "a" * 495
    assert len(long_text) > 500
    with pytest.raises(ValueError, match="route_command_oversize"):
        parse_route_command(long_text)


def test_bind_hash_name_with_hyphens_and_underscores():
    result = parse_route_command("bind #my-channel_name")
    assert result == ("bind", "#my-channel_name")


def test_bind_g_prefixed_channel_mention():
    # G-prefix channels (legacy group DMs) accepted
    result = parse_route_command("bind <#G12345678|group>")
    assert result == ("bind", "<#G12345678|group>")


# ── resolve_destination ──────────────────────────────────────────────────────

def make_info_api(channel_id, *, is_member=True, is_archived=False,
                  is_read_only=False, is_frozen=False, ok=True):
    def api(method, params):
        assert method == "conversations.info"
        assert params.get("channel") == channel_id
        if not ok:
            return {"ok": False, "error": "channel_not_found"}
        return {"ok": True, "channel": {
            "id": channel_id,
            "is_member": is_member,
            "is_archived": is_archived,
            "is_read_only": is_read_only,
            "is_frozen": is_frozen,
        }}
    return api


def test_resolve_structured_mention_returns_id():
    api = make_info_api(CHANNEL_ID)
    result = resolve_destination(f"<#{CHANNEL_ID}|general>", api)
    assert result == CHANNEL_ID


def test_resolve_structured_mention_id_stable_despite_channel_rename():
    # Only conversations.info is called; display name ignored
    calls = []
    def api(method, params):
        calls.append(method)
        return {"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    result = resolve_destination(f"<#{CHANNEL_ID}|old-renamed-name>", api)
    assert result == CHANNEL_ID
    assert calls == ["conversations.info"]  # no conversations.list


def test_resolve_structured_mention_not_member_raises():
    api = make_info_api(CHANNEL_ID, is_member=False)
    with pytest.raises(ValueError, match="route_destination_not_member"):
        resolve_destination(f"<#{CHANNEL_ID}|general>", api)


def test_resolve_structured_mention_archived_raises():
    api = make_info_api(CHANNEL_ID, is_archived=True)
    with pytest.raises(ValueError, match="route_destination_inaccessible"):
        resolve_destination(f"<#{CHANNEL_ID}|general>", api)


def test_resolve_structured_mention_read_only_raises():
    api = make_info_api(CHANNEL_ID, is_read_only=True)
    with pytest.raises(ValueError, match="route_destination_inaccessible"):
        resolve_destination(f"<#{CHANNEL_ID}|general>", api)


def test_resolve_structured_mention_frozen_raises():
    api = make_info_api(CHANNEL_ID, is_frozen=True)
    with pytest.raises(ValueError, match="route_destination_inaccessible"):
        resolve_destination(f"<#{CHANNEL_ID}|general>", api)


def test_resolve_structured_mention_api_error_raises():
    api = make_info_api(CHANNEL_ID, ok=False)
    with pytest.raises(ValueError, match="route_destination_api_error"):
        resolve_destination(f"<#{CHANNEL_ID}|general>", api)


def test_resolve_structured_mention_id_mismatch_raises():
    def api(method, params):
        return {"ok": True, "channel": {
            "id": "C00000000",  # mismatched
            "is_member": True, "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    with pytest.raises(ValueError, match="route_destination_id_mismatch"):
        resolve_destination(f"<#{CHANNEL_ID}|general>", api)


def test_resolve_hash_name_single_match():
    def api(method, params):
        if method == "conversations.list":
            return {"ok": True, "channels": [{"id": CHANNEL_ID, "name": "general"}],
                    "response_metadata": {"next_cursor": ""}}
        # conversations.info
        return {"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    result = resolve_destination("#general", api)
    assert result == CHANNEL_ID


def test_resolve_hash_name_case_insensitive():
    def api(method, params):
        if method == "conversations.list":
            return {"ok": True, "channels": [{"id": CHANNEL_ID, "name": "General"}],
                    "response_metadata": {}}
        return {"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    result = resolve_destination("#general", api)
    assert result == CHANNEL_ID


def test_resolve_hash_name_not_found_raises():
    def api(method, params):
        return {"ok": True, "channels": [], "response_metadata": {}}
    with pytest.raises(ValueError, match="route_destination_name_not_found"):
        resolve_destination("#nosuchchannel", api)


def test_resolve_hash_name_ambiguous_raises():
    def api(method, params):
        if method == "conversations.list":
            return {"ok": True, "channels": [
                {"id": CHANNEL_ID, "name": "general"},
                {"id": CHANNEL_ID2, "name": "general"},
            ], "response_metadata": {}}
        return {"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    with pytest.raises(ValueError, match="route_destination_name_ambiguous"):
        resolve_destination("#general", api)


def test_resolve_hash_name_api_error_raises():
    def api(method, params):
        return {"ok": False, "error": "missing_scope"}
    with pytest.raises(ValueError, match="route_destination_api_error"):
        resolve_destination("#general", api)


def test_resolve_hash_name_paginated_found_on_page_two():
    page = 0
    def api(method, params):
        nonlocal page
        if method == "conversations.list":
            page += 1
            if page == 1:
                return {"ok": True, "channels": [{"id": "C00000001", "name": "other"}],
                        "response_metadata": {"next_cursor": "cursor2"}}
            return {"ok": True, "channels": [{"id": CHANNEL_ID, "name": "general"}],
                    "response_metadata": {}}
        return {"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    result = resolve_destination("#general", api)
    assert result == CHANNEL_ID
    assert page == 2


def test_resolve_hash_name_exhausted_at_3_pages_raises():
    page_count = 0
    def api(method, params):
        nonlocal page_count
        page_count += 1
        return {"ok": True, "channels": [{"id": "C0000000" + str(page_count), "name": "other"}],
                "response_metadata": {"next_cursor": "cursor_" + str(page_count)}}
    with pytest.raises(ValueError, match="route_destination_name_exhausted"):
        resolve_destination("#general", api)
    assert page_count == 3


def test_resolve_hash_name_not_member_raises():
    def api(method, params):
        if method == "conversations.list":
            return {"ok": True, "channels": [{"id": CHANNEL_ID, "name": "private"}],
                    "response_metadata": {}}
        return {"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": False,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    with pytest.raises(ValueError, match="route_destination_not_member"):
        resolve_destination("#private", api)


def test_resolve_invalid_argument_raises():
    def api(method, params):
        return {"ok": True}
    with pytest.raises(ValueError, match="route_command_arg_invalid"):
        resolve_destination("notachannel", api)


# ── Regression: strict API boolean checks ───────────────────────────────────

def test_verify_channel_ok_truthy_int_raises():
    """ok=1 (truthy but not True) must be rejected."""
    def api(method, params):
        return {"ok": 1, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    with pytest.raises(ValueError, match="route_destination_api_error"):
        resolve_destination(f"<#{CHANNEL_ID}|general>", api)


def test_verify_channel_is_member_truthy_int_raises():
    """is_member=1 (truthy but not True) must be rejected as not_member."""
    def api(method, params):
        return {"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": 1,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    with pytest.raises(ValueError, match="route_destination_not_member"):
        resolve_destination(f"<#{CHANNEL_ID}|general>", api)


def test_resolve_by_name_ok_truthy_int_raises():
    """ok=1 in conversations.list response must be rejected."""
    def api(method, params):
        return {"ok": 1, "channels": [], "response_metadata": {}}
    with pytest.raises(ValueError, match="route_destination_api_error"):
        resolve_destination("#general", api)


def test_resolve_by_name_non_dict_channel_entry_raises():
    """Non-dict channel list entries must be rejected."""
    def api(method, params):
        if method == "conversations.list":
            return {"ok": True, "channels": ["not-a-dict"],
                    "response_metadata": {}}
        return {"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }}
    with pytest.raises(ValueError, match="route_destination_malformed"):
        resolve_destination("#general", api)


def test_resolve_by_name_non_dict_response_metadata_raises():
    """Non-dict response_metadata must be rejected."""
    def api(method, params):
        return {"ok": True, "channels": [], "response_metadata": "bad"}
    with pytest.raises(ValueError, match="route_destination_malformed"):
        resolve_destination("#general", api)


def test_verify_channel_is_archived_truthy_int_raises():
    """is_archived=1 must raise inaccessible (strict is True check)."""
    def api(method, params):
        return {"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": 1, "is_read_only": False, "is_frozen": False,
        }}
    with pytest.raises(ValueError, match="route_destination_inaccessible"):
        resolve_destination(f"<#{CHANNEL_ID}|general>", api)


# ── Wire-level tests for slack_conversations_get ─────────────────────────────

def _mock_urlopen(body_dict):
    """Return a context-manager mock that reads body_dict as JSON."""
    raw = _json.dumps(body_dict).encode()
    cm = MagicMock()
    cm.__enter__ = lambda s: s
    cm.__exit__ = MagicMock(return_value=False)
    cm.read.return_value = raw
    return cm


def _http_error(code):
    return HTTPError("https://slack.com/api/conversations.info", code, "Error", {}, BytesIO(b""))


def test_wire_get_request_method_query_auth_no_body():
    """slack_conversations_get must issue GET, encode params in query string,
    send token in Authorization header only, and send no request body."""
    captured = []

    def fake_urlopen(req, timeout):
        captured.append(req)
        return _mock_urlopen({"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }})

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        result = slack_conversations_get("xoxb-tok", "conversations.info", {"channel": CHANNEL_ID})

    assert result["ok"] is True
    assert len(captured) == 1
    req = captured[0]
    # No body — GET request
    assert req.data is None
    # URL scheme/host/path
    parsed = urlparse(req.full_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "slack.com"
    assert parsed.path == "/api/conversations.info"
    # channel in query string
    qs = parse_qs(parsed.query)
    assert qs.get("channel") == [CHANNEL_ID]
    # Token in Authorization header, not in URL
    assert req.get_header("Authorization") == "Bearer xoxb-tok"
    assert "xoxb-tok" not in req.full_url


def test_wire_get_timeout_is_10():
    """urlopen must be called with timeout=10."""
    timeouts = []

    def fake_urlopen(req, timeout):
        timeouts.append(timeout)
        return _mock_urlopen({"ok": True, "channel": {
            "id": CHANNEL_ID, "is_member": True,
            "is_archived": False, "is_read_only": False, "is_frozen": False,
        }})

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        slack_conversations_get("xoxb-tok", "conversations.info", {"channel": CHANNEL_ID})

    assert timeouts == [10]


def test_wire_list_booleans_encoded_as_lowercase_strings():
    """Boolean params must appear as 'true'/'false', not 'True'/'False'."""
    captured_qs = {}

    def fake_urlopen(req, timeout):
        captured_qs.update(parse_qs(urlparse(req.full_url).query))
        return _mock_urlopen({"ok": True, "channels": []})

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        slack_conversations_get(
            "xoxb-tok",
            "conversations.list",
            {"exclude_archived": True, "types": "public_channel,private_channel", "limit": 200},
        )

    assert captured_qs.get("exclude_archived") == ["true"]
    assert captured_qs.get("types") == ["public_channel,private_channel"]
    assert captured_qs.get("limit") == ["200"]


def test_wire_list_cursor_pagination_encoded():
    """Cursor string must appear verbatim in the query string on page 2."""
    captured_queries = []

    calls = [0]
    def fake_urlopen(req, timeout):
        captured_queries.append(parse_qs(urlparse(req.full_url).query))
        calls[0] += 1
        if urlparse(req.full_url).path.endswith('/conversations.info'):
            return _mock_urlopen({
                "ok": True, "channel": {"id": CHANNEL_ID2, "is_member": True},
            })
        if calls[0] == 1:
            return _mock_urlopen({
                "ok": True,
                "channels": [{"id": CHANNEL_ID, "name": "general"}],
                "response_metadata": {"next_cursor": "cursor_abc+/="},
            })
        return _mock_urlopen({
            "ok": True,
            "channels": [{"id": CHANNEL_ID2, "name": "target"}],
            "response_metadata": {},
        })

    def injected_api(method, params):
        with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
            return slack_conversations_get("xoxb-tok", method, params)

    assert resolve_destination("#target", injected_api) == CHANNEL_ID2

    assert calls[0] == 3
    assert captured_queries[1].get("cursor") == ["cursor_abc+/="]


def test_wire_missing_scope_raises_correct_error():
    """missing_scope in Slack response must raise ValueError('route_destination_missing_scope')."""
    def fake_urlopen(req, timeout):
        return _mock_urlopen({"ok": False, "error": "missing_scope"})

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        with pytest.raises(ValueError, match="route_destination_missing_scope"):
            slack_conversations_get("xoxb-tok", "conversations.info", {"channel": CHANNEL_ID})


def test_wire_other_api_error_raises_safe_error():
    """Non-missing_scope Slack errors must raise route_destination_api_error, not leak details."""
    def fake_urlopen(req, timeout):
        return _mock_urlopen({"ok": False, "error": "channel_not_found", "detail": "secret"})

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        with pytest.raises(ValueError, match="route_destination_api_error") as exc_info:
            slack_conversations_get("xoxb-tok", "conversations.info", {"channel": CHANNEL_ID})
    assert "secret" not in str(exc_info.value)
    assert "channel_not_found" not in str(exc_info.value)


def test_wire_http429_is_reraised():
    """HTTPError 429 must propagate unchanged for the caller's retry/backoff."""
    err = _http_error(429)

    def fake_urlopen(req, timeout):
        raise err

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        with pytest.raises(HTTPError) as exc_info:
            slack_conversations_get("xoxb-tok", "conversations.info", {"channel": CHANNEL_ID})
    assert exc_info.value is err


def test_wire_http5xx_raises_safe_error():
    """HTTPError other than 429 must raise route_destination_api_error, not propagate."""
    def fake_urlopen(req, timeout):
        raise _http_error(500)

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        with pytest.raises(ValueError, match="route_destination_api_error"):
            slack_conversations_get("xoxb-tok", "conversations.info", {"channel": CHANNEL_ID})


def test_wire_malformed_json_raises_safe_error():
    """Non-JSON response body must raise route_destination_api_error."""
    cm = MagicMock()
    cm.__enter__ = lambda s: s
    cm.__exit__ = MagicMock(return_value=False)
    cm.read.return_value = b"not json{"

    def fake_urlopen(req, timeout):
        return cm

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        with pytest.raises(ValueError, match="route_destination_api_error"):
            slack_conversations_get("xoxb-tok", "conversations.info", {"channel": CHANNEL_ID})


def test_wire_non_dict_json_raises_safe_error():
    """JSON array response must raise route_destination_api_error."""
    cm = MagicMock()
    cm.__enter__ = lambda s: s
    cm.__exit__ = MagicMock(return_value=False)
    cm.read.return_value = _json.dumps(["not", "a", "dict"]).encode()

    def fake_urlopen(req, timeout):
        return cm

    with patch("codex_watchdog.slack_route_commands.urlopen", fake_urlopen):
        with pytest.raises(ValueError, match="route_destination_api_error"):
            slack_conversations_get("xoxb-tok", "conversations.info", {"channel": CHANNEL_ID})


def test_wire_disallowed_method_raises_safe_error():
    """Methods other than conversations.info/list must raise route_destination_api_error."""
    with pytest.raises(ValueError, match="route_destination_api_error"):
        slack_conversations_get("xoxb-tok", "conversations.history", {})
