# Codex WatchDog 2.2.4

Slack polling now rotates past an inaccessible active parent before provider,
validation, handler or acknowledgement errors can starve other conversations.
Unread replies remain at their saved positions and provider-wide rate-limit
backoff is preserved.

Feishu/Lark polling reads native threads for the last four still-valid tickets
per provider and session. Use **Reply in thread** on the mapped notification;
ordinary quoted chat replies do not enter this polling path. Per-tick reads are
bounded, failed parents rotate, and observed deferred replies retain exact
message identities. Edited messages and unknown saturated unread windows fail
closed. Closed and evicted tickets stop being tracked and require no later
acknowledgement. Without a compatible saved cursor, first adoption starts at
launch and does not backfill old commands.

Dual-provider notification receipts now use indexed durable operations instead
of rewriting all historical receipts for every notification. One-time migration
retains an exact legacy snapshot and commits uncertain claims before sending.
Sent and uncertain receipts continue to prevent repeated sends. Before an
old-binary rollback, stop every process using the runtime and run the current
executable's `notification-receipts-export` command to export **all latest**
receipts; restoring only the original snapshot is unsafe. See
[the rollback procedure](NOTIFICATION_RECEIPTS.md).

Granted Slack bot instructions receive request-correlated feedback in the
originating mapped thread. A queued acknowledgement establishes admission,
not model execution or completion. Existing grants, one-shot tickets, UUID
deduplication and conservative uncertainty remain; unverified or unmapped input
does not gain access. No old instruction is replayed by an upgrade.

Saved pairings, credentials, notification destinations, runtime/profile choices
and unrelated settings are reused. Windows x64, Linux x64, Linux ARM64 and macOS
ARM64 preview are built from one canonical public source revision. Acceptance
retains native packages, privacy, complete artifact hashes, the embedded Windows
icon and the immediately previous public 2.2.2 upgrade path. The separately
deployed 2.2.3 candidate is also an upgrade-preservation predecessor.

macOS remains an ARM64 preview. Unmapped-window and other-window ownership limits,
shared-home Linux stale previous-boot writer recovery and Windows follower-view
recovery-notification limits remain documented. Native writer parking alone
does not establish editor GUI recovery. QQ/OneBot behavior is unchanged.
