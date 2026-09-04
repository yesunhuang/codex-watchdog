# Progress report - 2026-09-03 checkpoint 15 Slack reply relay

Final implementation snapshot: 2026-09-03 CDT

Scope: add a narrow Slack walkie-talkie path from a WatchDog notification back
to the already existing exact VS Code Codex thread, without creating another
Codex session or changing the established local, GPU lab, MFA HPC, and GitHub
workflows.

## 1. Architecture implemented

When the optional Slack relay is fully configured, WatchDog sends Slack
notifications through `chat.postMessage` rather than the one-way incoming
webhook. The successful response supplies the authoritative Slack channel ID
and message timestamp. WatchDog stores that Slack-thread identity with the
already resolved workspace, exact Codex thread UUID, and the minimum locality
routing required by the existing queue adapter.

The Socket Mode listener accepts only ordinary thread replies that satisfy all
of these conditions:

- the message is in the one configured Slack channel;
- the sender is one of the explicitly configured Slack member IDs;
- the parent is a notification thread that WatchDog itself mapped;
- the event is neither a bot/subtype event nor a previously claimed Slack
  event; and
- the stored Codex target is still a valid exact local or Remote-SSH route.

Accepted text is not parsed or interpreted by WatchDog. It is handed
essentially verbatim to the existing `QueueWakeDispatcher` for a local VS Code
thread or to the existing remote probe with that remote workspace's exact
expected session ID. No shell command, Git command, model call, or new Codex
session is created by the Slack code.

A positive first-party queue acknowledgement produces the Slack reply
`Queued for the exact existing Codex thread.` An uncertain local or remote
delivery is reported as uncertain and is never blindly resent. Event claims
are durable before dispatch, so Slack retries or process recovery cannot turn
an ambiguous delivery into a duplicate instruction.

## 2. Persistence and transport safety

`.codex-watchdog/slack/relay-state.json` contains Slack channel/thread routing,
workspace/thread identifiers, timestamps, event and text hashes, character
counts, and delivery states. It never stores Slack tokens or reply text. The
state uses atomic replacement and the existing cross-process file lock.

Only one Socket Mode listener may own a runtime. A long-held advisory lock is
acquired before Slack Bolt starts and released on clean shutdown or failed
startup. This prevents two foreground WatchDog processes using the same runtime
from silently splitting events. Operators must also avoid launching a second
runtime with the same Slack app token.

The existing incoming webhook remains supported unchanged. If both modes are
configured and the bot API cannot post, WatchDog falls back to the webhook and
labels that message as not reply-mapped. SMTP, opt-in `msg.exe`, local audit,
and notification deduplication retain their existing order and behavior.

## 3. Configuration and operator flow

The relay uses four environment values:

- `CODEX_WATCHDOG_SLACK_BOT_TOKEN` (`xoxb-...`);
- `CODEX_WATCHDOG_SLACK_APP_TOKEN` (`xapp-...`);
- `CODEX_WATCHDOG_SLACK_CHANNEL_ID`; and
- `CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS`.

`setup-slack-relay.ps1` prompts securely for the two tokens, validates the IDs,
and saves the secrets as current-user Windows DPAPI CLIXML. It refuses to
replace an existing configuration unless `-Force` is explicit. `watchdog.ps1`
restores those files, validates the complete combination, and prints only a
safe `slack_reply` source status. Partial configurations fail closed.

The launcher now supports:

```powershell
.\watchdog.ps1 -SlackRelayTest -SlackRelayWorkspace LocalCodexWatchDog -NoDuo
```

This posts a uniquely tagged, reply-enabled notification for one exactly
auto-discovered local workspace without requiring the operator to enter a
Codex thread ID. The normal long-running `watchdog.ps1` process must remain open
as the sole Socket Mode listener while the test reply is sent.

## 4. Permission approval boundary

Direct Slack Approve/Deny controls were investigated but deliberately not
implemented. Local evidence on the installed Windows Codex build shows:

- `codex queue` accepts an exact durable thread and text but exposes no pending
  approval/request selector; and
- `codex app-server daemon` lifecycle control reports that it is supported only
  on Unix platforms.

The reviewed `wabol/codex-bot` approval design works because that program owns
its Codex app-server process and its pending request IDs. WatchDog does not own
the VS Code app-server. Taking over VS Code private IPC merely to answer an
approval would violate the exact-session ownership boundary and was rejected.
The safe supported behavior is text relay, including replies such as
`permission fixed; retry` after the operator completes a local UAC, Duo, or
other unavoidable approval action.

## 5. Verification

Automated verification completed after implementation:

- full suite: 279 collected, 278 passed, one existing intentional skip;
- focused notification, Slack relay, service, CLI, and launcher tests: passed;
- `python -m compileall -q src tools tests`: passed;
- `python -m black --check src tests`: all 36 files unchanged;
- `git diff --check`: passed;
- both PowerShell files parsed without syntax errors; and
- Slack Bolt 1.30.0 plus Slack SDK 3.44.1 imported successfully in the active
  Python 3.9 environment.

Coverage includes allowlisted verbatim local delivery, exact-session remote
delivery, unknown-thread and unauthorized rejection, bot filtering, persistent
duplicate suppression, uncertain no-resend behavior, mapping without plaintext
message retention, bot-post mapping, incomplete configuration, foreground
listener lifecycle, single-listener locking, auto-discovered relay-test
selection, and privacy-safe launcher validation.

The interactive setup helper was also exercised against an isolated local test
directory with nonsecret fake tokens. It wrote DPAPI credentials, and a fresh
launcher process restored them while printing only
`slack_reply: encrypted_store`.

## 6. Initial live-acceptance state

The ordinary end-to-end Slack reply acceptance has not yet been claimed. A
privacy-safe dry run of the real current-user configuration reported the
existing incoming webhook as `encrypted_store` and the new reply relay as
`not_configured`. No bot/app tokens were present. Slack Bolt was installed, but
there is intentionally no way to derive Socket Mode credentials from an
incoming webhook. Browser control also failed to start in this environment, so
WatchDog did not edit the operator's signed-in Slack app or expose/create tokens
through an unsafe workaround.

The remaining live procedure is therefore operational rather than an
implementation gap:

1. enable Socket Mode and the documented minimum bot scopes/events in the
   existing Slack app;
2. run `.\setup-slack-relay.ps1` and enter the Slack-provided bot token, app
   token, channel ID, and operator member ID;
3. restart one `watchdog.ps1` listener;
4. run the one-command relay test from a second PowerShell and reply with a
   unique completion instruction; and
5. confirm that the exact existing VS Code thread receives it and its normal
   Stop notification returns to Slack.

No production Slack credential, Slack message, external repository, Git state,
or VS Code private transport was modified during the deferred live portion.

## 7. Reuse evidence

Implementation choices were checked against:

- Slack Bolt Python Socket Mode and message-sending documentation:
  https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/ and
  https://docs.slack.dev/tools/bolt-python/concepts/message-sending/;
- Slack `chat.postMessage` response behavior:
  https://docs.slack.dev/reference/methods/chat.postMessage;
- `wabol/codex-bot`, specifically its Socket Mode, allowlist,
  Slack-thread/session correlation, and app-server-owned approval model:
  https://github.com/wabol/codex-bot; and
- `pkemp-ai/slack-local-claude-code-wrapper`, specifically its Bolt Socket Mode
  and authorized-operator patterns:
  https://github.com/pkemp-ai/slack-local-claude-code-wrapper.

No code was vendored from either external bot.

## 8. Live follow-up diagnosis

The first operator reply exposed a configuration ambiguity that the original
validation accepted: the saved destination began with `D`, identifying a
one-to-one direct-message conversation rather than the channel where the bot
was invited. The bot and app tokens authenticated successfully, but Slack
returned `channel_not_found`; the outgoing message therefore arrived through
the existing webhook fallback and never created a reply mapping.

The relay now accepts only bot-addressable channel identifiers beginning with
`C` or `G`. The interactive setup and launcher apply the same restriction. The
relay-test command also checks the durable mapping after notification delivery,
prints `relay_mapping: missing` when a webhook fallback cannot be replied to,
and exits nonzero instead of presenting that case as a successful relay test.

After this correction, the full suite collected 281 tests: 280 passed and the
one existing intentional test was skipped. Live acceptance remains pending
only replacement of the nonsecret saved destination with the invited channel's
actual `C`/`G` identifier.

## 9. End-to-end live acceptance

The operator supplied the invited public channel's actual `C`-format ID. A
read-only `conversations.history` check then returned `ok: true`, proving that
the configured bot could access the intended channel. A fresh
`-SlackRelayTest` subsequently reported `status: sent` and
`relay_mapping: created`; it did not use the legacy webhook fallback.

The operator replied `SLACK_RELAY_LIVE_TEST_PASSED` inside that newly created
Slack thread. The foreground Socket Mode listener correlated the reply to this
exact existing VS Code Codex thread and enqueued it successfully. Codex resumed
here with the exact instruction under a `slack:` WatchDog wake ID. The durable
relay receipt contained one mapped thread and one event with `state: delivered`
and `delivery_status: enqueued`; a privacy check confirmed that the receipt did
not retain the plaintext reply.

This completes the required ordinary-reply live acceptance. The same default
WatchDog listener remains running for normal polling and Slack replies.

## comment

### Checkpoint 16 — public-release preflight audit (audit only; do not publish yet)

The WatchDog is now working well enough that the next goal is a possible public GitHub repository and later a packaged Windows executable. Before either action, perform a **full privacy/security/public-release audit of the current repository and its complete reachable Git history**.

This checkpoint is audit-first. **Do not change repository visibility, create a public mirror, publish a release/executable, rewrite Git history, force-push, revoke/rotate credentials, or broadly delete historical material without review.** If an active credential is discovered, stop and report the credential type and required rotation action immediately without reproducing the secret.

Audit at least the following surfaces:

1. **Current tracked tree and all reachable historical blobs/commits/refs.** Do not limit the review to current files. Inspect commit diffs/history and objects for anything that was committed and later deleted.
2. **Secrets/credentials:** Slack webhook URLs, `xoxb-`/`xapp-` tokens, GitHub tokens, OAuth material, SMTP credentials, private SSH keys, passwords, auth cookies/tokens, DPAPI/CLIXML credential files or payloads, `.env` contents, or any equivalent secret. Use a reputable secret scanner if conveniently available, but also perform targeted repository/history searches; do not trust one scanner alone. Never echo a discovered secret into the progress report.
3. **Privacy/internal metadata:** personal email/address-like data, usernames/home paths, machine names, SSH aliases/hosts, HPC provider/GPU lab identifiers, Slack channel/member IDs, exact VS Code workspace/thread/session UUIDs, private repository/project names, private remote URLs, local filesystem paths, or other identifiers that are not credentials but may be undesirable in a public project.
4. **Runtime/debug leakage:** logs, rollouts, SQLite/DB files, transient Stop output, state/queue files, saved notification payloads, credential stores, screenshots, test artifacts, backups, or generated files that should be gitignored rather than public.
5. **Commit metadata and messages:** author/committer emails and commit messages can leak information even if the file tree is clean. Classify anything requiring user choice separately from actual secrets.
6. **Documentation/progress reports.** The preference is to **keep the progress reports if they can be made public safely**, because they document the dogfooding/architecture evolution. Audit every report. Recommend targeted redaction/generalization of private metadata rather than deleting the reports wholesale. Distinguish clearly between (a) secret requiring purge/rotation, (b) privacy metadata worth sanitizing, and (c) harmless technical history worth retaining.
7. **Public-release/IP/license hygiene:** identify third-party code/assets/binaries or branding that would need attribution, license files, redistribution review, or replacement before a public repo/exe release. In particular, review the current mascot/icon assets as public project branding rather than assuming they are automatically safe to redistribute. Also identify dependencies whose licenses/notice obligations matter for a packaged executable.
8. **Packaging boundary:** inspect what a future Windows executable/package must *not* embed (tokens, local config, usernames, machine-specific paths, saved DPAPI material, runtime state). Do not build the executable yet; just report packaging risks and a minimal recommended release layout.

For every finding, classify severity and remediation:

- **BLOCKER — secret/security:** rotate/revoke if applicable, then history purge may be required before publication.
- **PRIVACY — sanitize/user decision:** no credential compromise, but redact/generalize before publication if appropriate.
- **RELEASE HYGIENE — fix before public release:** license, asset, packaging, or documentation cleanup.
- **SAFE / intentional public information.**

If history cleanup is needed, propose the narrowest plan (for example `git filter-repo` with exact paths/replacements), but **do not execute history rewriting yet**. Remember that deleting a file in HEAD is insufficient if the sensitive blob remains in Git history.

Produce a concise new progress report with:

- an executive go/no-go assessment for making the repository public **as it currently exists**;
- findings grouped by the four classes above, with exact file/path/commit references where safe;
- a separate assessment of whether the existing progress reports can remain public and what sanitization they need;
- any credentials that require rotation described only by type/location, never by value;
- a proposed minimal cleanup sequence before visibility change;
- a separate, later plan for Windows executable packaging only after the repository is cleared for publication.

Run appropriate read-only audits/tests within the normal active-work limit. Preserve the existing WatchDog architecture and working runtime; this checkpoint is not an excuse to redesign features. Publish the report and ordinary Codex-owned Git commit/push, then stop for review.