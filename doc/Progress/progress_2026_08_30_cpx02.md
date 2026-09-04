# Progress report - 2026-08-30 checkpoint 2 hooks/queue feasibility PoC

Start: 2026-08-30 00:33 CDT

End: 2026-08-30 01:27 CDT

Elapsed: approximately 54 minutes, under the two-hour checkpoint limit

Scope: the reviewer-approved checkpoint-2 feasibility and minimal-PoC work:
native Stop/PermissionRequest hooks, a 5-20 minute short grace window,
identified same-conversation continuation, loop protection, true parking,
long-stop wake investigation, Remote-SSH feasibility, and notifier ecosystem
review.

Not attempted: the full Git state machine, workspace discovery/service loop,
automatic Git preservation by the watchdog, Slack/SMTP delivery, a service
installer, broad UI automation, or production deployment. No `## comment`
section was created.

## 1. What was attempted

- Read the checkpoint-1 report and its reviewer-provided `## comment` as the
  approved scope.
- Consulted the [official OpenAI Hooks documentation](https://learn.chatgpt.com/docs/hooks)
  and [official App Server protocol documentation](https://learn.chatgpt.com/docs/app-server).
- Inspected the installed Codex extension/CLI, features, hook configuration,
  generated App Server schemas, active thread context, and queue implementation.
- Reviewed existing hook/notifier projects and relevant upstream failure
  reports before choosing what to reuse.
- Built a stdlib-first Python PoC for short Stop-hook continuation, instruction
  identity, durable local state, loop protection, PermissionRequest recording,
  first-party queue wake, and the claim/retain half of resume-prompt handling.
- Added unit, protocol, failure, collision, privacy, deadline, and real
  cross-process Windows lock tests.
- Ran a negative native `codex exec` hook probe.
- Ran a fresh App Server model probe with three ordinary turns followed by an
  external `codex queue` wake.
- Ran two no-model `hooks/list` preflights to inspect project hook discovery and
  trust.
- Attempted each of the eight requested acceptance areas and recorded failures
  or unavailable prerequisites without substituting synthetic evidence for a
  live VS Code result.

## 2. What was successfully implemented / derived / verified

### Minimal PoC

- `InstructionStore` atomically publishes JSON instructions, uses hashed
  Windows-safe filenames, validates content digests, and distinguishes queued,
  inflight, consumed, and per-turn guard state.
- Short-stop instructions have an explicit target session ID. A different
  session cannot claim them. The CLI requires `submit --thread`.
- Claims are FIFO by creation timestamp rather than filename hash.
- A synchronous Stop invocation waits for 300-1200 seconds in production, with
  a 600-second default and a bounded test mode.
- The first claim attempt is immediate; later attempts check the deadline before
  claiming, so an instruction published after expiry remains queued.
- One session/turn can create at most one continuation intent. Re-entry only
  treats JSON boolean `stop_hook_active: true` as active, confirms the matching
  intent, and parks without claiming another instruction.
- Malformed hook input, malformed inbox state, handler errors, and lock
  contention fail open rather than accidentally blocking Codex.
- Audits retain assistant-output SHA-256/length, not assistant text.
- `PermissionRequest` is recorded as `permission_observed_pre_routing` and the
  handler returns `{}` to defer; it is not mislabeled as a user wait.
- Queue wake journals `dispatching` before invoking an argv-array subprocess.
  Reuse of an ID with another thread/source/prompt is a collision. `uncertain`
  and `dispatching` stay unresolved and return failure instead of becoming a
  successful duplicate.
- Queue journals retain stdout/stderr digests and lengths rather than raw output.
- Remote Git wake IDs are scoped by workspace/thread and remote OID, and use a
  fixed mechanical prompt.
- `resume_prompt.md` is moved to a UUID-named inflight file before it is read.
  Identical intentional later prompts are not permanently deduplicated. The
  queued prompt requests one DISCARD/ARCHIVE disposition line, while the
  inflight file remains retained.
- A spawned-process test proved that the one-byte advisory lock excludes a
  second Windows process and releases after the owner exits.

### First-party queue discovery and live result

Installed `codex queue` does not need to attach to the IDE-owned stdio stream.
The command writes a durable queue item into shared Codex state, and every
loaded App Server watches external queue revisions. This was first derived from
the installed CLI/source and then proved empirically on Windows.

The fresh durable App Server probe created thread:

```text
<UUID>
```

A separate process returned:

```text
Queued message <UUID>
for thread <UUID>.
```

The owning App Server then started a new turn on that same thread and produced:

```text
QUEUE_WAKE_SAME_THREAD:APPSERVER_PARK_PHASE
```

`APPSERVER_PARK_PHASE` was the immediately preceding assistant marker, so the
queued turn demonstrated retained conversation context. The thread was archived
after the probe. This is strong evidence for the narrow queue architecture, but
it is not evidence for the user's already-open VS Code thread or Remote-SSH.

### Ecosystem review

- [`Wangmerlyn/coding-agent-notifier`](https://github.com/Wangmerlyn/coding-agent-notifier)
  (MIT) is the best Slack/Remote-SSH notification transport pattern to reuse.
- [`CorridorSecurity/hookshot`](https://github.com/CorridorSecurity/hookshot)
  (MIT) has useful Go hook protocol/types and installers for Stop and
  PermissionRequest.
- [`mylee04/code-notify`](https://github.com/mylee04/code-notify) (MIT) has a
  mature Slack/Discord notification surface, but Codex support uses legacy
  notify rather than the new synchronous hooks.
- [`bentoner/codex-516-hook`](https://github.com/bentoner/codex-516-hook) (MIT)
  demonstrates Windows Stop-hook fail-open patterns.
- [`soulucasbonfim/codex-approval-notifier`](https://github.com/soulucasbonfim/codex-approval-notifier)
  (MIT) provides PermissionRequest patterns for macOS/Linux/WSL.
- [`ch040602/codex-cli-notify`](https://github.com/ch040602/codex-cli-notify)
  contains a Windows overlay pattern but had no license identified, so it is
  inspect-only.

No reviewed project combines synchronous grace continuation, durable IDs,
loop guards, same-thread parked wake, AutoReview-aware PermissionRequest
semantics, Slack plus SMTP, and Remote-SSH. Reuse notifier transports/patterns;
keep the continuation state machine local.

## 3. Current best implementation state / architecture

The Phase-0 shim-first architecture is superseded. The current candidate is:

```text
native Stop / PermissionRequest hooks
    -> privacy-limited audit
    -> short deterministic grace wait
    -> one decision:block continuation in the same turn
    -> stop_hook_active confirmation and PARK

PARK
    -> future deterministic Git/resume watcher
    -> identified `codex queue` wake
    -> same durable thread
```

The queue mechanism is narrower and less invasive than UI automation or an
extension-host shim. It is now the preferred long-wake candidate. A deep shim
remains last resort only if a trusted live VS Code/Remote-SSH test disproves
hooks or queue behavior.

This remains a PoC, not the full watchdog. Logical at-most-once intent is
preferred over automatic retries. Crash windows can strand inflight or
uncertain state and require manual reconciliation.

The example TOML is descriptive only. The repository deliberately contains no
live `.codex/hooks.json`; installation requires explicit absolute paths,
fresh-session loading, inspection, and manual hash trust through `/hooks`.

## 4. Observed runtime behavior / probe results

Observed environment:

| Component | Value |
| --- | --- |
| Codex CLI | `0.151.0-alpha.7.2` |
| Codex VS Code extension | `openai.chatgpt` 26.825.51511 |
| Hooks feature | stable, enabled |
| Probe approval policy | `on-request` |
| Probe approval reviewer | `auto_review` |
| Probe sandbox | `workspace-write` |
| Python | 3.9.12 |
| Host | Windows |

### Installed hook behavior

`codex exec --json` completed its model turn but emitted no Stop hook/audit.
That matches upstream reports that the exec path does not exercise project Stop
hooks and is retained as negative evidence, not an acceptance substitute.

The fresh App Server `hooks/list` preflight found both generated hooks with:

```text
source = project
enabled = true
trustStatus = untrusted
warnings = []
errors = []
```

The full fresh App Server run completed ordinary model turns, but there were no
`hook/started`, `hook/completed`, or local audit records. Passing
`--dangerously-bypass-hook-trust` did not make this installed App Server execute
the untrusted project hooks. The exact hook file was removed after each probe.

Adding a project hook under the already-open development thread also reproduced
the known hot-reload failure: subsequent sandbox setup refreshes failed until
the generated hook file was removed. No live project hook remains installed.

### Requested acceptance matrix

| # | Requested proof | Result |
| --- | --- | --- |
| 1 | Local VS Code Stop observed via native hooks | **Failed live.** Hooks were discovered but untrusted and did not execute. Synthetic handler tests pass. |
| 2 | Payload identifies workspace/session/latest output | **Partial.** Official/installed schema exposes `cwd`, `session_id`, `turn_id`, and `last_assistant_message`; digest capture passes tests, but no trusted live payload was received. |
| 3 | Grace-window prompt continues same conversation | **Partial.** Protocol tests prove one `decision:block` with the exact identified prompt. Live continuation was not exercised because Stop did not fire. |
| 4 | Repeated cycles avoid loops/duplicates | **Partial.** Three distinct synthetic cycles pass; same-turn duplicate and `stop_hook_active` tests pass. Installed re-entry semantics remain unproved. |
| 5 | Grace expiry parks with no model polling/token burn | **Partial.** The handler returns `{}` exactly once at expiry and uses no model polling. No live hook interval existed, so billing-level/token proof was not claimed. |
| 6 | Parked local VS Code UI wake, same conversation | **Partial substitute only.** UI automation of Codex is prohibited by the available computer-use safety rules. The first-party queue successfully woke a fresh loaded durable App Server thread with context; the user's existing VS Code thread was not targeted. |
| 7 | Real Remote-SSH Stop and long wake | **Not testable this checkpoint.** No real remote Codex conversation/thread was available, and no SSH/MFA weakening was attempted. |
| 8 | UI accessibility or smallest screenshot fallback | **Not attempted by rule.** The computer-use skill prohibits automating Codex/Codex CLI UI; screenshot/vision was not used to circumvent it. |

### PermissionRequest / AutoReview

The installed thread context confirmed `approvals_reviewer=auto_review`.
PermissionRequest handling is unit-tested as a pre-routing observation, but the
untrusted project hook prevented an installed payload/reviewer-routing probe.
No claim is made that PermissionRequest means a human is waiting.

### Relevant upstream risk reports

The design explicitly accounts for reported hook loops, hot-edit failures,
missing Stop events, and pre-AutoReview PermissionRequest timing. Relevant
OpenAI Codex issues reviewed include
[#34477](https://github.com/openai/codex/issues/34477),
[#37937](https://github.com/openai/codex/issues/37937),
[#33992](https://github.com/openai/codex/issues/33992),
[#21160](https://github.com/openai/codex/issues/21160),
[#28833](https://github.com/openai/codex/issues/28833), and
[#22858](https://github.com/openai/codex/issues/22858).

## 5. Tests and sanity checks performed

- `python -m pytest`: **24 passed**.
- `python -m compileall -q src tools tests`: passed.
- `examples/hooks.json`: parsed successfully as JSON.
- Both CLI wrapper `--help` paths executed successfully.
- `git diff --check`: passed after trailing-newline cleanup.
- Cross-process lock test used a spawned process rather than an in-process mock.
- The queue adapter tests use a fake subprocess to prove argv construction,
  journaling, collision, privacy, uncertain delivery, and no automatic retry.
- The live queue proof used the real installed `codex queue` and a real fresh
  App Server/model thread.
- The first sandboxed `black --check` could not access its normal user cache.
  It was rerun with the required filesystem permission, all 10 indicated files
  were formatted, and final `python -m black --check src tests tools` passed
  with all 15 Python files unchanged.

The suite covers more than the requested eight attempts, including targeted
claim isolation, creation-order FIFO, ID/content collisions, at-most-once
claim/confirmation, wrong-turn confirmation rejection, malformed inbox/input,
deadline expiry, one continuation during grace, repeated loop-guard cycles,
PermissionRequest classification, output privacy, queue collisions, unresolved
delivery retention, resume atomic claim, and Windows process locking.

## 6. Unresolved technical issues / limitations

- The critical feasibility gate is still open: manually trusted Stop and
  PermissionRequest hooks must run in a fresh real VS Code conversation.
- The trust-bypass flag did not bypass project-hook trust in the installed App
  Server path. The PoC will not silently persist trust state.
- The queue proof used a fresh durable App Server thread, not the user's current
  VS Code thread. Queue receipt verification currently stops at CLI enqueue
  acknowledgement; production needs later thread/turn observation.
- No real Remote-SSH Codex conversation was available. Remote hook files,
  queue state, runtime, and Git must execute in remote locality.
- The filesystem journal is logical at-most-once, not power-loss transactional.
  A crash between move/rewrite/guard operations can strand an intent.
- No explicit reconcile/reissue command exists for `dispatching`, `uncertain`,
  or orphaned inflight state.
- Resume prompt disposition output is requested but not parsed/correlated, so
  DISCARD/ARCHIVE actions are not implemented.
- Resume producers must publish with temp-file plus atomic rename; the consumer
  cannot prove a directly written source file is closed.
- Runtime prompts, absolute cwd, session IDs, and turn IDs are plaintext local
  data. Queue prompt text is briefly visible in the Windows process command
  line. Directory ACL and retention policy are not automated.
- Audits prove the latest assistant output only by digest/length. Notification
  transport and user-facing output capture are deferred.
- `config.example.toml` has no loader. There is no polling loop, state reducer,
  workspace adapter, Git adapter, notifier, installer, or dogfood service.
- Temporary acceptance probes still create a short-lived project hook file.
  Cleanup now removes it only when its content still matches, but an abrupt
  process kill could leave it behind; probes must check `.codex/` afterward.
- Installed APIs are from an alpha CLI and must be re-probed after upgrades.

Notification implementation should reuse the MIT Slack patterns from
`coding-agent-notifier`, with SMTP as a minimal fallback, rather than spending a
future checkpoint inventing another transport. That work was intentionally not
performed here.

## 7. Exact files modified or created

Modified:

- `.gitignore`
- `architecture.md`
- `probe_report.md`

Created:

- `README.md`
- `config.example.toml`
- `examples/hooks.json`
- `pyproject.toml`
- `src/codex_watchdog/__init__.py`
- `src/codex_watchdog/__main__.py`
- `src/codex_watchdog/cli.py`
- `src/codex_watchdog/models.py`
- `src/codex_watchdog/queue_wake.py`
- `src/codex_watchdog/stop_hook.py`
- `src/codex_watchdog/storage.py`
- `tests/test_locking.py`
- `tests/test_queue_wake.py`
- `tests/test_stop_hook.py`
- `tests/test_storage.py`
- `tools/codex_watchdog.py`
- `tools/codex_watchdog_hook.py`
- `tools/run_appserver_hook_probe.py`
- `tools/run_native_hook_probe.py`
- `doc/Progress/progress_2026_08_30_cpx02.md`

Generated `.schema-probe/`, `pytest-of-*`, `codex-watchdog-native-*`, Microsoft
PowerShell cache files, Python caches, and runtime state are ignored probe
debris and are not checkpoint artifacts. They are removed at handoff.

## 8. Recommended next step - not performed

In a fresh expendable local VS Code project conversation, manually inspect and
trust the exact project-hook hashes through `/hooks`, then run only this ordered
acceptance sequence:

1. one Stop with a recognizable final output and no instruction;
2. one Stop with a targeted instruction arriving inside a short test grace;
3. two more identified Stop/continue cycles to capture real
   `stop_hook_active` behavior and hook start/completion timing;
4. one PermissionRequest under AutoReview to distinguish pre-routing hook
   observation from effective reviewer handling; and
5. after a confirmed park, invoke `codex queue` from a separate process and
   verify the already-open VS Code thread resumes with prior context.

If and only if those local checks pass, repeat the smallest relevant sequence
in a real Remote-SSH Codex conversation. Then decide whether checkpoint 3 should
build the deterministic Git/notifier loop around hooks plus queue, or record a
hard feasibility blocker. This recommended work was not performed.

## comment

## Checkpoint 3 — Live acceptance only: trusted hooks plus `codex queue` on real VS Code threads

Checkpoint 2 produced the main architectural breakthrough: first-party `codex queue --thread <UUID> --message <TEXT>` appears to provide the narrow PARKED -> RUNNING courier we need without UI automation or an App-Server shim. The next checkpoint should therefore **not expand the framework**. It should close the remaining live feasibility gates on real VS Code sessions.

### 1. Freeze the architecture during this checkpoint

Do not build the full Git state machine, polling/service loop, Slack/SMTP subsystem, workspace manager, installer, reconciliation UI, or a deeper App-Server shim yet.

The only purpose of checkpoint 3 is to answer, with real live evidence:

```text
A. Do trusted native hooks work reliably in an actual VS Code Codex conversation?
B. Can `codex queue` wake the actual already-open VS Code Codex thread after it is parked?
C. Do the same minimal mechanisms work for a real Remote-SSH Codex conversation?
```

If these answers are positive, the remaining project becomes ordinary deterministic engineering.

### 2. Prefer a user-level hook installation over per-project hot-edited hooks

Checkpoint 2 showed that project-local hook hot reload is fragile and can break sandbox refreshes. Before further project-hook experiments, investigate the supported **user-level hook** path (for example the relevant `CODEX_HOME` / user hook configuration) and determine whether it is better suited to an always-on watchdog.

The intended deployment model is one watchdog hook installation per Codex environment, not a new `.codex/hooks.json` edited inside every repository.

For local Windows, test the user-level installation in the local Codex environment. For Remote-SSH, the equivalent hook must eventually be installed/trusted in the remote Codex environment because that session has its own remote `CODEX_HOME` and execution locality.

Do not bypass trust. If a one-time manual `/hooks` review/trust action is required, provide the exact minimal instruction to the user and wait for that action. Do not attempt to mutate persistent trust state silently.

Record explicitly:

- where the user-level hook file lives in the installed version;
- whether it is loaded by VS Code Codex;
- what trust model applies;
- whether trust is one-time per command hash/environment;
- whether it survives new projects and new VS Code sessions.

### 3. Local live Stop-hook acceptance

Use a fresh expendable **real VS Code Codex conversation**, not `codex exec` and not a synthetic handler, and run the following ordered sequence.

#### Test A — Stop event and payload

Trigger one ordinary Stop and prove that the trusted hook actually runs.

Capture only what is needed to establish the contract:

```text
session_id
turn_id
cwd
last_assistant_message availability
stop_hook_active
hook start/completion timing
```

Do not retain full assistant text in durable audit logs unless needed for the temporary acceptance observation.

#### Test B — short-grace continuation

Use a short test grace interval (for example 30–60 seconds; keep the production default at 600 seconds).

During the live Stop hook wait, publish one instruction targeted to the exact session. Prove:

```text
Stop hook receives instruction
-> returns decision:block + exact reason
-> SAME VS Code conversation continues
-> prior conversation context is retained
```

#### Test C — repeated-cycle loop guard

Repeat at least two additional identified Stop/continue cycles and observe the **real installed** `stop_hook_active` behavior.

Prove that:

- one external instruction is consumed at most once;
- the same Stop/turn cannot emit two continuations;
- re-entry does not create an infinite Stop loop;
- a later independent turn can receive a later independent instruction.

#### Test D — true park

Allow one Stop grace interval to expire with no instruction.

Prove operationally that the hook returns normally and the VS Code conversation remains parked/idle without watchdog-triggered model polling.

Do not attempt a billing-level proof if it is not observable; just establish that our code makes no further model calls while parked.

### 4. Local `codex queue` acceptance on the actual already-open VS Code thread

After Test D has produced a genuinely parked VS Code thread, invoke `codex queue` from a **separate process** against that exact real VS Code thread ID.

Use a recognizable context marker from the immediately preceding conversation and require the queued turn to report it, so that same-thread continuity is proved rather than assumed.

This is the most important acceptance test of the checkpoint.

Do not substitute another fresh App Server thread if the real VS Code thread test fails. Record the real failure.

### 5. Strengthen queue delivery evidence

The current `QueueWakeDispatcher` treats `returncode == 0` as `accepted`. That is only enqueue acknowledgement, not proof that the queued message was actually consumed and started as a turn.

During this checkpoint, strengthen the PoC just enough to preserve useful delivery identity:

1. parse and record the queued-message ID returned by `codex queue`;
2. retain the target thread ID and prompt digest;
3. when feasible, observe later evidence that the queue item was consumed / a new turn was started;
4. distinguish at least:

```text
enqueued
consumed_or_started
uncertain
```

Do not create aggressive automatic retry logic. If delivery is uncertain, retain the record for manual reconciliation rather than risk duplicate turns.

### 6. PermissionRequest / AutoReview characterization is secondary

Run one real PermissionRequest under the user's existing AutoReview configuration after hooks are trusted.

The only goal is to characterize timing and semantics:

```text
Does PermissionRequest hook fire before AutoReview routing?
Does the hook event imply user intervention, or can AutoReview resolve it immediately afterward?
```

Do not make PermissionRequest the core stopped-state detector yet. Record the observed behavior and keep notification logic conservative.

### 7. Remote-SSH live acceptance

Only after the local live tests above pass, repeat the **smallest relevant subset** on a real VS Code Remote-SSH Codex conversation.

At minimum test:

1. trusted Stop hook execution in the remote Codex environment;
2. short-grace continuation into the same remote-backed VS Code conversation;
3. true park;
4. `codex queue` executed in the correct remote Codex locality against the exact remote thread;
5. same-thread context retention after the queued wake.

Do not weaken MFA/SSH security. Do not assume local Windows `codex queue` can address a remote Codex store. The queue command and runtime state must be bound to the environment that owns that thread.

If a human must first open a real remote Codex conversation or perform one-time hook trust, state exactly what is needed and stop at that prerequisite rather than substituting synthetic evidence.

### 8. UI automation is no longer the preferred path

Checkpoint 2's first-party queue discovery makes UI automation unnecessary unless real VS Code queue acceptance fails.

Do **not** spend this checkpoint on screenshot/OCR/UI automation.

Likewise, do not revive the extension-host/App-Server shim unless supported hooks or queue fail in the real target sessions.

### 9. Preserve current safety properties

Keep the good properties already implemented:

- fail-open hook behavior on watchdog errors;
- targeted session IDs;
- instruction IDs and prompt digests;
- no blind duplicate retry;
- atomic resume-prompt claim;
- per-turn loop guard;
- no raw assistant output in durable audit by default;
- no silent trust modification.

Do not over-engineer power-loss transactions in this checkpoint.

### 10. Decision gate for the next checkpoint

If **local trusted Stop continuation + local parked `codex queue` wake + Remote-SSH parked queue wake** all pass, then the architecture is considered feasible and checkpoint 4 may begin the ordinary deterministic integration work:

```text
Git preserve/push/fetch/ff-only logic
remote update detection
Slack primary notification
SMTP fallback
resume_prompt disposition handling
polling/service loop
dogfood deployment
```

If local hooks fail even after explicit correct trust, investigate that narrow blocker before building anything else.

If hooks work but real VS Code `codex queue` wake fails, record that as the remaining hard blocker and reconsider only the narrow wake mechanism.

### 11. Dogfooding and stopping rule

Continue to follow the repository rule: at most two hours of work, then create the next standard progress report and stop for review.

Where practical, use the watchdog repository's own development Codex conversation as the local real-session acceptance target. However, do not modify the currently active conversation's hook configuration in a way known to trigger hot-reload breakage; use a fresh expendable conversation/environment when necessary.

Do not proceed to production integration simply because synthetic tests pass. This checkpoint is complete only when the live acceptance evidence or a clearly documented hard blocker is recorded.