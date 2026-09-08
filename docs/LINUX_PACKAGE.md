# Linux executable packages

The ARM64 and x64 ZIPs include Python and the application dependencies. Ordinary
use does not require Python, pip, a virtualenv, or this source checkout. Git and
the first-party Codex CLI/VS Code extension remain external prerequisites.

Choose `codex-watchdog-vX.Y.Z-linux-arm64.zip` for `aarch64`, or
`codex-watchdog-vX.Y.Z-linux-x64.zip` for `x86_64`, from
[GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases).
Verify its entry in `SHA256SUMS.txt` before extracting the complete ZIP.

From v0.2.6, the x64 package targets RHEL 8.10 and Ubuntu 22.04 or newer. It is
built natively against glibc 2.28 in a pinned Red Hat UBI 8 container, with the
same Python 3.12.14 and application dependency versions. ARM64 continues to
target Ubuntu 22.04 or newer with glibc 2.35. Alpine/musl is not supported by
these recipes. Linux packages still use the operating system's glibc and ELF
loader. The manifest records the verified minimum glibc required by every
bundled native library and the bootloader. See
[PyInstaller's Linux compatibility guidance](https://pyinstaller.org/en/stable/usage.html#making-gnu-linux-apps-forward-compatible).
An executable temporary directory is needed for the bundled runtime to unpack.

The older v0.2.5 x64 package needs glibc 2.35 and fails before startup on RHEL 8.
Use v0.2.6 or newer there. The compatible package uses the existing installation
and state-preservation procedure below; no host glibc replacement is involved.

The frozen executable selects the current host's Ubuntu or RHEL CA bundle for
HTTPS and SMTP TLS, with bundled certifi roots only when no supported host
bundle exists. Explicit `SSL_CERT_FILE` and `SSL_CERT_DIR` settings are preserved.
This selection affects only the running process; it changes no saved profile,
credential store, or operating-system trust setting.

Packaging does not expand thread discovery. The explicit same-thread workflow
is separate from general Linux desktop discovery, which remains a CI-verified
preview awaiting real desktop E2E. See [platform support](PLATFORM_SUPPORT.md)
for the current acceptance level of each architecture and workflow.

## Install or upgrade

Stop the foreground monitor, or use `linux-release` and wait for the explicit
owner to release its thread, before replacing the executable. In the extracted
directory, run:

```sh
./codex-watchdog --version
./codex-watchdog linux-install
```

The default stable executable is
`${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog`.
The schema-1 `linux-launcher.json` beside `bin` records the stable runtime,
Codex home, installation path, application version, and installed file hashes.
`CODEX_WATCHDOG_LINUX_CONFIG_DIR` can select another absolute current-user data
directory. Paths containing spaces are supported.

The first source-to-package install discovers the runtime from existing
WatchDog hook commands. An existing package profile is reused on later installs.
Conflicting hook runtimes or unsupported profiles block installation. If the
source runtime has no saved hooks, select it explicitly with
`linux-install --runtime PATH`; `--codex-home PATH` selects an existing alternate
Codex home. A fresh install defaults to the data directory's `runtime` child.
`--install-dir PATH` selects another stable executable directory.

Installation preserves existing workspace mappings, journals, bindings, routing,
notification settings, and compatible unknown profile fields. Credentials stay
in their existing boundary. There is no root install, system-wide daemon,
secret export, or automatic provider reconfiguration.

Changed package files and profile bytes receive atomic content-addressed backups.
Unknown or modified destination files are refused. A live foreground owner
blocks replacement, and failed profile publication restores the previous
executable. The previous runtime is never removed.

## Review and trust stable hooks

```sh
watchdog="${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog"
"$watchdog" doctor
"$watchdog" linux-hooks
"$watchdog" linux-hooks --install
```

Review the rendered definitions before installation. Existing unrelated hooks,
grace/poll/timeout settings, and unknown keys are preserved; changed hook bytes
are backed up beside the original. The default production Stop grace is 30
seconds. The installed hook command always uses the stable packaged executable
and saved runtime, without Python or a checkout path.

In Codex **Settings > Hooks**, reload, inspect, and trust the changed definitions.
The initial source-to-executable move needs human trust because the exact hook
command changes. Installation does not alter Codex's trust state. Later package
replacements keep the command/path stable, so unchanged hooks retain their trust.

## Bind and run the existing conversation

Follow the [explicit Linux workflow](LINUX_SOURCE_WORKFLOW.md) to select the
exact existing VS Code conversation ID and repository. Using the stable
executable above, bind that selection and start its foreground owner:

```sh
"$watchdog" linux-bind --workspace project --repo "$repository" --thread "$thread_id"
"$watchdog" doctor --linux-bound
"$watchdog" linux-run
```

The saved profile supplies the same runtime and Codex home on every command.
If Codex is absent from PATH, pass its verified executable to
`linux-run --codex-executable PATH`. Use `linux-status` to inspect bounded,
privacy-safe state. Doctor may report `PARTIAL` for unsupported credentials or
launchers; that is not evidence of general desktop discovery or provider support.

While VS Code owns the exact writer lock, WatchDog waits. After detach it resumes
only that same thread using the first-party stdio App Server. Ambiguous ownership
blocks it; it never starts a replacement conversation or automatically approves
requests. Existing queue receipts and journals survive restart, and uncertain
sends are not retried blindly. Closing VS Code may interrupt an in-flight command;
packaging does not make that command uninterrupted or replay it.

Before reopening the same conversation in VS Code:

```sh
"$watchdog" linux-release
"$watchdog" linux-status
```

Wait for `state: released`. A user-managed persistence command such as
`systemd-run --user` may keep the foreground owner alive after an SSH disconnect;
the source guide describes that optional setup. No service is installed for you.

## Rollback and removal

Keep the previous usable ZIP. To roll back, release/stop WatchDog, extract that
ZIP, and run its `linux-install`; compatible profile/runtime/provider state stays
in place. Content-addressed backups also retain the replaced binary and profile.
To remove the application, first release it and disable its trusted hooks, then
remove only the owned executable at the recorded install path. Keep the profile,
runtime, backups, and credentials unless you intend to remove that user state.

## Build and package acceptance

For x64, use `packaging/linux-rhel8.Dockerfile` from a clean public checkout.
It pins the UBI 8.10 image, verifies the Python 3.12.14 source checksum, and
installs the pinned package dependencies. Build the image with
`docker build --file packaging/linux-rhel8.Dockerfile --tag watchdog-rhel8-builder .`.
Mount that checkout at `/workspace` when running the image, install the project
with `python -m pip install --no-deps --no-build-isolation .`, and run
`python scripts/build_linux_package.py` inside the container. The
[Linux package workflow](../.github/workflows/linux-package.yml) gives the full
build and acceptance commands, including output ownership for hosted runners.

ARM64 retains native Ubuntu 22.04, Python 3.12.14 and pip 26.0.1. Install
`requirements-linux-package.txt`, then the project with
`--no-deps --no-build-isolation`, and run `scripts/build_linux_package.py`.
For either architecture, run `scripts/test_linux_package.py --package
dist/codex-watchdog-vX.Y.Z-linux-ARCH` using an external Python 3.12 harness
interpreter, whose minor version must match the bundled bytecode being inspected.
The packaged child processes run with source Python hidden. x64 must pass the
same executable acceptance on both Ubuntu 22.04 and UBI 8's glibc 2.28.

The manifest records the architecture, version, source commit, build runtime,
minimum glibc requirement, and every shipped file digest.
`THIRD_PARTY_LICENSES/inventory.json` identifies
Python dependencies, CPython, the PyInstaller bootloader, and every collected
native library with its license text and hash. The recipe refuses an unaccounted
native library. Package acceptance checks archive membership, hashes, ELF
architecture, privacy, source-free operation, installation/replacement, stable
hooks, production fixture Stop, and explicit-owner behavior in isolated state.
It also checks default certificate discovery against Slack's credential-free
`api.test` endpoint, which does not send a message.
Fixture App Server checks are distinct from a real user's Codex/VS Code test.

The ARM64 executable additionally passes the package harness on a real Ubuntu
ARM64 machine with glibc 2.39. A separate live check verifies automatic reuse of
the existing source runtime, preserved user state, exact idempotent binding,
foreground replacement/competing-owner refusal, and one deduplicated reply in
the original conversation. The packaged owner waits and releases while VS Code
retains its writer lock. Existing trusted source hooks remain unchanged in that
check. Real-user packaged-hook trust and detached package lifecycle acceptance
are separate from these tests and the already accepted source lifecycle.

The x64 executable also passes isolated package acceptance on a real RHEL 8.10
host with glibc 2.28, including a 30-second production Stop, HTTPS trust,
installation/replacement, manual registration migration, and exact-owner queue,
restart and release fixtures. No real user thread or provider configuration is
used for that test. An external harness may use `--archive-python` to select a
matching Python 3.12 bytecode inspector separately from its runtime-fixture
interpreter. Every packaged child still runs with source Python hidden.
