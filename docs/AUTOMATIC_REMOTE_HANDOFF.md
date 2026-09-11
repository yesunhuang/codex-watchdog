# Automatic remote handoff

Version 0.2.7 adds automatic handoff alongside the explicit Linux binding workflow.
Upgrade the desktop
WatchDog, Linux WatchDog and the code behind the existing trusted Stop command
together before enabling automatic handoff. Existing hook trust is reused only
when the command remains unchanged; WatchDog never fabricates hook approval.

Run the Linux process independently of an SSH terminal. On a host whose user
services remain running after logout, the installed package can use systemd:

```sh
watchdog="${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog"
systemd-run --user --unit=codex-watchdog-auto \
  --property=KillMode=mixed --property=TimeoutStopSec=infinity \
  "$watchdog" --runtime /absolute/existing/runtime linux-auto-run --interval 5
```

Use the existing absolute runtime path and the same current-user Codex home.
Check `systemctl --user status codex-watchdog-auto.service`. Hosts without
persistent user services can use their existing user process supervisor; a
foreground process in an SSH terminal is not sufficient. No service installer
or system-wide configuration change is performed by WatchDog. If executable
discovery is unavailable, add `--codex-executable /absolute/path/to/codex`.

The equivalent source entry point is:

```sh
python -m codex_watchdog --runtime /absolute/existing/runtime linux-auto-run --interval 5
```

The desktop's existing Remote-SSH observer automatically identifies eligible
threads. No bind command is needed for this path. The Linux process only considers
threads previously identified by an attached desktop. From v0.2.17, a running
Linux WatchDog monitors those threads and sends their completion notifications
using its local settings even while VS Code holds the native writer. The desktop
WatchDog is the fallback when the host observer's lease expires or it releases
ownership. Use `--exclude workspace-name` to exclude a workspace, or repeat
`--thread UUID` to restrict automatic mode to specific existing threads.

The remote Codex home contains one `watchdog-control/<thread>/owner.json` record,
protected by a kernel file lock. `ATTACHED_LOCAL` records desktop fallback;
`DETACHED_REMOTE` records the host observer. These legacy names describe WatchDog
authority, not which process currently owns Codex execution. A host observer
reports `observing` when VS Code owns execution and `owned` when it owns the
detached writer. `HANDOFF` requests an idle boundary to return only that writer
to VS Code; the host continues checking completions while waiting for attachment.
Every ownership grant advances the epoch. An unresolved external send blocks
ownership changes, and an expired lease cannot displace a still-held remote
writer. Older desktop helpers remain compatible with host observation priority.
No active turn is interrupted or command replayed merely to transfer ownership.

If VS Code starts resuming while Linux is still releasing, its first resume
request can remain pending after control returns. In that workspace, run
**Developer: Reload Window** and reopen the same existing conversation. The
native test required this retry; a control-owner record alone is not proof that
the VS Code execution writer has resumed.

Queue delivery, trusted Stop consumption, writer claims, cursor updates and
notifications validate the captured epoch. Queue receipts and notification receipts
remain on the remote host across ownership changes. Slack reply routing is copied
as immutable metadata; incoming replies still pass allowlist and ownership checks.
Tokens, OAuth credentials and provider configuration are not copied between hosts.
A detached Linux process uses its existing local notification settings.

For a persistent service bound with `linux-bind`, use
`linux-run --interval 30 --renew-lease`. Without this opt-in, the explicit binding
still expires after its configured lease (at most 24 hours). Renewal extends only
an armed, unexpired binding while its controller verifies the existing writer or
waits behind VS Code. It preserves the same thread and all saved settings.
`linux-release`, SIGTERM and expired reservations are never rearmed by renewal.
Use a persistent user service with `Restart=no`, graceful shutdown and logout
survival enabled; a failure must remain visible instead of forcing another resume.
Stopping the user service requests idle release. Disable it as well to cancel
automatic startup. Hosts with shared home directories should pin the unit to the
verified execution host, since shared files do not imply shared process ownership.

From v0.2.16, add **`--continue-interrupted`** to `linux-run` or `linux-auto-run`
to continue interrupted work automatically after Linux acquires the same thread.
This is opt-in; upgrades preserve the previous behavior unless the option is
enabled. A manual VS Code window close can terminate its extension-owned Codex
backend. WatchDog resumes the stored conversation and starts a continuation turn;
it cannot keep the terminated process or its in-flight command alive.

The controller reads only the latest native turn summary. It sends a fixed
continuation instruction once for an interrupted turn after verifying its exact
writer, live idle status, active lease and empty queue. It waits while VS Code
owns the writer and skips active turns, pending approvals and release requests.
The instruction tells Codex to inspect uncertain outcomes, preserve the existing
scope and approvals, and stop if the task is complete or needs user input.

The existing queue journal deduplicates the continuation across restarts. A
notification through the configured Slack/email transport says **automatic
continuation started** only after native rollout evidence confirms that exact
queued instruction started. Enqueue acknowledgement alone is not success.
Uncertain delivery is reported without replaying it. If that automatic
continuation itself is interrupted, WatchDog reports that it needs attention
instead of generating a retry loop. A later distinct user turn can be continued.

Use `linux-release` for an explicit binding, or stop the persistent service, to
disable takeover. With this option enabled, a recorded interruption followed by
detach authorizes continuation; the controller cannot infer whether a vanished
VS Code backend resulted from a window close or an accidental disconnection.
`linux-run` covers its one bound thread; `linux-auto-run` covers eligible threads
already identified by the desktop observer, subject to its exclusions.
Use repeated `--thread UUID` options to limit an automatic service to selected
existing conversations. To include newly registered conversations in an approved
workspace, use repeated `--repo /absolute/repository/path` options instead.
Repository matching uses the full canonical path, and each thread keeps its own
ownership, runtime and notification receipts. If both filters are supplied, both
must match. Neither option creates a conversation or enrolls an unobserved thread.
Without either filter it retains the existing discovery behavior. Automatic mode
also accepts `--renew-lease` for persistent services.

From v0.2.8, detached-owner loss is reported immediately when the owner detects an App Server
exit, a changed writer, unavailable exact-thread metadata, or an observation
failure. From v0.2.18, automatic mode retires a confirmed exited backend and
releases its stale claim. A previously verified thread can then recover under a
fresh ownership epoch; an existing VS Code writer is preserved. An initial resume
without a verified ownership receipt waits for native attachment or explicit
service restart. Live or uncertain writers and unresolved notification outcomes
remain protected. Failed observation does not renew its lease indefinitely, and
`linux-status` records the failure instead of retaining a stale healthy state.
The explicit foreground owner reports the failure before exiting. Repeated checks
and restarts reuse the same outage identity. Successful monitoring sends one
recovery notification. Normal idle handback, planned release and healthy standby
do not create loss alerts. An expired or replaced owner has no authority to send.

The Linux **service process** must have its own existing notification environment:
`CODEX_WATCHDOG_SLACK_WEBHOOK_URL`, or the Slack relay variables from the setup
guide, and/or `CODEX_WATCHDOG_SMTP_HOST`, `CODEX_WATCHDOG_SMTP_FROM` and
`CODEX_WATCHDOG_SMTP_TO` with the provider's existing TLS/authentication settings.
Slack is preferred; configured SMTP is the fallback. An interactive shell's
variables are not automatically inherited by a systemd user service. Keep these
settings in the host's existing private service configuration; do not paste
credentials into command lines or copy another host's secure store.

Automatic status output includes the notification result for a blocked owner.
The runtime also retains schema-1 `linux/health/*.json` records. `sent` or
`sent_fallback` records delivery; `audit_only` means no external transport was
configured. Failed/uncertain delivery is not reported as successful suppression
and is not blindly retried. A running owner can report lost thread monitoring;
a terminated process or an unreachable host cannot send its own alert. Use the
host's existing service supervision/host monitoring for those outages.


An external notification has a durable in-progress record until its result is
recorded. A timeout or disconnected desktop cannot revoke a possibly sent request.
An uncertain outcome blocks takeover instead of sending again. Preserve that
record for inspection; deleting it or editing epochs is not a safe recovery step.
Completed receipts recover automatically, including a crash during final cleanup.

`linux-release` requests idle release and pauses automatic takeover for that bound
thread. From v0.2.9 it waits up to one second for initial control/sender lock
admission before changing state. Persistent contention returns `linux_release_busy`;
inspect the owner and retry the command. A changed binding or first activation
during the wait fails closed. An operation that already started writing is never
replayed. `linux-bind` can explicitly rearm a released, vacant target; `linux-run`
then uses the same ownership protocol. An active owner or unknown writer is a
reason to wait, not to force a takeover. Ordinary explicit bindings without an
automatic control record keep their existing workflow.

To stop the example service, request `linux-release` with its runtime and wait
for `linux-status` to report `released`, then stop the user service. Keep the
current hook implementation in place while any coordinated owner is active.

Existing runtime paths and compatible cursors are reused. Unknown configuration
keys, previous Slack reply mappings and pre-upgrade delivery receipts are retained.
The protocol uses Python 3.6-compatible helper code so older Linux SSH targets can
continue using the desktop helper without installing another service.

Native source acceptance on Ubuntu ARM64 demonstrated both WatchDogs, actual
client close, autonomous same-thread queue delivery and trusted Stop at 30,004 ms,
idle remote restart, stale-epoch refusal, and desktop handback with the VS Code
retry above. Queue journals and protected settings were unchanged. Crash and busy
handback cases also have deterministic tests; native restart used an idle release.
Packaged fixtures and other desktop/platform topologies remain separate checks.
