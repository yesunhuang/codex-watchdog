# Codex WatchDog 2.3.7

An already trusted Linux `Stop` or `PermissionRequest` hook can recover an
existing opted-in node service when its user systemd manager reports the original
enabled unit missing and inactive. The helper verifies the exact native writer,
thread and repository, retained unit/environment, selected image hash and typed
effective `ExecStart` argument array before starting the original service.
Typed C-escaped strings are decoded to their exact UTF-8 bytes, keeping literal
backslashes distinct from control characters; malformed or ambiguous strings refuse. Active,
deliberately stopped, failed or ambiguous services are not restarted.

Recovery has a five-second work budget and bounded configuration/output reads.
It reuses the node installation lock, refuses a competing controller and retains
one versioned receipt. It does not adopt ownership, dispatch a model command,
change authorization or replay uncertain effects. The original owner guard still
runs after optional recovery.

The hook-selected executable must receive the upgrade as well as the daemon
image. Existing `/usr/bin/systemctl` and `/usr/bin/busctl` are required. This is
recovery on renewed native use, **not an unattended-startup guarantee before a
genuine callback after reboot**. See [Linux recovery](LINUX_STARTUP_RECOVERY.md)
for prerequisites, shared-home installation boundaries and refusal behavior.

The package set covers Windows x64, Linux x64/ARM64 and macOS ARM64 preview from
one canonical source. It retains the Windows discovery fix from 2.3.5. On Windows
and macOS, the Linux-only hook path is not imported; normal owner enforcement is
unchanged. Saved profiles, runtime, settings, secure credentials, pairings,
grants, routes, claims and queues are reused without a persistent schema change.
Windows acceptance uses the immediately previous public 2.3.5 upgrade path and
verifies the embedded application icon; Linux/macOS deployed-profile acceptance
must also preserve the existing 2.3.3 baseline.

Existing stale-boot, other-window coverage, provider uncertainty and macOS preview
limitations remain. Do not restore old journals or replay uncertain work. No
antivirus exclusion, trust change or security-product clearance is implied by
this release. Release history and prior Windows detection disclosures remain in
[GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases).
