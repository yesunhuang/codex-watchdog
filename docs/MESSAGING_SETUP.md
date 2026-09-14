# First-use messaging setup

On a pristine interactive foreground launch, WatchDog offers **Slack**, **Feishu /
Lark**, **Both**, or **Skip**. You can also start the same flow yourself:

```text
codex-watchdog setup-messaging
```

In PowerShell, use `./codex-watchdog.exe setup-messaging` from the extracted
package. On macOS/Linux, use the installed executable's full path if it is not
on `PATH`. Run setup on the machine that will send notifications.

## Prepare the app and pair

Create/install your bot first using the [Slack prerequisites](SETUP.md#slack-quick-reply-relay)
or [Feishu/Lark prerequisites](FEISHU_LARK.md#prepare-the-application). Setup does
not change app permissions. Stop that app's other listeners during pairing;
the same runtime's active listener locks also prevent competing setup.

1. Choose the provider(s). Enter Slack's bot and app tokens, or Feishu/Lark's
   app ID, secret, and region. Secret prompts are hidden.
2. Send the displayed `WATCHDOG-PAIR-...` phrase as a new plain-text message in
   the intended bot conversation within three minutes. For Slack, use a public
   or private channel containing the bot; the existing relay does not support
   direct-message channel IDs.
3. Check the detected conversation and account in the terminal, then confirm.
   No channel ID or user/Open ID lookup is needed. Both-provider setup pairs
   both apps before saving credentials.
4. Start the foreground monitor or restart your existing service when its work
   permits. Reply using the chat app's **Reply** action on a new WatchDog
   notification. Pairing alone does not route arbitrary top-level messages to
   Codex; existing notification-to-thread mappings are still required.

The bounded listeners use the providers' authenticated connections and exact
message context. Slack's authenticated `hello` supplies the app identity and
connection count, and message authorizations must match the bot's `auth.test`
identity. Feishu/Lark events must match the selected app and region. Expired,
edited, bot, unrelated, or ambiguous confirmations are rejected. See the official
[Slack Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/),
[Slack event authorization](https://docs.slack.dev/apis/events-api/), and
[Feishu receive-message event](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
references. No public callback server or additional provider scope is added.

## Existing settings and upgrades

Only **PRISTINE_UNCONFIGURED** opens setup automatically. Any provider environment
entry (even empty), relay/profile file, protected credential, legacy pairing,
transport selection, or setup marker suppresses it. **CONFIGURED** reuses the
current settings; **EXISTING_OR_PARTIAL** preserves them and reports a manual
diagnostic. Partial explicit provider variables are never combined with a saved
app's credentials. Unknown configuration fields are retained.

```text
codex-watchdog setup-messaging --check
```

This read-only command reports evidence names and state, never credential values.
Headless/service startup never asks questions. Choosing **Skip** saves a nonsecret
schema-1 marker, so the next launch does not ask again. Cancelled/expired pairing
saves no provider secrets. A failed storage operation records an incomplete
setup marker; setup never calls that configuration complete or overwrites it.

Manual setup can retry a marker-only skipped/cancelled setup. It deliberately
does not replace partial or existing provider settings. Review those using the
advanced provider instructions below; do not erase working state to make an
upgrade look pristine.

## Credential locations and service startup

| Platform | Existing current-user boundary |
| --- | --- |
| Windows | DPAPI `PSCredential` files and nonsecret routing under `%LOCALAPPDATA%\CodexWatchdog` |
| macOS | Current-user Keychain; nonsecret routing under `~/Library/Application Support/CodexWatchdog` |
| Linux | Private, owner-only mode-600 `linux-notifications.env` under the existing Linux configuration directory; this file is permission-protected, not encrypted |

Linux uses `CODEX_WATCHDOG_LINUX_CONFIG_DIR`, or
`${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog`. The shared CLI loads the
saved file for normal starts. For a systemd node, pass that same file to
[`linux-node-install --environment-file`](LINUX_NODE_SETUP.md); it is referenced
in place, never copied into the unit. The existing daemon must be restarted to
reload configuration. Setup does not start a daemon or change hooks/workspaces.

Credentials never enter command-line arguments, plaintext setup markers, logs,
packages, or source. Keychain/DPAPI access failure requires fixing that host's
existing secure-store access; there is no plaintext fallback. Existing Mac
Desktop helpers and previously paired Windows profiles remain usable unchanged.

## Advanced / recovery

Manual channel/user ID configuration remains available for deliberate managed
environments: [Slack](SETUP.md#advanced-manual-slack-configuration),
[Feishu/Lark](FEISHU_LARK.md#advanced-process-configuration).
Use these when app access cannot deliver the confirmation event or when
preserving an intentionally notification-only setup. Do not broaden permissions
or use an empty allowlist to authorize every sender.
