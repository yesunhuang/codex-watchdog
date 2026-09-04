# Progress report - 2026-08-31 checkpoint 4 Remote-SSH gate and Git sensor

Start: 2026-08-31 01:19 CDT

End: approximately 2026-08-31 02:15 CDT

Elapsed: approximately 56 minutes of active work.

Scope: incorporate the approved checkpoint-4 comment, attempt the minimum
Remote-SSH prerequisite without changing SSH or trust state, add a conservative
user-hook installer, and implement the smallest useful process-local workspace
registry plus deterministic fail-closed Git sensor/service slice.

Not attempted: automatic staging/commit/push by the service, pull or
fast-forward mutation, Codex ACTIVE/NOT_ACTIVE inference, service scheduling,
Slack/SMTP delivery, resume-prompt disposition actions, or a service-driven
queue wake. No Remote-SSH acceptance result is claimed without remote runtime
evidence. No `## comment` section was created.

## 1. What was attempted

- Read checkpoint 3 and its appended reviewer comment as the approved scope.
- Rechecked the [official OpenAI Hooks documentation](https://learn.chatgpt.com/docs/hooks)
  and preserved exact-definition manual trust as an explicit user action.
- Inspected the real open Remote-SSH target and determined its owning workspace,
  host, Codex App Server, and execution locality without attaching a shim or
  changing SSH/MFA.
- Added a conservative, portable user-hook renderer/installer suitable for
  invocation inside either a local or remote owning environment.
- Implemented an atomic process-local workspace/session registry.
- Implemented fetch-before-classification Git inspection and a deterministic
  one-pass service that persists per-workspace observations.
- Added CLI entry points, documentation, unit/integration coverage, and two real
  dogfood sensor passes against this repository.
- Committed and automatically pushed the installer and implementation slices.

## 2. What was successfully implemented / derived / verified

### Conservative user-hook installer

`tools/install_user_hooks.py` renders production hooks with a 600-second default
grace or explicitly requested test settings. Installation is deliberately
one-way and conservative:

- it creates a missing `CODEX_HOME/hooks.json` atomically;
- an exactly equivalent existing document is idempotent;
- a symlink, unreadable document, or different existing configuration is
  refused rather than overwritten;
- paths and commands are rendered in the target process locality;
- Windows commands retain the quote-free safety rule; and
- the tool never reads, writes, or fabricates Codex hook trust state.

### Process-local workspace registry

The schema-1 registry binds:

```text
workspace_id
canonical local repo_root
exact Codex session/thread UUID
execution_locality = process_local
registration timestamp
```

Writes are locked and atomic. Exact repeats do not rewrite registration time.
Workspace-ID and repository-root collisions fail closed. Listing is sorted by
workspace ID. URI, Remote-SSH, SCP-style, UNC, nonexistent, and noncanonical
stored paths are rejected or reported without allowing Windows Git to operate
on a remote repository path.

### Fail-closed Git sensor

`LocalGitAdapter.observe()` uses argv arrays, no shell, bounded timeouts,
disabled terminal/askpass interaction, and privacy-limited errors. It:

- verifies the exact registered repository, Git directory, and common
  directory;
- detects operation markers, conflicts, index/ref locks, detached or unborn
  HEAD, shallow/graft/replace layouts, submodules, and sparse checkout;
- records tracked dirt and untracked-file presence separately without
  persisting filenames;
- validates one explicit remote/upstream mapping;
- fetches before topology classification;
- re-observes repository identity, status, locks, branch, upstream, and object
  IDs after fetch and again after topology calculation;
- computes topology from captured exact 40- or 64-character object IDs; and
- reports `equal`, `remote_ahead`, or `local_ahead`, while retaining
  `topology=diverged` with a hard `diverged` blocker.

It never stages, commits, pushes, pulls, merges, rebases, resets, resolves a
conflict, or changes `HEAD`, the index, or the worktree. Fetch can update
remote-tracking refs and `FETCH_HEAD`, which is documented explicitly.

### Deterministic one-pass service

`RunOnceService` acquires one nonblocking service lock, snapshots the validated
registry once, processes sorted workspaces sequentially, and atomically writes:

```text
runtime/service/observations/<sha256(workspace_id)>.json
```

One adapter or persistence failure is hash-only and does not prevent later
workspaces from being attempted. Ordinary Git blockers are successful sensor
observations, not service crashes. Stable transition fingerprints exclude
observation timestamps. The service imports no queue adapter and performs no
Codex wake or worktree mutation.

CLI commands now include:

```text
workspace-add --workspace ... --repo ... --thread ...
workspace-list
service-once
```

## 3. Current best implementation state / architecture

The accepted continuation architecture remains unchanged, and the first
ordinary watchdog layer now exists beneath it:

```text
SHORT STOP
trusted native Stop hook
    -> bounded grace
    -> one identified continuation
    -> stop_hook_active confirmation
    -> park

PARKED WAKE
deterministic producer
    -> first-party codex queue for the exact thread

CHECKPOINT-4 SENSOR
process-local workspace registry
    -> globally locked sorted service-once pass
    -> noninteractive Git fetch and fail-closed topology observation
    -> atomic per-workspace state and transition fingerprint
```

The sensor is intentionally not yet joined to the queue path. There is no
reliable ACTIVE/PARKED provider in the service, so treating a Git observation
as permission to mutate or wake would be unsafe. A future mutation layer must
consume only fresh, unblocked observations and perform another exact state
revalidation immediately before any change.

## 4. Observed runtime behavior / probe results

### Remote-SSH target

A real target was available:

```text
host       gpu-lab-personal
workspace  /home/operator/LocalCodexWatchDog
OS         Linux arm64 6.17
Codex      remote openai.chatgpt App Server
CODEX_HOME expected default /home/operator/.codex (not directly evidenced)
```

VS Code showed the remote extension host, remote Codex App Server, and an open
remote bash terminal. The existing VS Code SSH transport is a protocol/SOCKS
channel, not a reusable command shell. A separate read-only `ssh -o
BatchMode=yes` check failed cleanly with normal authentication required. No
password was requested, no MFA/key/configuration was weakened, and no remote
file or trust state was changed through that path.

The exact target-local installer command and manual `/hooks` trust step were
provided. At report cut there was no inspectable remote hook runtime, new remote
thread ID, `stop_hook_active` audit, grace-expiry audit, or remote queue receipt.
Therefore the Remote-SSH sequence is **pending, not failed**, and no local
Windows result is substituted for it.

### Local service dogfood

The ignored runtime was:

```text
.codex-watchdog/cpx04-service-dogfood/
workspace local-watchdog-cpx04
thread    <UUID>
```

Before the implementation commit, the service fetched and reported:

```text
HEAD/upstream  b8aceec / b8aceec
topology       equal
dirty_tracked  true
untracked      true
blockers       none
```

After commit `ce799aa` was pushed, a second pass reported:

```text
HEAD/upstream  ce799aa / ce799aa
topology       equal
dirty_tracked  false
untracked      false
blockers       none
```

Both passes returned service status `ok`, persisted one complete atomic
observation, and changed the transition fingerprint when the repository state
changed. Neither pass moved `HEAD`, the index, or the worktree.

## 5. Tests and sanity checks performed

- `python -m pytest -q` -> **103 passed, 1 skipped**.
- `python -m pytest --collect-only` -> **104 tests collected**.
- Registry/Git/service focused integration run -> **47 passed**.
- `python -m black --check src tests tools` -> **28 files unchanged**.
- `python -m compileall -q src tools tests` -> passed.
- `git diff --cached --check` -> passed before the implementation commit.
- Bare-remote Git tests cover equal, remote-ahead, local-ahead, divergence,
  fetch failure, detached/missing upstream, operation/lock state, strict object
  IDs/counts, rename parsing, hostile environment, and post-fetch races.
- Service tests cover sorted processing, atomic hashed observation files,
  stable fingerprints, lock exclusion, missing repositories, hash-only
  adapter/write failures, workspace isolation, and CLI isolation from queue
  initialization.
- Installer and registry tests cover exact idempotence, collision/refusal
  behavior, path locality, malformed durable state, and trust non-mutation.

## 6. Unresolved technical issues / limitations

- Remote-SSH Stop continuation, active re-entry, true park, and same-thread
  remote `codex queue` wake remain unverified in the remote owning locality.
- The service has no Codex ACTIVE/NOT_ACTIVE/UNKNOWN adapter, so it cannot yet
  decide that a session is parked or safely dispatch a wake.
- Git preservation, tracked-only staging/commit, normal push, safe
  fast-forward, and pre-mutation revalidation are not implemented.
- The current sensor fetch may update remote-tracking refs and `FETCH_HEAD`.
  Subprocess output capture is not size-bounded, and extra integration coverage
  for slash-containing remote names and deleted upstream branches is deferred.
- Slack/SMTP notification and transition debounce, background scheduling,
  configuration loading, and resume disposition/archive actions remain absent.
- A different existing user hook configuration still requires a human merge,
  and every new exact hook definition still requires explicit `/hooks` review
  and trust.
- Runtime files contain absolute paths and stable session identifiers; prompt
  stores can contain plaintext. The runtime remains gitignored and should be
  protected with user-only filesystem permissions.

## 7. Exact files modified or created

Checkpoint-4 repository changes after reviewer commit `2781968`:

```text
README.md
architecture.md
pyproject.toml
src/codex_watchdog/cli.py
src/codex_watchdog/git_adapter.py
src/codex_watchdog/service.py
src/codex_watchdog/workspace_registry.py
tests/test_git_adapter.py
tests/test_install_user_hooks.py
tests/test_service.py
tests/test_service_cli.py
tests/test_workspace_registry.py
tests/test_workspace_registry_cli.py
tools/install_user_hooks.py
doc/Progress/progress_2026_08_31_cpx04.md
```

Checkpoint commits pushed before this closing report commit:

```text
b8aceec  watchdog: add conservative user hook installer
ce799aa  watchdog: add fail-closed Git sensor loop
```

Local-only evidence, not added to Git:

```text
D:\projects\LocalCodexWatchDog\.codex-watchdog\cpx04-service-dogfood\
```

No remote runtime evidence was copied into or fabricated in the repository.

## 8. Recommended next step - not performed

First complete only the minimum Remote-SSH owning-locality sequence on the
already-open target: install or conservatively merge the remote user hook,
manually inspect/trust its exact definition, prove one retained-marker Stop
continuation, confirm `stop_hook_active`, allow one true grace expiry, then run
one remote-local `codex queue --thread <exact-remote-thread>` wake and retain its
receipt/audit evidence.

Then add a reliable, independently testable Codex ACTIVE/NOT_ACTIVE/UNKNOWN
adapter and freshness policy. Only after that state gate exists should the
service gain the smallest mutation slice: captured-state revalidation, safe
tracked-change preservation, normal non-force push, and exact-OID
fast-forward-only synchronization. Notification/debounce and queue dispatch can
then be connected to real state transitions. None of those next-step mutations
or wakes were performed in this checkpoint.

## comment

## Checkpoint 5 — Build a runnable MVP now; dogfood first, harden from real failures

Checkpoint 4 is approved. Change of priority: **do not spend this cycle extending the architecture or proving every edge case. Produce a runnable end-to-end watchdog as quickly as possible and start using it.** We will harden it from real logs and failures while it runs.

The acceptance criterion for this checkpoint is not architectural completeness. It is:

> A user can start the watchdog, register this repository/thread, leave it running, and observe the basic Stop → notify → Git sync → park/wake loop work end to end on `LocalCodexWatchDog` itself.

### 1. Freeze architecture; no more feasibility work unless something actually breaks

Use what is already proven:

- native trusted `Stop` hook for stop/short-grace continuation;
- first-party `codex queue --thread ...` for later wake;
- current workspace registry;
- current Git sensor.

Do **not** implement a general ACTIVE/NOT_ACTIVE/UNKNOWN provider, App-Server shim, UI automation, elaborate event model, or more architecture layers in this cycle.

For the MVP, the native queue's `start_turn_if_idle` behavior plus exact thread ID and existing duplicate-suppression are sufficient protection for wake delivery. If a concrete failure appears during dogfood, record it and fix that failure rather than anticipating every possible one.

Remote-SSH validation is desirable but **must not block the MVP**. If remote trust/auth requires user action, leave it pending and finish the local runnable system first.

### 2. Make one command run the watchdog continuously

Implement the simplest foreground service, for example:

```text
codex-watchdog run --interval 300
```

or an equivalent command.

It may simply call the existing service logic every five minutes in one process. No Windows service installer, daemon framework, scheduler abstraction, distributed coordinator, or sophisticated lifecycle manager is required yet.

Minimum behavior:

```text
start
-> load registered workspaces
-> every ~5 min inspect Git/state
-> perform the minimal safe action
-> log/audit what happened
-> sleep
-> repeat
```

Ctrl-C shutdown is enough for this checkpoint.

### 3. Implement only the Git mutations needed for the real workflow

Minimal safety rules are enough:

- `git fetch` before topology-sensitive actions;
- if tracked files are dirty, preserve **tracked changes only** (`git add -u`), commit mechanically, and never auto-stage untracked files;
- normal non-force push only;
- if remote is ahead and the local worktree is clean, use fast-forward-only synchronization;
- if histories diverge, conflicts/merge/rebase are in progress, HEAD is detached, credentials fail, or an operation is ambiguous: **do nothing destructive, notify/log, and move on**;
- no force push, hard reset, auto merge/rebase, conflict resolution, or deletion of untracked files.

Do not add elaborate transaction/state machinery beyond what is necessary to avoid obvious destructive behavior. Re-check the few critical facts immediately before mutation, then execute.

### 4. Wire notifications with the smallest implementation that works

Get a real notification path working in this cycle.

Preference:

```text
Slack primary
email/SMTP fallback capability
```

If Slack credentials are available, use Slack for dogfood. If not, make configuration straightforward and at least exercise a real email/SMTP or other already-available notifier path. Do not block the whole MVP on perfect multi-channel configuration.

Notify on useful human-attention events, especially:

- Codex stops/parks;
- Git sync/push/auth fails;
- divergence/conflict/unsafe state;
- untracked files are present and therefore were not preserved automatically;
- wake delivery is uncertain.

A simple persisted last-event fingerprint is enough to suppress identical spam. Do not build a notification subsystem beyond this.

### 5. Connect the already-proven wake path

When the watchdog detects a remote update/new instruction condition for a registered exact thread, use the existing `codex queue` path and its instruction IDs/duplicate suppression.

Do not build a new state adapter first. If the thread is already active, rely on the first-party queue behavior rather than trying to attach to the live App Server. If queue behavior creates a real workflow problem during dogfood, fix that observed problem afterward.

Keep the wake prompt mechanical: tell Codex to inspect the latest progress report for a new unprocessed `## comment` and continue only if actionable.

`resume_prompt.md` may use the same queue path. Keep inflight files rather than guessing if disposition handling is not ready.

### 6. Dogfood immediately — this is the main deliverable

As soon as the minimum loop can run, turn it on for `LocalCodexWatchDog` itself. Do not wait until every planned component is polished.

Try to exercise a realistic sequence such as:

```text
watchdog running
-> Codex produces/stops
-> hook records stop and notification fires
-> local tracked changes are preserved and pushed
-> user/ChatGPT changes progress comment remotely
-> watchdog fetches / fast-forwards
-> watchdog queues the fixed wake prompt
-> same Codex thread continues
```

If some step fails, inspect the audit/log, fix the concrete bug, and rerun. **This run–observe–fix loop is preferred over adding speculative abstractions or dozens of synthetic edge-case tests.**

Keep useful audit information so failures can be reconstructed, but do not over-design the audit schema.

### 7. Testing priority

Keep the existing test suite green, and add targeted tests for code you actually add. Do not spend most of the checkpoint expanding combinatorial edge-case coverage.

A live dogfood cycle that exposes and fixes two real bugs is more valuable at this stage than another large batch of hypothetical tests.

### 8. Stop rule

Use the remaining checkpoint time to get as close as possible to a genuinely running MVP. If a noncritical feature is incomplete, leave it incomplete and document it rather than delaying the runnable loop.

Continue the existing maximum-two-hours-active-work rule. At the next checkpoint, report primarily:

1. how to launch the MVP;
2. what end-to-end dogfood path actually ran;
3. what real bugs were encountered and fixed;
4. what still prevents unattended use, if anything.

Then stop for review.