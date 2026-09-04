# Phase 0 feasibility probe

> Historical checkpoint-1 evidence. The checkpoint-2 work discovered and
> live-probed native Hooks plus the experimental first-party `codex queue`
> mechanism. See [architecture.md](architecture.md) and the latest file under
> [`Progress/`](Progress/)
> for the current decision. The shim recommendation below is no longer the
> preferred next step.

Date: 2026-08-29 (America/Chicago)

## Result

The Phase 0 gate does **not** permit implementation of an operational standalone
watchdog yet.

Workspace discovery and best-effort persisted-output capture are feasible for a
local VS Code Codex session. Authoritative live activity state and prompt
delivery to the already-open IDE session are not available to a separate
process in the observed installation. A Remote-SSH window is discoverable, but
there was no active/known Codex conversation in that window to test, and an
independent noninteractive SSH connection was not authenticated.

Building the watchdog core on top of a second Codex App Server would therefore
violate two requirements in the implementation plan:

- `UNKNOWN` must not be treated as a stop.
- prompts must target the existing session, not a fresh unrelated conversation.

The least-fragile next step is a narrowly scoped extension-host/App-Server shim
probe. That work was not performed in this checkpoint.

## Probe safety

The probe was read-only with respect to Codex and the repositories:

- no prompt was sent;
- no thread was resumed, started, steered, interrupted, archived, or approved;
- no remote Git operation was attempted;
- no Git trust configuration was changed;
- temporary App Server schema artifacts were removed after inspection; and
- a PowerShell module-analysis cache accidentally created under the repository
  was identified as probe debris and removed before checkpoint files were
  written.

## Environment observed

| Component | Observed value |
| --- | --- |
| Host | Windows 10/11 family, x86-64 |
| VS Code | 1.135.0 |
| OpenAI Codex VS Code extension | `openai.chatgpt` 26.825.51511 |
| Bundled Codex CLI | 0.151.0-alpha.7.2 |
| Local test workspace | `LocalCodexWatchDog` |
| Remote test window | one Linux Remote-SSH workspace (identity redacted) |
| Python | 3.9.12 |
| pytest | 7.4.0 |
| Git | 2.37.3.windows.1 |

The current repository is owned by a different Windows SID from the sandboxed
probe process. Ordinary Git discovery correctly failed with a dubious-ownership
error. No global `safe.directory` exception was added.

## Capability matrix

| Capability | Local VS Code | Remote-SSH VS Code |
| --- | --- | --- |
| Window/workspace discovery | **Proved**, using VS Code storage plus `code --status` | **Proved** for the open window and execution locality |
| Repository mapping | **Proved** for the local folder URI | **Partially proved** from the remote workspace URI; Git was not run remotely |
| Codex process discovery | **Proved** | **Proved** on the remote extension host |
| Existing Codex thread identity | **Partial**: exact cwd/source found the current user thread, but cwd is not unique across threads and no window ID is exposed | **Not testable**: no remote conversation/thread was evidenced |
| `ACTIVE` / `NOT_ACTIVE` / `UNKNOWN` | **Failed for a standalone process** | **Not testable** |
| Last Codex output | **Proved best effort** from persisted thread data | **Not testable** |
| Prompt to the existing session | **Failed for a standalone process** | **Not testable** |

## 1. Workspace and window identity

VS Code maintains the current window set in user storage. For each window,
`workspace.json` gives the canonical folder/workspace URI, and `storage.json`
records whether it is local or has a `remoteAuthority`. `code --status` then
provides a live snapshot of renderer, extension-host, and remote-server
processes.

The current snapshot contained two simultaneous windows:

1. the local `LocalCodexWatchDog` folder; and
2. one Linux Remote-SSH folder.

The durable discovery identity should be:

```text
canonical workspace/folder URI
+ remoteAuthority (if present)
+ VS Code workspace-storage key
```

Window numbers and process IDs are transient enrichments, not persisted
identities. The implementation must read `workspace.json`; it must not try to
re-create VS Code's storage-key hashing algorithm.

These files are private VS Code implementation details and may change:

```text
%APPDATA%/Code/User/globalStorage/storage.json
%APPDATA%/Code/User/workspaceStorage/<key>/workspace.json
%APPDATA%/Code/User/workspaceStorage/<key>/state.vscdb
```

## 2. Codex session identity

The local Codex store contained thread rows with `id`, `source`, `thread_source`,
`cwd`, `rollout_path`, recency, and Git metadata. The current main thread was
identified by:

```text
source = vscode
thread_source = user
cwd = the canonical local repository path
```

Windows paths in the Codex store used the extended-length `\\?\` prefix and
must be normalized before comparison.

This mapping is not sufficient for unattended prompt delivery. The same cwd had
multiple matching rows (the user thread and subagent threads), and multiple
ordinary IDE threads may also share a repository. App Server thread objects do
not expose a VS Code window identifier. Name and recency would only be
heuristics.

## 3. Activity state

The [official OpenAI App Server documentation](https://learn.chatgpt.com/docs/app-server)
defines runtime thread states and the `turn/started` / `turn/completed` event
stream. In the installed schema, runtime status is:

```text
active | idle | notLoaded | systemError
```

The safe normalization is:

```text
active                         -> ACTIVE
idle                           -> NOT_ACTIVE
notLoaded/systemError/failure  -> UNKNOWN
```

`waitingOnApproval` and `waitingOnUserInput` are flags on an **active** thread.
They must remain `ACTIVE`; the core may issue a separate deterministic
needs-attention notification. Mapping either flag to `NOT_ACTIVE` could trigger
Git mutation while a turn still owns the workspace.

VS Code launched its App Server as a child of the extension host with the
default stdio transport:

```text
codex ... app-server --analytics-default-enabled
```

There was no TCP listener or documented control pipe. On Windows,
`codex app-server daemon` reported that daemon lifecycle is Unix-only.

A separate analytics-disabled App Server was tested with only:

```text
initialize
thread/list (sourceKinds=["vscode"], exact cwd, useStateDbOnly=true)
thread/read (includeTurns=true)
```

It found the correct persisted thread and output, but its runtime status was
`notLoaded`. Its hydrated turn lifecycle also disagreed with the live IDE/team
execution. A private SQLite projection observed during the probe showed an
in-progress turn, while the separate server's hydrated view reported an
interrupted turn. That disagreement is itself evidence that a second process or
persisted record is not an authoritative live-state source.

Process existence, file modification time, writer-lock-file existence, and log
recency are likewise insufficient to distinguish `ACTIVE` from `NOT_ACTIVE`.

## 4. Last-output capture

Best-effort local output capture is feasible from the persisted thread rollout
or thread-history projection. The probe retrieved the latest `agentMessage.text`
for this live session and matched it to the current thread.

The adapter must tolerate:

- an incomplete final JSONL record;
- schema/version changes;
- commentary without a final answer;
- a turn that stopped before setting `final_agent_item_id`; and
- output being unavailable.

This source is acceptable as the plan's best-effort fallback. It is not a live
activity detector.

## 5. Existing-session prompt delivery

On the same live App Server, the official protocol supports:

```text
idle loaded thread  -> turn/start
active turn         -> turn/steer with the exact expectedTurnId
```

The standalone probe cannot invoke those methods on the observed IDE server:

- the extension owns both ends of its stdio connection;
- duplicating or intercepting those handles would race the VS Code reader and
  corrupt/interleave protocol messages;
- launching another server and calling `thread/resume` establishes ownership in
  another process rather than attaching to the IDE server; and
- the matching thread writer lock was held by the live owner.

The installed extension exposes UI commands to open/new/add context, but no
documented command that accepts arbitrary prompt text plus a target thread. The
VS Code CLI also does not expose arbitrary in-window extension-command
execution.

Therefore `send_prompt(session, text)` is unavailable without a shim. It would
be unsafe to represent a second App Server or a new thread as the existing IDE
session.

## 6. Remote-SSH findings

VS Code reported a live Linux remote server, remote extension host, and remote
Codex App Server. The process topology proves that the following all live on the
remote host:

```text
Codex App Server stdio
Codex home/session data
repository path
Git execution context
```

The remote Codex log contained extension/App Server initialization but no
conversation/thread ID, so there was no remote Codex session available for the
required transition, output, or prompt tests.

An independent `ssh -o BatchMode=yes` probe failed authentication. The watchdog
must not reuse passwords, bypass MFA, or assume that VS Code's private SSH
tunnel is a general command channel. Local Windows Git must never be pointed at
the remote URI/path.

Remote support therefore requires one of:

1. separately configured noninteractive SSH access and a remote helper;
2. a supported VS Code remote execution bridge; or
3. the same minimal extension-host shim used for local sessions, running in the
   remote extension-host context.

## 7. Least-fragile fallback

The preferred adapter boundary is a very small, user-authenticated shim inside
or upstream of the OpenAI VS Code extension's existing App Server connection.
It should expose only:

```text
window/workspace identity
selected Codex thread/session identity
live thread status and active turn ID
completed assistant output plus a monotonic cursor
start/steer a verbatim prompt with compare-and-send preconditions
```

The standalone watchdog retains all policy, Git, email, debounce, state, and
resume-prompt lifecycle logic. The shim must not become an orchestrator.

An authenticated loopback WebSocket/shared App Server could be a proof of
concept, but the official transport is experimental, Windows daemon lifecycle
is unavailable, and it cannot retrofit the already-open session. It is not the
recommended production dependency.

## 8. Gate decision and next experiment

Do not implement Phase 1 against persisted status or a second App Server.

The next checkpoint should perform only a minimal-shim feasibility test and
must prove all of the following for both a local window and a Remote-SSH window
with a real Codex thread:

1. stable window/workspace/thread identity across several polls;
2. a positive `active -> idle` transition;
3. retrieval of the completed assistant output and cursor;
4. verbatim prompt delivery to that exact idle thread;
5. observation that the same thread becomes active; and
6. network/extension-host failure maps to `UNKNOWN`.

If no supported hook can provide these contracts, record a hard feasibility
blocker. Do not launch a fresh conversation as a substitute.
