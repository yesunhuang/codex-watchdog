# Codex WatchDog

<p align="center">
  <strong>English</strong> | <a href="README.zh-CN.md">中文</a> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog and Parrot Dog logo" width="320">
</p>

**Bring your own agents. Keep your tools. Work as one distributed team.**

*Ultra-lightweight, no-migration multi-user and multi-agent collaboration across machines and platforms.*

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

## Workflows at a glance

### WatchDog: the durable GitHub loop

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](images/watchdog_workflow_en.png)

### Parrot Dog: the quick Slack relay

![Parrot Dog workflow: Codex asks for help, Slack relays the message, the human replies, and Codex continues](images/parrot_workflow_en.png)

## What this project is really optimizing for

- **No workflow migration.** Keep the VS Code windows, Codex threads, repositories,
  SSH hosts, GitHub projects, Slack channels, and habits the team already uses.
- **Extremely lightweight coordination.** No Redis, database, orchestration
  cluster, second agent runtime, mandatory central service, or central AI scheduler
  is required for the core workflow.
- **Multi-user by composition.** Different people can keep their own agents and
  machines while participating in the same GitHub/Slack collaboration surfaces.
  Sharing an agent does not require surrendering its native session or host.
- **Cross-platform, cross-machine operation.** Windows is the packaged reference;
  Linux Remote-SSH and detached same-thread handoff are real tested paths; macOS
  has a native developer-preview path.
- **Multiple Codex sessions without flattening them into one runtime.** Each agent
  keeps its own native session, context, repository, and execution environment.
  WatchDog routes to the exact thread instead of replacing those sessions.
- **Managers are optional and distributed.** A human, ChatGPT, another Codex, or
  automation can act as a manager. A team may use one manager, several managers,
  or direct human-to-agent control without changing the transport layer.
- **Asynchronous but auditable.** Git history, progress reports, queue receipts,
  notification receipts, machine identity, and exact-thread identity preserve what
  happened even when machines and people are not online at the same time.
- **Mechanism, not policy.** WatchDog does not decide who may command which agent,
  team hierarchy, work-time limits, checkpoint rules, or merge authority. Those
  belong to GitHub permissions, branches, and each project's own `AGENTS.md`
  contract.

## Design philosophy

- **Reuse mature infrastructure instead of rebuilding it.** GitHub, Slack, SSH,
  VS Code, Git, Codex, and the operating system already solve difficult problems.
  WatchDog should integrate them, not replace them with less mature copies.
- **Only implement the missing edges.** Identity/routing, exact-thread wakeup,
  handoff, fencing, notification, and relay belong here only when the surrounding
  tools do not already provide them.
- **Keep the mechanism dumb.** WatchDog should mostly observe, wake, notify, relay,
  route, and then get out of the way.
- **Preserve native agent ownership.** WatchDog does not create replacement chats
  just to simplify orchestration. Existing Codex sessions stay authoritative.
- **WatchDog observes Git; Codex owns Git.** WatchDog never stages, commits, pulls,
  merges, rebases, resets, checks out, or pushes.
- **GitHub is the durable coordination plane.** Comments, commits, branches, and
  progress reports survive terminals, machines, managers, restarts, and time zones.
- **Slack is the shared fast interaction plane.** It provides mature users,
  channels, threads, notifications, and visibility boundaries for quick team
  interaction; it is not a replacement for durable project history.
- **No mandatory center.** Each machine/locality can keep its own WatchDog and
  native sessions. Managers can also be distributed; the fabric does not require
  one authoritative manager session.
- **Manager-agnostic, Codex-specific.** The management side can be a human,
  ChatGPT, another agent, or automation that writes durable direction. The
  execution side currently relies on Codex's exact-thread queue, hooks, state, and
  completion contracts.
- **Many observers, one actor.** Where local and detached WatchDogs coexist, they
  coordinate ownership rather than racing to perform side effects.
- **Reuse before rebuilding. Integrate before inventing.** Prefer files, Git,
  GitHub, Slack, SSH, locks, and existing CLIs over inventing another platform.

## Multi-agent projects: policy stays in `AGENTS.md`

WatchDog itself does **not** assign Codex A/B/C, impose work-hour limits, decide
who may instruct somebody else's agent, or decide who may merge. That would turn
a thin control fabric into a project-management framework.

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

## Multi-user collaboration: bring your own agents

WatchDog does not require a team to register every machine and agent under one
central runtime. Each person can run their own WatchDog on their own machines and
connect selected agents to the collaboration surfaces the team already shares.

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

## One possible topology: an aggregated manager

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

This is only one topology. The manager itself may be a persistent Codex session on
AWS, several distributed managers, a human working directly with individual
agents, or any mixture of those patterns. The WatchDog layer does not change.

## Platform status

| Platform | Overall maturity |
| --- | --- |
| Windows x64 | **Stable desktop reference**, native E2E and package upgrades verified |
| Linux ARM64 / x64 | **Native server/detached support with successful user dogfood**; local desktop remains preview |
| macOS Apple Silicon | **Preview with bounded native E2E evidence**; discovery topology and package caveats remain |

### Workflow support matrix

| Workflow | Windows x64 | Linux ARM64 / x64 | macOS Apple Silicon |
| --- | --- | --- | --- |
| Local desktop | Native E2E verified | Preview; native desktop E2E pending | Native E2E on resolvable window topologies |
| Remote-SSH | Linux-target controller verified | Native execution target verified | Controller acceptance remains bounded |
| Detached same-thread owner | Controls Linux targets | Native E2E; successful manual Spark dogfood | No native macOS detached owner |
| Automatic remote handoff | Desktop side verified | Native lifecycle verified; VS Code reload sometimes needed | Full handoff E2E not established |
| Packaged executable | Startup, icon and upgrade acceptance | ARM64 Ubuntu; x64 Ubuntu and RHEL 8/glibc 2.28 acceptance | Developer preview; version-specific device acceptance |

The user completed manual detached dogfood on Spark with no observed issue. This
is native server-workflow evidence, separate from Linux desktop acceptance.
Missing-log discovery uses native writer-PID evidence on Windows/Linux; macOS
still needs resolvable routing evidence. See [details and limitations](docs/PLATFORM_SUPPORT.md).

## What WatchDog currently does

- Observes Codex Stop/completion events and captures final output for notifications.
- Wakes or continues the **exact existing Codex thread** instead of creating a new
  context.
- Uses read-only Git remote-OID checks as a GitHub update doorbell, leaving all Git
  mutation to Codex.
- Sends Slack notifications with Outlook/SMTP fallback and a local audit trail.
- Relays allowlisted Slack replies back to the mapped exact thread through
  **Parrot Dog**.
- Includes machine identity in routed Slack notifications so distributed sessions
  remain distinguishable in shared channels.
- Discovers local and VS Code Remote-SSH workspaces and keeps session identities
  distinct.
- Keeps local VS Code discovery working when older routing logs disappear, by
  verifying the exact thread's native writer in its current window.
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

From v0.2.20, idle Linux conversations release their writer lock automatically
after a short grace and live idle/empty-queue checks. Monitoring and Slack replies
stay enabled; VS Code can reopen the same conversation. `--repo` includes every
enrolled thread in that repository, so one Git update can wake several distinct
conversations. Use `--thread UUID` when only one conversation should be monitored.

On clusters with shared home directories, run one local WatchDog on each eligible
login node, with hostname-specific runtime state. Each dog discovers native work
on its own node; conversations do not roam automatically. See the
[node setup and existing-installation limits](docs/LINUX_NODE_SETUP.md).

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

- [Manual builds, tests, and releases (GitHub Actions disabled)](docs/MANUAL_RELEASE.md)
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
