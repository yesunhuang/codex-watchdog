# Delegated access to an exact Codex session

Available from 2.2.0. Human delegation supports Slack, Feishu/Lark and OneBot.
Explicit bot instruction access currently supports Slack. Human delegation has
automated acceptance; a live trial with the intended person/provider is required.
Slack bot delivery has passed live Windows/macOS tests, including a macOS
same-request replay rejection. OneBot delegation has automated coverage only.

Provider `allowed_user_ids` are administrators. An administrator can grant another
person permission to send ordinary replies to one exact Codex session through
Slack, Feishu/Lark or OneBot. Each provider and session has its own delegate list,
including when several sessions share a repository.

## Commands

Reply to a recent active WatchDog notification for the intended session. Select
the person with the messaging client's native mention picker. Use a conversation
that the person can access; a grant does not invite them into a private chat.

| Administrator reply | Result |
| --- | --- |
| `add @person` | Grant that person ordinary-reply access to this session. |
| `remove @person` | Revoke that person's delegated access to this session. |
| `access` | Show this session's delegates. |

Each mutation names one person. Administrator authority comes from the provider
configuration and remains independent of session grants. Adding or removing an
administrator through these commands leaves that authority intact.

A successful command consumes its source reply ticket and posts a fresh mapped
notification in the same destination. Reply to that new notification to continue
the exact existing Codex conversation. The command itself does not wake Codex.

Delegates may send ordinary text through active mapped notifications for their
granted sessions. Access commands, `bind`, `unbind` and binding confirmation codes
remain administrator-only. Reserved control text is rejected when unauthorized,
including malformed commands; it is never forwarded as a Codex prompt.

## Provider identity and permissions

Slack uses the immutable user ID in a native mention and verifies the selected
person through [`users.info`](https://docs.slack.dev/reference/methods/users.info/).
New grants need the bot's `users:read` permission. Bot, app, deleted and ambiguous
identities are rejected.

Feishu/Lark uses the mention's app-scoped Open ID. Both socket events and history
polling are supported. New grants verify the person through the authenticated
[contact user endpoint](https://open.feishu.cn/document/server-docs/contact-v3/user/get).
The app needs permission and directory visibility to read that user. An unavailable
or mismatched lookup leaves access unchanged.

OneBot uses the exact account ID in a structured `at` segment. The selected bot
cannot become a delegate. Reply formatting may include an initial mention of that
bot; the command must still name exactly one separate person. Raw CQ strings and
display-name guesses are rejected. OneBot supplies account identity, rather than
a general classification of third-party automated accounts.

Removing a recorded delegate uses the stored identity and exact native mention,
so revocation remains available when a provider lookup is unavailable. Existing
administrator replies keep their current provider permissions and configuration.

## Scope and persistence

Access belongs to the runtime, provider scope and canonical exact session ID.
Moving that session's destination preserves its delegates. Replies still require
a valid active notification actually mapped in the destination being used.
Other sessions and providers remain independent; ACLs are runtime-local.

Grant changes and source-ticket consumption commit together in the existing
reply-ticket journal. Ordinary reply admission reads the current ACL in the same
transaction that claims the ticket. Revocation therefore applies to the next
admission, including tickets created before the revoke.

Restart preserves access and consumed tickets. Duplicate commands cannot repeat
a mutation or confirmation. An uncertain confirmation is not automatically
resent. Malformed saved ACL state blocks the affected access operation and is
preserved for diagnosis. Existing ownership fences and the four-active-ticket
limit continue to apply.

## Explicit Slack bot access

An administrator may separately authorize a Slack bot for one exact existing
session. Use the Slack mention picker in a reply to that session's active mapped
notification:

| Human administrator reply | Result |
| --- | --- |
| `bot add @bot` | Grant the verified bot ordinary instruction access to this session. |
| `bot remove @bot` | Revoke the stored bot principal for this session. |
| `bot access` | Show this session's bot grants. |

These controls require an authenticated, active human administrator. The ordinary
human `add` command rejects bots. Channel membership, human delegate lists and
the configured administrator list do not substitute for a bot grant. Bots cannot
grant access, inspect access lists, bind destinations or act as administrators.
A successful bot control consumes its source ticket and posts one fresh mapped
notification in that destination, without waking Codex.

The bot must reply to an active mapped notification. For senders that convert
Markdown line breaks, use this exact single-line envelope:

```text
!codex 7abff3bc-a4ad-41f2-8237-b3a6672fe941 -- Run the agreed checks and report the result.
```

The separator is exactly one ASCII space, two hyphens and one ASCII space.
Keep the instruction on that same line. The existing multiline form also works
when the sender preserves the actual newline:

```text
!codex 7abff3bc-a4ad-41f2-8237-b3a6672fe941
Run the agreed checks and report the result.
```

Generate a new lowercase canonical UUID for each intentional request. The UUID
is a replay identifier; it does not select a session. The mapped notification
selects the exact existing session. Reuse the same request UUID if retransmitting
that request. A UUID already admitted for this bot cannot be moved to another
message, destination or session within the same transport journal. Ordinary
messages, acknowledgments, quotes, forwarded/edited/deleted messages and
administrative command bodies are rejected. Send plain text; rich-text blocks
are accepted only when their plain sections exactly reproduce that text.
The single-line form uses the same identity, grant, session and replay checks.
It does not normalize mismatched text/blocks or interpret quotes, code or styles
as commands. If a sender changes the envelope or its plain content, inspect the
stored event before retrying; a successful post alone does not prove admission.

### Identity and transport

The verified principal includes workspace, user, bot and app IDs. WatchDog checks
its own token with [`auth.test`](https://docs.slack.dev/reference/methods/auth.test/),
then resolves both its own identity and the selected bot through
[`users.info`](https://docs.slack.dev/reference/methods/users.info/) and
[`bots.info`](https://docs.slack.dev/reference/methods/bots.info/). Both identity
lookups require `users:read`. Missing permissions or conflicting metadata leave
state unchanged. Display names and supplied message text confer no authority.
Removing an existing grant reuses its stored bot principal, while still verifying
the acting human and authenticated WatchDog context.

Socket events must name WatchDog's receiving workspace and app in the outer
[Events API envelope](https://docs.slack.dev/apis/events-api/). The inner sender
user/bot IDs and any supplied app/team/profile fields must agree with the verified
bot. Polling derives its receiving context from the API token and its destination
from the requested mapped conversation. Slack's default self-event filter stays
enabled; WatchDog additionally rejects its own user, bot and app. Bot instructions
can receive the bounded sender receipts described below. WatchDog's own receipts
and ordinary bot acknowledgment chatter remain excluded from instruction input.

### Request receipts

From 2.2.3, an eligible bot instruction can receive a sender receipt in its
originating mapped Slack thread. The receipt echoes the canonical request UUID.
It does not select another session, create a notification mapping or issue a new
reply ticket.

| Sender receipt | Meaning |
| --- | --- |
| Accepted and queued | WatchDog admitted the request and confirmed its queue delivery to the exact existing session. Execution and completion require separate native evidence or a Codex result. |
| Delivery uncertain | WatchDog could not confirm delivery. The UUID remains reserved; do not replay or resend the instruction to force delivery. |
| Duplicate request | This repeat admits no new task. The notice provides no proof that the earlier request executed or completed. |
| Closed mapped ticket | No task was admitted by this message. A later intentional request needs an active mapped notification. |

Closed-ticket feedback requires the current verified bot principal and its
current grant for that exact mapped session. Missing grants, failed identity
checks, unmapped messages and malformed instructions receive no sender feedback.
Duplicate notices are generic and reveal no hidden target information.

Polling tracks only still-active mapped tickets, using the existing last four
tickets per provider and session. Closed or evicted parents stop being polled;
silence for later replies there is expected. No feedback-only lane, historical
backfill or reopened ticket is created to acknowledge a repeat. The initial
valid-ticket acknowledgement remains required. Socket delivery can produce the
bounded rejection or duplicate receipt below when the event is otherwise eligible.

Accepted/queued and delivery-uncertain feedback share one primary receipt limit
per logical UUID. A new physical message repeating that UUID can receive at most
one duplicate notice. Closed-ticket rejection can receive at most one rejection
notice for that UUID. Sender feedback for one UUID is capped across socket and
polling receipts. Each transport keeps its own grants and task-admission
reservations. A retry of the original provider event or physical message
does not repeat its primary receipt or create a duplicate notice.
If the process stops before attempting the primary receipt, an original retry
stays silent: the captured owner capability cannot be recovered for feedback.

Each schema-1 receipt is durably claimed before the API send. Claimed, sent and
uncertain outcomes are permanent and are never automatically retried, including
after restart. A failed send or invalid API response does not change task
admission, dispatch the task again or consume another ticket. Inspect the native
request and delivery records when feedback is missing; absence of a receipt
does not establish that the task was rejected.

Ordinary human queued/uncertain replies retain their existing behavior. Human
`bot add`, `bot remove` and `bot access` controls retain their fresh mapped
confirmations.

### Admission, audit and upgrades

Current bot grants, the complete immutable target, active ticket and logical
request reservation are checked in the same SQLite transaction as ticket claim.
Revocation therefore applies to older active notifications; it cannot cancel an
instruction already admitted. Existing local/remote ownership, handoff and queue
checks still govern dispatch. Human replies and other providers retain their
existing paths. Known bot users remain excluded from the human path after revoke.

Version-1 bot grant, known-user, control and request records use the existing
ReplyTickets journal. They record the exact principal/target and hashes of the
provider event, physical message and request payload, with confirmation and
delivery outcomes. They contain no tokens or raw prompt text. Existing settings,
human ACLs and schema-2 tickets need no migration. Socket and polling journals
remain separate: grants and replay reservations do not transfer between modes.

Bot actions share ordinary event/message receipts. Human access and new route
controls also fence those identities so one provider message cannot become two
actions. New Slack route commands store a versioned physical-message receipt;
older route-command records remain readable with their existing event/source
protections, without retroactively adding physical receipts. Restart and
duplicate delivery cannot repeat an admitted instruction or control confirmation.
An uncertain send is never automatically repeated. Transient metadata/storage
failures before admission return deferred; polling keeps its cursor for retry.
Socket callbacks are already acknowledged by Bolt, so a deferred result does
not itself schedule a retry; the sender may retransmit the same request UUID.

Sender-receipt records are separate from task admission and reuse the existing
journal. For admitted owner-controlled work, the receipt uses the notification
capability captured during that admission. Eligible rejection and duplicate
receipts use journal-authorized provider metadata only; they do not acquire a
native owner, wake Codex or operate a native client. Receipt failure never
reopens a task reservation. Existing grants, ticket schemas, configuration and
credentials remain in place on upgrade.

Inspect only the relevant session's bot records and associated common delivery
receipt when investigating admission. Malformed or colliding state is retained
and rejected. An access confirmation lists that session's bot grants only.

### Live acceptance

Source tests use synthetic provider identities. A live trial separately requires
owner approval for deployment, any provider permission update, and the exact
session grant. Keep the existing configuration and running owner until that
approval is given.

After the approved build and required permissions are available:

1. Reconcile any already posted test request against its native request, ticket
   and queue records before sending another instruction. A successful provider
   post alone does not establish admission.
2. Verify the intended bot's association through authenticated Slack metadata
   and reuse its compatible existing session grant. If a grant is missing, the
   human owner sends `bot add @bot` on a fresh notification for the chosen session
   and checks the resulting mapped confirmation.
3. After the candidate is ready, have that bot send the exact instruction format
   above with a fresh UUID and a recognizable result marker, as a reply to an
   active notification for the exact existing session. Check the echoed UUID and
   originating Slack parent in the sender receipt. Verify one native dispatch
   and the real Codex result separately; a queued receipt alone does not prove
   execution or completion.
4. Intentionally repeat that UUID in one new physical reply. Confirm no new
   task, no changed grant and at most one generic duplicate notice if the message
   is observed while eligible. A polling parent closed by the original request
   is no longer tracked and needs no duplicate notice. Retrying the
   original event or physical message must not repeat its receipt. Check that
   WatchDog's own receipt, ordinary bot chatter, another session and an unmapped
   destination have no instruction effect. Human ordinary replies should
   continue to work.
5. In isolated provider fixtures, fail the acknowledgment send after dispatch,
   then restart and repeat the same input. Confirm one task dispatch, a retained
   claimed/uncertain receipt and no repeated send. Exercise both socket and
   polling paths without manufacturing a live provider outage.
6. When revocation is part of the approved trial, revoke with `bot remove @bot`
   on an active notification, then test a new bot request against an older
   still-active notification. Confirm no delivery or sender feedback.

Do not replay an uncertain instruction with a new UUID merely to force delivery;
first check whether the original instruction reached the existing conversation.
