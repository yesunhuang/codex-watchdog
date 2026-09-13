# Release notes

## v0.2.26

- Git-attention alerts now queue a deduplicated prompt to the exact existing
  Codex thread even when the read-only Git check cannot obtain an upstream
  commit ID. Codex handles the blocker; WatchDog performs no Git mutations.
  Pending and uncertain deliveries survive restart without a blind resend.
- Linux automatic controllers reconcile the saved wake ID with its original
  courier receipt, so a delivered wake does not remain pending indefinitely.
- Cluster node mode retains Git observation after an idle thread parks. A small
  shared observation receipt retains the verified node, excludes old parked
  nodes, and requires fresh native ownership for a handoff. Active or pending
  work blocks that handoff. Changed foreign history and foreign queue items
  cannot authorize a wake. Existing Slack parent routing is preserved.
- Linux controller shutdown checks the actual child process status when an
  expired lease prevented normal polling. Proven exited children are retired;
  live or uncertain writers remain protected.
- Existing profiles, hooks, credentials, mappings and completion cursors remain
  in place. Shared-home upgrades with ambiguous old node records need fresh
  native attachment before selecting a Git observer. Automatic migration of
  an active conversation between login nodes remains unsupported.

## v0.2.25

- Corrects the Slack mapping API mismatch in the unpublished v0.2.24 candidate
  that prevented Linux controllers from starting with Slack configured. Native
  package acceptance now exercises Slack-enabled node startup and restart with
  synthetic state and no provider credentials. v0.2.24 was not released.

- Linux node mode registers verified native VS Code writers independently of
  the configured Git/notification observation interval. A new thread can now be
  recorded while a slower observation is in progress, preserving its eligibility
  for same-thread continuation after the window closes. Previously a brief
  attachment entirely between observation cycles could be missed.
- Registration checks the current node's kernel writer and exact repository;
  shared-home logs still cannot authorize another node's conversation. The
  registration worker stops before the controller lock is released. Existing
  profiles, trusted hooks, Slack settings and delivery receipts are preserved.
- Automatic continuation remains opt-in with `--continue-interrupted`; it
  resumes only a verified interrupted turn and sends its existing confirmation
  after the continuation actually starts. The configured observation interval
  and desktop foreground behavior are unchanged.

## v0.2.23

- A newly discovered workspace retains its first completed Stop when the hook
  finishes after WatchDog starts but before discovery resolves the window.
  Existing completion cursors and notification deduplication are preserved;
  completions present before startup remain excluded by default.
- Discovery recognizes saved VS Code workspaces recorded with
  `workspaceIdentifier.configURIPath`. Conflicting workspace targets still
  fail closed, and the exact existing Codex conversation remains the target.
- Slack notifications show the native machine name. Windows and Linux
  reply acknowledgements also identify their sending host. Remote-SSH notifications
  identify the thread's SSH destination separately from the WatchDog machine.
  Routing, duplicate suppression and saved configuration are unchanged.
- The Mac launcher supports `--shared-slack-app`; the small Desktop helper uses
  it to poll the Mac's mapped notification threads when the same Slack app is
  also connected on another machine. Existing Socket Mode mappings are retained
  without replaying old replies across transports.
- Compatible profiles, runtime paths, Slack settings, Keychain entries, trusted
  hook commands and delivery receipts are reused during upgrade. No UI or
  background service is added.

## v0.2.19

- Linux can receive allowlisted Slack replies independently of the desktop with
  `CODEX_WATCHDOG_SLACK_REPLY_MODE=poll`, the existing bot token/channel, and an
  approved-user list. Bot/channel-only configuration remains notifications only.
- Both bound and automatic Linux modes start their reply listener. Polling uses
  exact saved parents, durable cursors and existing queue receipts; uncertain
  delivery is never blindly replayed. Existing Socket Mode state stays separate
  and unchanged, preventing competing listeners from duplicating a reply.
- Concurrent host observation and reply requests retain separate helper results.
- The three README translations explain reply configuration and shared-home
  limits. Sharing installation files does not make native conversation locks
  safe across cluster nodes; keep execution and WatchDog on the same node.

## v0.2.18

Automatic Linux mode can select exact repositories with repeated `--repo` paths.
Confirmed exited backends release stale ownership for safe same-thread recovery.
Failed observation no longer renews its lease indefinitely; live or uncertain
writers remain protected.

## v0.2.17

Version 0.2.17 fixes missing or delayed Linux completion notifications while
VS Code is attached. The running host WatchDog now keeps observation and
notification authority for its enrolled threads. VS Code retains its native
Codex writer; returning that writer from detached execution no longer abandons
monitoring. Desktop WatchDog remains the fallback when the host releases its
lease or becomes unavailable. Older desktop helpers remain compatible.

Ownership changes preserve completion cursors, notification receipts, existing
thread IDs, hooks, profiles and credentials. An unresolved external send blocks
takeover, and already delivered completions are not replayed. Manual release and
thread exclusions still apply. v0.2.16's opt-in interrupted-turn continuation
and confirmed-start notification remain available.

Version 0.2.14 restores automatic tracking of an existing conversation when Codex
shows it as an active follower in its workspace window but retains the writer in
another live VS Code window. WatchDog now verifies the exact thread, repository,
held lock, operating-system writer PID and current owner log together. Unrelated
threads remain separate; stale owner logs cannot authorize a different writer.

This recovery is available on Windows and Linux. Hosts without writer-process
evidence keep failing closed. Normal same-window ownership is unchanged. The
ownership mismatch stays visible and produces a deduplicated notification through
the configured transport. An unverified or ambiguous owner pauses automatic
targeting and produces an attention notification instead of silently disappearing.
No ownership transfer, second conversation, App Server restart or configuration
reset is performed. Codex's native window role may still require manual recovery.

Version 0.2.13 restores tracking when VS Code reuses a window for a different
workspace. Extension-host logs can contain both the previous workspace's storage
ID and the current workspace's ID. Discovery previously combined these IDs across
host generations and rejected the current window as ambiguous. It now accepts
only the storage ID recorded for the exact current extension-host generation.
Missing or conflicting current-generation evidence still fails closed.

This fixes a reproduced case where a local thread disappeared while a separate
Remote-SSH thread in a repository with the same name stayed tracked. Local and
remote identities remain independent. The underlying bug predates the recent
handoff releases: replaying the same log against v0.2.0, v0.2.3, v0.2.6, v0.2.10,
v0.2.11 and v0.2.12 reproduces the failure. Existing thread ownership, handoff,
queues, hooks, credentials and notification receipts retain their behavior.

This release retains v0.2.12's fix for control-lock contention during Linux queue
admission. A busy admission keeps the owner alive without replaying queue effects.

Version 0.2.11 fixed a false monitoring failure reproduced during native Linux
package acceptance. A queue/release command can briefly hold the control lock
between the owner's writer check and its workspace observation. That exact
contention now skips the observation, keeping the owner running without issuing
a loss or recovery alert. The next cycle checks ownership again. Stale epochs,
expired leases and changed activation remain failures; skipped observations do
not establish recovery.

This release retains v0.2.10's notification-only Slack bot delivery. A service can use its
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
The Mac package remains ad-hoc signed and not notarized. Manual publication
requires three-OS tests, all four package gates, complete-history secret scanning,
the approved embedded Windows icon and upgrade from the actual immediately
previous public Windows v0.2.12 executable. Earlier releases remain immutable.
