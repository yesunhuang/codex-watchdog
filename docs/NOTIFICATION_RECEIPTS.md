# Dual-provider notification receipts

Slack and Feishu notifications retain a receipt for each event and provider.
WatchDog replaces repeated full-history JSON rewrites with
indexed SQLite point operations. Existing notification destinations, mappings,
settings and credentials remain in place.

The store durably records `uncertain` before attempting a provider send. A
confirmed response changes that provider's receipt to `sent`. Both states
suppress another attempt, including after restart, a timeout or a failed
confirmation write. Receipt-storage failure prevents a new send; it never
replays a task or resets a reply ticket. Slack bot-command acknowledgements
continue to use their separate existing reply journal.

On first use, the current version validates and imports the complete legacy
`notifications/dual-deliveries.json` once under `notifications.lock`, retains an
exact atomic backup, and commits the indexed store before publishing a schema-2
marker. Every sent, uncertain and valid empty legacy entry is retained. Later
notifications use point reads and small committed writes; migration and explicit
rollback export are separate one-time history-sized operations. Missing,
incompatible or ambiguous state fails closed.

## Rollback to a previous binary

Stop every WatchDog process using the runtime normally, then use the current
executable with that runtime:

```text
codex-watchdog --runtime <existing-runtime> notification-receipts-export
```

This offline support command exports **all current receipts**, including those
created after migration, atomically into the legacy schema-1 format. It uses the
existing notification lock, refuses a held foreground or notification lock,
retains the indexed store and recovery snapshots, and does not load credentials
or contact either provider. Every process using the runtime must remain stopped
until rollback is complete.
Only after a successful export should the previous executable start.

An older binary encountering the schema-2 marker refuses dual-provider sends.
Restoring only the original pre-migration JSON backup is unsafe because it omits
later receipts. Preserve the indexed store and use the complete export instead.
On reupgrade, the current version reconciles the readable legacy state, including
legitimate older-version additions, without losing or downgrading prior evidence.
Interrupted export or reconciliation preserves recoverable state and permits no
blind resend.
