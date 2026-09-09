# Automatic remote handoff (development candidate)

This source candidate is awaiting native client detach/reattach acceptance.
Published v0.2.6 retains the explicit Linux binding workflow. Upgrade the desktop
WatchDog, Linux WatchDog and the code behind the existing trusted Stop command
together before enabling automatic handoff. Existing hook trust is reused only
when the command remains unchanged; WatchDog never fabricates hook approval.

Run the Linux process independently of an SSH terminal, for example from a user
service. The source entry point is:

```sh
python -m codex_watchdog --runtime /absolute/existing/runtime linux-auto-run --interval 5
```

The desktop's existing Remote-SSH observer automatically identifies eligible
threads. No bind command is needed for this path. The Linux process only considers
threads previously identified by an attached desktop and waits while VS Code
holds their writer. Use `--exclude workspace-name` to exclude a workspace.

The remote Codex home contains one `watchdog-control/<thread>/owner.json` record,
protected by a kernel file lock. `ATTACHED_LOCAL` gives an attached desktop control
priority. `HANDOFF` requests an idle boundary. `DETACHED_REMOTE` permits the Linux
process to resume the same exact existing thread. Every grant advances the epoch.
An expired lease cannot displace a still-held remote writer. Reattachment requests
release; it does not interrupt an active turn or replay an interrupted command.

Queue delivery, trusted Stop consumption, writer claims, cursor updates and
notifications validate the captured epoch. Queue receipts and notification receipts
remain on the remote host across ownership changes. Slack reply routing is copied
as immutable metadata; incoming replies still pass allowlist and ownership checks.
Tokens, OAuth credentials and provider configuration are not copied between hosts.
A detached Linux process uses its existing local notification settings.

An external notification has a durable in-progress record until its result is
recorded. A timeout or disconnected desktop cannot revoke a possibly sent request.
An uncertain outcome blocks takeover instead of sending again. Preserve that
record for inspection; deleting it or editing epochs is not a safe recovery step.
Completed receipts recover automatically, including a crash during final cleanup.

`linux-release` requests idle release and pauses automatic takeover for that bound
thread. `linux-bind` can explicitly rearm a released, vacant target; `linux-run`
then uses the same ownership protocol. An active owner or unknown writer is a
reason to wait, not to force a takeover. Ordinary explicit bindings without an
automatic control record keep their existing workflow.

Existing runtime paths and compatible cursors are reused. Unknown configuration
keys, previous Slack reply mappings and pre-upgrade delivery receipts are retained.
The protocol uses Python 3.6-compatible helper code so older Linux SSH targets can
continue using the desktop helper without installing another service.

Native acceptance must demonstrate both processes, actual client close/reopen,
same-thread queued work and trusted Stop, restart recovery and idle handback before
this candidate is included in a public release.
