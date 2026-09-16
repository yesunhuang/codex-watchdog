import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_watchdog import messaging_setup, onebot_setup
from codex_watchdog.messaging_profile import (
    ConfigurationState, PREFIX, SELECTOR, SLACK_SERVICE, detect, load_saved,
)
from codex_watchdog.onebot_setup import ACCOUNT, PROFILE, SERVICE, setup_onebot


class Store:
    def __init__(self, platform="darwin"):
        self.platform, self.values = platform, {}

    def has(self, service, account):
        return (service, account) in self.values

    def get(self, service, account, **kwargs):
        return self.values[(service, account)]

    def put(self, service, account, value, **kwargs):
        assert not self.has(service, account)
        self.values[(service, account)] = value

    def _security(self, *args):
        return SimpleNamespace(returncode=44)


def pairing(url="ws://127.0.0.1:3001", token=None, **kwargs):
    return dict(schema_version=1, protocol_version=11, ws_url=url, self_id="12345",
                chat_type="group", chat_id="67890", allowed_user_ids=["54321"])


def configure(root, store, **kwargs):
    return setup_onebot(environment={}, root=root, store=store, is_interactive=True,
                        read=lambda _: "ws://127.0.0.1:3001", secret=lambda _: "fixture-secret",
                        pair=pairing, **kwargs)


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_pairing_saved_in_existing_platform_boundary_and_reused(tmp_path, platform):
    store = Store(platform)
    assert configure(tmp_path, store) == 0
    route = json.loads((tmp_path / PROFILE).read_text())
    assert route["allowed_user_ids"] == ["54321"] and route["interactive_transport"] == "onebot"
    assert "fixture-secret" not in (tmp_path / PROFILE).read_text()
    if platform == "linux":
        path = tmp_path / onebot_setup.ENVIRONMENT
        assert path.stat().st_mode & 0o777 == 0o600
        assert not store.values
    else:
        assert store.values == {(SERVICE, ACCOUNT): "fixture-secret"}
    assert detect({}, tmp_path, store).state == ConfigurationState.CONFIGURED
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert configure(tmp_path, store) == 0
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    loaded = load_saved({}, tmp_path, store)
    assert loaded[SELECTOR] == "onebot"
    assert loaded[PREFIX + "ONEBOT_ACCESS_TOKEN"] == "fixture-secret"


def test_adding_onebot_preserves_slack_credentials_unknown_keys_and_choice(tmp_path):
    store = Store()
    store.values[(SLACK_SERVICE, "slack-bot-token")] = "xoxb-existing-fixture"
    route = dict(schema_version=1, channel_id="C12345678", allowed_user_ids=["U12345678"],
                 reply_mode="poll", future_key={"keep": True})
    path = tmp_path / "slack-relay.json"
    path.write_text(json.dumps(route))
    before = path.read_bytes()
    assert configure(tmp_path, store) == 0
    assert path.read_bytes() == before
    assert store.values[(SLACK_SERVICE, "slack-bot-token")] == "xoxb-existing-fixture"
    assert json.loads((tmp_path / PROFILE).read_text())["interactive_transport"] == "slack+onebot"
    loaded = load_saved({}, tmp_path, store)
    assert loaded[SELECTOR] == "slack+onebot"
    assert loaded[PREFIX + "SLACK_BOT_TOKEN"] == "xoxb-existing-fixture"
    explicit = load_saved({SELECTOR: "slack"}, tmp_path, store)
    assert explicit[SELECTOR] == "slack"


def test_partial_explicit_provider_never_loads_saved_token(tmp_path):
    store = Store()
    assert configure(tmp_path, store) == 0
    with pytest.raises(Exception, match="partial_explicit_onebot"):
        load_saved({PREFIX + "ONEBOT_WS_URL": "ws://different.example:3001"}, tmp_path, store)


def test_failed_pairing_never_writes_credentials_or_routing(tmp_path):
    store = Store()
    def failed(*args, **kwargs):
        raise TimeoutError("private fixture details")
    output = []
    assert setup_onebot(environment={}, root=tmp_path, store=store, is_interactive=True,
                        read=lambda _: "ws://127.0.0.1:3001", secret=lambda _: "fixture-secret",
                        pair=failed, output=output.append) == 1
    assert not store.values and not (tmp_path / PROFILE).exists()
    assert all("private fixture details" not in text for text in output)


def test_unknown_profile_keys_preserved_and_noninteractive_check_is_read_only(tmp_path):
    store = Store()
    assert configure(tmp_path, store) == 0
    path = tmp_path / PROFILE
    route = json.loads(path.read_text())
    route["future_setting"] = {"preserve": 1}
    path.write_text(json.dumps(route))
    before = path.read_bytes()
    assert setup_onebot(environment={}, root=tmp_path, store=store, check=True) == 0
    assert setup_onebot(environment={}, root=tmp_path, store=store, is_interactive=False) == 0
    assert path.read_bytes() == before


def test_fresh_menu_choice_five_uses_nonce_pairing(tmp_path, monkeypatch):
    store = Store()
    monkeypatch.setattr(onebot_setup, "pair_onebot", pairing)
    answers = iter(["5", "ws://127.0.0.1:3001"])
    assert messaging_setup.setup(environment={}, root=tmp_path, store=store, is_interactive=True,
        read=lambda _: next(answers), secret=lambda _: "fixture-secret") == 0
    assert detect({}, tmp_path, store).state == ConfigurationState.CONFIGURED
    assert json.loads((tmp_path / "messaging-setup.json").read_text())["transport"] == "onebot"


def test_notification_only_explicit_config_stays_notification_only(tmp_path):
    env = {PREFIX + "ONEBOT_" + key: value for key, value in dict(
        WS_URL="ws://127.0.0.1:3001", ACCESS_TOKEN="fixture", SELF_ID="12345",
        CHAT_TYPE="group", CHAT_ID="67890").items()}
    loaded = load_saved(env, tmp_path, Store())
    assert PREFIX + "ONEBOT_ALLOWED_USER_IDS" not in loaded
    assert loaded[SELECTOR] == "onebot"


def test_existing_incomplete_credential_is_preserved(tmp_path):
    store = Store()
    store.values[(SERVICE, ACCOUNT)] = "retain-orphan-fixture"
    assert configure(tmp_path, store) == 1
    assert store.values[(SERVICE, ACCOUNT)] == "retain-orphan-fixture"
    assert not (tmp_path / PROFILE).exists()
