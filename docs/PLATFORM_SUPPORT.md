# Platform support and diagnostics

Codex WatchDog keeps routing, dispatch journals, filesystem state, Git
observation, notifications, Slack relay, queue evidence, and fail-closed safety
rules in shared Python code. `platform_adapters.py` contains only the host facts
that genuinely differ: standard VS Code data paths, `code --status` invocation,
Codex extension roots, application-data paths, credential-backend expectations,
and launcher capability.

## Support matrix

| Execution mode | Current level | What is verified | What remains |
| --- | --- | --- | --- |
| Windows x64 local desktop | **Full E2E verified; stable reference** | Native hooks, exact live workspace/thread ownership, 30-second Stop continuation, queue wake, Git observation, Slack/SMTP, Slack reply relay, Remote-SSH adapters, packaged one-click startup, previous-release upgrade, and embedded icon | Continue regression dogfood for every release |
| Linux Remote-SSH execution target | **Native-probe and real remote path verified** | Compact remote state/thread/Git/queue logic and notification/relay paths on a real Linux target | More host distributions and reconnect patterns |
| Linux local desktop | **CI verified preview** | Shared tests, POSIX locking/atomic replacement, standard VS Code/XDG paths, native CLI invocation, Codex binary discovery, and CLI/version smoke tests | Real Linux-desktop VS Code/Codex ownership, hooks, credential store, and full E2E |
| macOS local desktop | **CI verified preview/beta** | Shared tests on GitHub-hosted macOS, standard VS Code paths, native CLI invocation, Apple Silicon path/architecture modeling, POSIX storage, and CLI/version smoke tests | Real Apple Silicon VS Code/Codex ownership, hooks, Keychain-backed OAuth, launcher ergonomics, and full E2E |

Hosted CI is not full E2E validation. A platform advances from **CI verified** to
**native-probe verified** only after the diagnostic and relevant native probes
run on that operating system. It advances to **full E2E verified** only after a
real existing VS Code Codex thread completes Stop, notification, wake, and
restart/recovery acceptance.

No standalone Linux or macOS binary is published yet. Source installation and a
foreground CLI are the preview path. Windows remains the only packaged stable
release and the behavior that cross-platform changes must not regress.

## Read-only doctor

Run the capability audit from an installed source environment:

```sh
codex-watchdog doctor
codex-watchdog doctor --export
codex-watchdog doctor --export doctor-report.json
```

The global selectors also work:

```sh
codex-watchdog --runtime /path/to/runtime \
  --codex-home /path/to/codex-home \
  doctor --vscode-user-data /path/to/Code/User --export doctor-report.json
```

`doctor` does not dispatch, notify, modify Git, claim prompts, create runtime
locks, or write state. The optional export is the only write and is an atomic
JSON file requested by the caller.

The report contains:

- platform, normalized architecture, and support tier;
- availability counts and reason codes for the VS Code CLI and User data;
- workspace-storage readability counts;
- Codex extension, executable, home, state, session, and hook availability;
- live-window and exact current-thread resolution counts when VS Code is open;
- queue database/courier readiness without sending anything;
- the expected credential backend, launcher mode, and filesystem primitives;
- one overall `PASS`, `PARTIAL`, or `FAIL` result.

The export deliberately excludes raw home and repository paths, usernames,
workspace-storage keys, thread/session/workspace IDs, SSH hosts, commands,
conversation or message contents, tokens, webhooks, and other credentials. It
uses bounded counts, booleans, enumerated sources, and stable reason codes so it
can be attached to a macOS or Linux test report.

Interpretation:

- `PASS`: every probed capability expected for that support tier is available;
- `PARTIAL`: the host is usable for some paths, but a live window, queue state,
  secure credential backend, or platform launcher still needs validation;
- `FAIL`: a required local dependency/state source is missing, unreadable, or
  unsupported. WatchDog will not guess a target.

Closing VS Code before running the command normally produces `PARTIAL`, because
there is no live window/current owner to resolve. On macOS and Linux, the
preview credential and foreground-launcher checks also remain `PARTIAL` until
their native security and UX paths are accepted.

## Preview source installation

Python 3.9 or newer, Git, VS Code with the Codex extension, and a first-party
Codex CLI are required. Use an isolated environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
codex-watchdog --version
codex-watchdog doctor --export doctor-report.json
```

Choose an explicit user-owned runtime outside the source tree. For example:

```sh
# Linux
codex-watchdog --runtime "$HOME/.local/state/codex-watchdog/runtime" run --interval 120

# macOS
codex-watchdog --runtime "$HOME/Library/Application Support/CodexWatchdog/runtime" run --interval 120
```

These are foreground commands. Use Ctrl-C to stop them. A systemd user unit,
launchd agent, login item, or other background-service installer is not yet
provided; do not claim one exists and do not run the preview unattended before
native validation.

## Platform notes

### Windows

The one-click EXE discovers and preserves the versioned current-user launcher
profile. `watchdog.ps1` is an implementation detail/advanced launcher; opening
the EXE with no arguments is the upgrade-aware path. Slack/Duo configuration
uses current-user DPAPI stores and Outlook OAuth uses encrypted MSAL
persistence. Native hooks still require explicit review and trust.

### Linux local desktop

The adapter checks `$XDG_CONFIG_HOME/Code/User` or
`~/.config/Code/User`, invokes a native `code --status`, and searches the
standard local/Insiders/VS Code Server extension roots. Core storage uses
`flock` and atomic rename. Slack and generic SMTP environment configuration are
shared. Outlook OAuth requires an encrypted libsecret-compatible persistence;
it fails closed if that backend is unavailable.

Linux as a Remote-SSH target is a separate support surface. The compact helper
runs in the owning remote locality and returns bounded JSON to the foreground
WatchDog. It must remain a helper, not a second polling service.

### macOS

The adapter checks `~/Library/Application Support/Code/User`, invokes a native
`code --status` from `PATH` or the standard application bundle, and uses the
standard VS Code extension roots. Application data belongs under
`~/Library/Application Support/CodexWatchdog`. Storage uses POSIX locking and
atomic rename. Outlook OAuth requires Keychain-backed encrypted persistence and
must fail closed rather than fall back to plaintext.

Apple Silicon is the first native validation target. Intel packaging is not
planned until it is cheap and justified. Hosted macOS CI covers source behavior
only; the platform remains preview/beta until a real Apple Silicon VS Code
Codex session passes the complete live procedure.

## Native validation checklist

For a new local desktop platform:

1. Save `doctor --export` before opening VS Code and confirm expected `PARTIAL`
   reason codes.
2. Open exactly one ordinary VS Code folder with one Codex thread, rerun doctor,
   and verify the live/current-thread counts without exposing identifiers.
3. Review and manually trust native Stop/PermissionRequest hooks.
4. Verify one completed Stop, the 30-second parked behavior, and an actual
   notification.
5. Verify one exact-thread `codex queue` wake and restart reconciliation.
6. Verify Slack reply relay only for an allowlisted user/channel.
7. Verify encrypted credential persistence or record that the transport is not
   configured; never use plaintext as a test shortcut.
8. Repeat with two windows and an intentionally ambiguous/stale state case to
   confirm fail-closed behavior.
9. Record the OS, architecture, version, result, and remaining gaps without raw
   paths, IDs, hosts, messages, or secrets.
