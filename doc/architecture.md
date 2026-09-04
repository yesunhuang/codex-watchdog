# Codex watchdog architecture decision

Status: the current MVP connects the manually trusted local hook, automatic
stable-VS-Code window/session discovery, locality-bound manual overrides,
read-only Git observation, notifications, and first-party queue wake in one
foreground process. A thin Remote-SSH probe is implemented and live-accepted on
the key-authenticated GPU lab host; Duo-only HPC provider transport remains pending redesign.

## Decision summary

Use native Codex hooks for the short stop window and the first-party `codex
queue` command as the preferred parked-thread courier. Both mechanisms passed
on an actual already-open local VS Code thread, so a deep
extension-host/App-Server shim is not justified. Keep the watchdog deterministic
and standalone.

For workspace selection, correlate VS Code's private persisted state with its
live process tree and privacy-safe lifecycle markers on every cycle. Do not ask
the operator to enter repository paths or thread UUIDs for ordinary eligible
local windows. Explicit registrations remain an advanced, durable override.

The current candidate architecture is:

```text
Codex Stop / PermissionRequest
        |
        v
native synchronous hooks
        |
        +-- audit output digest and context
        +-- transient exact final output for correlated notification
        +-- wait 5-20 minutes (default 10)
        +-- return one decision:block continuation
        v
same turn continues once
        |
        +-- stop_hook_active confirms intent and parks
        v
PARKED
        |
        +-- foreground deterministic Git/resume watcher
        +-- codex queue identified wake
        v
same durable thread resumes
```

Notifications use environment-configured Slack first, SMTP second, an opt-in
native Windows message transport for local dogfood, then an audit-only result.
SMTP supports the existing password/login path and a personal Outlook.com
OAuth2 path. Stable event fingerprints suppress identical repeats across
restarts.

Notification identity is rendered once before transport selection. The tracked
repository basename is the human-facing label shared by SMTP, Slack, and local
Windows messages; the internal workspace ID remains the durable identity and
is only the presentation fallback. Remote results use the same renderer with a
locality suffix such as `ProjectAlpha @ hpc-login`.

## SMTP authentication decision

Keep transport selection and authentication separate. SMTP remains the second
delivery transport, while `CODEX_WATCHDOG_SMTP_AUTH=outlook_oauth2` selects a
public-client OAuth2 mechanism instead of password login. This preserves the
existing generic SMTP configuration and fallback order without treating an
OAuth access token as a password.

The personal Outlook.com/Hotmail profile is deliberately narrow:

```text
personal Microsoft account
        |
        +-- one-time device-code consent
        |       authority: login.microsoftonline.com/consumers
        |       delegated scope: outlook.office.com/SMTP.Send
        v
DPAPI-encrypted, current-user MSAL cache in local application data
        |
        +-- silent access-token acquisition/refresh
        v
STARTTLS smtp-mail.outlook.com:587
        |
        +-- SASL XOAUTH2 (signed-in address + bearer token)
        v
existing SMTP notification result and fallback semantics
```

The app registration targets personal Microsoft accounts and enables public
client flows. Device-code flow has no redirect URI and no client secret. The
only app-specific environment value is the nonsecret Application (client) ID
in `CODEX_WATCHDOG_OUTLOOK_CLIENT_ID`; sender, recipient, username, host, port,
and TLS settings continue to use the SMTP variables. The OAuth profile accepts
only the exact `smtp-mail.outlook.com` host, port 587, and STARTTLS; sender must
case-insensitively equal username, the client ID must be a canonical UUID, and
password configuration is invalid. Generic SMTP retains `password` as the
default authentication mode and rejects an Outlook client ID in that mode.

`outlook-login` is the only interactive authentication operation. It displays
Microsoft's device verification URI and short user code, then persists the MSAL
cache by default beneath `%LOCALAPPDATA%\CodexWatchdog\oauth`, keyed by the full
SHA-256 digest of the stripped client ID. It is intentionally independent of
the watchdog runtime. On Windows the cache must be DPAPI encrypted for the
current user, locked for concurrent access, excluded from notification/audit
output, and never downgraded to a plaintext file. Access and refresh tokens are
never environment variables or command-line arguments. Device login also
verifies that exactly one cached account matches the configured SMTP username.
A different user, machine, execution locality, or client ID requires a distinct
login. Non-Windows execution uses the platform user-data location and still
fails closed when encrypted persistence is unavailable.

Routine delivery first acquires silently from that cache. MSAL returns a cached
access token or refreshes it as necessary; an interaction-required or revoked
grant fails that SMTP attempt normally and allows the existing later transports
or audit-only result to run. It never launches an interactive browser or device
flow from the foreground polling cycle. The operator deliberately reruns
`outlook-login` to restore authorization.

The direct acceptance test runs `notify-test` with Slack absent and must report
`sent` through `smtp` plus an actually received message. The fallback test uses
an unreachable loopback Slack URL and a different notification ID; it must
report `sent_fallback`, attempt `slack` then `smtp`, and deliver a second real
message. These tests exercise the production notifier and its durable debounce,
not a separate SMTP probe.

## Workspace discovery, Git sensor, and foreground loop

The observation pass remains available on its own. By default, both it and the
MVP loop use a fresh effective-workspace catalog rather than a registry-only
list:

```text
persisted stable-VS-Code window candidates
        + live code --status extension-host/App-Server tree
        + exact extension-host PID lifecycle and workspace-storage key
        + exact per-window Codex resource and current owner evidence
        |
        +------ automatic eligible local workspaces ------+
                                                          |
atomic process-local manual overrides --------------------+
        |                                                 |
        +---------------- effective catalog <-------------+
                                  |
                                  v
globally locked, sorted service-once pass
        |
        v
read-only local/remote inspection + exact git ls-remote branch OID
        |
        +-- completed exact Stop or delayed rollout completion:
        |   notify with exact correlated output
        +-- changed remote OID:
        |   no Git mutation, queue exact-OID wake for Codex-owned synchronization
        +-- dirty/untracked/local-ahead state: observe only and leave untouched
        +-- genuine Git blocker: notify and leave untouched
        |
        v
atomic per-workspace observation + transition fingerprint
```

Automatic discovery currently targets the current user's standard stable VS
Code installation and data root on Windows. Persisted `windowsState` entries
are candidates only. The live index runs `code --status`, binds each reported
window slot to an exact extension-host PID, and selects the corresponding log
session only when its latest lifecycle event is that PID starting with no later
termination or exit. The same extension-host log must expose exactly one
workspace-storage key. Historical storage directories, closed
`lastActiveWindow` entries, reused window slots, duplicate live mappings, and
unmatched live storage keys are omitted or surfaced as fail-closed issues.

For an eligible local window, the process tree must also contain exactly one
OpenAI Codex App Server child. The resolver opens only that window's
`state.vscdb` read-only and selects only `providerType` and `resource` from the
`agentSessions.model.cache` array. A candidate resource must be exactly
`openai-codex://route/local/<canonical-UUID>`. Those IDs are intersected with
unarchived `source=vscode`, `thread_source=user` rows in Codex `state_5.sqlite`,
an exact canonical workspace/repository working-directory match, and a
currently held writer lock.

The final ownership proof comes from the selected window's `Codex.log`. Only
exact-thread stream-role markers are parsed, and the latest role must be
`owner`. Each `[CodexMcpConnection] Spawning codex app-server` marker resets the
remembered role, so evidence from an earlier App Server generation is never
carried forward. Missing, malformed, partial, zero-match, or multi-match state
remains unresolved; no timestamp or newest-thread fallback is used. Labels,
prompts, and chat contents are not selected or persisted by discovery.

The effective catalog merges those ephemeral automatic results with the atomic
process-local manual registry. A manual record binds a stable workspace ID,
canonical repository path, exact session ID, and `process_local` execution
locality; it wins for the same canonical repository and remains effective when
the VS Code window is closed. `workspace-add`, `workspace-list`, and
`workspace-remove` manage these overrides, while `--manual-only` deliberately
disables automatic discovery. URI, UNC, SCP-style, and other nonlocal manual
paths are rejected so Windows cannot accidentally operate on a Remote-SSH
repository.

Codex state lookup and queue delivery share one home selection rule: an
explicit `--codex-home`/constructor value wins, otherwise `CODEX_HOME` is used,
then `~/.codex`. This prevents discovery, writer-lock checks, rollout evidence,
and `codex queue` from silently targeting different Codex state trees.

The Git adapter reports tracked dirt and untracked presence separately without
persisting filenames. It blocks ambiguous layouts, operation state, conflicts,
locks, detached/unborn HEAD, missing or unsupported upstreams, remote-check failures,
divergence, and detected races. The foreground policy treats the sole
`state_changed_during_observation` race as transient: retry one read-only
snapshot, then defer non-consumingly if the repository is still moving. It does
not emit Git attention or advance pending work/cursors for that busy cycle.
Ordinary dirty, untracked, and local-ahead observations are persisted but do not
independently emit notifications; genuine blockers still do.
Both local and Remote-SSH adapters enforce a read-only Git subcommand allowlist.
The detector uses `git ls-remote` and never changes `FETCH_HEAD`,
remote-tracking refs, HEAD, the index, or the worktree. It never pulls, fetches,
fast-forwards, resets, rebases, resolves conflicts, stages files, commits, or
pushes. The exact queued Codex turn owns ordinary synchronization because
it has the task context required to resolve straightforward Git situations.

The foreground loop intentionally has no general Codex activity-state adapter.
It correlates a Stop hook or delayed rollout completion to the exact thread and
turn, then notifies. Missing, duplicate, malformed, or later activity evidence
fails closed. Remote-update
queue delivery does not require PARKED; it relies on the native queue's
same-thread idle behavior and exact-OID idempotence. Each cycle refreshes
automatic discovery. Immediately before a queue wake, the catalog is resolved again and
must contain the exact same automatic workspace/repository/session binding.
Wake revalidation occurs while holding the target-session watchdog lock. A
manual target is instead re-read from the durable registry and must still match
exactly. Discovery errors make automatic `run --once` and `service-once`
unsuccessful rather than treating an empty workspace set as success.

## Evidence that changed the Phase-0 decision

The installed Codex CLI exposes:

```text
codex queue --thread <UUID> --message <TEXT>
```

Source inspection showed that the command journals into Codex's shared queue
database, while loaded App Servers watch for external queue revisions. Live
Windows probes then confirmed the behavior twice: first on a fresh loaded App
Server thread and finally on the actual already-open VS Code development thread.
The latter received the identified queued prompt as a new turn on the same
thread after an identified Stop continuation retained its preceding marker.

A later live-window probe found the actual `ProjectAlpha @ hpc-login` Remote-SSH window.
The local process now invokes a compact adapter through SSH; that adapter opens
only remote Codex state, rollout, queue, and Git metadata in the owning locality
and returns compact JSON. The exact local VS Code window cache supplies bounded
thread claims; the remote adapter accepts one only after matching its remote
state row, repository cwd, and current owner log. A remote window-cache lookup
remains as compatibility fallback for layouts that store the cache remotely.

This disproves the Phase-0 assumption that every external courier needs access
to the IDE-owned stdio connection. It does not yet prove delivery to a
Remote-SSH thread. [probe_report.md](probe_report.md) is retained as historical
Phase-0 evidence, not as the current recommendation.

## Short-stop protocol

Each instruction contains:

```text
instruction ID
source
target session ID
verbatim prompt
prompt SHA-256
created timestamp
state
```

The producer publishes an atomic JSON file. On Stop, the handler:

1. records `waiting` without retaining the assistant text;
2. performs one immediate claim attempt;
3. polls deterministic files until the configured deadline;
4. claims only an instruction targeted to the hook `session_id`;
5. writes a continuation intent and per-turn guard; and
6. returns one `decision:block` reason.

On the next Stop for the same turn, only JSON boolean
`stop_hook_active: true` activates the loop guard. The matching inflight intent
is marked confirmed and moved to consumed. The handler returns `{}` and does
not claim more work.

The design is logical at-most-once, not guaranteed delivery. A process or power
failure between journal transitions can strand an inflight intent. Automatic
retry would risk duplicate model turns, so recovery is deliberately manual in
this PoC.

## Parked-thread queue protocol

The queue adapter writes `dispatching` before invoking the CLI. Its key is the
instruction ID, and reuse with a different thread, source, or prompt is a hard
collision. Results are:

```text
enqueued               exact CLI acknowledgement and queue UUID recorded
consumed_or_started    matching row disappeared after observed/bounded revision evidence
started                exact new target-thread UserMessage marker observed in rollout
dispatching           prior process may have died during send
uncertain             timeout, launch failure, or nonzero CLI exit
```

An identical repeat never invokes the queue command again. For `enqueued` or
`consumed_or_started`, it passively reconciles the existing queue/rollout
evidence and returns the promoted record with `deduplicated=true`.
`dispatching` and `uncertain` stay unresolved; the watchdog never guesses
whether a prompt was delivered and does not retry automatically. `started`
proves turn input, not model completion.

Remote OID state therefore remains pending after `enqueued`. It is cleared only
after positive `consumed_or_started`/`started` evidence, or when the exact
remote OID already equals local `HEAD`. A newer observed remote OID does not
overwrite an older pending wake; after that wake is consumed, the next cycle
detects and queues the newer OID.

Passive observation may rewrite the same local journal as queue/rollout evidence
advances. A live Windows observer received a transient `os.replace` denial;
atomic metadata replacement therefore retries only Win32 errors 5, 32, and 33
for 10/20/40/80 ms. It then re-raises. This never retries the queue command or
model continuation.

Remote Git wakes use a fixed prompt and an ID scoped by workspace/thread plus
remote OID. The prompt first requires Codex to inspect a local temporal/resume
prompt and decide its relevance in current task context, preserving ambiguous
files and archiving or deleting used prompts. It then requires safe Git
synchronization, followed by a new progress-report comment or idle. When a
remote OID coexists with `resume_prompt.md`, the watcher leaves the file visible
for that fixed wake turn instead of claiming it into `resume/inflight/` first.
Standalone resume prompts still receive a fresh UUID so two intentional
identical prompts are not collapsed.

## PermissionRequest and AutoReview

`PermissionRequest` fires before final reviewer routing. The hook records
`permission_observed_pre_routing` and returns `{}` to defer. That observation
must not be presented as "waiting for the user": AutoReview may approve or deny
without user interaction.

The manually trusted user hook emitted this observation repeatedly on the live
VS Code development thread. AutoReview continued routing afterward, confirming
that the event alone does not imply user intervention.

## Locality and UI fallback

The local Windows discovery process detects open `vscode-remote://ssh-remote+`
windows and extracts only the authority, absolute remote path, and exact
workspace-storage key. It never passes that path to Windows Git. A self-contained
Python probe runs through non-interactive SSH beside the remote VS Code Server,
intersects the exact window resource with remote Codex state and current owner
logs, observes rollout completion and read-only Git, and journals queue delivery
under the remote user's private runtime. Large remote state is never copied.
Transport/authentication failure is hashed, notified once per transition, and
fails closed.

No UI or screenshot fallback was exercised. The first-party queue is narrower
and succeeded on the actual local VS Code target, so UI automation is not part
of the recommended architecture.

## Runtime and privacy invariants

- One runtime may hold several targeted sessions, but every production short
  instruction must name its exact session.
- Automatic workspace records are ephemeral and are refreshed each cycle;
  manual overrides alone are durable registry entries.
- A live extension host, window-scoped resource, held writer lock, and current
  same-window owner role are all necessary for automatic targeting; none is
  sufficient alone.
- Prompts are stored in plaintext in the local runtime and briefly appear in
  the queue process command line.
- Assistant output is represented in durable audits only by SHA-256 and
  character count. Exact terminal output exists only in the gitignored
  transient spool until a correlated external notification succeeds.
- Queue stdout/stderr are represented in journals only by digest and length.
- Hooks run with the user's privileges and require explicit review/trust.
- Malformed input, lock contention, or handler exceptions fail open and never
  block Codex by accident.
- No inbound pull, fetch, fast-forward, reset, merge, rebase, conflict
  resolution, or untracked-file deletion belongs in the watchdog Git layer.

## Deferred components

The following remain unimplemented:

- live Remote-SSH Stop and queue acceptance after batch SSH authorization;
- ACTIVE/NOT_ACTIVE/UNKNOWN workspace observation;
- automatic interpretation of resume disposition lines;
- background service installation; and
- crash reconciliation UI or commands.
