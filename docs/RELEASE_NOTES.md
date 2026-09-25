# Codex WatchDog 2.0.0-rc.1

This prerelease corrects reply-ticket retention: every exact Codex session keeps
up to four active tickets per provider. A busy session's fifth notification retires
only its own oldest ticket. Slack, Feishu/Lark and OneBot budgets remain separate.

Slack bind/unbind now require an active mapped notification. Closed notifications
are retained only for audit and deduplication; they cannot wake or change routing.
The historical control polling lane is removed. Successful binding still posts a
new reply-enabled hello in the destination.

Upgrade reuses saved messaging settings, credentials, routes and ticket states.
The SQLite journal migrates to schema 2 with a backup; obsolete historical cursors
are removed without reopening closed tickets. Older binaries cannot read the new
journal. Do not restore old ticket backups after admitting new replies.

Windows upgrade and live reply checks passed before expanding this candidate to
the remote fleet. Native package gates cover Windows x64, Linux x64/ARM64 and the
macOS ARM64 preview. Fresh human bind acceptance and remote production testing
remain candidate acceptance work; this is not the stable 2.0.0 release.

Slack polls one active parent per ten-second tick. With three sessions holding
four tickets each, a full rotation takes about two minutes plus request time,
pagination and backoff. Closed audit history no longer adds polling work.
