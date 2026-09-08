# Codex WatchDog

<p align="center">
  <strong>English</strong> | <a href="README.zh-CN.md">中文</a> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog and Parrot Dog logo" width="320">
</p>

A lightweight, deterministic watchdog for existing VS Code Codex sessions: it
watches, wakes, relays, and notifies without becoming another AI agent.

## Workflows at a glance

### WatchDog: the durable GitHub loop

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](images/watchdog_workflow_en.png)

### Parrot Dog: the quick Slack relay

![Parrot Dog workflow: Codex asks for help, Slack relays the message, the human replies, and Codex continues](images/parrot_workflow_en.png)

## Design philosophy

- **Lightweight and deterministic.** Small, explicit mechanisms are easier to
  inspect, test, and trust.
- **Human in the loop, with low friction.** You keep control of decisions while
  routine observation and routing stay out of the way.
- **WatchDog observes Git; Codex owns Git.** WatchDog never stages, commits,
  pulls, merges, rebases, resets, checks out, or pushes.
- **GitHub is the durable management and review plane.** Comments, commits, and
  progress reports preserve context across machines and time.
- **Manager-agnostic, Codex-specific.** The management side is intentionally
  replaceable: any human, agent, or automation that can write durable direction
  to GitHub can drive WatchDog. The execution side currently depends on Codex's
  exact-thread queue, hooks, and rollout/completion contracts.
- **Slack is the quick authenticated relay plane.** It is for notifications and
  short allowlisted replies, not durable project history.
- **No extra agent and no unnecessary orchestration.** WatchDog routes evidence
  and instructions to the exact existing Codex thread; Codex still does the
  reasoning and work.

## Platform status

| Platform/path | Support level |
| --- | --- |
| Windows x64 local desktop | **Stable, full E2E verified, packaged reference** |
| Linux Remote-SSH target | **Real remote path verified** |
| Linux explicit same-thread source owner | **Native E2E verified on Ubuntu ARM64** |
| Linux ARM64 executable package | **Native package acceptance on Ubuntu ARM64; explicit same-thread workflow** |
| Linux x64 executable package | **Hosted native package acceptance; real-user desktop E2E pending** |
| Linux local desktop | **CI-verified preview; native desktop E2E pending** |
| macOS Apple Silicon source workflow | **Native E2E verified; topology limitations remain** |
| macOS 15 ARM64 package | **Developer preview; bounded v0.2.3 real-user E2E verified with a CA workaround** |

Linux and macOS share POSIX locking/storage, standard VS Code paths, native
`code --status`, and Codex executable discovery on Linux and macOS. They remain
foreground previews. Apple Silicon and both Linux architectures have self-contained
ZIPs. Linux also supports the [explicit source workflow](docs/LINUX_SOURCE_WORKFLOW.md)
and the separate Remote-SSH helper path. No background-service
installer is included. Run the privacy-safe read-only audit with `codex-watchdog doctor` or
produce a tester attachment with `codex-watchdog doctor --export report.json`.
See [platform support and diagnostics](docs/PLATFORM_SUPPORT.md) for exact
support meanings and the native validation checklist.

## What it does

- Observes Codex Stop/completion events and can capture the final output for a
  notification.
- Continues or wakes the exact existing Codex thread instead of starting a new
  context.
- Uses read-only Git remote-OID checks as a GitHub update doorbell, then lets
  Codex perform any synchronization.
- Sends Slack notifications with Outlook/SMTP fallback and a local audit trail.
- Discovers eligible local and VS Code Remote-SSH workspaces.
- Optionally relays allowlisted replies from a WatchDog-created Slack thread
  back to Codex (the **Parrot Dog** path).
- Enforces a zero-Git-mutation boundary in every WatchDog locality.

## Quick Start

**Ultra-easy setup:** ask your local Codex to scan this repository and guide you through installation and startup step by step.

### Windows x64 beta

1. Download `codex-watchdog-vX.Y.Z-windows-x64.zip` and
   `SHA256SUMS.txt` from [GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases),
   verify the checksum, and extract the complete ZIP. Python is not required.
2. If you will install native hooks, use a permanent extraction path without
   spaces. Git, VS Code with Codex, Codex CLI, and Windows OpenSSH remain
   external prerequisites.
3. Double-click `codex-watchdog.exe`. It creates or reuses a versioned
   current-user launcher profile and starts the foreground monitor. Press
   Ctrl-C or close its console window to stop it.
4. PowerShell remains available for inspection and advanced options:

   ```powershell
   .\codex-watchdog.exe --version
   .\watchdog.ps1 -DryRun
   ```

5. Render, review, and conservatively install the native Codex hooks:

   ```powershell
   .\codex-watchdog.exe install-user-hooks
   .\codex-watchdog.exe install-user-hooks --install
   ```

   If another `hooks.json` already exists, the installer refuses to overwrite
   it; follow the detailed setup guide to merge it manually. In Codex, open
   `/hooks`, inspect the exact definitions, and trust them.

   The default Stop grace window is 30 seconds. Longer windows are an explicit
   opt-in; ordinary completion notifications are not delayed for ten minutes.

> [!IMPORTANT]
> An upgrade automatically reuses a compatible launcher profile, the runtime
> referenced by existing WatchDog hooks, or the newest adjacent previous-release
> runtime. It does not copy or re-enter Slack, Outlook, Duo, OAuth, workspace, or
> notification state. Keep the previous release directory until any hooks that
> invoke its executable have been reviewed, replaced, and trusted in Codex.

Notifications, Slack reply relay, Outlook OAuth, Remote-SSH, Duo fallback, and
source installation are opt-in. See the [Windows package guide](WINDOWS_PACKAGE.md)
and [detailed setup and operations](docs/SETUP.md) when you need them.

### macOS Apple Silicon developer preview

Download `codex-watchdog-vX.Y.Z-macos-arm64-preview.zip` from
[GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases), verify
its entry in `SHA256SUMS.txt`, and extract it. Python is included. This preview
targets macOS 15 on Apple Silicon and is ad-hoc signed, not notarized.

Stop any running WatchDog before an upgrade. From the extracted directory:

```sh
./codex-watchdog --version
./codex-watchdog macos-install
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" doctor
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" macos-tls-check
```

The TLS check contacts Slack without credentials or sending a message. From
v0.2.5, the package selects the system CA bundle automatically and preserves
explicit certificate settings. The earlier real-user acceptance used v0.2.3,
one manually registered workspace, and an explicit CA override; native package
checks remain separate from testing the new version on your own Mac.

Existing runtime, routing, and Keychain settings are reused. See the
[Mac package guide](docs/MACOS_PACKAGE.md) for stable hook installation, normal
human trust, foreground Slack operation, upgrades, rollback, and manual testing.

### Linux ARM64 and x64 packages

Download `codex-watchdog-vX.Y.Z-linux-arm64.zip` for `aarch64`, or
`codex-watchdog-vX.Y.Z-linux-x64.zip` for `x86_64`, from
[GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases). Check
`SHA256SUMS.txt` and extract the complete ZIP. From v0.2.6, x64 targets RHEL 8.10
and Ubuntu 22.04 or newer, with a glibc 2.28 baseline. ARM64 retains Ubuntu 22.04
or newer with glibc 2.35. Python, pip, a virtualenv, and a source checkout are not
needed; Git and Codex CLI/VS Code remain external prerequisites.

Release/stop a running WatchDog before an upgrade. From the extracted directory:

```sh
./codex-watchdog --version
./codex-watchdog linux-install
watchdog="${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog"
"$watchdog" doctor
"$watchdog" linux-hooks
```

Installation reuses the existing hook runtime or saved package profile, keeps
user settings and credentials in place, and backs up replaced files. Review the
rendered hooks before `linux-hooks --install`, then trust the changed definitions
in Codex. The stable executable path supports spaces. See the
[Linux package guide](docs/LINUX_PACKAGE.md) for exact-thread binding,
foreground `linux-run`, idle `linux-release`, upgrades, and rollback.

ARM64 packages pass native acceptance on a real Ubuntu ARM64 machine; x64
packages pass hosted native acceptance. These checks include isolated owner and
Stop fixtures. General Linux desktop discovery remains a CI-verified preview;
packaging does not replace real-user hook trust or desktop E2E acceptance.

#### Optional Linux source installation

Source use requires Python 3.9 or newer. From this repository's checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
codex-watchdog --version
```

Follow the [Linux source guide](docs/LINUX_SOURCE_WORKFLOW.md) to bind the exact
existing conversation, review and trust its hooks, and start the foreground
owner with `linux-run`. Use `linux-release` and wait for release before reopening
the same conversation in VS Code. This explicit workflow is native E2E verified
on Ubuntu ARM64. The Remote-SSH helper is a separate execution path and does not
require installing a second WatchDog owner on every remote host.

## Typical workflow

```text
human / manager agent -> GitHub -> WatchDog -> exact Codex thread
                        progress/report <- Codex -> notification

Codex -> Parrot Dog (Slack) -> human -> Parrot Dog -> exact Codex thread
```

A human, ChatGPT, another agent, or automation can leave durable direction on
GitHub. WatchDog notices the change and rings the doorbell for the existing
thread. Codex owns the work and Git operations, writes the progress record, and
WatchDog reports the outcome.

## AI development declaration

This is a **human-led vibe-coding project with extensive AI assistance**:

- **Human maintainer:** product direction, architecture and safety boundaries,
  acceptance decisions, and release responsibility.
- **ChatGPT:** architecture discussion and review, failure analysis, and
  instruction/document drafting.
- **OpenAI Codex:** most implementation, tests, diagnostics, packaging, and
  iterative fixes.

The detailed dogfooding record is public so this collaboration is explicit,
inspectable, and not presented as conventional human-only development.

## More docs

- [Windows package and first-time setup](WINDOWS_PACKAGE.md)
- [Mac package, upgrades, and manual testing](docs/MACOS_PACKAGE.md)
- [Linux ARM64/x64 packages, upgrades, and rollback](docs/LINUX_PACKAGE.md)
- [Linux source installation and same-thread lifecycle](docs/LINUX_SOURCE_WORKFLOW.md)
- [Detailed setup and operations](docs/SETUP.md)
- [Platform support and privacy-safe doctor](docs/PLATFORM_SUPPORT.md)
- [Security policy and operational boundary](SECURITY.md)
- [Architecture decision](doc/architecture.md)
- [Asset provenance](ASSETS.md) and [third-party notices](THIRD_PARTY_NOTICES.md)
- [Implementation plan](doc/codex_watchdog_implementation_plan.md)
- [Historical feasibility probe](doc/probe_report.md)
- [Dogfooding and development history](doc/Progress/)

> [!NOTE]
> Codex WatchDog is an independent community project. It is not affiliated
> with or endorsed by OpenAI, Microsoft, GitHub, Slack, or their affiliates.
