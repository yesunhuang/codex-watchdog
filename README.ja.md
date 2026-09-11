# Codex WatchDog

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a> | <strong>日本語</strong>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog と Parrot Dog のロゴ" width="320">
</p>

**分散実行、統一コントロール。**

Codex WatchDog は、**既存の** VS Code Codex セッションを対象にした極めて軽量な
coordination / control fabric です。マシン、ターミナル、通信インターフェースをまたいで、
正確な既存セッションを監視・wake・route・handoff・relay・notify しますが、
自分自身が別の AI agent や重量級 orchestration runtime になることはありません。

このプロジェクトは単純な watchdog から始まりました。しかし現在の本当の価値は、
その周囲にできたワークフローです。agent はローカル、Remote-SSH、detached host、
複数の VS Code window に分散していても、人間 / manager は GitHub、Slack、
progress report、exact-thread routing を通じて一つの control surface を保てます。

## ワークフロー概要

### WatchDog：永続的な GitHub ループ

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](images/watchdog_workflow_jp.png)

### Parrot Dog：素早い Slack リレー

![Parrot Dog workflow: Codex asks for help, Slack relays the message, the human replies, and Codex continues](images/parrot_workflow_jp.png)

## このプロジェクトが本当に最適化しているもの

- **極めて軽量な coordination。** コアワークフローのために Redis、DB、
  orchestration cluster、第二の agent runtime、中央 AI scheduler は不要です。
- **クロスプラットフォーム / クロスマシン。** Windows は packaged reference、
  Linux Remote-SSH と detached same-thread handoff は実機検証済み、macOS には
  native developer-preview path があります。
- **複数ターミナルでも一つのワークフロー。** VS Code、GitHub、Slack、local shell、
  remote host を組み合わせても、ユーザーが一つの terminal に縛られません。
- **複数 Codex session を一つの runtime に潰さない。** 各 agent は native session、
  context、repository、execution environment をそのまま保持し、WatchDog は exact thread
  にだけ route します。
- **人間 / manager の interface を集約。** GitHub が durable direction、Slack が quick
  interrupt / reply、progress report が agent state の圧縮された manager 向け出力です。
- **非同期でも監査可能。** Git history、progress report、queue receipt、notification receipt、
  exact-thread identity により、人とマシンが同時に online でなくても継続できます。
- **Mechanism と policy を分離。** WatchDog はチーム階層、session の作業時間、checkpoint
  ルール、merge 権限を決めません。それらは各 project の `AGENTS.md` contract に置きます。

## 設計思想

- **仕組みは薄く、できるだけ dumb に。** WatchDog は observe / wake / notify / relay /
  route を行い、その後は邪魔をしません。
- **native agent ownership を保つ。** orchestration を楽にするためだけに replacement chat
  を作らず、既存 Codex session を実行上の authority として維持します。
- **WatchDog observes Git; Codex owns Git.** WatchDog は stage、commit、pull、merge、rebase、
  reset、checkout、push を行いません。
- **GitHub は durable management plane。** comment、commit、progress report は terminal、
  machine、restart を越えて残ります。
- **Slack は quick interrupt / relay plane。** 通知と短い allowlisted reply のための層で、
  durable project history の代替ではありません。
- **Manager-agnostic, Codex-specific。** manager は人間、ChatGPT、別 agent、automation の
  いずれでも構いません。execution side は現在 Codex の exact-thread queue、hook、state、
  completion contract に依存します。
- **observer は複数でも、actor は一つ。** local と detached の WatchDog が共存する場合、
  side effect を競うのではなく ownership / fencing で調整します。
- **仕組みを増やす前に削れる仕組みを削る。** file、Git、lock、既存 CLI を優先します。

## Multi-agent project：policy は `AGENTS.md` に置く

WatchDog 自身は Codex A/B/C を割り当てず、2 時間の作業上限を強制せず、誰が merge
できるかも決めません。そこまで担うと、薄い control fabric が project-management
framework に膨らんでしまうからです。

その代わり、この repository には任意で使える project-contract template があります：

**[`examples/AGENTS.multi-agent.md`](examples/AGENTS.multi-agent.md)**

新しい project の root に `AGENTS.md` としてコピーし、必要に応じて調整してください。
デフォルト template は次の 4 つの軽量ルールを実装します。

1. **単一 session の最大連続 active time：** デフォルト 2 時間。境界に達する前に
   checkpoint / report / stop が必須で、report を書くだけで自動延長されません。
2. **標準 progress report：** 各 checkpoint の終了時に、日付・checkpoint 番号・agent
   suffix を含む report を作成します。例：`progress_2026_09_10_cpx071_codex_b.md`。
3. **先着順の agent name claim：** 最初の agent が `AGENTS.md` registry で Codex A を
   atomic に claim し、その後 B、C… と続きます。Git push が競合で reject された場合は
   refetch して次の空き slot を claim し、他 agent の claim を force-push で上書きしません。
4. **integration authority：** デフォルトで cross-agent conflict と merge を扱えるのは
   Manager と Codex A のみです。他 agent は durable な `## comment` による明示的かつ
   scope 限定の one-shot authorization が必要です。

これは意図的に template に留めています。**Policy は project に属し、WatchDog は
transport / control mechanism だけを提供します。**

## 典型的な集約ワークフロー

```text
                         Human / Manager
                               |
                     unified control surface
                               |
                 +-------------+-------------+
                 |                           |
              GitHub                       Slack
       durable direction / reports    quick notify / reply
                 |                           |
                 +-------------+-------------+
                               |
                           WatchDog
                    observe / wake / route
                    relay / notify / handoff
                               |
           +-------------------+-------------------+
           |                   |                   |
      Codex A (local)    Codex B (Remote-SSH)  Codex C (detached)
       native session       native session        native session
           |                   |                   |
           +---------- checkpoint reports ---------+
                               |
                             GitHub
```

manager が GitHub に durable な指示を残すと、WatchDog が update を検知し、正確な既存
thread を wake します。Codex は実作業と Git を所有し、checkpoint progress report に
状態を圧縮します。WatchDog は結果を通知します。ユーザーが WatchDog の Slack thread に
返信すると、Parrot Dog は allowlisted text を正確な session に返します。

Multi-agent の場合、任意の `AGENTS.md` template が team contract を提供します。
WatchDog はその policy を理解したり enforcement したりする必要はありません。

## プラットフォーム状況

| Platform / path | Support level |
| --- | --- |
| Windows x64 local desktop | **Stable、full E2E verified、packaged reference** |
| Linux Remote-SSH target | **実 remote path verified** |
| Linux explicit same-thread owner | **Ubuntu ARM64 native E2E verified** |
| Linux automatic remote handoff | **Native E2E verified。reattach 時に VS Code reload が必要な場合あり** |
| Linux ARM64 package | **Ubuntu ARM64 native package acceptance 済み** |
| Linux x64 package | **Ubuntu と RHEL 8.10 / glibc 2.28 で acceptance 済み** |
| Linux local desktop | **CI-verified preview。native desktop E2E は未完了** |
| macOS Apple Silicon source workflow | **Native E2E verified、topology 制限あり** |
| macOS 15 ARM64 package | **Developer preview。package acceptance はあり、新版 device E2E は遅れる場合あり** |

すべての topology が同じ成熟度だとは主張しません。詳細は
[platform support and diagnostics](docs/PLATFORM_SUPPORT.md) を参照してください。

## WatchDog が現在できること

- Codex Stop / completion を観測し、final output を notification に利用。
- 新しい context を作らず、**正確な既存 Codex thread** を wake / continue。
- read-only Git remote OID を GitHub update の doorbell として使い、Git mutation は Codex に任せる。
- Slack notification、Outlook/SMTP fallback、local audit trail。
- **Parrot Dog** による allowlisted Slack reply の exact-thread relay。
- local と VS Code Remote-SSH workspace を発見し、session identity を混同しない。
- Remote-SSH detach 後の Linux persistent detached owner による same-thread takeover。
- ownership / fencing により local と detached authority を調整し、stale owner を拒否。
- すべての WatchDog locality で zero-Git-mutation boundary を維持。

## Quick Start

**最も簡単な方法：** local Codex にこの repository を scan させ、installation と startup を
step-by-step で案内してもらってください。

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

### macOS Apple Silicon preview

Releases から ARM64 preview ZIP を取得し、`SHA256SUMS.txt` を確認して展開後：

```sh
./codex-watchdog --version
./codex-watchdog macos-install
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" doctor
```

hook trust、Slack、upgrade、rollback、preview の制約は
[Mac package guide](docs/MACOS_PACKAGE.md) を参照してください。

### Linux ARM64 / x64

対応する Linux ZIP を取得し、`SHA256SUMS.txt` を確認して展開後：

```sh
./codex-watchdog --version
./codex-watchdog linux-install
watchdog="${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog"
"$watchdog" doctor
"$watchdog" linux-hooks
```

Remote-SSH を閉じた後も remote host が same thread を引き継ぐ構成は
[automatic remote handoff](docs/AUTOMATIC_REMOTE_HANDOFF.md) を参照してください。
persistent remote owner は現在 **Linux-specific** であり、macOS / Windows remote owner
との feature parity はまだ claim していません。

> [!IMPORTANT]
> upgrade は compatible な user state をデフォルトで保持する設計です。WatchDog は既存の
> runtime / profile / provider settings を可能な限り再利用し、再設定を通常の upgrade 手順に
> しません。hook executable が変わる場合は review / re-trust が完了するまで旧版を保持してください。

## AI 開発宣言

この project は **human-led の vibe-coding project であり、AI を広範囲に利用しています**。

- **Human maintainer：** product direction、architecture / safety boundary、acceptance、release responsibility。
- **ChatGPT：** architecture discussion / review、failure analysis、instruction / document drafting。
- **OpenAI Codex：** 実装の大部分、tests、diagnostics、packaging、iterative fixes。

Dogfooding history は意図的に inspectable にしています。この project は、自分自身の workflow を
継続的に使いながら開発されており、実際の failure も設計上の evidence として残します。

## その他のドキュメント

- [Multi-agent project contract template](examples/AGENTS.multi-agent.md)
- [Windows package / first-time setup](WINDOWS_PACKAGE.md)
- [Mac package / upgrade / manual testing](docs/MACOS_PACKAGE.md)
- [Linux ARM64/x64 package / upgrade / rollback](docs/LINUX_PACKAGE.md)
- [Linux source same-thread workflow](docs/LINUX_SOURCE_WORKFLOW.md)
- [Automatic remote handoff / persistent startup / safe reattachment](docs/AUTOMATIC_REMOTE_HANDOFF.md)
- [Detailed setup / operations](docs/SETUP.md)
- [Platform support / privacy-safe doctor](docs/PLATFORM_SUPPORT.md)
- [Security boundary](SECURITY.md)
- [Architecture](doc/architecture.md)
- [Asset provenance](ASSETS.md) / [third-party notices](THIRD_PARTY_NOTICES.md)
- [Implementation plan](doc/codex_watchdog_implementation_plan.md)
- [Historical feasibility probe](doc/probe_report.md)
- [Dogfooding / development history](doc/Progress/)

> [!NOTE]
> Codex WatchDog は独立した community project であり、OpenAI、Microsoft、GitHub、Slack、
> およびその関連会社とは提携・公式 endorsement の関係にありません。
