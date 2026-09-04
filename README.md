# Local Codex Watchdog

<p align="center">
  <img src="Shiro.png" alt="Shiro, the Codex Watchdog icon" width="240">
</p>

This repository contains a runnable foreground MVP of a deterministic Codex
watchdog. It is deliberately a foreground process rather than an installed
Windows service or daemon. It was built intentionally through **human-led vibe
coding and continuous live dogfooding**, with substantial assistance from
ChatGPT and OpenAI Codex.

> [!NOTE]
> This is an independent community project. It is not affiliated with,
> endorsed by, or sponsored by OpenAI, Microsoft, GitHub, Slack, or their
> respective affiliates. Product names and marks belong to their owners.

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](principleManga.png)

## Vibe coding and AI development declaration

This project is openly and intentionally a product of **human-led vibe coding**.
AI assistance was not incidental: ChatGPT and OpenAI Codex were deeply involved
throughout design, implementation, debugging, testing, documentation, and live
acceptance. No attempt is made to conceal or minimize that involvement.

The division of work was roughly:

- **Human maintainer / project owner** — identified the workflow problem and
  product goal; made the major architecture and ownership decisions; repeatedly
  removed unnecessary complexity; decided what WatchDog must and must not do;
  set privacy, permission, and operational boundaries; prioritized work from
  real failures; performed operator-side setup and live acceptance; and retains
  final responsibility for project direction and release decisions. Important
  design decisions include the lightweight `observe -> wake -> notify -> relay`
  model, using GitHub as the durable management plane, keeping semantic work in
  Codex, and the rule **"WatchDog observes Git; Codex owns Git."**
- **ChatGPT (OpenAI)** — served primarily as the architecture, management, and
  review layer: discussed and critiqued designs with the maintainer, analyzed
  failure modes, audited Codex progress reports, translated decisions into
  concrete implementation/review instructions, helped define safety and trust
  boundaries, reviewed live results, edited project documentation through the
  connected GitHub workflow, and helped create explanatory visuals and project
  presentation material.
- **OpenAI Codex** — performed a substantial fraction of the hands-on software
  engineering: implemented production code, tests, PowerShell tooling, remote
  adapters, notification and Slack relay paths; ran diagnostics and bounded
  live probes; fixed bugs found during dogfooding; maintained documentation and
  progress reports; and carried out normal repository changes under the agreed
  scope and ownership model.

The development method was deliberately empirical: run the real workflow,
observe an actual failure, make the smallest useful repair, live-test it, and
continue working — informally, **"repair the tires while driving on the
highway."** The progress reports under `doc/Progress/` are kept as part of that
development record, subject to the repository's privacy and public-release
review.

The implementation combines five narrow mechanisms:

1. a synchronous native `Stop` hook that waits for a short grace period and can
   return one identified continuation to the same turn; and
2. the experimental first-party `codex queue` command for waking a durable
   existing thread after it has parked; and
3. per-cycle discovery of eligible local and Remote-SSH VS Code workspaces,
   with a durable process-local manual-override registry and fail-closed,
   read-only Git inspection; and
4. a foreground polling loop with Slack, SMTP, opt-in Windows desktop-message,
   and local-audit notification results; and
5. an optional allowlisted Slack Socket Mode relay that maps replies only from
   WatchDog-created Slack threads back to the exact existing Codex thread.

The handler also records `PermissionRequest` as a pre-routing observation. It
does not treat that event as proof that a user is waiting, because AutoReview
may handle the request after the hook runs.

## Current status

Implemented and unit-tested:

- a filesystem instruction inbox with atomic publication and Windows-safe
  filenames;
- explicit target session IDs for short-stop instructions;
- per-session and store locks, including a cross-process Windows test;
- bounded retry of transient Windows store/atomic-replace contention without
  retrying model continuation or queue delivery;
- one continuation intent per session/turn;
- `stop_hook_active` loop protection and continuation confirmation;
- a configurable 5-20 minute production grace range (10 minutes by default);
- privacy-limited audit records that hash assistant output instead of retaining
  it, plus an exactly correlated transient Stop-output spool for notifications;
- a narrow `codex queue` adapter with collision and uncertain-delivery handling;
- passive queue-revision and exact rollout-marker observation that distinguishes
  `enqueued`, `consumed_or_started`, and `started`;
- atomic claim and retention of `resume_prompt.md`;
- a fixed mechanical prompt for a remote Git update;
- a conservative user-hook installer that refuses to overwrite a different
  existing configuration and never edits Codex trust state;
- automatic discovery of every exactly mappable live window in the current
  stable VS Code user-data instance, local Git-root resolution, and fail-closed
  selection of one loaded VS Code user thread for each eligible repository;
- an atomic, locality-bound manual workspace/session override registry that
  rejects Remote-SSH URIs, UNC paths, and other paths not owned by the current
  process locality;
- read-only `git ls-remote` branch-OID inspection with separate
  tracked/untracked flags and fail-closed blockers for ambiguous or unsafe
  repository states; and
- a globally locked `service-once` pass that isolates workspace failures and
  atomically persists privacy-limited observations and stable transition
  fingerprints;
- a foreground `run` loop with a configurable interval and Ctrl-C shutdown;
- a runtime-enforced read-only Git command allowlist; WatchDog never stages,
  commits, fetches, pulls, merges, rebases, resets, checks out, or pushes;
- a thin Remote-SSH adapter that runs beside the owning VS Code Server,
  correlates the exact loaded thread, observes terminal rollout completion and
  Git/OID state, and journals one exact-thread queue wake;
- a 45-second rollout `task_complete` fallback for the rare case where VS Code
  omits the Stop hook, with exact output delivery and late-hook deduplication;
- exact remote-OID change detection that leaves HEAD, the index, the worktree,
  and remote-tracking refs unchanged and queues one identified Codex-owned
  synchronization turn; and
- debounced Remote-SSH health tracking that reports a lost SSH adapter or a
  vanished VS Code window, retries failed notification delivery, and reports
  recovery; and
- environment-configured Slack-primary, SMTP-fallback, optional Windows
  `msg.exe`, and hash-only persisted notification deduplication; and
- a single-listener Slack reply relay with exact thread/workspace mapping,
  allowlisted operators, idempotent event receipts, and local plus Remote-SSH
  delivery through the existing first-party queue paths.

Important limitations:

- A manually trusted user-level hook passed live acceptance on an actual
  already-open VS Code thread: Stop continued once with retained context,
  `stop_hook_active` confirmed and parked the intent, and a separately queued
  wake started on that same durable thread. A trusted installed-hook harness
  also passed two independent continuation cycles and a 30-second no-instruction
  park with no watchdog model polling during the waits.
- Open Remote-SSH windows are automatically passed to a small Python probe over
  non-interactive SSH. The probe runs next to the remote VS Code Server and
  never hands a remote path to Windows Git. Automated behavior is covered by
  focused tests; live HPC provider acceptance still requires the Windows watchdog's SSH
  public key to be authorized on the remote account.
- Automatic session resolution requires one live VS Code extension host and
  Codex App Server, the exact window-scoped Codex session resource, one
  unarchived VS Code user-thread row whose canonical working directory matches
  the workspace or repository, a held writer lock, and that same window's
  latest exact-thread stream role of `owner`. A held lock proves that the
  session is loaded, not that a model turn is active. No match, multiple
  matches, malformed private state, or ambiguous repository ownership remains
  unresolved and is never guessed from recency.
- `enqueued` means an exact native queue acknowledgement was parsed. Passive
  queue-revision evidence can promote that to `consumed_or_started`, and an
  exact new rollout `UserMessage` marker can promote it to `started`; none of
  these states claims that the model completed the turn.
- Crash recovery intentionally favors no automatic duplicate continuation over
  guaranteed delivery. An inflight or uncertain record may need manual review.
- Passive observation reconciles durable delivery evidence and retries only a
  narrow set of transient Windows metadata replacements. It never redispatches
  an acknowledged queue message.
- Resume disposition parsing and DISCARD/ARCHIVE file actions are not built.
- The MVP is a user-started foreground loop. Service installation, scheduling,
  UI automation, and a general Codex ACTIVE/NOT_ACTIVE provider are not built.
- Slack text replies can wake an existing thread, but Slack cannot directly
  approve a VS Code-owned Codex permission request. The installed Windows Codex
  CLI exposes exact-thread queuing but no supported exact-request approval API,
  and WatchDog does not commandeer VS Code's private app-server transport.
- Git is observation-only in every WatchDog locality. Dirty tracked files,
  untracked files, and local-ahead commits remain available in internal
  observations but do not independently notify the user; Codex alone decides
  whether to stage, commit, synchronize, or publish them.
- Divergence, conflicts, operation/lock state, detached or unborn HEAD,
  missing/ambiguous upstream, authentication failure, and state races remain
  observable blockers. The queued Codex turn owns all repository changes.
- `config.example.toml` remains a concise settings reference. Notification
  credentials are read directly from environment variables and must not be
  written into that file.

See [architecture.md](architecture.md) and the latest report under
`doc/Progress/` for the exact evidence and feasibility decision.

## Requirements

- Python 3.9 or newer
- pytest for the test suite
- Git for read-only local and remote-OID inspection
- Codex CLI for native-hook and queue acceptance probes

The queue behavior reported by this checkpoint was observed specifically with:

```text
codex-cli 0.151.0-alpha.7.2
openai.chatgpt 26.825.51511
Windows
```

`codex queue` is experimental in that installed alpha build. Re-probe behavior
after upgrading Codex.

## Launch the foreground MVP

Install this checkout in editable mode, then inspect what the watchdog can
resolve from the currently open VS Code windows:

```powershell
python -m pip install -e .
codex-watchdog workspace-discover
```

`workspace-discover` correlates VS Code's window state with the live process
tree and prints each surviving window's workspace URI, storage identity,
locality, repository, session resolution, tracking status, and any fail-closed
reason. It does not fetch or mutate Git, send a prompt, or modify Codex state.
Eligible local windows need no manually entered workspace name, repository
path, or thread UUID.

Discovery, hook evidence, and queue wake must use the same Codex state tree.
For a nondefault installation, set `CODEX_HOME` before launch or pass the same
`--codex-home` path explicitly.

Repeat `--exclude` to omit an exact repository name, canonical path, or VS Code
workspace URI from automatic discovery:

```powershell
codex-watchdog workspace-discover --exclude scratch --exclude test-repo
codex-watchdog run --interval 300 --exclude scratch --exclude test-repo
```

Exclusions affect automatic local and Remote-SSH candidates before any remote
adapter is invoked; a deliberate manual registration for the same repository
remains an override.

Start the foreground loop at the five-minute default cadence:

```powershell
codex-watchdog run --interval 300
```

On Windows, the root-level launcher finds this checkout automatically and also
restores the DPAPI-encrypted Slack webhook and optional reply-relay credentials
described below:

```powershell
.\watchdog.ps1
```

Useful test and filtering forms are:

```powershell
.\watchdog.ps1 -Once
.\watchdog.ps1 -IntervalSeconds 5 -Exclude ProjectAlpha,scratch
.\watchdog.ps1 -DryRun -Exclude ProjectAlpha
```

`-DryRun` validates configuration and prints a privacy-safe launch summary
without starting the loop. The launcher never prints Slack tokens or the
webhook URL. It inherits other notification environment variables, including
Outlook settings, from the calling PowerShell process. A saved Duo fallback
opens its interactive login window only while the matching, non-excluded
Remote-SSH workspace is open.

From an uninstalled checkout, use the repository launcher with the same global
options and subcommands:

```powershell
python tools\codex_watchdog.py --runtime .codex-watchdog run --interval 300
```

Each cycle discovers the open windows again, combines eligible local windows
with any explicit manual overrides, invokes the thin adapter for Remote-SSH
windows, inspects Git and the exact remote branch OID without updating Git
state, records an audit result, sends or deduplicates useful notifications, and
sleeps. Opening or closing a window therefore takes
effect without restarting the watchdog. The cycle persists a privacy-sensitive
discovery snapshot under `.codex-watchdog/service/workspace-discovery.json` and
includes discovery counts and reason codes in its JSON result. Press Ctrl-C for
a clean shutdown. An automatically discovered workspace is resolved again
immediately before a queue wake; a changed or missing window/thread mapping
defers the operation.

Remote monitoring loss is debounced across two consecutive cycles. The same
rule covers a failed SSH/Plink probe, an unresolved exact remote Codex thread,
and a previously tracked Remote-SSH VS Code window that disappears. After the
second failure, the loop sends one Slack-primary attention message and keeps
polling. A failed external delivery is retried on later cycles instead of being
deduplicated as delivered. When the window and exact thread become reachable
again, the loop sends one recovery message. A MFA HPC shared-connection alert
explicitly says that fresh password and Duo approval are required.

This protection requires the Windows watchdog process itself to remain running
and able to reach Slack or a configured fallback. If the watchdog host loses
power/network access, no process can send an immediate remote alert; normal
polling and pending delivery resume when the process and network return.

Run exactly one cycle when testing configuration or recovering manually:

```powershell
codex-watchdog run --once
```

Use `--manual-only` only when deliberately operating from explicit
registrations or diagnosing automatic discovery:

```powershell
codex-watchdog run --once --manual-only
codex-watchdog service-once --manual-only
```

On first startup, existing Stop audits are baselined instead of replayed. The
following override replays only the latest matching Stop during the first
cycle:

```powershell
codex-watchdog run --once --replay-latest-stop
```

`--replay-latest-stop` is for explicit dogfood or recovery only. Do not use it
for routine startup: it can deliberately reprocess a historical Stop.

For each new matching Stop, the MVP notifies with the exact correlated terminal
output. If VS Code omits the Stop hook, the foreground loop recognizes the
exact rollout `task_complete` after 45 seconds and produces the same deduplicated
notification. Ordinary dirty, untracked, and local-ahead state is recorded
without producing a notification; WatchDog never changes the repository. The
fixed wake prompt tells Codex to inspect and safely publish unfinished work when
appropriate.

Inbound Git uses a separate, mutation-free path. `git ls-remote` reads the
configured branch OID without fetching or updating `FETCH_HEAD`, remote refs,
HEAD, the index, or the worktree. A changed OID queues one fixed instruction to
the exact current Codex thread. That turn inspects and safely synchronizes Git,
handles straightforward conflicts when the correct result is clear, and stops
for a decision when it is not. Queue dispatch does not require PARKED evidence;
the first-party queue holds a message until the thread can consume it. An exact
CLI acknowledgement leaves the remote OID durably pending. Later cycles
passively reconcile the same queue record without resending, and clear the OID
only after `consumed_or_started`/`started` evidence or when local `HEAD` already
equals that remote OID.

`state_changed_during_observation` is a transient busy result rather than Git
attention. The foreground loop retries that read-only observation once; if the
repository is still moving, the cycle records the snapshot but does not advance
its Stop cursor, pending remote OID, wake state, or temporal prompt. The next
ordinary cycle retries the same work without requiring another commit.

When a remote OID and a single-workspace `resume_prompt.md` coexist, the remote
wake leaves the file in place and its fixed prompt tells Codex to inspect the
temporal instruction before Git. Codex decides in task context whether to use
it and then delete it or archive useful context under `resume/archive/`.
Ambiguous multi-workspace resume files remain untouched and do not block an
independently exact remote-OID wake.

For Remote-SSH, the Windows process discovers the open window and sends a
self-contained probe to the SSH authority with `python3 -`. The local window
cache supplies a bounded set of exact thread claims. The remote probe accepts a
claim only after matching the remote Codex state row, repository cwd, and owner
log. When a window reload leaves multiple historical owner claims in its cache,
exactly one currently active owner may disambiguate them; zero or multiple
active owners fail closed. Older layouts can still resolve from a remote window
cache. The adapter then observes the rollout, queue database, and Git checkout
in that locality. It returns compact JSON and never copies large state or
invokes mutating Git. No workspace/thread ID is entered manually.

The foreground process is non-interactive, so the SSH host must permit key
authentication. The adapter invokes the exact VS Code host alias with `ssh -T`
and command-line overrides for `BatchMode=yes`, `PubkeyAuthentication=yes`, and
`PreferredAuthentications=publickey`. This permits one alias to remain
password-only for normal interactive VS Code use while the watchdog selects a
restricted no-PTY key solely for its probe. Verify that path with
`ssh -T -o BatchMode=yes -o PubkeyAuthentication=yes -o
PreferredAuthentications=publickey <alias> true`; it must exit zero before
starting the watchdog.

Some MFA-protected HPC login nodes are not ordinary key-authenticated hosts.
Their site policy may require password plus a second factor and may limit key
authentication to approved exceptions. Editing `~/.ssh/authorized_keys` must
not be treated as a policy bypass. The current non-interactive adapter therefore
remains the primary transport and is unchanged for key-enabled hosts such as
the generic `gpu-lab` example used in this repository.

On Windows, an optional MFA HPC fallback can reuse a PuTTY/Plink shared SSH-2
connection after one interactive password and Duo approval. PuTTY documents
that later tools can run as downstreams of one authenticated upstream, and
that `plink -shareexists` checks for an upstream without opening a new
connection. The watchdog performs that check first, uses `-batch` for every
automated downstream, and selects the fallback only for the exact configured
host after the ordinary OpenSSH adapter reports an authentication/transport
failure. It never stores a password, Duo response, or token.

Install PuTTY, or download the official 64-bit `plink.exe` from
[PuTTY's download site](https://www.chiark.greenend.org.uk/~sgtatham/putty/latest.html)
to `.codex-watchdog/bin/plink.exe`. Configure the nonsecret target once:

```powershell
.\watchdog.ps1 `
  -DuoTarget operator@hpc-login.example.edu `
  -SaveDuoConfig
```

If no upstream exists, the launcher opens a separate visible Plink console.
Verify and accept the host key if this is the first PuTTY connection, then
complete the password and Duo prompts. The watchdog starts as soon as
`-shareexists` finds the upstream and a real downstream `true` command succeeds;
this avoids treating the pre-authentication sharing endpoint as ready. Keep
that small Plink window open; closing it deliberately disables the fallback.
Future launches need only:

```powershell
.\watchdog.ps1
```

Use `-NoDuo` to ignore the saved fallback for one run. Direct Python launches
can opt in with `CODEX_WATCHDOG_DUO_PLINK_TARGET=user@host` and
`CODEX_WATCHDOG_PLINK_EXE=C:\path\to\plink.exe`. On Windows, native OpenSSH
accepts `ControlMaster` syntax but does not implement the required control
socket, and Git-for-Windows OpenSSH cannot pass the multiplexed standard-input
descriptor from a native PowerShell process. The fallback therefore does not
depend on either unsupported behavior or VS Code's private tunnel.

The remote payload retains compatibility with MFA HPC's observed Python 3.6 by
using `universal_newlines=True` rather than the Python 3.7 `text=True` alias.
The same payload remains the only implementation of thread correlation, Git
observation, queue wake, and completion observation for both transports.

## Notifications

Notification endpoint configuration and password credentials are
environment-only. Personal Outlook OAuth tokens use the encrypted local cache
described below. Delivery order is Slack, SMTP, opt-in Windows `msg.exe`, then a
local-audit result. An atomic per-workspace and event fingerprint suppresses
unchanged repeats across cycles after the event is handled; a new transition is
delivered immediately. When every configured external transport fails, the
fingerprint is not recorded as delivered, so the next cycle retries it.
Durable debounce state contains hashes and timestamps only, not webhook URLs,
credentials, recipients, or notification text.

Every user-facing transport receives the same presentation-layer subject. The
workspace label is the tracked repository root basename (for example,
`LocalCodexWatchDog`); the stable internal workspace ID is used only as a safe
fallback and remains unchanged in durable state and diagnostic fields. The
shared label helper adds the remote locality where useful, for example
`ProjectAlpha @ hpc-login`.

For a terminal Stop, the hook atomically spools the exact
`last_assistant_message` under the gitignored runtime. The foreground loop
attaches it verbatim to the matching Stop notification only after exact
invocation/session/turn/workspace/hash correlation. Normal outputs are
unchanged; outputs over 32,000 characters are clearly truncated with the
original count. The spool is deleted only after a configured external transport
succeeds and is retained after failure or audit-only delivery. Durable audits
continue to store only availability, SHA-256, and character count.

Supported environment variables are:

```text
CODEX_WATCHDOG_SLACK_WEBHOOK_URL
CODEX_WATCHDOG_SLACK_BOT_TOKEN
CODEX_WATCHDOG_SLACK_APP_TOKEN
CODEX_WATCHDOG_SLACK_CHANNEL_ID
CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS
CODEX_WATCHDOG_SMTP_HOST
CODEX_WATCHDOG_SMTP_PORT
CODEX_WATCHDOG_SMTP_AUTH
CODEX_WATCHDOG_SMTP_USERNAME
CODEX_WATCHDOG_SMTP_PASSWORD
CODEX_WATCHDOG_SMTP_FROM
CODEX_WATCHDOG_SMTP_TO
CODEX_WATCHDOG_SMTP_SECURITY
CODEX_WATCHDOG_OUTLOOK_CLIENT_ID
CODEX_WATCHDOG_WINDOWS_MSG
CODEX_WATCHDOG_WINDOWS_MSG_TARGET
CODEX_WATCHDOG_NOTIFICATION_TIMEOUT_SECONDS
```

### Slack incoming webhook

Create a Slack app, activate Incoming Webhooks, and add one webhook to the
intended channel by following Slack's [incoming-webhook guide][slack-webhooks].
The generated URL is a secret: do not paste it into chat, commit it, or print it
in logs. Copy it to the clipboard and load it into the current PowerShell
process without placing the URL in command history:

```powershell
$webhook = (Get-Clipboard -Raw).Trim()
if ($webhook -notmatch '\Ahttps://hooks\.slack\.com/services/[^\s]+\z') {
    throw "Clipboard does not contain a valid Slack webhook URL."
}
$env:CODEX_WATCHDOG_SLACK_WEBHOOK_URL = $webhook
Remove-Variable webhook
```

Send a direct test with a unique ID so durable debounce cannot suppress an
intentional repeat:

```powershell
$stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
codex-watchdog --runtime .codex-watchdog notify-test `
  --id "slack-direct-$stamp" --workspace watchdog-dev
```

The result should report `sent`, channel `slack`, and attempted channels
containing only `slack`. When SMTP is also configured, Slack is primary and
SMTP is used only after a Slack failure.

On Windows, the current user can optionally save the webhook with DPAPI-backed
CLIXML and restore it in a later PowerShell without repasting the secret. The
encrypted file works only for the same Windows account on the same computer:

```powershell
$store = Join-Path $env:LOCALAPPDATA "CodexWatchdog\slack-webhook.clixml"
New-Item -ItemType Directory -Force -Path (Split-Path $store) | Out-Null
$credential = [pscredential]::new(
    "slack-webhook",
    (ConvertTo-SecureString $env:CODEX_WATCHDOG_SLACK_WEBHOOK_URL -AsPlainText -Force)
)
$credential | Export-Clixml -LiteralPath $store
Remove-Variable credential

$credential = Import-Clixml $store
$env:CODEX_WATCHDOG_SLACK_WEBHOOK_URL = $credential.GetNetworkCredential().Password
Remove-Variable credential
```

PowerShell documents that credential objects exported with `Export-Clixml` on
Windows are encrypted using DPAPI; this protection does not apply to CLIXML
exported on non-Windows systems. See the official
[`Export-Clixml` documentation][powershell-export-clixml].

### Slack quick-reply relay

The optional relay adds inbound replies without replacing the incoming webhook
or creating another Codex session. WatchDog posts a notification with the bot
API, stores the returned Slack channel/thread identity beside the already
resolved Codex workspace and thread, and accepts replies only from configured
Slack user IDs in that known Slack thread. It passes ordinary text essentially
verbatim to the existing exact-thread queue. Bot messages, arbitrary channel
messages, unknown threads, unauthorized users, and duplicate events are
ignored. Durable relay receipts contain hashes and routing identifiers, never
Slack tokens or reply text.

Use one Slack app and one running WatchDog Socket Mode listener:

1. Enable **Socket Mode** and create an app-level token with
   `connections:write`.
2. Add the bot scope `chat:write`. For a public channel, also add
   `channels:history` and subscribe to the `message.channels` bot event. For a
   private channel, use `groups:history` and `message.groups` instead.
3. Reinstall the app after changing scopes, invite the bot to the selected
   channel, then copy that channel ID and the operator's Slack member ID. Use a
   channel ID beginning with `C` or `G`; one-to-one `D` identifiers are not
   supported by this relay.

These are the standard [Slack Bolt Socket Mode][slack-bolt-socket-mode] and
[`chat.postMessage`][slack-chat-post-message] paths. No public callback URL is
required. Stop any older WatchDog process before starting the configured one;
running two Socket Mode clients for the same Slack app can split deliveries.

Install the relay dependency. The direct form also works with older pip
versions that cannot perform a PEP 517 editable install:

```powershell
python -m pip install "slack-bolt>=1.20,<2"
```

Save both tokens without putting their plaintext in shell history. The two IDs
are nonsecret but are kept in a local configuration file for convenience. The
interactive helper validates the values and writes the two secrets with
current-user Windows DPAPI protection:

```powershell
.\setup-slack-relay.ps1
```

Use `-Force` only when deliberately replacing an existing relay configuration.

Start exactly one foreground listener. Its safe startup summary should show
`slack_reply : encrypted_store`:

```powershell
.\watchdog.ps1
```

While it remains running, open a second PowerShell in this checkout and post a
uniquely tagged, reply-enabled notification for one exactly discovered local
workspace:

```powershell
.\watchdog.ps1 -SlackRelayTest -SlackRelayWorkspace LocalCodexWatchDog -NoDuo
```

Reply inside that Slack thread. A successful relay immediately answers
`Queued for the exact existing Codex thread.` The text then appears in that
same VS Code Codex conversation; its ordinary completion notification returns
to Slack and creates another safely mapped notification thread. An uncertain
queue or remote delivery is reported and never blindly resent.

The relay-test JSON must contain `"relay_mapping":"created"`. A
`"relay_mapping":"missing"` result means the visible Slack message came from
the one-way webhook fallback and replies to it cannot be relayed. Confirm that
the configured ID belongs to the bot-accessible channel and begins with `C` or
`G`, then generate a fresh test message.

Direct Approve/Deny buttons are deliberately absent. On this Windows build,
`codex queue` supports exact-thread text delivery but does not expose a
supported API for answering a pending approval owned by VS Code, while
`codex app-server daemon` lifecycle control is Unix-only. The safe workaround
is to perform the unavoidable local approval/UAC/Duo action and reply with text
such as `permission fixed; retry`.

SMTP requires host, sender, and one or more comma- or semicolon-separated
recipients. `CODEX_WATCHDOG_SMTP_AUTH` selects the authentication mechanism.
Its values are `password`, the default, and `outlook_oauth2`. The existing
password mode accepts an optional username and password, but they must be
supplied together; leave `CODEX_WATCHDOG_OUTLOOK_CLIENT_ID` unset in that mode.
Security is `starttls` by default and also accepts `ssl` or `plain`; the default
port is 587, or 465 with `ssl`. The transport timeout defaults to 10 seconds.

### Personal Outlook.com or Hotmail with OAuth2

Outlook.com requires OAuth2/Modern Auth for SMTP. Its current submission
endpoint is `smtp-mail.outlook.com` on port 587 with STARTTLS. The watchdog uses
the delegated `https://outlook.office.com/SMTP.Send` permission and a one-time
device-code login; it does not ask for or store the Microsoft account password.
Microsoft documents the [Outlook.com SMTP settings][outlook-smtp-settings] and
the [SMTP OAuth/XOAUTH2 protocol][outlook-smtp-oauth].

First create a public-client app registration:

1. Sign in to the Microsoft Entra admin center. Creating the registration
   requires an Entra tenant, an Azure account with an active subscription, and
   at least the Application Developer role in that tenant. A personal Microsoft
   account can create a free Azure account and use its default directory.
2. Under **Entra ID > App registrations > New registration**, choose
   **Personal accounts only**, then copy the **Application (client) ID**.
3. Under **Authentication > Advanced settings**, set **Allow public client
   flows** to **Yes**. Device-code flow needs no redirect URI.
4. Under **API permissions**, add the Office 365 Exchange Online delegated
   permission `SMTP.Send`. Do not select the application permission
   `SMTP.SendAsApp` for a personal mailbox.
5. Do not create a client secret. A local/desktop public client cannot protect
   one, and the device-code flow does not use one.

The personal mailbox owner grants the delegated permission during device login;
this path does not use tenant-wide admin consent or Exchange service-principal
registration.

Microsoft's [app-registration guide][entra-app-registration] describes the
prerequisites and personal-account audience. Its [public-client guide][public-client]
documents the required public-client setting.

Configure the same PowerShell process that will run `outlook-login`, the tests,
and the foreground watchdog. Use the signed-in Outlook.com/Hotmail address for
both the username and sender; Outlook OAuth mode requires those values to match.
It also requires the exact host, port, and security values shown below and
rejects a configured SMTP password. Do not paste an address or token into an
issue, progress report, or chat transcript.

```powershell
$env:CODEX_WATCHDOG_SMTP_AUTH = "outlook_oauth2"
$env:CODEX_WATCHDOG_OUTLOOK_CLIENT_ID = "<Application (client) ID UUID>"
$env:CODEX_WATCHDOG_SMTP_HOST = "smtp-mail.outlook.com"
$env:CODEX_WATCHDOG_SMTP_PORT = "587"
$env:CODEX_WATCHDOG_SMTP_SECURITY = "starttls"
$env:CODEX_WATCHDOG_SMTP_USERNAME = "<your Outlook.com or Hotmail address>"
$env:CODEX_WATCHDOG_SMTP_FROM = $env:CODEX_WATCHDOG_SMTP_USERNAME
$env:CODEX_WATCHDOG_SMTP_TO = $env:CODEX_WATCHDOG_SMTP_USERNAME
Remove-Item Env:CODEX_WATCHDOG_SMTP_PASSWORD -ErrorAction SilentlyContinue
```

Authorize the account once:

```powershell
codex-watchdog --runtime .codex-watchdog outlook-login
```

Follow the verification URI and user code printed by the command and approve
only the named watchdog app and SMTP send permission. By default, the command
stores the resulting MSAL cache at
`%LOCALAPPDATA%\CodexWatchdog\oauth\outlook-<client-id-sha256>.bin`, where the
filename contains a SHA-256 digest rather than the client ID. The cache is
encrypted for the current Windows user with DPAPI. The command does not print
access or refresh tokens and does not fall back to plaintext storage. A
different Windows user, host, or client ID must perform its own login; changing
the watchdog `--runtime` does not select a different OAuth cache. Deleting the
cache removes only the watchdog's local ability to refresh. Revoke the app grant
in the Microsoft account security UI when authorization itself must be
withdrawn.

The command first prints one JSON object with `status` equal to
`authorization_required` and the short-lived Microsoft verification fields.
After successful consent it prints a second privacy-safe object with `status`
equal to `authenticated` and exits zero. Login fails closed if the authenticated
account does not match `CODEX_WATCHDOG_SMTP_USERNAME`. The command never
includes a token or mailbox address in either result.

Verify direct SMTP delivery with Slack unset. Use a fresh ID on every attempt
because failed and successful notification fingerprints are durable:

```powershell
Remove-Item Env:CODEX_WATCHDOG_SLACK_WEBHOOK_URL -ErrorAction SilentlyContinue
$stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
codex-watchdog --runtime .codex-watchdog notify-test `
  --id "outlook-direct-$stamp" `
  --workspace watchdog-dev
```

The command should exit zero with `status` equal to `sent`, `channel` equal to
`smtp`, and a real message in the recipient mailbox. Then force the documented
Slack-to-SMTP fallback without sending data to a real Slack endpoint:

```powershell
$env:CODEX_WATCHDOG_SLACK_WEBHOOK_URL = "http://127.0.0.1:1/watchdog-fallback"
$stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
codex-watchdog --runtime .codex-watchdog notify-test `
  --id "outlook-fallback-$stamp" `
  --workspace watchdog-dev
Remove-Item Env:CODEX_WATCHDOG_SLACK_WEBHOOK_URL -ErrorAction SilentlyContinue
```

That command should exit zero with `status` equal to `sent_fallback`, `channel`
equal to `smtp`, attempted channels `slack` then `smtp`, and a second received
message. Keep the configured PowerShell open and start `codex-watchdog run`
from it so the child process inherits the environment. Normal sends silently
refresh short-lived access tokens from the encrypted cache; if refresh is no
longer possible, run `outlook-login` interactively again.

Send a privacy-safe direct delivery test without manufacturing a Stop. Use a
new ID for each intentional delivery so durable debounce does not suppress it:

```powershell
codex-watchdog notify-test --id smtp-direct-001 --workspace watchdog-dev
```

The command emits the same privacy-limited notification result as the
foreground loop and exits successfully only for `sent` or `sent_fallback`.
It never prints the supplied credential or puts the test ID in the message.

The loop notifies on matching Stops and operational attention states such as
genuine Git blockers, sustained unreachable remote adapters or vanished remote
windows, remote monitoring recovery, and uncertain queue delivery.

Windows desktop messages are off by default. Opt in for a local dogfood run;
the target defaults to the current `USERNAME` when omitted:

```powershell
$env:CODEX_WATCHDOG_WINDOWS_MSG = "1"
# Optional: $env:CODEX_WATCHDOG_WINDOWS_MSG_TARGET = "my-user"
codex-watchdog run --once
```

Do not invent placeholder credentials. With no configured transport, the loop
returns and audits `local_audit` rather than failing the entire workspace cycle.

[outlook-smtp-settings]: https://support.microsoft.com/en-US/Outlook/pop-imap-and-smtp-settings-for-outlook-com
[outlook-smtp-oauth]: https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth
[entra-app-registration]: https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app
[public-client]: https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration
[slack-webhooks]: https://api.slack.com/messaging/webhooks
[slack-bolt-socket-mode]: https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/
[slack-chat-post-message]: https://docs.slack.dev/reference/methods/chat.postMessage
[powershell-export-clixml]: https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/export-clixml

## Run the tests

From the repository root:

```powershell
python -m pytest
python -m compileall -q src tools tests
```

The acceptance probes make model calls and should be run deliberately:

```powershell
python tools\run_appserver_hook_probe.py --hooks-only
python tools\run_appserver_hook_probe.py --isolated-user-hooks-only
python tools\run_appserver_hook_probe.py
```

The first command inspects temporary project-hook discovery. The second safely
proves that an isolated `CODEX_HOME/hooks.json` is discovered as a user hook;
it does not modify the real user configuration. The final command creates and
archives a fresh durable App Server thread. The older
`run_native_hook_probe.py` is retained as a negative probe because `codex exec`
did not emit `Stop` in the installed build.

## Hook setup

The repository intentionally does not install a live project
`.codex/hooks.json`. Project hook hot editing proved unreliable. The preferred
deployment is one reviewed user hook at `CODEX_HOME/hooks.json` in each Codex
environment (local Windows and Remote-SSH have separate environments).

Render the exact user-hook document before installing it:

```powershell
python tools\install_user_hooks.py --runtime .codex-watchdog
```

Install only when `CODEX_HOME/hooks.json` is absent or already semantically
identical:

```powershell
python tools\install_user_hooks.py --runtime .codex-watchdog --install
```

The command refuses symlinks, unreadable files, and any different existing hook
configuration. Merge a pre-existing configuration manually; the installer will
not guess. For Remote-SSH, run the installer from the remote checkout with the
remote Python, remote runtime, and remote `CODEX_HOME`.

Then:

1. Inspect the rendered or installed JSON and exact command paths.
2. If adapting `examples/hooks.json` manually, merge rather than overwrite and
   replace every placeholder with an absolute path and desired runtime.
   On current Windows builds, keep the Python, script, and runtime paths free of
   spaces and keep `commandWindows` completely quote-free. The hook runner's
   outer `cmd.exe /C` quoting can make embedded-quote forms exit before the
   handler starts ([openai/codex#38168](https://github.com/openai/codex/issues/38168)).
3. Start or resume a fresh Codex conversation.
4. Open `/hooks`, inspect the exact command/hash, and trust it manually.
5. Confirm that `Stop` and `PermissionRequest` are listed as enabled and
   trusted before relying on them.

Non-managed user hooks are trusted by exact definition hash. Any definition
change requires another explicit review. Never synthesize or edit Codex's
persistent hook trust state.

Hook commands run with the user's privileges. Never trust a hook you have not
reviewed. If the example is the only hook configuration, disabling the PoC
means removing that configuration before opening a fresh session; otherwise
remove only its entries and preserve unrelated hooks.

## Short-stop instruction

Publish one instruction for one exact Codex session/thread:

```powershell
python tools\codex_watchdog.py --runtime .codex-watchdog submit `
  --thread 11111111-2222-4333-8444-555555555555 `
  --id manual-1 `
  --source manual `
  --message "Continue with the approved instruction."
```

During the Stop grace window, the hook atomically claims only an instruction
whose target matches its `session_id`. It returns:

```json
{"decision":"block","reason":"[CODEX_WATCHDOG_INSTRUCTION ...]\n..."}
```

That native response asks Codex to continue in the same turn. When Stop is
entered again with JSON boolean `stop_hook_active: true`, the handler confirms
the inflight intent and parks. It never claims another instruction from that
active re-entry.

Every instruction ID is idempotent within one runtime directory. Reusing an ID
with different content, source, or target is a collision.

## Open-window discovery, manual overrides, and one-pass Git sensor

Inspect every eligible live window in the current stable VS Code user-data
instance, then run one sensor pass over the effective local workspaces:

```powershell
python tools\codex_watchdog.py --runtime .codex-watchdog workspace-discover
python tools\codex_watchdog.py --runtime .codex-watchdog service-once
```

Discovery first maps VS Code's persisted window entries to the live
`code --status` extension-host/App-Server process tree. Historical storage and
closed `lastActiveWindow` entries are therefore filtered out. For each local
window, it reads only `providerType` and `resource` from that window's
`agentSessions.model.cache`, then intersects those window-scoped resource IDs
with unarchived `source=vscode`, `thread_source=user` Codex rows having an exact
canonical working-directory match, a currently held writer lock, and the exact
window's latest privacy-safe stream-owner marker. Exactly one match is required;
labels, chat contents, and prompt log lines are neither selected nor persisted.
It does not select the newest thread when zero or several loaded sessions
match. Two windows that resolve the same repository and session are
deduplicated; the same repository resolving to different sessions is ambiguous
and omitted. Remote-SSH windows remain visible as remote candidates and are
handled only by the owning-locality SSH adapter.

The VS Code window files and Codex state/lock layout used for this correlation
are private implementation details. Re-run `workspace-discover` after VS Code
or Codex upgrades and treat any unresolved reason as a hard no-action result.
The normal loop never mutates Git. A held writer lock is ownership evidence,
not proof of an idle turn. Remote-update queue delivery deliberately does not
require PARKED.

Manual registration is an advanced override, not required normal setup. It can
pin an exact local repository and durable thread when automatic resolution is
unavailable or deliberately needs to be overridden:

```powershell
python tools\codex_watchdog.py --runtime .codex-watchdog workspace-add `
  --workspace watchdog-dev `
  --repo D:\projects\LocalCodexWatchDog `
  --thread 11111111-2222-4333-8444-555555555555
python tools\codex_watchdog.py --runtime .codex-watchdog workspace-list
python tools\codex_watchdog.py --runtime .codex-watchdog workspace-remove `
  --workspace watchdog-dev
```

An explicit registration wins over automatic resolution for the same canonical
repository and remains effective even when its VS Code window is closed.
`workspace-list` shows only these durable manual overrides; use
`workspace-remove` to return that repository to automatic discovery and
`workspace-discover` to see the effective combined set. `--manual-only` on
`run` or `service-once` disables automatic discovery while retaining the
override registry.

`service-once` processes effective workspace IDs in sorted order under a
nonblocking global lock. For each repository it performs a noninteractive
`git ls-remote` against the exact configured branch, validates local repository
state, and atomically writes one latest observation. Inspection does not modify
`FETCH_HEAD`, remote-tracking refs, `HEAD`, the index, or the worktree.

Tracked changes and untracked-file presence are recorded as separate booleans;
filenames and raw Git errors are not persisted. Divergence, operations in
progress, conflicts, locks, detached or unborn HEAD, missing/ambiguous upstream,
unsupported layouts, authentication/remote-check failures, and state races are blocked
rather than repaired.

## Long wake

Queue one identified prompt for an existing durable thread:

```powershell
python tools\codex_watchdog.py --runtime .codex-watchdog queue `
  --thread 11111111-2222-4333-8444-555555555555 `
  --id wake-1 `
  --message "Check the approved checkpoint comment."
```

The adapter executes an argv array with `shell=False` semantics. The installed
queue CLI accepts prompt text only as a command-line argument, so the prompt is
temporarily visible to same-user process inspection. Queue journal files store
only output digests/lengths, not raw subprocess output.

The queue adapter resolves `codex` from `PATH` first. A fresh PowerShell opened
outside VS Code often lacks that inherited path, so the Windows fallback finds
the newest installed `openai.chatgpt-*` extension under the current user's
stable or Insiders VS Code extension directory and invokes its bundled
`codex.exe` by absolute path. The equivalent VS Code Server extension roots are
checked for process-local remote agents. If neither a PATH command nor a
validated installed binary is available, dispatch remains fail-closed as
`uncertain`.

An identical dispatch request never invokes the queue command twice. For an
`enqueued` record it instead checks the existing queue database and rollout
evidence, promoting the same journal to `consumed_or_started` or `started` when
proven. An `uncertain` or `dispatching` record remains non-consuming and is
never retried automatically.

## Resume prompt

Producers must publish `.codex-watchdog/resume_prompt.md` by writing a temporary
file completely and atomically renaming it into place. The consumer moves it to
`resume/inflight/` before reading or dispatching it:

```powershell
python tools\codex_watchdog.py --runtime .codex-watchdog queue-resume-prompt `
  --thread 11111111-2222-4333-8444-555555555555
```

The inflight file is retained whether enqueue succeeds or becomes uncertain.
The prompt asks Codex to end with one disposition line, but the watchdog does
not interpret that line. If a remote wake exists at the same time, the source
file is deliberately not claimed: the fixed wake turn sees it at
`resume_prompt.md`, decides relevance with its current task context, then moves
valuable context to `resume/archive/` or deletes a one-shot prompt after use.

## Runtime data and security

The default runtime is `.codex-watchdog/`, which is gitignored. It can contain
plaintext prompts, absolute workspace paths, and stable session/turn
identifiers:

```text
.codex-watchdog/
  inbox/
  inflight/
  consumed/
  guards/
  locks/
  audit/
  workspaces.json
  service/workspace-discovery.json
  service/observations/
  slack/relay-state.json
  transient/stop-output/
  wake/records/
  resume/inflight/
  resume/archive/
```

Protect the directory with user-only filesystem permissions and apply an
appropriate retention policy. Do not place secrets in watchdog prompts.
