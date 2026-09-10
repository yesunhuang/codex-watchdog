Version 0.2.7 fixes tracking of an active VS Code conversation when an older,
inactive conversation remains loaded. Discovery selects the unique explicitly
active owner only when competing loaded chats are explicitly inactive; ambiguous
cases still fail closed. Monitoring cycles now expose degraded workspace reasons.

Linux gains automatic handoff for the same existing Remote-SSH conversation.
A persistent remote WatchDog waits while the desktop has control, takes over
after detach and writer release, and returns control at a safe idle boundary.
Remote-host file locks, owner leases and increasing epochs fence queue delivery,
Stop consumption, writer claims, cursors and notifications. Queue receipts, Slack
reply routing and compatible runtime state survive ownership changes. Explicit
Linux bind/run/release commands remain available.

Trusted Stop hooks retry brief ownership-lock contention, including initial
admission, without adopting a replacement owner's epoch or shortening the
production grace period. Unknown notification outcomes remain blocked instead
of being blindly resent. Provider credentials stay in their existing stores.

Native Ubuntu ARM64 source acceptance covers attached priority, actual client
close, autonomous same-thread queue and trusted Stop at 30,004 ms, idle remote
restart, stale-owner rejection, and handback. VS Code's first resume during
handback remained pending; reloading that workspace and reopening the same chat
completed execution reattachment. See the
[automatic handoff guide](https://github.com/yesunhuang/codex-watchdog/blob/main/docs/AUTOMATIC_REMOTE_HANDOFF.md)
for persistent startup, coordinated upgrades and this retry. Other native desktop
topologies and packaged real-user lifecycle tests remain separate acceptance paths.

Upgrade the desktop, remote WatchDog and trusted hook implementation together
before enabling automatic handoff. Compatible profiles, unknown settings,
runtime paths and stable trusted commands are preserved; a changed hook command
still requires normal user trust. No secrets are migrated or reauthorization
fabricated.

Windows x64, macOS 15 ARM64 preview, Linux ARM64 and Linux x64 remain separate
executable ZIPs. Linux x64 retains the RHEL 8/glibc 2.28 baseline; ARM64 retains
glibc 2.35. The Mac package remains ad-hoc signed and not notarized. This release
retains automatic publication gates for three-OS source tests, all four packages,
full-history secret scanning, the embedded approved Windows icon and upgrade from
the actual immediately previous public Windows v0.2.6 executable.

English, Chinese and Japanese READMEs remain together at the repository root,
with matching Quick Start sections and automatic-handoff guidance. Previous
published versions and assets are not overwritten.
