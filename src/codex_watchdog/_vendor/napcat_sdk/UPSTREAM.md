# Upstream connection component

- Project: https://github.com/faithleysath/napcat-sdk
- Revision: `4d2f72a7e11ff749e1b0d7d8962198db31fc74a8`
- Paths: `src/napcat/connection.py`, `src/napcat/exceptions.py`, `LICENSE`
- License: MIT, copyright (c) 2026 faithleysath; full notice in `LICENSE`.
- Original connection SHA-256: `13d9eee59b020707644c1d9e8e6e9f99edb02358e5ded7468ae40992cba70da0`.
- Original exceptions SHA-256: `fa0865a6abd392be716c1a1416819d6fd836183326db9b6b63a37b666ebd88c9`.

Local adaptations: deferred annotations and `asyncio.wait_for` retain Python 3.9
compatibility; standard-library JSON replaces orjson and invalid Unicode is
discarded along with invalid JSON. The existing bounded event queue is reduced
from 500 to 32 entries for WatchDog's control-only workload. UUID echo correlation,
pending-call cleanup, event dispatch and shutdown behavior remain upstream code.
Exceptions and license are unchanged. WatchDog supplies authentication, connection
policy and its own exact-thread admission outside this module.

This is client code, not NapCatQQ backend code. No NapCatQQ source is bundled.
Update by comparing these exact upstream files and keeping local changes explicit;
do not replace the component with an unreviewed moving branch.
