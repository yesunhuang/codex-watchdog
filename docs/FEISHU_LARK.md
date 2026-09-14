# Feishu / Lark notifications and replies

Windows, Linux and macOS packages include the Feishu/Lark transport. It sends
notifications and relays an allowed user's text to the exact existing Codex
conversation. Mainland Feishu and international Lark use different API domains;
choose the domain that owns your application.

## Prepare the application

1. Create a tenant-owned application in the [Feishu developer console](https://open.feishu.cn/)
   or [Lark developer console](https://open.larksuite.com/), and enable its bot.
2. Enable bot message sending (`im:message:send_as_bot`). For a private chat,
   enable receiving private messages (`im:message.p2p_msg:readonly`). For group
   chats, grant the receive permissions required for the messages you intend to
   relay; mention-only permissions can omit ordinary replies.
3. Configure long-connection event delivery and subscribe to
   `im.message.receive_v1`. Publish the application and permission changes to
   your tenant, and open the bot conversation or add it to the chosen group.
4. Obtain the app's `cli_...` ID, app secret, target conversation's `oc_...` chat
   ID and permitted people's app-scoped `ou_...` open IDs from the developer
   console/API explorer. All identifiers must belong to the same app and tenant.

WatchDog uses an authenticated outbound WebSocket. It does not require a public
callback server. An incoming-webhook bot alone cannot receive these replies.

## Configure the process that runs WatchDog

| Variable | Value |
| --- | --- |
| `CODEX_WATCHDOG_INTERACTIVE_TRANSPORT` | `lark` |
| `CODEX_WATCHDOG_LARK_DOMAIN` | `feishu` for mainland Feishu; `lark` for international Lark |
| `CODEX_WATCHDOG_LARK_APP_ID` | The application's `cli_...` ID |
| `CODEX_WATCHDOG_LARK_APP_SECRET` | App secret supplied from the host's protected configuration |
| `CODEX_WATCHDOG_LARK_CHAT_ID` | The permitted `oc_...` conversation |
| `CODEX_WATCHDOG_LARK_ALLOWED_USER_IDS` | Comma-separated permitted `ou_...` open IDs |

Omitting the allowed-user list enables notifications only. An invalid list does
not enable replies. When Slack is already configured, explicitly select `lark`;
otherwise Slack remains the default. Selecting `slack` again reuses its saved
settings. SMTP and desktop fallback remain available.

The nonsecret selector and domain can be set in PowerShell:

```powershell
$env:CODEX_WATCHDOG_INTERACTIVE_TRANSPORT = "lark"
$env:CODEX_WATCHDOG_LARK_DOMAIN = "feishu"
```

Or in a Linux/macOS shell:

```sh
export CODEX_WATCHDOG_INTERACTIVE_TRANSPORT=lark
export CODEX_WATCHDOG_LARK_DOMAIN=feishu
```

Provide the remaining variables through the host's existing protected launch
configuration. This transport does not add a graphical credential setup screen
or migrate credentials between hosts. Terminal settings do not automatically
reach an already-running service: configure its launch environment, then restart
that WatchDog service when its work permits. Keep secrets out of command-line
arguments, shell history, source files, release bundles and chat messages.

Upgrades reuse compatible runtimes and retain existing Slack, Outlook, workspace
and provider choices. Detached Linux owners need configuration on the Linux
host; handoff does not copy credentials from the desktop.

## Check configuration and test a reply

Run the installed executable with its existing runtime:

```text
codex-watchdog --runtime <existing-runtime> lark-check
```

The check reports configuration booleans, domain and SDK availability. It does
not contact Feishu/Lark or validate tenant permissions. Exit code 1 means the
configuration is incomplete or the SDK is unavailable.

Start the normal WatchDog process with that configuration and an enrolled
workspace. To send one mapped test notification, use a unique test ID:

```text
codex-watchdog --runtime <existing-runtime> lark-relay-test --workspace <exact-workspace-id> --id <unique-test-id>
```

The normal running WatchDog owns the reply listener. Use the chat application's
native **Reply** action on the test notification, then verify the text arrives
in the existing Codex conversation. A separate top-level message has no recorded
target and is ignored. Windows users can substitute `./codex-watchdog.exe` for
the executable name; Unix users can use its full installed path.

## Limits and recovery

Replies must be plain text from an allowed human, directed to a recorded
notification in the configured conversation. Whitespace and Unicode are
preserved, with an 8,000-character limit. Media, card actions and edited messages
are outside this reply path.

Mapping and deduplication records use schema version 1 and are scoped to the app
and domain. Duplicate or conflicting events do not cause a second admission.
The normal Codex instruction queue retains its existing ownership and
authorization checks. WatchDog does not interpret replies as project policy.

After an uncertain send or admission, inspect the chat and Codex conversation
before sending a new explicit reply. Keep existing receipts; deleting them to
force retries can cause duplicates. A provider acknowledgement and a recorded
admission cannot guarantee delivery across every possible process crash.
