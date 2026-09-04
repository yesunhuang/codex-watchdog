# Implementation Plan — Lightweight VS Code Codex Watchdog

## 0. Goal

Implement a lightweight deterministic local watchdog for open VS Code Codex sessions.

The watchdog is **not** an autonomous agent and must not make research decisions.

Its only responsibilities are:

```text
observe
→ preserve state
→ synchronize Git when safe
→ notify user
→ mechanically forward approved prompts
```

The intended workflow is:

```text
VS Code + Codex
      ↕
local watchdog
      ↕
Git repository / GitHub
      ↕
ChatGPT + user
```

The user should normally interact only with ChatGPT.

Do not build a general agent orchestrator, MCP service, web dashboard, VS Code extension, task database, or multi-agent framework unless later evidence shows one is necessary.


---

## 0.1 Dogfooding requirement — develop the watchdog using the watchdog workflow

The implementation of `codex-watchdog` itself is the first real end-to-end test of this workflow.

Codex must therefore develop this project **under the same checkpoint discipline that the watchdog is intended to support**.

### Hard development-time limit

Every Codex implementation run must stop after at most:

```text
2 hours
```

of active work.

Do not continue past the two-hour boundary merely because a subtask is almost finished.

At or before the boundary, Codex must:

1. leave the repository in a coherent preservable state;
2. run whatever tests/checks are appropriate for the work completed so far;
3. create a new progress report;
4. record all unresolved issues and partial work;
5. recommend the next step but **not perform it**;
6. stop and wait for review.

This requirement applies throughout:

```text
Phase 0
Phase 1
Phase 2
Phase 3
Phase 4
```

and any debugging/fix cycle.

### Progress-report location and naming

Use the same established convention as the existing research workflow:

```text
doc/Progress/
    progress_YYYY_MM_DD_cpxNN.md
```

Create `doc/Progress/` in the watchdog repository if it does not yet exist.

Do not hard-code one fixed filename.

Use the next checkpoint number appropriate for that date/session.

### Required progress-report structure

Each report should follow the established format as closely as the task allows:

```markdown
# Progress report — YYYY-MM-DD checkpoint N <short task description>

<start/end time, elapsed time, scope, and explicit statement of what was not attempted>

## 1. What was attempted

## 2. What was successfully implemented / derived / verified

## 3. Current best implementation state / architecture

## 4. Observed runtime behavior / probe results

## 5. Tests and sanity checks performed

## 6. Unresolved technical issues / limitations

## 7. Exact files modified or created

## 8. Recommended next step — not performed
```

For implementation checkpoints, adapt scientific headings only where necessary, but preserve the same reporting philosophy:

```text
scope
results
current best state
checks
unresolved issues
exact files
recommended next step — not performed
```

### Feedback protocol

After Codex writes the checkpoint report, it must stop.

The approved next instruction will be appended to the same progress file under:

```markdown
## comment

<approved next-checkpoint instruction>
```

Codex must not create, modify, or fabricate the `## comment` section itself.

When resumed, Codex should:

1. read the previous checkpoint report;
2. read the appended `## comment`;
3. treat that comment as the approved scope for the next run;
4. work for at most another two hours;
5. create the next progress report;
6. stop again.

### Self-hosting transition

Before the watchdog is functional enough to monitor itself, Codex should follow the two-hour checkpoint protocol directly as a development rule.

As soon as the watchdog can reliably:

```text
detect ACTIVE → NOT_ACTIVE
capture the latest Codex output
preserve/push repository state
detect remote updates
send a mechanical wake-up prompt
```

switch the watchdog repository into **dogfood mode**:

```text
the watchdog monitors the VS Code/Codex session that is implementing the watchdog itself
```

From that point onward, use the watchdog's own development repository/session as a continuous real-world integration test.

Do not disable the watchdog merely because a failure is inconvenient.

Instead, record watchdog failures as implementation bugs in the next progress report.

### Why this is required

This development workflow is part of the acceptance test.

It verifies under real conditions that the system can survive:

```text
normal Codex stops
unexpected Codex stops
partial local changes
Git synchronization
remote progress/comment updates
mechanical wake-up prompts
resume_prompt handling
email notification
multi-checkpoint continuity
```

The watchdog should not be considered production-ready until it has successfully supported its own development across multiple two-hour checkpoints.

---

## 1. Core philosophy

The watchdog must remain intentionally stupid.

It must **not** determine:

- whether a Codex stop was scientifically normal or abnormal;
- whether Codex's latest result is correct;
- whether a new research direction is appropriate;
- whether an experiment should be redesigned;
- whether an old task should be abandoned;
- whether a GitHub update contains an important scientific change.

Those decisions belong to:

```text
user + ChatGPT
```

The watchdog only recognizes deterministic machine states.

At minimum:

```text
ACTIVE
NOT_ACTIVE
UNKNOWN
```

`UNKNOWN` must not automatically be treated as a stop.

---

## 2. Primary workflows

### Workflow A — Codex stops

When a tracked Codex session changes from:

```text
ACTIVE
→
NOT_ACTIVE
```

the watchdog should:

1. capture the latest available Codex output/message on a best-effort basis;
2. inspect Git state;
3. if safe local changes exist, preserve them with a commit;
4. push them if pushing is safely possible;
5. email the user with:
   - repository/workspace;
   - Codex state transition;
   - last Codex output;
   - Git status;
   - whether changes were committed;
   - whether push succeeded;
   - any error encountered.

The watchdog does **not** classify the stop as:

```text
checkpoint
approval problem
remote failure
normal completion
unexpected termination
```

The same procedure applies to all stops.

### Workflow B — Remote Git changes while Codex is inactive

If:

```text
Codex == NOT_ACTIVE
AND
remote Git branch changed
```

then:

1. `git fetch`;
2. determine whether the remote update can be applied as a strict fast-forward;
3. if safe, perform:

```text
git pull --ff-only
```

4. mechanically prompt the existing Codex session:

```text
The repository has been updated remotely.

Check the latest progress report and determine whether there is a new
unprocessed comment/instruction.

If there is a new actionable comment, follow it and continue the work.

If there is no new actionable comment, do not invent new work.
Remain idle.
```

5. observe the resulting Codex state;
6. if Codex remains inactive, notify the user by email that:
   - the remote update was pulled;
   - Codex checked it;
   - no new actionable instruction appears to have caused continuation.

The watchdog itself does not parse or interpret the progress comment.

Codex performs that semantic check.

### Workflow C — Ephemeral resume prompt

Support a local file:

```text
resume_prompt.md
```

for operational recovery when no permanent Git progress comment is needed.

Example use:

```text
Codex stopped because of approval / remote / transient operational issue
↓
user discusses with ChatGPT
↓
no research decision changed
↓
ChatGPT/user creates resume_prompt.md
↓
watchdog forwards it to the stopped Codex session
```

The prompt is local and should normally not be committed to Git.

Suggested runtime location:

```text
.codex-watchdog/
    resume_prompt.md
    inflight/
    archive/
```

This directory should normally be gitignored.

---

## 3. Resume-prompt lifecycle

The lifecycle must prevent accidental loss or repeated consumption.

Recommended behavior:

```text
resume_prompt.md
↓
watchdog detects file
↓
rename/move atomically to:
inflight/<timestamp>_resume_prompt.md
↓
send content to target Codex session
↓
Codex processes prompt
```

Codex should be instructed to decide whether the prompt contains information worth retaining.

At the end of its handling, it should emit one of:

```text
RESUME_PROMPT_DISPOSITION: DISCARD
```

or:

```text
RESUME_PROMPT_DISPOSITION: ARCHIVE
```

The watchdog then performs the file operation.

### DISCARD

Delete the inflight prompt after successful handling.

### ARCHIVE

Move it to:

```text
archive/
```

with timestamp and repository/session metadata.

### Codex stops before disposition

Do not delete the prompt.

Keep it under:

```text
inflight/
```

for recovery/manual inspection.

The watchdog handles file lifecycle.

Codex only makes the semantic archive/discard recommendation.

---

## 4. Git behavior

Git operations must be conservative and deterministic.

### On Codex stop

Run:

```text
git status --porcelain
```

If there are local changes and the repository is in a safe state, commit them.

A generic commit message is acceptable, e.g.:

```text
watchdog: preserve Codex workspace state
```

Do not try to infer whether this is a scientific checkpoint.

If the Codex progress report itself has a more appropriate normal commit already prepared, preserve that behavior where possible.

### Safe automatic push conditions

Automatic push is allowed only when:

- current branch is known;
- branch has a configured upstream;
- no merge conflict exists;
- no rebase is in progress;
- no merge is in progress;
- no cherry-pick is in progress;
- HEAD is not detached;
- remote/local history is compatible with a normal push;
- credentials work normally.

Never:

```text
git push --force
git push --force-with-lease
git reset --hard
```

Never automatically resolve divergence.

---

## 5. Remote-update behavior

Always use:

```text
git fetch
```

before deciding what to do.

If:

```text
remote == local
```

do nothing.

If:

```text
remote is strictly ahead of local
AND
working tree is clean
```

perform:

```text
git pull --ff-only
```

If local changes exist, preserve/push them first if safe and then re-evaluate.

If histories diverge:

```text
do not merge
do not rebase
do not reset
```

Send an email and stop automatic Git handling for that workspace.

---

## 6. Email notification

Email is the interruption channel.

The watchdog should send concise emails for important state transitions.

### Codex stopped

Example:

```text
Subject:
[Codex Watchdog] QLSCI stopped

Workspace:
QLSCI

Previous state:
ACTIVE

Current state:
NOT_ACTIVE

Last Codex output:
--------------------------------
<best-effort latest response>
--------------------------------

Git:
Local changes: yes
Commit created: yes
Push: successful
HEAD: abc123

Watchdog action:
Workspace state preserved.
```

### Remote update checked but no continuation

Example:

```text
Subject:
[Codex Watchdog] QLSCI remote update checked

Remote repository changed while Codex was inactive.

Fast-forward pull:
successful

Codex was asked to inspect the latest progress report.

Current Codex state:
NOT_ACTIVE

No continued work was observed.
```

### Error

Example:

```text
Subject:
[Codex Watchdog] QLSCI needs attention

Reason:
Git histories diverged.

Local:
abc123

Remote:
def456

No merge, rebase, reset, or force push was attempted.
```

---

## 7. Notification debounce

Do not repeatedly send identical notifications every polling cycle.

Track at least:

```text
event type
workspace
session
Git HEAD
timestamp
```

Suppress duplicate notifications for the same unchanged event.

A simple configurable debounce window such as:

```text
30 minutes
```

is sufficient.

A real new state transition should still produce a new notification immediately.

---

## 8. Polling

Default interval:

```text
5 minutes
```

Configurable.

Example:

```toml
poll_seconds = 300
```

The implementation should also support a shorter development/test interval.

Do not attempt sub-second real-time monitoring in the MVP.

The watchdog should prioritize robustness and low complexity.

---

## 9. VS Code workspace discovery

The watchdog should automatically track all currently open VS Code windows that satisfy:

```text
1. a Git repository is associated with the workspace;
2. an active/known Codex session exists for that workspace/window.
```

Do not require the user to manually register every repository if reliable automatic discovery is possible.

However, provide optional configuration to exclude repositories.

Example:

```toml
[tracking]
exclude = [
    "scratch",
    "test-repo"
]
```

The discovery layer must distinguish multiple simultaneously open VS Code windows.

---

## 10. Locality support

The watchdog must investigate and support at least:

```text
local VS Code workspace
VS Code Remote-SSH workspace
```

If practical, design the abstraction so later support can include:

```text
WSL
Dev Container
```

without changing watchdog core logic.

The core should think in terms of:

```text
WorkspaceAdapter
```

rather than assuming all repositories live on the Windows filesystem.

For each tracked workspace, determine:

```text
workspace identity
repository path
execution locality
Codex session identity
Git execution context
```

Do not accidentally run local Windows Git against a repository that actually lives on a remote SSH host.

---

## 11. Main technical probe — do this before implementation

Before building the watchdog, perform a focused probe.

Do not begin the full implementation until these questions are answered empirically.

For both:

```text
A. local VS Code Codex session
B. Remote-SSH VS Code Codex session
```

determine whether we can reliably obtain:

### A. Workspace/window identity

Can we identify:

```text
VS Code window
workspace
repository
```

and maintain a stable mapping?

### B. Codex session identity

Can we identify the Codex session/thread associated with that VS Code workspace?

### C. Codex activity state

Can we reliably distinguish:

```text
ACTIVE
NOT_ACTIVE
UNKNOWN
```

Preferably also record richer states if reliably available, but the core logic must not require them.

### D. Last Codex output

Can we retrieve the latest assistant/Codex response shown for that session?

This is best-effort functionality.

Failure to retrieve the last output must not prevent the rest of the watchdog from operating.

### E. Prompt injection

Can we send a mechanical prompt to the **already-open existing Codex session**?

This must target the correct session.

Do not create a fresh unrelated Codex conversation unless there is no alternative.

---

## 12. State detection safety

The watchdog must avoid false stops due to transient failure.

If the previous state was:

```text
ACTIVE
```

and one poll returns:

```text
UNKNOWN
```

do not immediately announce a stop.

Suggested rule:

```text
ACTIVE → UNKNOWN
→ wait for next poll
```

Only classify as a real disappearance/problem after configurable consecutive unknown detections.

Example:

```toml
unknown_threshold = 2
```

If a reliable positive `NOT_ACTIVE` state is available, it may trigger immediately.

---

## 13. Best-effort last-output capture

Implement last-output capture as an adapter.

Do not tightly couple watchdog correctness to one Codex implementation detail.

Preferred sources, in order:

```text
1. official/session-facing Codex state/API if available
2. Codex persisted thread/session data
3. VS Code extension/session state
4. logs/output channel
```

Do not use screen OCR or image scraping.

Expose:

```python
get_last_codex_output(session) -> str | None
```

If unavailable, email:

```text
Last Codex output: unavailable
```

and continue.

---

## 14. Sending prompts

Expose a single adapter method:

```python
send_prompt(session, text)
```

It must support at least two prompt types.

### Git-update probe

Fixed mechanical prompt:

```text
The repository has been updated remotely.

Check the latest progress report for a new unprocessed comment.

If there is a new actionable instruction, follow it and continue.

If there is no new actionable instruction, do not invent new work.
Remain idle.
```

### Resume prompt

Send the content of:

```text
resume_prompt.md
```

verbatim, plus only minimal wrapper text if necessary.

The watchdog must not rewrite or summarize the prompt.

---

## 15. Persistent watchdog state

Store local state outside Git or under a gitignored runtime directory.

Suggested:

```text
~/.codex-watchdog/state.json
```

Track per workspace:

```text
workspace_id
repo_path
execution_locality
branch
remote
session_id
last_codex_state
last_local_head
last_remote_head
last_seen_time
last_state_transition
last_email_event
last_prompt_consumed
```

The state file exists only to avoid duplicate actions and maintain continuity.

Do not turn it into a task database.

Use atomic writes.

---

## 16. Architecture

Recommended modular structure:

```text
codex-watchdog/
    core/
        watchdog.py
        state.py
        config.py

    adapters/
        vscode.py
        codex.py
        git.py
        email.py

    environments/
        local.py
        remote_ssh.py

    runtime/
        resume_prompt.py

    tests/
```

Core logic must not depend directly on VS Code/Codex internals.

For example:

```python
workspace_adapter.list_workspaces()

codex_adapter.get_state(workspace)

codex_adapter.get_last_output(workspace)

codex_adapter.send_prompt(workspace, prompt)

git_adapter.inspect(workspace)

git_adapter.safe_push(workspace)

git_adapter.safe_fast_forward(workspace)
```

This allows Codex/VS Code implementation details to change without rewriting watchdog logic.

---

## 17. Main loop

Conceptually:

```text
every N minutes:

    discover tracked VS Code workspaces

    for each workspace:

        inspect Codex state
        inspect Git local/remote state

        CASE 1:
        previous Codex state == ACTIVE
        current Codex state == NOT_ACTIVE

            capture last Codex output

            preserve local Git changes if safe

            push if safe

            email user

        CASE 2:
        Codex state == NOT_ACTIVE
        remote Git changed

            fetch

            safe ff-only update

            send mechanical "check progress comment" prompt

            later observe whether Codex resumed

            if it remains inactive:
                email user

        CASE 3:
        Codex state == NOT_ACTIVE
        resume_prompt exists

            atomically move prompt to inflight

            send prompt to Codex

            await Codex disposition if available

            DISCARD:
                delete

            ARCHIVE:
                archive

            no disposition / failure:
                retain inflight file

        CASE 4:
        ambiguous state or unsafe Git situation

            do not improvise

            notify user if material
```

---

## 18. Ordering when multiple triggers happen

Use deterministic priority.

Recommended:

```text
1. protect/preserve local changes
2. resolve Git synchronization if safely possible
3. consume explicit resume_prompt
4. otherwise react to remote Git update
```

An explicit local `resume_prompt` should normally take precedence over a generic “remote updated, inspect progress” wake-up after repository state has been safely synchronized.

Do not send two prompts concurrently to the same Codex session.

Use a per-session lock.

---

## 19. Git commit behavior after stop

Do not attempt to determine whether the stopped work constitutes a complete checkpoint.

If there are safe uncommitted changes, preserve them.

A generic commit message such as:

```text
watchdog: preserve Codex state after stop
```

is acceptable.

If Codex itself already made a meaningful commit, do not create a useless empty/synthetic commit.

Do not require a progress report to exist before preserving work.

---

## 20. Security boundaries

Hard rules:

```text
no force push
no hard reset
no automatic merge conflict resolution
no arbitrary credential storage
no MFA bypass
no VPN bypass
no automatic secret-file handling
```

If Git authentication fails:

```text
email user
leave workspace untouched
```

If a repository is in a complex Git state:

```text
rebase
merge
cherry-pick
bisect
detached HEAD
conflicts
```

do not try to fix it.

Notify and stop automated Git actions for that workspace.

---

## 21. Email implementation

Keep email provider support simple.

Prefer one of:

```text
SMTP using an app password / existing secure credential mechanism
or
local mail provider/API already available to the user
```

Do not commit credentials.

Use environment variables, OS credential storage, or a user-local config excluded from Git.

Provide:

```text
watchdog test-email
```

for setup validation.

---

## 22. CLI

Provide a minimal CLI:

```text
codex-watchdog probe
codex-watchdog run
codex-watchdog once
codex-watchdog status
codex-watchdog test-email
```

### `probe`

This is the most important initial command.

It should print detected:

```text
VS Code windows
workspace
repo path
environment/locality
Codex session
Codex state
last output availability
prompt-send capability
```

Example:

```text
Window 1
  Workspace: QLSCI
  Environment: remote-ssh:rcc
  Repo: /home/.../QLSCI
  Codex session: found
  Codex state: ACTIVE
  Last output: available
  Send prompt: available
```

Do not modify repository or Codex state during normal `probe`.

---

## 23. Phase 0 — feasibility probe

This phase is mandatory.

Before implementing the watchdog core:

1. open one local VS Code repository with a Codex session;
2. open one Remote-SSH VS Code repository with a Codex session;
3. identify the actual mechanism for:
   - workspace discovery;
   - session mapping;
   - active/inactive detection;
   - last-output extraction;
   - prompt injection;
4. test Codex active → idle transitions;
5. test remote workspace behavior;
6. document all implementation details and fragility.

Deliver a short probe report.

If any part requires relying on unstable/private internals, clearly document that fact and propose the least fragile fallback.

Do not hide uncertainty.

---

## 24. Phase 1 — minimal watchdog

Implement only:

```text
workspace discovery
Codex ACTIVE/NOT_ACTIVE/UNKNOWN detection
last-output capture
Git status
safe preserve commit
safe push
email on active → inactive
```

Test this for several days/work cycles before adding automatic wake-up.

This phase alone is already useful.

---

## 25. Phase 2 — Git wake-up

Add:

```text
remote Git polling
fetch
fast-forward-only pull
mechanical progress-check prompt
email if Codex remains idle
```

Do not parse research comments in the watchdog.

---

## 26. Phase 3 — resume_prompt

Add:

```text
resume_prompt.md
inflight handling
prompt forwarding
Codex archive/discard disposition
archive storage
```

Make prompt consumption idempotent.

---

## 27. Phase 4 — Remote-SSH hardening

Verify all previous behavior for:

```text
VS Code Remote-SSH
```

including:

```text
Codex state
last output
prompt injection
Git execution locality
network interruptions
remote extension-host restart
```

Do not assume local and remote VS Code internals are identical.

---

## 28. Tests

### Unit tests

Test:

```text
state transitions
debounce
Git topology decisions
resume_prompt lifecycle
archive/discard parsing
email formatting
configuration
```

### Git integration tests

Use temporary bare remotes.

Test:

```text
clean push
dirty workspace preservation
remote fast-forward
divergence
detached HEAD
merge conflict
credential failure simulation
```

### Codex adapter tests

Where possible, build a fake adapter that simulates:

```text
ACTIVE
NOT_ACTIVE
UNKNOWN
last output
prompt accepted
prompt rejected
```

The watchdog core should be fully testable without a real Codex session.

---

## 29. Logging

Keep local structured logs.

Example:

```text
2026-08-29 22:10 QLSCI ACTIVE
2026-08-29 22:15 QLSCI NOT_ACTIVE
2026-08-29 22:15 last-output captured
2026-08-29 22:15 git commit abc123
2026-08-29 22:16 push success
2026-08-29 22:16 email sent
```

Do not log credentials or full sensitive environment variables.

---

## 30. Acceptance criteria

The MVP succeeds if the following scenarios work.

### Scenario A — ordinary completion

```text
Codex active
→ Codex stops
→ watchdog captures final output
→ local changes committed/pushed if needed
→ user receives email
```

No attempt is made to classify the stop.

### Scenario B — approval/operational stop

```text
Codex active
→ Codex stops/waits
→ watchdog captures last message
→ preserves/pushes local state
→ emails user
→ user + ChatGPT decide no scientific update is needed
→ resume_prompt.md created
→ watchdog sends it to same Codex session
→ Codex continues
```

### Scenario C — formal new instruction

```text
Codex inactive
→ user + ChatGPT update Git progress/comment remotely
→ watchdog sees remote update
→ ff-only pull
→ watchdog asks Codex to inspect latest progress
→ Codex finds new comment
→ Codex resumes work
```

### Scenario D — unrelated remote update

```text
Codex inactive
→ remote Git changes
→ watchdog pulls
→ asks Codex to inspect progress
→ Codex finds no actionable comment
→ Codex remains idle
→ watchdog emails user
```

### Scenario E — Git ambiguity

```text
Codex stops
→ local/remote Git diverged
→ watchdog does not merge/rebase/force
→ user receives error email
```

### Scenario F — transient detector failure

```text
Codex ACTIVE
→ one UNKNOWN poll
→ no false stop email
→ next poll recovers ACTIVE
```

---


### Scenario G — self-hosting / dogfood development

```text
Codex is implementing codex-watchdog
→ works for no more than 2 hours
→ writes the standard progress report
→ stops
→ watchdog detects the stop and preserves/pushes state
→ user + ChatGPT review the report
→ approved instruction is appended under ## comment
→ watchdog detects the remote update
→ Codex is mechanically prompted to inspect the progress comment
→ Codex resumes the same implementation workflow
→ repeats for multiple checkpoints
```

The implementation project itself should exercise this scenario repeatedly before production use.

## 31. Deliverables

Produce:

```text
README.md
doc/architecture.md
doc/codex_watchdog_implementation_plan.md
doc/probe_report.md
config.example.toml
src/...
tests/...
```

Documentation should include:

```text
installation
email configuration
poll interval
VS Code detection mechanism
Codex detection mechanism
Remote-SSH behavior
resume_prompt usage
Git safety policy
troubleshooting
known fragile dependencies
```

Do not modify production research repositories during development except for explicitly approved disposable testing.

---

## 32. Scope control

Do not expand this checkpoint into:

```text
a VS Code extension
an autonomous agent
a GitHub bot
an MCP server
a ChatGPT wrapper
a generic workflow engine
a research-decision system
```

unless the Phase-0 probe proves that a very small extension/shim is strictly necessary for reliable Codex session inspection or prompt injection.

If such a component is necessary, implement only the minimum adapter needed and keep all policy/state-machine logic in the standalone watchdog.

---


## 32.1 Mandatory implementation stopping rule

For the implementation of this project, the following rule overrides convenience:

```text
No Codex implementation run may exceed 2 hours without producing a checkpoint report and stopping for review.
```

If a task cannot be completed within the current two-hour window:

```text
document partial completion
preserve the repository
record tests performed
record the exact blocker/remaining work
recommend the next step
stop
```

Do not silently roll into a third hour.

The purpose is both workflow discipline and continuous validation of the system being built.

---

## Final design principle

The watchdog should behave like:

```text
sensor + courier + backup clerk
```

not like a manager.

Its job is to notice, preserve, synchronize, notify, and forward.

All meaningful decisions remain with:

```text
user + ChatGPT
```
