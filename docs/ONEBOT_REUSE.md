# OneBot 11 reuse audit — 2026-09-16, Codex C

Audited the following pinned GitHub revisions before implementing the transport.
License decisions use the repository's actual license text, not package metadata.

| Project | Exact revision | License and decision |
| --- | --- | --- |
| [NapCatQQ](https://github.com/NapNeko/NapCatQQ) | `2049e64260d378e9f1f1f318ae033347d46ab994` | Limited Redistribution License, including noncommercial restrictions. External backend only; no code copied or bundled. Its forward WS server broadcasts events to authenticated connected clients. |
| [openclaw-onebot](https://github.com/xucheng/openclaw-onebot) | `c926fe62db49f6ba66fe67ff0dba240efa8fc9dd` | MIT. Inspected `src/gateway.ts` and `src/outbound.ts`: OpenClaw/Node-bound gateway with WS receive/reconnect and HTTP send. Direct reuse would add a second runtime and unrelated gateway behavior. |
| [napcat-sdk](https://github.com/faithleysath/napcat-sdk) | `4d2f72a7e11ff749e1b0d7d8962198db31fc74a8` | MIT. Selected the small connection module and exception definitions, preserving their license and provenance. |
| [NoneBot OneBot adapter](https://github.com/nonebot/adapter-onebot) | `58bb4874768bad06ba5a60baa4357e6d8a21ce49` | MIT. Maintained Python adapter, but its transport/event lifecycle depends on NoneBot drivers, configuration, models and dispatch. Adding that framework is unnecessary here. |
| [NcatBot](https://github.com/ncatbot/NcatBot) | `6f1ad0a35ca4b6c6b05ec12d1e6e38a01eef42c6` | Actual license is NcatBot Non-Commercial License despite MIT package metadata. Excluded from code reuse. |
| [Hermes OneBot adapter](https://github.com/tiny-ai-ops/hermes-agent-onebot) | `879f0ae03902e11581d4eea9bae4073c7d58ca8e` | MIT. Inspected `gateway/platforms/onebot.py`; connection logic is embedded in Hermes platform/session/media/debounce behavior. The standalone SDK component is smaller to integrate intact. |

## Reuse decision

Vendor `napcat-sdk`'s `connection.py` and unchanged `exceptions.py`, with the
original MIT notice and exact provenance under
`src/codex_watchdog/_vendor/napcat_sdk/`. It supplies the receive loop, UUID action
echo correlation, pending-request cleanup, API/event separation and bounded event
queues. Reuse `websockets` for authenticated connections, framing, ping/pong and
connection retry. WatchDog keeps its own durable dedupe/effect ledgers because
an in-memory SDK cannot establish exact-thread admission across process restarts.

The full current SDK source requires Python >=3.12 and websockets >=16; WatchDog
supports Python >=3.9 and Feishu's SDK requires websockets <16. The published
napcat-sdk 0.6.8 distribution metadata differs from current Git source despite
the same version (published constraints allow websockets >=15.0.1). Avoid a silent
runtime/dependency upgrade or an ambiguous source pin: use the audited small
component, standard JSON, deferred annotations and `asyncio.wait_for` instead.
No QQ protocol implementation, backend launcher, media support, general bot
framework or raw-content diagnostic journal is added.

## Acceptance boundary

This audit does not establish QQ support. Required evidence remains synthetic
WS/action/reconnect/deduplication tests, exact routing across machines, unchanged
Slack/Feishu behavior, a real NapCat/QQ human round trip, and all affected native
package/privacy/previous-release upgrade gates. Existing production installations
remain on the accepted release until those gates and guarded deployment pass.
