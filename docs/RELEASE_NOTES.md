# Codex WatchDog 2.2.2

Fix reconciliation of a saved Linux continuation receipt after an idle owner
parks and the monitor restarts. Parked monitoring now checks the exact existing
receipt under the current ownership fence and bounded check interval. It can
recognize the saved started turn without reopening the native conversation,
creating a continuation, queuing another instruction or sending a notification.
Parking and the armed binding remain intact. Notifications still require genuine
native writer ownership through the existing guarded path.

The exact queued-wake matcher introduced in the unpublished 2.2.1 candidate is
included. It accepts one terminal newline added by Codex and the recorded end of
the declared interrupted turn when its start predates the saved receipt baseline.
Exact conversation, marker, prompt hash and native turn evidence remain required.
Ambiguous, malformed, truncated or replaced evidence remains unresolved;
uncertain instructions are never replayed merely because a queue is empty.

Existing runtimes, credentials, pairings, notification destinations, grants and
reply-ticket journals are preserved. Windows x64, Linux x64, Linux ARM64 and
macOS ARM64 preview are rebuilt from one canonical public revision. Gates include
native package checks, privacy, complete hashes, the Windows embedded icon and
upgrade from the immediately previous public release, 2.2.0.

Session sharing and Slack bot controls remain available; see the shipped
session-access and destination-binding guides for syntax and provider permissions.
Upgrading grants no additional access. Outbound notification success alone does
not establish incoming reply acceptance. This patch adds no human delegation or
QQ/OneBot live acceptance.

Known limits remain: macOS is an ARM64 preview; unmapped-window and other-window
ownership evidence can limit automatic discovery. A stale previous-boot writer
flag on shared-home Linux can still require evidence-based operator recovery.
The separate Windows follower-view recovery-notification limitation is unchanged.
Native writer parking does not itself establish editor GUI recovery.
