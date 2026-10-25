# Host completion notifications

On an already controlled Linux host, WatchDog can notify a freshly completed
Codex turn even when later work has started. It uses the existing current compact
Stop and matching output from that exact session, repository and runtime.
The original native owner, notification target and sender guards remain required.
No session, callback definition or permission is created by this feature.

The host completion cursor is versioned. The first observation after upgrade
records a baseline and sends no preexisting output. A later fresh completion can
be sent once through the existing notification ledger. Missing, invalid or
uncertain evidence is not repaired or replayed. Existing closed tickets remain
closed, and the per-provider/per-session last-four ticket policy is unchanged.

The supported host cycle selects the optional native rollout path by an indexed
exact-thread lookup. It does not search historical session directories. The
existing tail fallback reads at most one MiB, including concurrent file growth.
Current Stop metadata and output reads also have fixed caps. An optional hint
failure does not add a history scan; independent native-owner verification still
applies before observation or notification.

Observation remains latest-only and best effort. It does not guarantee every
turn between arbitrarily fast completions, reopen consumed parents or backfill
historical notifications. Desktop and other remote adapters retain their existing
selection behavior; the common tail read cap applies to their existing callers.

Saved settings, credentials, grants, routes, queues, receipts and native work
are reused. The nested schema-version-1 cursor is added without replacing or
normalizing other state. Unknown or malformed cursor data is retained, with a
conservative process-local baseline permitting later independent work.
