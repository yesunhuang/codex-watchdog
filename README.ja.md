# Codex WatchDog

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a> | <strong>日本語</strong>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog と Parrot Dog のロゴ" width="320">
</p>

**自分の Agent を持ち込み、慣れたツールのまま、一つの分散チームとして働く。**

*ワークフロー移行を必要としない、超軽量なマルチユーザー／マルチエージェント、クロスマシン／クロスプラットフォーム協調レイヤー。*

Codex WatchDog は、**既存の** VS Code Codex セッションを対象にした軽量な coordination /
control fabric です。人、Agent、マシン、通信インターフェースをつなぎますが、チームに
別の agent platform、runtime、dashboard、DB、scheduler、あるいは必須の中央 manager への
移行を要求しません。

基本思想は単純です。成熟したツールは、それぞれの役割をすでによく解決しています。
GitHub は durable な共同状態と audit history、Slack は team communication、SSH は remote
machine への接続、VS Code は developer workspace、Codex は conversation と execution context
をすでに所有しています。**WatchDog はそれらの小さな代替品を作り直すのではなく、
足りない接続だけを補います。**

各メンバーは、自分の machine、credential、native Codex session、管理スタイルをそのまま
維持できます。共有したい agent だけを既存の GitHub / Slack surface に参加させ、project policy
と reply allowlist が許す場合には、別の teammate や manager がその正確な既存 agent session に
指示できます。Manager は human でも AI でもよく、集中型でも分散型でも構いません。
単一の manager や中央 WatchDog server は必須ではありません。

## ワークフロー概要

### WatchDog：永続的な GitHub ループ

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](images/watchdog_workflow_jp.png)

### Parrot Dog：素早い Slack リレー

![Parrot Dog workflow: Codex asks for help, Slack relays the message, the human replies, and Codex continues](images/parrot_workflow_jp.png)

## このプロジェクトが本当に最適化しているもの

- **ワークフロー移行が不要。** チームがすでに使っている VS Code window、Codex thread、
  repository、SSH host、GitHub project、Slack channel、作業習慣をそのまま使います。
- **極めて軽量な coordination。** コアワークフローのために Redis、DB、orchestration cluster、
  第二の agent runtime、必須の中央 service、中央 AI scheduler は不要です。
- **構成そのものがマルチユーザー。** 各メンバーは自分の agent と machine を所有したまま、
  同じ GitHub / Slack collaboration surface に参加できます。agent を共有しても native session や
  host ownership を手放す必要はありません。
- **クロスプラットフォーム / クロスマシン。** Windows は packaged reference、Linux Remote-SSH
  と detached same-thread handoff は実機検証済み、macOS には native developer-preview path があります。
- **複数 Codex session を一つの runtime に潰さない。** 各 agent は native session、context、
  repository、execution environment をそのまま保持し、WatchDog は exact thread にだけ route します。
- **Manager は optional で distributed にできる。** human、ChatGPT、別の Codex、automation の
  いずれでも manager になれます。単一 manager、複数 manager、direct human-to-agent のいずれでも、
  transport layer を変える必要はありません。
- **非同期でも監査可能。** Git history、progress report、queue receipt、notification receipt、
  machine identity、exact-thread identity により、人と machine が同時に online でなくても継続できます。
- **Mechanism と policy を分離。** WatchDog は誰が誰に命令できるか、team hierarchy、session の
  作業時間、checkpoint rule、merge authority を決めません。それらは GitHub permission、branch、
  各 project の `AGENTS.md` contract に置きます。

## 設計思想

- **成熟した infrastructure を再利用し、作り直さない。** GitHub、Slack、SSH、VS Code、Git、
  Codex、OS はすでに難しい問題を解決しています。WatchDog はそれらをつなぎ、未成熟な複製を
  新たに作らないことを優先します。
- **不足している edge だけを実装する。** identity / routing、exact-thread wakeup、handoff、
  fencing、notification、relay は、周囲の成熟ツールが提供しない場合だけ WatchDog の責務です。
- **仕組みは薄く、できるだけ dumb に。** WatchDog は observe / wake / notify / relay / route を行い、
  その後は邪魔をしません。
- **native agent ownership を保つ。** orchestration を楽にするためだけに replacement chat を作らず、
  既存 Codex session を実行上の authority として維持します。
- **WatchDog observes Git; Codex owns Git.** WatchDog は stage、commit、pull、merge、rebase、reset、
  checkout、push を行いません。
- **GitHub は durable coordination plane。** comment、commit、branch、progress report は terminal、
  machine、manager、restart、time zone を越えて残ります。
- **Slack は shared fast interaction plane。** mature な user、channel、thread、notification、visibility
  boundary をそのまま利用し、team の短い interaction に使います。durable project history の代替ではありません。
- **必須の中央ノードを置かない。** 各 machine / locality が自分の WatchDog と native session を持てます。
  manager も distributed にでき、唯一の authoritative manager session は不要です。
- **Manager-agnostic, Codex-specific。** manager は human、ChatGPT、別 agent、automation のいずれでも構いません。
  execution side は現在 Codex の exact-thread queue、hook、state、completion contract に依存します。
- **observer は複数でも、actor は一つ。** local と detached の WatchDog が共存する場合、side effect を
  競うのではなく ownership / fencing で調整します。
- **Reuse before rebuilding. Integrate before inventing.** file、Git、GitHub、Slack、SSH、lock、既存 CLI を
  優先し、別の platform を作る前に既存のものを組み合わせます。

## Multi-agent project：policy は `AGENTS.md` に置く

WatchDog 自身は Codex A/B/C を割り当てず、2 時間の作業上限を強制せず、誰が他人の agent に
指示できるかも、誰が merge できるかも決めません。そこまで担うと、薄い control fabric が
project-management framework に膨らんでしまうからです。

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

## マルチユーザー協調：Bring Your Own Agents

WatchDog は、team がすべての machine と agent を一つの中央 runtime に登録することを要求しません。
各メンバーは自分の machine 上で自分の WatchDog を動かし、共有したい agent だけを、team がすでに
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
              +------------- GitHub + Slack ------------+
                              shared team surfaces
```

Slack notification には machine identity が含まれるため、team member はどの locality から来た
message かを区別できます。owner が shared Slack surface に設定した dog だけがそこに現れます。
共有したくない agent は、その channel に参加させる必要がありません。project rule と reply allowlist
が許す場合、teammate はその dog の Slack thread に返信し、owner の machine 上の**正確な既存 Codex
session** に message を route できます。

GitHub branch、repository permission、`AGENTS.md` は**誰が何をしてよいか**を定義できます。
WatchDog が提供するのは別の mechanism、つまり**正しい machine / thread を見つけ、安全に message を
届けること**です。policy は transport layer の外に残します。

## 一つの選択肢：集約型 Manager

```text
                         Human / Manager
                               |
                     aggregated control surface
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

manager が GitHub に durable な指示を残すと、WatchDog が update を検知し、正確な既存 thread を
wake します。Codex は実作業と Git を所有し、checkpoint progress report に状態を圧縮します。
WatchDog は結果を通知します。ユーザーが WatchDog の Slack thread に返信すると、Parrot Dog は
allowlisted text を正確な session に返します。

これは一つの topology に過ぎません。manager は AWS 上の persistent Codex session でも、複数の
分散 manager でも、人間が individual agent に直接介入する形でもよく、それらを混ぜても構いません。
WatchDog layer 自体は変わりません。

## プラットフォーム状況

| プラットフォーム | 全体の成熟度 |
| --- | --- |
| Windows x64 | **安定したデスクトップ参照実装**。ネイティブ E2E とパッケージ更新を検証済み |
| Linux ARM64 / x64 | **サーバー／切断後の運用はネイティブ検証とユーザー実運用で確認済み**。ローカルデスクトップはプレビュー |
| macOS Apple Silicon | **一部のネイティブ E2E を検証したプレビュー**。ウィンドウ検出とパッケージに制限あり |

### ワークフロー対応表

| ワークフロー | Windows x64 | Linux ARM64 / x64 | macOS Apple Silicon |
| --- | --- | --- | --- |
| ローカルデスクトップ | ネイティブ E2E 検証済み | プレビュー。ネイティブデスクトップ E2E は未完了 | 所属を特定できるウィンドウ構成で検証済み |
| Remote-SSH | Linux 向け制御側を検証済み | ネイティブ実行側を検証済み | 制御側の検証範囲は限定的 |
| 切断後の同一スレッド引き継ぎ | Linux 実行側を制御 | ネイティブ E2E と Spark の手動実運用が成功 | ネイティブ macOS 向けの引き継ぎ機能なし |
| 自動リモート引き継ぎ | デスクトップ側を検証済み | ネイティブ動作を検証済み。VS Code の再読み込みが必要な場合あり | 完全な引き継ぎ E2E は未検証 |
| 実行可能パッケージ | 起動、アイコン、更新を検証済み | ARM64 Ubuntu、x64 Ubuntu と RHEL 8/glibc 2.28 で検証済み | 開発者プレビュー。実機検証はバージョンごとに必要 |

ユーザーによる Spark の手動切断テストは、問題が観測されずに完了しました。これは
サーバーワークフローのネイティブな証拠であり、Linux デスクトップの検証とは別です。
過去のログがない場合、Windows/Linux はネイティブ writer の PID を確認できますが、
macOS には引き続き解決可能なルーティング情報が必要です。[詳細と制限](docs/PLATFORM_SUPPORT.md)。

## WatchDog が現在できること

- Codex Stop / completion を観測し、final output を notification に利用。
- 新しい context を作らず、**正確な既存 Codex thread** を wake / continue。
- read-only Git remote OID を GitHub update の doorbell として使い、Git mutation は Codex に任せる。
- Slack notification、Outlook/SMTP fallback、local audit trail。
- **Parrot Dog** による allowlisted Slack reply の exact-thread relay。
- Slack routing notification に machine identity を含め、shared channel 内の distributed session を区別可能にする。
- local と VS Code Remote-SSH workspace を発見し、session identity を混同しない。
- 古いルーティングログが失われても、現在のウィンドウの native writer を確認して正確な既存 thread を検出。
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

v0.2.17 以降、稼働中の Linux WatchDog は登録済みの会話を優先して監視し、VS Code の
接続中も完了通知を送ります。ホストの WatchDog が利用できない場合は、ノート PC の
WatchDog が監視を引き継ぎます。

v0.2.18 以降は `linux-auto-run --repo /absolute/repository/path` で、そのワークスペースに
登録された会話を新しい thread も含めて監視できます。複数のリポジトリには `--repo` を
繰り返し指定します。`--thread UUID` は指定した会話だけに監視を限定します。バックエンドが
終了した場合は古い所有権を解放し、同じ thread と稼働中の VS Code writer を維持します。

v0.2.19 以降、Linux は `CODEX_WATCHDOG_SLACK_REPLY_MODE=poll`、既存の bot token /
channel、許可ユーザー一覧で Slack の返信を受信できます。ノート PC の Slack 接続には
依存しません。bot token と channel だけでは通知の送信のみが有効です。
[返信設定と制限](docs/AUTOMATIC_REMOTE_HANDOFF.md)を参照してください。

v0.2.20 以降、Linux の会話が短い猶予期間を経てアイドル状態になり、キューも空であることを
確認すると、書き込みロックを自動で解放します。監視と Slack の返信は有効なままで、VS Code
から同じ会話を再び開けます。`--repo` は登録済みの全 thread を対象にするため、一つの Git 更新が
複数の独立した会話を再開する場合があります。一つだけを監視するには `--thread UUID` を使います。

ホームを共有するクラスタでは、作業に使う各ログインノードでローカル WatchDog を実行し、
実行時の状態をホスト名ごとに分離します。各 WatchDog は自分のノードのネイティブな作業を
検出し、会話を自動移行しません。[ノード設定と既存環境の制限](docs/LINUX_NODE_SETUP.md)
を参照してください。

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

- [手動ビルド・テスト・リリース（GitHub Actions は無効）](docs/MANUAL_RELEASE.md)
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