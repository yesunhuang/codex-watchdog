# Apple Silicon foreground package preview

This package includes its Python runtime. It runs without a source checkout or
a user-maintained virtual environment. The initial package acceptance target is
macOS 15 on Apple Silicon. Intel Macs and older macOS versions are not accepted
by this recipe. The executable uses ad-hoc signing; it is not Developer ID
signed or notarized. First use may require an attended approval in macOS.

The ZIP is published as a developer-preview asset alongside the Windows beta.
The published v0.2.3 package passed real-user acceptance for one manually
registered workspace, foreground Slack-only operation, trusted hooks, exact
Slack replies, repeated 30-second Stop notifications, and Git wake. That test
required `SSL_CERT_FILE=/etc/ssl/cert.pem` and a manual state recovery after
changing the registered thread. Version 0.2.5 addresses both findings below.
Hosted acceptance of a new build remains separate from testing it with your
actual Keychain, trusted hooks, and VS Code window.

## Install or upgrade

Download the Mac ZIP and `SHA256SUMS.txt` from
[GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases).
Verify the ZIP's checksum against its entry before extracting it.

Stop the foreground WatchDog before replacing it. Extract the ZIP, open a
terminal in that directory, and run:

```sh
./codex-watchdog --version
./codex-watchdog macos-install
```

The default installation is under
`~/Library/Application Support/CodexWatchdog/bin`. The existing schema-1
`slack-relay.json` and current-user Keychain entries are reused in place.
Installation never reads or copies Keychain secrets. It does not send messages.

On the first source-to-package upgrade, the installer finds the existing
WatchDog runtime from the exact saved user hook commands. Conflicting runtimes
or ambiguous hook definitions stop installation. If no previous WatchDog hooks
exist, a fresh runtime is selected under the same Application Support directory.
Use `macos-install --runtime PATH` to explicitly choose a different runtime.
`--install-dir PATH` selects a different stable current-user executable directory.
All paths may contain spaces.

The versioned `macos-launcher.json` profile retains the runtime, install path,
and installed file hashes. Compatible unknown profile fields survive upgrades.
Ordinary upgrades reuse that profile and never need unchanged Slack settings
or credentials entered again. Modified or unrelated files at the destination
are refused. Previous package files and materially changed profile bytes are
saved as content-addressed `.backup-...` files; failed publication restores the
previous installed files. No directory is recursively removed.

## Move hooks to the stable executable

Inspect the proposed definitions, then install them:

```sh
watchdog="$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog"
"$watchdog" macos-hooks
"$watchdog" macos-hooks --install
```

The command preserves other providers' hooks, unknown fields, and the existing
WatchDog grace/poll/timeout choices. It backs up changed hook files beside the
original. It does not edit Codex's trust state.

Moving from source Python to the packaged executable changes the exact hook
command. In Codex **Settings > Hooks**, reload, inspect, and trust the changed
definitions. This first move needs new trust because the executable path
changed. Later package replacements keep the same command/path and preserve
that trust without programmatic reauthorization. Repeated equivalent hook
installation leaves the file unchanged.

## Foreground operation

For an existing 0.2.23.dev2 or later installation at the default path, copy the repository helper
[`Start Codex WatchDog.command`](../packaging/Start%20Codex%20WatchDog.command)
to your Desktop and double-click it. Terminal opens automatically and runs the
installed WatchDog with saved Keychain settings, Slack only, automatic workspace
discovery and a 30-second interval. No command entry is needed. Keep that window
open; press **Control-C** there to stop. This small helper needs no graphical
frontend or additional runtime, and reuses the installed executable after upgrades.
It also selects `--shared-slack-app`, which polls replies to this Mac's own
notifications instead of opening a competing Socket Mode connection. This lets
the Mac share a Slack app with a Windows listener. The existing saved credentials
and channel remain in place. Replies to new notifications use polling; older
Socket Mode mappings and receipts are preserved without importing or replaying them.

Slack notifications identify the machine by its runtime hostname. For a
Remote-SSH thread, they identify the SSH destination separately from the machine
running WatchDog, so a desktop relay is not mislabeled as the thread's host.

From 0.2.23.dev3, a window first discovered after a turn finishes retains that
completion when its Stop hook was recorded during the current monitor process.
This covers the first turn in a newly opened window. Stops already present when
WatchDog starts remain excluded by default; persisted delivery state is reused.

For the accepted Keychain Slack workflow with only your explicit registrations:

```sh
"$HOME/Library/Application Support/CodexWatchdog/bin/watchdog-macos.sh" --slack-only -- --manual-only --interval 300
```

The existing launcher loads credentials only into its child environment.
`--slack-only` removes SMTP and Outlook settings from that child. Use `--dry-run`
to inspect configuration without starting a listener, or add `-- --once` for a
single service cycle. Keep the established Slack workspace/channel allowlist.
`--manual-only` disables automatic workspace discovery. Register the exact
existing repository and thread first using `workspace-add`; see
[manual registration and rebind](SETUP.md#changing-a-manually-registered-thread).
Omit `--manual-only` only when you intend to include automatically discovered
workspaces as well.

Fresh users can use the included `setup-slack-relay-macos.sh --help` for the
normal attended Keychain setup. Existing users do not repeat setup.

The packaged executable also accepts the ordinary CLI commands, including
`doctor`, `run`, and `hook`. When `--runtime` is absent it reuses the saved
runtime. The doctor remains read-only and can return `PARTIAL` or `FAIL` when
VS Code/Codex or a provider capability is unavailable. No launchd service,
network listener, root installation, or automatic startup is added.

## Certificate discovery and connectivity

From v0.2.5, the frozen Mac executable selects `/etc/ssl/cert.pem` when neither
`SSL_CERT_FILE` nor `SSL_CERT_DIR` is set. If that system file is absent, it uses
its bundled certifi roots. A present but invalid system bundle fails closed.
Explicit certificate environment settings, including empty values, are retained.
Selection affects only the process environment; it does not change macOS trust,
Keychain entries, or saved profiles. Certificate and hostname verification stay
enabled. Existing v0.2.3 launch commands with a CA override continue to work.

Check the installed executable's outbound TLS independently of Slack login:

```sh
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" macos-tls-check
```

This explicit check uses Slack's [unauthenticated `api.test` method](https://docs.slack.dev/reference/methods/api.test/).
It needs network access but no credentials, sends no message, and prints only
the CA source and bounded verification result. Normal startup does not run this
probe. When testing automatic selection after upgrading, omit the old
process-local override from that test command; leave deliberate custom trust
settings intact.

## Rollback and removal

To roll back, stop WatchDog and run `macos-install` from the previous usable ZIP.
Schema-1 profile/runtime and provider state are reused. Hash-named backups are
also retained for recovery. Keep the previous ZIP until the replacement passes
your real workflow.

For removal, stop WatchDog, disable/remove its two user hooks in Codex, then
remove only `codex-watchdog`, `watchdog-macos.sh`, and
`setup-slack-relay-macos.sh` from the recorded install directory. Leave the
Application Support profile, routing settings, runtime, backups, and Keychain
entries in place unless you explicitly intend to remove that user state.

## Build and acceptance

Use Apple Silicon Python 3.12, pip 26.0.1, and the exact versions in
`requirements-macos-package.txt`, then install the project with
`--no-deps --no-build-isolation`. Run `scripts/build_macos_package.py` and
`scripts/test_macos_package.py --package PATH_TO_EXTRACTED_PACKAGE`.

The recipe uses [PyInstaller's ARM64 and ad-hoc signing support](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support)
and validates its executable on a [GitHub-hosted ARM64 Mac](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
The package test hides Python and the source checkout from child execution,
uses paths with spaces, checks doctor/privacy and foreground/hook commands,
and tests source-profile reuse and package replacement with an isolated
Keychain fixture. It also verifies outbound Slack TLS using the default system
CA and a manual thread rebind through the installed executable: exact backup,
preserved compatible fields, first and subsequent Stop processing once each,
and unchanged Git state. Source regressions cover interrupted migration,
notification deduplication, and preservation of pending delivery evidence.
Real Mac VS Code/Keychain/hook acceptance remains a separate manual test; this
published package is explicitly a developer preview.

For manual acceptance, retain the existing configuration, install the package,
and verify the reported runtime is reused. Move and trust the exact hook
definitions, run the saved Slack-only foreground workflow, and complete a
normal turn in the existing Codex chat. Verify the 30-second Stop behavior and
an exact-thread reply, then stop/restart WatchDog and repeat. Reinstalling the
same ZIP should report unchanged state and keep the trusted hook bytes intact.
