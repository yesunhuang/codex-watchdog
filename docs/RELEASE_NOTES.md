# Codex WatchDog 2.3.3

Automatic Git updates now share one pending native queue wake per exact Codex
session. If another Git wake is already queued while Codex is busy, WatchDog
keeps the latest remote tip pending instead of appending a wake for every commit.
After native consumption, the latest still-unsynchronized tip can queue one
follow-up; if Codex already caught up, no follow-up is needed.

The same bounded, indexed, read-only queue check covers local and Remote-SSH
observers. It runs under the existing owner/sender fences, before claiming the
new event. Unknown current queue evidence defers only that Git action. Historical
uncertainty remains terminal for its own effect and does not block independent
work. Existing queued messages are not removed or rewritten. Human commands,
provider replies, temporal prompts, exact authorization, native FIFO, one-shot
tickets and UUID deduplication retain their existing behavior.

All four packages use the same canonical source. Windows acceptance includes the
immediately previous public 2.3.2 upgrade path and the embedded application icon.
Existing saved settings, credentials, pairings, queues, grants and prepared
notification state remain in place; no persistent schema migration is introduced.
Sparse-checkout support from 2.3.2 is retained.

Existing other-window coverage, stale-boot recovery, provider retry and macOS
ARM64 preview limitations remain. Prepared notification roots retain forward-only
recovery; do not restore old journals or restart old senders against them.
See [retention and upgrades](NOTIFICATION_RETENTION.md).
