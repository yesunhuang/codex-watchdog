# Codex WatchDog 2.3.1

Desktop discovery now retains a uniquely verified native Codex owner when VS Code
has discarded routing markers and its readable session cache is empty. The
fallback still requires the exact live window's app-server PID to hold that
thread's native writer lock, a unique loaded user thread and a final held-lock
check. Unavailable or conflicting caches, multiple owners, explicit inactive or
failed views and released writer locks continue to fail closed.

Saved profiles, credentials, pairings, routes, grants, queues and prepared
notification state are reused. This release changes discovery only; it does not
replay historical commands or relax authorization, one-shot tickets, UUID
deduplication, native FIFO or provider/session last-four ticket limits.

Windows x64, Linux x64, Linux ARM64 and macOS ARM64 preview packages use the same
canonical source. Windows package acceptance includes the immediately previous
public 2.3.0 upgrade path and the embedded application icon.

Existing other-window coverage, stale-boot native recovery and macOS preview
limitations remain. Provider polling alone does not prove model completion;
Feishu/Lark retry problems are not claimed fixed by this change. Existing prepared
notification roots retain forward-only recovery: do not restart an old sender
against them or restore old journals. See [retention and upgrades](NOTIFICATION_RETENTION.md).
