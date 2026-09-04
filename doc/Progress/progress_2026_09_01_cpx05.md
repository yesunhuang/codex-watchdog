# Progress report - 2026-09-01 checkpoint 5 runnable foreground MVP

Start: 2026-09-01 00:14 CDT

End: approximately 2026-09-01 01:05 CDT

Elapsed: approximately 51 minutes of active work.

Scope: implement the checkpoint-5 reviewer comment as a runnable local MVP,
dogfood the trusted Stop-to-notification-to-Git path on this repository, prove
the fast-forward-to-first-party-queue path with a safe isolated Git remote,
fix concrete loop defects, document launch/configuration, and automatically
push the completed work.

Not attempted: a Windows service or scheduler installer, general Codex
ACTIVE/NOT_ACTIVE inference, an App Server shim, UI automation, Remote-SSH
acceptance, resume disposition deletion/archive, or fabricated Slack/SMTP
credentials. No new reviewer-comment section was created.

## 1. What was attempted

- Read and followed the approved checkpoint-5 scope: freeze architecture and
  prioritize a usable foreground command plus real dogfood.
- Rechecked the [official OpenAI Hooks documentation](https://learn.chatgpt.com/docs/hooks)
  and reused the already reviewed/trusted user Stop hook without changing its
  definition or Codex trust state.
- Implemented conservative tracked-change preservation, normal push, and
  exact-OID fast-forward-only Git actions on top of the existing sensor.
- Implemented environment-only Slack, SMTP, and opt-in native Windows message
  delivery with persistent transition debounce and privacy-limited results.
- Implemented one foreground orchestration loop and exposed it as
  `codex-watchdog run --interval 300`, with an immediate first cycle, one-cycle
  mode, explicit latest-Stop replay for dogfood/recovery, and Ctrl-C shutdown.
- Registered this repository and exact durable thread in the runtime already
  shared with the trusted hook.
- Exercised the real Stop, notification, tracked-only commit, normal push,
  clean remote-ahead fast-forward, exact-thread queue wake, and duplicate
  suppression paths.
- Ran focused and full tests, reviewed critical integration paths, fixed three
  concrete unattended-loop defects, committed, and automatically pushed.

## 2. What was successfully implemented / derived / verified

### One-command foreground service

The repo-local launch command is:

```powershell
$env:CODEX_WATCHDOG_WINDOWS_MSG = "1"
python tools\codex_watchdog.py --runtime .codex-watchdog\live-acceptance run --interval 300
```

The editable-install equivalent is `codex-watchdog --runtime <runtime> run
--interval 300`. The first cycle runs immediately, each cycle emits one JSON
line, the service lock is held only during a cycle, and Ctrl-C stops the
foreground process. `run --once` is available for configuration checks.

Normal first startup baselines historical audits. Explicit
`--replay-latest-stop` processes only the latest exact completed parked Stop on
the first cycle of a new service state and is documented as a recovery/dogfood
option, not a routine launch flag.

### Conservative Git actions

The mutation layer re-fetches and revalidates critical state. It:

- stages only tracked modifications/deletions with `git add -u`;
- refuses auto-staging of untracked files or pre-staged additions;
- creates the fixed commit `watchdog: preserve Codex workspace state`;
- performs only an explicit normal non-force push to the configured upstream;
- retries a clean local-ahead normal push on later cycles;
- fast-forwards a clean remote-ahead worktree only to the exact fetched
  upstream OID; and
- blocks on divergence, conflicts, operations/locks, detached/unborn HEAD,
  missing/ambiguous upstream, hooks/signing policy, authentication failure, or
  changed critical state.

It has no force-push, reset, rebase, conflict-resolution, non-fast-forward
merge, untracked deletion, trust/configuration mutation, or credential prompt
path.

### Notification and wake wiring

Notification order is Slack, SMTP, opt-in Windows `msg.exe`, then an
audit-only result. Secrets and recipients stay in environment/process memory;
durable debounce state stores only hashes and timestamps. One real Windows
message was delivered and an identical replay was suppressed.

The service scans only schema-1, exact-session, canonical-workspace, completed
parked/fail-open Stop records. A hook `return_intent` is deliberately excluded
because Codex is continuing rather than parked. A matched Stop notifies before
tracked preservation.

After a verified exact-OID fast-forward, the existing deterministic remote-OID
wake ID and fixed prompt are dispatched through first-party `codex queue` for
the registered exact thread. Pending `resume_prompt.md` has priority only when
one workspace gives it an unambiguous target.

## 3. Current best implementation state / architecture

```text
trusted native Stop hook
        |
        +-- bounded same-turn continuation when instructed
        +-- completed parked audit in shared runtime
        v
foreground `run` loop
        |
        +-- exact workspace/session Stop scan
        +-- real notification + persisted debounce
        +-- fetch and fail-closed Git observation
        +-- tracked-only commit + normal push
        +-- clean exact-OID fast-forward
        +-- first-party exact-thread queue wake
        v
atomic observation, loop state, queue journal, and privacy-limited audit
```

The architecture remains intentionally local and narrow. The MVP relies on the
native queue's idle-thread behavior and durable instruction/OID idempotence; it
does not infer a general Codex activity state or attach to the IDE App Server.

## 4. Observed runtime behavior / dogfood results

### Real Stop -> Windows notify -> tracked commit -> normal push

Runtime and registration:

```text
runtime       .codex-watchdog/live-acceptance
workspace     local-watchdog-mvp
repo          D:\projects\LocalCodexWatchDog
thread        <UUID>
```

Cycle `<UUID>` explicitly replayed only the
latest genuine exact parked Stop:

```text
audit ID      <UUID>
outcome       grace_expired_parked
hook window   2026-08-31T07:17:18.868643Z .. 07:17:48.892689Z
```

The cycle observed `HEAD == origin/main == 2c7593c`, tracked dirt true, and no
untracked files. It sent a real `windows_msg` notification, ran the tracked-only
preservation path, created `0eff3fd`, pushed it normally, and re-observed:

```text
HEAD          0eff3fd242284b6c57ca3e81218af85f9aa52134
origin/main   0eff3fd242284b6c57ca3e81218af85f9aa52134
topology      equal
dirty         false
preserve      completed; commit_created=true; pushed=true
```

Cycle `<UUID>` immediately followed with zero
Stops, notifications, preserve actions, fast-forwards, or wakes. The exact
foreground `run --interval 300` command also emitted immediate clean cycle
`<UUID>`; PTY Ctrl-C stopped it without a Python
traceback or stale service lock. The host PTY reported cancellation code 1,
while injected and direct PowerShell tests confirm the application itself
returns 0 on `KeyboardInterrupt`.

### Real fast-forward -> actual same-thread queue acknowledgement

Publishing a synthetic test commit to GitHub's shared default branch was
rejected by the execution safety reviewer before any remote mutation. The test
was moved to an ignored local bare remote instead. That safer probe still used
real Git fetch/reflog/object behavior and the actual first-party Codex queue.

Cycle `<UUID>` observed the isolated clean target
one commit behind, then verified this fast-forward:

```text
before        0eff3fd242284b6c57ca3e81218af85f9aa52134
remote target 5e1527680863434a326d2351c29935c9a295ef56
after         5e1527680863434a326d2351c29935c9a295ef56
topology      remote_ahead -> equal
```

It then received an exact native queue acknowledgement:

```text
instruction   git:572daa8d7945c9cd:5e1527680863434a326d2351c29935c9a295ef56
thread        <UUID>
status        enqueued
message ID    <UUID>
```

Cycle `<UUID>` sent no duplicate action. A
read-only queue observation still reports `enqueued`; this checkpoint does not
claim that this new queued turn started while the present thread remained
active. Checkpoint 3 already retained separate live same-thread `started`
evidence for the same first-party mechanism.

### Concrete defects found and fixed

1. The audit cursor originally advanced to the latest file at cycle end. A Stop
   completed during Git work could therefore be skipped without ever being
   scanned. The cursor now advances only to the immutable audit snapshot that
   the cycle actually scanned; a race regression test proves next-cycle pickup.
2. Every completed Stop outcome was initially accepted, including
   `return_intent`. That could label Codex parked and commit while its retained
   turn continued. An explicit parked/fail-open outcome allowlist now excludes
   continuation intent; the confirmed parked audit triggers exactly once.
3. A successful mechanical commit followed by a failed push left a clean
   local-ahead branch, but the next cycle required another Stop before retrying.
   Ordinary cycles now retry only an already-clean local-ahead normal push.

No destructive Git recovery or duplicate queue send occurred during these
fixes.

## 5. Tests and sanity checks performed

- Full suite after implementation: **138 passed, 1 skipped** (139 collected).
- Final full suite after all three hardening fixes: **141 passed, 1 skipped**
  (142 collected).
- Final focused MVP/CLI suite: **15 passed**.
- Combined Git mutation, notification, MVP, and CLI focused suite: passed.
- Git mutation suite: **11 passed**.
- Notification suite: **13 passed**.
- Initial MVP orchestration suite: **9 passed**; final suite adds three concrete
  regression tests.
- `python -m compileall -q src tools tests` -> passed.
- `python -m black --check src tests tools` -> passed after mechanical
  formatting.
- `git diff --cached --check` -> passed before each commit.
- Owner-context normal push dry-run authenticated; repository hooks and Git
  commit/push signing settings were absent.
- Real first and second Stop/Git cycles, real Windows notification delivery and
  suppression, real local-remote fast-forward and repeat, actual queue
  acknowledgement, and exact queue journal observation all passed.

## 6. Unresolved technical issues / limitations

- The queued dogfood wake is durably `enqueued`, not newly observed `started`,
  because this exact Codex thread was active during the checkpoint.
- Slack and SMTP capability is implemented and tested with injected transports,
  but no real credentials were available. Native Windows delivery was the real
  exercised channel.
- The trusted local hook currently uses the prior 30-second acceptance grace,
  not the documented 600-second production default. Changing its exact command
  requires another manual `/hooks` review/trust action.
- Remote-SSH owning-locality hook, park, Git, and queue acceptance remains
  pending and was not allowed to block the local MVP.
- The watchdog is a foreground process. It can be left running, but it has no
  service installer, automatic restart, scheduler integration, or log
  rotation.
- A final hidden detached launch was rejected by the execution safety reviewer
  before process creation because it would retain autonomous future
  commit/push/wake authority. No background watchdog was left behind; the exact
  foreground command and Ctrl-C lifecycle were live-tested successfully.
- Brand-new/untracked files are deliberately not auto-staged. Initial MVP files
  and new progress reports therefore require a reviewed manual bootstrap
  commit. Pre-staged additions are also blocked conservatively.
- Configured Git hooks or commit/push signing block automatic mutation rather
  than attempting an interactive or policy-changing path.
- Resume prompt disposition parsing and DISCARD/ARCHIVE actions remain absent.
- Runtime directories may contain plaintext prompts, absolute paths, and stable
  thread IDs and still require user-only permissions and retention management.

## 7. Exact files modified or created

Checkpoint-5 tracked files after reviewer commit `00e5cc3`:

```text
README.md
architecture.md
config.example.toml
src/codex_watchdog/cli.py
src/codex_watchdog/git_mutations.py
src/codex_watchdog/mvp_service.py
src/codex_watchdog/notifications.py
tests/test_git_mutations.py
tests/test_mvp_service.py
tests/test_notifications.py
tests/test_service_cli.py
doc/Progress/progress_2026_09_01_cpx05.md
```

Pushed implementation/dogfood commits before this closing report:

```text
2c7593c  watchdog: add runnable foreground MVP
0eff3fd  watchdog: preserve Codex workspace state   (created/pushed by watchdog)
ba55e58  watchdog: harden foreground event loop
```

Ignored local evidence includes:

```text
.codex-watchdog/live-acceptance/
.codex-watchdog/cpx05-notifier-live-20260901/
.codex-watchdog/cpx05-wake-dogfood/
```

The unpushed synthetic commit used to advance the isolated bare remote exists
only under ignored dogfood storage. It was never published to GitHub.

## 8. Recommended next step - not performed

After this queued wake is allowed to start on the now-idle exact thread,
observe its existing queue record and retain `started` evidence if available.
Then configure a real Slack webhook in the foreground process environment and
run the current MVP for ordinary work long enough to collect actual transient
Git/auth/notification failures. Harden only those observed failures.

Separately, when convenient, replace the 30-second local acceptance hook with
the reviewed 600-second production command and manually trust that exact new
definition. Remote-SSH and service installation remain independent follow-up
work; neither should reopen the already accepted local continuation/queue
architecture.

## comment

## Checkpoint 6 — Fix the remaining MVP workflow bugs, then guide the user through real SMTP setup and live testing

Checkpoint 5 is approved. The foreground MVP is now useful enough to keep dogfooding. Do not expand the architecture in this cycle. Fix the concrete workflow gaps below, then switch to interactive user setup/testing.

### 1. Fix automatic publication of new progress reports

The current tracked-only policy breaks the core reporting workflow because a newly created `doc/Progress/progress_*.md` is untracked on its first checkpoint and therefore never reaches GitHub automatically.

Add a **very narrow untracked allowlist** for watchdog-owned progress reports only, for example:

```text
doc/Progress/progress_*.md
```

Requirements:

- Continue to use tracked-only staging for all ordinary changes.
- Automatically add only new files matching the exact reviewed progress-report allowlist.
- Do not use `git add -A` or broad untracked staging.
- All other untracked files remain untouched and should still trigger the existing attention notification.
- Reject suspicious path traversal/symlink/non-regular-file cases rather than broadening the allowlist.
- Dogfood this by creating the next progress report as a genuinely new file and proving the watchdog can include and push it without a manual bootstrap `git add`.

This is a required MVP fix because the automatic Codex -> GitHub -> reviewer loop depends on it.

### 2. Add only a minimal guard for the active-worktree fast-forward race

I previously flagged one concrete race: a repository may be clean while Codex is actively reasoning/reading it, so blindly fast-forwarding any clean `remote_ahead` worktree could change files underneath an active turn.

Do **not** build a general ACTIVE/NOT_ACTIVE state machine or App-Server shim.

Add the smallest fail-closed guard you can justify from existing durable Stop/rollout/queue evidence. The goal is simply:

```text
if there is positive recent evidence that the exact thread is parked -> automatic ff-only is allowed
if later evidence shows a turn started, or the ordering is ambiguous -> do not mutate the worktree; notify/defer
```

Keep this deliberately conservative. Missing a fast-forward for one cycle is acceptable; mutating underneath an active Codex turn is not. If the minimal evidence check becomes complicated, stop and classify the case as unsafe rather than inventing another architecture layer.

### 3. Make real SMTP testing the main user-facing task of this checkpoint

Email is important to the user. SMTP code already exists; do not redesign it. After the two fixes above are implemented and tests are green, **guide the user step-by-step in the active Codex conversation to configure and test a real mailbox**.

Do not ask the user to put any password/app-password/token into Git, progress reports, tracked config, runtime audit JSON, or chat messages that will be persisted to the repository. Credentials must remain in process/user environment or an appropriate local secret mechanism.

The guide should:

1. Ask/identify which mail provider the user wants to use only when provider-specific SMTP settings are actually needed.
2. Explain the required environment variables:
   - `CODEX_WATCHDOG_SMTP_HOST`
   - `CODEX_WATCHDOG_SMTP_PORT`
   - `CODEX_WATCHDOG_SMTP_USERNAME`
   - `CODEX_WATCHDOG_SMTP_PASSWORD`
   - `CODEX_WATCHDOG_SMTP_FROM`
   - `CODEX_WATCHDOG_SMTP_TO`
   - `CODEX_WATCHDOG_SMTP_SECURITY`
3. Give exact PowerShell commands/templates for the selected provider, using placeholders for secrets.
4. If the provider requires an app password or SMTP-specific credential, explain how the user should create/use it without echoing or committing the secret.
5. Provide the smallest direct live SMTP test. If necessary, add a tiny `notify-test` / equivalent CLI command rather than manufacturing a fake Codex Stop. Do not build a new notification framework.
6. Confirm from watchdog output that the SMTP transport reports `sent` or `sent_fallback`, and ask the user to confirm the test email actually arrived.
7. Test the fallback behavior once: with Slack absent or intentionally unavailable, confirm SMTP still delivers.
8. Keep notification debounce from suppressing the deliberate second test by using a distinct test fingerprint/event.

Do not declare SMTP live-verified until a real mailbox receives a real message.

### 4. After SMTP succeeds, run the MVP for real

Once the user has confirmed a live email delivery, provide the exact final foreground launch command with the chosen environment variables and the current runtime/workspace registration.

Keep the watchdog running in foreground dogfood mode. From this point forward, prefer:

```text
run -> inspect real audit/log -> fix observed bug -> continue running
```

over speculative hardening or large new abstractions.

Remote-SSH, service installation, Slack, and resume disposition can remain follow-up work unless a real blocker appears during this cycle.

### 5. Scope / stopping rule

This checkpoint is successful when:

- a new `doc/Progress/progress_*.md` can be safely auto-added/committed/pushed;
- the remote-ahead mutation path has a minimal fail-closed protection against changing a clearly active/ambiguous Codex worktree;
- a real SMTP account is configured without committing secrets;
- a real test email is received;
- SMTP fallback is live-tested; and
- the user is given the exact command to leave the MVP running.

Do not spend the cycle increasing test count for hypothetical cases. Keep existing tests green, add only focused regressions for the two fixes, and use live dogfood as the primary audit mechanism. Work for at most two hours of active implementation before the next progress report.