# Codex WatchDog 2.3.8

Linux host monitoring can report a freshly completed turn while later native
work is already active. It reads the existing compact Stop and matching bounded
output from the exact controlled runtime, retaining the original completion time,
outcome and notification identity. This corrects a selection gap that could leave
an already-finished reply without a notification attempt.

The opted-in host cycle uses the existing indexed native thread lookup, with
no recursive session-history search. The existing tail fallback has an explicit
one-MiB read cap, including when its file grows after the size check.

The versioned host completion cursor starts with a forward-only baseline.
Preexisting completions are not sent on upgrade. Malformed or unavailable current
evidence is refused without blocking later independent observation. Existing
claims, uncertain deliveries, one-shot tickets and queues are preserved; no old
request or missed historical notification is replayed. Current/latest-only
observation remains best effort rather than a journal of every intervening turn.

Windows x64, Linux x64/ARM64 and macOS ARM64 preview are built from one canonical
source. Saved profiles, routes, credentials, registrations, native owners and
workloads are reused. Windows acceptance includes the previous public 2.3.5
profile; native deployment also checks the installed 2.3.7 baseline. The embedded
Windows icon and existing package/privacy gates remain required.

The Linux startup recovery and Windows discovery corrections from earlier
versions are retained. Existing Linux recovery prerequisites and macOS preview
limitations remain documented. No security exclusion, trust change or
security-product clearance is implied by this release.

See [host completion behavior](HOST_COMPLETION.md) and the retained
[Linux startup recovery](LINUX_STARTUP_RECOVERY.md) prerequisites.
