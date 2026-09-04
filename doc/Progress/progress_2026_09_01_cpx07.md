# Progress report - 2026-09-01 checkpoint 7 automatic live VS Code discovery

Start: 2026-09-01 03:34 CDT

Implementation snapshot: 2026-09-01 05:24 CDT

Elapsed at snapshot: approximately 110 minutes of active work.

Automatic publication and foreground migration: 2026-09-01 05:29 CDT.

Scope: remove the normal requirement to enter a workspace name, repository
path, and Codex thread UUID; discover eligible open stable VS Code windows and
their exact currently owned user threads; integrate that set into every sensor
and foreground cycle; preserve the Remote-SSH process-locality boundary; test
against this repository; then commit and push automatically.

Not attempted: a VS Code extension or supported public session-enumeration API,
VS Code Insiders/other user-data roots, automatic inspection of Codex state on a
Remote-SSH host from Windows, background service installation, or a general
Codex activity provider. No prompt, chat label, credential, or notification
secret was selected or persisted by discovery.

## 1. What was attempted

- Replaced registry-only operation with per-cycle live VS Code discovery by
  default while retaining an explicit manual-override escape hatch.
- Correlated persisted window metadata with the live VS Code process tree,
  exact workspace storage, window-scoped Codex resources, Codex's user-thread
  database, held writer locks, and privacy-safe stream-owner lifecycle markers.
- Added immediate exact workspace/session revalidation before Git mutations and
  queue wakes.
- Added exclusion, discovery-inspection, manual-only, and manual-override
  removal CLI paths.
- Ran adversarial review, focused/full tests, compilation/format checks, and
  live discovery plus sensor probes against this repository.

## 2. What was successfully implemented / derived / verified

Normal local use no longer needs `workspace-add`. `workspace-discover`,
`service-once`, and `run` discover every exactly mappable live window in the
current stable VS Code user-data instance. Each loop cycle refreshes the set,
so opening, closing, or changing a window takes effect without restarting the
watchdog.

Automatic targeting is fail-closed. A local candidate must have all of:

1. a current `code --status` extension-host process and its Codex App Server;
2. the newest matching VS Code window log whose latest extension-host lifecycle
   is the exact current PID and has not terminated;
3. one exact live workspace-storage mapping;
4. one canonical `openai-codex://route/local/<uuid>` resource selected from the
   window-scoped `agentSessions.model.cache`;
5. one unarchived `source=vscode`, `thread_source=user` database row with an
   exact canonical workspace/repository working-directory match;
6. a currently held exact thread writer lock; and
7. a later exact-thread stream role of `owner` in that window's current App
   Server generation.

Zero, multiple, malformed, stale, terminated, unmatched, or changed candidates
remain unresolved. Historical duplicate storage is harmless when exactly one
mapping is live. Closed `lastActiveWindow` entries are not counted as live.
Owner parsing resets on App Server respawn and on malformed/partially appended
state-change lines.

Discovery selects only provider/resource fields from the per-window SQLite JSON
cache and only exact lifecycle tokens from Codex logs. It does not select or
persist labels, prompts, chat bodies, arbitrary log lines, or raw process
output. The durable snapshot contains workspace/session identifiers, canonical
paths, localities, status, and reason codes.

## 3. Current best implementation state / architecture

```text
persisted VS Code window entry
            +
current code --status extension host / Codex App Server
            +
current window log generation and exact storage key
            |
            v
window-scoped openai-codex resource IDs
            +
exact unarchived VS Code user row / cwd / held writer lock
            +
current-generation exact-thread owner marker
            |
            v
ephemeral effective workspace (deterministic vscode-* ID)
            |
            +--> re-resolve before every Git mutation or queue wake
```

A durable manual registration still overrides automatic resolution for the
same canonical repository. `workspace-remove` removes that pin and returns the
repository to automatic discovery. Manual registrations remain independently
valid if VS Code discovery is unavailable. `CODEX_HOME` and `--codex-home` are
honored consistently by discovery, evidence, and queue state.

## 4. Observed runtime behavior / probe results

An empty-registry live probe found exactly two current VS Code windows:

- this local repository was automatically mapped to its exact current user
  thread and a deterministic `vscode-*` workspace ID; and
- the open Remote-SSH repository was reported as `remote_agent_required` and
  was not passed to local Windows Git, locks, Codex state, or queue operations.

A previously open second local window was closed during development. Persisted
state initially contained it, but the strengthened live-process correlation
excluded it; the final count remained two rather than three. This is direct
evidence that discovery is not merely enumerating historical workspaceStorage.

The integrated `service-once` live probe automatically processed one local
workspace, completed its normal noninteractive Git observation, persisted the
observation plus discovery summary, and returned `status=ok`. No manual
workspace/thread input was supplied.

After publication, the legacy `local-watchdog-mvp` manual override was removed
from `.codex-watchdog/live-acceptance`. A replacement foreground watchdog was
launched from a separate session inside the already configured VS Code
PowerShell host, so its notification environment was inherited without reading
or printing any value. Its first durable snapshot reported two live windows,
one automatically tracked local workspace, and only
`remote_agent_required`; the manual registry count was zero. The replacement
was verified running before the old foreground PID was stopped.

## 5. Tests and sanity checks performed

- Final full repository suite: **229 passed, 2 skipped** in 212.06 seconds.
- Final focused discovery/registry/MVP/service/CLI suite after all hardening:
  **91 passed**.
- Live `workspace-discover` with an empty manual registry: partial only because
  the one Remote-SSH window correctly requires a remote agent; one local
  workspace tracked automatically.
- Live `service-once`: one auto-discovered local workspace observed and
  persisted successfully.
- `python -m compileall -q src tests tools` -> passed.
- Black formatting and `git diff --check` -> passed.
- CLI help exposes `workspace-discover`, `workspace-remove`, `--exclude`, and
  `--manual-only`.
- Verified implementation commit `49fe0564c69d0dd9c7557faf06fa0dcc6abbf531`
  was pushed normally to `origin/main` before the live foreground migration.

## 6. Unresolved technical issues / limitations

- VS Code/OpenAI do not currently expose a documented public API used here to
  enumerate all windows and their active Codex sessions. The adapter relies on
  private stable-VS-Code/Codex storage and log layouts observed on this
  installation, is deliberately narrow, and fails closed after incompatible
  upgrades.
- Automatic discovery currently targets the default stable VS Code user-data
  instance on Windows. Insiders, portable, and explicitly separate user-data
  roots require future adapters or manual registration.
- A Windows process can identify an open Remote-SSH window but cannot prove its
  remote thread ownership or safely run remote Git/queue operations. Run a
  watchdog in the remote locality with a process-local manual registration and
  `--manual-only`.
- A held writer lock proves a thread is loaded, not that a turn is inactive;
  the separate exact recent parked-evidence gate remains mandatory for every
  automatic Git mutation.
- The foreground watchdog remains user-started rather than an installed
  background service.

## 7. Exact files modified or created

```text
README.md
architecture.md
config.example.toml
src/codex_watchdog/cli.py
src/codex_watchdog/mvp_service.py
src/codex_watchdog/service.py
src/codex_watchdog/workspace_discovery.py                 (new)
src/codex_watchdog/workspace_registry.py
tests/test_mvp_service.py
tests/test_service.py
tests/test_service_cli.py
tests/test_workspace_discovery.py                         (new)
tests/test_workspace_registry.py
doc/Progress/progress_2026_09_01_cpx07.md                 (new)
```

Ignored runtime evidence under `.codex-watchdog/` was intentionally not staged.
No credential, prompt text, chat label, raw VS Code process listing, or raw log
line is included in the tracked change set.

## 8. Recommended next step - not performed

After this checkpoint is reviewed and published, add an explicit adapter for a
second VS Code user-data root only when there is a real need (for example
Insiders or portable mode), and perform Remote-SSH acceptance from a watchdog
running inside that remote owning locality. Do not weaken the current exact
correlation rules merely to increase discovery coverage.

## comment

## Checkpoint 8 — Simplify the real dogfood path: latched PARKED state, Codex-owned inbound synchronization, and useful Stop email output

Checkpoint 7 is accepted. The automatic discovery work is sufficient for now; do **not** expand discovery coverage or add new framework layers in this checkpoint.

Real dogfood exposed three concrete workflow problems. Fix these directly and keep the architecture smaller.

### 1. `PARKED` is a latched state, not a 15-minute lease

A real workflow produced:

```text
Hook outcome: grace_expired_parked
...
Action: remote_update
No Git mutation or wake was attempted because recent parked evidence for the exact registered Codex thread was not safe.
Reason: stale_parked_stop
```

This is a design bug. The user may naturally spend tens of minutes or hours discussing a report before posting the next `## comment`. A terminal Stop does not become unsafe merely because time passed.

Remove `_PARKED_EVIDENCE_MAX_AGE_SECONDS` / `stale_parked_stop` as a blocking condition. Do **not** merely increase the TTL.

Use event semantics instead:

```text
trusted terminal Stop for exact thread/workspace
    -> PARKED is established
    -> PARKED remains valid indefinitely
    -> invalidate only when later positive/ambiguous evidence shows the thread may have become active or ownership changed
```

Continue to invalidate/fail closed on real later evidence such as:

- a later exact-thread `task_started` / user turn / rollout activity;
- a later queue wake/start or ambiguous queue ordering;
- exact thread/workspace/session ownership changing;
- unresolved/ambiguous rollout correlation;
- incompatible/malformed evidence where ordering cannot be trusted.

Age may be retained only as a diagnostic field (`parked_age_seconds`), not as permission to block a wake.

### 2. Simplify inbound Git: the watchdog must **not synchronize or mutate the worktree**

The user does not want watchdog-owned inbound Git mutation. The watchdog should transport state changes and wake the correct Codex thread; Codex should own the actual repository synchronization because it has the task context needed to resolve ordinary Git situations intelligently.

Freeze the direction split as follows:

```text
Codex -> GitHub:
    Codex stops
    -> watchdog may preserve tracked changes + reviewed new progress_*.md
    -> mechanical commit
    -> normal non-force push
    -> notification

GitHub -> Codex:
    watchdog detects remote branch OID changed
    -> watchdog does NOT pull / merge / rebase / ff / reset / modify HEAD/index/worktree
    -> watchdog queues the exact Codex thread
    -> Codex inspects the current Git state and synchronizes the repository itself
    -> Codex reads the latest progress report / new `## comment`
    -> Codex continues if actionable
```

For remote-update detection, prefer a read-only remote-OID check such as `git ls-remote` if practical, so the inbound detector does not even need to update local remote-tracking refs. A normal `fetch` is acceptable only if needed for a minimal implementation, but **no watchdog path may update the checked-out branch, index, or worktree on inbound remote changes**.

Keep exact remote-OID deduplication: one remote OID should produce at most one queue instruction unless the prior dispatch is explicitly unresolved. Do not resend blindly.

The remote-update queue prompt should be mechanical but should leave synchronization strategy to Codex. It should tell Codex, before continuing task work, to:

```text
1. inspect the current local/remote Git state and synchronize the repository safely;
2. handle ordinary synchronization and straightforward conflicts autonomously when the correct resolution is clear and consistent with the current task;
3. do not discard work, hard-reset, force-push, overwrite remote history, delete unknown files, or make an arbitrary conflict choice merely to hide uncertainty;
4. if synchronization requires a substantive research/implementation decision, or the correct conflict resolution is genuinely unclear, stop and report the exact blocker/decision needed instead of guessing;
5. after synchronization, inspect the latest progress report for a new unprocessed `## comment`;
6. if there is an actionable comment, follow it and continue;
7. if there is no actionable new comment, remain idle.
```

Do **not** hard-code `git pull --ff-only` as the required strategy. Codex may choose an appropriate normal Git workflow (for example fetch/pull/merge/rebase or explicit conflict resolution) when it can determine the correct result from context. The safety boundary is semantic: Codex may resolve ordinary Git problems, but it must not use destructive operations or arbitrary conflict choices to conceal uncertainty.

If a conflict or synchronization problem is straightforward, Codex should solve it and continue. If it is not straightforward, the existing workflow is already the escalation path:

```text
Codex cannot determine the correct resolution
    -> Codex stops and explains the blocker/decision needed
    -> Stop hook / email notification
    -> human + manager review
    -> new instruction/comment
    -> Codex resumes
```

Do not make the watchdog interpret scientific/research comments or Git conflicts.

For this inbound remote-update wake, do not require a fresh parked TTL. First-party `codex queue` already starts a turn only when the target is idle; if the exact thread is currently active, the message may remain queued for later consumption. Continue to require exact current thread/workspace mapping and queue deduplication, but do not reintroduce a general ACTIVE/PARKED state machine just to send the remote-update queue message.

This change removes the active-worktree fast-forward race entirely rather than trying to guard that race more aggressively.

### 3. Stop notification email must contain Codex's actual final assistant output

Another real dogfood problem: the Stop email currently contains only:

```text
Last output available: yes
Last output characters: <N>
```

This is not useful. The user cannot tell whether Codex stopped because it completed normally, hit a blocker, needs a decision, or reported an error.

The Stop hook already receives `last_assistant_message`. Keep the durable audit privacy policy unchanged:

```text
durable audit:
    availability
    SHA-256
    character count
    no raw assistant text
```

But add a small transient spool for terminal Stop output, correlated by exact `invocation_id` / session / turn, for example under the gitignored runtime:

```text
runtime/transient/stop-output/<invocation_id>.json
```

The transient record may contain the exact raw `last_assistant_message` plus only the IDs required for safe correlation. Write it atomically. It must never be staged or committed.

When the foreground watchdog processes the matching terminal Stop, include the raw final assistant message in the notification body, for example:

```text
Workspace: ...
Hook outcome: grace_expired_parked
Git topology: ...
Tracked changes: ...

Codex final output:
------------------------------------------------
<exact last_assistant_message>
------------------------------------------------
```

Do **not** use another model to summarize it. The point is to show the user's exact final Codex output.

A reasonable bounded size such as 16k–32k characters is fine; if truncation is necessary, mark it clearly and preserve the original character count. Normal outputs should remain unmodified.

Delete the transient spool only after the correlated notification has been successfully delivered through the configured external notification path. If delivery fails or is audit-only, retain the spool for retry/recovery. Missing/mismatched spool data must fail safely and fall back to the existing hash/count metadata rather than attaching the wrong thread's output.

### 4. Preserve the now-working outbound publication path

Do not regress the checkpoint-6 behavior:

- `git add -u` for tracked modifications/deletions;
- only the narrow reviewed `doc/Progress/progress_*.md` new-file allowlist;
- normal non-force push;
- no broad untracked staging;
- no watchdog force/reset/rebase/automatic conflict resolution;
- the watchdog may publish Codex's stopped-work state outward to GitHub.

The architectural simplification in item 2 applies to the **inbound GitHub -> Codex** direction only. Codex itself may use normal non-destructive Git synchronization/conflict-resolution operations as described above.

### 5. Dogfood these fixes immediately; no new feature work

Acceptance for this checkpoint should be based on the real workflow, not additional architecture work.

At minimum prove:

1. A terminal Stop older than the previous 15-minute threshold is still considered parked unless later activity exists; `stale_parked_stop` no longer blocks.
2. A remote OID update causes **no watchdog worktree/HEAD/index mutation** and queues exactly one remote-update instruction for the exact current Codex thread.
3. The queued Codex turn itself inspects and safely synchronizes the repository, resolves an ordinary synchronization/conflict case autonomously when the correct resolution is clear, or stops for a decision when it is not; after successful synchronization it finds the latest `## comment` and continues.
4. A real terminal Stop notification to the already configured Outlook email contains the actual Codex final assistant output, not only its character count.
5. Duplicate remote OID and duplicate Stop notification behavior remains suppressed as intended.

Use the existing foreground watchdog and automatic VS Code discovery. Harden only concrete failures encountered while running these tests.

Do not work on Remote-SSH, service installation, Insiders/portable discovery, Slack, or resume disposition in this checkpoint unless one of those directly blocks the above real acceptance path.

Work for at most two hours of active implementation, write the next standard progress report with exact live results and any remaining real blocker, publish it through the normal watchdog path, and stop for review.