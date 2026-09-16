# OneBot 11 / QQ candidate

This feature is under development for 1.1.0. Synthetic tests do not establish
real QQ support. Release acceptance requires a real NapCat/QQ human round trip
and native package/upgrade checks on all supported platforms.

WatchDog connects to an external OneBot 11 forward WebSocket server. NapCat owns
QQ login and protocol maintenance; it is installed and operated separately under
its own license. WatchDog does not bundle it or add a QQ runtime.

## Pair this machine

1. Prepare the external backend using [NapCat's documentation](https://napneko.github.io/).
   Enable a forward WebSocket server, configure an access token and use array-form
   messages. The endpoint must carry both events and actions. Use a private
   connection or `wss://` when traffic crosses an untrusted network.
2. In a foreground terminal on the WatchDog machine, run:

   ```text
   codex-watchdog setup-messaging --onebot
   ```

   On Windows use `./codex-watchdog.exe`; on macOS/Linux use the executable's
   installed path. Fresh setup also offers option **5 OneBot (QQ)**.
3. Enter the WebSocket address and the access token at the hidden prompt. Do not
   put the token in the URL. After authenticating the backend, setup displays a
   short-lived `PAIR_CODEX_ONEBOT_...` code. Send that exact code as a new text
   message to the bot in the intended direct conversation or group within ten
   minutes. Setup learns the bot, conversation and authorized human automatically;
   no user-ID lookup is needed. A quotation/reply is not a pairing confirmation.
4. Run `codex-watchdog onebot-check --connect`. This authenticates the backend and
   checks its bot identity without posting. It does not prove reply delivery.
5. Restart the monitor when its active work permits. Quote/reply to a WatchDog
   notification to send text to the exact existing Codex thread it names.

Only plain text, one quoted message and an optional mention of the bot are
accepted for control. Top-level messages, other users, anonymous senders, other
chats, rich media, unknown quotations and ambiguous mappings do not wake Codex.
Notifications include the machine name. One bot can serve several machines:
each listener only admits replies to its own known notification mappings, and
existing exact-thread ownership fences remain in force.

## Saved settings and provider selection

The new schema-1 `onebot-relay.json` profile uses the existing current-user
configuration directory. The token uses macOS Keychain, current-user Windows
DPAPI, or a separate mode-0600 `onebot-notifications.env` on Linux. Existing
Slack/Feishu profiles, credentials and delivery journals are retained. Repeated
setup reuses a complete pairing. Partial state is preserved for manual review;
setup never overwrites an orphaned token or silently chooses another backend.

Adding OneBot saves a selection including the existing providers. A deliberate
`CODEX_WATCHDOG_INTERACTIVE_TRANSPORT` environment value still takes precedence:
`slack`, `lark`, `both` (Slack + Feishu), `onebot`, `slack+onebot`, `lark+onebot`, or
`all`. An existing Linux service/environment file that explicitly selects `both`
must select `all` for this candidate to send to all three. Preserve its previous
value for rollback; 1.0.x does not recognize the new selections. Do not copy
secrets between machines to perform an upgrade.

Advanced explicit configuration uses `CODEX_WATCHDOG_ONEBOT_WS_URL`,
`ACCESS_TOKEN`, `SELF_ID`, `CHAT_TYPE` (`private` or `group`), `CHAT_ID` and
`ALLOWED_USER_IDS`, each with the same `CODEX_WATCHDOG_ONEBOT_` prefix. An empty
allowlist is notification-only. Partial explicit settings are never mixed with
another saved pairing.

## Acceptance and failure behavior

Use `codex-watchdog --runtime <existing-runtime> onebot-relay-test --id <unique-id>
--workspace <exact-workspace>` to send one mapped test to OneBot only. Keep the
monitor serving that same runtime. Have the authorized human quote it, verify the
same existing Codex conversation wakes, and verify its final reply returns to the
correct QQ chat. Repeat with a second machine sharing the bot and with a backend
disconnect/reconnect before marking QQ accepted.

Observation reconnects automatically and rechecks the bot identity. A hash-only
health record is stored under `onebot/<bot-scope>/health.json` in the runtime;
it reports transport status, not proof that Codex has observed a thread. There
is no history backfill: messages sent while disconnected or dropped from the
bounded event queue may need a fresh human reply. A send or wake whose result is
uncertain is not automatically replayed. Resolve uncertainty before deliberately
sending a new instruction.

The small MIT-licensed upstream connection component supplies action/echo
correlation, receive dispatch and cleanup. `websockets` 15.0.1 supplies framing,
authentication headers and connection backoff. See [the reuse audit](ONEBOT_REUSE.md)
and the packaged third-party inventory for exact revisions, licenses and changes.
