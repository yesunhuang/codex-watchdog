# Third-party notices

`src/codex_watchdog/_vendor/napcat_sdk/` vendors the MIT-licensed connection
module and exception definitions from `faithleysath/napcat-sdk`, revision
`4d2f72a7e11ff749e1b0d7d8962198db31fc74a8`, copyright (c) 2026 faithleysath.
The original license and exact upstream paths, hashes and local changes are in
that directory. Package inventories include its license and provenance, and the
executable embeds both texts. No NapCatQQ backend code or executable is bundled.

The Python project directly depends on:

- Microsoft Authentication Library for Python (`msal`) — MIT License;
- Microsoft Authentication Extensions for Python (`msal-extensions`) — MIT
  License; and
- Slack Bolt for Python (`slack-bolt`) — MIT License; and
- Feishu/Lark Channel SDK (`lark-channel-sdk`) — MIT and BSD-3-Clause licenses,
  including the SDK's vendored protobuf notice; and
- `websockets` 15.0.1 — BSD-3-Clause License.

Those packages bring transitive dependencies. A packaged executable must be
built from a locked, isolated environment and include a generated dependency
inventory, software bill of materials, and all licenses/notices required by the
resolved versions. This file is not a substitute for that generated inventory.

Git, Codex CLI, VS Code, OpenSSH, PuTTY/Plink and any OneBot/QQ backend are external prerequisites and
are not distributed by this project.

Product names and marks mentioned in documentation or artwork belong to their
respective owners. Their mention does not imply affiliation or endorsement.
