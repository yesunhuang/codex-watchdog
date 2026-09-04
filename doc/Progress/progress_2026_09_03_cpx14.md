# Progress report - 2026-09-03 checkpoint 14 remote outage lifecycle

Final snapshot: 2026-09-03 CDT

Scope: make Remote-SSH monitoring failures visible without generating alerts
for a one-cycle transient, retry notifications that were never delivered, and
add the supplied Shiro icon to the project README.

## 1. Behavior implemented

Each automatically tracked remote workspace now persists a small health state
beside its existing completion and Git cursors. A failed SSH/Plink probe, an
unresolved exact Codex thread, or the disappearance of a previously tracked
Remote-SSH VS Code window increments one shared consecutive-failure counter.

The first failed cycle is recorded but does not notify. The second consecutive
failure opens a unique outage and sends `remote_adapter_attention` through the
existing Slack-primary notification chain. Later failed cycles do not repeat a
successfully handled alert. If every configured delivery channel fails, the
outage remains pending and the next polling cycle retries it.

When the remote window, transport, and exact Codex thread become reachable
again, the service clears the failure streak and sends one
`remote_adapter_recovered` notification. A later outage receives a new outage
identity and is therefore not incorrectly suppressed as a duplicate of the
earlier outage.

The MFA HPC-specific `remote_duo_upstream_unavailable` message tells the operator
that the authenticated Plink upstream ended and that a fresh password and Duo
approval are required. Other failures direct the operator to the VS Code remote
connection and SSH authentication. A missing-window message distinguishes a
closed, reloaded, or disconnected VS Code window from an adapter failure.

Only remote state created or observed by this version opts into window-presence
tracking. This prevents stale pre-feature state files from manufacturing outage
alerts after upgrade. Current excluded windows remain visible to the lifecycle,
and an excluded repository name, path, or Remote-SSH URI does not become a
missing-window alert after it closes.

## 2. Delivery semantics

The notifier no longer records a transition fingerprint after
`delivery_failed`. Previously, this made the next identical attempt appear
delivered and suppressed it forever. Successful delivery, fallback delivery,
explicit audit-only handling, and an already persisted duplicate retain their
existing deduplication behavior.

The watchdog process must itself remain alive and have a working Slack or
fallback route. A machine power failure or total network outage cannot produce
an immediate external message; pending delivery resumes on a later healthy
cycle.

## 3. Documentation and icon

`Shiro.png` is now rendered at a bounded width beneath the README title. The
README also documents the two-cycle debounce, lost-window detection, delivery
retry, recovery message, MFA HPC/Duo instruction, and the watchdog-host outage
boundary.

## 4. Verification

- Full Python suite: 267 passed with one existing intentional skip.
- New coverage: one-cycle transient recovery, sustained adapter loss, repeated
  failed delivery, recovery, a second independent outage, and remote VS Code
  disappearance/reappearance.
- `python -m compileall -q src tools tests`: passed.
- `python -m black --check src tests`: passed for all 32 files.
- `git diff --check`: passed.

No live GPU lab or MFA HPC connection was deliberately interrupted for this
checkpoint. The failure lifecycle was exercised deterministically through the
service boundary so the operator's authenticated sessions remained intact.

## 5. Operational note

The already running watchdog process must be restarted once after pulling this
commit because Python does not hot-reload the service implementation. At the
current 120-second launcher interval, an outage notification is expected after
two failed polls (roughly two to four minutes after the fault), followed by one
recovery message after a successful poll.

## comment

### Checkpoint 15 — Slack “parrot dog” reply relay

The current WatchDog is working well. Continue the highway-tire-repair rule:
keep this checkpoint narrow, reuse proven pieces, and do not redesign the
existing local/GPU lab/MFA HPC/GitHub architecture.

Goal: add a lightweight **interactive Slack reply path** so the operator can
answer small Codex questions from the phone (for example `retry`, `done`, `use
B`, `stop and report`, or a simple permission/approval decision) without first
routing the message through ChatGPT + GitHub/temporal prompt.

The architectural invariant is:

> **Slack is a relay to the already-existing exact VS Code Codex thread. It is
> not a second Codex frontend/session owner and not another agent.**

#### 1. Reuse existing wheels first

Inspect and selectively reuse patterns from:

- `wabol/codex-bot`: Slack Socket Mode, trusted-user allowlist, Codex
  app-server approval semantics, interrupt/steer ideas, and thread/session
  correlation;
- `pkemp-ai/slack-local-claude-code-wrapper`: Slack Bolt/Socket Mode,
  authorized approver handling, and compact Block Kit Approve/Deny UX;
- official `slackapi/bolt-python`: prefer the normal Python Socket Mode path if
  it keeps our implementation smaller.

Do **not** vendor or transplant either bot wholesale. Our existing exact VS Code
thread discovery and ownership model remain authoritative.

#### 2. Required relay model

For a WatchDog-originated Slack notification, persist only the minimum mapping
needed to correlate a Slack thread/message back to the already-resolved Codex
thread/workspace. A reply from the configured operator should be authenticated
by Slack user ID and accepted only in a known WatchDog-created Slack thread.

Normal text should be relayed essentially verbatim into that exact Codex thread
using the existing first-party wake/queue mechanism where possible. WatchDog
must not interpret the research meaning, choose commands, modify Git, or execute
the requested action itself.

Desired flow:

```text
Codex / WatchDog event
  -> Slack notification thread
  -> operator reply from allowlisted Slack user
  -> WatchDog correlates known Slack thread -> exact existing Codex thread
  -> first-party Codex wake/queue/steer path
  -> Codex decides and acts with its retained context
```

Do not create a new Codex thread merely because Slack received a message.

#### 3. Permission / approval handling: investigate before implementing

`wabol/codex-bot` can directly answer Codex approval requests because that bot
owns the `codex app-server` process. Our WatchDog does **not** own the VS Code
Codex app-server. Therefore do not assume its `approve`/`deny` implementation is
portable.

First determine, from live/local evidence and supported first-party Codex
interfaces, whether an approval request belonging to the existing exact VS Code
thread can be safely answered externally without taking ownership of or
commandeering VS Code's private app-server transport.

- If a supported exact-thread approval/steer interface exists, implement the
  smallest adapter and optionally expose Slack Approve/Deny buttons.
- If no supported interface exists, **do not hijack private VS Code IPC, stdin,
  sockets, or internal app-server ownership merely to obtain approval buttons.**
  In that case implement the text reply relay first and report the remaining
  approval boundary honestly. A Slack reply such as `permission fixed; retry`
  is still useful after the operator performs the unavoidable local/UAC/Duo
  action.

Any direct approval path must fail closed, bind to the exact pending request,
require the authorized Slack operator, and never turn a generic Slack message
into blanket future approval.

#### 4. Slack transport and secrets

The current Incoming Webhook notification path remains valid and must not
regress. Add inbound Slack capability as an additional narrow path, preferably
Socket Mode so no public callback URL is required.

Use the minimum Slack scopes required. Keep bot/app tokens out of Git, reports,
logs, and command history; follow the existing DPAPI/local-secret pattern where
appropriate. Do not expose or rotate the existing webhook unnecessarily.

Avoid two competing Socket Mode listeners for the same Slack app. If the
existing app can safely host the single WatchDog listener, prefer that over
creating extra infrastructure; otherwise document the smallest required app
change.

#### 5. Safety / behavior boundaries

- accept messages only from explicitly configured Slack user ID(s);
- accept replies only for known WatchDog notification threads (no arbitrary
  channel-as-shell behavior);
- ignore bot/self events and Slack retries/duplicates idempotently;
- preserve exact workspace/thread correlation and fail closed on ambiguity;
- no natural-language intent parser in WatchDog;
- no shell execution from Slack;
- no Git mutation in WatchDog;
- no second Codex session lifecycle;
- keep GitHub as the durable manager/review channel; Slack is the quick
  walkie-talkie/interrupt channel, not a replacement for progress reports.

#### 6. Acceptance target

At minimum, live-accept one end-to-end ordinary reply on an existing VS Code
Codex thread:

```text
WatchDog sends a uniquely tagged Slack notification
-> operator replies in that Slack thread
-> WatchDog authenticates and correlates it
-> the same exact Codex thread receives the text
-> Codex emits a unique completion marker
-> normal Slack notification returns the result
```

If supported direct permission approval is feasible, live-accept one scoped
Approve or Deny as a second test. Do not manufacture a risky permission request
just for acceptance; use a harmless controlled request or defer live approval
acceptance until one occurs naturally.

Keep the foreground service lightweight. Do not add a general ChatOps framework,
HTTP service, database server, or new model call. Prefer small reuse of Slack
Bolt/Socket Mode over a large dependency stack.

Maximum active work for this checkpoint: 2 hours. Run focused tests plus the
normal regression suite, publish a standard progress report with implementation
and live-vs-test evidence clearly separated, commit/push through Codex-owned Git,
and stop for review if a substantive ownership/API boundary is encountered.
