Version 0.2.10 adds notification-only Slack bot delivery. A service can use its
bot token and an explicit channel ID without an app token or reply allowlist.
This fixes setup where an existing incoming webhook points to a different channel
from the desktop reply configuration. The bot response must confirm the requested
channel; notification-only posts do not create reply mappings or start a competing
Socket Mode listener. Partially configured replies still fail closed, and complete
existing reply configurations keep their previous behavior.

Incoming webhooks retain their own destination. Omit a webhook from a bot-only
service when fallback to that destination would be incorrect. No upgrade copies
or changes credentials, channel selections, provider settings, or durable receipts.

This release retains v0.2.9's fix for a transient `linux-release` failure found during native x64
acceptance. The command now waits up to one second for initial control/sender
lock admission before changing state. Persistent contention returns the bounded
`linux_release_busy` reason. Binding changes and first activation during that
wait still fail closed; writes that have already started are never replayed.
The package harness now exercises the frozen release command under a held lock.

This release retains v0.2.8's notifications when a detached Linux WatchDog loses
control or monitoring of its existing Codex thread. App Server exit, a changed
writer, missing exact-thread metadata and observation errors now produce an
immediate loss alert through the Linux process's configured Slack/email transport.
The foreground owner reports before exiting; automatic mode remains blocked.

Outage identities persist across checks and restarts, so one outage produces one
alert and successful monitoring produces one recovery alert. Normal idle handback,
planned release, standby and brief lock contention do not generate loss alerts.
No thread is created, retargeted or automatically resumed again after uncertainty.

Health notifications use the same captured ownership capability and canonical
notification ledger as other effects. They can report unavailable thread metadata
without requiring that broken database probe to succeed. Expired/replaced owners
cannot send or change health state. Failed delivery no longer becomes successful
duplicate suppression; uncertain sends retain their existing barrier and are not
replayed. Status output and schema-1 local health records retain delivery results.

The Linux service must already have its own Slack and/or SMTP configuration.
Slack remains preferred with configured SMTP as fallback; no credentials means
`audit_only`, not an external alert. Host or whole-process failure requires the
host's existing service supervision/monitoring. See the
[automatic handoff guide](https://github.com/yesunhuang/codex-watchdog/blob/main/docs/AUTOMATIC_REMOTE_HANDOFF.md)
for service environment, delivery checks and the existing VS Code reload caveat.

English, Chinese and Japanese Quick Starts are synchronized at the repository
root. Compatible profiles, runtime paths, bindings, hooks, provider settings,
unknown configuration keys and delivery receipts are preserved. No credentials
are copied between hosts or security boundaries.

The four executable packages retain their existing platform baselines: Windows
x64, macOS 15 ARM64 preview, Linux ARM64/glibc 2.35 and Linux x64/glibc 2.28.
The Mac package remains ad-hoc signed and not notarized. Automatic publication
requires three-OS tests, all four package gates, complete-history secret scanning,
the approved embedded Windows icon and upgrade from the actual immediately
previous public Windows v0.2.9 executable. Earlier releases remain immutable.
