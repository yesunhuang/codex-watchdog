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
threads previously identified by an attached desktop and waits while VS Code
holds their writer. Use `--exclude workspace-name` to exclude a workspace.

The remote Codex home contains one `watchdog-control/<thread>/owner.json` record,
protected by a kernel file lock. `ATTACHED_LOCAL` gives an attached desktop control
priority. `HANDOFF` requests an idle boundary. `DETACHED_REMOTE` permits the Linux
process to resume the same exact existing thread. Every grant advances the epoch.
An expired lease cannot displace a still-held remote writer. Reattachment requests
release; it does not interrupt an active turn or replay an interrupted command.

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
