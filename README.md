# Codex WatchDog

<p align="center">
  <strong>English</strong> | <a href="README.zh-CN.md">中文</a> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog and Parrot Dog logo" width="320">
</p>

**Distributed execution, unified control.**

Codex WatchDog is a lightweight coordination and control fabric for **existing**
VS Code Codex sessions across machines, terminals, and communication surfaces.
It watches, wakes, routes, hands off, relays, and notifies exact existing sessions
without becoming another AI agent or a heavyweight orchestration runtime.

The project started as a watchdog. Its broader value is the workflow around it:
local and remote agents may be distributed, while the human/manager keeps one
coherent control surface through GitHub, Slack, progress reports, and exact-thread
routing.

## Workflows at a glance

### WatchDog: the durable GitHub loop

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](images/watchdog_workflow_en.png)

### Parrot Dog: the quick Slack relay

![Parrot Dog workflow: Codex asks for help, Slack relays the message, the human replies, and Codex continues](images/parrot_workflow_en.png)

## What this project is really optimizing for

- **Extremely lightweight coordination.** No Redis, database, orchestration
  cluster, second agent runtime, or central AI scheduler is required for the core
  workflow.
- **Cross-platform, cross-machine operation.** Windows is the packaged reference;
  Linux Remote-SSH and detached same-thread handoff are real tested paths; macOS
  has a native developer-preview path.
- **Multiple terminals, one workflow.** VS Code, GitHub, Slack, local shells, and
  remote hosts can participate without forcing the user to live in one terminal.
- **Multiple Codex sessions without flattening them into one runtime.** Each agent
  keeps its own native session, context, repository, and execution environment.
  WatchDog routes to the exact thread instead of replacing those sessions.
- **Aggregated human/manager interface.** Durable direction lives in GitHub;
  quick authenticated replies live in Slack; progress reports compress agent state
  back to the manager.
- **Asynchronous but auditable.** Git history, progress reports, queue receipts,
  notification receipts, and exact-thread identity preserve what happened even
  when machines and people are not online at the same time.
- **Mechanism, not policy.** WatchDog does not decide team hierarchy, work-time
  limits, checkpoint rules, or merge authority. Those belong to each project's
  own `AGENTS.md` contract.

## Design philosophy

- **Keep the mechanism dumb.** WatchDog should mostly observe, wake, notify, relay,
  route, and then get out of the way.
- **Preserve native agent ownership.** WatchDog does not create replacement chats
  just to simplify orchestration. Existing Codex sessions stay authoritative.
- **WatchDog observes Git; Codex owns Git.** WatchDog never stages, commits, pulls,
  merges, rebases, resets, checks out, or pushes.
- **GitHub is the durable management plane.** Comments, commits, and progress
  reports survive terminals, machines, restarts, and time zones.
- **Slack is the quick interrupt/relay plane.** It is for notifications and short
  allowlisted replies, not durable project history.
- **Manager-agnostic, Codex-specific.** The management side can be a human,
  ChatGPT, another agent, or automation that writes durable direction. The
  execution side currently relies on Codex's exact-thread queue, hooks, state, and
  completion contracts.
- **Many observers, one actor.** Where local and detached WatchDogs coexist, they
  coordinate ownership rather than racing to perform side effects.
- **Delete machinery before adding machinery.** Prefer files, Git, locks, and
  existing CLIs over inventing another control platform.

## Multi-agent projects: policy stays in `AGENTS.md`

WatchDog itself does **not** assign Codex A/B/C, impose work-hour limits, or decide
who may merge. That would turn a thin control fabric into a project-management
framework.

Instead, this repository includes an optional project-contract template:

**[`examples/AGENTS.multi-agent.md`](examples/AGENTS.multi-agent.md)**

Copy it into a project's root as `AGENTS.md` and customize it. The default example
implements four lightweight coordination rules:

1. **Maximum continuous active time:** 2 hours per Codex session before a mandatory
   checkpoint/report/stop boundary.
2. **Standard progress reports:** every checkpoint writes a dated report with a
   checkpoint number and agent suffix, for example
   `progress_2026_09_10_cpx071_codex_b.md`.
3. **First-come agent-name claiming:** the first agent atomically claims Codex A in
   the `AGENTS.md` registry; later agents claim B, C, and so on. A rejected Git
   push means the claim lost the race; refetch and claim the next slot, never
   force-push over another agent.
4. **Integration authority:** Manager and Codex A may resolve cross-agent conflicts
   and merge by default. Other agents need explicit scoped authorization in a
   durable `## comment`.

This is deliberately just a template. **Policy lives with the project; WatchDog
provides the transport/control mechanism.**

## A typical aggregated workflow

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

A manager can leave durable instructions on GitHub. WatchDog notices the update
and wakes the exact existing thread. The Codex session does the work and owns Git,
then compresses its state into a checkpoint progress report. WatchDog surfaces the
result. If the user answers a WatchDog-created Slack thread, Parrot Dog relays the
allowlisted text back to that exact session.

For multiple agents, the optional `AGENTS.md` template supplies the team contract;
WatchDog does not need to understand or enforce that contract.

## Platform status

| Platform/path | Support level |
| --- | --- |
| Windows x64 local desktop | **Stable, full E2E verified, packaged reference** |
| Linux Remote-SSH target | **Real remote path verified** |
| Linux explicit same-thread owner | **Native E2E verified on Ubuntu ARM64** |
| Linux automatic remote handoff | **Native E2E verified; reattachment may need a VS Code reload** |
| Linux ARM64 package | **Native package acceptance on Ubuntu ARM64** |
| Linux x64 package | **Accepted on Ubuntu and RHEL 8.10 / glibc 2.28** |
| Linux local desktop | **CI-verified preview; native desktop E2E pending** |
| macOS Apple Silicon source workflow | **Native E2E verified with topology limits** |
| macOS 15 ARM64 package | **Developer preview; packaged acceptance exists, new-version device E2E may lag** |

Support levels are intentionally explicit instead of pretending every topology is
equivalent. See [platform support and diagnostics](docs/PLATFORM_SUPPORT.md).

## What WatchDog currently does

- Observes Codex Stop/completion events and captures final output for notifications.
- Wakes or continues the **exact existing Codex thread** instead of creating a new
  context.
- Uses read-only Git remote-OID checks as a GitHub update doorbell, leaving all Git
  mutation to Codex.
- Sends Slack notifications with Outlook/SMTP fallback and a local audit trail.
- Relays allowlisted Slack replies back to the mapped exact thread through
  **Parrot Dog**.
- Discovers local and VS Code Remote-SSH workspaces and keeps session identities
  distinct.
- Supports persistent Linux detached ownership and safe same-thread handoff after
  Remote-SSH detaches.
- Coordinates local-vs-detached authority with fencing so stale owners cannot keep
  acting after handoff.
- Enforces a zero-Git-mutation boundary in every WatchDog locality.

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

For persistent same-thread takeover after Remote-SSH closes, see
[automatic remote handoff](docs/AUTOMATIC_REMOTE_HANDOFF.md). The persistent remote
owner is currently Linux-specific; macOS/Windows remote-owner parity is not yet
claimed.

From v0.2.17, the running Linux WatchDog monitors its enrolled threads and sends
their completion notifications even while VS Code is attached. The laptop
WatchDog provides fallback monitoring when the host WatchDog is unavailable.

From v0.2.18, use `linux-auto-run --repo /absolute/repository/path` to monitor
registered conversations in a workspace, including newly opened threads. Repeat
`--repo` for more repositories. `--thread UUID` deliberately limits monitoring to
that conversation. Exited backends release their stale ownership; recovery keeps
the same thread and preserves any live VS Code writer.

From v0.2.19, Linux can receive Slack replies with
`CODEX_WATCHDOG_SLACK_REPLY_MODE=poll`, the existing bot token/channel, and an
approved-user list. This works independently of the laptop's Slack connection.
Bot token/channel alone enable outgoing notifications only. See the
[reply configuration and limits](docs/AUTOMATIC_REMOTE_HANDOFF.md).

On clusters with shared home directories, keep the WatchDog service and the VS
Code execution workspace on the same chosen node. Shared installation files do
not provide safe automatic roaming of one conversation between nodes; see the
[shared-home limitations](docs/AUTOMATIC_REMOTE_HANDOFF.md).

> [!IMPORTANT]
> Upgrades preserve compatible user state by default. WatchDog reuses existing
> runtime/profile/provider settings where compatible and does not treat
> reconfiguration as a normal upgrade step. Keep the previous release until any
> changed hook executable has been reviewed and trusted.

## AI development declaration

This is a **human-led vibe-coding project with extensive AI assistance**:

- **Human maintainer:** product direction, architecture and safety boundaries,
  acceptance decisions, and release responsibility.
- **ChatGPT:** architecture discussion and review, failure analysis, and
  instruction/document drafting.
- **OpenAI Codex:** most implementation, tests, diagnostics, packaging, and
  iterative fixes.

The dogfooding history is intentionally inspectable. Real failures are preserved
because the project is developed by continuously using it on its own workflow.

## More docs

- [Multi-agent project-contract example](examples/AGENTS.multi-agent.md)
- [Windows package and first-time setup](WINDOWS_PACKAGE.md)
- [Mac package, upgrades, and manual testing](docs/MACOS_PACKAGE.md)
- [Linux ARM64/x64 packages, upgrades, and rollback](docs/LINUX_PACKAGE.md)
- [Linux source installation and same-thread lifecycle](docs/LINUX_SOURCE_WORKFLOW.md)
- [Automatic remote handoff, persistent startup, and safe reattachment](docs/AUTOMATIC_REMOTE_HANDOFF.md)
- [Detailed setup and operations](docs/SETUP.md)
- [Platform support and privacy-safe doctor](docs/PLATFORM_SUPPORT.md)
- [Security policy and operational boundary](SECURITY.md)
- [Architecture decision](doc/architecture.md)
- [Asset provenance](ASSETS.md) and [third-party notices](THIRD_PARTY_NOTICES.md)
- [Implementation plan](doc/codex_watchdog_implementation_plan.md)
- [Historical feasibility probe](doc/probe_report.md)
- [Dogfooding and development history](doc/Progress/)

> [!NOTE]
> Codex WatchDog is an independent community project. It is not affiliated with or
> endorsed by OpenAI, Microsoft, GitHub, Slack, or their affiliates.
