# Session destination affinity

Each Codex session uses the configured provider inbox until an authorized user
binds that exact session to another destination. Other conversations in the same
repository, including newly created ones, keep their existing destination.

For the Slack MVP, reply to an active WatchDog notification:

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
repost the original completion. Closed notifications cannot bind, unbind or wake
Codex; they remain only for audit and deduplication.

Each provider gives each exact Codex session up to four recent active reply
tickets. A fifth notification retires only that session's oldest active ticket.
Other sessions keep their tickets, including other conversations in the same
repository. Slack, Feishu/Lark and OneBot have independent budgets.

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

## Active notification polling

The integration keeps the existing polling transport and one reply page per
tick. Every tick serves the active ticket set; closed history is never polled.
The SQLite active index separates polling from audit history. The next page
rotates across active notifications belonging to the monitored sessions.

At the normal ten-second cadence, three sessions with four active tickets each
take about two minutes for a full rotation before pagination, API time or backoff.
Closed history adds no polling delay. Successful confirmation and destination
hello are each attempted at most once; a timeout or crash can lose a confirmation
without undoing the saved binding. Repeating the same provider event neither
changes the route nor sends another confirmation. An uncertain hello is not
automatically resent; the saved binding still applies to later notifications.

Upgrade preserves existing active/closed state and saved routes. The previous
database and historical cursor file are backed up before obsolete control cursor
state is removed. Closed tickets are never reopened during migration; use a new
notification if an older version already retired a useful reply surface.

Slack lookup/access checks use
[conversations.list](https://docs.slack.dev/reference/methods/conversations.list/)
and [conversations.info](https://docs.slack.dev/reference/methods/conversations.info/).
Those methods require appropriate conversation-read permissions. Ordinary
unbound notification delivery must not require new lookup calls or re-pairing.
Feishu/Lark and OneBot route commands are outside this first implementation.
