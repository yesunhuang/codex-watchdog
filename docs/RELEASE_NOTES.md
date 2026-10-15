# Codex WatchDog 2.2.9

WatchDog keeps routine observation, polling, Stop handling and notification work
independent of accumulated history. Indexed current snapshots, bounded receipt
retention and source-time retirement preserve at-most-once delivery: a failed,
uncertain or crash-left claim remains ineligible for automatic replay while later
independent work can proceed. Current native writers, sender fences, exact grants,
FIFO, one-shot tickets, UUID deduplication and provider backoff retain their guards.

Reply tickets expire at their absolute 72-hour lifetime and retain the last four
still-valid tickets per provider and exact session. Closed or evicted tickets stay
untracked. Dormancy follows genuine activity; a restart is not new activity and
genuine ongoing native work remains monitored. A later verified activity
generation can produce one new Hello through the configured route.

First-install notifications initialize only a genuinely absent namespace under
actual sender exclusion, preserving original event birth and committing both
finite receipt families before sending. Existing notification history uses the
explicit controlled offline `notification-retention-prepare` transition. It
requires actual old-sender/subprocess exclusion, an exact current projection and
a fresh bound native receipt; the command does not establish an OS census itself.
Interrupted preparations recover forward with their original plan and claims.
Do not restart old senders against prepared finite state or restore old journals.
See [notification retention and upgrades](NOTIFICATION_RETENTION.md).

Saved compatible profiles, credentials, pairings, destinations, grants and provider
journals are reused without routine reconfiguration. Windows x64, Linux x64,
Linux ARM64 and macOS ARM64 preview use one canonical public revision. Package,
previous-public 2.2.8 upgrade, compiled behavior, native rollout and fresh
end-to-end acceptance remain distinct; individual results and exclusions must be
verified before publication. The Windows executable embeds the application icon.

macOS remains an ARM64 preview. Other-window coverage and Linux stale-boot writer
recovery limitations remain. Native writer parking does not establish editor GUI
recovery. No historical request replay or access expansion is introduced.
