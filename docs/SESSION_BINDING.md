# Exact-session destination binding

Feishu/Lark and OneBot support exact-session destination binding from 2.1.0.
Slack's existing channel-mention commands are unchanged.

A route belongs to one provider, authenticated app/bot scope, runtime and exact
Codex session. Other sessions, including sessions in the same repository, keep
their own destinations. New sessions use the configured default. Routes are not
synchronized between machines.

## Move Feishu/Lark or QQ notifications

1. Reply/quote **bind** on an active WatchDog notification for the desired session.
2. WatchDog consumes that notification's one-shot ticket and returns a code.
3. Using the same allowlisted account, send the exact code as a new plain-text
   message in the destination conversation containing the same bot. Do this
   within five minutes. Do not quote another message or add mentions or text.
4. WatchDog saves the route and posts a small binding-success hello in the new
   destination. That hello has a fresh reply ticket for the same existing session.

The completion used to initiate binding is not replayed. A pending, expired or
rejected challenge leaves the previous route unchanged. To move again, reply
`bind` on a fresh active notification and complete a new challenge. A later
binding request supersedes the earlier pending challenge for that session.

Reply **unbind** on an active notification to return that provider/session to its
configured default. Other providers' routes are unchanged. These controls never
become Codex prompts. Closed/consumed notifications cannot bind, unbind or wake.

## Provider discovery and permissions

Feishu/Lark uses its authenticated conversation-list and history APIs already used
for first-use pairing. Discovery runs only during an explicit pending challenge.
The app needs group-list access, such as `im:chat:read`, in addition to its existing
message permissions. If Feishu returns `99991672`, enable that permission and
publish/apply the app update; WatchDog pairing and credentials remain unchanged.
It checks a bounded fixed time window before accepting a destination, rejecting
competing matches. The limit is 32 chats, four history pages per chat and four
history requests per polling tick. A larger or inaccessible scan fails closed;
the health record explains the failure. The configured default conversation is
included along with the bot's discoverable groups. No arbitrary user chats are
browsed. Socket mode accepts authenticated events directly and retains its
existing exclusive-listener limitation.

After binding, the existing poller reads the default conversation and rotates
through additional chats with active mapped tickets. Each has a durable cursor.
An allowlisted human must still reply to a known active notification in that same
chat. Top-level ordinary messages, unknown parents and messages in another chat
cannot wake Codex.

OneBot uses the existing authenticated incoming event stream. It does not add
another listener or history scanner. Bound sends require the backend's
`get_msg` response to confirm the sent message, bot identity and exact destination.
Backends that omit destination evidence fail closed after the send; the result
is uncertain and is not automatically repeated. The default unbound send path
keeps its existing behavior. Official API reference:
[OneBot 11 public actions](https://github.com/botuniverse/onebot-11/blob/master/api/public.md).

## Persistence and uncertainty

Routes, challenge hashes, consumption and effect receipts reuse the existing
ReplyTickets SQLite journal. There is no new routing database or credential
migration. Existing settings, tickets and Slack routes are preserved.

Source-ticket consumption and challenge creation are atomic. Challenge consumption
and route persistence are atomic. The initiating human, provider scope, source
notification and exact target are bound to the challenge. Completion cannot be
replayed after restart. The destination hello is claimed before sending; a failed
or ambiguous send is never retried automatically. Verify the destination before
issuing a fresh explicit control on another active notification.

Live acceptance should test bind, destination hello, an ordinary exact-thread
reply, second-session isolation and unbind with the intended provider/backend.
Synthetic transport and state tests do not substitute for that human test.
