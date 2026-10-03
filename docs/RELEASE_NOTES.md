# Codex WatchDog 2.2.3

Granted Slack bot requests now receive bounded feedback in their originating
mapped thread, correlated with the request UUID. Confirmed queue delivery,
uncertain delivery, a repeated request and an authorized closed ticket have
distinct meanings. Queue confirmation does not prove execution or completion;
uncertain instructions remain reserved and must not be replayed to force delivery.
Unverified, ungranted, revoked, malformed, own-bot and unmapped input stays silent.

Receipt claims are durable before sending and capped across polling and socket
feedback paths. Provider receipt sends make one attempt. Errors, invalid responses,
concurrent callbacks, crashes and restarts never resend a claimed receipt or
dispatch a task again. Original event retries cannot recover a lost native owner
capability. Receipts do not create sessions, mappings or reply tickets.

Existing human replies, administrator controls, exact-session grants, notification
destinations, credentials and ticket journals are preserved. No new permission,
grant or pairing is required for an upgrade. The English, Chinese and Japanese
READMEs and session-access guide describe the current behavior and its limits.

Windows x64, Linux x64, Linux ARM64 and macOS ARM64 preview are rebuilt from one
canonical public source revision. Acceptance includes native packages, privacy,
complete hashes, the Windows embedded application icon and the immediately
previous public 2.2.2 upgrade path. Fresh installation is a separate gate.

Known limits remain: macOS is an ARM64 preview; unmapped-window and other-window
ownership evidence can limit automatic discovery. Stale previous-boot writer
flags on shared-home Linux can still require evidence-based operator recovery.
The Windows follower-view recovery-notification limitation is unchanged. Native
writer parking alone does not establish editor GUI recovery. This release adds
no new live human-delegation or QQ/OneBot acceptance.
