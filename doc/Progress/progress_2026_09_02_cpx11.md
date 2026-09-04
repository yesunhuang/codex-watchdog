# Progress report - 2026-09-02 checkpoint 11 notification cleanup

Final snapshot: 2026-09-02 21:22 CDT

Scope: implement the checkpoint-10 review comment by removing obsolete
ordinary Git-state notifications after the zero-Git-mutation convergence,
retain only actionable operational alerts, verify the shared pre-transport
event policy, and publish the narrow cleanup.

## 1. Synchronization

The repository was clean and safely fast-forwarded from `c6107ea` to remote
review commit `12640d316ba57b5fb21175a8784b0f45021e70ee`. The new checkpoint-10
`## comment` was the only actionable instruction. No active temporal/resume
prompt was present; the runtime contained archived prompts only.

## 2. Ordinary Git state is observation-only

The local and Remote-SSH service paths no longer construct notification events
solely because an observation contains dirty tracked files, untracked files, or
local-ahead commits. Those fields remain in the privacy-limited Git observation
and cycle result, so Codex can reason about repository state without WatchDog
sending Outlook, Slack, or Windows/local notifications.

The decision remains upstream of `EnvironmentNotifier`: removed events never
enter transport selection, fallback, or debounce. This prevents transports
from developing different policies.

The README and architecture now distinguish ordinary observed work state from
genuine Git blockers.

## 3. Actionable alerts retained

The cleanup retains notifications for:

- exact terminal Stop and delayed exact `task_complete` completion;
- uncertain or failed/deferred exact-thread wake delivery;
- unreachable or unauthorized Remote-SSH adapters; and
- genuine local or Remote-SSH Git blockers.

Remote-SSH blocker observations now use the existing `git_attention` category,
matching the local event policy. No new notification category, Git behavior,
service machinery, Remote-SSH scope, or Slack setup was added.

A production/documentation/test search outside historical progress reports
found no remaining mutation-era subjects or messages for `Git action deferred`,
`Action: preserve`, mutation eligibility, `workspace_session_changed`, failed
preservation, push retry, `unpublished_local_work`, or `untracked_files`.

## 4. Verification

- Focused service suite: 36 tests passed.
- Full suite: 251 tests passed with one intentional skip.
- Explicit negative coverage: local dirty tracked, untracked, and local-ahead;
  combined Remote-SSH dirty/untracked/local-ahead; all emit no notification.
- Explicit positive controls: terminal completion, uncertain wake, unreachable
  Remote-SSH, and genuine local/Remote-SSH blockers still notify.
- `python -m black --check src tests`: passed, 32 files unchanged.
- `python -m compileall -q src tests`: passed.
- `git diff --check`: passed.

## 5. Foreground restart boundary

The implementation and report were published as `f325129`. An initial process
query incorrectly appeared empty because Windows hid the elevated process's
command line. A broader process and runtime-state correlation identified the
old foreground instance as Python PID `42780`, started under the configured
Administrator PowerShell and actively updating `.codex-watchdog/live-acceptance`.

The old instance could not be terminated from the non-administrator Codex
process (`Access is denied`). Codex also cannot safely launch an equivalent
replacement: the working Outlook variables exist only inside that configured
Administrator PowerShell and are not present in the Codex, user, or machine
environment. The operator must press `Ctrl+C` in that PowerShell, then rerun the
foreground command there so the new process loads `f325129` while retaining the
working Outlook profile. This checkpoint does not claim a restarted process or
post-restart email acceptance.

## 6. Review stop

The requested notification-policy cleanup, automated evidence, commit, and push
are complete. Live acceptance is limited to restarting the foreground command
from the configured elevated operator shell and confirming that ordinary
ProjectAlpha work state stays silent while a real terminal event still emails.
