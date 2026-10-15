# Notification receipts and finite retention

WatchDog durably claims an exact event/provider/destination/payload before its
one permitted external attempt. A confirmed response records `sent`; failed,
uncertain and crash-left attempts remain ineligible for automatic retry. Missing
acknowledgements and historical uncertainty do not block later independent work.
Current live sender/owner/writer guards, authorization and native FIFO remain.

Prepared finite notification roots use indexed receipt operations, a compact
current fingerprint per configured workspace/event and bounded recent claims.
Eligibility uses the producer's original source time and the durable monotonic
retirement boundary. A retry, ticket timestamp or restart cannot make an old
effect fresh. Unknown source evidence or an unavailable receipt family refuses
the affected action; it does not authorize a legacy fallback or another attempt.
Independent healthy providers can continue.

Native-session and unbound observer scopes are distinct. An observer alert does
not establish native ownership, a session grant or permission to dispatch work.
A newly verified current native scope can enroll on an already prepared root
using its exact committed current claim, preserving the original root floor.
Existing damaged boundaries are not silently replaced. Current HTTP receipt pins
remain separate from native/provider queue and journal references.

## First install

A verified current notification can initialize a genuinely absent notification
directory under its actual sender and notification locks. Both finite families
and readiness are published before the current claim. The producer's original
birth is retained; initialization/restart time does not make an old event fresh.
Only exclusive creation of that absent directory establishes a fresh namespace.
An existing empty, legacy, partial, malformed or redirected directory is never
silently treated as new. Interrupted publication needs controlled forward
recovery, preserving its original plan and uncertain claims. New current observer
and native scopes enroll through their exact current claim; damaged existing
boundaries are refused without blocking an independent healthy scope.

## Controlled upgrade

Profiles, pairings, routes, grants, unknown compatible settings and credentials
are reused. Notification compaction does not delete Codex conversations/history,
modify native queues or export secrets. Existing schema2 reply journals retain
their bounded compatibility reader; that reader does not implicitly migrate or
prune another owner's state.

The selected notification root is supplied with `--runtime`; it is distinct
from a controller root when an existing authority selects portable storage.
Existing notification history requires the explicit offline transition in
[retention and upgrades](NOTIFICATION_RETENTION.md). The operator excludes all
actual old notification senders and subprocesses, supplies the exact current
projection and a fresh native receipt, then invokes `notification-retention-prepare`
on the candidate. A cooperative lock or JSON assertion alone is not OS proof.
Ordinary polling, observation, Stop handling and startup do not perform historical
cold import. Genuine running Codex jobs can remain active while senders are
excluded.

Readiness and the original cutoff precede retirement of recognized disposable
notification history. Original source runtimes, provider-authority rollback
backups and unknown or malformed files remain untouched. Optional cleanup failure
does not undo readiness or block a later independent event. No default historical
archive is created. See [session activity and reply expiry](SESSION_ACTIVITY.md).

## Forward recovery and legacy export

After finite activation, preserve the compact intent/ready metadata and existing
claims. An interrupted operation resumes explicitly forward with fresh actual
sender exclusion. Do not restart an old executable against compacted state or
restore an old receipt archive as live state. Retained old images do not authorize
an active downgrade after candidate activity.

The retained `notification-receipts-export` support command applies only to
supported unconverted legacy stores. It cannot export a prepared finite root
for old senders, reopen closed tickets, replay old UUIDs or authorize a downgrade.
Legacy compatibility and finite retention are different formats; a schema label
alone does not prove safe recovery. Preserve the original evidence on refusal.
