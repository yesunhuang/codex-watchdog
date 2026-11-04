# Codex WatchDog 2.3.9

Linux automatic interruption recovery can start an already-admitted automatic Git
wake left at the native queue head. Previously, an interrupted turn could leave
the same conversation idle while the waiting Git item prevented both continuation
and idle handback.

Recovery uses the existing owned native client and exact queued submission ID.
The current owner, native idle state, FIFO head, prompt identity, current-node
courier receipt and Git retirement are rechecked. A durable versioned claim is
written before the single native start attempt. Lost or uncertain outcomes are
never retried automatically. Human and foreign queue heads are preserved; no
replacement prompt, session, grant or historical notification replay is added.

The existing opt-in automatic interruption-continuation setting is reused.
Saved profiles, credentials, routes, registrations, native work, queues, tickets
and delivery records are preserved. Windows upgrade acceptance uses the previous
public 2.3.8 package. Windows x64, Linux x64/ARM64 and macOS ARM64 preview are
rebuilt from the same canonical source with the existing package, icon, privacy
and native acceptance checks. Existing preview and startup prerequisites remain.
