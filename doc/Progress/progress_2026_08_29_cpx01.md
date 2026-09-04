# Progress report — 2026-08-29 checkpoint 1 Phase 0 feasibility probe

Start: 2026-08-29 23:16 CDT
End: 2026-08-29 23:34 CDT
Elapsed: approximately 18 minutes
Scope: mandatory Phase 0 empirical probe for one local VS Code Codex workspace
and one open Remote-SSH VS Code workspace; architecture decision only.
Not attempted: operational watchdog code, Git commits/pushes by the watchdog,
remote Git, live email, resume-prompt handling, prompt injection, or any Phase
1-4 mutation behavior.

## 1. What was attempted

- Inspected the repository and implementation plan.
- Consulted current official OpenAI App Server documentation.
- Identified the installed VS Code, Codex extension, Codex CLI, Python, pytest,
  Git, and SSH environment.
- Enumerated simultaneous local and Remote-SSH VS Code windows and their local
  or remote extension-host/Codex process topology.
- Inspected VS Code workspace storage, Codex metadata/history, rollout records,
  extension logs, installed extension commands, and App Server transport help.
- Queried a separate App Server with read-only `initialize`, `thread/list`, and
  `thread/read` calls only.
- Tested independent noninteractive Remote-SSH authentication without falling
  back to password/MFA prompting.
- Derived the minimum safe adapter and state-machine architecture.

## 2. What was successfully implemented / derived / verified

- Verified stable local/remote workspace discovery inputs: canonical workspace
  URI, remote authority, workspace-storage key, and live `code --status`
  enrichment.
- Verified that the local current Codex user thread can be matched by normalized
  cwd, `source=vscode`, and `thread_source=user`.
- Verified best-effort retrieval of the latest persisted `agentMessage` text.
- Verified that local and Remote-SSH Codex App Servers run in their respective
  extension-host execution contexts.
- Verified that a separate App Server does **not** expose authoritative live
  state for the existing IDE thread: it returned `notLoaded`, and persisted
  lifecycle views disagreed while the IDE/team turn was active.
- Verified that the observed IDE App Server is private stdio with no supported
  standalone attach endpoint; Windows daemon lifecycle is unavailable.
- Verified that the open Remote-SSH window had an initialized Codex App Server
  but no evidenced Codex conversation/thread to exercise.
- Derived safe state normalization and a minimal shim contract.
- Created `probe_report.md` and `architecture.md`.

## 3. Current best implementation state / architecture

There is intentionally no operational watchdog implementation after this
checkpoint. The Phase 0 gate failed for a standalone process.

The current best architecture is:

```text
existing VS Code/Codex App Server connection
-> minimal authenticated live-session shim
-> standalone deterministic Python watchdog
-> locality-bound Git / atomic state / resume spool / SMTP
```

The shim exposes identity, live status, active-turn ID, completed output cursor,
and compare-and-send prompt delivery. The standalone watchdog retains all
policy. Remote Git and Codex operations execute in the remote locality.

## 4. Observed runtime behavior / probe results

- The local window had one main user thread plus subagent threads sharing the
  same cwd, proving that cwd alone is not a unique session key.
- The current owning App Server remained a child of the VS Code extension host
  using default stdio.
- A separate read-only App Server found the correct thread/output but returned
  runtime `notLoaded`; it cannot observe the owner's in-memory state.
- Installed live states are `active`, `idle`, `notLoaded`, and `systemError`.
- Approval/user-input waits are flags on `active`, not stopped states.
- The Remote-SSH window ran its extension host, Codex App Server, Codex home,
  workspace, and Git context on Linux.
- Independent BatchMode SSH authentication failed and was not weakened or
  retried interactively.
- Ordinary Git working-tree discovery from the sandbox failed closed on dubious
  ownership. No trust exception was added.

## 5. Tests and sanity checks performed

- Repeated read-only VS Code process/window discovery.
- Cross-checked the local workspace URI against Codex cwd after removing the
  Windows extended-length prefix.
- Cross-checked persisted thread ID and latest assistant output across metadata,
  history, and rollout/App Server reads.
- Confirmed historical completed VS Code turns and the current active turn in
  private persisted data, while rejecting those records as an authoritative
  live detector.
- Confirmed no supported App Server TCP listener/control socket for the current
  Windows IDE process.
- Confirmed the installed extension provides no documented arbitrary
  prompt-to-thread command.
- Confirmed Remote-SSH execution locality with `code --status`.
- Confirmed no prompt or Codex mutation API was invoked.
- Confirmed temporary probe artifacts/debris were removed before writing the
  checkpoint.
- Validated all three checkpoint artifacts as strict UTF-8, checked every
  required report heading, and confirmed that no `## comment` section exists.

## 6. Unresolved technical issues / limitations

1. A supported way to attach to the exact live IDE App Server is absent.
2. No stable VS Code window ID is present in persisted Codex thread objects.
3. The minimum local/remote extension-host shim has not been proved.
4. No real Remote-SSH Codex thread was available for transition/output/prompt
   testing, and independent noninteractive SSH was not configured.
5. Approval/user-input waits require a separate attention event while remaining
   `ACTIVE`; this refines the simple acceptance-scenario wording.
6. The safe policy for preserving untracked files needs review because automatic
   staging may leak secrets.
7. Remote resume-prompt spool placement is not yet selected.
8. SMTP configuration is absent, so a live email cannot yet be tested.
9. Prompt and email exactly-once delivery cannot be guaranteed across every
   crash without backend idempotency.
10. The sandbox's Git ownership mismatch prevents a truthful dogfood commit/push
    test from this execution identity; the watchdog must fail closed rather than
    alter trust configuration.

## 7. Exact files modified or created

Created:

- `probe_report.md`
- `architecture.md`
- `doc/Progress/progress_2026_08_29_cpx01.md`

No source code, configuration, credentials, research repository, or permanent
Codex/VS Code state was modified.

## 8. Recommended next step — not performed

After review, append an approved instruction under `## comment` in this file.
The recommended checkpoint 2 scope is only a minimal same-session shim
feasibility test:

1. identify a supported extension-host hook or explicitly approved tiny shim;
2. expose the exact selected window/workspace/thread and live status;
3. prove local `active -> idle`, final-output cursor, verbatim prompt delivery to
   that same idle thread, and return to `active`;
4. repeat with a real Remote-SSH Codex thread; and
5. map extension/network loss to `UNKNOWN`.

If these contracts cannot be proved, record a hard feasibility blocker. Do not
substitute a fresh App Server or unrelated conversation.

## comment

## Checkpoint 2 — Replace live-state shim as the default path; prove hook-based short-stop recovery and UI-based long-stop wake-up

The Phase-0 probe was useful, but subsequent review changes the preferred architecture substantially.

Do **not** proceed directly to a general live-session/App-Server shim. The next checkpoint should first test whether native Codex hooks plus a very small long-stop UI wake adapter eliminate the need for such a shim.

The design goal remains deliberately narrow: the watchdog is a deterministic sensor/courier. It must not become a general orchestrator or research-decision system.

### 1. Use native Codex hooks as the primary stop/attention event source

Investigate and, if feasible, use the current native Codex hook mechanism rather than polling VS Code live state.

In particular, verify the actual installed behavior of:

- `Stop`;
- `PermissionRequest`;
- any relevant `waitingOnApproval` / `waitingOnUserInput` behavior under the user's existing AutoReview configuration.

The important practical definition is not the App-Server raw `active` flag. A Codex session is effectively **stopped for the user's workflow** whenever it cannot continue autonomously without external input, including an approval/user-input wait.

The notification path is already considered solved conceptually by existing hook-based notifier projects. Do not spend this checkpoint building a large notification framework. Slack is preferred for the eventual primary notification channel, with email/SMTP retained as a minimum fallback, but notification plumbing is secondary to proving the continuation/wake path.

Explicitly inspect existing implementations such as hook-based Codex notifier/approval projects before writing redundant code.

### 2. Prove the `Stop` hook as a short-stop continuation channel

Test whether the installed Codex version supports the following exact behavior in VS Code:

```text
Codex reaches Stop
-> Stop hook receives session/cwd/last assistant output
-> hook waits for a short configurable grace period
-> a new external instruction arrives
-> hook returns a continuation/block decision with a reason/prompt
-> the SAME VS Code Codex conversation continues
```

Use a short default grace window, initially:

```text
10 minutes
```

Make this configurable; 5–30 minutes should be reasonable. Do not design a multi-hour or multi-day synchronous hook wait.

The grace window exists only for the common case where the user is nearby and can review the result quickly with ChatGPT.

Verify experimentally that waiting in the hook does not cause repeated model calls or continuing token consumption. Do not assume this purely from documentation.

### 3. Test repeated checkpoints and loop protection

The short-stop mechanism must not create a Stop-hook continuation loop.

Test at least several consecutive cycles:

```text
work
-> Stop
-> external prompt arrives inside grace window
-> same session continues
-> Stop again
-> wait again / park
```

Inspect and document the installed semantics of `stop_hook_active` or equivalent loop-protection state.

Every externally supplied instruction must have an identity (for example a Git commit/OID, prompt-file digest, or UUID), and the same instruction must never be consumed twice.

If the native Stop-hook semantics only permit one safe continuation or otherwise make repeated use fragile, document that clearly rather than hiding it.

### 4. Long waits must PARK completely

If no new instruction arrives during the configured short grace window:

```text
Stop hook returns normally
-> Codex becomes genuinely parked/idle
-> no model polling
-> no repeated wake-up turns
-> no ongoing token burn
```

This behavior is required for hours/days-long absences such as travel.

The ordinary watchdog process may continue to poll Git/files with normal deterministic code, but Codex itself must remain completely parked.

### 5. For PARKED -> RUNNING, prefer a minimal UI automation adapter over a deep App-Server shim

Once Codex is truly parked, native Stop-hook continuation is no longer available. This is now the only remaining hard wake-up problem.

For this rare long-stop recovery path, test a minimal Windows UI automation approach before building an invasive App-Server shim.

Preferred implementation order:

1. Windows UI Automation / accessibility tree;
2. stable VS Code commands/keyboard navigation if sufficient;
3. screenshot + lightweight vision/OCR only as fallback;
4. deep App-Server/extension-host shim only if the simpler approaches fail.

The UI wake adapter only needs to do a very narrow job:

```text
identify the VS Code window for a tracked workspace
-> activate that window
-> focus/open the existing Codex conversation/input
-> paste a deterministic mechanical prompt
-> submit it
```

Do not make it a general computer-use agent.

The major advantage of the UI path is that local and Remote-SSH sessions look the same at the UI layer. The adapter should therefore be evaluated explicitly for both local and Remote-SSH windows.

### 6. Long-stop wake prompts

Support two wake sources.

#### A. Remote Git update

After deterministic Git synchronization succeeds, inject a fixed mechanical prompt such as:

```text
The repository has been updated remotely.

Check the latest progress report for a new unprocessed `## comment`.
If there is a new actionable instruction, follow it and continue.
If there is no new actionable instruction, do not invent new work; remain idle.
```

The watchdog itself does not interpret the scientific comment.

#### B. Local `resume_prompt.md`

If an ephemeral resume prompt exists, inject its contents verbatim (with only minimal wrapper text if technically necessary).

Preserve the previously agreed lifecycle:

```text
resume_prompt.md
-> atomically claim to inflight
-> deliver once
-> Codex may recommend DISCARD or ARCHIVE
-> watchdog performs the file lifecycle action
```

If delivery certainty is lost, retain the inflight file rather than blindly retrying.

### 7. Same-conversation requirement

Both short-stop continuation and long-stop UI wake must continue the **same existing project conversation**, not silently create a fresh unrelated thread.

For UI automation, prove this empirically by placing recognizable context in the existing conversation and confirming that the wake-up turn has access to that context.

A practical MVP assumption is acceptable if necessary:

```text
one watchdog-managed primary Codex conversation per tracked VS Code window
```

Document any such assumption explicitly.

### 8. Remote-SSH must be tested with a real Codex conversation

The previous checkpoint only proved remote window/process locality; it did not have a real Remote-SSH Codex thread.

For checkpoint 2, create/use a real Remote-SSH VS Code Codex conversation and test whichever mechanisms are feasible:

- native Stop hook firing remotely;
- hook payload including useful cwd/session/last-output information;
- short-stop continuation into the same remote-backed VS Code conversation;
- long-stop UI wake from the local Windows desktop into that Remote-SSH VS Code window.

Do not weaken SSH/MFA security or require independent noninteractive SSH merely to prove the UI wake path.

### 9. Notification findings to record, not overbuild

Because existing Codex hook notifier projects already handle Stop/PermissionRequest notifications, record which implementation or pattern should be reused.

Desired eventual policy:

```text
Slack primary
email/SMTP minimum fallback
```

But do not let Slack/email implementation consume the checkpoint if the short-stop/long-stop continuation path is still unproved.

### 10. Explicit architecture preference after this review

The preferred architecture to test is now:

```text
Native Codex hooks
    |
    +-- Stop / attention event
    +-- last assistant output
    +-- short grace-window continuation
    |
    v
Deterministic watchdog
    |
    +-- Git synchronization
    +-- Slack/email notification
    +-- resume_prompt spool
    |
    v
If short grace expires: PARKED
    |
    v
Rare long-stop wake adapter
    |
    +-- Windows UI Automation first
    +-- screenshot/vision fallback
    +-- deep App-Server shim only if necessary
```

Do not implement the previously proposed live-session shim unless this checkpoint demonstrates that native hooks plus UI wake cannot meet the required contracts.

### 11. Checkpoint-2 acceptance tests

At minimum, attempt to prove:

1. A local VS Code Codex Stop event can be observed through native hooks.
2. The hook exposes enough context to identify the workspace/session and report the latest assistant output.
3. A new prompt arriving within the grace window can be injected via the Stop-hook continuation mechanism into the same conversation.
4. Repeated Stop/continue cycles do not create an infinite loop or duplicate instruction consumption.
5. With no instruction during the grace period, Codex genuinely parks without model polling/token burn.
6. A parked local VS Code conversation can be awakened by a minimal UI automation PoC and continues the same conversation.
7. Repeat the relevant Stop-hook and long-stop UI wake tests on a real Remote-SSH Codex conversation.
8. If Windows UI Automation cannot access the Codex input reliably, document exactly why and test only the smallest screenshot/vision fallback necessary.

### 12. Scope and stopping rule

This checkpoint is a **feasibility and minimal-PoC checkpoint**, not a production implementation sprint.

Do not build the full Git state machine, complete Slack/email subsystem, service installer, or broad UI automation framework yet.

Follow the repository dogfooding rule: work for at most two hours, create the next standard progress report, list exact files/tests/results, recommend the next step but do not perform it, and stop for review.
