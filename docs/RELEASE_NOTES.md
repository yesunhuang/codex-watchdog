# Codex WatchDog 2.3.2

Git-triggered wakes now support cone and non-cone sparse checkouts, sparse indexes,
and sparse linked worktrees. WatchDog delegates index/pattern interpretation to
Git and disables optional Git locks so observation does not refresh the index.
Excluded files remain excluded; patterns, configuration, working files and refs
are not rewritten. Existing live Git locks, conflicts, ambiguous configuration,
upstream/identity checks and exact-session targeting still fail closed.

This fixes a blanket sparse-checkout refusal that prevented automatic wakes even
when the original Codex conversation was tracked and its remote was reachable.
The same remote-event claim, at-most-once dispatch and native ownership rules apply;
historical uncertain events are not replayed.

All four supported packages use the same canonical source. Windows acceptance
includes upgrading the immediately previous public 2.3.1 profile/runtime without
re-entering settings and verifying the embedded application icon. Saved credentials,
pairings, routes, queues, grants and prepared notification state are reused in place.

Existing other-window coverage, stale-boot recovery, provider retry and macOS
ARM64 preview limitations remain. Prepared notification roots retain forward-only
recovery; do not restore old journals or restart old senders against them.
See [retention and upgrades](NOTIFICATION_RETENTION.md).
