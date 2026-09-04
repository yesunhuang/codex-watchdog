# Progress report - 2026-09-03 checkpoint 12 GPU lab Remote-SSH acceptance

Final snapshot: 2026-09-03 02:20 CDT

Scope: use the already-open GPU lab `LocalCodexWatchDog` workspace as the normal
key-authenticated Remote-SSH control case, repair automatic exact-thread
mapping for the observed split-local/remote VS Code layout, and retain the
MFA HPC Duo limitation as a separate redesign boundary.

## 1. Starting state and host authentication

The repository started clean and synchronized at `113b306`. No remote Git
update or new progress-report comment was present.

`gpu-lab` accepted the existing dedicated GPU lab key in `BatchMode`. The
already-open VS Code workspace used `gpu-lab-personal`, whose SSH block must
disable public-key authentication for interactive password/PTY use because the
dedicated watchdog key is intentionally restricted from allocating a PTY. An
initial key-first edit exposed that restriction as `PTY allocation request
failed on channel 0`; it was reverted. The interactive alias keeps
`PubkeyAuthentication no`, password/keyboard-interactive authentication, and
`BatchMode no`. The watchdog now overrides that same alias only for its `ssh -T`
probe with `BatchMode=yes`, `PubkeyAuthentication=yes`, and
`PreferredAuthentications=publickey`. The original user SSH config is backed up
outside the repository as
`~/.ssh/config.codex-watchdog-gpu_lab-20260903.bak`; the superseded key-first
configuration is preserved separately as
`~/.ssh/config.codex-watchdog-gpu_lab-pty-20260903.bak`.

## 2. Split-local/remote discovery repair

The first exact GPU lab probe failed with
`remote_vscode_session_cache_unavailable`. Live evidence showed that this VS
Code build keeps the open remote window's `agentSessions.model.cache` in the
local Windows workspace cache, while the Codex thread database, current owner
log, rollout, queue, and repository remain on GPU lab.

Automatic discovery now extracts only canonical Codex thread IDs from the exact
live local window cache and passes the bounded candidate set to the owning
Remote-SSH adapter. The remote side accepts a candidate only when exactly one:

- is an active VS Code user thread in remote `state_5.sqlite`;
- has an exact cwd match to the remote repository; and
- is the current owner in the remote Codex extension log.

Zero matches, multiple current matches, malformed IDs, duplicate candidates,
or more than 64 claims fail closed. When a window reload leaves multiple
historical owner claims, the verifier accepts exactly one active owner and
rejects zero or multiple active owners. The previous remote-window-cache
resolver remains as a compatibility fallback when no local claim set is
available. No thread or workspace ID is entered manually.

## 3. GPU lab-only live acceptance

The final adapter probe was limited to the already-open workspace:

```text
Authority: ssh-remote+gpu-lab-personal
Repository: /home/operator/LocalCodexWatchDog
Status: ok
Session resolved: yes
Git status: observed
Topology: equal
Dirty tracked: false
Untracked: false
Blockers: none
Terminal completion available: yes
```

The probe was read-only and did not inspect or modify another project. Raw
terminal output was not printed or persisted by the acceptance command.

## 4. MFA HPC boundary

Current HPC provider documentation requires Duo and limits key-based authentication to
approved PI exceptions installed by HPC provider staff. The README no longer instructs
ordinary MFA HPC users to append a key to `authorized_keys`. The current
non-interactive SSH adapter is therefore valid for normal key-enabled hosts such
as GPU lab but does not solve a standard Duo-only MFA HPC account. That transport
requires a separate design rather than an authentication bypass.

## 5. Verification

- Focused discovery/Remote-SSH/MVP suite: passed.
- Full suite: 254 tests passed with one intentional skip.
- `python -m black --check src tests`: passed, 32 files unchanged.
- `python -m compileall -q src tests`: passed.
- `git diff --check`: passed.
- Installed interactive alias verification: password/keyboard-interactive,
  `PubkeyAuthentication no`, and `BatchMode no`.
- Live watchdog-style GPU lab authentication (`ssh -T` with explicit public-key
  overrides): exit zero without a PTY request.
- Live exact GPU lab adapter probe: passed with session, repository, Git, owner,
  and completion correlation.

## 6. Review boundary

The GPU lab control case and automatic split-local/remote mapping are complete.
No continuous foreground process or external Outlook notification was started
from the Codex environment, which does not inherit the configured SMTP
variables. MFA HPC Duo support remains the next explicit architecture task.

## 7. Isolated remote-session test correction

The step-by-step live test exposed that `--exclude` was applied to local
windows but not Remote-SSH candidates. Remote discovery now compares exclusions
against the exact workspace URI, POSIX workspace path, and repository basename,
marks a matching remote window as `excluded`, and prevents the MVP from creating
an SSH target for it. Focused and full-suite regression tests pass. A live
discovery with exclusions for the two local repositories and ProjectAlpha left only
`gpu-lab-personal` as a remote adapter candidate.

The correct one-cycle command for end-to-end Remote-SSH behavior is `run
--once`; `service-once` is the narrower local Git sensor and does not invoke the
Remote-SSH MVP path.

Reloading the GPU lab window then exposed two matching, unarchived thread claims:
the prior thread was an inactive owner and the new thread was the sole active
owner. The remote verifier now retains the latest owner and view-activity state
per thread and uses the sole active owner to resolve this otherwise ambiguous
case. A subsequent isolated live cycle resolved the new thread, observed clean
Git state with the remote branch ahead, and received an exact queue
acknowledgement for the remote-update wake.

The final end-to-end acceptance ran the foreground watchdog at a five-second
interval with every workspace except GPU lab excluded. After the remote Codex
thread completed the unique output `SPARK_REMOTE_EMAIL_TEST_20260903`, the
45-second rollout fallback correlated that exact completion and delivered the
output successfully through the configured Outlook SMTP transport. The user
confirmed receipt in the target mailbox.

## 8. Slack notification acceptance

A Slack app incoming webhook was configured without exposing its URL in chat,
source control, command history, or durable watchdog state. A direct
`notify-test` reported Slack as the sole successful channel. With the
five-second GPU lab-only foreground loop running, the exact remote completion
`SPARK_REMOTE_SLACK_TEST_20260903` was delivered to the selected Slack channel,
and the user confirmed receipt.

The webhook was then saved outside the repository under `%LOCALAPPDATA%` as a
Windows DPAPI-protected CLIXML credential. The process environment value was
removed, restored from that encrypted credential, and a second direct Slack
test succeeded. The README now records both the official Slack app setup and
the Windows-only secure save/restore procedure. Outlook remains configured as
the fallback after Slack.

## 9. PowerShell convenience launcher

The root-level `watchdog.ps1` now starts the foreground service from any current
directory without requiring an editable package installation or a manually
entered repository path. It restores the DPAPI-encrypted Slack credential when
the process environment does not already contain a webhook and never prints the
secret. It also supports `-Once`, `-IntervalSeconds`, `-Exclude`,
`-ReplayLatestStop`, `-ManualOnly`, a custom `-Runtime`, and a privacy-safe
`-DryRun`.

PowerShell parser validation and a dry run passed. A real `-Once` invocation
through the wrapper, with every workspace except GPU lab excluded, completed the
remote adapter cycle and received an exact-thread queue acknowledgement. The
full Python regression suite also remained green.

## comment

### Checkpoint 13 — MFA HPC/Duo as an additional Remote-SSH fallback

The current GPU lab/key-authenticated Remote-SSH implementation is working and is now the reference path. **Do not replace, rewrite, weaken, or regress it.** Continue dogfooding it as the normal remote transport.

Explore how to support standard MFA HPC accounts that require Duo, but implement MFA HPC only as an additional fallback transport selected when the ordinary non-interactive key-authenticated SSH adapter is unavailable for that remote authority.

Design constraint:

```text
remote workspace
    -> ordinary key/batch SSH adapter, when available        [existing primary path]
    -> MFA HPC/Duo-compatible fallback, only when required    [new optional path]
```

The fallback must preserve the same higher-level contract already proven on GPU lab:

- automatic exact remote workspace/thread correlation;
- read-only Git/OID observation only;
- exact-thread `codex queue` wake;
- terminal Stop / rollout-completion observation;
- compact state returned to the local watchdog;
- no WatchDog Git mutation;
- same Slack/Outlook notification path and human-readable labels.

Investigate the actual MFA HPC/VS Code environment first rather than assuming a transport. In particular, determine whether there is a lightweight user-authorized way to reuse an already authenticated Remote-SSH session, launch a small remote-side helper after one interactive Duo approval, or otherwise maintain a narrow persistent channel that avoids requiring a fresh Duo interaction every polling cycle. Prefer a one-time/operator-authorized bootstrap followed by a sleeping lightweight helper or transport over repeated interactive automation.

Do **not** bypass Duo, scrape/automate the Duo UI, store passwords or Duo tokens, weaken SSH configuration, or commandeer undocumented VS Code private tunnels unless there is strong evidence that doing so is stable and appropriate. If the existing VS Code session exposes a safe supported mechanism that can be reused, validate it empirically before depending on it.

Keep the implementation minimal. Avoid a general remote-host framework or daemon architecture unless the real MFA HPC constraints force it. A small transport abstraction/fallback selector is acceptable if needed, but the existing GPU lab adapter should remain intact and independently testable.

Acceptance priority:

1. Inspect the live MFA HPC Remote-SSH layout and authentication/session behavior.
2. Identify the smallest credible Duo-compatible transport and document why it is safe and stable enough.
3. Build a minimal PoC/fallback without changing the existing GPU lab path.
4. If practical, live-test one already-open MFA HPC workspace through discovery -> exact-thread observation -> queue wake -> completion notification.
5. If a clean implementation is not possible without brittle UI/auth automation, stop with the concrete boundary and recommended alternative rather than forcing it.

Continue the current highway-repair approach: solve only failures or constraints observed on the live MFA HPC path. Work for at most two hours of active implementation, write the next standard progress report with exact evidence and remaining limitations, recommend the next step but do not perform unrelated expansion, and stop for review.
