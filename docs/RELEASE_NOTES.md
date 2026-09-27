# Codex WatchDog 2.1.0

Feishu/Lark and OneBot now support exact-session destination binding. Reply `bind`
to an active notification, then post the returned five-minute code as a new
message in the destination bot conversation using the same authorized account.
WatchDog saves the route and sends one reply-enabled hello. `unbind` returns that
provider/session to its default. Other sessions and providers remain independent.
Slack's existing `bind #channel` flow is unchanged.

Feishu/Lark discovers codes through bounded authenticated group-list/history
requests only while a challenge is pending. Group-list permission such as
`im:chat:read` is required in addition to existing messaging access. An existing
app can grant it without repeating WatchDog pairing or changing credentials.
Polling can discover bot groups and the configured default conversation, not
arbitrary new direct chats. QQ uses the existing authenticated OneBot event stream.

Routes, tickets and settings survive upgrades. There is no new routing database
or credential migration. Binding controls never become Codex prompts. Wrong-user,
expired, modified, ambiguous and replayed confirmations cannot redirect a session.
Source identity and one-shot effect receipts remain enforced. Bound sends require
provider-confirmed destination identity; uncertain sends are not blindly retried.
OneBot backends lacking exact destination evidence in `get_msg` fail closed.

The Windows candidate passed a human Feishu binding, destination-hello and
same-session reply test. QQ/OneBot binding has automated coverage only; human QQ
acceptance was explicitly excluded. Native package and source gates cover Windows
x64, Linux x64/ARM64 and macOS ARM64 preview from one revision. Windows upgrade
acceptance uses the immediately previous public 2.0.0 package and verifies that
saved settings and runtime are reused. The macOS package remains a preview.
