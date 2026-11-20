# Codex WatchDog 2.3.10

Restore automatic discovery of existing local VS Code Codex conversations after
the extension changed its routing metadata and began leaving the session cache
empty. WatchDog recognizes the new successful history-attachment marker while
independently verifying the active window and its held native writer. The marker
alone never grants ownership or creates a conversation.

The shared parser preserves legacy owner/follower behavior. Failed, malformed,
conflicting, inactive and stale-generation evidence refuses selection. Windows
retains exact process-generation, same-user, writer-lock and finite-work checks.
An explicitly inactive previous chat does not hide the active conversation.

Saved profiles, secure credentials, routes, registrations, grants, queues and
delivery records are preserved. Existing headless node monitoring and the legacy
Remote-SSH inspection mechanism are unchanged. Windows x64, Linux x64/ARM64 and
macOS ARM64 preview are rebuilt from the same source. Upgrade acceptance uses
previous public version 2.3.9, with existing package, icon, privacy and native
checks. macOS remains a preview with its documented startup prerequisites.
