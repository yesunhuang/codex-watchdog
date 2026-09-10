# Codex WatchDog

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a> | <strong>日本語</strong>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog と Parrot Dog のロゴ" width="320">
</p>

既存の VS Code Codex セッションを見守り、必要なときに起こし、メッセージを
中継し、結果を通知する、軽量で決定論的なウォッチドッグです。新しい AI
エージェントとして振る舞うことはありません。

## ワークフロー早見図

### WatchDog：GitHub を使った永続的なループ

![WatchDog の流れ：タスクを相談し、GitHub にコメントを書き、更新を検知して Codex を起こし、実行結果を通知する](images/watchdog_workflow_jp.png)

### Parrot Dog：Slack を使った素早い双方向中継

![Parrot Dog の流れ：Codex が助けを求め、Slack でメッセージを中継し、人が返信すると Codex が作業を続ける](images/parrot_workflow_jp.png)

## 設計思想

- **軽量で決定論的。** 小さく明示的で、確認・テストしやすい仕組みだけを使います。
- **人間をループの中心に置き、手間を減らす。** 重要な判断は人が行い、定型的な
  監視と中継だけを WatchDog に任せます。
- **WatchDog observes Git; Codex owns Git.** WatchDog は Git を観察するだけです。
  ステージ、コミット、pull、merge、rebase、reset、checkout、push は Codex が
  担当します。
- **GitHub は永続的な管理・レビュープレーン。** コメント、コミット、進捗報告に
  よって、端末や時間をまたいで文脈を残します。
- **管理側には依存せず、実行側は現状 Codex 固有です。** GitHub に永続的な指示を
  書けるなら、管理側は人、ChatGPT、別のエージェント、あるいは自動化スクリプトでも
  構いません。一方、現在の実行側は Codex の正確なスレッドキュー、Hooks、
  rollout／完了イベントの仕組みに依存しています。
- **Slack は素早く認証された中継プレーン。** 通知と許可された短い返信に使い、
  永続的なプロジェクト履歴の代わりにはしません。
- **余分な AI エージェントや過剰なオーケストレーションを増やさない。** WatchDog
  は正確な既存 Codex スレッドへ証拠と指示を届け、推論と作業は Codex が行います。

## プラットフォーム対応状況

| プラットフォーム/経路 | 対応レベル |
| --- | --- |
| Windows x64 ローカルデスクトップ | **安定版・完全 E2E 検証済みのパッケージ基準実装** |
| Linux Remote-SSH ターゲット | **実機のリモート経路を検証済み** |
| Linux の明示的な同一スレッド・ソース実行 | **Ubuntu ARM64 実機 E2E 検証済み** |
| Linux リモート会話の自動引き継ぎ | **Ubuntu ARM64 のソース実機 E2E 検証済み。再接続時に VS Code の再読み込みが必要な場合あり** |
| Linux ARM64 実行ファイルパッケージ | **Ubuntu ARM64 実機でパッケージ検証済み。明示的な同一スレッド手順に対応** |
| Linux x64 実行ファイルパッケージ | **Ubuntu と RHEL 8.10 でネイティブパッケージ検証済み。実ユーザーのデスクトップ E2E は未完了** |
| Linux ローカルデスクトップ | **CI 検証済みプレビュー。実機デスクトップ E2E は未完了** |
| macOS Apple Silicon ソース実行 | **実機 E2E 検証済み。ウィンドウ構成の制限あり** |
| macOS 15 ARM64 パッケージ | **開発者プレビュー。v0.2.3 で限定的な実ユーザー E2E を検証済み。v0.2.5 以降は CA 回避設定不要。新バージョンの実機 E2E は未完了** |

Linux/macOS では、POSIX ロックとストレージ、標準 VS Code
パス、ネイティブ `code --status`、Codex 実行ファイル検出を共有します。
現時点ではフォアグラウンドのプレビューです。Apple Silicon と両 Linux アーキテクチャに
ランタイム同梱の ZIP があります。Linux では
[明示的なソース実行手順](docs/LINUX_SOURCE_WORKFLOW.md)と独立した Remote-SSH
ヘルパー経路も利用できます。バックグラウンドサービスのインストーラーは含まれません。
`codex-watchdog doctor`
で読み取り専用診断を実行し、`codex-watchdog doctor --export report.json`
でプライバシー保護済みの診断 JSON を作成できます。対応レベルの定義と実機検証項目は
[プラットフォーム対応と診断](docs/PLATFORM_SUPPORT.md)を参照してください。

## 主な機能

- Codex の Stop／完了イベントを観察し、最終出力を通知できます。
- 新しい会話を作らず、正確な既存 Codex スレッドを継続または再開します。
- 読み取り専用のリモート Git OID 確認を GitHub 更新の呼び鈴として使い、同期は
  Codex に任せます。
- Slack 通知、Outlook／SMTP フォールバック、ローカル監査記録に対応します。
- ローカルと VS Code Remote-SSH の対象ワークスペースを検出します。
- 以前に読み込んだ会話が明示的に非アクティブな場合、現在のアクティブな会話を追跡し、追跡異常の理由を表示します。
- WatchDog が作成した Slack スレッドから、許可された返信だけを Codex に戻す
  **Parrot Dog** 中継を任意で利用できます。
- どの実行環境でも WatchDog による Git 変更を禁止します。

## クイックスタート

**いちばん簡単な導入方法：** お使いの Codex にこのリポジトリをスキャンさせ、インストールから起動まで順番に案内してもらってください。

### Windows x64 ベータ

1. [GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases) から
   `codex-watchdog-vX.Y.Z-windows-x64.zip` と `SHA256SUMS.txt` をダウンロードし、
   チェックサムを確認して ZIP 全体を展開します。Python は不要です。
2. ネイティブフックを導入する場合は、空白を含まない固定パスへ展開してください。
   Git、Codex を導入した VS Code、Codex CLI、Windows OpenSSH は別途必要です。
3. `codex-watchdog.exe` をダブルクリックします。バージョン付きのユーザー別
   起動プロファイルが作成または再利用され、フォアグラウンド監視が始まります。
   停止するには Ctrl-C を押すか、コンソールを閉じます。
4. 確認や詳細オプションには引き続き PowerShell を利用できます。

   ```powershell
   .\codex-watchdog.exe --version
   .\watchdog.ps1 -DryRun
   ```

5. ネイティブ Codex フックを生成して内容を確認し、安全な方法でインストールします。

   ```powershell
   .\codex-watchdog.exe install-user-hooks
   .\codex-watchdog.exe install-user-hooks --install
   ```

   異なる `hooks.json` がすでに存在する場合、インストーラーは上書きしません。
   詳細ガイドに従って手動で統合してください。その後 Codex で `/hooks` を開き、
   正確な定義を確認して手動で信頼します。

   Stop フックの既定の猶予時間は 30 秒です。より長い待機は明示的な
   オプトインであり、通常の完了通知が 10 分遅れることはありません。

> [!IMPORTANT]
> アップグレード時には、互換性のある起動プロファイル、既存の WatchDog フックが
> 参照するランタイム、または隣接する最新の旧バージョンのランタイムが自動的に
> 再利用されます。Slack、Outlook、Duo、OAuth、ワークスペース、通知設定のコピーや
> 再入力は不要です。新しいフックを確認・置換し、Codex で信頼するまでは旧バージョン
> のディレクトリを残してください。

通知、Slack 返信中継、Outlook OAuth、Remote-SSH、Duo フォールバック、ソース
インストールは必要な場合だけ設定します。詳しくは
[Windows パッケージガイド](WINDOWS_PACKAGE.md)と
[詳細なセットアップ・運用ガイド](docs/SETUP.md)を参照してください。

### macOS Apple Silicon 開発者プレビュー

[GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases) から
`codex-watchdog-vX.Y.Z-macos-arm64-preview.zip` をダウンロードし、
`SHA256SUMS.txt` の該当チェックサムを確認して展開します。Python を同梱しており、
対象は Apple Silicon 上の macOS 15 です。ad-hoc 署名のみで、公証は未実施です。

更新前に実行中の WatchDog を停止してください。展開したディレクトリで実行します。

```sh
./codex-watchdog --version
./codex-watchdog macos-install
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" doctor
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" macos-tls-check
```

TLS チェックは認証情報を使わず、メッセージを送信せずに Slack へ接続します。
v0.2.5 以降はシステムの CA バンドルを自動選択し、ユーザーが明示した証明書設定を
保持します。以前の実機検証は v0.2.3、手動登録した単一ワークスペース、明示的な
CA 環境変数で実施しました。ネイティブのパッケージ検証と、ご自身の Mac での
新バージョンの検証は別の確認手順です。

既存のランタイム、ルーティング、Keychain 設定は再利用されます。固定パスへの
フック導入と手動の信頼、Slack のフォアグラウンド実行、更新、ロールバック、
手動検証は [Mac パッケージガイド](docs/MACOS_PACKAGE.md)を参照してください。

### Linux ARM64 と x64 パッケージ

[GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases) から
`aarch64` 用の `codex-watchdog-vX.Y.Z-linux-arm64.zip`、または `x86_64` 用の
`codex-watchdog-vX.Y.Z-linux-x64.zip` をダウンロードします。`SHA256SUMS.txt`
を確認して ZIP 全体を展開してください。v0.2.6 以降の x64 は RHEL 8.10 と
Ubuntu 22.04 以降を対象とし、glibc 2.28 を基準にビルドします。ARM64 は引き続き
Ubuntu 22.04 以降が対象で、glibc 2.35 が必要です。
Python、pip、仮想環境、ソースの取得は不要ですが、Git と Codex CLI/VS Code は
別途必要です。

更新前に実行中の WatchDog を解放または停止してください。展開したディレクトリで実行します。

```sh
./codex-watchdog --version
./codex-watchdog linux-install
watchdog="${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog"
"$watchdog" doctor
"$watchdog" linux-hooks
```

既存フックのランタイムまたは保存済みパッケージプロファイルを再利用し、ユーザー設定と
認証情報を保持して、置換するファイルをバックアップします。生成されたフックを確認してから
`linux-hooks --install` を実行し、変更された定義を Codex で手動で信頼してください。
固定実行パスは空白に対応します。正確な同一スレッドのバインド、フォアグラウンドの
`linux-run`、アイドル時の `linux-release`、更新とロールバックは
[Linux パッケージガイド](docs/LINUX_PACKAGE.md)を参照してください。

v0.2.7 以降の[リモート会話の自動引き継ぎ](docs/AUTOMATIC_REMOTE_HANDOFF.md)では、
常駐する Linux WatchDog が Remote-SSH 切断後に同じ会話を引き継ぎ、安全なアイドル状態で
制御を戻します。デスクトップ側、リモート側 WatchDog、信頼済みフックの実装を一緒に更新し、
既存ランタイムを指定してユーザーサービスから `linux-auto-run --interval 5` を実行します。
完全なコマンドはガイドを参照してください。この経路では手動バインドは不要です。
引き継ぎ後も VS Code の最初の再開が完了しない場合は、そのワークスペースウィンドウを
再読み込みして既存の会話を開き直してください。

ARM64 パッケージは実際の Ubuntu ARM64 マシンで、x64 パッケージはホスト型の
Ubuntu/UBI 環境と実際の RHEL 8.10 マシンの隔離環境で検証済みです。
検証には HTTPS の信頼、所有権と Stop のテストを含みます。一般的な Linux
デスクトップ検出は引き続き CI 検証済みプレビューであり、パッケージ検証は実ユーザーによる
フックの信頼やデスクトップ E2E 検証の代わりにはなりません。

#### 任意の Linux ソースインストール

ソース実行には Python 3.9 以降が必要です。このリポジトリのソースディレクトリで実行します。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
codex-watchdog --version
```

[Linux ソースガイド](docs/LINUX_SOURCE_WORKFLOW.md)に従って正確な既存の会話を
明示的にバインドし、フックを確認して信頼した後、`linux-run` でフォアグラウンドの
制御プロセスを起動します。VS Code で同じ会話を開き直す前に `linux-release` を実行し、
解放の完了を待ってください。この明示的な手順は Ubuntu ARM64 で実機 E2E 検証済みです。
Remote-SSH ヘルパーは独立した実行経路であり、各リモートホストに別の WatchDog
所有権制御プロセスを導入する必要はありません。

## 典型的な流れ

```text
人 / manager agent -> GitHub -> WatchDog -> 正確な Codex スレッド
                    進捗/報告 <- Codex -> 通知

Codex -> Parrot Dog (Slack) -> 人 -> Parrot Dog -> 正確な Codex スレッド
```

人、ChatGPT、別のエージェント、または自動化が GitHub に永続的な指示を残せます。
WatchDog が更新を検知して既存スレッドを起こし、Codex が作業と Git 操作、進捗報告を
担当します。WatchDog はその結果を通知し、短い判断が必要なときは Parrot Dog が
Slack で往復を中継します。

## AI 開発に関する宣言

本プロジェクトは、**人間が主導し、AI を広範に活用した vibe-coding
プロジェクト**です。

- **人間のメンテナー：** 製品方針、アーキテクチャと安全境界、受け入れ判断、
  リリース責任を担います。
- **ChatGPT：** アーキテクチャの議論とレビュー、障害分析、指示書・文書の作成を
  支援します。
- **OpenAI Codex：** 実装、テスト、診断、パッケージング、反復的な修正の大部分を
  担います。

詳細な dogfooding 記録を公開し、この協働方法を明示的かつ検証可能にしています。

## 関連ドキュメント

- [Windows パッケージと初回セットアップ](WINDOWS_PACKAGE.md)
- [Mac パッケージ、更新、手動検証](docs/MACOS_PACKAGE.md)
- [Linux ARM64/x64 パッケージ、更新とロールバック](docs/LINUX_PACKAGE.md)
- [Linux ソース導入と同一スレッドのライフサイクル](docs/LINUX_SOURCE_WORKFLOW.md)
- [リモート会話の自動引き継ぎ、常駐起動、安全な再接続](docs/AUTOMATIC_REMOTE_HANDOFF.md)
- [詳細なセットアップと運用](docs/SETUP.md)
- [プラットフォーム対応とプライバシー安全な診断](docs/PLATFORM_SUPPORT.md)
- [セキュリティポリシーと運用境界](SECURITY.md)
- [アーキテクチャ決定](doc/architecture.md)
- [画像の出典](ASSETS.md)と[サードパーティ通知](THIRD_PARTY_NOTICES.md)
- [実装計画](doc/codex_watchdog_implementation_plan.md)
- [過去の実現可能性調査](doc/probe_report.md)
- [Dogfooding と開発履歴](doc/Progress/)

> [!NOTE]
> Codex WatchDog は独立したコミュニティプロジェクトです。OpenAI、Microsoft、
> GitHub、Slack、またはその関連会社による提携・推奨プロジェクトではありません。
