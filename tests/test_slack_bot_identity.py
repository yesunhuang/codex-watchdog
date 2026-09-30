from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from urllib.error import HTTPError, URLError

import pytest

from codex_watchdog import slack_bot_identity as identity
from codex_watchdog.models import MAX_PROMPT_CHARS
from codex_watchdog.slack_bot_identity import (
    BotPrincipal, BotVerificationDeferred, BotVerifier, bot_api_call,
    event_principal, parse_bot_control, parse_bot_control_event, parse_bot_instruction,
)


OWN = BotPrincipal("T00000001", "U00000001", "B00000001", "A00000001")
BOT = BotPrincipal("T00000001", "U00000002", "B00000002", "A00000002")
HUMAN = "U00000003"
REQUEST = "11111111-2222-4333-8444-555555555555"
COMMAND = "!codex " + REQUEST + "\nInspect the failing test."
INLINE_COMMAND = "!codex " + REQUEST + " -- Inspect the failing test."


def user(principal):
    return dict(id=principal.user_id, team_id=principal.team_id, deleted=False,
                is_bot=True, is_app_user=False,
                profile=dict(bot_id=principal.bot_id, api_app_id=principal.app_id,
                             team=principal.team_id))


class FakeApi:
    def __init__(self):
        self.calls = []
        self.auth = dict(ok=True, team_id=OWN.team_id, user_id=OWN.user_id, bot_id=OWN.bot_id)
        self.users = {p.user_id: user(p) for p in (OWN, BOT)}
        self.users[HUMAN] = dict(id=HUMAN, team_id=OWN.team_id, deleted=False,
                                is_bot=False, is_app_user=False, profile={})
        self.bots = {p.bot_id: dict(id=p.bot_id, user_id=p.user_id, app_id=p.app_id,
                                   deleted=False) for p in (OWN, BOT)}

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "auth.test":
            return deepcopy(self.auth)
        if method == "users.info":
            return dict(ok=True, user=deepcopy(self.users[params["user"]]))
        if method == "bots.info":
            return dict(ok=True, bot=deepcopy(self.bots[params["bot"]]))
        raise AssertionError("unexpected method")


@pytest.fixture(autouse=True)
def never_use_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unmocked Slack access")
    monkeypatch.setattr(identity, "urlopen", forbidden)


def event(**changes):
    return dict(dict(type="message", user=BOT.user_id, bot_id=BOT.bot_id,
                     text=COMMAND, team=BOT.team_id), **changes)


def rich_text(text):
    return [dict(type="rich_text", block_id="generated", elements=[
        dict(type="rich_text_section", elements=[dict(type="text", text=text)])])]


def test_principal_roundtrip_is_exact_and_frozen():
    value = BOT.to_dict()
    assert BotPrincipal.from_dict(value) == BOT
    value["user_id"] = HUMAN
    assert BOT.user_id != HUMAN
    with pytest.raises(FrozenInstanceError):
        BOT.user_id = HUMAN
    for bad in (None, [], dict(BOT.to_dict(), name="friendly"), {"user_id": BOT.user_id}):
        with pytest.raises(ValueError, match="^bot_principal_invalid$"):
            BotPrincipal.from_dict(bad)


@pytest.mark.parametrize("key,value", [
    ("team_id", "T1"), ("team_id", "t00000001"), ("user_id", "B00000001"),
    ("bot_id", "B00000001\n"), ("app_id", None), ("app_id", 123),
])
def test_principal_rejects_malformed_identity(key, value):
    with pytest.raises(ValueError, match="^bot_principal_invalid$"):
        BotPrincipal(**dict(BOT.to_dict(), **{key: value}))


def test_authenticated_association_and_cache_do_not_cache_delegate_authority():
    api = FakeApi()
    verifier = BotVerifier(api)
    assert verifier.context() == OWN
    assert verifier.context() == OWN
    assert len(api.calls) == 3
    assert verifier.bot(BOT.user_id) == BOT
    assert verifier.human_owner(HUMAN) == HUMAN
    api.users[BOT.user_id]["deleted"] = True
    with pytest.raises(ValueError, match="bot_verify_inactive"):
        verifier.bot(BOT.user_id)
    assert sum(method == "auth.test" for method, _ in api.calls) == 1
    BotVerifier(api).context()
    assert sum(method == "auth.test" for method, _ in api.calls) == 2


@pytest.mark.parametrize("section,key,value", [
    ("user", "id", HUMAN), ("user", "team_id", "T00000099"),
    ("user", "is_bot", False), ("user", "deleted", True),
    ("user", "suspended", True), ("user", "bot_id", "B00000099"),
    ("profile", "bot_id", "B00000099"), ("profile", "api_app_id", "A00000099"),
    ("profile", "team", "T00000099"), ("profile", "api_app_id", None),
    ("bot", "id", "B00000099"), ("bot", "user_id", HUMAN),
    ("bot", "app_id", "A00000099"), ("bot", "deleted", True),
    ("bot", "team_id", "T00000099"),
])
def test_bot_metadata_mismatch_never_establishes_identity(section, key, value):
    api = FakeApi()
    verifier = BotVerifier(api)
    verifier.context()
    target = {"user": api.users[BOT.user_id], "profile": api.users[BOT.user_id]["profile"],
              "bot": api.bots[BOT.bot_id]}[section]
    target[key] = value
    with pytest.raises(ValueError):
        verifier.bot(BOT.user_id)


@pytest.mark.parametrize("field", ["user_id", "bot_id", "app_id"])
def test_own_user_bot_and_app_are_all_denied(field):
    api = FakeApi()
    verifier = BotVerifier(api)
    verifier.context()
    if field == "user_id":
        candidate = OWN.user_id
    else:
        candidate = BOT.user_id
        if field == "bot_id":
            api.users[candidate]["profile"]["bot_id"] = OWN.bot_id
            api.bots[OWN.bot_id] = dict(id=OWN.bot_id, user_id=candidate,
                                      app_id=BOT.app_id, deleted=False)
        else:
            api.users[candidate]["profile"]["api_app_id"] = OWN.app_id
            api.bots[BOT.bot_id]["app_id"] = OWN.app_id
    with pytest.raises(ValueError, match="bot_verify_self"):
        verifier.bot(candidate)


@pytest.mark.parametrize("change", [
    {"is_bot": True}, {"is_app_user": True}, {"deleted": True},
    {"is_agentforce_bot": True}, {"is_workflow_bot": True}, {"team_id": "T00000099"},
    {"profile": {"bot_id": BOT.bot_id}}, {"api_app_id": BOT.app_id},
])
def test_human_owner_cannot_be_bot_app_or_other_workspace(change):
    api = FakeApi()
    api.users[HUMAN].update(change)
    with pytest.raises(ValueError):
        BotVerifier(api).human_owner(HUMAN)


def test_context_failure_is_not_cached():
    api = FakeApi()
    api.auth["bot_id"] = BOT.bot_id
    verifier = BotVerifier(api)
    with pytest.raises(ValueError, match="bot_verify_identity_mismatch"):
        verifier.context()
    api.auth["bot_id"] = OWN.bot_id
    assert verifier.context() == OWN
    assert sum(method == "auth.test" for method, _ in api.calls) == 2


@pytest.mark.parametrize("error", [
    HTTPError("https://secret.invalid/credential", 429, "secret", {}, None),
    HTTPError("https://secret.invalid/credential", 503, "secret", {}, None),
    URLError("secret"), TimeoutError("secret"), ConnectionError("secret"),
], ids=["http-429", "http-503", "url-error", "timeout", "connection-error"])
def test_transient_provider_failures_are_safe_and_retryable(error):
    def api(*args):
        raise error
    with pytest.raises(BotVerificationDeferred, match="^bot_verify_deferred$"):
        BotVerifier(api).context()


@pytest.mark.parametrize("value,expected", [
    ({"ok": False, "error": "missing_scope"}, "bot_verify_missing_scope"),
    ({"ok": False, "error": "invalid_auth"}, "bot_verify_api_error"),
    ({"ok": True}, "bot_verify_context_invalid"),
    ([], "bot_verify_malformed_response"),
])
def test_malformed_auth_or_denial_is_not_a_transient_grant(value, expected):
    with pytest.raises(ValueError, match="^" + expected + "$"):
        BotVerifier(lambda *args: value).context()


@pytest.mark.parametrize("error", ["ratelimited", "internal_error", "service_unavailable"])
def test_provider_error_responses_defer(error):
    with pytest.raises(BotVerificationDeferred):
        BotVerifier(lambda *args: dict(ok=False, error=error)).context()


def test_api_helper_uses_header_token_and_exact_read_methods(monkeypatch):
    requests = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, maximum):
            assert maximum == 1_048_577
            return json.dumps({"ok": True}).encode()
    def fake_open(request, timeout):
        requests.append(request)
        assert timeout == 10
        assert request.get_header("Authorization") == "Bearer xoxb-synthetic"
        assert "xoxb" not in request.full_url
        return Response()
    monkeypatch.setattr(identity, "urlopen", fake_open)
    for method, params in (("auth.test", {}), ("users.info", {"user": BOT.user_id}),
                           ("bots.info", {"bot": BOT.bot_id})):
        assert bot_api_call("xoxb-synthetic", method, params) == {"ok": True}
    assert requests[0].get_method() == "POST"
    assert all(request.get_method() == "GET" for request in requests[1:])
    for method, params in (("chat.postMessage", {}), ("auth.test", {"token": "secret"}),
                           ("users.info", {"user": "untrusted"})):
        with pytest.raises(ValueError, match="^bot_verify_api_error$"):
            bot_api_call("xoxb-synthetic", method, params)
    assert len(requests) == 3


@pytest.mark.parametrize("token,method,params", [
    ("xoxb-synthetic\nsecret", "auth.test", {}), ("xoxb-", "auth.test", {}),
    (None, "auth.test", {}), ("xoxb-synthetic", [], {}),
    ("xoxb-synthetic", "users.info", []), ("xoxb-synthetic", "bots.info", {}),
])
def test_bad_helper_arguments_never_reach_network(token, method, params):
    with pytest.raises(ValueError, match="^bot_verify_api_error$"):
        bot_api_call(token, method, params)


@pytest.mark.parametrize("raw", [b"not JSON secret", b'[]', b'{"ok": true', b'\xff'])
def test_api_helper_malformed_payloads_have_safe_errors(monkeypatch, raw):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, maximum):
            return raw
    monkeypatch.setattr(identity, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match="^bot_verify_malformed_response$"):
        bot_api_call("xoxb-synthetic", "auth.test", {})


def test_api_helper_preserves_retryable_http_failure(monkeypatch):
    def unavailable(*args, **kwargs):
        raise HTTPError("https://secret.invalid", 503, "private body", {}, None)
    monkeypatch.setattr(identity, "urlopen", unavailable)
    with pytest.raises(BotVerificationDeferred, match="^bot_verify_deferred$"):
        bot_api_call("xoxb-synthetic", "auth.test", {})


@pytest.mark.parametrize("text,result", [
    ("bot add <@U00000002>", ("add", BOT.user_id)),
    ("bot remove <@U00000002|friendly>", ("remove", BOT.user_id)),
    ("bot access", ("access", None)), ("ordinary bot chat", None),
    ("add <@U00000002>", None), (None, None),
])
def test_explicit_owner_control_grammar(text, result):
    assert parse_bot_control(text) == result


@pytest.mark.parametrize("text", [
    "bot", "BOT access", "bot ADD <@U00000002>", "bot add Dora", "bot add U00000002",
    "bot add <@U00000002> <@U00000003>", "bot access now", "bot bind <#C00000001>",
    "bot add\n<@U00000002>", "bot add <@U00000002>\r", "bot " + "x" * 500,
])
def test_malformed_reserved_bot_control_raises(text):
    with pytest.raises(ValueError, match="^bot_command_invalid$"):
        parse_bot_control(text)


def native_mention_control(mention=None, *, section_type="rich_text_section"):
    return dict(type="message", text=f"bot add <@{BOT.user_id}>", blocks=[
        dict(type="rich_text", block_id="generated", elements=[
            dict(type=section_type, elements=[dict(type="text", text="bot add "),
                dict(type="user", user_id=BOT.user_id, from_llm=False)
                if mention is None else mention])])])


def test_native_mention_false_provenance_metadata_preserves_owner_control():
    assert parse_bot_control_event(native_mention_control()) == ("add", BOT.user_id)
    assert parse_bot_control_event(native_mention_control(
        dict(type="user", user_id=BOT.user_id))) == ("add", BOT.user_id)


@pytest.mark.parametrize("value", [True, None, 0, 1, "false", {}, []])
def test_unobserved_or_malformed_mention_provenance_is_rejected(value):
    event = native_mention_control(dict(type="user", user_id=BOT.user_id, from_llm=value))
    with pytest.raises(ValueError, match="^bot_command_content_invalid$"):
        parse_bot_control_event(event)


@pytest.mark.parametrize("change", [
    {"style": {}}, {"style": {"code": True}}, {"extra": "hidden content"},
    {"user_id": HUMAN}, {"type": "link", "url": "https://example.invalid"},
])
def test_false_provenance_does_not_relax_visible_mention_validation(change):
    event = native_mention_control(dict(dict(type="user", user_id=BOT.user_id, from_llm=False), **change))
    with pytest.raises(ValueError, match="^bot_command_content_invalid$"):
        parse_bot_control_event(event)


@pytest.mark.parametrize("section_type", ["rich_text_quote", "rich_text_preformatted"])
def test_false_provenance_does_not_admit_quoted_or_preformatted_controls(section_type):
    with pytest.raises(ValueError, match="^bot_command_content_invalid$"):
        parse_bot_control_event(native_mention_control(section_type=section_type))


def test_false_provenance_mentions_remain_forbidden_in_bot_instructions():
    event = native_mention_control()
    event["text"] = f"!codex {REQUEST}\nInspect <@{BOT.user_id}>"
    event["blocks"][0]["elements"][0]["elements"][0]["text"] = f"!codex {REQUEST}\nInspect "
    assert parse_bot_instruction(event) is None


def test_false_provenance_is_not_accepted_on_unrelated_text_elements():
    event = native_mention_control()
    event["blocks"][0]["elements"][0]["elements"][0]["from_llm"] = False
    with pytest.raises(ValueError, match="^bot_command_content_invalid$"):
        parse_bot_control_event(event)


def test_false_provenance_does_not_replace_required_native_user_identity():
    with pytest.raises(ValueError, match="^bot_command_content_invalid$"):
        parse_bot_control_event(native_mention_control(dict(type="user", from_llm=False)))


def test_instruction_preserves_prompt_and_plain_generated_rich_text():
    assert parse_bot_instruction(event()) == (REQUEST, "Inspect the failing test.")
    assert parse_bot_instruction(event(blocks=rich_text(COMMAND))) == (
        REQUEST, "Inspect the failing test.")
    plain = "!codex " + REQUEST + "\nExplain A & B < C."
    encoded = plain.replace("&", "&amp;").replace("<", "&lt;")
    assert parse_bot_instruction(event(text=encoded, blocks=rich_text(plain))) == (
        REQUEST, "Explain A &amp; B &lt; C.")
    assert parse_bot_instruction(event(text="!codex " + REQUEST + "\n" + "x" * MAX_PROMPT_CHARS))


def test_inline_instruction_preserves_plain_content_and_whitespace():
    assert parse_bot_instruction(event(text=INLINE_COMMAND)) == (REQUEST, "Inspect the failing test.")
    assert parse_bot_instruction(event(text=INLINE_COMMAND, blocks=rich_text(INLINE_COMMAND))) == (
        REQUEST, "Inspect the failing test.")
    prompt = " \tInspect A & B < C -- keep the body unchanged.  "
    text = "!codex " + REQUEST + " -- " + prompt
    assert parse_bot_instruction(event(text=text, blocks=rich_text(text))) == (REQUEST, prompt)
    encoded = text.replace("&", "&amp;").replace("<", "&lt;")
    assert parse_bot_instruction(event(text=encoded, blocks=rich_text(text))) == (
        REQUEST, prompt.replace("&", "&amp;").replace("<", "&lt;"))


@pytest.mark.parametrize("separator", ["\n", " -- "])
@pytest.mark.parametrize("length", [MAX_PROMPT_CHARS - 1, MAX_PROMPT_CHARS,
                                    MAX_PROMPT_CHARS + 1, MAX_PROMPT_CHARS + 3,
                                    MAX_PROMPT_CHARS + 4])
def test_both_instruction_headers_apply_limit_to_exact_body(separator, length):
    prompt = "x" * length
    parsed = parse_bot_instruction(event(text="!codex " + REQUEST + separator + prompt))
    assert parsed == ((REQUEST, prompt) if length <= MAX_PROMPT_CHARS else None)


@pytest.mark.parametrize("header", [
    " !codex " + REQUEST + " -- ", "!CODEX " + REQUEST + " -- ",
    "!codex  " + REQUEST + " -- ", "!codex\t" + REQUEST + " -- ",
    "!codex " + REQUEST + "-- ", "!codex " + REQUEST + " --",
    "!codex " + REQUEST + "\t-- ", "!codex " + REQUEST + " --\t",
    "!codex " + REQUEST + " - ", "!codex " + REQUEST + " — ",
    "!codex " + REQUEST + " – ", "!codex " + REQUEST + "  -- ",
    "!codex " + REQUEST + "\u00a0-- ", "!codex " + REQUEST + " --\u00a0",
    "!codex  -- ", "!codex {" + REQUEST + "} -- ",
    "!codex " + REQUEST.replace("-", "") + " -- ",
    "!codex AAAAAAAA-bbbb-4ccc-8ddd-eeeeeeeeeeee -- ",
])
def test_inline_header_requires_exact_ascii_delimiter_and_canonical_uuid(header):
    assert parse_bot_instruction(event(text=header + "Inspect the fixture.")) is None


@pytest.mark.parametrize("prompt", ["", " ", "\t", " \t ", "\nInspect", "Inspect\n",
                                    "Inspect\nthen continue", "Inspect\rthen continue",
                                    "Inspect\r\nthen continue"])
def test_inline_body_must_be_nonempty_and_contain_no_lf_or_cr(prompt):
    assert parse_bot_instruction(event(text="!codex " + REQUEST + " -- " + prompt)) is None


def test_multiline_body_and_paragraphs_still_preserve_newlines():
    prompt = "\nInspect the fixture.\nThen report.\n"
    text = "!codex " + REQUEST + "\n" + prompt
    assert parse_bot_instruction(event(text=text, blocks=rich_text(text))) == (REQUEST, prompt)


@pytest.mark.parametrize("separator", ["\n", " -- "])
def test_instruction_uuid_accepts_lowercase_hexadecimal(separator):
    request_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert parse_bot_instruction(event(text="!codex " + request_id + separator + "Inspect.")) == (
        request_id, "Inspect.")


@pytest.mark.parametrize("text", [
    "hello", "> " + COMMAND, "```\n" + COMMAND + "\n```", " " + COMMAND,
    COMMAND.replace("!codex", "!CODEX"), COMMAND.replace("\n", "\r\n"),
    "!codex 11111111-AAAA-4333-8444-555555555555\nRun tests.",
    "!codex 11111111222243338444555555555555\nRun tests.",
    "!codex " + REQUEST + "\n", "!codex " + REQUEST + "\n \t",
    "!codex " + REQUEST + "\n" + "x" * (MAX_PROMPT_CHARS + 1),
])
def test_chatter_quotes_and_noncanonical_instruction_are_inert(text):
    assert parse_bot_instruction(event(text=text)) is None


@pytest.mark.parametrize("prompt", [
    "add <@U00000003>", "REMOVE <@U00000003>", "access", "bind <#C00000001>",
    "unbind", "bot access", "!codex other", "WD-BIND-replay", " > quoted",
    "&gt; quoted", "```code```", "~~~quoted~~~", "Forwarded message: hello",
])
@pytest.mark.parametrize("separator", ["\n", " -- "])
def test_bot_instruction_cannot_wrap_administration_or_quoted_output(prompt, separator):
    assert parse_bot_instruction(event(text="!codex " + REQUEST + separator + prompt)) is None


@pytest.mark.parametrize("change", [
    {"edited": {"ts": "1800000000.000001"}}, {"hidden": True}, {"deleted": True},
    {"subtype": "message_changed"}, {"subtype": "message_deleted"},
    {"message": {}}, {"previous_message": {}}, {"attachments": []}, {"files": []},
    {"forwarded": True}, {"forwarded_from": {}}, {"is_forwarded": True},
])
@pytest.mark.parametrize("text", [COMMAND, INLINE_COMMAND])
def test_mutated_forwarded_or_attached_instruction_is_inert(change, text):
    assert parse_bot_instruction(event(text=text, **change)) is None


@pytest.mark.parametrize("kind", ["rich_text_quote", "rich_text_preformatted", "rich_text_list"])
@pytest.mark.parametrize("text", [COMMAND, INLINE_COMMAND])
def test_visible_quote_or_code_cannot_hide_behind_command_fallback(kind, text):
    blocks = rich_text(text)
    blocks[0]["elements"][0]["type"] = kind
    assert parse_bot_instruction(event(text=text, blocks=blocks)) is None


@pytest.mark.parametrize("element", [
    {"type": "link", "url": "https://example.invalid", "text": COMMAND},
    {"type": "user", "user_id": HUMAN}, {"type": "broadcast", "range": "here"},
    {"type": "text", "text": COMMAND, "style": {"code": True}},
    {"type": "text", "text": "Hidden different instructions"},
])
@pytest.mark.parametrize("text", [COMMAND, INLINE_COMMAND])
def test_rich_text_must_be_proven_identical_plain_content(element, text):
    blocks = rich_text(text)
    element = dict(element)
    if element.get("text") == COMMAND:
        element["text"] = text
    blocks[0]["elements"][0]["elements"] = [element]
    assert parse_bot_instruction(event(text=text, blocks=blocks)) is None


@pytest.mark.parametrize("text,visible", [
    (INLINE_COMMAND, INLINE_COMMAND.replace(" -- ", "\n")),
    (INLINE_COMMAND, INLINE_COMMAND.replace(" -- ", "  ")),
    (COMMAND.replace("\n", " "), COMMAND),
    (COMMAND.replace("\n", "  "), COMMAND.replace("\n", "\n\n")),
])
def test_new_inline_form_does_not_normalize_mismatched_or_flattened_content(text, visible):
    assert parse_bot_instruction(event(text=text, blocks=rich_text(visible))) is None


def test_sender_metadata_uses_trusted_team_and_accepts_modern_and_classic_subtypes():
    assert event_principal(event(), BOT.team_id, BOT)
    assert event_principal(event(subtype="bot_message"), BOT.team_id, BOT)
    assert event_principal(event(bot_profile=dict(id=BOT.bot_id, user_id=BOT.user_id,
                           app_id=BOT.app_id, team_id=BOT.team_id, deleted=False)), BOT.team_id, BOT)
    assert not event_principal(event(), None, BOT)
    assert not event_principal(event(), "T00000099", BOT)
    assert not event_principal(event(user=None), BOT.team_id, BOT)
    assert not event_principal(event(bot_id=None), BOT.team_id, BOT)
    # The outer receiving app ID must never be copied into sender metadata.
    assert not event_principal(event(api_app_id=OWN.app_id), BOT.team_id, BOT)


@pytest.mark.parametrize("change", [
    {"team": "T00000099"}, {"team_id": None}, {"app_id": OWN.app_id},
    {"user": HUMAN}, {"bot_id": OWN.bot_id}, {"subtype": "message_changed"},
    {"bot_profile": None}, {"bot_profile": {"id": OWN.bot_id}},
    {"bot_profile": {"app_id": OWN.app_id}}, {"bot_profile": {"user_id": HUMAN}},
    {"bot_profile": {"team_id": "T00000099"}}, {"bot_profile": {"deleted": True}},
])
def test_conflicting_sender_metadata_is_never_ignored(change):
    assert not event_principal(event(**change), BOT.team_id, BOT)
