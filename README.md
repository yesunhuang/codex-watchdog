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
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/macOS-Apple%20Silicon-44627e" alt="macOS Apple Silicon support"></a>
  <br>
  <a href="#architecture-and-philosophy"><img src="https://img.shields.io/badge/sessions-native-355c7d" alt="Existing native Codex sessions"></a>
  <a href="#multi-user-collaboration-bring-your-own-agents"><img src="https://img.shields.io/badge/collaboration-multi--user%20%2F%20multi--agent-355c7d" alt="Multi-user and multi-agent collaboration"></a>
  <a href="#multi-user-collaboration-bring-your-own-agents"><img src="https://img.shields.io/badge/central%20server-not%20required-355c7d" alt="No required central server"></a>
  <br>
  <a href="docs/SETUP.md"><img src="https://img.shields.io/badge/supports-Slack-4A154B" alt="Slack support"></a>
  <a href="docs/FEISHU_LARK.md"><img src="https://img.shields.io/badge/supports-Feishu-3370FF" alt="Feishu support"></a>
  <a href="docs/FEISHU_LARK.md"><img src="https://img.shields.io/badge/supports-Lark-00B96B" alt="Lark support"></a>
  <a href="docs/ONEBOT_QQ.md"><img src="https://img.shields.io/badge/supports-OneBot%2011-2679b5" alt="OneBot 11 support"></a>
  <a href="docs/ONEBOT_QQ.md"><img src="https://img.shields.io/badge/tested-QQ%20via%20NapCat-12b886" alt="QQ via NapCat tested"></a>
  <a href="docs/SETUP.md"><img src="https://img.shields.io/badge/supports-SMTP-6c757d" alt="SMTP support"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> · <a href="#messaging-transports">Messaging</a> · <a href="#architecture-and-philosophy">Architecture</a> · <a href="https://github.com/yesunhuang/codex-watchdog/releases">Releases</a>
</p>

Codex WatchDog is a lightweight coordination and control fabric for **existing**
VS Code Codex sessions. It connects people, agents, machines, and communication
surfaces without asking a team to move into another agent platform, runtime,
dashboard, database, scheduler, or mandatory central manager.

The core idea is simple: mature tools already solve their own jobs well. GitHub is
good at durable collaborative state and audit history. Slack, Feishu/Lark, and QQ
are good communication surfaces. SSH is good at reaching remote machines. VS Code
is already the developer workspace. Codex already owns the conversation and
execution context. **WatchDog does not rebuild smaller copies of them; it connects
the missing edges.**

Each teammate can keep their own machines, credentials, native Codex sessions, and
preferred management style. Selected agents can join shared GitHub and messaging
surfaces; where project policy and configured reply permissions allow it, another
teammate or manager can address that exact existing agent. Managers may be human
or AI, centralized or distributed. No single manager or WatchDog server is
required.

## Architecture and philosophy

- **WatchDog observes; Codex acts.** WatchDog uses read-only Git checks to detect
  updates. Codex reads instructions and owns Git changes, including commits,
  pulls, and merges. WatchDog is not another AI agent.
- **GitHub is the durable control plane; messaging transports are quick control
  surfaces.** Parrot Dog relays allowlisted replies from Slack, Feishu/Lark, or
  OneBot 11 back to the exact existing Codex conversation.
- **OneBot is a transport boundary, not a QQ reimplementation.** WatchDog speaks
  generic OneBot 11 over an authenticated forward WebSocket. The external backend
  owns platform-specific login and protocol maintenance. **NapCat is the tested
  QQ reference backend; WatchDog does not bundle NapCat or a QQ runtime.**
- **One execution owner per conversation.** Ambiguous ownership, destination, or
  delivery blocks action rather than guessing.
- **Keep coordination lightweight.** Reuse native sessions, files, existing
  tools, and mature protocol adapters. Team roles and review rules belong in the
  project's `AGENTS.md`.

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](images/watchdog_workflow_en.png)

![Parrot Dog workflow: Codex asks for help, a messaging surface relays the message, the human replies, and Codex continues](images/parrot_workflow_en.png)

> The diagrams use Slack as one example messaging surface. The same exact-thread
> relay model is used by Feishu/Lark and OneBot 11.

### Single-user, multi-agent workflow

```text
                         Human / Manager
                               |
                  aggregated control surface
                               |
                 +-------------+--------------------------+
                 |                                        |
              GitHub                             Messaging transports
        durable direction/reports        Slack / Feishu / Lark / OneBot 11
                 |                                        |
                 +-------------------+--------------------+
                                     |
                                 WatchDog
                       observe / wake / route / relay
                         notify / handoff / fencing
                                     |
           +-------------------------+-------------------------+
           |                         |                         |
     Codex A (local)          Codex B (Remote-SSH)       Codex C (detached)
      native session             native session             native session
           |                         |                         |
           +------------- checkpoint progress reports -------+
                                     |
                                   GitHub
```

Codex owns Git and writes progress reports; WatchDog detects updates, wakes the
exact thread, and sends notifications. Parrot Dog routes authorized replies from
the configured messaging transport back to that thread.

## Multi-user collaboration: bring your own agents

WatchDog does not require a team to register every machine and agent under one
central runtime. Each person can run their own WatchDog on their own machines and
connect selected agents to collaboration surfaces the team already shares.

```text
        Alice's machines                         Bob's machines
   +----------------------+                 +----------------------+
   | Windows / Codex A1   |                 | Linux / Codex B1     |
   | HPC / Codex A2       |                 | macOS / Codex B2     |
   +----------+-----------+                 +-----------+----------+
              |                                         |
          Alice's dogs                              Bob's dogs
              |                                         |
              +-------- GitHub + messaging surfaces ----+
                              shared team control
```

Notifications include the machine identity so teammates can tell which locality
produced a message. A dog only appears on a shared communication surface if its
owner configures it there. Where project rules and the reply allowlist permit it,
a teammate can reply and have the message routed back to the **exact existing
Codex session** on the owner's machine.

GitHub branches, repository permissions, and `AGENTS.md` can define **who should
be allowed to do what**. WatchDog supplies the separate mechanism: **find the
right machine/thread and deliver the message safely**. Policy remains outside the
transport layer.

## Messaging transports

| Transport | Role | Current support |
| --- | --- | --- |
| Slack | Notification + exact-thread reply relay | Supported and production-tested |
| Feishu / Lark | Notification + exact-thread plain-text reply relay | Supported and production-tested |
| OneBot 11 | Generic authenticated forward-WebSocket transport | Supported in v1.1.0 |
| QQ via NapCat | Reference OneBot 11 backend | Real Windows acceptance passed with NapCat 4.18.28 and official QQ quoted replies |
| Other OneBot 11 backends | Protocol-compatible path | Best effort until individually live-tested |
| SMTP | Outbound notification fallback | Supported |

For OneBot 11, WatchDog owns only the thin transport and exact-thread routing
logic. The external backend owns the chat platform. For QQ, the recommended and
tested path is:

```text
Codex existing thread
        |
     WatchDog
        |
   OneBot 11 WebSocket
        |
      NapCat
        |
        QQ
```

WatchDog never guesses a reply destination. Official QQ quoted replies passed the
live acceptance path. TIM 3.4.5 did not preserve a usable quoted-message identity
in the tested backend and is therefore not accepted for control replies. See
[OneBot 11 / QQ](docs/ONEBOT_QQ.md) for setup, security boundaries, and exact
acceptance limits.

## Supported topologies

| Platform | Recommended workflow | Status and limits |
| --- | --- | --- |
| Windows x64 | Local VS Code and desktop control of Linux Remote-SSH targets | Stable desktop reference; native E2E, package upgrades, icon, and OneBot/QQ acceptance verified |
| Linux ARM64 / x64 | Native server execution, Remote-SSH, and detached continuation | Native server/detached tests and user acceptance passed; local desktop remains best-effort |
| macOS Apple Silicon | Native desktop | Stable native desktop release; package install/upgrade/rollback and messaging acceptance passed; Remote-SSH/handoff scope remains limited |

**Detached execution ownership is currently implemented only on Linux. Native
Windows and macOS detached ownership is not yet implemented.** Linux packages
cover ARM64 Ubuntu and x64 Ubuntu/RHEL 8; see
[platform support](docs/PLATFORM_SUPPORT.md) for exact requirements and acceptance
limits. VS Code may require Retry or a window reload after handback.

## Quick Start

**Ultra-easy setup:** ask your local Codex to scan this repository and guide you
through installation and startup step by step.

On a pristine interactive first launch, WatchDog can configure **Slack**,
**Feishu/Lark**, **Both**, **OneBot (QQ)**, or **Skip**. Existing settings and
upgrades preserve the saved profile. For manual setup, run
`codex-watchdog setup-messaging`; provider-specific instructions are in
[messaging setup](docs/MESSAGING_SETUP.md), [Feishu/Lark](docs/FEISHU_LARK.md),
and [OneBot 11 / QQ](docs/ONEBOT_QQ.md).

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

### macOS Apple Silicon

Download the latest macOS ARM64 ZIP from Releases, verify `SHA256SUMS.txt`, extract
it, and double-click `Install and Start Codex WatchDog.command` (or run it from
Terminal). The installer places the executable in a stable per-user location,
sets up WatchDog, and keeps hook trust under your control.

See the [Mac package guide](docs/MACOS_PACKAGE.md) for hook trust, messaging,
upgrades, rollback, signing/notarization status, and current platform limits.

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
profiles, runtimes, and provider settings. See
[Linux packages](docs/LINUX_PACKAGE.md) for installation and rollback.

### OneBot 11 / QQ quick path

1. Run an external OneBot 11 backend. For QQ, use the tested reference backend
   NapCat and enable an authenticated forward WebSocket.
2. Run `codex-watchdog setup-messaging --onebot` and enter the WebSocket endpoint
   and token at the hidden prompt.
3. Send the displayed `PAIR_CODEX_ONEBOT_...` code to the bot in the intended
   direct chat or group. WatchDog learns the bot, conversation, and authorized
   human automatically.
4. Run `codex-watchdog onebot-check --connect`.
5. Quote/reply to a WatchDog notification to address the exact mapped Codex
   thread. Unknown or ambiguous quotations are rejected.

WatchDog does not install, bundle, or manage NapCat or QQ itself.

## Current usage

On a Linux server, monitor enrolled conversations in an approved repository:

```sh
"$watchdog" linux-auto-run --repo /absolute/repository/path \
  --interval 30 --renew-lease --continue-interrupted
```

Repeat `--repo` for more repositories, or add `--thread UUID` to restrict
monitoring to one existing conversation. These filters do not create or enroll a
conversation.

For Remote-SSH/detached use, run WatchDog under a persistent user service
independent of the SSH connection. Monitoring continues while idle; the detached
writer is released when safe and reacquired for new work so VS Code can reopen
the same thread. See [handoff and startup](docs/AUTOMATIC_REMOTE_HANDOFF.md).

On shared-home clusters, run **one node-local WatchDog per eligible login node**.
Each node discovers its native threads and maintains separate volatile runtime
state. Conversations do not migrate automatically. See
[node setup](docs/LINUX_NODE_SETUP.md).

Interactive transport selection supports `slack`, `lark`, `both`, `onebot`,
`slack+onebot`, `lark+onebot`, and `all`. Existing explicit selections remain
stable across compatible upgrades; see the provider guides before changing a
production service environment.

## Documentation

- Setup: [Windows](WINDOWS_PACKAGE.md), [macOS](docs/MACOS_PACKAGE.md),
  [Linux](docs/LINUX_PACKAGE.md), [provider configuration](docs/SETUP.md).
- Messaging: [Feishu/Lark](docs/FEISHU_LARK.md),
  [OneBot 11 / QQ](docs/ONEBOT_QQ.md), and [OneBot reuse audit](docs/ONEBOT_REUSE.md).
- Linux: [handoff](docs/AUTOMATIC_REMOTE_HANDOFF.md),
  [login nodes](docs/LINUX_NODE_SETUP.md), [source workflow](docs/LINUX_SOURCE_WORKFLOW.md).
- [Platform status and doctor](docs/PLATFORM_SUPPORT.md).
- [Architecture](doc/architecture.md) and [security boundaries](SECURITY.md).
- [Builds and releases](docs/MANUAL_RELEASE.md) and
  [release history](https://github.com/yesunhuang/codex-watchdog/releases).
- [Optional multi-agent project contract](examples/AGENTS.multi-agent.md).
- [Asset provenance](ASSETS.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
- [Development and dogfooding history](doc/Progress/).

This is a human-led project with extensive AI assistance. The maintainer owns
product direction, acceptance, and releases; ChatGPT supports design and review;
OpenAI Codex performs much of the implementation, testing, and packaging.

Codex WatchDog is an independent community project, unaffiliated with OpenAI,
Microsoft, GitHub, Slack, ByteDance, Tencent, NapCat, or their affiliates.
