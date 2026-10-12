# Shared Linux session relay authority

For opted-in Linux nodes sharing one current user's Codex home, portable messaging
state belongs to the existing exact Codex session. The canonical authority lives
under `watchdog-relay-authority/` in that home. Slack, Feishu/Lark and OneBot keep
separate provider journals, their existing namespaces and indexed UUID collision
protection. Session indexes restrict grants, tickets, routes and polling cursors
to the selected exact session. This does not create a Codex conversation, change
credentials, re-pair a provider or authorize another principal.

Native writer PIDs, boots, App Server ownership, queue transport and temporary
paths stay node-local. The shared native observation record selects the executor
and increments a relay epoch. A stale controller cannot consume a ticket, advance
a provider polling cursor, acknowledge a command or send a notification after losing that epoch.
Historical provider envelopes remain immutable; dispatch resolves the verified
current executor separately. Each logical effect is durably claimed before its
one allowed external attempt. Claimed, failed, uncertain, timed out, crashed and
abandoned-before-send effects are permanently ineligible for automatic retry.
Historical uncertainty is retained truthfully; it does not block a new command,
observation, notification or handoff. Current native queued work, exact sender
authorization, live ownership and in-flight sends retain their narrow fences.

Provider fetches occur outside the shared lock and are rechecked before effects.
The GPFS-compatible POSIX record lock excludes both processes and nodes; an
additional reentrant thread lock prevents sibling callbacks racing. Nested users
retain one file descriptor. Closed and evicted parents remain untracked. The
existing last four valid tickets per provider/session and bounded provider
pagination/backoff remain in effect.

## Existing-installation upgrade

Upgrade reuses compatible saved state. The initial authority import is an offline
operation; subsequent node handoffs never copy journals. Prepare the verified
candidate and preserve the previous executable before stopping old listeners.
Do not interrupt an active Codex writer to create a migration window.

Preview through the candidate executable:

```sh
codex-watchdog --codex-home "$codex_dir" linux-relay-authority-migrate
```

The output contains source paths, counts and logical snapshot hashes, rather than
journal values or credentials. Inspect the plan and any precise refusal. A
standalone legacy JSON journal or a missing schema-2 database fails closed; do
not remove it to force a fresh installation. The immediately previous release's
schema-2 provider journals are the supported import path.

Gracefully quiesce every configured old cluster listener using its existing
service mechanism. Verify each native host, boot, unit state and absence of
WatchDog processes. Preserve Codex writers and queues. Store the actual receipts
in a private mode-0600 JSON file; every configured node must be covered, and
each verification must be at most five minutes old:

```json
{
  "schema_version": 1,
  "purpose": "offline-relay-migration",
  "codex_home": "/absolute/current-user/codex-home",
  "nodes": [{
    "node": "login1.example",
    "boot_id": "actual-native-boot-id",
    "verified_at": "actual-UTC-timestamp",
    "MainPID": 0,
    "ActiveState": "inactive",
    "no_watchdog_processes": true
  }]
}
```

Use verified observations rather than copying the example as evidence. Then:

```sh
codex-watchdog --codex-home "$codex_dir" linux-relay-authority-migrate \
  --apply --quiescence "$receipt_file"
```

The importer takes consistent same-filesystem SQLite backups, freezes source
JSON/cursor/notification hashes, stages canonical provider and notification
state, rechecks the sources and native quiescence, fences old JSON readers and
publishes cluster/session readiness last. Its private migration manifest retains
backup provenance and historical quarantine variants. Interrupted staging or
readiness publication can resume from the same verified plan; changed sources,
unplanned staged authorization or damaged installed state are refused. Original
databases, owners, native state, user settings and credentials remain intact.

Existing Feishu/Lark and OneBot binding challenges and operations retain their
numeric epoch timestamps. The importer validates finite numeric SQLite metadata
and the established binding schema and exact identity without rewriting either
timestamp. SQLite conversion can change text precision, and a later binding
generation can update its payload while retaining the first metadata timestamp.
Other record kinds retain their ISO timestamp checks. Conflicting copies of a
binding still fail closed; migration does not reopen or complete a challenge.

Conflicting historical parent/notification identities become inert occupied-key
quarantine records. No hostname, payload or timestamp selects a winner. Conflicts
in grants, logical UUIDs or physical claims fail the import. A quarantined parent
does not open a ticket. New valid notifications can establish new mapped tickets
through the existing exact-session/grant checks.

Notification history stays in an indexed suppression union. Existing sent and
uncertain provider claims remain protected independently, including when the
user selects just one provider. An uncertain send is not reported as completed
or retried automatically. Historical imports do not enlarge per-event JSON.

In-place upgrades retain the one-shot SMTP/desktop fallback ledger unchanged.
The offline relocation importer refuses a source containing fallback claims or
an invalid fallback ledger instead of omitting that evidence. Existing 2.2.7
sources without this ledger, and valid empty ledgers, retain the existing import
path. This refusal creates no destination state and leaves the sources intact.

Start the verified candidate through the existing service. The selected native
owner enrolls future fresh sessions without importing old state again. Fresh
installations initialize an empty authority only after existence checks prove
there is no prior messaging journal or notification evidence.

## Live handoff and optional completion evidence

Native execution selection follows current ownership evidence. The shared POSIX
observation lock spans admission and effects; saved writer flags, transcript
stamps and old pending receipts are not live writer proof. An exact foreign-node
transition needs a bounded authenticated native writer observation. Its current
hostname, user, boot and session/repository must match. The native server verifies
its existing canonical lock or samples exact lock-file absence twice beneath a
stable private native directory; it creates no lock. Foreign and local inodes
need not match. Active writers, missing proof, access failures and ambiguous
native state refuse that transition. The
read-only `writer_observation` helper exposes this proof through an existing
verified transport. No credentials or routes are invented. A caller without a
verified peer route reports that current transition limitation; unrelated
same-node work continues. A remote snapshot is transition evidence, not a
lifetime lock against first-party VS Code starting elsewhere later.

Before a new native resume, admission holds the shared observation lock and
checks the current native FIFO. Already queued work is never duplicated or
reordered. Native VS Code writers and active model work are preserved. Current
local sender kernel leases also fence takeover; a foreign sender whose lease is
unobservable retains its currently live owner capability's handoff fence.
Expired capabilities are never revived by historical external-effect pointers.

Completion reconciliation is an explicit optional forensic operation. It is not
run synchronously before routine observation, activation, new commands or sends.
Missing or oversized historical evidence cannot hold those paths indefinitely.

## Completion and rollback

Passive reconciliation requires the exact instruction, prompt hash, thread and
immutable native dispatch boundary, one matching durable user-message turn, its
terminal completion and absence of that exact queued item. Queue admission,
consumption, a missing queue row or provider delivery alone is insufficient.
Reconciliation rotates at most four pending rows per provider per cycle, using
indexed seeks and a fixed 2 MiB transcript-read budget per receipt. Across the
three providers, one explicitly requested session pass reads at most twelve proofs / 24 MiB. Records
above that cap remain unread; they are never skipped to manufacture completion.
Missing, malformed or oversized evidence remains uncertain without blocking
independent traffic or receipt examination.

An existing registered session can optionally settle its original admitted
completion proof independently of a changed-boot ownership claim. This completion-only path requires
the existing ready cluster/session manifest, schema-2 journals, namespaces,
partial index and locks. It never initializes or migrates missing state. Under
the observation and provider locks, it updates only original proof progress,
proven completion metadata and the bounded reconciliation cursor. Other
providers and active rows can progress after an individual refusal.

Completion settlement grants no executor capability and changes no owner,
epoch, grant, ticket, UUID reservation, route or provider polling cursor. Native
writer verification and normal handoff admission remain separate. In particular,
a completed relay receipt does not resolve an independent uncertain Git wake.
Closed and evicted tickets remain untracked.

Before the candidate changes any canonical journal or notification state, a
verified offline rollback is available:

```sh
codex-watchdog --codex-home "$codex_dir" linux-relay-authority-migrate \
  --rollback --quiescence "$receipt_file"
```

Rollback verifies the frozen originals and pristine canonical state, fences the
candidate, restores only the known original refusal-marked JSON files and retains
canonical evidence. It never overwrites databases, owners, settings or queues.
After canonical state changes, restoring a previous dedup snapshot is refused:
retain the authority and repair forward with a compatible candidate. Do not
restart an older sender by deleting the markers, readiness or uncertainty rows.

One-attempt claims introduce a separate executable compatibility boundary.
The public 2.2.7 dispatcher, its embedded remote helper and its notification
sender do not read the newer shared wake and indexed notification claims. A
crash after a new claim but before a legacy receipt can therefore leave that
effect eligible to an old binary. Preserving the files alone does not make an
active downgrade replay-safe.

Once a candidate may have claimed an effect, retain the latest claims, provider
receipts, configuration and journals and recover with a reservation-aware build.
Do not automatically restart 2.2.7 senders or embedded-helper clients against
that state, or restore a pre-upgrade journal over newer claims. Old executable
backups remain usable before candidate effect admission. An excluded old host
must remain excluded until its current native state and supported upgrade are
verified; partial deployment does not establish protocol support on that host.

Native acceptance must separately record provider observation, exact admission,
queued/start state, durable model completion, returned provider notification and
idle handback. Use a fresh request UUID after migration; never replay a missed
historic command to prove success.

## Explicit retirement of an obsolete Git wake

An operator may retire one obsolete commit-change wake only with the owner's
explicit approval. Retirement is a separate disposition: the original delivery
remains uncertain. An empty queue, expired lease, changed hostname or absent
completion message never authorizes retirement automatically.

The native recovery API requires the exact Git instruction, session, repository,
original node and epoch, prompt hash, and original wake, owner and observation
hashes. It verifies the existing native VS Code writer, exact session and empty
session queue. The original selected observation must be parked, retain only
that pending wake, and have no unresolved external effect or saved writer. A
different pending command, an acknowledged native queue item or conflicting
identity fails closed.

Retirement adds an immutable schema-1 record under
`watchdog-git-retirements/` in the existing Codex home, indexed by instruction-ID
hash. It preserves the original wake, owner, observation, grants, tickets and
journals. The reservation permanently prevents dispatch of that ID across
nodes, even after the old owner changes. A conflicting or malformed reservation
blocks delivery; it never falls back to a fresh send. Publication is exclusive,
durable and coordinated with the courier's per-ID lock.

Creating the reservation grants no ownership and performs no handback. The
ordinary guarded observation path independently reserves selected legacy wake
IDs in `watchdog-wake-attempts/`, without modifying their uncertain receipts.
Explicit retirement is optional audit machinery, never a manual-per-UUID
activation prerequisite. Native writer checks, current owners, real foreign
queues and held sender/courier locks remain current-action fences. Continue
through that normal path and verify the actual selected node and monitoring;
never clear an ownership stamp or receipt to manufacture acceptance.

Older binaries do not implement this shared reservation. Preserve their original
occupied courier records and established observation fences during recovery,
and verify the affected executable upgrade before claiming fleet-wide protocol
support. Do not delete the reservation or restore an old snapshot over it.
