Version 0.2.4 adds self-contained Linux ARM64 and x64 executable ZIPs alongside
Windows x64 and macOS 15 ARM64 preview packages. All four include Python and
resolved dependency licenses, with archive checksums and automated package
acceptance results attached. Linux targets Ubuntu 22.04 or newer with glibc;
ordinary package use needs no Python installation, pip, virtualenv, or checkout.

The Linux installer reuses the current user's existing hook runtime or saved
package profile, preserves journals, workspace/routing/provider settings and
compatible unknown fields, and backs up replaced files atomically. Hooks use
a stable executable path, including paths with spaces. Review and trust changed
hook commands in Codex; the installer never changes trust or exports credentials.
See [the Linux package guide](https://github.com/yesunhuang/codex-watchdog/blob/main/docs/LINUX_PACKAGE.md)
for installation, the explicit same-thread foreground workflow, and rollback.

Both Linux architectures pass hosted native package acceptance, including
source-free startup, license/privacy checks, production fixture Stop, kernel
locks, exact-thread fixture queue/restart/release, and read-only Git observation.
ARM64 also undergoes native package testing on a real Ubuntu ARM64 machine.
General Linux desktop discovery remains a CI-verified preview; hosted package
checks do not establish real-user desktop E2E or replace human hook trust.

The Apple Silicon package is a developer preview: ad-hoc signed, not Developer
ID signed or notarized. Hosted native package checks pass before publication;
manual acceptance with real user Keychain, trusted hooks, and VS Code remains
separate. See [the Mac package guide](https://github.com/yesunhuang/codex-watchdog/blob/main/docs/MACOS_PACKAGE.md)
for installation and normal attended trust. Existing runtime, routing, and
credentials are reused in place.

Instruction submission now preserves creation order even when the wall clock
ties or moves backward, while retaining compatible existing queue records.
Linux detach/restart/release preserves the original conversation; an interrupted
extension-owned command is not automatically replayed. Windows remains the
packaged reference, with embedded-icon and immediately previous public release
upgrade from the actual v0.2.3 executable required before publishing. Future
version changes on public `main` run all four package gates and publish an
immutable beta from the exact tested commit. The v0.2.3 release is unchanged.
