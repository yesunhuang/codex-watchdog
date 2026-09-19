# Bounded one-shot replies

Starting with v1.1.1, each reply-enabled notification grants one
reply ticket. Slack, Feishu/Lark and QQ each retain at most **four active tickets
per runtime**, across provider scopes/transports. Creating a fifth active ticket
retires the oldest. Different tickets keep their exact Codex destinations.

The first authorized, correlated plain-text reply claims its ticket durably before
local queue admission or a remote ownership RPC. A second reply cannot wake Codex,
even if the first admission is uncertain or the monitor restarts. A notification
copied between runtimes also uses a notification-address receipt at the remote
owner, preventing two distinct replies from bypassing the local ticket claim.

Claimed tickets immediately leave the active set. Store contention before claiming
has no side effect and can be retried by the existing provider history cursor.
Once claimed, a failed/ambiguous remote operation closes the ticket with an
uncertain receipt. That transport cannot prove no remote admission happened, so
it does not retry the wake or accept replacement text. No raw reply text is added
to the ticket journal; existing queue persistence remains unchanged.

Slack polling requests one page for one active ticket per tick and removes closed
parents from its cursor set. The 10-second cadence and HTTP429 `Retry-After`
handling remain. With four active tickets the rotation is at most four ticks,
plus request/dispatch time and any backoff. Provider history visibility and Codex
start time are separate from queue-admission latency. The app's API allowance can
be shared by several machines, so this change does not increase request rates.
See [Slack's method limits](https://docs.slack.dev/reference/methods/conversations.replies/)
and [workspace/app rate limits](https://docs.slack.dev/apis/web-api/rate-limits/).

## Persistence and migration

Each provider uses a standard-library SQLite journal at
`<runtime>/<provider>/reply-tickets.sqlite3` with `user_version=1`, rollback
journaling and full synchronization. A partial index holds only active tickets;
indexed mapping/event/message/notification history preserves collision checks and
send deduplication without scanning lifetime history during polling or admission.
Provider scopes and Slack socket/poll namespaces remain separate routing domains.

On first access, validate the old schema-1 JSON journal, atomically retain its exact
bytes in `relay-state.json.v1-backup` (or the corresponding poll filename), then
import its mappings and receipts in a transaction. **All legacy tickets are
retired.** Old Lark/QQ reply receipts do not identify the parent ticket, and old
Slack controlled deliveries could bypass its local reply ledger; none reliably
proves that a mapping is still unused. Zero retained tickets is the conservative
case of the four-ticket upper bound. The next normal notification opens a ticket.

The JSON path becomes a small schema-2 marker after the transaction commits.
Interrupted marker publication is recoverable without a second import. New
mapping envelopes retain their creation timestamp and ticket schema through the
ownership handoff. Legacy envelopes without that metadata are cached only as
closed evidence. Re-caching a consumed/retired address never reopens it.

Upgrade does not touch provider settings, encrypted credentials, conversation
history or existing queue receipts. Older binaries reject the schema-2 marker.
Do not restore a legacy backup over a journal that has admitted new replies:
that would restore stale tickets and lose deduplication evidence. Keep the journal
and use a compatible monitor; rollback of binaries must not roll back admissions.
