# Session destination affinity

Each Codex session uses the configured provider inbox until an authorized user
binds that exact session to another destination. Other conversations in the same
repository, including newly created ones, keep their existing destination.

For the Slack MVP, reply to a known WatchDog notification:

```text
bind #channel
```

Select Slack's channel mention where possible. WatchDog resolves and saves the
immutable channel ID, so a channel rename does not change routing. The bot must
already be a member of an accessible, writable destination. Name-only lookup is
bounded and runs only for a bind command. No automatic channel join is performed.
Missing access or ambiguous identity must leave the current route unchanged.
Reply `unbind` to return just that session to the configured inbox.

Channel lookup requires the Slack app's Bot Token Scopes `channels:read` for
public channels and `groups:read` for private channels. Existing posting/history
permissions do not grant this metadata access. If these scopes are newly added,
reinstall/reauthorize the existing Slack app in its workspace to grant them before
retrying a bind. WatchDog pairing and unrelated settings remain unchanged.

These are WatchDog control commands. They never become Codex prompts. A successful
command closes its source notification's wake ticket and sends a short confirmation
in that Slack thread. A successful bind also posts a new, reply-enabled hello in
the destination channel for that exact session, without waking Codex. It does not
repost the original completion. A known closed
notification may establish a binding without reopening its wake ticket.

## State and safety

Bindings and control-command receipts use additive, versioned records in the
existing provider reply-ticket SQLite journal. Scope includes the provider,
configured default conversation and exact Codex session ID. Applying a command
and closing its source ticket must occur in one transaction. An acknowledged or
uncertain command must not be replayed over a newer route.
The latest command timestamp remains stored after `unbind`, so an older reply
discovered later cannot restore a superseded destination.

The route persists through normal restart with the same runtime and transport.
It is not a project setting and is not synchronized across machines. Rebinding
after changing runtime or transport is acceptable in this MVP. Credentials,
pairing and allowlists are reused; they are not copied into route records.

Future bound notifications receive normal one-shot tickets in the chosen channel.
A known exact channel-and-parent mapping is required for replies there. A channel
name or repository name in arbitrary text grants no routing authority. Failed
bound Slack delivery must not use the default-inbox webhook as a fallback.

## Historical command polling

The integration keeps the existing polling transport and one reply page
per tick. Four ticks serve the active ticket set; every fifth tick can inspect
one closed parent for control commands only. Selection and historical cursors
are indexed in SQLite rather than loading lifetime history into memory. Closed
parents never regain plain-text wake authority. This avoids a competing socket
listener and does not increase the normal reply-page request rate.
When no closed parent exists, that tick continues active polling. Successful
confirmation and destination hello are each attempted at most once; a timeout or
crash can lose a confirmation
without undoing the saved binding. Repeating the same provider event neither
changes the route nor sends another confirmation.

There is a latency tradeoff: with closed history present, a full rotation of four
active tickets can take five ticks. A historical parent's command may wait for
the closed-parent rotation and pagination; that delay grows with stored history.
At the normal ten-second cadence, 100 closed parents can take about 83 minutes
for one rotation, before pagination or backoff. Prefer a recent active
notification when prompt configuration feedback matters.
Provider rate limiting and backoff can add delay. This is not immediate command
delivery. An uncertain hello is not automatically resent; the saved binding still
applies to later notifications.

Slack lookup/access checks use
[conversations.list](https://docs.slack.dev/reference/methods/conversations.list/)
and [conversations.info](https://docs.slack.dev/reference/methods/conversations.info/).
Those methods require appropriate conversation-read permissions. Ordinary
unbound notification delivery must not require new lookup calls or re-pairing.
Feishu/Lark and OneBot route commands are outside this first implementation.
