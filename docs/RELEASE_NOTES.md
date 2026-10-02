# Codex WatchDog 2.2.1

Fix automatic writer handback after a Linux interruption has already completed.
The queued-wake receipt matcher now recognizes the exact saved instruction when
Codex appends one terminal newline, and recognizes completion of the declared
interrupted turn when its original start precedes the saved receipt baseline.
This lets the existing idle, queue and writer checks release the backend so the
same conversation can return to VS Code. No second conversation is created.

Matching still requires the exact thread, instruction marker, saved prompt hash
and native turn evidence. Ambiguous, malformed, truncated or replaced transcript
evidence remains unresolved. An uncertain instruction is not replayed and a
receipt is not cleared merely because it is old or the queue is empty. Transcript
matching streams a fixed snapshot instead of loading the whole appended history.

Existing runtimes, credentials, pairings, notification destinations, grants and
reply-ticket journals are preserved. Version 2.2.1 rebuilds Windows x64, Linux
x64, Linux ARM64 and macOS ARM64 preview from one public source revision. Release
gates include native package checks, complete file hashes, privacy checks, the
Windows embedded application icon and upgrade from public 2.2.0.

Session sharing and Slack bot controls from 2.2.0 remain available; see the shipped
session-access and destination-binding guides for syntax and provider permissions.
No additional access is granted by upgrading.

Known limits remain: macOS is an ARM64 preview; unmapped-window and other-window
ownership evidence can limit automatic discovery. A stale previous-boot writer
flag on shared-home Linux can still require evidence-based operator recovery.
The separate Windows follower-view recovery-notification limitation is unchanged.
Human delegation and QQ/OneBot have no new live acceptance in this patch.
Outbound notification success alone does not establish incoming reply acceptance.
