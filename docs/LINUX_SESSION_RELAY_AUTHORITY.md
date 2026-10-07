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
a cursor, acknowledge a command or send a notification after losing that epoch.
Historical provider envelopes remain immutable; dispatch resolves the verified
current executor separately. Same-node/boot restart can reconcile existing work;
another executor cannot take over unresolved delivery or native queued work.

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

Conflicting historical parent/notification identities become inert occupied-key
quarantine records. No hostname, payload or timestamp selects a winner. Conflicts
in grants, logical UUIDs or physical claims fail the import. A quarantined parent
does not open a ticket. New valid notifications can establish new mapped tickets
through the existing exact-session/grant checks.

Notification history stays in an indexed suppression union. Existing sent and
uncertain provider claims remain protected independently, including when the
user selects just one provider. An uncertain send is not reported as completed
or retried automatically. Historical imports do not enlarge per-event JSON.

Start the verified candidate through the existing service. The selected native
owner enrolls future fresh sessions without importing old state again. Fresh
installations initialize an empty authority only after existence checks prove
there is no prior messaging journal or notification evidence.

## Completion and rollback

Passive reconciliation requires the exact instruction, prompt hash, thread and
immutable native dispatch boundary, one matching durable user-message turn, its
terminal completion and absence of that exact queued item. Queue admission,
consumption, a missing queue row or provider delivery alone is insufficient.
Reconciliation rotates at most four pending rows per provider per cycle, using
indexed seeks and a fixed 1 MiB transcript-read budget per receipt. Missing,
malformed or oversized evidence remains fenced without starving other receipts.

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

Native acceptance must separately record provider observation, exact admission,
queued/start state, durable model completion, returned provider notification and
idle handback. Use a fresh request UUID after migration; never replay a missed
historic command to prove success.
