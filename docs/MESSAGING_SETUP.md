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
not change app permissions. Existing monitors on other devices can keep running
throughout pairing. New setups automatically use polling for both providers.

1. Choose the provider(s). Enter Slack's bot token, or Feishu/Lark's
   app ID, secret, and region. Secret prompts are hidden.
2. Select the intended channel/group from the bot's conversation list. Send the
   displayed `WATCHDOG-PAIR-...` phrase there as a new plain-text message within
   three minutes. Send only the code, without code-block formatting, as a new
   conversation message rather than a thread reply. Slack supports public/private
   channels containing the bot. Setup displays the selected conversation ID and
   checks for the message every five seconds.
3. Check the detected conversation and account in the terminal, then confirm.
   Group setup needs no channel ID or user/Open ID lookup. Feishu's group-list
   API cannot discover direct conversations; the advanced direct-chat option
   asks for that chat ID only. Both-provider setup pairs both apps before saving
   credentials.
   If an older app lacks conversation-list permission, setup asks only for the
   channel/chat ID instead; existing permissions can remain unchanged.
4. Start the foreground monitor or restart your existing service when its work
   permits. Reply using the chat app's **Reply** action on a new WatchDog
   notification. Pairing alone does not route arbitrary top-level messages to
   Codex; existing notification-to-thread mappings are still required.

Each runtime creates a persistent random seed and hashes it with the machine
name. The first 12 hexadecimal characters label the device in pairing prompts
and polling health records. No MAC address or raw machine name is stored in this
identity file. The label is for identification; a fresh 128-bit random code
authorizes each pairing attempt. A short label alone never grants access.

Pairing reads only the selected conversation through the authenticated provider
API. Reads do not consume events, so simultaneous devices cannot steal each
other's code. Expired, bot, unrelated, ambiguous, known-edited or changed
confirmations are rejected. The confirmation is re-read before saving. Feishu's
general `updated` flag also changes on new messages; validation uses creation
time, exact text, sender and observed content changes instead of treating that
flag alone as a text edit. No callback server or temporary socket is needed.
See [Slack bot conversations](https://docs.slack.dev/reference/methods/users.conversations/),
[Slack thread history](https://docs.slack.dev/reference/methods/conversations.replies/),
[Feishu group discovery](https://open.feishu.cn/document/server-docs/group/chat/list)
and [Feishu history](https://open.feishu.cn/document/server-docs/im-v1/message/list).

## Existing settings and upgrades

**PRISTINE_UNCONFIGURED** opens setup automatically. Any provider environment
entry (even empty), relay/profile file, protected credential, legacy pairing,
or transport selection suppresses it. A marker left by unfinished setup permits
another attempt on the next interactive foreground launch only when no provider
settings or credentials exist. **CONFIGURED** reuses the
current settings; **EXISTING_OR_PARTIAL** preserves them and reports a manual
diagnostic. Partial explicit provider variables are never combined with a saved
app's credentials. Unknown configuration fields are retained.

Fresh Slack profiles save `reply_mode=poll` on every platform. Existing explicit
transport choices remain intact; legacy Slack profiles without a mode retain
their existing Socket Mode behavior and mappings. Converting those managed
profiles is a separate deliberate change, not an upgrade-time state rewrite.
Feishu/Lark's default reply mode is polling. Provider permissions and rate limits
still apply; setup checks history access before declaring pairing complete.

```text
codex-watchdog setup-messaging --check
```

This read-only command reports evidence names, state and the last recorded setup
error code, never credential values. Older failed markers may lack an error code.
Headless/service startup never asks questions. Choosing **Skip** saves a nonsecret
schema-1 marker, so the next launch does not ask again. Cancelled/expired pairing
saves no provider secrets. A failed storage operation records an incomplete
setup marker; setup never calls that configuration complete or overwrites it.

Double-clicking the Windows EXE again retries a failed or cancelled fresh setup;
no configuration deletion is needed. Choosing Skip still suppresses future
prompts. Manual setup can also retry a marker-only skipped setup. Neither path
replaces partial or existing provider settings. Review those using the
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
