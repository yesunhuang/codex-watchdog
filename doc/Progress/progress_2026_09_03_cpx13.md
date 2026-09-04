# Progress report - 2026-09-03 checkpoint 13 MFA HPC Duo fallback

Final snapshot: 2026-09-03 03:25 CDT

Scope: preserve the proven GPU lab/key-authenticated Remote-SSH path and add the
smallest operator-authorized fallback capable of monitoring the already-open
MFA HPC `ProjectAlpha` workspace without bypassing or automating Duo.

## 1. Starting state and live transport evidence

The watchdog wake found no temporal/resume prompt. The repository was one
commit behind `origin/main`; it fast-forwarded safely to `702b067`, whose only
change was the Checkpoint 13 comment. The unrelated untracked `Shiro.png` was
left untouched and later disappeared through activity outside this checkpoint.

The open MFA HPC window uses VS Code Remote-SSH with a standalone Windows
OpenSSH process, `ssh.exe -T -D <local-port> hpc-login.example.edu sh`.
That process exposes a VS Code-owned SOCKS tunnel, not a documented general
command channel, so the fallback does not commandeer it.

All three installed OpenSSH clients accepted `ControlMaster`, `ControlPath`,
and `ControlPersist` during configuration expansion. Runtime tests established
the Windows boundary:

- native Windows OpenSSH failed to create a master with `getsockname failed:
  Not a socket`;
- Git-for-Windows OpenSSH created a socket and answered `-O check`, but a slave
  command failed because the master could not receive the client's standard
  input file descriptor;
- no WSL distribution or PuTTY/Plink installation was initially available.

This matches VS Code's current documentation, which recommends ControlMaster
for repeated interactive authentication only on macOS/Linux. OpenSSH documents
the underlying connection-sharing semantics, while HPC provider documents that ordinary
MFA HPC access requires password plus Duo and that key exceptions are limited.

Primary sources:

- https://code.visualstudio.com/docs/remote/troubleshooting#_enabling-alternate-ssh-authentication-methods
- https://man.openbsd.org/ssh_config#ControlMaster
- https://example.edu/hpc/connection/ssh/main/

## 2. Selected design

PuTTY/Plink provides a supported Windows-native SSH-2 sharing implementation.
One visible operator-controlled `plink -ssh -share -N user@host` window performs
the normal password and Duo interaction. The unattended adapter first runs
`plink -batch -ssh -shareexists user@host`, then attaches as a downstream with
`-batch -share` and the same `python3 -` payload used by the GPU lab adapter.
PuTTY documents that downstream tools reuse the upstream's authenticated SSH-2
connection and that `-shareexists` never creates a new connection:

- https://the.earth.li/~sgtatham/putty/0.85/htmldoc/Chapter4.html#config-ssh-sharing
- https://the.earth.li/~sgtatham/putty/0.85/htmldoc/Chapter7.html#plink-option-shareexists

The selector invokes the existing OpenSSH adapter first. It considers the
Plink fallback only after `remote_ssh_failed` or
`remote_ssh_auth_or_transport_failed`, and only when the discovered authority
exactly matches the configured `user@host`. GPU lab therefore remains independent
and cannot enter the MFA HPC fallback.

No password, Duo answer, token, session traffic, or VS Code tunnel detail is
stored. The optional saved configuration under `%LOCALAPPDATA%\CodexWatchdog`
contains only schema version, `user@host`, and the Plink executable path.

## 3. Implementation and observed repairs

`SharedPlinkRemoteSshAdapter` reuses the exact existing remote payload and adds
a transport label to safe observations/audits. `FallbackRemoteSshAdapter`
contains the narrow selection rule. Environment configuration is opt-in via
`CODEX_WATCHDOG_DUO_PLINK_TARGET` and `CODEX_WATCHDOG_PLINK_EXE`.

The root `watchdog.ps1` launcher now:

- loads or explicitly saves the nonsecret Duo target;
- locates Plink without installing a package system-wide;
- bootstraps only when the exact, non-excluded MFA HPC window is open;
- opens `duo-upstream.ps1` visibly for operator authentication;
- uses only `-batch` downstreams;
- permits a one-run `-NoDuo` override; and
- waits for both `-shareexists` and a real downstream `true` command.

The operator's concurrent launcher-default change from 300 seconds to 120
seconds was preserved.

The real downstream readiness check was added after live testing showed that
the sharing endpoint can appear before Duo authentication finishes. The helper
window stays open after connection failure so the operator can read the error.

The first authenticated exact-thread probe then exposed MFA HPC's Python 3.6.
The shared remote payload used the Python 3.7 `text=True` subprocess alias and
failed with a deterministic traceback. Both remote subprocess calls now use
the equivalent `universal_newlines=True`, which is supported by Python 3.6.

The portable official PuTTY 0.85 `plink.exe` used for acceptance is stored only
under the gitignored runtime. Its SHA-256 is
`969F36879D5716AA1A9811F43A6A6510E8F08372DBEB9695B810B9C776F39C75`;
Windows reported a valid Authenticode signature from Simon Tatham.

## 4. Live MFA HPC acceptance

Discovery saw four VS Code windows and isolated one Remote-SSH candidate:

```text
Authority: ssh-remote+hpc-login.example.edu
Repository: /home/operator/ProjectAlpha
Workspace ID: vscode-remote-<STATE_ID>
Transport: plink_shared_connection
Status: ok
Session resolved: <UUID>
Git status: observed
Topology: equal
Dirty tracked: false
Untracked: false
Blockers: none
```

The isolated service cycle correlated the existing terminal completion and
sent it through Slack. No other project was probed by that cycle.

A bounded live wake then used instruction
`cpx13-hpc-duo-live-20260903t0823z`. Remote `codex queue` acknowledged the
exact ProjectAlpha thread as `enqueued`; a passive poll advanced the durable remote
record to `started` with turn `<UUID>`. The
terminal output was exactly `MIDWAY_DUO_SHARED_WAKE_PASSED`. After the existing
45-second rollout fallback delay, the next isolated cycle correlated that turn
and delivered the exact output through Slack with status `sent`.

All MFA HPC Git operations were read-only. The watchdog did not stage, commit,
fetch, pull, merge, reset, or push in ProjectAlpha.

## 5. Verification

- Focused Remote-SSH/MVP suite: passed.
- Full Python suite: 263 passed with one existing intentional skip.
- `python -m black --check src tests`: passed.
- `python -m compileall -q src tests`: passed.
- Both PowerShell launchers parse without errors.
- Saved-config and `-NoDuo` dry runs passed without printing secrets.
- `git diff --check`: passed.
- Live exact MFA HPC discovery, thread resolution, Git observation, queue wake,
  completion observation, and Slack notification: passed.

## 6. Remaining boundary and recommendation

The fallback lasts only while the operator keeps the small Duo SSH window
open. Closing it intentionally disables MFA HPC monitoring; the next launcher
run opens a fresh interactive authentication window only if the exact MFA HPC
workspace is open. This is deliberate and avoids persisting authentication
material or automating Duo.

The next review should confirm receipt of the exact Slack token and ordinary
long-running behavior across a MFA HPC connection expiry. No broader remote
host framework, service installation, VS Code tunnel reuse, or authentication
automation is recommended.
