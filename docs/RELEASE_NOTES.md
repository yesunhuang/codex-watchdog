# Codex WatchDog 2.3.14

Fix a relay race in which a valid reply consumed its one-shot ticket before
finding that the local native-owner fence was busy. Slack human and authorized
bot replies, Feishu/Lark and OneBot now acquire that fence before admission and
hold it through dispatch. A busy unclaimed reply can defer without losing its
poll position; healthy active parents continue making bounded progress.

Fix Git wake eligibility after a genuine conversation reactivation. Activation
no longer records the outstanding upstream tip as attempted without dispatch.
The normal current-tip scheduler and durable exact claims still decide whether
a wake can occur. Previously attempted or uncertain effects remain terminal;
there is no history replay, backlog scan, session replacement or state reset.

Saved profiles, provider credentials, routes, grants, queues and original native
writers remain preserved. All affected Windows x64, Linux x64/ARM64 and macOS
ARM64 preview packages use the same canonical source. Windows upgrade acceptance
uses immediately previous public 2.3.13, with package/privacy, embedded-icon and
native preservation checks retained.
