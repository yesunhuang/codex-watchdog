# Codex WatchDog

<p align="center">
  <strong>English</strong> | <a href="README.zh-CN.md">中文</a> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog and Parrot Dog logo" width="320">
</p>

**Distributed execution, unified control.**

Codex WatchDog connects **existing VS Code Codex conversations** to GitHub and
Slack across local computers and Remote-SSH servers. It watches for updates and
completed work, wakes the right conversation, sends notifications, and relays
approved Slack replies. Each conversation keeps its own context and workspace.

## Architecture and philosophy

- **WatchDog observes; Codex acts.** WatchDog uses read-only Git checks to detect
  updates. Codex reads instructions and owns Git changes, including commits,
  pulls and merges. WatchDog is not another AI agent.
- **GitHub holds durable direction; Slack carries quick replies.** The
  **Parrot Dog** relay returns allowlisted replies from a WatchDog-created Slack
  thread to the exact existing Codex conversation.
- **One execution owner per conversation.** A Linux host's WatchDog takes
  priority for its enrolled threads; the desktop provides fallback when the host
  observer is unavailable. Ambiguous ownership or delivery blocks action.
- **Keep coordination lightweight.** Reuse native sessions, files and existing
  tools. Team roles and review rules belong in the project's `AGENTS.md`.

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](images/watchdog_workflow_en.png)

![Parrot Dog workflow: Codex asks for help, Slack relays the message, the human replies, and Codex continues](images/parrot_workflow_en.png)

## Supported topologies

| Platform | Recommended workflow | Status and limits |
| --- | --- | --- |
| Windows x64 | Local VS Code and desktop control of Linux Remote-SSH targets | Stable desktop reference; native E2E, package upgrades and icon verified |
| Linux ARM64 / x64 | Native server execution, Remote-SSH and detached continuation | Native server/detached tests and user acceptance passed; local desktop remains preview |
| macOS Apple Silicon | Native desktop preview | Bounded native E2E on resolvable window topologies; Remote-SSH/handoff acceptance remains limited |

The detached execution owner is Linux-specific. Native macOS/Windows detached
ownership is not supported. Linux packages cover ARM64 Ubuntu and x64 Ubuntu/RHEL
8; see [platform support](docs/PLATFORM_SUPPORT.md) for exact requirements and
acceptance limits. VS Code may require Retry or a window reload after handback.

## Quick Start

**Ultra-easy setup:** ask your local Codex to scan this repository and guide you
through installation and startup step by step.

### Windows x64

1. Download the latest `codex-watchdog-vX.Y.Z-windows-x64.zip` and
   `SHA256SUMS.txt` from [GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases).
2. Verify the checksum and extract the complete ZIP to a stable path.
3. Double-click `codex-watchdog.exe` to create/reuse the current-user launcher
   profile and start the foreground monitor.
4. For native hooks, render and review them first:

   ```powershell
   .\codex-watchdog.exe install-user-hooks
   .\codex-watchdog.exe install-user-hooks --install
   ```

   Then inspect `/hooks` in Codex and trust the exact definitions yourself.

### macOS Apple Silicon preview

Download the ARM64 preview ZIP from Releases, verify `SHA256SUMS.txt`, extract it,
and run:

```sh
./codex-watchdog --version
./codex-watchdog macos-install
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" doctor
```

See the [Mac package guide](docs/MACOS_PACKAGE.md) for hook trust, Slack operation,
upgrades, rollback, and current preview limits.

### Linux ARM64 / x64

Download the matching Linux ZIP from Releases, verify `SHA256SUMS.txt`, extract it,
and run:

```sh
./codex-watchdog --version
./codex-watchdog linux-install
watchdog="${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog"
"$watchdog" doctor
"$watchdog" linux-hooks
```

Review the Linux hook definitions before running `"$watchdog" linux-hooks --install`,
then trust the exact definitions in Codex. Compatible upgrades preserve existing
profiles, runtimes and provider settings; changed hook commands need review and
trust. See [Linux packages](docs/LINUX_PACKAGE.md) for installation and rollback.

## Current usage

On a Linux server, monitor enrolled conversations in an approved repository:

```sh
"$watchdog" linux-auto-run --repo /absolute/repository/path \
  --interval 30 --renew-lease --continue-interrupted
```

Repeat `--repo` for more repositories, or add `--thread UUID` to restrict monitoring
to one existing conversation. One Git update may wake several enrolled threads
in the same repository. These filters do not create or enroll a conversation.

This command runs in the foreground. For Remote-SSH/detached use, run WatchDog
under a persistent user service independent of the SSH connection. The options
above enable continuation after interruption. Monitoring continues while idle;
the detached writer is released when safe and reacquired for new work, so VS Code
can reopen the same thread. See [handoff and startup](docs/AUTOMATIC_REMOTE_HANDOFF.md).

For Linux Slack replies, use `CODEX_WATCHDOG_SLACK_REPLY_MODE=poll` with the existing
bot token, channel and approved-user list. Replies work without the laptop's Slack
connection. A token and channel alone enable outgoing notifications only; provider
settings and limits are in the handoff guide.

On shared-home clusters, run **one node-local WatchDog per eligible login node**.
Each node discovers its native threads and maintains separate runtime state.
Finish or pause work before switching nodes; conversations do not migrate
automatically. See [node setup](docs/LINUX_NODE_SETUP.md) for persistent startup,
native runtime isolation and existing-installation limits.

## Documentation

- Setup: [Windows](WINDOWS_PACKAGE.md), [macOS](docs/MACOS_PACKAGE.md),
  [Linux](docs/LINUX_PACKAGE.md), [provider configuration](docs/SETUP.md).
- Linux: [handoff](docs/AUTOMATIC_REMOTE_HANDOFF.md), [login nodes](docs/LINUX_NODE_SETUP.md),
  [source workflow](docs/LINUX_SOURCE_WORKFLOW.md).
- [Platform status and doctor](docs/PLATFORM_SUPPORT.md).
- [Architecture](doc/architecture.md) and [security boundaries](SECURITY.md).
- [Manual builds and releases](docs/MANUAL_RELEASE.md) and
  [release history](https://github.com/yesunhuang/codex-watchdog/releases).
- [Optional multi-agent project contract](examples/AGENTS.multi-agent.md).
- [Asset provenance](ASSETS.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
- [Development and dogfooding history](doc/Progress/).

This is a human-led project with extensive AI assistance. The maintainer owns
product direction, acceptance and releases; ChatGPT supports design and review;
OpenAI Codex performs much of the implementation, testing and packaging.

Codex WatchDog is an independent community project, unaffiliated with OpenAI,
Microsoft, GitHub, Slack or their affiliates.
