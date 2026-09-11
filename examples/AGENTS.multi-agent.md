# Multi-Agent Project Contract (Template)

> Copy this file to the root of your project as `AGENTS.md`, then customize the
> project-specific values if needed.
>
> This file is **policy, not mechanism**. Codex WatchDog does not enforce these
> rules. Each agent is expected to read and follow them. Git is the durable
> coordination record; WatchDog only observes, wakes, routes, relays, and notifies.

## 0. Project coordination defaults

- Default maximum continuous active time per Codex session: **2 hours**.
- Default progress-report directory: `doc/Progress/`.
- Default integration authority: **Manager** and **Codex A** only.
- Default worker branch namespace: `codex-<letter>/*`, for example
  `codex-a/*`, `codex-b/*`, `codex-c/*`.
- The repository default branch is the canonical coordination branch unless the
  Manager explicitly designates another branch.

The Manager may override these defaults with an explicit durable instruction.

## 1. Mandatory agent identity claim

Every Codex session must claim a unique project identity **before doing
substantive work**.

Identity order is first-come, first-served:

`Codex A`, `Codex B`, `Codex C`, `Codex D`, ...

The first unclaimed identity must be used. Do not skip an available earlier slot.

### Claim procedure

1. Fetch/pull the latest canonical coordination branch.
2. Read the `Agent registry` below.
3. Choose the alphabetically first `AVAILABLE` slot.
4. Change only that row to `CLAIMED`, add the current UTC timestamp, and optionally
   record the branch/workspace used by this session.
5. Commit the minimal registry change and push it normally.
6. **The claim exists only if that push succeeds.**
7. If the push is rejected because another agent updated the canonical branch,
   do not force-push and do not overwrite the other claim. Fetch again, choose the
   next available identity, and retry.
8. After a successful claim, keep that identity for the lifetime of this project
   session unless the Manager explicitly retires or reassigns it.

The registry claim is a narrow bootstrap exception to the merge restrictions in
Section 4. It may modify only the registry rows needed for the claim.

If all listed rows are claimed, append the next alphabetical row and claim it using
exactly the same procedure.

## 2. Agent registry

| Agent | Status | Claimed at (UTC) | Branch / workspace | Notes |
| --- | --- | --- | --- | --- |
| Codex A | AVAILABLE | - | - | Primary integration agent after claim |
| Codex B | AVAILABLE | - | - | Worker |
| Codex C | AVAILABLE | - | - | Worker |
| Codex D | AVAILABLE | - | - | Worker |
| Codex E | AVAILABLE | - | - | Worker |
| Codex F | AVAILABLE | - | - | Worker |

## 3. Continuous active-time limit

A single Codex session may perform at most **2 hours of continuous active work**
by default.

This limit is a checkpoint boundary, not a lease that silently renews itself.
Before reaching the limit, the agent must:

1. Stop starting new substantial sub-tasks.
2. Bring the current operation to the safest practical boundary.
3. Run the relevant tests/checks that fit within the remaining time.
4. Write the required progress report described in Section 5.
5. Commit and push only work the agent is authorized to publish.
6. Stop and yield control to the Manager.

If the task is incomplete, say so explicitly in the report. A new manager comment
or a new session may resume the work later. Writing a checkpoint report does **not**
automatically grant another 2 hours to the same uninterrupted run.

The Manager may set a different maximum for a specific task, but the override must
be explicit and durable.

## 4. Merge and conflict authority

### Manager and Codex A

By default, only the **Manager** and the successfully claimed **Codex A** may:

- merge pull requests or integration branches;
- resolve conflicts between work produced by different agents;
- rebase/cherry-pick one agent's work into another agent's integration history;
- rewrite, force-push, reset, or otherwise repair shared integration refs;
- decide which side wins when two agents changed the same logical behavior;
- delete or supersede another agent's published integration work.

Codex A is the default technical integration lead, not the owner of every task.
It should preserve other agents' work and prefer narrow, reviewable integration.

### Codex B / C / D / ...

Worker agents may, without extra authorization:

- edit files within their assigned scope;
- commit and push their own branch;
- run tests and diagnostics;
- open or update their own pull request;
- write progress reports;
- report conflicts and propose a resolution.

Worker agents must **not** perform the integration/conflict operations listed above
unless the Manager or Codex A explicitly authorizes that operation.

If a worker encounters a conflict, it should preserve the evidence, stop the
integration action, describe the conflict in its progress report, and wait for
resolution or authorization.

### Scoped authorization through `## comment`

A worker may receive temporary integration authority through an explicit durable
`## comment` written by the Manager or Codex A in a progress report.

The authorization should identify:

- the authorized agent;
- the exact operation (for example `resolve`, `merge`, `rebase`, `cherry-pick`);
- the exact branch / PR / commit scope;
- the current checkpoint or task scope.

Example:

```text
## comment

Codex B is authorized for this checkpoint to resolve the conflict between
`codex-b/lark-tests` and `main`, then merge PR #17. This authorization is one-shot
and does not extend to any other branch or future checkpoint.
```

Authorization is **explicit, scoped, non-transitive, and one-shot** unless the
comment says otherwise. Old, ambiguous, or unrelated comments do not grant merge
rights.

## 5. Mandatory checkpoint progress reports

Every agent must write a progress report at the end of **every checkpoint**, and
always before stopping because of the active-time limit.

Use:

`doc/Progress/progress_YYYY_MM_DD_cpxNNN_codex_a.md`

Replace the date, checkpoint number, and agent suffix as appropriate, for example:

- `progress_2026_09_10_cpx071_codex_a.md`
- `progress_2026_09_10_cpx071_codex_b.md`
- `progress_2026_09_10_cpx072_codex_c.md`

Use the project/manager-assigned checkpoint number when one exists. Otherwise use
an unambiguous next checkpoint number visible from the latest canonical history.
The agent suffix is mandatory and must match the identity claimed in Section 1.

Each report must contain, at minimum:

```markdown
# Checkpoint NNN - <short title>

Date: YYYY-MM-DD
Agent: Codex A
Status: complete | partial | blocked
Active time: <approximate duration>

## Scope
What this checkpoint was supposed to do.

## Changes
Concrete files / behaviors / decisions changed.

## Evidence
Tests, commands, logs, measurements, hashes, or live acceptance results that
support the claims above.

## Failures / anomalies
Anything that failed, behaved unexpectedly, or remains uncertain. Do not hide
failed attempts that materially affect the next decision.

## Current state
What is running, stopped, committed, pushed, open, pending, or intentionally left
untouched.

## Next recommended action
The smallest useful next step for the Manager or next agent.

## Blockers / permissions needed
Anything that requires human credentials, approval, conflict resolution, merge
authority, or another agent.

## comment

<!-- Reserved for the Manager / Codex A to leave durable continuation instructions. -->
```

Progress reports are the standard asynchronous **agent -> manager compression
format**. They should summarize evidence and decisions rather than dump raw logs.
Raw logs may be retained separately when useful.

Do not delete or silently rewrite a Manager's `## comment`. When resuming, read the
latest applicable comment before acting; record how it was handled in the next
checkpoint report.

## 6. Branch and workspace isolation

Recommended branch names follow the claimed identity:

- `codex-a/<task>`
- `codex-b/<task>`
- `codex-c/<task>`

Do not reuse another active agent's branch or worktree unless explicitly authorized.
Do not discard another agent's unmerged work merely to obtain a clean tree.

When multiple agents are active, prefer small independent scopes and let the
Manager/Codex A integrate at explicit checkpoints.

## 7. General coordination invariants

- Read this `AGENTS.md` before every new assignment or resumed checkpoint.
- Preserve user data, credentials, unrelated work, and other agents' state.
- Never force through ambiguity simply to keep work moving.
- Prefer narrow changes over speculative framework expansion.
- Report uncertainty honestly; do not turn an unverified assumption into a success
  claim.
- Keep durable decisions in Git/progress reports rather than only in terminal logs
  or transient chat context.
- The Manager remains the final authority and may pause, reassign, retire, or
  override any agent when explicitly stated.
