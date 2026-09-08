Version 0.2.5 fixes two problems found during real-user testing of the v0.2.3
Apple Silicon package. Windows x64, macOS 15 ARM64 preview, Linux ARM64, and
Linux x64 executable ZIPs continue to include Python, resolved dependency
licenses, checksums, and automated package-acceptance records.

The frozen Mac executable now selects the system CA bundle automatically when
no certificate environment setting is supplied. It uses bundled certifi roots
only when the system bundle is absent, preserves explicit certificate settings,
and keeps certificate and hostname verification enabled. It changes no saved
profile, Keychain entry, or macOS trust setting. The new `macos-tls-check` command
checks Slack connectivity without credentials or sending a message. Native
package acceptance requires that check to pass with Python and certificate
overrides absent. See [the Mac package guide](https://github.com/yesunhuang/codex-watchdog/blob/main/docs/MACOS_PACKAGE.md).

Changing an explicit manual registration to another existing thread in the
same repository no longer leaves the foreground service stuck on incompatible
state. The first normal cycle retains an exact atomic backup, preserves
compatible state and pending Git/delivery evidence, and processes the new
thread's Stop completions since registration without losing the first one to
the previous audit cursor. Restart regressions verify notification deduplication.
Existing queued or uncertain prompts retain their original target; a rebind
never blindly sends them to a different thread. Automatic discovery changes,
different repository identities, and invalid/future state still fail closed.
See [manual thread rebind](https://github.com/yesunhuang/codex-watchdog/blob/main/docs/SETUP.md#changing-a-manually-registered-thread).

The earlier v0.2.3 Mac acceptance passed the foreground Slack-only workflow with
one manual workspace, human-trusted hooks, exact Slack replies, repeated
30-second Stop notifications, and Git wake. It used an explicit CA override
and manual state recovery. That bounded real-user evidence remains distinct
from the new version's hosted native package tests. The Mac asset is still
ad-hoc signed and not notarized; Intel, background-service, Outlook, and general
multi-window acceptance are not added by this release.

English, Chinese, and Japanese READMEs remain together at the repository root,
with matching Quick Start structure, commands, and current support information.
Existing runtime, routing, provider credentials, and trusted stable hook paths
are reused. Release gates retain the full three-OS source matrix, all four
native packages, complete-history secret scan, embedded Windows icon check,
and Windows upgrade from the actual immediately previous public v0.2.4 package.
Future version changes on public `main` automatically publish only after these
gates pass. Published v0.2.3 and v0.2.4 tags and assets remain unchanged.
