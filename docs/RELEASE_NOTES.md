# Codex WatchDog 2.2.7

Linux shared-home relay state now uses one indexed authority for each provider.
The explicit offline migration preserves existing grants, routes, request UUIDs,
uncertain deliveries and notification suppression. It requires verified stopped
listeners and native ownership evidence; an unreachable node is not proof that
its listener stopped. Conflicting authorization or request identities fail closed.
See [migration and rollback](LINUX_SESSION_RELAY_AUTHORITY.md).

Existing Feishu/Lark and OneBot binding records migrate without changing their
numeric timestamps. SQLite text precision and later binding generations remain
compatible. ISO checks for other records and strict conflict checks are preserved.

Passive native completion reconciliation requires the exact durable admission,
matching completed turn and absence of its queued item. It never redispatches an
old command. Saved credentials, pairings, destinations and settings are reused.
Closed and evicted tickets remain untracked; the last four still-valid tickets
per provider and session retain bounded polling and failure isolation.

Windows x64, Linux x64, Linux ARM64 and macOS ARM64 preview are built from one
canonical public revision. Windows acceptance includes its embedded application
icon and the immediately previous public 2.2.5 profile/runtime upgrade path.
Native acceptance and deployment evidence remain separate from source tests.

macOS remains an ARM64 preview. Other-window ownership coverage and Linux stale
previous-boot writer recovery limitations remain. Native writer parking alone
does not establish editor GUI recovery. No new grant or historical replay is
introduced by this release.
