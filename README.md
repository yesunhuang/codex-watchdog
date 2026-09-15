<p align="center">
  <strong>English</strong> | <a href="README.zh-CN.md">中文</a> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog and Parrot Dog logo" width="160">
</p>

<h1 align="center">Codex WatchDog</h1>

<p align="center">
  <strong>Bring your own agents. Keep your tools. Work as one distributed team.</strong><br>
  <em>Ultra-lightweight, no-migration multi-user and multi-agent collaboration across machines and platforms.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2e7d32" alt="MIT license"></a>
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/Windows-x64-44627e" alt="Windows x64 support"></a>
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/Linux-ARM64%20%2F%20x64-44627e" alt="Linux ARM64 and x64 support"></a>
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/macOS-preview-a66b00" alt="macOS preview"></a>
  <br>
  <a href="#architecture-and-philosophy"><img src="https://img.shields.io/badge/sessions-native-355c7d" alt="Existing native Codex sessions"></a>
  <a href="#multi-user-collaboration-bring-your-own-agents"><img src="https://img.shields.io/badge/collaboration-multi--user%20%2F%20multi--agent-355c7d" alt="Multi-user and multi-agent collaboration"></a>
  <a href="#multi-user-collaboration-bring-your-own-agents"><img src="https://img.shields.io/badge/central%20server-not%20required-355c7d" alt="No required central server"></a>
  <br>
  <a href="docs/SETUP.md"><img src="https://img.shields.io/badge/supports-Slack-4A154B" alt="Slack support"></a>
  <a href="docs/FEISHU_LARK.md"><img src="https://img.shields.io/badge/supports-Feishu-3370FF" alt="Feishu support"></a>
  <a href="docs/FEISHU_LARK.md"><img src="https://img.shields.io/badge/supports-Lark-00B96B" alt="Lark support"></a>
  <a href="docs/SETUP.md"><img src="https://img.shields.io/badge/supports-SMTP-6c757d" alt="SMTP support"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> · <a href="#architecture-and-philosophy">Architecture</a> · <a href="docs/SETUP.md">Configuration</a> · <a href="https://github.com/yesunhuang/codex-watchdog/releases">Releases</a>
</p>

Codex WatchDog is a lightweight coordination and control fabric for **existing**
VS Code Codex sessions. It connects people, agents, machines, and communication
surfaces without asking a team to move into another agent platform, runtime,
dashboard, database, scheduler, or mandatory central manager.

The core idea is simple: mature tools already solve their own jobs well. GitHub is
good at durable collaborative state and audit history. Slack is good at team
communication. SSH is good at reaching remote machines. VS Code is already the
developer workspace. Codex already owns the conversation and execution context.
**WatchDog does not rebuild smaller copies of them; it connects the missing edges.**

Each teammate can keep their own machines, credentials, native Codex sessions, and
preferred management style. Selected agents can join shared GitHub/Slack surfaces;
where project policy and configured reply permissions allow it, another teammate or
manager can address that exact existing agent. Managers may be human or AI,
centralized or distributed. No single manager or WatchDog server is required.

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

### Single-user, multi-agent workflow

One user can coordinate several existing agents through GitHub and Slack,
with an optional human or AI manager.

```text
                         Human / Manager
                               |
                  aggregated control surface
                               |
                 +-------------+-------------+
                 |                           |
              GitHub                       Slack
        durable direction/reports     quick notify/reply
                 |                           |
                 +-------------+-------------+
                               |
                           WatchDog
                     observe / wake / route
                     relay / notify / handoff
                               |
           +-------------------+-------------------+
           |                   |                   |
     Codex A (local)     Codex B (Remote-SSH)   Codex C (detached)
     native session       native session          native session
           |                   |                   |
           +-------- checkpoint progress reports --+
                               |
                             GitHub
```

Codex owns Git and writes progress reports; WatchDog detects updates, wakes the
exact thread and sends notifications. Parrot Dog relays allowlisted Slack replies
back to that thread. This is one possible arrangement; a central manager or
WatchDog server is not required.

## Multi-user collaboration: bring your own agents

WatchDog does not require a team to register every machine and agent under one
central runtime. Each person can run their own WatchDog on their own machines and
connect selected agents to the collaboration surfaces the team already shares.

### Multi-user, multi-agent workflow

```text
        Alice's machines                         Bob's machines
   +----------------------+                 +----------------------+
   | Windows / Codex A1   |                 | Linux / Codex B1     |
   | HPC / Codex A2       |                 | macOS / Codex B2     |
   +----------+-----------+                 +-----------+----------+
              |                                         |
          Alice's dogs                              Bob's dogs
              |                                         |
              +------------- GitHub + Slack ------------+
                               shared team surfaces
```

Slack notifications include the machine identity so teammates can tell which
locality produced a message. A dog only appears on a shared Slack surface if its
owner configures it there. Where the project's rules and configured reply
allowlist permit it, a teammate can reply to that dog's Slack thread and have the
message routed back to the **exact existing Codex session** on the owner's machine.

GitHub branches, repository permissions, and `AGENTS.md` can define **who should be
allowed to do what**. WatchDog supplies the separate mechanism: **find the right
machine/thread and deliver the message safely**. Policy remains outside the
transport layer.

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

On a pristine interactive first launch, choose **Slack**, **Feishu/Lark**, **Both**,
or **Skip**. Select the bot's group/channel and send the displayed device-specific
confirmation phrase to pair your account. New setups use independent polling for
both providers; other devices can stay running. Existing settings and upgrades suppress
the prompt. For manual setup, run `codex-watchdog setup-messaging`; see
[messaging setup](docs/MESSAGING_SETUP.md) for services and recovery.

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

Feishu and international Lark can also send notifications and relay plain-text
replies to the existing Codex conversation. Set `CODEX_WATCHDOG_INTERACTIVE_TRANSPORT=both`
to use the selected service alongside Slack. Configure the correct region and
allowed users in the [Feishu/Lark setup guide](docs/FEISHU_LARK.md). Replies use
local polling by default, so machines can share the same app and conversation. Packages include
the SDK; existing Slack settings are retained when switching providers.

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
