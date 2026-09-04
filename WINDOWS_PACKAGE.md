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

From PowerShell in the extracted directory:

```powershell
.\codex-watchdog.exe --version
.\watchdog.ps1 -DryRun -NoDuo
.\watchdog.ps1 -NoDuo
```

`watchdog.ps1` restores optional current-user DPAPI configuration and invokes
the adjacent packaged executable. Runtime state defaults to `.codex-watchdog`
beside the launcher and is never part of the release archive.

## Native hook setup

Render the exact hook document before installing it:

```powershell
.\codex-watchdog.exe --runtime "$PWD\.codex-watchdog" install-user-hooks
```

The conservative installer writes only when `CODEX_HOME\hooks.json` is absent
or already identical:

```powershell
.\codex-watchdog.exe --runtime "$PWD\.codex-watchdog" install-user-hooks --install
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
