# Codex WatchDog 2.0.0

This release corrects reply-ticket retention: every exact Codex session keeps
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

The release candidate passed user-confirmed production testing across Windows,
Linux and macOS, including reply and binding tests. Final release checks caught
an equal-timestamp edge case: retirement now uses persisted insertion order to
break timestamp ties, preserving the oldest-first rule across restarts and
provider scopes. All four packages are rebuilt from one revision:
Windows x64, Linux x64, Linux ARM64 and macOS ARM64 preview. The macOS package
retains its preview support designation.

Existing users can upgrade in place without re-entering compatible messaging
configuration. Native gates verify fresh startup and the immediately previous
public Windows release upgrade, the embedded Windows icon, provider integrations,
Linux compatibility and package privacy before publication.

Slack polls one active parent per ten-second tick. With three sessions holding
four tickets each, a full rotation takes about two minutes plus request time,
pagination and backoff. Closed audit history no longer adds polling work.
