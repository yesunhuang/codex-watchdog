from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from codex_watchdog.messaging_setup import setup, prepare_launch
from codex_watchdog.messaging_profile import (
    ConfigurationState as State, SecretStore, MessagingError, MARKER, PREFIX, SELECTOR,
    SLACK_SERVICE, LARK_SERVICE, detect, load_saved, json_bytes, write_new, linux_environment,
)


class FixtureStore:
    platform = "win32"

    def __init__(self, root):
        self.root, self.values = root, {}
        self.reads = []

    def has(self, service, account):
        return (self.root / (account + ".clixml")).exists()

    def _security(self, *args):
        return SimpleNamespace(returncode=44)

    def get(self, service, account, **kwargs):
        self.reads.append(account)
        return self.values[account]

    def put(self, service, account, value, **kwargs):
        self.values[account] = value
        write_new(self.root / (account + ".clixml"), b"encrypted fixture; no credential bytes")


def route(provider):
    if provider == "slack":
        return dict(schema_version=1, channel_id="C12345678", allowed_user_ids=["U12345678"], future="preserve")
    return dict(schema_version=1, domain="feishu", app_id="cli_fixture00001", chat_id="oc_fixture00001",
                allowed_user_ids=["ou_fixture00001"], future="preserve")


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def configure(root, providers):
    root.mkdir(mode=0o700)
    store = FixtureStore(root)
    for provider in providers:
        (root / (provider + "-relay.json")).write_bytes(json_bytes(route(provider)))
        if provider == "slack":
            store.put(SLACK_SERVICE, "slack-bot-token", "xoxb-fixture")
            store.put(SLACK_SERVICE, "slack-app-token", "xapp-fixture")
        else:
            store.put(LARK_SERVICE, "lark-app-secret", "lark-fixture")
    return store


def forbidden(*args, **kwargs):
    raise AssertionError("must not prompt/connect/read credentials")


@pytest.mark.parametrize("selection", ["slack", "lark", "both"])
def test_complete_previous_profiles_reused_without_prompt_or_byte_changes(tmp_path, selection):
    providers = ("slack", "lark") if selection == "both" else (selection,)
    store = configure(tmp_path / "config", providers)
    env = {SELECTOR: selection}
    before = snapshot(store.root)
    assert detect(env, store.root, store).state == State.CONFIGURED
    assert store.reads == []  # Detection never decrypts credentials.
    assert setup(environment=env, root=store.root, store=store, auto=True, is_interactive=True,
                 read=forbidden, secret=forbidden, pair=forbidden) == 0
    loaded = prepare_launch(tmp_path / "runtime", environment=env, root=store.root, store=store)
    assert loaded[SELECTOR] == selection
    assert snapshot(store.root) == before


@pytest.mark.parametrize("name", ["slack-relay.json", "slack-bot-token.clixml", "slack-app-token.clixml",
    "slack-webhook.clixml", "lark-relay.json", "lark-app-secret.clixml", "feishu-launcher.json",
    "feishu-test/pairing-v1.json", "feishu-test/app-credential-v1.clixml", "linux-notifications.env", MARKER])
@pytest.mark.parametrize("contents", [b"", b"{}", b"malformed legacy bytes"])
def test_every_legacy_file_suppresses_auto_without_rewriting(tmp_path, name, contents):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)
    store = FixtureStore(tmp_path)
    before, output = snapshot(tmp_path), []
    assert detect({}, tmp_path, store).state == State.EXISTING_OR_PARTIAL
    assert setup(environment={}, root=tmp_path, store=store, auto=True, is_interactive=True,
                 read=forbidden, pair=forbidden, output=output.append) == 0
    assert snapshot(tmp_path) == before
    assert "setup-messaging" in " ".join(output)


@pytest.mark.parametrize("key", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_WEBHOOK_URL", "SLACK_CHANNEL_ID",
    "SLACK_ALLOWED_USER_IDS", "SLACK_REPLY_MODE", "LARK_APP_ID", "LARK_APP_SECRET", "LARK_CHAT_ID",
    "LARK_ALLOWED_USER_IDS", "LARK_DOMAIN", "INTERACTIVE_TRANSPORT"])
@pytest.mark.parametrize("value", ["", "partial"])
def test_partial_environment_even_empty_is_evidence(tmp_path, key, value):
    env = {PREFIX + key: value}
    store = FixtureStore(tmp_path)
    assert detect(env, tmp_path, store).state == State.EXISTING_OR_PARTIAL
    assert setup(environment=env, root=tmp_path, store=store, auto=True, is_interactive=True, read=forbidden) == 0
    assert snapshot(tmp_path) == {}


def test_pristine_skip_auto_prompts_once_and_preserves_marker(tmp_path):
    root, output, calls = tmp_path / "config", [], []
    store = FixtureStore(root)
    def skip(prompt):
        calls.append(prompt)
        return "4"
    assert detect({}, root, store).state == State.PRISTINE_UNCONFIGURED
    for _ in range(2):
        assert setup(environment={}, root=root, store=store, auto=True, is_interactive=True,
                     read=skip, secret=forbidden, pair=forbidden, output=output.append) == 0
    assert len(calls) == 1
    assert json.loads((root / MARKER).read_bytes()) == dict(schema_version=1, status="skipped")
    assert detect({}, root, store).state == State.EXISTING_OR_PARTIAL


def test_headless_pristine_never_prompts_or_writes(tmp_path):
    root, output = tmp_path / "config", []
    assert setup(environment={}, root=root, store=FixtureStore(root), auto=True, is_interactive=False,
                 read=forbidden, pair=forbidden, output=output.append) == 0
    assert not root.exists()
    assert "interactive" in " ".join(output)


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), EOFError(), MessagingError("messaging_pairing_timed_out")])
def test_failed_or_cancelled_pairing_can_retry_on_next_foreground_launch(tmp_path, failure):
    answers = iter(["2", "feishu", "cli_fixture00001"])
    def failed(*a, **kw):
        raise failure
    store = FixtureStore(tmp_path)
    assert setup(environment={}, root=tmp_path, store=store, is_interactive=True,
        read=lambda _: next(answers), secret=lambda _: "never-persist-this-secret", pair=failed) != 0
    assert store.values == {}
    files = snapshot(tmp_path)
    assert set(files) == {MARKER, "setup.lock"}
    assert b"never-persist" not in b"".join(files.values())
    assert json.loads(files[MARKER])["status"] in ("failed", "cancelled")
    prompts = []
    def retry(prompt):
        prompts.append(prompt)
        return "4"
    assert setup(environment={}, root=tmp_path, store=store, auto=True, is_interactive=True,
                 read=retry, secret=forbidden, pair=forbidden) == 0
    assert len(prompts) == 1
    assert json.loads((tmp_path / MARKER).read_bytes())["status"] == "skipped"


@pytest.mark.parametrize("status", ["started", "failed", "cancelled"])
def test_marker_only_retry_is_interactive_and_preserves_unknown_fields(tmp_path, status):
    (tmp_path / MARKER).write_bytes(json_bytes(dict(schema_version=1, status=status, future="keep")))
    store = FixtureStore(tmp_path)
    before = snapshot(tmp_path)
    assert setup(environment={}, root=tmp_path, store=store, auto=True, is_interactive=False,
                 read=forbidden) == 0
    assert snapshot(tmp_path) == before
    prompts = []
    assert setup(environment={}, root=tmp_path, store=store, auto=True, is_interactive=True,
                 read=lambda prompt: prompts.append(prompt) or "4") == 0
    assert len(prompts) == 1
    assert json.loads((tmp_path / MARKER).read_bytes())["future"] == "keep"


@pytest.mark.parametrize("failure,reason", [
    (MessagingError("messaging_pairing_expired"), "messaging_pairing_expired"),
    (RuntimeError("xoxb-private-sdk-exception"), "messaging_setup_failed_or_busy"),
    (MessagingError("provider says: xoxb-private-sdk-exception"), "messaging_setup_failed_or_busy"),
])
def test_failed_setup_records_safe_reason_and_check_is_read_only(tmp_path, failure, reason):
    store, output = FixtureStore(tmp_path), []
    def fail(*args, **kwargs):
        raise failure
    assert setup(environment={}, root=tmp_path, store=store, is_interactive=True,
                 read=lambda _: "1", secret=lambda _: "xoxb-private",
                 pair=fail, output=output.append) == 1
    assert json.loads((tmp_path / MARKER).read_bytes())["last_error"] == reason
    before = snapshot(tmp_path)
    output.clear()
    assert setup(environment={}, root=tmp_path, store=store, check=True,
                 read=forbidden, output=output.append) == 0
    assert json.loads(output[0])["last_error"] == reason
    assert snapshot(tmp_path) == before
    assert b"xoxb-private" not in b"".join(before.values())
    assert "xoxb-private" not in " ".join(output)


@pytest.mark.parametrize("name", ["slack-relay.json", "slack-bot-token.clixml", "lark-relay.json"])
def test_retry_never_overwrites_partial_saved_provider(tmp_path, name):
    (tmp_path / MARKER).write_bytes(json_bytes(dict(schema_version=1, status="failed")))
    (tmp_path / name).write_bytes(b"preserve partial state")
    before = snapshot(tmp_path)
    assert setup(environment={}, root=tmp_path, store=FixtureStore(tmp_path), auto=True,
                 is_interactive=True, read=forbidden) == 0
    assert snapshot(tmp_path) == before


def test_cli_keeps_specific_safe_startup_error(monkeypatch, capsys, tmp_path):
    from codex_watchdog import cli, messaging_setup
    def fail(*args, **kwargs):
        raise MessagingError("messaging_pairing_expired")
    monkeypatch.setattr(messaging_setup, "prepare_launch", fail)
    assert cli.main(["--runtime", str(tmp_path), "run", "--once"]) == 1
    assert "messaging_pairing_expired" in capsys.readouterr().err


def test_pairing_diagnostics_survive_failed_setup_without_message_or_secret(tmp_path):
    store = FixtureStore(tmp_path)
    def fail(*args, **kwargs):
        error = MessagingError("messaging_pairing_expired")
        error.pairing_diagnostics = dict(provider="slack", conversation_id="C12345678",
            code_sha256="a"*64, device_label="b"*12, started_at=1700000000.0,
            poll_count=36, observed_message_count=3, exact_code_seen=False,
            text="never-store-this-message", bot_token="xoxb-private")
        raise error
    assert setup(environment={}, root=tmp_path, store=store, is_interactive=True,
                 read=lambda _: "1", secret=lambda _: "xoxb-private", pair=fail) == 1
    data = json.loads((tmp_path / MARKER).read_bytes())
    assert data["pairing"]["poll_count"] == 36
    assert not data["pairing"]["exact_code_seen"]
    assert "text" not in data["pairing"] and "bot_token" not in data["pairing"]
    output = []
    assert setup(environment={}, root=tmp_path, store=store, check=True, output=output.append) == 0
    assert json.loads(output[0])["pairing"] == data["pairing"]


def test_both_providers_pair_before_any_credentials_are_saved(tmp_path):
    answers = iter(["3", "feishu", "cli_fixture00001"])
    tokens = iter(["xoxb-private", "lark-private"])
    store = FixtureStore(tmp_path)
    def pair(provider, values, runtime, **kwargs):
        assert not store.values
        return route(provider)
    assert setup(environment={}, root=tmp_path, store=store, is_interactive=True,
                 read=lambda _: next(answers), secret=lambda _: next(tokens), pair=pair) == 0
    assert detect({}, tmp_path, store).state == State.CONFIGURED
    env = load_saved({}, tmp_path, store)
    assert env[SELECTOR] == "both"
    assert env[PREFIX + "LARK_APP_SECRET"] == "lark-private"
    assert b"private" not in b"".join(snapshot(tmp_path).values())
    assert env[PREFIX + "SLACK_REPLY_MODE"] == env[PREFIX + "LARK_REPLY_MODE"] == "poll"
    assert PREFIX + "SLACK_APP_TOKEN" not in env


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_new_desktop_setup_saves_poll_without_app_token_or_listener_confirmation(tmp_path, platform):
    store = FixtureStore(tmp_path)
    store.platform = platform
    prompts = []
    def read(prompt):
        prompts.append(prompt)
        assert "Choose 1-5" in prompt
        return "1"
    assert setup(environment={}, root=tmp_path, store=store, is_interactive=True,
                 read=read, secret=lambda _: "xoxb-fixture", pair=lambda *a, **k: route("slack")) == 0
    assert len(prompts) == 1
    env = load_saved({}, tmp_path, store)
    assert env[PREFIX + "SLACK_REPLY_MODE"] == "poll"
    assert PREFIX + "SLACK_APP_TOKEN" not in env
    assert detect({}, tmp_path, store).state == State.CONFIGURED
    from codex_watchdog.notifications import NotificationConfig
    config = NotificationConfig.from_environment(env)
    assert config.slack_reply_mode == "poll" and config.interactive_relay_configured


@pytest.mark.parametrize("provider", ["slack", "lark"])
def test_saved_reply_mode_and_explicit_mode_choices_preserved(tmp_path, provider):
    store = configure(tmp_path / "config", (provider,))
    path = store.root / (provider + "-relay.json")
    value = json.loads(path.read_bytes())
    value["reply_mode"] = "poll"
    path.write_bytes(json_bytes(value))
    before = snapshot(store.root)
    key = PREFIX + provider.upper() + "_REPLY_MODE"
    assert load_saved({}, store.root, store)[key] == "poll"
    assert load_saved({key: "socket"}, store.root, store)[key] == "socket"
    assert snapshot(store.root) == before


@pytest.mark.parametrize("provider,field", [("slack", "BOT_TOKEN"), ("slack", "CHANNEL_ID"),
    ("lark", "APP_ID"), ("lark", "APP_SECRET"), ("lark", "CHAT_ID"), ("lark", "DOMAIN")])
def test_partial_environment_is_not_mixed_with_saved_app(tmp_path, provider, field):
    store = configure(tmp_path / "config", (provider,))
    before = snapshot(store.root)
    with pytest.raises(MessagingError, match="partial_explicit"):
        load_saved({PREFIX + provider.upper() + "_" + field: "different"}, store.root, store)
    assert store.reads == [] and snapshot(store.root) == before


def test_complete_explicit_environment_wins_without_decryption(tmp_path):
    store = configure(tmp_path / "config", ("slack", "lark"))
    env = {PREFIX + "LARK_" + k: v for k, v in dict(APP_ID="cli_explicit0001", APP_SECRET="explicit",
        CHAT_ID="oc_explicit0001", DOMAIN="lark", ALLOWED_USER_IDS="ou_explicit0001").items()}
    env.update({PREFIX + "SLACK_" + k: v for k, v in dict(BOT_TOKEN="xoxb-explicit", APP_TOKEN="xapp-explicit",
        CHANNEL_ID="C99999999", ALLOWED_USER_IDS="U99999999").items()})
    env[SELECTOR] = "both"
    before = snapshot(store.root)
    assert load_saved(env, store.root, store) == env
    assert detect(env, store.root, store).state == State.CONFIGURED
    assert not store.reads and snapshot(store.root) == before


def test_second_provider_failure_never_claims_complete_or_overwrites_state(tmp_path):
    store = FixtureStore(tmp_path)
    original = store.put
    def fail_second(service, account, value, **kwargs):
        if account == "lark-app-secret":
            raise MessagingError("messaging_keychain_store_failed")
        return original(service, account, value, **kwargs)
    store.put = fail_second
    answers = iter(["3", "feishu", "cli_fixture00001"])
    tokens = iter(["xoxb-private", "lark-private"])
    assert setup(environment={}, root=tmp_path, store=store, is_interactive=True,
        read=lambda _: next(answers), secret=lambda _: next(tokens), pair=lambda provider, *a, **kw: route(provider)) == 1
    assert detect({}, tmp_path, store).state == State.EXISTING_OR_PARTIAL
    before = snapshot(tmp_path)
    assert b"private" not in b"".join(before.values())
    assert json.loads(before[MARKER])["status"] == "failed"
    assert setup(environment={}, root=tmp_path, store=store, auto=True, is_interactive=True, read=forbidden) == 0
    assert snapshot(tmp_path) == before


def test_background_terminal_process_is_not_interactive(monkeypatch):
    from codex_watchdog import messaging_setup as module
    class Terminal:
        def isatty(self): return False
    monkeypatch.setattr(module.sys, "stdin", Terminal())
    monkeypatch.setattr(module.sys, "stdout", Terminal())
    assert not module.interactive()


def test_unknown_or_unavailable_keychain_suppresses_onboarding(tmp_path):
    store = FixtureStore(tmp_path)
    def unavailable(*args): raise MessagingError("messaging_keychain_unavailable")
    store.has = unavailable
    assert detect({}, tmp_path, store).state == State.EXISTING_OR_PARTIAL
    assert setup(environment={}, root=tmp_path, store=store, auto=True, is_interactive=True, read=forbidden) == 0
    assert snapshot(tmp_path) == {}


@pytest.mark.skipif(os.name != "nt", reason="native current-user Windows DPAPI fixture")
def test_native_dpapi_roundtrip_compatible_with_powershell_and_no_plaintext(tmp_path):
    store = SecretStore(tmp_path)
    value = "xoxb-isolated-onboarding-fixture"
    store.put(SLACK_SERVICE, "slack-bot-token", value)
    assert value.encode() not in (tmp_path / "slack-bot-token.clixml").read_bytes()
    assert store.get(SLACK_SERVICE, "slack-bot-token") == value
    before = snapshot(tmp_path)
    with pytest.raises(MessagingError): store.put(SLACK_SERVICE, "slack-bot-token", "replace")
    assert snapshot(tmp_path) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode-600 EnvironmentFile boundary")
def test_native_linux_saved_environment_permissions_and_reuse(tmp_path):
    store = FixtureStore(tmp_path)
    store.platform = "linux"
    tmp_path.chmod(0o700)
    answers = iter(["1"])
    tokens = iter(["xoxb-fixture"])
    assert setup(environment={}, root=tmp_path, store=store, is_interactive=True, read=lambda _: next(answers),
        secret=lambda _: next(tokens), pair=lambda *a, **kw: route("slack")) == 0
    path = tmp_path / "linux-notifications.env"
    assert path.stat().st_mode & 0o777 == 0o600
    assert linux_environment(path)[PREFIX + "SLACK_BOT_TOKEN"] == "xoxb-fixture"
    assert linux_environment(path)[PREFIX + "SLACK_REPLY_MODE"] == "poll"
    assert detect({}, tmp_path, store).state == State.CONFIGURED
    # The selected environment still wins over a saved operational default.
    key = PREFIX + "SLACK_REPLY_MODE"
    assert load_saved({key: "socket"}, tmp_path, store)[key] == "socket"
    path.chmod(0o644)
    with pytest.raises(MessagingError): linux_environment(path)


def test_existing_linux_service_environment_is_not_reparsed_or_rewritten(tmp_path):
    store = FixtureStore(tmp_path)
    store.platform = "linux"
    legacy = tmp_path / "linux-notifications.env"
    legacy.write_bytes(b"# legacy file already interpreted by the service\nexport CODEX_WATCHDOG_SLACK_BOT_TOKEN='opaque'\n")
    before = snapshot(tmp_path)
    env = {PREFIX + "SLACK_" + k: v for k, v in dict(BOT_TOKEN="xoxb-service", CHANNEL_ID="C12345678",
        ALLOWED_USER_IDS="U12345678", REPLY_MODE="poll").items()}
    assert load_saved(env, tmp_path, store) == env
    assert detect(env, tmp_path, store).state == State.CONFIGURED
    assert snapshot(tmp_path) == before


def test_cli_setup_check_never_reads_input(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith((PREFIX, "LARK_", "SLACK_"))}
    env.update(LOCALAPPDATA=str(tmp_path), HOME=str(tmp_path), CODEX_WATCHDOG_LINUX_CONFIG_DIR=str(tmp_path / "config"),
               CODEX_WATCHDOG_MACOS_CONFIG_DIR=str(tmp_path / "config"), PYTHONPATH=str(Path(__file__).parents[1] / "src"))
    result = subprocess.run([sys.executable, "-m", "codex_watchdog", "--runtime", str(tmp_path / "runtime"),
        "setup-messaging", "--check"], env=env, input="", capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] in (State.PRISTINE_UNCONFIGURED, State.EXISTING_OR_PARTIAL)
    assert not (tmp_path / "runtime").exists()
