# Codex WatchDog 2.2.0

Share an existing Codex session with another person through Slack, Feishu/Lark or
OneBot. Administrators reply `add @person`, `remove @person` or `access` to an active
WatchDog notification, selecting the person through the native mention picker.
Successful controls return a fresh mapped confirmation for further replies.
Delegates can send ordinary instructions to that exact session; administration and
destination changes remain restricted to configured administrators.

Human administrators can also authorize a Slack bot, such as Dora, with
`bot add @bot`, inspect grants with `bot access`, and revoke with `bot remove @bot`.
The bot replies to an active mapped notification using the single-line format:

```text
!codex 7abff3bc-a4ad-41f2-8237-b3a6672fe941 -- Reply only HELLO_TEST_OK.
```

Generate a new lowercase UUID for each new request; retain it when retransmitting
the same request. The separator is exactly ` -- `. A multiline envelope remains
supported when its actual newline is preserved. Instructions use plain text;
rich-text plain sections must reproduce it exactly. The mapped notification, not
the UUID, selects the session. Ordinary bot chatter and bot administrative controls
are rejected. Durable request admission suppresses replay of an admitted request.

Slack grants require `users:read`. Feishu/Lark grants require contact-user lookup
permission and visibility of the selected person. Grants remain local to the
runtime, provider and exact session, and do not transfer between Slack socket and
polling modes. A grant does not invite a person into a private conversation.

Existing runtime, credentials, pairings, routes, grants and reply tickets are
preserved. Durable access records reuse the reply-ticket journal. Revocation
governs subsequent admission, including replies to older active notifications.
Ambiguous identity, ownership or delivery continues to block action. Uncertain
external sends are not automatically repeated. The session-access guide ships in
every supported package alongside the existing destination-binding guide.

The accepted candidate passed native Windows, Linux x64/ARM64 and macOS ARM64
tests. Slack bot delivery passed live Windows/macOS tests, including same-request
replay rejection on macOS. Human delegation and OneBot delegation have automated
coverage; test the intended person/provider before relying on that access path.
Release CI additionally checks all rebuilt packages and Windows upgrade from the
immediately previous public 2.1.0 release, including the embedded application icon.

Known limits: macOS remains an ARM64 preview; existing unmapped-window and
other-window ownership warnings can limit session discovery. On shared-home Linux
hosts, a stale previous-boot writer flag can still block monitoring handoff and
require evidence-based operator recovery. This release does not add automatic
recovery for that case. Outbound greeting success alone does not establish incoming
human-reply acceptance. QQ/OneBot has no new live acceptance in this release.
