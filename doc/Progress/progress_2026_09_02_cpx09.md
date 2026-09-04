# Progress report - 2026-09-02 checkpoint 9 wake liveness and temporal-prompt acceptance

Final snapshot: 2026-09-02 02:50 CDT

Scope: record the completed wake-liveness hardening and the live acceptance of
the fixed remote-wake prompt, including same-thread temporal-prompt discovery,
execution, disposition, and normal Git publication. No new implementation,
debugging, or research was performed during this acceptance turn.

## 1. Current WatchDog state

- The checkpoint-9 implementation is published on `main` in commit
  `30112c3304339b8a8ad30f0509e8102baa3b8af0`.
- The foreground watchdog is running from the production entry point with the
  `.codex-watchdog/live-acceptance` runtime and a two-second interval.
- Automatic VS Code discovery is observing this exact LocalCodexWatchDog
  workspace and thread without user-entered workspace or thread identifiers.
- Inbound Git handling remains read-only inside WatchDog. WatchDog detects the
  exact remote branch OID and queues the fixed prompt; Codex chooses and
  performs the safe synchronization in thread context.
- Outlook OAuth2 notification delivery and exact correlated terminal-output
  email delivery were proven in earlier live acceptance.
- Remote-SSH execution, background service installation, and Slack remain
  deliberately outside this checkpoint.

## 2. Implemented liveness semantics

- A sole `state_changed_during_observation` condition is retried once and then
  deferred as transient/busy. The deferred cycle does not consume the audit
  cursor, pending remote OID, wake, Stop evidence, or resume/temporal work, and
  does not send a Git-attention alert.
- `enqueued` is durable pending state, not completion. Later cycles reconcile
  the same queue record without duplicate enqueue and clear the pending remote
  work only after `consumed_or_started` or `started` evidence, or after local
  `HEAD` already equals the remote OID.
- The fixed remote wake checks local temporal/resume instructions first. Codex
  decides relevance and archives useful instructions or deletes one-shot
  instructions after use.

Automated verification completed before publication: 238 tests passed with 2
intentional skips; the focused queue/MVP/service/Git suites, compile-all,
formatting, and diff checks passed.

## 3. Live wake-liveness acceptance

Remote trigger OID `71a20eb41e2c8643546e6ade9d3b9b0af04428c9`
created exactly one queue record. That same record was accepted at
`2026-09-02T07:38:28Z` and reached `started` at
`2026-09-02T07:39:10Z`; it was not enqueued twice. The exact Codex thread woke
automatically, inspected the runtime temporal prompt before Git, archived the
useful prompt, fast-forwarded the clean repository, read
`WATCHDOG_LIVENESS_SMOKE_3`, and published `WATCHDOG_LIVENESS_SMOKE_OK` in
commit `3124386062f4ba88427da6ae591e59ec8198dca5`.

The runtime temporal marker from that acceptance was
`CPX09_TEMPORAL_PROMPT_FOUND`.

## 4. Tracked temporal-prompt acceptance

Remote trigger OID `a8a64979ef48d386afc8945e472522f3524adb7b`
added the one-shot tracked file `temporal prompt.md`. The fixed WatchDog prompt
again resumed the exact same thread automatically. Its sole queue record was
accepted at `2026-09-02T07:47:23Z` and reached `started` at
`2026-09-02T07:47:56Z`.

Codex first found and archived the relevant runtime continuation instruction,
then fast-forwarded the clean repository, discovered the new tracked temporal
prompt, and executed it in the same thread. The tracked one-shot prompt is
deleted in the same publication commit as this report, as requested.

WATCHDOG_TEMPORAL_PROMPT_OK

## 5. Result

The live path is complete:

```text
remote update
  -> read-only exact-OID detection
  -> one durable queue record
  -> exact-thread start
  -> temporal prompt discovered in context
  -> requested report produced
  -> one-shot prompt disposed
  -> normal non-force publication
```

The repository contains no new WatchDog implementation changes in this
acceptance commit. It contains only this standard progress report and deletion
of the one-shot temporal prompt.

## comment

### Human-readable notification identity

The current user-facing notification subjects expose internal auto-discovery IDs such as:

```text
vscode-<STATE_ID>
```

This is useful only as an internal stable identifier and is poor UX for email/Slack/desktop notifications. Change the human-facing notification identity so the user can immediately tell which Codex/repository needs attention.

Requirements:

1. For all user-facing notification subjects/titles, prefer a human-readable Git repository label derived from the tracked repo. For this repository the visible label should be something like `LocalCodexWatchDog`, not the `vscode-<hash>` workspace ID.
2. If a reliable owner/repository form is already available cheaply, `operator/LocalCodexWatchDog` is also acceptable; otherwise the repository root basename is sufficient. Do not add network/API lookup just for naming.
3. Keep the internal `workspace_id`, exact thread/session IDs, hashes, etc. unchanged for durable state, deduplication, audit, and diagnostics. This is only a presentation-layer change.
4. Use the same human-readable label consistently across Outlook email, future Slack notifications, Windows/local notifications, and any other user-facing transport, rather than each transport inventing its own naming rule.
5. When Remote-SSH support is added later, include enough locality to disambiguate identical repo names when necessary, e.g. `ProjectAlpha @ hpc-login` or equivalent. Do not build the Remote-SSH feature in this small fix; just keep the label helper/design reusable for it.
6. Fallback safely: if repository identity cannot be derived, then and only then fall back to the existing internal workspace ID rather than failing notification delivery.

Examples of desired subjects:

```text
[Codex Watchdog] LocalCodexWatchDog stopped
[Codex Watchdog] LocalCodexWatchDog needs Git attention
[Codex Watchdog] QLSCI needs attention
```

Keep this change lightweight. Do not expand notification semantics or add a metadata service. Add focused tests proving the human-readable label is used when repo identity is available and the internal ID remains only a fallback/debug field.

### Checkpoint 10 — architecture convergence: zero Git mutation, Remote-SSH, and Slack

This section supersedes the narrower stopping instruction above as the next-version scope. Keep WatchDog lightweight. The governing architecture rule is now:

> **WatchDog observes Git; Codex owns Git.**

WatchDog should converge to only three responsibilities: **observe, wake, notify**. Do not turn it into a Git actor or another agent.

#### 1. Remove all WatchDog-owned Git mutation, including outbound commit/push

The current inbound direction is already read-only, but the outbound Stop path still contains WatchDog-owned preservation/commit/push behavior. Remove that production behavior as well.

After this change, WatchDog must never perform repository mutation. In production WatchDog code, prohibit Git operations such as:

```text
git add
git commit
git push
git pull
git fetch
git merge
git rebase
git reset
git checkout / switch
ref updates or any equivalent repository mutation
```

Read-only observation is allowed, for example `rev-parse`, `status`, `ls-remote`, and narrowly justified diagnostic reads.

Concrete requirements:

- remove/retire `LocalGitMutator` and `preserve_and_push` from the production WatchDog path wherever they exist solely for WatchDog-owned publication;
- remove tracked-progress-file staging allowlists, mechanical WatchDog commits, push retries, and mutation permission gates that become unnecessary once WatchDog never writes Git state;
- preserve the current non-consuming/transient observation semantics and remote-OID liveness behavior;
- add a regression/invariant test proving the production WatchDog path cannot invoke mutating Git subcommands;
- keep internal Git observation deterministic and cheap.

Codex becomes the sole Git actor in both directions. Before an intentional Stop, Codex should itself write/update the progress report and perform the ordinary safe commit/push appropriate to the task, then stop. If Codex stops unexpectedly with unpublished local work, WatchDog may detect that state and wake/notify Codex, but WatchDog must not publish the work itself. The wake prompt should tell Codex to inspect and safely publish unfinished local work when appropriate.

This also means Remote-SSH support below must remain read-only with respect to Git; the remote adapter must not inherit the old mutation behavior.

#### 2. Implement real VS Code Remote-SSH tracking end-to-end

`remote_agent_required` is currently discovery-only. The next version must actually manage one real Remote-SSH Codex workspace end-to-end, because HPC provider/remote research work is a core use case.

Keep the first implementation narrow and dogfood one real HPC provider workspace rather than building a generic distributed framework.

Desired shape:

```text
local WatchDog
  -> detect current Remote-SSH VS Code window/workspace
  -> invoke a thin remote-side adapter in the owning SSH/VS Code Server locality
  -> remote adapter performs only:
       * exact VS Code Server / Codex thread discovery
       * Stop/audit observation
       * read-only Git observation / exact remote-OID detection
       * exact-thread `codex queue` wake
       * small status/result return
  -> local notifier presents the result to the human
```

Requirements:

- do not copy/parse large remote state locally when the check can run next to the remote workspace;
- do not perform remote Git mutation;
- preserve exact-thread/session correlation and current fail-closed behavior;
- preserve queue liveness semantics (`enqueued` remains pending until consumed/started evidence);
- use the human-readable notification label with locality when useful, e.g. `ProjectAlpha @ hpc-login`;
- live-accept one actual Remote-SSH workflow: discovery -> Stop observation -> remote Git/OID detection -> exact-thread wake -> Codex continuation. Record what is genuinely live accepted versus unit-tested.

Do not expand to multiple remote-host orchestration, service installation, or a general remote-agent framework unless the one real HPC provider path requires a minimal piece of it.

#### 3. Connect Slack as the real low-friction notification channel

Slack is the remaining important notification transport. Keep the existing notifier/fallback philosophy rather than creating a new messaging subsystem.

Target behavior:

```text
Slack
  -> SMTP/Outlook fallback
  -> Windows/local fallback
  -> audit
```

Requirements:

- use the existing Slack transport path if already implemented; configure/fix only what is necessary to make it genuinely live;
- send the same human-readable repository/session label used by email/local notifications;
- cover the important notification classes already produced by WatchDog: terminal Stop/parked notification, genuine attention/failure, and queue/wake delivery problems where human action is useful;
- preserve existing notification deduplication;
- prove one real Slack delivery and, where practical, one Slack-unavailable -> Outlook fallback without changing the underlying event semantics;
- do not put secrets/webhook URLs into tracked files, reports, logs, or durable audit content;
- if one-time Slack authorization/configuration is genuinely required and cannot be completed non-interactively, stop and report the exact minimal human action required instead of inventing a workaround.

#### 4. Scope and order

Implement these as the next-version convergence work, with priority:

```text
1. Remove all WatchDog Git mutation and establish the zero-mutation invariant.
2. Remote-SSH end-to-end on one real HPC provider workspace.
3. Real Slack delivery/fallback acceptance.
4. Human-readable notification identity should be used throughout all of the above.
```

Do not spend this checkpoint on background service installation, general ACTIVE/NOT_ACTIVE state frameworks, multi-host orchestration, Slack feature expansion, or unrelated cleanup. Work for at most two hours of active implementation. Publish the next standard progress report with exact automated and live evidence, recommend the next step but do not perform unrelated follow-on work, and stop for review.
