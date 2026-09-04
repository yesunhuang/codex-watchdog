# Codex WatchDog Windows x64 beta

The versioned Windows ZIP is self-contained: running Codex WatchDog does not
require Python, pip, a virtual environment, or a source checkout.

## External prerequisites

Install these separately and keep them updated:

- Git;
- Visual Studio Code with the Codex extension and Codex CLI;
- Windows OpenSSH for ordinary Remote-SSH workspaces; and
- PuTTY/Plink only when using the optional shared-connection Duo fallback.

They are not bundled in the beta.

## Start

Extract the complete ZIP. If you will install the native Codex hooks, choose a
permanent path without spaces because the current Windows Codex hook runner
requires a quote-free command.

Double-click `codex-watchdog.exe` to start the foreground monitor. The console
stays open while WatchDog is running; press Ctrl-C or close it to stop.

PowerShell remains available for inspection and advanced options:

```powershell
.\codex-watchdog.exe --version
.\watchdog.ps1 -DryRun
.\watchdog.ps1
```

`watchdog.ps1` restores optional current-user DPAPI configuration and invokes
the adjacent packaged executable. A no-argument executable launch performs this
bootstrap automatically; argument-bearing CLI behavior remains available.

The one-click launcher stores a non-secret, schema-versioned runtime pointer at
`%LOCALAPPDATA%\CodexWatchdog\launcher-profile.json`. Resolution is
deterministic: an existing valid profile wins; otherwise WatchDog checks an
explicit `CODEX_WATCHDOG_RUNTIME`, one unique runtime referenced by recognizable
installed WatchDog hooks, an adjacent runtime, and the newest unambiguous
previous-release sibling. A fresh installation defaults to
`%LOCALAPPDATA%\CodexWatchdog\runtime`. Malformed profiles, missing saved
runtimes, and ambiguous hook/sibling results fail closed without overwriting or
creating replacement state.

Current-user Slack DPAPI files, Outlook environment/OAuth state, saved Duo
configuration, notification state, workspace mappings, and audit history stay
in their existing security boundaries. They are not copied into the release
folder or rewritten during startup. A second foreground monitor for the same
runtime is rejected by a process-lifetime lock.

## Upgrade from v0.1.0

1. Stop the old foreground WatchDog, but keep its extracted directory.
2. Extract the new complete ZIP to its own permanent path.
3. Double-click the new `codex-watchdog.exe`. With no prior launcher profile,
   it recovers the old runtime from the installed WatchDog hooks or adjacent
   previous release and records that path atomically.
4. Confirm the existing local/remote workspaces and notification channels. No
   unchanged Slack, Outlook, Duo, OAuth, runtime, or workspace setting should be
   entered again.
5. Existing hooks continue to invoke the old trusted executable. Keep that
   directory until you deliberately render, review, install/merge, and trust
   hooks for the new executable. Codex trust is an external security boundary
   and cannot be transferred or fabricated by WatchDog.

The previous runtime/profile is never deleted or overwritten by this upgrade,
so stopping the new executable and starting the old launcher remains a rollback
path.

## Native hook setup

After one-click startup has created/recovered the launcher profile, render the
exact hook document before installing it. The packaged CLI automatically uses
the same saved runtime when `--runtime` is omitted:

```powershell
.\codex-watchdog.exe install-user-hooks
```

The conservative installer writes only when `CODEX_HOME\hooks.json` is absent
or already identical:

```powershell
.\codex-watchdog.exe install-user-hooks --install
```

It refuses to overwrite or merge a different existing hook file. Review and
merge such a file manually. Then open `/hooks` in Codex, inspect each exact
definition, and trust it manually. Any executable path or hook-definition
change requires renewed trust.

## Credentials and configuration

The archive contains no Slack, Outlook, SSH, Git, Codex, or machine-specific
configuration. Supply notification environment variables at runtime or use the
documented current-user DPAPI setup. `setup-slack-relay.ps1` stores Slack relay
tokens for only the current Windows user. See
[`docs/SETUP.md`](docs/SETUP.md) for Outlook, Slack, remote-workspace, and Duo
configuration.

Verify `SHA256SUMS.txt` after extraction. Dependency versions, declared
licenses, and copied license texts are under `THIRD_PARTY_LICENSES`.
