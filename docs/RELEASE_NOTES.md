# Codex WatchDog 2.2.8

WatchDog now reserves each logical effect durably before one permitted external
attempt. Failed, uncertain, timed-out and crash-left attempts remain ineligible
for automatic retry. Their original evidence is preserved, while later distinct
commands, Git observations, notifications and supported handoffs can proceed.
Historical wake, acknowledgement and completion records no longer gate routine
activation. Completion reconciliation remains an explicit bounded forensic tool.

Current native writers, queued work, live ownership, sender leases and in-flight
sends retain their guards. Exact-session grants, principals, routes, one-shot
tickets and UUID deduplication are preserved. Closed or evicted tickets remain
untracked; the last four valid tickets per provider/session and bounded indexed
polling, failure isolation and provider backoff remain in effect.

Linux handoffs use current authenticated native ownership evidence rather than
historical receipt settlement. A cross-node transition still needs a verified
peer transport; automatic peer-route configuration is not included. A refusal
affects that transition, while independent selected-owner work can continue.
Existing shared authority state is reused without repeating its offline import.
See [Linux authority, recovery and rollback](LINUX_SESSION_RELAY_AUTHORITY.md).

Saved profiles, credentials, pairings, destinations, grants and journals are
reused. Windows x64, Linux x64, Linux ARM64 and macOS ARM64 preview are built from
one canonical public revision. Package acceptance includes the immediately
previous public 2.2.7 profile/runtime upgrade path and the embedded Windows icon.
Source, package, native deployment and end-to-end acceptance remain distinct.
Once the candidate has claimed an effect, older 2.2.7 senders and embedded
helpers do not enforce those new claims. Recover with a reservation-aware build;
do not automatically restart old senders against post-candidate state or restore
an older journal. Retained old images support rollback before candidate activity.

macOS remains an ARM64 preview. Other-window coverage and Linux stale-boot writer
recovery limitations remain. Native writer parking does not establish editor GUI
recovery. No historical request replay or access expansion is introduced.
