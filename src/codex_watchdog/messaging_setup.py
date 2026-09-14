"""One conservative setup command shared by foreground platform launchers."""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

from .lark_transport import valid_id
from .messaging_pairing import pair_provider
from .messaging_profile import (
    ConfigurationState as State, MARKER, PREFIX, SELECTOR, SLACK_SERVICE, LARK_SERVICE,
    MessagingError, SecretStore, config_directory, detect, json_bytes, load_saved,
    private_directory, provider_keys, read_object, route_environment, write_new,
)
from .storage import FileLock, StoreBusyError

MANUAL = "Run codex-watchdog setup-messaging --check to inspect setup state; run codex-watchdog setup-messaging in an interactive terminal to configure messaging."


def interactive():
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return False
        return os.name == "nt" or os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except (OSError, ValueError, AttributeError):
        return False


def _marker(root, status, **extra):
    path = root / MARKER
    previous = path.read_bytes() if path.exists() else None
    value = dict(schema_version=1, status=status, **extra)
    if previous is None:
        write_new(path, json_bytes(value))
        return
    # Only our valid nonsecret state marker is changed, under the setup lock.
    old = read_object(path)
    if old.get("schema_version") != 1:
        raise MessagingError("messaging_setup_marker_requires_review")
    old.update(value)
    fd, filename = tempfile.mkstemp(prefix=".messaging-marker-", dir=root)
    temp = Path(filename)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json_bytes(old))
            f.flush()
            os.fsync(f.fileno())
        if path.read_bytes() != previous:
            raise MessagingError("messaging_setup_changed_concurrently")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _retryable(root, detection, environment):
    if provider_keys(environment, "slack") or provider_keys(environment, "lark") or SELECTOR in environment:
        return False
    if set(detection.evidence) != {"saved:" + MARKER}:
        return False
    try:
        value = read_object(root / MARKER)
        return value.get("schema_version") == 1 and value.get("status") in ("started", "cancelled", "failed", "skipped")
    except MessagingError:
        return False


def _save(root, store, selection, pairings, credentials):
    # Pair ALL requested providers before writing ANY credentials. Pairing
    # failure/cancellation leaves only the explicit nonsecret setup marker.
    if store.platform.startswith("linux"):
        values = {SELECTOR: selection, PREFIX + "SLACK_REPLY_MODE": "poll"} if "slack" in pairings else {SELECTOR: selection}
        for provider, route in pairings.items():
            values.update(credentials[provider])
            values.update(route_environment(provider, route))
        # Existing Linux node setup already accepts a current-user mode-600
        # EnvironmentFile. This is that boundary, not an encrypted store.
        lines = ["# Codex WatchDog messaging environment; schema_version=1"]
        for key, value in sorted(values.items()):
            if any(ord(c) < 32 for c in value):
                raise MessagingError("messaging_credential_invalid")
            lines.append(key + "=" + json.dumps(value, ensure_ascii=False))
        write_new(root / "linux-notifications.env", ("\n".join(lines) + "\n").encode())
    else:
        for provider, route in pairings.items():
            if provider == "slack":
                for account, key in (("slack-bot-token", "BOT_TOKEN"), ("slack-app-token", "APP_TOKEN")):
                    store.put(SLACK_SERVICE, account, credentials[provider][PREFIX + "SLACK_" + key])
            else:
                store.put(LARK_SERVICE, "lark-app-secret", credentials[provider][PREFIX + "LARK_APP_SECRET"],
                          username=route["app_id"])
                route = dict(route, credential_path="lark-app-secret.clixml", interactive_transport=selection)
            write_new(root / (provider + "-relay.json"), json_bytes(route))
    _marker(root, "configured", transport=selection)


def setup(*, environment=None, root=None, runtime=None, store=None, auto=False, check=False,
          is_interactive=None, read=None, secret=None, output=print, pair=pair_provider):
    env = dict(os.environ if environment is None else environment)
    root = config_directory(env) if root is None else Path(root)
    runtime = Path(runtime or ".codex-watchdog")
    store = store or SecretStore(root, environment=env)
    found = detect(env, root, store)
    if check:
        output(json.dumps(dict(schema_version=1, state=found.state.value, evidence=list(found.evidence)), sort_keys=True))
        return 0
    if found.state == State.CONFIGURED:
        output("Messaging is configured; existing settings and credentials are unchanged.")
        return 0
    if found.state == State.EXISTING_OR_PARTIAL and (auto or not _retryable(root, found, env)):
        output("Existing or partial messaging settings were preserved. Review the saved provider profile/environment before manual setup. " + MANUAL)
        return 0 if auto else 1
    if not (interactive() if is_interactive is None else is_interactive):
        output("Messaging setup needs an interactive foreground terminal. " + MANUAL)
        return 0 if auto else 1
    read = read or input
    secret = secret or getpass.getpass
    try:
        private_directory(root)
        with FileLock(root / "setup.lock"):
            again = detect(env, root, store)
            if again != found:
                raise MessagingError("messaging_setup_changed_concurrently")
            _marker(root, "started")
            try:
                output("Set up WatchDog messaging: 1 Slack / 2 Feishu or Lark / 3 Both / 4 Skip (do not ask again)")
                choice = read("Choose 1-4: ").strip().lower()
                selection = {"1": "slack", "slack": "slack", "2": "lark", "lark": "lark", "feishu": "lark", "3": "both", "both": "both", "4": "skip", "skip": "skip"}.get(choice)
                if selection is None:
                    raise MessagingError("messaging_setup_choice_invalid")
                if selection == "skip":
                    _marker(root, "skipped")
                    output("Messaging setup skipped. " + MANUAL)
                    return 0
                output("Use an app whose WatchDog listeners are stopped on other machines during this short pairing step.")
                if read("No other listener is using the selected app(s). Type yes to continue: ").strip().lower() != "yes":
                    raise MessagingError("messaging_pairing_cancelled")
                credentials, pairings = {}, {}
                for provider in (("slack", "lark") if selection == "both" else (selection,)):
                    if provider == "slack":
                        bot = secret("Slack bot token (xoxb-, hidden): ").strip()
                        app = secret("Slack app token (xapp-, hidden): ").strip()
                        if not bot.startswith("xoxb-") or not app.startswith("xapp-"):
                            raise MessagingError("messaging_slack_tokens_invalid")
                        values = {PREFIX + "SLACK_BOT_TOKEN": bot, PREFIX + "SLACK_APP_TOKEN": app}
                    else:
                        domain = read("Domain (feishu or lark) [feishu]: ").strip().lower() or "feishu"
                        app_id = read("App ID (cli_...): ").strip()
                        app_secret = secret("App secret (hidden): ").strip()
                        if domain not in ("feishu", "lark") or not valid_id(app_id, "cli") or not app_secret:
                            raise MessagingError("messaging_lark_app_invalid")
                        values = {PREFIX + "LARK_DOMAIN": domain, PREFIX + "LARK_APP_ID": app_id,
                                  PREFIX + "LARK_APP_SECRET": app_secret}
                    credentials[provider] = values
                    pairings[provider] = pair(provider, values, runtime, read=read, output=output)
                # Recheck all external provider traces before publishing.
                current = detect(env, root, store)
                if set(current.evidence) != {"saved:" + MARKER}:
                    raise MessagingError("messaging_setup_changed_concurrently")
                _save(root, store, selection, pairings, credentials)
                output("Messaging pairing saved. Ordinary replies still require Reply on a WatchDog notification for the exact Codex thread.")
                return 0
            except (KeyboardInterrupt, EOFError):
                _marker(root, "cancelled")
                output("Messaging setup cancelled; no automatic retry. " + MANUAL)
                return 130
            except Exception:
                _marker(root, "failed")
                raise
    except (Exception, KeyboardInterrupt) as exc:
        reason = str(exc) if isinstance(exc, MessagingError) else "messaging_setup_failed_or_busy"
        output(reason + ". Existing settings were preserved. " + MANUAL)
        return 1


def prepare_launch(runtime, *, environment=None, root=None, store=None, allow_auto=True, output=print):
    env = dict(os.environ if environment is None else environment)
    root = config_directory(env) if root is None else Path(root)
    store = store or SecretStore(root, environment=env)
    found = detect(env, root, store)
    if allow_auto and found.state != State.CONFIGURED:
        code = setup(environment=env, root=root, runtime=runtime, store=store, auto=True, output=output)
        if code:
            raise MessagingError("messaging_setup_did_not_complete")
    return load_saved(env, root, store)
