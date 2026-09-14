"""Current-user messaging evidence and existing platform credential boundaries.

Detection is read-only, including malformed/legacy files. Loading never fills a
partial explicit provider from saved credentials belonging to another app.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

from .lark_transport import LarkConfig, valid_id

PREFIX = "CODEX_WATCHDOG_"
SELECTOR = PREFIX + "INTERACTIVE_TRANSPORT"
MARKER = "messaging-setup.json"
SLACK_SERVICE = "org.localcodexwatchdog.slack"
LARK_SERVICE = "org.localcodexwatchdog.lark"


class MessagingError(RuntimeError):
    """Only fixed, nonsecret diagnostics cross provider/credential boundaries."""


class ConfigurationState(str, Enum):
    PRISTINE_UNCONFIGURED = "PRISTINE_UNCONFIGURED"
    EXISTING_OR_PARTIAL = "EXISTING_OR_PARTIAL"
    CONFIGURED = "CONFIGURED"


@dataclass(frozen=True)
class Detection:
    state: ConfigurationState
    evidence: tuple[str, ...]


def config_directory(environment=None, platform=None):
    env = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        base = env.get("LOCALAPPDATA")
        if not base:
            raise MessagingError("messaging_current_user_directory_unavailable")
        return Path(base) / "CodexWatchdog"
    home = Path(env.get("HOME") or Path.home())
    if platform == "darwin":
        return Path(env.get("CODEX_WATCHDOG_MACOS_CONFIG_DIR") or home / "Library/Application Support/CodexWatchdog")
    return Path(env.get("CODEX_WATCHDOG_LINUX_CONFIG_DIR") or
                Path(env.get("XDG_DATA_HOME") or home / ".local/share") / "codex-watchdog")


def read_object(path):
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise MessagingError("messaging_saved_profile_invalid") from None


def provider_keys(env, provider):
    return tuple(k for k in env if k.startswith(PREFIX + provider.upper() + "_"))


def identity_keys(env, provider):
    suffixes = ("TOKEN", "URL", "APP_ID", "APP_SECRET", "CHAT_ID", "CHANNEL_ID", "ALLOWED_USER_IDS")
    if provider == "lark":
        suffixes += ("DOMAIN",)
    return tuple(k for k in provider_keys(env, provider) if k.endswith(suffixes))


def slack_complete(env):
    get = lambda k: env.get(PREFIX + "SLACK_" + k, "")
    users = re.split(r"[,;\s]+", get("ALLOWED_USER_IDS").strip())
    return bool(get("BOT_TOKEN").startswith("xoxb-") and
                (get("APP_TOKEN").startswith("xapp-") or get("REPLY_MODE") == "poll") and
                re.fullmatch(r"[CG][A-Z0-9]{8,}", get("CHANNEL_ID")) and users and
                all(re.fullmatch(r"[UW][A-Z0-9]{8,}", u) for u in users))


def lark_complete(env):
    try:
        return bool(LarkConfig.from_environment(env).relay_configured)
    except ValueError:
        return False


def route_environment(provider, route):
    if route.get("schema_version") != 1:
        raise MessagingError("messaging_saved_profile_invalid")
    users = route.get("allowed_user_ids")
    if not isinstance(users, list) or not users or not all(isinstance(u, str) for u in users):
        raise MessagingError("messaging_saved_profile_incomplete")
    if provider == "slack":
        values = dict(CHANNEL_ID=route.get("channel_id"), ALLOWED_USER_IDS=",".join(users))
        probe = dict(BOT_TOKEN="xoxb-probe", APP_TOKEN="xapp-probe", **values)
        if not slack_complete({PREFIX + "SLACK_" + k: v for k, v in probe.items()}):
            raise MessagingError("messaging_saved_profile_invalid")
    else:
        values = dict(DOMAIN=route.get("domain"), APP_ID=route.get("app_id"),
                      CHAT_ID=route.get("chat_id"), ALLOWED_USER_IDS=",".join(users))
        probe = {PREFIX + "LARK_" + k: v for k, v in dict(APP_SECRET="probe", **values).items()}
        if not lark_complete(probe):
            raise MessagingError("messaging_saved_profile_invalid")
    return {PREFIX + provider.upper() + "_" + k: v for k, v in values.items()}


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise MessagingError("messaging_directory_not_private")
    if os.name != "nt":
        info = path.stat()
        # Existing package metadata directories can be 0755. Secrets are 0600
        # (or in Keychain); never chmod an existing directory on upgrade.
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise MessagingError("messaging_directory_not_private")


def write_new(path, contents):
    """Publish new bytes atomically without replacing any concurrent file."""
    fd, name = tempfile.mkstemp(prefix=".messaging-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic no-clobber on supported package filesystems.
    finally:
        temporary.unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")


class SecretStore:
    def __init__(self, root, platform=None, environment=None):
        self.root = Path(root)
        self.platform = sys.platform if platform is None else platform
        self.environment = dict(os.environ if environment is None else environment)

    def _security(self, *args, input=None):
        return subprocess.run([self.environment.get("CODEX_WATCHDOG_MACOS_SECURITY_BIN", "/usr/bin/security"), *args],
                              input=input, capture_output=True, timeout=15, env=self.environment)

    def has(self, service, account):
        if self.platform == "win32":
            return os.path.lexists(self.root / (account + ".clixml"))
        if self.platform == "darwin":
            result = self._security("find-generic-password", "-s", service, "-a", account)
            if result.returncode == 44:  # errSecItemNotFound, not denial/locked keychain.
                return False
            if result.returncode != 0:
                raise MessagingError("messaging_keychain_unavailable")
            return True
        return False

    def get(self, service, account, path=None, username=None):
        if self.platform == "win32":
            target = self.root / (path or account + ".clixml")
            if not target.resolve().is_relative_to(self.root.resolve()):
                raise MessagingError("messaging_credential_path_invalid")
            script = "$ErrorActionPreference='Stop'; $v=[Console]::In.ReadToEnd()|ConvertFrom-Json; $c=Import-Clixml -LiteralPath $v.path; if($c.UserName -cne $v.username){exit 2}; [Console]::Out.Write($c.GetNetworkCredential().Password)"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    input=json.dumps(dict(path=str(target), username=username or account)),
                                    text=True, capture_output=True, timeout=20, env=self.environment)
            if result.returncode:
                raise MessagingError("messaging_dpapi_credential_unavailable")
            return result.stdout
        if self.platform == "darwin":
            result = self._security("find-generic-password", "-s", service, "-a", account, "-w")
            if result.returncode:
                raise MessagingError("messaging_keychain_credential_unavailable")
            return result.stdout.decode("utf-8").rstrip("\r\n")
        raise MessagingError("messaging_secret_backend_unavailable")

    def put(self, service, account, value, username=None):
        if self.has(service, account):
            raise MessagingError("messaging_credential_already_exists")
        if self.platform == "win32":
            # JSON enters only via stdin. PowerShell serializes a current-user
            # PSCredential exactly as the existing launchers expect.
            script = "$ErrorActionPreference='Stop'; $v=[Console]::In.ReadToEnd()|ConvertFrom-Json; $c=[pscredential]::new($v.username,(ConvertTo-SecureString $v.secret -AsPlainText -Force)); [Console]::Out.Write([System.Management.Automation.PSSerializer]::Serialize($c))"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    input=json.dumps(dict(username=username or account, secret=value)),
                                    capture_output=True, text=True, timeout=20, env=self.environment)
            if result.returncode:
                raise MessagingError("messaging_dpapi_store_failed")
            write_new(self.root / (account + ".clixml"), result.stdout.encode("utf-8"))
            return
        if self.platform != "darwin":
            raise MessagingError("messaging_secret_backend_unavailable")
        # Use the same creator/reader identity as existing Mac launchers.
        # A Python-created item has a different default ACL and can require
        # consent when /usr/bin/security reads it later. The tool's bounded
        # stdin mode avoids secrets in argv without granting other apps access.
        # Its parser supports escaped double quotes/backslashes, not shell
        # concatenation. Refuse controls and its 4096-byte line limit; send one
        # command and EOF so a later command cannot mask a storage failure.
        if any(ord(c) < 32 or ord(c) == 127 for text in (service, account, value) for c in text):
            raise MessagingError("messaging_credential_invalid")
        quote = lambda text: json.dumps(text, ensure_ascii=False)
        line = ("add-generic-password -a " + quote(account) + " -s " + quote(service) +
                " -w " + quote(value) + "\n").encode("utf-8")
        if len(line) >= 4096:
            raise MessagingError("messaging_credential_invalid")
        result = self._security("-q", "-i", input=line)
        if result.returncode != 0 or self.get(service, account) != value:
            raise MessagingError("messaging_keychain_store_failed")


def linux_environment(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 65536):
        raise MessagingError("messaging_linux_environment_not_private")
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition("=")
        if not sep or not re.fullmatch(r"CODEX_WATCHDOG_[A-Z0-9_]+", key):
            raise MessagingError("messaging_linux_environment_requires_manual_review")
        values = shlex.split(raw, comments=False, posix=True)
        if len(values) != 1 or key in result:
            raise MessagingError("messaging_linux_environment_requires_manual_review")
        result[key] = values[0]
    return result


def detect(environment=None, root=None, store=None):
    env = dict(os.environ if environment is None else environment)
    root = config_directory(env) if root is None else Path(root)
    store = store or SecretStore(root, environment=env)
    evidence = ["provider_environment"] if provider_keys(env, "slack") or provider_keys(env, "lark") or SELECTOR in env else []
    try:
        if root.exists():
            evidence.extend("saved:" + p.name for p in root.iterdir()
                            if p.name.startswith(("slack", "lark", "feishu", "linux-notifications", "messaging-setup")))
        for service, account in ((SLACK_SERVICE, "slack-bot-token"), (SLACK_SERVICE, "slack-app-token"),
                                 (LARK_SERVICE, "lark-app-secret")):
            if store.has(service, account):
                evidence.append("protected_credential")
        # Existing Mac host helper used its own service. Its presence is enough
        # to suppress onboarding, even with a missing or malformed route.
        if store.platform == "darwin":
            r = store._security("find-generic-password", "-s", "org.localcodexwatchdog.feishu")
            if r.returncode != 44:
                evidence.append("legacy_keychain_or_unavailable")
        if not evidence:
            return Detection(ConfigurationState.PRISTINE_UNCONFIGURED, ())
        loaded = load_saved(env, root, store, secrets=False)
        slack, lark = slack_complete(loaded), lark_complete(loaded)
        selected = loaded.get(SELECTOR) or ("lark" if lark and not slack else "slack")
        good = {"slack": slack, "lark": lark, "both": slack and lark}.get(selected, False)
        marker = root / MARKER
        if marker.exists() and read_object(marker).get("status") not in ("configured", "skipped"):
            good = False
        # An unused partial provider is still partial; no silent repair.
        if identity_keys(env, "slack") and not slack_complete(env):
            good = False
        if identity_keys(env, "lark") and not lark_complete(env):
            good = False
        return Detection(ConfigurationState.CONFIGURED if good else ConfigurationState.EXISTING_OR_PARTIAL,
                         tuple(sorted(set(evidence))))
    except (MessagingError, OSError, ValueError, TypeError, subprocess.SubprocessError):
        return Detection(ConfigurationState.EXISTING_OR_PARTIAL, tuple(sorted(set(evidence + ["unreadable_or_partial_state"]))))


def load_saved(environment, root, store=None, *, secrets=True):
    env = dict(environment)
    store = store or SecretStore(root, environment=env)
    for provider, complete in (("slack", slack_complete), ("lark", lark_complete)):
        # Operational policy knobs alone count as evidence, but never select a
        # saved app. A full explicit notification-only config remains supported.
        identity = identity_keys(env, provider)
        if identity:
            core = [PREFIX + provider.upper() + "_" + k for k in
                    (("BOT_TOKEN", "CHANNEL_ID") if provider == "slack" else ("APP_ID", "APP_SECRET", "CHAT_ID"))]
            webhook_only = provider == "slack" and bool(env.get(PREFIX + "SLACK_WEBHOOK_URL")) and not any(k in env for k in core)
            if not webhook_only and not all(env.get(k) for k in core):
                raise MessagingError("messaging_partial_explicit_" + provider + "_environment")
            continue
        path = root / (provider + "-relay.json")
        if not path.exists():
            continue
        route = read_object(path)
        values = route_environment(provider, route)
        if provider == "slack":
            for account, key, placeholder in (("slack-bot-token", "BOT_TOKEN", "xoxb-probe"),
                                               ("slack-app-token", "APP_TOKEN", "xapp-probe")):
                if not store.has(SLACK_SERVICE, account):
                    raise MessagingError("messaging_saved_profile_incomplete")
                values[PREFIX + "SLACK_" + key] = store.get(SLACK_SERVICE, account) if secrets else placeholder
        else:
            credential = route.get("credential_path", "lark-app-secret.clixml")
            if store.platform == "win32":
                if not isinstance(credential, str) or not (root / credential).resolve().is_relative_to(root.resolve()):
                    raise MessagingError("messaging_credential_path_invalid")
                exists = (root / credential).is_file()
            else:
                exists = store.has(LARK_SERVICE, "lark-app-secret")
            if not exists:
                raise MessagingError("messaging_saved_profile_incomplete")
            values[PREFIX + "LARK_APP_SECRET"] = (store.get(LARK_SERVICE, "lark-app-secret", path=credential,
                                                         username=route.get("app_id")) if secrets else "probe")
            if SELECTOR not in env and route.get("interactive_transport") in ("slack", "lark", "both"):
                env[SELECTOR] = route["interactive_transport"]
        env.update(values)
    marker = root / MARKER
    managed_environment = marker.exists() and read_object(marker).get("status") == "configured"
    explicit_provider = bool(identity_keys(environment, "slack") or identity_keys(environment, "lark"))
    if (store.platform.startswith("linux") and (root / "linux-notifications.env").exists()
            and (managed_environment or not explicit_provider)):
        # Existing systemd/shell launchers already interpreted their own legacy
        # environment file. Do not reparse it with a narrower grammar when a
        # complete explicit provider was supplied by that launcher.
        saved = linux_environment(root / "linux-notifications.env")
        for provider in ("slack", "lark"):
            if not identity_keys(environment, provider):
                env.update({k: v for k, v in saved.items() if k.startswith(PREFIX + provider.upper() + "_")})
        if SELECTOR not in env and SELECTOR in saved:
            env[SELECTOR] = saved[SELECTOR]
    if SELECTOR not in env and marker.exists():
        value = read_object(marker)
        if value.get("schema_version") == 1 and value.get("status") == "configured" and value.get("transport") in ("slack", "lark", "both"):
            env[SELECTOR] = value["transport"]
    return env
