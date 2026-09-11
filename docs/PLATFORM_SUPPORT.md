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
| Linux explicit same-thread source owner | **Native E2E verified on Ubuntu ARM64** | Exact operator binding, trusted Stop, actual client detach, same-thread queue/restart/context, idle release, and VS Code reattachment | Automatic Linux desktop discovery remains separate |
| Linux automatic remote handoff | **Native source lifecycle verified on Ubuntu ARM64** | Desktop priority, actual detach, independent remote owner, trusted Stop/queue deduplication, idle restart, old-epoch fencing and safe control handback | The first VS Code resume required a window reload; busy handback/crashes have deterministic coverage, and packaged/general-desktop E2E remains separate |
| Linux ARM64 executable package | **Hosted and real Ubuntu ARM64 package acceptance verified** | Actual ZIP with Python hidden: manifests, native licenses, privacy, startup, source-runtime reuse, replacement, stable hooks, production Stop and exact-owner queue/restart/release fixtures | Real-user packaged-hook trust and full detached package lifecycle remain distinct from fixture acceptance |
| Linux x64 executable package | **Hosted and real RHEL 8.10 package acceptance verified** | x86-64 ELF built on UBI 8/glibc 2.28 and tested on Ubuntu 22.04, UBI 8, and an isolated real RHEL 8.10 host: source-free startup, state preservation, HTTPS trust, licenses/privacy, Stop, locks and owner fixtures | Real-user/native-desktop E2E |
| Linux local desktop | **CI verified preview** | Shared tests, POSIX locking/atomic replacement, standard VS Code/XDG paths, native CLI invocation, Codex binary discovery, and CLI/version smoke tests | Real Linux-desktop VS Code/Codex ownership, hooks, credential store, and full E2E |
| macOS Apple Silicon source workflow | **Native E2E verified preview** | Exact live ownership where topology is resolvable, trusted Stop/continuation, queue/restart, and Keychain-backed Slack-only notification/reply/dedup | Shared or unnumbered VS Code window topology still fails closed; Outlook acceptance is separate |
| macOS 15 ARM64 package | **Developer preview; bounded v0.2.3 real-user E2E verified with a CA override** | Hosted source-free package checks; real-user stable install, Keychain reuse, trusted hooks, exact Slack reply, repeated Stop notification and Git wake in one manual workspace | v0.2.3 needed a CA override and manual rebind recovery; new-version device acceptance is separate; no Developer ID signature or notarization |

On 2026-09-11, the user reported successful manual detached dogfood on Spark
with no observed issue. This strengthens the Linux remote/server detached path
beyond generic preview and complements native lifecycle and package checks.
It is not evidence for Linux local desktop or for every cluster topology.

The local VS Code discovery defect addressed in v0.2.21 was reproduced and
natively accepted first on Windows. The resolver is shared: Windows can prove
writer PIDs through Restart Manager and Linux through local kernel lock/process
evidence. `writer_process_probe` currently supplies **no macOS writer PID**.
Consequently, macOS cannot use the missing-log fallback when routing evidence is
also unavailable. Such cases still fail closed; rebuilding the shared source
does not mean that this macOS limitation has been fixed.

Hosted CI is not full E2E validation. A platform advances from **CI verified** to
**native-probe verified** only after the diagnostic and relevant native probes
run on that operating system. It advances to **full E2E verified** only after a
real existing VS Code Codex thread completes Stop, notification, wake, and
restart/recovery acceptance.

The Apple Silicon ZIP is a published developer preview; see the
[Mac package guide](MACOS_PACKAGE.md). Linux ARM64 and x64 have
[self-contained executable ZIPs](LINUX_PACKAGE.md), with native package checks
on Ubuntu 22.04, plus UBI 8/glibc 2.28 for x64. Additional isolated real-machine
acceptance covers x64 RHEL 8.10/glibc 2.28 and ARM64 Ubuntu/glibc 2.39.
The [explicit same-thread source workflow](LINUX_SOURCE_WORKFLOW.md)
and existing Remote-SSH helper remain available. Windows is the packaged stable reference and the
behavior that cross-platform changes must not regress.

## Read-only doctor

Run the capability audit with the installed executable or source environment:

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
there is no live window/current owner to resolve. Mac credential/launcher
capability checks describe the accepted Keychain Slack foreground path. Linux
credential/launcher capability checks conservatively remain `PARTIAL` even with
the explicit package installed; they do not certify a general desktop launcher
or secure credential backend. `doctor --linux-bound`
checks an explicit binding and live kernel owners without desktop discovery.

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

These are foreground commands. Use Ctrl-C to stop them. The Linux package also
offers opt-in [node-local systemd setup](LINUX_NODE_SETUP.md), separately from
desktop acceptance. A launchd agent or login-item installer is not provided;
do not run a desktop preview unattended before native validation.

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

The Apple Silicon source path passed real VS Code/Codex and Keychain Slack
acceptance. The macOS 15 ARM64 ZIP additionally passes hosted native package
checks, with Python absent from child execution. The public v0.2.3 package passed
real-user foreground Slack-only acceptance for one manual registration, with
trusted hooks and an explicit system-CA override. Rebinding the thread required
manual state recovery. Version 0.2.5 adds default CA selection and state-preserving
manual rebind migration, with native package regressions; that evidence does
not substitute for new-version device acceptance. Intel Macs and older macOS
versions are not accepted by this build recipe. An unnumbered/shared VS Code
window that cannot be correlated still fails closed as `vscode_live_window_unmapped`.

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
