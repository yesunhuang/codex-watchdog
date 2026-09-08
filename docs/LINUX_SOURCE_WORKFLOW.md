# Linux source preview: exact-thread detach and resume

This workflow uses one Linux-local WatchDog runtime and the existing first-party
Codex App Server. It resumes the same saved VS Code user thread after its writer
exits. Closing VS Code can terminate an in-flight command; that command is not
automatically replayed. Reopen the original conversation after releasing the
Linux writer.

The source owner has passed native trusted Stop, detach, queued reply, restart,
context recovery, deduplication, idle release, and real VS Code reattachment on
Ubuntu ARM64 with the extension's Codex 0.153.0. This explicit-binding source
workflow is native E2E verified. It does not establish automatic Linux
desktop/Remote-SSH discovery, uninterrupted in-flight execution, or a packaged
Linux release.

## Source installation and binding

Install the source in a stable current-user directory and virtual environment.
Use the same current-user Codex home as the remote VS Code extension. Existing
provider configuration and credentials stay in place. This workflow does not
install or approve hooks.

The generic doctor checks desktop VS Code locations. For a Remote-SSH-server-only
host, use `doctor --linux-bound` after creating the binding below. This explicit
mode audits the bound workspace/thread and current kernel writer/controller
locks instead of claiming desktop or automatic remote discovery. Missing or
changed bindings fail; inactive bindings and absent processes remain visible.
Credential and launcher limitations can still produce `PARTIAL` overall.

With the exact conversation selected in VS Code, record its thread UUID and
canonical repository path. Choose one stable runtime, then explicitly bind:

```sh
codex-watchdog --runtime "$runtime" --codex-home "$codex_dir" linux-bind \
  --workspace project --repo "$repository" --thread "$thread_id"
codex-watchdog --runtime "$runtime" --codex-home "$codex_dir" doctor --linux-bound
```

The binding validates the exact saved VS Code user thread and cwd. It never
chooses the newest conversation. A reservation binds thread, workspace, Codex
home, runtime, Linux machine identity, and current user. The default lease is
six hours; `--lease-seconds` accepts 60 through 86400 seconds. Repeating an
active bind is idempotent and does not extend its lease. A released or expired
binding can be explicitly armed again when its owner has exited. A different
thread or runtime is refused instead of overwriting the original binding.

Bindings have schema version 1. Private mode-0600 state lives under the runtime's
`linux/` directory and the Codex home's `watchdog-linux/` directory. Unknown
compatible reservation keys survive updates. Journals and existing settings
are not reset during binding, release, or source replacement.

## Foreground owner and current-user persistence

```sh
codex-watchdog --runtime "$runtime" --codex-home "$codex_dir" linux-run
```

The owner waits while the exact writer belongs to a VS Code extension host.
It does not send or resume during that wait. After the lock becomes vacant it
initializes a first-party stdio App Server, reads the exact ID/cwd, resumes only
that ID, and verifies the child owns the kernel writer lock. A competing or
unreadable writer fails closed. No fork, new thread, substitute history, model,
permission profile, or replacement prompt is supplied.

Run that same command under a small current-user persistence primitive when
closing SSH/VS Code. For a source venv and a host with `systemd --user`:

```sh
systemd-run --user --unit=watchdog-project --collect \
  "$venv/bin/codex-watchdog" --runtime "$runtime" --codex-home "$codex_dir" linux-run
```

Use one unit per runtime and no automatic restart policy for a blocked owner.
No root changes or network listener are needed. If executable discovery is
unavailable, pass `linux-run --codex-executable "$codex_executable"` with the
verified installed first-party binary.

The owner composes the existing Git/Stop/queue service for this one workspace.
Git remains read-only. Existing notification settings apply. Native hooks still
require their normal human trust and must point at this same stable runtime.
The unattended stdio client declines command/file approval requests and rejects
unsupported interactive requests; it never grants consent or answers for the
user. `approval_required` remains visible in status for later attended recovery.

In the installed VS Code Codex UI, use **Settings > Hooks > Reload hooks**, then
review and **Trust** each exact WatchDog definition. Installation alone does not
establish trust. The CLI's `/hooks` surface is a separate attended alternative.

The runtime foreground lock and per-thread owner lock prevent competing owners.
The reservation fences new WatchDog queue sends from other runtimes and the
Remote-SSH helper. It does not control direct first-party Codex clients. Existing
queue receipts can always be passively reconciled; uncertain/dispatching sends
are never retried. Normal queue commands targeting this runtime remain available.

## Restart and reattachment

Inspect privacy-safe state with:

```sh
codex-watchdog --runtime "$runtime" --codex-home "$codex_dir" linux-status
```

`last_observation` is persisted evidence, not a live process claim. On restart,
the owner checks the same binding, locality, exact thread row, and kernel lock.
It reuses queue/service journals. An absent or changed thread, malformed binding,
unsupported schema, conflicting writer, or failed resume blocks control without
retargeting or reconnect retry.

Before reopening the original VS Code conversation, request release:

```sh
codex-watchdog --runtime "$runtime" --codex-home "$codex_dir" linux-release
```

Release blocks new sends and waits for first-party idle status and an empty
queue before closing the owned stdio child. A SIGINT/SIGTERM or lease expiry
requests the same idle release. A turn already running may finish after lease
expiry; expiry does not forcibly interrupt it. If the owner is not running,
run `linux-run` once to reconcile a pending release without resuming the thread.
Wait for `state: released`, then reopen the same conversation in VS Code.

Do not force-stop the user unit to perform ordinary handback: systemd can kill
the in-flight Codex child. Source upgrades should similarly release first,
replace only application/venv files, and reuse the original runtime, Codex home,
credentials, hooks, binding, and queue journals.
