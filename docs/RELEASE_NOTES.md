# Codex WatchDog 2.3.4 - Windows discovery hotfix

Windows automatic discovery can now recover when VS Code's `--status`
diagnostic returns empty or malformed output, fails in its process formatter,
or times out. Newer CLI-only log directories no longer hide the running GUI
generation. Diagnostic output, work and outstanding pipe readers are bounded.

The read-only fallback verifies current-user native GUI, extension-host and
Codex app-server identities, exact workspace/thread qualification, fresh owner
selection and the original held writer PID and creation identity. It preserves
the existing native conversation. Collection and completion use finite budgets
and fair ordering; historical log/storage/thread growth does not add hot-path
scans. Ambiguous or incomplete evidence remains unresolved.

This is a **Windows x64-only** immutable hotfix. All changed application behavior
is Windows-guarded. The existing **2.3.3** macOS ARM64 preview and Linux ARM64/x64
packages remain applicable for those platforms in the
[2.3.3 release](https://github.com/yesunhuang/codex-watchdog/releases/tag/v2.3.3).
The full-platform release remains marked Latest. No existing release is replaced.

Upgrade the existing current-user Windows installation normally. Saved settings,
secure credentials, pairings, grants, queues, claims and prepared notification
state are reused in place; no persistent schema migration or reconfiguration is
introduced. Windows package acceptance includes the immediately previous public
2.3.3 upgrade path, startup without Python and the embedded application icon.

Coverage remains best effort. A GUI startup outside the bounded locator interval,
inaccessible native identity or an unreadable log needed to prove same-main
uniqueness can leave that GUI domain unmonitored. Existing explicit registrations
remain supported. See [Windows discovery and recovery](WINDOWS_DISCOVERY.md).

Existing stale-boot recovery, other-window coverage, provider retry and macOS
preview limitations remain. Prepared notification roots retain forward-only
recovery; do not restore old journals, replay uncertain effects or restart old
senders against them. Git wake coalescing and sparse-checkout support are retained.
See [retention and upgrades](NOTIFICATION_RETENTION.md).
