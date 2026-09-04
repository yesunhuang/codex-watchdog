# Progress report - 2026-09-01 checkpoint 8 latched PARKED and Codex-owned inbound Git

Start: 2026-09-01 23:09 CDT

Final implementation snapshot: 2026-09-02 00:02 CDT

Elapsed at snapshot: approximately 53 minutes of active work.

Scope: implement the accepted checkpoint-8 review directly: make PARKED a
latched event state, remove watchdog-owned inbound Git synchronization, queue
exact remote-OID changes for Codex-owned synchronization, and include the exact
correlated final assistant output in terminal Stop notifications without
putting raw output in durable audits.

Explicitly not expanded: Remote-SSH execution, stable-VS-Code discovery
coverage, service installation, Slack behavior, or general activity-state
framework work.

## 1. What was implemented

- Removed the 15-minute PARKED permission lease and the
  `stale_parked_stop` blocker. Exact parked evidence remains valid indefinitely
  until later positive or ambiguous hook, rollout, queue, session, workspace,
  or ownership evidence invalidates it. `parked_age_seconds` remains diagnostic.
- Replaced inbound `git fetch` plus watchdog fast-forward with read-only
  `git ls-remote --exit-code --refs` inspection of the exact configured branch.
- Removed `LocalGitMutator.fast_forward`; no foreground watchdog path can pull,
  fetch, merge, rebase, fast-forward, update HEAD/index/worktree, or resolve an
  inbound conflict.
- Added durable `last_remote_oid` plus pending-OID migration/deduplication.
  A remote OID is queued at most once unless its prior dispatch remains
  explicitly unresolved; uncertain queue records still prevent blind resend.
- An OID already equal to local HEAD (for example after Codex synchronization
  or Codex's own push) advances the remote baseline and clears a stale pending
  OID without queuing a redundant synchronization turn.
- Replaced the short remote-update message with the accepted mechanical
  seven-step synchronization/safety/comment prompt. Codex chooses an ordinary
  non-destructive synchronization strategy using current task context.
- Decoupled remote-update wakes from PARKED evidence. Exact live
  workspace/session revalidation and queue idempotence remain mandatory.
- Kept an ambiguous multi-workspace `resume_prompt.md` retained and notified,
  but no longer lets that unrelated ambiguity block exact per-workspace remote
  OID wakes.
- Added atomic terminal output spooling under
  `runtime/transient/stop-output/<invocation_id>.json`. Continuation-blocking
  Stops do not create terminal spools.
- Added exact invocation/session/turn/workspace/hash/count correlation before
  attaching the raw assistant output to a Stop notification. Normal outputs are
  unchanged, outputs over 32,000 characters are explicitly truncated, and the
  spool is deleted only after successful external delivery.
- Preserved the existing hash/count-only durable Stop audit and hash-only
  notification debounce state.

## 2. Inbound and outbound direction split

```text
Codex -> GitHub
    exact latched PARKED evidence
    -> tracked changes + reviewed new progress report only
    -> mechanical commit
    -> normal non-force push

GitHub -> Codex
    exact branch OID changes under git ls-remote
    -> no fetch/ref/HEAD/index/worktree mutation
    -> one exact-thread codex queue instruction
    -> Codex inspects and synchronizes Git with task context
    -> Codex reads the latest unprocessed progress-report comment
```

## 3. Automated verification

- Final full repository suite: **232 passed, 2 skipped** (234 collected), no
  failures.
- Focused Git/Stop/MVP/queue suite passed after the implementation changes.
- `python -m compileall -q src tests tools` passed.
- Black formatting and `git diff --check` passed.
- A terminal Stop more than 15 minutes old remained `allowed` with reason
  `exact_thread_parked`, included a diagnostic age, and permitted the outbound
  preservation test.
- Remote-update service tests asserted no mutator call, unchanged head OID in
  initial/final observations, exact OID dispatch once, persisted OID
  deduplication, dispatch despite later rollout/queue activity, and dispatch
  despite an unrelated ambiguous resume prompt.
- Local Git integration captured HEAD, index bytes, worktree status,
  `refs/remotes/origin/main`, and `FETCH_HEAD` before and after a newly
  advertised remote commit; all remained byte/identity unchanged while the new
  exact remote OID was observed.
- Stop-output tests proved raw text absent from durable audit, present only in
  the correlated transient record, attached verbatim on successful delivery,
  deleted after success, retained after audit-only delivery, and never attached
  on mismatched session correlation.

## 4. Live local observations so far

Automatic discovery currently reports three open stable-VS-Code windows: two
exactly tracked local workspaces and one `remote_agent_required` window. A
read-only production-adapter probe against this repository returned
`status=observed`, `topology=equal`, and an exact remote OID. Snapshots proved
HEAD, index, worktree status, the remote-tracking ref, and `FETCH_HEAD` were all
unchanged by that live probe.

The existing configured Outlook terminal had exited, but the authorized OAuth
cache remains available. One real correlated terminal Stop spool exists with a
matching terminal audit and 750 characters of exact output. A live send was not
attempted after the execution safety reviewer required payload-specific consent
to transmit that unknown exact prior output. The spool remains intact, as the
new retention rule requires.

## 5. Remaining live acceptance at this snapshot

Still to run immediately after publishing this implementation:

1. create one harmless remote-only update to this report from a separate clone;
2. run the new foreground path against this exact local workspace and prove the
   remote OID queues once without changing local Git state;
3. synchronize the repository from Codex, inspect the remote report update, and
   record the exact result here; and
4. if payload-specific consent is supplied, send the already correlated exact
   Stop output through the configured Outlook path and verify `sent/smtp` plus
   spool deletion.

## 6. Exact tracked files modified or created

```text
README.md
architecture.md
src/codex_watchdog/git_adapter.py
src/codex_watchdog/git_mutations.py
src/codex_watchdog/mvp_service.py
src/codex_watchdog/queue_wake.py
src/codex_watchdog/stop_hook.py
tests/test_git_adapter.py
tests/test_git_mutations.py
tests/test_mvp_service.py
tests/test_queue_wake.py
tests/test_service_cli.py
tests/test_stop_hook.py
doc/Progress/progress_2026_09_01_cpx08.md
```

Ignored runtime state, raw Stop output, prompts, OAuth material, mailbox
identity, and notification secrets are not included in the tracked change set.

## 7. Live remote-OID dogfood trigger

This report-only commit was created from an isolated clone after publication of
the checkpoint-8 implementation. It is the deliberate remote branch OID change
for acceptance. Before Codex synchronizes the primary checkout, the watchdog
must queue this exact OID once while leaving that checkout's HEAD, index,
worktree, remote-tracking ref, and `FETCH_HEAD` unchanged.

## 8. Completed live inbound dogfood

The report-only remote commit was
`3376d09775368921817d3f44193e2d372a0a5180`. Automatic discovery, with the two
unrelated local repositories explicitly excluded, resolved exactly this local
workspace and exact current Codex thread.

The first production `run --once` cycle reported:

```text
workspace_count: 1
workspace_status: completed
wake_kind: remote_update
wake_status: enqueued
wake_deduplicated: false
wake_records_added: 1
queued_remote_oid: 3376d09775368921817d3f44193e2d372a0a5180
```

Before and after that cycle, HEAD, index bytes, worktree status,
`refs/remotes/origin/main`, and `FETCH_HEAD` were identical. A second cycle for
the same remote OID produced no wake and added zero wake records. The queue
journal remains `enqueued`, proving no blind resend while the target thread is
still active.

VS Code separately auto-fetched the remote-tracking ref, as shown by that ref's
own Git reflog; it did not change HEAD. Codex then inspected the clean
one-commit-behind state, selected the unambiguous normal fast-forward, ran
`git pull --ff-only origin main`, and inspected this exact report addition.
That Codex-owned synchronization changed only the report file and advanced HEAD
from `8fa8ca111a92a88f2de256d4fce05a82c1d63217` to the expected remote OID.

The only remaining live checkpoint item is external delivery of the unknown
750-character prior Stop output. The safety reviewer requires explicit
payload-specific consent before transmitting that exact text to the configured
Outlook mailbox. The correlated spool remains intact and no alternate route was
attempted.

After the final results push advanced the remote OID to local HEAD, one more
targeted production cycle produced no wake, added zero queue records, and
reported `head_oid == upstream_oid`. This closes the outbound-self-push
deduplication edge case with both a focused regression test and live evidence.

## comment

### Liveness smoke test — 2026-09-02

Harmless end-to-end test after the wake-liveness fix. Unique marker: `WATCHDOG_LIVENESS_SMOKE_3`.

Only if this thread is naturally resumed by WatchDog because of this GitHub update:

1. synchronize the repository using the ordinary safe Git strategy appropriate to the current state;
2. verify that you can read the exact marker `WATCHDOG_LIVENESS_SMOKE_3`;
3. do not perform implementation, debugging, or any unrelated work;
4. append a new section `## Liveness smoke result` containing `WATCHDOG_LIVENESS_SMOKE_OK` plus the current timestamp;
5. publish that minimal report update normally and stop.

This is only a live causality test of remote OID detection -> pending/enqueued tracking -> exact-thread wake -> Codex-owned synchronization -> comment read -> outbound publication -> Stop.

## Liveness smoke result

WATCHDOG_LIVENESS_SMOKE_OK — 2026-09-02T07:42:00Z

Temporal prompt evidence: `CPX09_TEMPORAL_PROMPT_FOUND`.
