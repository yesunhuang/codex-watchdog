# Progress report - 2026-09-02 checkpoint 10 architecture convergence

Final snapshot: 2026-09-02 04:20 CDT

Scope: diagnose the missing ProjectAlpha terminal email, pull and implement the new
checkpoint-10 review comment, remove WatchDog-owned Git mutation, add the narrow
Remote-SSH path, carry human-readable labels through all transports, and record
the exact live acceptance boundary for Remote-SSH and Slack.

## 1. Synchronization and diagnosis

The repository was safely fast-forwarded to remote review commit
`546ba960f0ac0c6879c3f1dc2120e2d163412c4c` before implementation. Local work
did not overlap the pulled progress-report comment.

There were two open ProjectAlpha windows:

- local `D:\projects\ProjectAlpha`, exact thread
  `<UUID>`; and
- Remote-SSH `/home/operator/ProjectAlpha` on
  `hpc-login.example.edu`.

For the local window, the rollout contained an exact `task_complete` and final
assistant output, but no Stop audit for that turn. SMTP had not failed: the
foreground service simply never received a terminal Stop event. For the remote
window, discovery intentionally stopped at `remote_agent_required`, so the
local service never observed its remote Codex state. Both causes are addressed
in the implementation below.

## 2. ProjectAlpha terminal-notification repair

The local service now tails at most 1 MiB of the exact session rollout and, if
VS Code omitted Stop, recognizes the latest `task_complete` after a 45-second
grace period. It sends the exact `last_agent_message` through the ordinary
notification event, persists only its hash/count, and deduplicates a later real
Stop by exact workspace/session/turn identity. A focused regression proves the
same completed turn sends once and does not retain raw output durably.

The Remote-SSH implementation uses the same terminal-rollout fallback in the
remote owning locality, so a missing remote Stop hook no longer prevents the
terminal email once the SSH transport is authorized.

## 3. Zero Git mutation

The production `LocalGitMutator`, its staging allowlist, mechanical progress
commit, push retry, and parked-evidence mutation gates were removed. Their
legacy result fields were also removed from the MVP cycle schema. WatchDog now
only observes dirty/untracked/topology state and asks Codex/the user to inspect
unpublished work.

The fixed wake prompt explicitly states that WatchDog never stages, commits,
pulls, merges, or pushes and that Codex owns safe publication. Both local and
remote Git runners enforce a runtime read-only subcommand allowlist. Regression
tests attempt `add`, `commit`, `fetch`, `merge`, `pull`, `push`, `rebase`,
`reset`, checkout/switch, and ref updates and prove they are rejected before a
process runner can execute them.

## 4. Remote-SSH implementation and live boundary

Automatic discovery now reports the actual HPC provider window as a
`remote_adapter` target rather than an unresolved issue. The thin adapter is
sent over SSH stdin and executes with remote `python3` beside the VS Code
Server. It:

- intersects the exact remote workspace cache, Codex state row, canonical cwd,
  and current owner log to resolve one thread or fail closed;
- observes only a bounded exact rollout tail;
- performs only read-only Git/status/exact-remote-OID commands;
- journals queue dispatch before invoking the remote first-party `codex queue`;
- never resends a dispatching/uncertain/enqueued record blindly; and
- reconciles queue-row/revision and exact rollout-marker evidence to
  `consumed_or_started` or `started`.

User-facing results use `ProjectAlpha @ hpc-login`; durable state keeps a deterministic
internal remote workspace ID and the exact thread.

Live Windows discovery succeeded at `2026-09-02T09:17:00Z` with status `ok`,
four live windows, three local tracked workspaces, one Remote-SSH adapter
workspace, and no discovery issues. Live adapter execution is not claimed:
both `hpc-login.example.edu` and the existing compute-host alias rejected
`BatchMode=yes` authentication. VS Code's private tunnel cannot safely be
commandeered, and terminal/Codex UI automation was deliberately not used.

The exact one-time unblock is to authorize the existing Windows
`~/.ssh/id_ed25519.pub` on the HPC provider account, verify
`ssh -o BatchMode=yes hpc-login.example.edu true`, then restart the foreground
watchdog. No workspace or thread ID is required.

## 5. Notifications and Slack boundary

All subjects now use the shared presentation helper: repository basename,
optional remote locality, and internal ID only as fallback. The same
`NotificationEvent.subject` is consumed by Slack, SMTP, and Windows/local
transports. Stop, Git attention, untracked/unpublished work, remote-adapter
failure, and uncertain wake events all use this rule.

The existing Slack-primary -> SMTP -> Windows/local fallback path and
deduplication remain covered by tests. No Slack webhook is configured in the
current environment, so no genuine Slack delivery or Slack-to-Outlook live
fallback is claimed. The exact remaining action is to create/obtain one Slack
Incoming Webhook and set `CODEX_WATCHDOG_SLACK_WEBHOOK_URL` only in the watchdog
PowerShell environment; the secret must not be committed or pasted into a
report.

## 6. Verification

- Full suite: 244 tests collected, exit zero, with one intentional skip.
- Focused Remote-SSH/Git/MVP/discovery/notification/queue suite: passed.
- `python -m black --check src tests`: passed.
- `python -m compileall -q src tests`: passed.
- `git diff --check`: passed.
- Live discovery: 4 windows = 3 local + 1 `ProjectAlpha @ hpc-login` remote adapter,
  status `ok`, zero issues.
- Live Remote-SSH terminal/wake: blocked only by batch SSH authorization; not
  claimed.
- Live Slack: blocked only by missing webhook configuration; not claimed.

## 7. Review stop

The requested architecture converges to observe, wake, and notify. The code and
automated evidence are complete for the ProjectAlpha repair, zero-Git-mutation
invariant, narrow Remote-SSH adapter, and shared labels. The two credentialed
live acceptances remain explicitly pending; no unrelated background-service,
multi-host, ACTIVE/NOT_ACTIVE, or UI work was added.

## comment

### Remove obsolete Git-state notifications after zero-Git-mutation convergence

The latest dogfood still produced user-facing emails such as:

```text
[Codex Watchdog] ProjectAlpha has untracked files
[Codex Watchdog] ProjectAlpha Git action deferred
Action: preserve
Reason: workspace_session_changed
```

These notifications belong to the old WatchDog-owned Git-mutation design and are now noise. The architecture is already converged to:

> **WatchDog observes Git; Codex owns Git.**

Clean up the notification policy accordingly.

Requirements:

1. Remove user-facing notifications whose only condition is ordinary repository work state:
   - dirty tracked files;
   - untracked files;
   - local-ahead / unpublished local commits;
   - any equivalent "workspace has unpublished work" informational event that does not itself require human intervention.
   These states may remain available internally in read-only observations, diagnostics, or wake context, but must not independently send email, Slack, or Windows/local attention notifications.

2. Remove all remaining mutation-era notification paths and wording, including but not limited to:
   - `Git action deferred`;
   - `Action: preserve`;
   - PARKED-evidence mutation eligibility/defer messages;
   - `workspace_session_changed` when it is only explaining why an outbound WatchDog Git mutation was not attempted;
   - failed outbound preservation / push-retry notifications that no longer correspond to any production WatchDog action.
   There should be no user-facing text implying that WatchDog might stage, preserve, commit, or push Git state.

3. Keep notifications for events that actually merit attention, for example:
   - terminal Codex Stop / delayed exact `task_complete` completion;
   - uncertain or genuinely failed queue delivery/wake;
   - unreachable or unauthorized Remote-SSH adapter;
   - genuine Git/repository blockers such as conflicts, malformed/unsupported repository state, or authentication failure when they prevent the observation/wake workflow;
   - other concrete conditions that require human action.

4. Apply the same event policy before transport selection so Outlook, future Slack, and Windows/local notifications cannot diverge.

5. Update tests so they explicitly prove that dirty/untracked/local-ahead observations alone emit no notification, while real terminal/wake/remote/blocker events still do.

6. Search for stale mutation-era subject/message strings and remove unreachable legacy notification code where safe. Historical progress reports may of course retain old wording as history.

7. Restart the actual foreground watchdog after publishing the fix. The two screenshots above likely include at least one message from a process still running pre-cpx10 code; live acceptance should confirm the restarted process no longer emits either obsolete notification class.

Keep this as a narrow cleanup. Do not add new Git behavior, notification categories, service machinery, Remote-SSH expansion, or Slack setup in this change. Publish the fix normally through Codex-owned Git and stop for review.
