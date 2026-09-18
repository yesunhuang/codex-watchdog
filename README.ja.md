<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a> | <strong>日本語</strong>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog と Parrot Dog のロゴ" width="160">
</p>

<h1 align="center">Codex WatchDog</h1>

<p align="center">
  <strong>自分の Agent を持ち込み、慣れたツールのまま、一つの分散チームとして働く。</strong><br>
  <em>ワークフロー移行を必要としない、超軽量なマルチユーザー／マルチエージェント、クロスマシン／クロスプラットフォーム協調レイヤー。</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2e7d32" alt="MIT ライセンス"></a>
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/Windows-x64-44627e" alt="Windows x64 対応"></a>
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/Linux-ARM64%20%2F%20x64-44627e" alt="Linux ARM64 と x64 対応"></a>
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/macOS-Apple%20Silicon-44627e" alt="macOS Apple Silicon 対応"></a>
  <br>
  <a href="#アーキテクチャと設計思想"><img src="https://img.shields.io/badge/sessions-native-355c7d" alt="既存のネイティブ Codex セッションを維持"></a>
  <a href="#マルチユーザー協調bring-your-own-agents"><img src="https://img.shields.io/badge/collaboration-multi--user%20%2F%20multi--agent-355c7d" alt="マルチユーザー・マルチエージェント協調"></a>
  <a href="#マルチユーザー協調bring-your-own-agents"><img src="https://img.shields.io/badge/central%20server-not%20required-355c7d" alt="中央サーバーは不要"></a>
  <br>
  <a href="docs/SETUP.md"><img src="https://img.shields.io/badge/supports-Slack-4A154B" alt="Slack 対応"></a>
  <a href="docs/FEISHU_LARK.md"><img src="https://img.shields.io/badge/supports-Feishu-3370FF" alt="Feishu 対応"></a>
  <a href="docs/FEISHU_LARK.md"><img src="https://img.shields.io/badge/supports-Lark-00B96B" alt="Lark 対応"></a>
  <a href="docs/ONEBOT_QQ.md"><img src="https://img.shields.io/badge/supports-OneBot%2011-2679b5" alt="OneBot 11 対応"></a>
  <a href="docs/ONEBOT_QQ.md"><img src="https://img.shields.io/badge/tested-QQ%20via%20NapCat-12b886" alt="NapCat 経由 QQ を検証済み"></a>
  <a href="docs/SETUP.md"><img src="https://img.shields.io/badge/supports-SMTP-6c757d" alt="SMTP 対応"></a>
</p>

<p align="center">
  <a href="#quick-start">クイックスタート</a> · <a href="#メッセージングトランスポート">メッセージング</a> · <a href="#アーキテクチャと設計思想">アーキテクチャ</a> · <a href="https://github.com/yesunhuang/codex-watchdog/releases">リリース</a>
</p>

Codex WatchDog は、**既存の** VS Code Codex セッションを対象にした軽量な coordination /
control fabric です。人、Agent、マシン、通信インターフェースをつなぎますが、チームに
別の Agent platform、runtime、dashboard、DB、scheduler、あるいは必須の中央 manager への
移行を要求しません。

基本思想は単純です。成熟したツールは、それぞれの役割をすでによく解決しています。
GitHub は durable な共同状態と audit history、Slack・Feishu/Lark・QQ は人と Agent の通信、
SSH は remote machine への接続、VS Code は developer workspace、Codex は conversation と
execution context をすでに所有しています。**WatchDog はそれらの小さな代替品を作り直すのではなく、
足りない接続だけを補います。**

各メンバーは、自分の machine、credential、native Codex session、管理スタイルをそのまま維持できます。
共有したい Agent だけを既存の GitHub と messaging surface に参加させ、project policy と reply allowlist
が許す場合には、別の teammate や manager がその**正確な既存 Agent session** に指示できます。
Manager は human でも AI でもよく、集中型でも分散型でも構いません。単一の manager や中央 WatchDog
server は必須ではありません。

## アーキテクチャと設計思想

- **WatchDog は監視し、Codex が実行します。** WatchDog は読み取り専用 Git チェックで更新を検出し、
  指示の読み取り、commit、pull、merge などの Git 操作は Codex が担います。WatchDog は別の AI Agent
  ではありません。
- **GitHub は durable control plane、messaging transport は quick control surface です。**
  Parrot Dog は Slack、Feishu/Lark、OneBot 11 の許可された返信を**正確な既存 Codex conversation**
  に戻します。
- **OneBot は transport boundary であり、QQ の再実装ではありません。** WatchDog は認証付き forward
  WebSocket で generic OneBot 11 を話し、platform 固有の login と protocol maintenance は外部 backend
  が担当します。**NapCat は実機検証済みの QQ reference backend です。WatchDog は NapCat や QQ runtime
  を同梱しません。**
- **各 conversation の execution owner は一つです。** ownership、destination、delivery が曖昧なら、
  推測せずに操作を止めます。
- **軽量な協調を保ちます。** native session、file、既存ツール、成熟した protocol adapter を再利用し、
  team role や review rule は project の `AGENTS.md` に置きます。

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](images/watchdog_workflow_jp.png)

![Parrot Dog workflow: Codex asks for help, a messaging surface relays the message, the human replies, and Codex continues](images/parrot_workflow_jp.png)

> 図では messaging surface の一例として Slack を使っています。Feishu/Lark と OneBot 11 も同じ
> exact-thread relay semantics を使います。

### シングルユーザー・マルチエージェントのワークフロー

```text
                         Human / Manager
                               |
                     aggregated control surface
                               |
                 +-------------+--------------------------+
                 |                                        |
              GitHub                             Messaging transports
       durable direction / reports     Slack / Feishu / Lark / OneBot 11
                 |                                        |
                 +-------------------+--------------------+
                                     |
                                 WatchDog
                       observe / wake / route / relay
                         notify / handoff / fencing
                                     |
           +-------------------------+-------------------------+
           |                         |                         |
      Codex A (local)         Codex B (Remote-SSH)       Codex C (detached)
       native session            native session             native session
           |                         |                         |
           +------------- checkpoint progress reports -------+
                                     |
                                   GitHub
```

Codex が Git と進捗報告を担当し、WatchDog が更新を検出して正確な thread を wake し、通知を送ります。
Parrot Dog は設定済み transport 上の許可された reply を同じ thread に戻します。

## マルチユーザー協調：Bring Your Own Agents

WatchDog は、team がすべての machine と Agent を一つの中央 runtime に登録することを要求しません。
各メンバーは自分の machine 上で自分の WatchDog を動かし、共有したい Agent だけを、team がすでに
使っている collaboration surface に接続できます。

```text
        Alice の machines                        Bob の machines
   +----------------------+                 +----------------------+
   | Windows / Codex A1   |                 | Linux / Codex B1     |
   | HPC / Codex A2       |                 | macOS / Codex B2     |
   +----------+-----------+                 +-----------+----------+
              |                                         |
          Alice's dogs                              Bob's dogs
              |                                         |
              +--------- GitHub + messaging surfaces --+
                              shared team control
```

Notification には machine identity が含まれるため、team member はどの locality から来た message かを
区別できます。owner が共有 surface に設定した dog だけがそこに現れます。project rule と reply allowlist
が許す場合、teammate は返信し、その message を owner の machine 上の**正確な既存 Codex session**へ
route できます。

GitHub branch、repository permission、`AGENTS.md` は**誰が何をしてよいか**を定義できます。
WatchDog が提供するのは別の mechanism、つまり**正しい machine / thread を見つけ、安全に message を
届けること**です。policy は transport layer の外に残します。

## 組み合わせ可能な model-backed worker

WatchDog が管理する境界は**既存の Codex session**であり、Codex が内部で利用する
すべての model や tool ではありません。Codex agent / subagent は、認証済みの
model-backed CLI や tool に境界の明確な subtask を委譲できます。たとえば Claude Code、
DeepSeek-backed CLI、その他の local / remote model interface です。モデルごとに新しい
WatchDog adapter を追加する必要はありません。

```text
Human / Manager
      |
   WatchDog
      |
existing Codex session
      |
Codex subagent
      |
model-backed worker / CLI
(Claude Code / DeepSeek / other)
```

下流 worker は WatchDog の first-class Agent ではありません。WatchDog はその session、
routing、progress report、merge authority を所有せず、呼び出し元の Codex が task scope、
validation、policy、integration の責任を持ち続けます。これは意図した設計です。Codex が
安全に tool を呼び出せて、user の authentication、quota、privacy、repository policy が
許可するなら、その能力は WatchDog の control plane を広げずに managed Codex session の
背後で利用できます。

すぐコピーして使える例として、
[**codex-use-claude** skill template](examples/skills/codex-use-claude/SKILL.md)
を同梱しています。基本の役割分担は単純で、Claude が境界の明確な implementation work を
担当し、Codex が contract の定義、結果の検証、判断、integration authority を保持します。
同じ pattern は DeepSeek や他の model-backed CLI にも適用できます。

これは**composable / indirect compatibility**であり、WatchDog が Claude、DeepSeek、
あるいはすべての下流 model session を native に管理するという意味ではありません。

## メッセージングトランスポート

| Transport | 役割 | 現在の対応状況 |
| --- | --- | --- |
| Slack | 通知 + exact-thread reply relay | 対応済み・production 検証済み |
| Feishu / Lark | 通知 + exact-thread plain-text reply relay | 対応済み・production 検証済み |
| OneBot 11 | generic authenticated forward-WebSocket transport | v1.1.0 から対応 |
| QQ via NapCat | OneBot 11 の reference backend | Windows 上で NapCat 4.18.28 + 公式 QQ の引用返信を実機検証済み |
| その他の OneBot 11 backend | protocol-compatible path | 個別の live test までは best-effort |
| SMTP | outbound notification fallback | 対応済み |

OneBot 11 では、WatchDog は薄い transport と exact-thread routing だけを担当し、chat platform 自体は
外部 backend が担当します。QQ の推奨・検証済み経路は次の通りです。

```text
existing Codex thread
        |
     WatchDog
        |
   OneBot 11 WebSocket
        |
      NapCat
        |
        QQ
```

WatchDog は**返信先 thread を推測しません**。公式 QQ の quoted reply は live acceptance を通過しています。
一方、テストした backend では TIM 3.4.5 が利用可能な quoted-message identity を保持しなかったため、
TIM は control reply 用として受け入れていません。設定、安全境界、acceptance limit は
[OneBot 11 / QQ](docs/ONEBOT_QQ.md) を参照してください。

## 対応する構成

| プラットフォーム | 推奨ワークフロー | 状況と制限 |
| --- | --- | --- |
| Windows x64 | ローカル VS Code と Linux Remote-SSH 対象の desktop control | 安定した desktop reference。native E2E、upgrade、icon、OneBot/QQ を検証済み |
| Linux ARM64 / x64 | native server、Remote-SSH、detached continuation | native server / detached の test と user acceptance が完了。local desktop は best-effort |
| macOS Apple Silicon | native desktop | 正式な stable desktop release。install、upgrade、rollback、messaging acceptance が完了。Remote-SSH / handoff の範囲は限定的 |

**Detached execution ownership は現在 Linux のみ実装済みです。Windows と macOS の native detached
ownership はまだ実装されていません。** Linux package は ARM64 Ubuntu と x64 Ubuntu/RHEL 8 を対象と
します。正確な要件と検証範囲は[プラットフォーム対応](docs/PLATFORM_SUPPORT.md)を参照してください。
Handback 後に VS Code の Retry や window reload が必要な場合があります。

## Quick Start

**最も簡単な方法：** local Codex にこの repository を scan させ、installation と startup を
step-by-step で案内してもらってください。

初回の interactive launch では **Slack**、**Feishu/Lark**、**Both**、**OneBot (QQ)**、**Skip** を
設定できます。既存 profile は互換 upgrade で保持されます。手動設定は
`codex-watchdog setup-messaging` を実行してください。provider ごとの説明は
[メッセージ設定](docs/MESSAGING_SETUP.md)、[Feishu/Lark](docs/FEISHU_LARK.md)、
[OneBot 11 / QQ](docs/ONEBOT_QQ.md) にあります。

### Windows x64

1. [GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases) から最新の
   `codex-watchdog-vX.Y.Z-windows-x64.zip` と `SHA256SUMS.txt` を取得します。
2. checksum を確認し、ZIP 全体を安定した path に展開します。
3. `codex-watchdog.exe` をダブルクリックすると current-user launcher profile を作成 / 再利用し、
   foreground monitor を開始します。
4. native hook を使う場合は先に render / review します：

   ```powershell
   .\codex-watchdog.exe install-user-hooks
   .\codex-watchdog.exe install-user-hooks --install
   ```

   その後 Codex の `/hooks` で exact definition を確認し、自分で trust してください。

### macOS Apple Silicon

Releases から最新の macOS ARM64 ZIP を取得し、`SHA256SUMS.txt` を確認して展開したら、
`Install and Start Codex WatchDog.command` をダブルクリックしてください（Terminal から実行することも
できます）。installer は実行ファイルを安定した current-user 領域へ配置し、WatchDog をセットアップします。
hook trust は引き続きユーザー自身が管理します。

hook trust、messaging、upgrade、rollback、signing / notarization 状態、現在の platform limit は
[Mac package guide](docs/MACOS_PACKAGE.md)を参照してください。

### Linux ARM64 / x64

対応する Linux ZIP を取得し、`SHA256SUMS.txt` を確認して展開したら：

```sh
./codex-watchdog --version
./codex-watchdog linux-install
watchdog="${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog"
"$watchdog" doctor
"$watchdog" linux-hooks
```

Linux hook definition を確認してから `"$watchdog" linux-hooks --install` を実行し、その exact definition を
Codex 側で trust してください。互換 upgrade は既存 profile、runtime、provider setting を保持します。
Install と rollback は [Linux package guide](docs/LINUX_PACKAGE.md) を参照してください。

### OneBot 11 / QQ の最短セットアップ

1. 外部 OneBot 11 backend を起動します。QQ では検証済み reference backend の NapCat を使い、access token
   付き forward WebSocket を有効にします。
2. `codex-watchdog setup-messaging --onebot` を実行し、hidden prompt に WebSocket endpoint と token を入力します。
3. 表示された `PAIR_CODEX_ONEBOT_...` code を対象の direct chat または group に送信します。WatchDog は bot、
   conversation、authorized human を自動で学習するため、QQ user ID の手動検索は不要です。
4. `codex-watchdog onebot-check --connect` を実行します。
5. WatchDog notification を quote / reply すると、対応する exact Codex thread に text を届けられます。
   unknown / ambiguous quotation は拒否されます。

WatchDog 自体は NapCat や QQ を install、bundle、manage しません。

## 現在の使い方

Linux server で approved repository 内の enrolled conversation を監視する例：

```sh
"$watchdog" linux-auto-run --repo /absolute/repository/path \
  --interval 30 --renew-lease --continue-interrupted
```

`--repo` を繰り返して repository を追加し、`--thread UUID` で既存 conversation 一つに絞れます。
これらの filter は新しい conversation を作成したり enroll したりしません。

Remote-SSH / detached use では、SSH connection から独立した persistent user service で WatchDog を動かしてください。
Idle 中も monitoring は続き、安全なときに detached writer を release し、新しい作業時に再取得します。
[handoff and startup](docs/AUTOMATIC_REMOTE_HANDOFF.md) を参照してください。

Shared-home cluster では、**eligible login node ごとに node-local WatchDog を一つ**動かします。各 node は native thread
を検出し、独立した volatile runtime state を保持します。conversation は node 間で自動 migrate しません。
[login-node setup](docs/LINUX_NODE_SETUP.md) を参照してください。

Interactive transport selection は `slack`、`lark`、`both`、`onebot`、`slack+onebot`、`lark+onebot`、`all`
をサポートします。既存の explicit selection は互換 upgrade で保持されます。production service の environment を
変更する前に各 provider guide を確認してください。

## ドキュメント

- Install: [Windows](WINDOWS_PACKAGE.md)、[macOS](docs/MACOS_PACKAGE.md)、
  [Linux](docs/LINUX_PACKAGE.md)、[provider configuration](docs/SETUP.md)。
- Messaging: [Feishu/Lark](docs/FEISHU_LARK.md)、[OneBot 11 / QQ](docs/ONEBOT_QQ.md)、
  [OneBot reuse audit](docs/ONEBOT_REUSE.md)。
- Linux: [handoff](docs/AUTOMATIC_REMOTE_HANDOFF.md)、[login nodes](docs/LINUX_NODE_SETUP.md)、
  [source workflow](docs/LINUX_SOURCE_WORKFLOW.md)。
- [Platform status and doctor](docs/PLATFORM_SUPPORT.md)。
- [Architecture](doc/architecture.md) と [security boundaries](SECURITY.md)。
- [Build and release](docs/MANUAL_RELEASE.md) と
  [release history](https://github.com/yesunhuang/codex-watchdog/releases)。
- [Optional multi-agent project contract](examples/AGENTS.multi-agent.md)。
- [Codex → Claude delegation skill template](examples/skills/codex-use-claude/SKILL.md)。
- [Asset provenance](ASSETS.md) と [third-party notices](THIRD_PARTY_NOTICES.md)。
- [Development / dogfooding history](doc/Progress/)。

これは human-led で、AI の支援を広く活用している project です。maintainer が product direction、acceptance、release を
所有し、ChatGPT が design / review を支援し、OpenAI Codex が implementation、test、packaging の多くを担っています。

Codex WatchDog は独立した community project であり、OpenAI、Anthropic、DeepSeek、Microsoft、GitHub、Slack、
ByteDance、Tencent、NapCat、およびそれらの関連組織とは提携していません。
