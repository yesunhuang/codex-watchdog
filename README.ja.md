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
  <a href="docs/SETUP.md"><img src="https://img.shields.io/badge/supports-SMTP-6c757d" alt="SMTP 対応"></a>
</p>

<p align="center">
  <a href="#quick-start">クイックスタート</a> · <a href="#アーキテクチャと設計思想">アーキテクチャ</a> · <a href="docs/SETUP.md">設定</a> · <a href="https://github.com/yesunhuang/codex-watchdog/releases">リリース</a>
</p>

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

## アーキテクチャと設計思想

- **WatchDog は監視し、Codex が実行します。** WatchDog は読み取り専用の Git 確認で
  更新を検出します。指示の読み取りとコミット、取得、マージを含む Git 操作は Codex が
  担います。WatchDog は別の AI エージェントではありません。
- **GitHub に永続的な指示、Slack に素早い返信。** **Parrot Dog** が WatchDog の
  作成した Slack スレッドから許可ユーザーの返信を正確な既存の Codex 会話へ戻します。
- **各会話の実行所有者は一つ。** Linux ホストの WatchDog が登録済みの会話を優先し、
  利用できない場合にデスクトップ側が補います。所有権や配信状態が不明なら操作を止めます。
- **軽量な連携を保つ。** ネイティブ会話、ファイル、既存ツールを再利用します。
  チームの役割やレビュー規則はプロジェクトの `AGENTS.md` で定めます。

![WatchDog workflow: discuss the task, publish a GitHub comment, detect the update, wake Codex, run the task, and notify the user](images/watchdog_workflow_jp.png)

![Parrot Dog workflow: Codex asks for help, Slack relays the message, the human replies, and Codex continues](images/parrot_workflow_jp.png)

### シングルユーザー・マルチエージェントのワークフロー

一人のユーザーが GitHub と Slack を通じて複数の既存エージェントを連携させられます。
必要に応じて、人間や AI を manager にすることもできます。

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

Codex が Git 操作と進捗報告を担い、WatchDog が更新を検出して正確なスレッドを起動し、
通知を送ります。Parrot Dog は許可ユーザーの Slack 返信を同じスレッドへ戻します。
これは構成の一例であり、中央 manager や WatchDog サーバーは必須ではありません。

## マルチユーザー協調：Bring Your Own Agents

WatchDog は、team がすべての machine と agent を一つの中央 runtime に登録することを要求しません。
各メンバーは自分の machine 上で自分の WatchDog を動かし、共有したい agent だけを、team がすでに
使っている collaboration surface に接続できます。

### マルチユーザー・マルチエージェントのワークフロー

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

## 対応する構成

| プラットフォーム | 推奨ワークフロー | 状況と制限 |
| --- | --- | --- |
| Windows x64 | ローカル VS Code と Linux Remote-SSH 対象のデスクトップ制御 | 安定したデスクトップ参照実装。ネイティブ E2E、パッケージ更新、アイコンを検証済み |
| Linux ARM64 / x64 | ネイティブサーバー実行、Remote-SSH、切断後の続行 | ネイティブサーバー/切断後のテストとユーザー受け入れが完了。ローカルデスクトップはプレビュー |
| macOS Apple Silicon | ネイティブデスクトップ | 正式な安定版デスクトップ。インストール、更新、ロールバック、メッセージングの受け入れが完了。Remote-SSH/引き継ぎの範囲は限定的 |

切断後のネイティブ実行所有者は Linux 専用です。macOS/Windows のネイティブな切断後の
実行所有者は未対応です。Linux パッケージは ARM64 Ubuntu と x64 Ubuntu/RHEL 8 を対象と
します。正確な要件と検証範囲は[プラットフォーム対応](docs/PLATFORM_SUPPORT.md)を参照
してください。制御が戻った後、VS Code の再試行やウィンドウ再読み込みが必要な場合があります。

## Quick Start

**最も簡単な方法：** local Codex にこの repository を scan させ、installation と startup を
step-by-step で案内してもらってください。

メッセージ設定が一切ない状態で初めて対話起動すると、**Slack**、**Feishu/Lark**、
**両方**、**スキップ**を選べます。ボットのグループやチャンネルを選び、
端末の識別ラベルを含む確認フレーズを送信してアカウントを登録します。
新規設定では両サービスとも独立したポーリングを使い、他の端末は稼働を続けられます。
既存設定がある場合や更新時には自動で設定を開始しません。手動では
`codex-watchdog setup-messaging` を実行します。サービスと復旧手順は
[メッセージ設定ガイド](docs/MESSAGING_SETUP.md)を参照してください。

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
`Install and Start Codex WatchDog.command` をダブルクリックしてください（Terminal から
実行することもできます）。インストーラーは実行ファイルを安定したユーザー領域へ配置し、
WatchDog をセットアップします。hook の trust は引き続きユーザー自身が管理します。

hook trust、メッセージング、upgrade、rollback、現在のプラットフォーム上の制約は
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

Linux のフック定義を確認して `"$watchdog" linux-hooks --install` を実行し、Codex で
正確な定義を信頼します。互換性のある更新はプロファイル、ランタイム、通知先設定を保持
します。変更されたフックのコマンドは再確認と信頼が必要です。インストールとロールバックは
[Linux パッケージ](docs/LINUX_PACKAGE.md)を参照してください。

## 現在の使い方

Linux サーバーで、許可したリポジトリの登録済み会話を監視します。

```sh
"$watchdog" linux-auto-run --repo /absolute/repository/path \
  --interval 30 --renew-lease --continue-interrupted
```

`--repo` を繰り返すと対象を追加でき、`--thread UUID` を加えると一つの既存会話に限定
できます。一度の Git 更新で同じリポジトリの複数の登録済み会話が再開される場合があります。
これらの指定は会話を作成・登録しません。

このコマンドは前景で動きます。Remote-SSH/切断後の利用では、SSH 接続に依存しない
永続的なユーザーサービスで WatchDog を動かしてください。上記のオプションは中断後の
自動続行を有効にします。アイドル中も監視は続き、安全な状態で切断後の書き込み所有権を
解放し、新しい作業に合わせて再取得するため、VS Code から同じ会話を開けます。
詳しくは[引き継ぎと起動](docs/AUTOMATIC_REMOTE_HANDOFF.md)を参照してください。

Linux の Slack 返信には `CODEX_WATCHDOG_SLACK_REPLY_MODE=poll` と既存のボットトークン、
チャンネル、許可ユーザー一覧を設定します。ノート PC の Slack 接続には依存しません。
トークンとチャンネルだけでは通知の送信のみが有効です。設定と制限は引き継ぎガイドにあります。

ホームを共有するクラスタでは、**対象となる各ログインノードに一つずつローカルの WatchDog**
を配置します。各ノードは自分のネイティブ会話を検出し、ランタイム状態を分離します。
ノードを変える前に作業を完了または一時停止してください。会話は自動移行しません。
永続起動、ネイティブランタイムの分離、既存環境の制限は[ノード設定](docs/LINUX_NODE_SETUP.md)
を参照してください。

Feishu と国際版 Lark でも通知を送信し、テキスト返信を既存の Codex 会話に転送できます。
`CODEX_WATCHDOG_INTERACTIVE_TRANSPORT=both` を設定すると Slack と同時に利用できます。
[Feishu/Lark 設定ガイド](docs/FEISHU_LARK.md)に従ってリージョンと許可ユーザーを設定してください。
返信は各マシンがポーリングするため、同じアプリとチャットを共有できます。
パッケージには SDK が含まれており、通知先を切り替えても既存の Slack 設定は保持されます。

## ドキュメント

- 設定：[Windows](WINDOWS_PACKAGE.md)、[macOS](docs/MACOS_PACKAGE.md)、
  [Linux](docs/LINUX_PACKAGE.md)、[通知先設定](docs/SETUP.md)。
- Linux：[引き継ぎ](docs/AUTOMATIC_REMOTE_HANDOFF.md)、[ログインノード](docs/LINUX_NODE_SETUP.md)、
  [ソース版の手順](docs/LINUX_SOURCE_WORKFLOW.md)。
- [プラットフォーム状況と doctor](docs/PLATFORM_SUPPORT.md)。
- [アーキテクチャ](doc/architecture.md)と[セキュリティ境界](SECURITY.md)。
- [手動ビルドとリリース](docs/MANUAL_RELEASE.md)、
  [リリース履歴](https://github.com/yesunhuang/codex-watchdog/releases)。
- [任意のマルチエージェント向けプロジェクト契約](examples/AGENTS.multi-agent.md)。
- [素材の出典](ASSETS.md)と[第三者ライセンス](THIRD_PARTY_NOTICES.md)。
- [開発と実運用の履歴](doc/Progress/)。

このプロジェクトは人間が主導し、AI を広く活用しています。メンテナーが製品方針、
受け入れ判断、リリースを担い、ChatGPT が設計とレビューを支援し、OpenAI Codex が
実装、テスト、パッケージ作成の多くを担当します。

Codex WatchDog は独立したコミュニティプロジェクトであり、OpenAI、Microsoft、GitHub、
Slack およびその関連会社とは提携しておらず、公式の推薦も受けていません。