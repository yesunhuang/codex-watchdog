# One WatchDog per Linux login node

On a cluster with a shared home, run one persistent WatchDog on each login node
that hosts VS Code/Codex. Every dog enrolls only threads whose native writer lock
and VS Code process are visible on its own node. Shared session history and VS
Code logs alone are insufficient. There is no transparent active conversation
migration. Do not intentionally work in the same
repository on several login nodes at once.

The opt-in layout is `$CODEX_HOME/watchdog-nodes/<native-hostname>/` (with
`~/.codex` as the usual Codex home). It contains a schema-1 `node.json`, the
controller runtime, ownership/binding records, per-thread runtimes, wake receipts,
health records and Slack mappings/cursors. Codex's own databases and session files
remain in their original Codex home. Native temporary files and writer locks must
be isolated per node when the filesystem's `flock` is host-local; a shared path
does not prove cross-node exclusion. A reboot requires fresh
local native writer evidence before automatic control can resume a saved thread.

Once a detached conversation finishes and releases its idle writer, a transcript
change or queue item from another node must not reopen it. Node mode waits for a
fresh local VS Code writer or an actually queued message with this node's own
accepted courier receipt. Its Slack mappings remain available. Git observation
continues while parked on the verified node, using a schema-1 receipt in
`$CODEX_HOME/watchdog-observers/` and a cross-node POSIX record lock. The filesystem
must support that lock across participating nodes. Another node requires fresh
native writer evidence to take observation after prior work is idle and no wake
is pending. An old parked node cannot observe foreign history or send a competing
Git wake. These checks survive controller restart. On upgrade, one unambiguous
same-boot registration can be reused; multiple old registrations require fresh
native attachment. The ordinary single-host mode retains its existing idle
monitoring behavior.

Unconfigured nodes retain their previous layout. Installation bytes and a private
notification environment file may be shared; volatile state must not be shared.
The generated unit forces Slack **poll mode**, which reads only that node's own
mapped parents. It cannot consume a different dog's Socket Mode events. Provider
credentials are referenced in place, never copied into the unit or package.

## Install on a new node

Use the current matching release on all participating desktop controllers and
Linux nodes before opting into this layout. Older helpers do not understand the
namespace marker. First install the Linux package in a stable location as
described in [Linux packages](LINUX_PACKAGE.md). Then run the following **on each
native login node**, substituting the installed executable and existing private
notification environment file:

```sh
wd="$HOME/.local/share/codex-watchdog/bin/codex-watchdog"
wd_env="$HOME/.local/share/codex-watchdog/linux-notifications.env"

"$wd" linux-node-install --environment-file "$wd_env"
"$wd" linux-node-install --environment-file "$wd_env" --install

systemctl --user daemon-reload
wd_unit="codex-watchdog-node-$(hostname).service"
systemctl --user enable --now "$wd_unit"
systemctl --user show "$wd_unit" --no-pager \
  -p ActiveState -p SubState -p MainPID -p NRestarts
journalctl --user -u "$wd_unit" -n 10 --no-pager
```

The environment file must belong to the current user with no group/other access
(normally mode 600). For replies, retain the bot token, channel and approved-user
list described in [Slack polling](AUTOMATIC_REMOTE_HANDOFF.md). The default
WatchDog interval is 30 seconds. Preview shows the exact unit before installation;
repeating an identical install preserves existing state. A differing existing
unit is reported for review instead of being overwritten.

Every generated unit has `ConditionHost=<native-hostname>`. Enabling it in a
shared home therefore starts it only on its intended node. Check the user manager
and `loginctl show-user "$(id -un)" -p Linger` on every node. Persistent operation
after logout requires user lingering or a site-provided equivalent; cluster policy
may require the administrator to enable it. Shared files do not prove that a
service is running on another node.

Install/review native hooks with `linux-hooks --install` and trust the exact
definitions in Codex. Existing trusted commands that invoke the upgraded binary
can keep their old runtime argument: the new hook implementation routes to this
node's enrolled thread runtime. A command invoking an older executable must be
upgraded too. The node installer neither rewrites hooks nor fabricates trust.

## Existing nodes and operational limits

Ordinary package upgrades preserve the previous profile and runtime; they do not
silently opt in. `linux_node_legacy_migration_required` means this node already has
legacy bindings. The installer preserves them and refuses to start a competing
layout. Keep that node's existing service until its native ownership is inspected
and a state-preserving migration can be performed while idle. Do not delete old
bindings, copy another node's runtime or manually reset owner epochs.

Changing login nodes is not a handoff protocol. Finish or explicitly pause the
old node's work before opening work on another node; the new node discovers its
own native process. Replies to old Slack parents remain with their originating
node. To stop a node service, use `systemctl --user stop "$wd_unit"`; it requests
graceful idle release and can wait for active work. Do not force-kill a live
writer merely to make service shutdown faster.
