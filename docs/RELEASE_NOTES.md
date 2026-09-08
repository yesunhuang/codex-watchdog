Windows x64 and macOS 15 ARM64 packages are attached, together with archive
checksums and automated package acceptance results. Both include Python and
their resolved dependency licenses.

The Apple Silicon package is a developer preview: ad-hoc signed, not Developer
ID signed or notarized. Hosted native package checks pass before publication;
manual acceptance with real user Keychain, trusted hooks, and VS Code remains
separate. See [the Mac package guide](https://github.com/yesunhuang/codex-watchdog/blob/main/docs/MACOS_PACKAGE.md)
for installation and normal attended trust. Existing runtime, routing, and
credentials are reused in place.

This version adds the Mac foreground package and Keychain launcher, accepted
Mac discovery/hook fixes, and the explicit same-thread Linux source owner.
Linux detach/restart/release preserves the original conversation; an interrupted
extension-owned command is not automatically replayed. Windows remains the
packaged reference, with embedded-icon and immediately previous public release
upgrade checks required before publishing.
