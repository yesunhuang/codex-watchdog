# Recovery when an opted-in Linux node service disappears

An existing trusted native `Stop` or `PermissionRequest` hook can recover a
previously configured node service when the user systemd manager has lost its
unit search path. This is recovery on renewed native use. It does not guarantee
unattended startup before the first genuine native callback after reboot.

The hook must execute the current installed WatchDog package. Upgrading only a
separate service image leaves an older hook executable unchanged. Use the normal
compatible installation path, keep the existing trusted command and runtime,
and verify both the hook-selected executable and the service image. On shared
homes, first establish a safe boundary for every affected user of the shared
executable; a per-node service override alone cannot update a shared hook.

## Prerequisites and limits

- An existing opted-in node installed with `linux-node-install`, its original
  enabled host-conditioned user unit, and its retained private environment.
- The exact native thread, repository, first-party Linux writer generation and
  callback ancestry must all be independently verified.
- Existing `/usr/bin/systemctl` and `/usr/bin/busctl` must be available. No new
  daemon, network listener, privileged service or package installation is added.
- The saved image/profile hash, canonical unit and supported drop-ins must match.
  Unknown directives, mismatched images or ambiguous effective commands refuse
  recovery.

Only a missing, inactive unit with no running process or pending job is eligible.
The helper may reload the user manager, verify the resulting unit and start that
original service once. It validates the typed D-Bus `ExecStart` executable and
complete argument array, including never-started command metadata, and repeats
the checks immediately before activation. Active services are left alone;
loaded services deliberately stopped or failed are not restarted.

The helper shares the per-node installation lock. A competing controller prevents
activation. Work is bounded to five seconds, limited command output and file
reads, at most 64 directory entries and 32 drop-in files. A single versioned
`service-bootstrap.json` receipt records the result. A later genuine callback can
finish a verified same-boot reload that never started the service; it does not
replay an earlier command or provider effect.

Recovery does not adopt an owner epoch, replace a conversation, dispatch a model
instruction, change grants or rewrite queues. The original hook-owner guard runs
after this optional step even if recovery fails. Existing settings, credentials,
routes and journals are reused without a schema migration.

For service setup and ordinary release/reattachment, see
[node setup](LINUX_NODE_SETUP.md) and [source workflow](LINUX_SOURCE_WORKFLOW.md).
For coverage limits and diagnostics, see [platform support](PLATFORM_SUPPORT.md).
