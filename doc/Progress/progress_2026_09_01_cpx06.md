# Progress report - 2026-09-01 checkpoint 6 workflow safety and live SMTP setup

Start: 2026-09-01 01:26 CDT

Implementation snapshot: 2026-09-01 02:26 CDT

Auto-publication dogfood: 2026-09-01 02:31 CDT

Personal Outlook OAuth and live SMTP acceptance: 2026-09-01 03:27 CDT

Elapsed at snapshot: approximately 60 minutes of active work.

Scope: implement the approved checkpoint-6 fixes for first-publication of new
progress reports and the active-worktree fast-forward race, add the smallest
direct notification test command needed for live SMTP acceptance, dogfood the
new report path on this repository, then guide the user through real mailbox
delivery and foreground launch.

Not attempted: Remote-SSH acceptance, service/scheduler installation, Slack
setup, resume disposition, a general ACTIVE/NOT_ACTIVE state machine, App
Server integration, UI automation, or any tracked/persisted notification
credential. No reviewer-comment section was created.

This report was intentionally created as a genuinely new untracked file. Its
first mechanical commit/push was the checkpoint's production allowlist dogfood.
SMTP and final foreground acceptance were still open at the implementation
snapshot. The later update below records the selected personal Hotmail/Outlook
provider, the required OAuth2 extension, and two user-confirmed real deliveries.

## 1. What was attempted

- Re-read the approved checkpoint-6 comment and kept the work within its
  two-hour implementation boundary.
- Rechecked the [official OpenAI Hooks documentation](https://learn.chatgpt.com/docs/hooks)
  for the durable Stop fields and the meaning of a blocking Stop continuation.
- Extended the Git mutator with one exact untracked report allowlist while
  retaining `git add -u` for all ordinary tracked changes.
- Added a fail-closed exact-thread parked-evidence gate, including an immediate
  second evidence check before every automatic Git mutation.
- Added a privacy-safe `notify-test` CLI path with caller-supplied unique IDs so
  direct and fallback SMTP tests cannot debounce each other.
- Added the smallest compatible authentication extension to the existing SMTP
  transport after the selected personal Outlook provider proved to require
  OAuth2/Modern Auth rather than password or app-password login.
- Updated the user-facing architecture and launch documentation.
- Ran focused, full, formatting, compilation, CLI, and live evidence checks.

## 2. What was successfully implemented / derived / verified

### Narrow new-report publication

`LocalGitMutator.preserve_and_push` now discovers only untracked paths under
`doc/Progress` and accepts only the exact case-sensitive three-component form
`doc/Progress/progress_*.md`. It still runs `git add -u --` for ordinary
changes, then runs an explicit `git add -- <reviewed report paths>`; it never
uses `git add -A` or broad untracked staging.

Each candidate must have regular, single-link, non-symlink/non-reparse file and
parent metadata. Control-character, malformed, nested, mismatched-case,
traversal-like, nonregular, symlink, hard-link, unstable-content, unsafe Git
attribute, and staged-blob mismatch cases fail closed. The staged stage-0 blob
must match Git's normalized view of the reviewed worktree file. Other untracked
files remain untouched and continue to produce attention notifications.

### Minimal active-worktree mutation guard

The foreground loop does not infer a general Codex state. Before preservation,
push retry, or fast-forward, it requires a recent completed parked Stop for the
registered exact session/workspace, validates the audit identities and Stop
state, and correlates the same turn with a unique rollout `task_started` and
latest `task_complete`. Later or ambiguously ordered hook, rollout, or exact
queue activity blocks mutation. Missing, stale, malformed, duplicated, or
unscopable evidence also blocks. The evidence is checked twice; for a
remote-ahead update the second check occurs after pending-OID persistence and
immediately before the Git mutator call.

The same gate safely retries a pre-commit or clean local-ahead push failure only
while the exact thread remains positively parked. It no longer treats a bare
clean local-ahead branch as sufficient publication authority.

### Direct notification acceptance command

The new command is:

```powershell
python tools\codex_watchdog.py --runtime .codex-watchdog\live-acceptance `
  notify-test --id <unique-id> --workspace local-watchdog-mvp
```

It sends a fixed, nonsecret test subject/body, persists only the existing
privacy-limited fingerprint/result state, prints `NotificationResult` JSON,
and exits successfully only for `sent` or `sent_fallback`. Reusing an ID is
deliberately suppressed; distinct direct and fallback IDs are required.

## 3. Current best implementation state / architecture

```text
trusted native Stop audit + exact rollout/queue evidence
                     |
                     v
        recent exact-thread parked proof
                     |
             check -> persist intent -> recheck
                     |
        +------------+----------------+
        |                             |
        v                             v
tracked `git add -u` +         clean exact-OID ff-only
explicit safe new report       then exact-thread queue wake
add + normal push
        |
        v
SMTP/Slack/Windows/local-audit notification result
```

The guard is intentionally evidence correlation, not a new activity provider.
Automatic mutation remains conservative and can defer a safe operation for a
cycle; it does not mutate when proof is absent.

## 4. Observed runtime behavior / probe results

- A live read-only guard probe used runtime
  `.codex-watchdog/live-acceptance`, workspace `local-watchdog-mvp`, and exact
  thread `<UUID>`.
- It identified parked audit `<UUID>`, turn
  `<UUID>`, outcome
  `grace_expired_parked`, and returned `status=deferred` with
  `reason=stale_parked_stop` during this active conversation. No Git mutation
  or queue wake was attempted by that probe.
- Focused integration tests prove positive parked evidence permits the exact
  fast-forward and wake once, while later rollout/user/queue evidence and an
  activity race between the first and second check defer without mutation.
- After a fresh explicit user approval, the production mutator processed this
  genuinely untracked report without manual staging. It created and normally
  pushed mechanical commit `106b45090c245fd8236b30b80ac209949449299a`.
  The result was `status=completed`, `commit_created=true`, `pushed=true`, no
  blockers or error digest, and `topology_before=equal -> topology_after=equal`.
  Independent verification showed `HEAD == origin/main == 106b4509`, a clean
  worktree, and this report as the commit's only `A` path; the other eight paths
  were tracked modifications. `untracked_present=false` after publication.
- At the 02:26 implementation snapshot, real SMTP was not yet configured or
  claimed live. No credential was written to this report, Git, runtime audit
  JSON, or chat; see the later live-acceptance section for the completed proof.

## 5. Tests and sanity checks performed

- Final full repository suite: **159 passed, 2 skipped** in 261.20 seconds.
- Combined Git mutation, MVP, notification, and CLI suite: **57 passed,
  1 skipped** in 205.00 seconds.
- Final MVP/CLI suite after mechanical formatting: **26 passed**.
- The skips are Windows identities that cannot create real symbolic links;
  injected metadata tests still cover link and nonregular rejection, and a real
  hard-link rejection test passed.
- Normal report, pre-staged stale-index replacement, unrelated-untracked,
  staged-blob race, path, control-character, link, nonregular, and hard-link
  regressions passed.
- `python -m compileall -q src tools tests` -> passed.
- `python -m black --check src tests tools` -> all 34 files unchanged.
- `python tools\codex_watchdog.py notify-test --help` -> rendered the expected
  unique ID and workspace options.
- `git diff --check` -> passed before this new report was created.

## 5A. Personal Outlook OAuth and live SMTP acceptance

The user selected a personal Hotmail/Outlook mailbox. Microsoft has required
OAuth2/Modern Auth for personal Outlook SMTP since retiring Basic Auth for
those accounts, so the existing `smtplib.login` password path could not safely
or reliably satisfy this provider choice. The implementation retained the
existing SMTP transport, notification ordering, result model, debounce, and
fallback behavior while adding only an explicit `outlook_oauth2` authentication
mode.

The Outlook profile is fail-closed to `smtp-mail.outlook.com:587`, STARTTLS,
matching username/from address, no configured password, a canonical public
client ID, the fixed Microsoft consumer authority, and only the delegated
`https://outlook.office.com/SMTP.Send` scope. Routine sends acquire silently
through MSAL and authenticate with SASL XOAUTH2. Interactive device login is
available only through the explicit `outlook-login` command; the foreground
watchdog never opens a browser or requests consent.

MSAL state is stored outside the repository beneath the current user's local
application-data directory. `msal-extensions` must provide an encrypted
persistence implementation; Windows uses DPAPI, and plaintext fallback is
forbidden. Client IDs and mailbox addresses are excluded from CLI result JSON,
while access tokens, refresh tokens, passwords, and device-flow internals are
excluded from tracked files, environment templates, notification state, logs,
and chat.

Live acceptance used runtime `.codex-watchdog/live-acceptance` and workspace
`local-watchdog-mvp`:

- The user created a personal-account public-client app registration, enabled
  device/public-client flow, and granted delegated `SMTP.Send` consent.
- `outlook-login` completed successfully and retained an encrypted current-user
  token cache without printing a token.
- A distinct `cpx06-outlook-direct-<UTC-ms>` notification test ran with Slack
  absent. The user confirmed the expected SMTP success and receipt of the real
  test message in the selected mailbox.
- A second distinct `cpx06-outlook-fallback-<UTC-ms>` test used an intentionally
  unreachable loopback Slack URL. The user confirmed the expected Slack-to-SMTP
  fallback result and receipt of the second real message.
- The loopback Slack variable was removed after the fallback test. No real
  Slack endpoint or credential was used.
- The reviewed OAuth implementation, tests, documentation, and this live
  acceptance record were committed as
  `7fd668b9754cbe1cfd6053834db4340be52468e1` and normally pushed to
  `origin/main`. Post-push verification showed local `HEAD` and
  `refs/remotes/origin/main` equal at that OID with a clean worktree.

Focused Outlook/notifier/CLI verification passed **50 tests**. A real Windows
DPAPI round trip proved injected fake access and refresh token strings were not
present in the encrypted cache bytes. The final full repository suite passed
**190 tests with 2 skips** in 296.74 seconds. Compilation, focused Black checks,
CLI help, and diff checks also passed. An unrestricted connectivity probe from
this machine reached Microsoft SMTP TCP port 587 successfully.

## 6. Unresolved technical issues / limitations

- Personal Outlook delivery now depends on the current Windows user's
  DPAPI-encrypted MSAL cache and the user's app registration remaining valid.
  Revocation or an interaction-required refresh fails closed until the explicit
  `outlook-login` command is run again.
- The parked proof expires after 15 minutes. A safe but older parked thread will
  defer until a fresh Stop rather than mutate on stale evidence.
- There is necessarily a small interval after the second evidence check in
  which Codex could start a turn before the separate Git process mutates. The
  double check narrows the approved MVP race without adding an App Server lock
  or general activity state machine.
- A Git/index race detected after staging blocks before commit, but Git staging
  already performed by that failed attempt can remain for human review; the
  watchdog does not destructively reset the user's index.
- Custom content-transforming Git attributes on a candidate progress report
  block automatic publication rather than trusting transformed bytes.
- Remote-SSH, background service installation, automatic restart, Slack, and
  resume disposition remain out of scope.

## 7. Exact files modified or created

```text
README.md
architecture.md
src/codex_watchdog/cli.py
src/codex_watchdog/git_mutations.py
src/codex_watchdog/mvp_service.py
tests/test_git_mutations.py
tests/test_mvp_service.py
tests/test_service_cli.py
doc/Progress/progress_2026_09_01_cpx06.md   (genuinely new)
```

The later personal-Outlook acceptance update additionally modifies or adds:

```text
.gitignore                                  (concurrent /Private/* ignore preserved)
README.md
architecture.md
config.example.toml
pyproject.toml
src/codex_watchdog/cli.py
src/codex_watchdog/notifications.py
src/codex_watchdog/outlook_oauth.py         (new)
tests/test_notifications.py
tests/test_outlook_oauth.py                 (new)
tests/test_service_cli.py
doc/Progress/progress_2026_09_01_cpx06.md
```

The concurrent `.gitignore` safeguard was preserved without inspecting,
staging, or reporting any ignored `Private` content.

No notification credential, webhook, recipient, test message body, runtime
audit file, or ignored dogfood artifact is included in the tracked change set.

## 8. Completed live acceptance and remaining foreground launch

Provider identification, secure account authorization, direct SMTP delivery,
forced Slack-to-SMTP fallback, both real mailbox receipt confirmations, and the
implementation publication are complete. The remaining action is to launch the
exact registered foreground loop from the same configured PowerShell process
and keep it running for ordinary dogfood observation.
