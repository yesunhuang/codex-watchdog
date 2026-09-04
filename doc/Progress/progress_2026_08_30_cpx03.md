# Progress report - 2026-08-30 checkpoint 3 trusted live VS Code acceptance

Start: 2026-08-30 01:59 CDT

End: approximately 2026-08-30 15:25 CDT

Elapsed: approximately 13 hours 26 minutes wall clock. Work was split into
bounded active runs by explicit user hook-trust actions, fresh-session actions,
and live Stop/queue handoffs; the long gaps were inactive waits. No individual
active implementation run exceeded the two-hour limit. Aggregate active time
was not continuously timed.

Scope: the reviewer-approved checkpoint-3 live feasibility gate only: trusted
user-level Stop/PermissionRequest hooks, short-grace continuation, loop and park
behavior, strengthened queue-delivery identity, a queue wake on the actual
already-open VS Code thread, PermissionRequest/AutoReview characterization, and
a read-only search for a usable Remote-SSH target. Two runtime defects exposed
by the live probes were fixed and regression-tested.

Not attempted: the full Git preservation/fetch/fast-forward state machine,
polling/service loop, Slack/SMTP notification, workspace manager, production
installer, resume-prompt disposition actions, a deep App Server shim, UI
automation, or a synthetic substitute for Remote-SSH. No SSH connection was
opened or changed. No `## comment` section was created.

## 1. What was attempted

- Read checkpoint 2 and its reviewer-provided comment as the approved scope.
- Consulted the [official OpenAI Hooks documentation](https://learn.chatgpt.com/docs/hooks)
  and inspected the installed Codex CLI/extension hook and queue behavior.
- Installed acceptance hooks at the user level, had the user review/trust each
  exact definition through `/hooks`, and never edited persistent trust state.
- Exercised trusted hooks in fresh App Server controls and the actual VS Code
  watchdog development conversation.
- Used a separate first-party `codex queue` process to wake that actual durable
  thread and pass this checkpoint back into the same conversation.
- Strengthened queue evidence from CLI acknowledgement to passive queue and
  exact rollout-marker observation.
- Investigated a Windows hook command-shell failure, a transient store-lock
  collision, and a transient atomic metadata replacement failure found live.
- Performed read-only local VS Code/Codex inventory to find a real Remote-SSH
  target.

## 2. What was successfully implemented / derived / verified

### User-level trusted hooks

The installed acceptance configuration is:

```text
C:\Users\operator\.codex\hooks.json
```

Both hooks were loaded as user hooks, enabled, free of reported warnings/errors,
and manually trusted by the user. Trust was bound to the exact hook definition
hash in this environment:

```text
PermissionRequest sha256:e4a01f3a5216373b1ca9789dab2b271ea3633dbc24a95f80eb3fe72cce80c84f
Stop              sha256:151f430c8e03da7ba3c3127042b20ec635c98c41e489eb0c68610369448098ad
```

The tested Windows command is deliberately quote-free:

```text
C:\Users\operator\anaconda3\python.exe D:\projects\LocalCodexWatchDog\tools\codex_watchdog_hook.py --runtime D:\projects\LocalCodexWatchDog\.codex-watchdog\live-acceptance hook --grace-seconds 30 --poll-seconds 0.1 --test-mode
```

An earlier command with embedded quotes was listed/trusted but exited with code
1 before the Python handler began. The installed hook runner's outer
`cmd.exe /C` quoting reproduced the failure described in
[`openai/codex#38168`](https://github.com/openai/codex/issues/38168). Switching
to paths that need no quoting and a fully quote-free `commandWindows` fixed the
live path. Any definition change requires another explicit trust review.

The user hook was observed in the existing VS Code project and across several
fresh App Server processes/threads using the same local `CODEX_HOME`, which
establishes that it is not project-local. A complete VS Code application restart
and an unrelated new-project VS Code conversation were not run after final
trust, so persistence across those two exact scenarios is not claimed.

### Delivery evidence and contention hardening

- `QueueWakeDispatcher` records the exact native queue UUID, target thread,
  prompt digest, selected queue database, baseline revision, and rollout offset.
- Passive observation distinguishes `enqueued`, `consumed_or_started`, and
  `started`; `started` requires an exact new target-thread `UserMessage` marker
  paired with a new task/turn after the baseline.
- Duplicate dispatch IDs remain suppressed. `dispatching` and `uncertain` are
  never automatically resent.
- The Stop handler now retries only transient `StoreBusyError` while its grace
  deadline remains. A producer holding the store lock no longer makes an
  otherwise valid instruction fail open immediately. Re-entry confirmation has
  a separate bounded one-second contention window.
- Atomic JSON metadata replacement retries only Windows sharing/lock errors 5,
  32, and 33 for 10/20/40/80 ms, then re-raises. The path and payload remain
  identical. This does not retry `codex queue`, a continuation, or any model
  action.
- The live-stop watcher recognizes the new terminal
  `lock_busy_grace_expired_parked` outcome.

## 3. Current best implementation state / architecture

The local architecture feasibility gate is now positive:

```text
trusted native Stop hook
    -> privacy-limited audit
    -> bounded deterministic grace wait
    -> one identified decision:block continuation
    -> stop_hook_active confirmation and park

parked durable thread
    -> deterministic external producer (future Git/resume watcher)
    -> identified first-party codex queue message
    -> exact rollout evidence on the same thread
```

The supported hook plus first-party queue path is sufficient locally. A deep
extension-host/App Server shim and UI automation are not justified by the
evidence. This remains a feasibility PoC rather than the full watchdog service:
Git synchronization, notifications, workspace/locality management, and the
long-running state loop are still deliberately absent.

## 4. Observed runtime behavior / probe results

Observed environment:

| Component | Value |
| --- | --- |
| Codex CLI | `0.151.0-alpha.7.2` |
| VS Code extension | `openai.chatgpt` 26.825.51511 |
| Python | 3.9.12 |
| Local host | Windows |
| Live hook source | user |
| Approval reviewer | AutoReview |

### Actual already-open VS Code Stop and continuation

The dogfood target was the real watchdog development conversation:

```text
thread  <UUID>
turn    <UUID>
cwd     D:\projects\LocalCodexWatchDog
```

The ordinary Stop payload had `stop_hook_active=false` and an available latest
assistant message of 31 characters. The durable audit retained only digest
`dfe8950ba81b445ee7a5a4edfea2caac3edf8fe52e31268a8d8c8d56e5f9d200`,
not its text.

Instruction `cpx03-vscode-live-final-1` had prompt digest
`bbdad07566a19460740d55f97449a42c113f518fbd9e6626d452f099d9a2f3b1`.
It produced a `return_intent` in 155 ms. Rollout evidence shows the exact base
marker followed by the exact continuation:

```text
LIVE_VSCODE_BASE_CPX03_20260830
LIVE_VSCODE_CONTINUED_CPX03_20260830:LIVE_VSCODE_BASE_CPX03_20260830
```

The next Stop carried `stop_hook_active=true`. It confirmed the same intent,
moved it to `continued_confirmed`, and returned normally in 46 ms without
claiming another instruction. The final-output audit again retained only its
68-character length and digest
`8a62e03fc1d1715f1df83754a0b97cb0af0a98817bdad8e9de26e044aab501d9`.

### Actual already-open VS Code queue wake

After confirmation/park, a separate queue process dispatched instruction
`cpx03-vscode-resume-queue-1`, prompt digest
`dd7bbcb0d01357c584f86c063307a3bb3bb831da77637e6cda256ebeee4c9ddc`.
The native acknowledgement was:

```text
queue message  <UUID>
target thread  <UUID>
started turn   <UUID>
```

The exact queued wake prompt became the current user turn in this same
conversation. Passive re-observation durably promoted the record to `started`
at queue revision 10. No redispatch or duplicate occurred.

The first observer metadata write hit transient `PermissionError [WinError 5]`
after native delivery had already succeeded. A later passive observation
reconciled `enqueued -> started`. This was an observation-journal failure, not a
queue-delivery failure, and motivated the narrow atomic-replace retry above.

### Trusted installed-hook harness controls

The actual UI thread establishes one continuation, its loop-guard re-entry
park, and the same-thread queue wake. Repeated-cycle and no-instruction park
evidence came from separate trusted user-hook App Server controls and is not
misattributed to the UI run.

- Thread `<UUID>` completed two independent
  identified continuation cycles. Their return intents took 1,141 and 1,140 ms;
  their active re-entry confirmations took 30 and 32 ms. A third turn expired
  after 30,030 ms with no instruction and parked normally.
- Harness counters recorded zero model events during each deliberate grace
  wait. This proves that the watchdog itself did not poll the model while
  waiting/parked; no billing-level claim is made.
- An initial queue-adapter harness run exposed `lock_busy_failed_open` after
  1,108 ms while an instruction producer held the store lock. That run is
  retained as negative bug evidence. After the bounded contention fix, fresh
  thread `<UUID>` produced the exact single
  response `ADAPTER_QUEUE_WAKE_CPX03:ADAPTER_QUEUE_CONTEXT_CPX03` and promoted
  the matching queue record to `started`.
- An earlier fresh VS Code marker run before the quote-free command fix did not
  enter the handler. It is negative command-shell evidence, not a passing Stop
  test.

### Requested acceptance matrix

| Area | Result |
| --- | --- |
| A. Actual VS Code Stop event/payload | **Pass.** Exact thread/turn/cwd, output availability/digest, active flag, and timings were captured by the trusted user hook. |
| B. Actual VS Code short-grace continuation | **Pass.** One identified instruction continued the same turn and retained the immediately preceding marker. |
| C. Repeated-cycle loop guard | **Pass in trusted installed-hook harness; one cycle on actual UI.** Two independent harness cycles consumed once and avoided loops; the actual UI run independently confirmed one active re-entry park. |
| D. True no-instruction park | **Pass in trusted installed-hook harness.** A 30-second grace expired normally with zero harness-observed model events during the wait. A separate UI no-instruction expiry was not claimed. |
| Actual-thread `codex queue` | **Pass.** Exact queue UUID and rollout marker prove a new turn on the same actual VS Code thread with retained context. |
| PermissionRequest / AutoReview | **Pass for characterization.** The hook is pre-routing observation, not proof of human wait. |
| Remote-SSH subset | **Unavailable, not failed.** No real open Remote-SSH Codex target existed. |
| UI/shim fallback | **Not needed and not attempted.** Supported local mechanisms passed. |

### PermissionRequest / AutoReview characterization

At the audit cut, twenty-six actual user-hook records from 19:47:09Z through
20:06:36Z all had:

```text
outcome          permission_observed_pre_routing
hook duration    0 ms
permission_mode  default
stop_hook_active false
```

The hook returned `{}` and ordinary operations proceeded afterward under
`approvals_reviewer=auto_review`. Therefore the event fires before final
reviewer routing and does not imply that a human is waiting.

### Remote-SSH availability audit

Remote-SSH live acceptance was not testable because no real Remote-SSH VS Code
Codex conversation was open:

- `code --status` showed only the local `LocalCodexWatchDog`, `ProjectAlpha`, and
  `ProjectBeta` windows, with no remote extension host/Codex App Server.
- VS Code `windowsState.openedWindows` contained only local `file:///d:/...`
  folders.
- All 82 `source=vscode` threads in local `state_5.sqlite` had zero POSIX,
  `ssh-remote`, or `.vscode-server` working directories.
- Historical metadata referenced
  `vscode-remote://ssh-remote%2Bhpc-login.example.edu/home/operator/ProjectAlpha`,
  last updated 2026-08-28, but it was not open and exposed no current remote
  thread.

This is an environment-availability prerequisite, not evidence that remote
hooks or queue wake failed. No SSH connection, MFA setting, remote hook, or
remote state was changed.

## 5. Tests and sanity checks performed

- `python -m pytest` -> **52 passed**.
- `python -m black --check src tests tools` -> **18 files unchanged**.
- `python -m compileall -q src tools` -> passed.
- `examples/hooks.json` -> parsed successfully as JSON.
- Deterministic tests cover transient store contention during both claim and
  confirmation, plus the invariant that contention cannot claim an instruction
  at or after the grace deadline.
- Deterministic tests cover atomic-replace transient success, retry exhaustion
  with temporary-file cleanup, and immediate propagation of a nonmatching
  permission error.
- Live evidence covered real user-hook invocation, exact continuation identity,
  active re-entry confirmation, true harness park, queue acknowledgement,
  queue/rollout promotion, same-thread context retention, and AutoReview timing.

## 6. Unresolved technical issues / limitations

- Remote-SSH remains an important feasibility prerequisite. Hooks, runtime,
  queue database, Codex executable, and trust must be established inside the
  remote environment that owns a real remote thread.
- The strict local ordered sequence still lacks a no-instruction grace expiry
  on an actual VS Code UI conversation followed by its queue wake. The trusted
  installed-hook harness passed the same 30-second expiry behavior, and the
  actual UI thread separately passed continuation/active park and queue wake,
  but those are deliberately not presented as one ordered UI run.
- The installed local hooks are acceptance settings (`--test-mode`, 30-second
  grace) pointing into this checkout. They are not a packaged production
  installer or service configuration.
- `codex queue` is experimental in the tested alpha CLI. Its database/rollout
  evidence contract must be re-probed after Codex upgrades.
- Logical at-most-once intent deliberately favors duplicate suppression over
  guaranteed delivery. Crashes can still leave inflight/dispatching/uncertain
  state for manual reconciliation.
- Queue prompt text is briefly visible to same-user process inspection because
  the installed CLI accepts it as an argument. Durable journals retain only
  hashes/lengths.
- Git preservation/push/fetch/fast-forward, remote update detection,
  notification, debouncing, workspace state, and service lifecycle remain
  unimplemented by checkpoint scope.

## 7. Exact files modified or created

Repository changes across checkpoint 3 (from reviewer commit `893b5b1`):

```text
README.md
architecture.md
examples/hooks.json
src/codex_watchdog/cli.py
src/codex_watchdog/queue_wake.py
src/codex_watchdog/stop_hook.py
src/codex_watchdog/storage.py
tests/test_hook_config.py
tests/test_queue_wake.py
tests/test_stop_hook.py
tests/test_storage.py
tests/test_wait_for_live_stop.py
tools/run_appserver_hook_probe.py
tools/run_native_hook_probe.py
tools/wait_for_live_stop.py
doc/Progress/progress_2026_08_30_cpx03.md
```

Checkpoint commits already pushed before this closing commit:

```text
e200007  watchdog: prepare trusted live acceptance
e771f39  watchdog: harden Windows hook commands
```

External/local-only state used for acceptance:

```text
C:\Users\operator\.codex\hooks.json
D:\projects\LocalCodexWatchDog\.codex-watchdog\live-acceptance\
C:\Users\operator\.codex\queue_1.sqlite and matching rollout files
```

Runtime evidence and first-party Codex state were not added to Git.

## 8. Recommended next step - not performed

First close the strict local evidence gap with one expendable actual VS Code
conversation: allow a no-instruction grace expiry, then wake that exact parked
thread once through the queue and verify its preceding marker. Do not change the
architecture if this merely reproduces the already separated passing results.

Then open one real VS Code Remote-SSH Codex conversation, install the same
minimal hook in that environment's separate remote `CODEX_HOME`, manually
review/trust its exact definition, and repeat only:

1. one short-grace Stop continuation with retained context;
2. one active re-entry confirmation;
3. one no-instruction true park; and
4. one first-party queue wake executed in the remote thread's owning locality.

If those pass, checkpoint 4 can begin the ordinary deterministic Git
preserve/push/fetch/fast-forward loop, remote update detection, Slack-primary
plus SMTP-fallback notification, resume disposition, and dogfood service loop.
If the remote subset fails after correct locality and trust, record that narrow
blocker before changing the local hook/queue architecture. This next work was
not performed.

## comment

## Checkpoint 4 — Local architecture accepted; close Remote-SSH gate and begin ordinary watchdog engineering

The checkpoint-3 result is satisfactory. The reviewer accepts the **local hook + first-party `codex queue` architecture** as the working architecture. The live evidence on the actual already-open VS Code conversation is sufficient to stop investigating UI automation, deep App-Server attachment, or extension-host shims unless a future concrete failure forces us back to them.

Do not spend another checkpoint trying to make the local feasibility evidence more aesthetically complete. The missing single ordered UI sequence (`no-instruction expiry -> park -> queue wake`) is low-risk evidence cleanup because its individual components have already passed under trusted live conditions. Re-run it only if useful while testing production behavior; it is no longer an architecture gate.

### 1. First priority: close the Remote-SSH feasibility gate, but do not over-investigate

When a real VS Code Remote-SSH Codex conversation is available, repeat only the minimum live sequence in the **remote owning locality**:

1. install/use the user-level hook in the remote `CODEX_HOME`;
2. manually inspect/trust the exact remote hook definition as required by Codex;
3. one short-grace Stop continuation retaining an unmistakable context marker;
4. one `stop_hook_active` re-entry confirmation;
5. one no-instruction grace expiry to a true park; and
6. one `codex queue --thread ...` wake executed in the remote environment, proving the same durable remote thread resumes with prior context.

Do not weaken SSH/MFA, do not make Windows Git operate on a remote repository path, and do not substitute a fresh unrelated local App Server for this test.

If no real Remote-SSH Codex conversation is available during the checkpoint, record that prerequisite briefly and **do not block all other implementation work waiting for it**. Proceed with the ordinary local/watchdog engineering below and leave the remote acceptance item explicitly pending.

If the correctly-localized remote hook/queue path fails, isolate and report the narrow remote-only blocker before changing the already-accepted local architecture.

### 2. Freeze the core continuation architecture

Treat the following as the baseline design unless contradicted by new live evidence:

```text
SHORT STOP
Codex Stop
-> trusted native Stop hook
-> bounded grace window (production default approximately 10 minutes)
-> identified instruction arrives
-> one `decision:block` continuation
-> `stop_hook_active` confirmation
-> park

LONG STOP / PARKED
external deterministic watcher sees new work
-> `codex queue --thread <exact-thread-id>`
-> identified queued message
-> same durable thread starts a new turn
```

Keep the existing at-most-once bias. An uncertain queue send must never be blindly retried. Preserve exact queue-message IDs / prompt digests / thread IDs and passive delivery evidence.

### 3. Start implementing the ordinary deterministic watchdog loop

Once the remote probe is complete or temporarily unavailable, move out of feasibility research and implement the actual watchdog in small, testable pieces.

The next useful components are:

- tracked workspace/session registry;
- deterministic polling/service loop;
- safe Git state inspection;
- conservative preserve/commit/push behavior;
- `git fetch` plus topology checks;
- remote-update detection;
- `git pull --ff-only` only when safe;
- dispatch of the fixed mechanical progress-comment wake prompt through `codex queue` when the session is parked;
- short-stop instruction submission when still inside the hook grace window;
- `resume_prompt.md` producer/claim lifecycle;
- durable state and notification debounce.

The watchdog must remain a mechanical sensor/courier. It does not interpret research content and does not decide whether a scientific `## comment` is correct. For a remote Git update, its job is only to synchronize safely and tell Codex to inspect the latest progress report for a new unprocessed comment.

### 4. Git safety remains strict and fail-closed

Preserve the previously agreed Git policy:

- always fetch before topology-sensitive decisions;
- no force push;
- no hard reset;
- no automatic merge or rebase;
- no automatic conflict resolution;
- fail closed on detached HEAD, merge/rebase/cherry-pick/bisect/revert state, conflicts, missing upstream, divergence, auth failure, index lock, or ambiguous topology;
- never modify Git trust/config/credentials/hooks/signing to make an operation pass;
- never run local Windows Git against a Remote-SSH repository path.

For untracked files, keep the conservative policy unless there is a compelling reason to change it: do not blindly `git add -A`. Automatically preserve tracked changes; surface untracked files separately so secrets are not accidentally committed.

### 5. Notifications: Slack primary, email minimum fallback

Notification transport is no longer a research problem. Reuse a mature/simple pattern rather than inventing a large subsystem.

Desired eventual behavior:

```text
Slack primary
SMTP/email fallback
```

At minimum notify on meaningful state transitions such as:

- Codex reached a real Stop/park;
- watchdog needs user attention;
- Git could not be preserved/synchronized safely;
- queue wake delivery became uncertain;
- remote/local watchdog error that prevents progress.

Avoid noisy repeated notifications for the same unchanged state. Real state transitions should bypass debounce.

Do not treat every `PermissionRequest` hook event as a human wait: checkpoint 3 established that it is a pre-routing observation under AutoReview.

### 6. Productionize the hook configuration carefully

The current local hook is an acceptance configuration (`--test-mode`, 30-second grace). Move toward a production configuration with a default short-stop grace around 600 seconds, configurable by the user.

Preserve the Windows quoting lesson from checkpoint 3. Do not silently create a fragile hook definition containing paths that the installed `cmd.exe /C` runner cannot execute. If packaging/installing a stable quote-free launcher path is the cleanest solution, use that approach and document it.

Hook trust remains an explicit user action. Do not bypass or mutate Codex trust state automatically.

### 7. Finish `resume_prompt.md` lifecycle when convenient

The already-agreed semantics remain:

```text
resume_prompt.md
-> atomic claim to inflight
-> deliver once
-> Codex emits exactly one disposition:
   RESUME_PROMPT_DISPOSITION: DISCARD
   or
   RESUME_PROMPT_DISPOSITION: ARCHIVE
-> watchdog performs deletion/archive
```

If delivery or disposition correlation is uncertain, retain the inflight file. Do not guess or resend automatically.

### 8. Dogfood as soon as the service loop is usable

Once the watchdog can reliably observe this repository, preserve Git state, notify, and wake via the accepted hook/queue path, use it to monitor the Codex session implementing LocalCodexWatchDog itself.

This is an acceptance goal, not permission to bypass the checkpoint discipline. Continue to stop after at most two hours of active work, write the standard progress report, recommend the next step but do not perform it, and wait for review.

### 9. Checkpoint-4 stopping rule

Do not try to finish the entire product in one run. Prioritize, in order:

1. Remote-SSH minimal live acceptance if a real target is available;
2. otherwise/afterward, the smallest useful deterministic service-loop + safe-Git slice;
3. Slack/email only to the extent needed for a real dogfood run;
4. dogfood one real stop/park/wake cycle if enough pieces are ready.

Stop at or before two hours of active work and report exactly what is genuinely live-tested versus unit-tested. The reviewer is satisfied with the core local architecture; the objective now is to turn it into a boring, reliable watchdog rather than continue searching for a more clever architecture.
