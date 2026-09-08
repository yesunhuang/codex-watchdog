# Codex WatchDog

<p align="center">
  <a href="README.md">English</a> | <strong>中文</strong> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog 与 Parrot Dog 标志" width="320">
</p>

一个面向现有 VS Code Codex 会话的轻量、确定性看门狗：负责观察、唤醒、
转发与通知，但不会变成另一个 AI agent。

## 工作流程一览

### WatchDog：持久的 GitHub 循环

![WatchDog 工作流程：讨论任务，在 GitHub 发布指令，检测更新，唤醒 Codex，执行任务并通知用户](images/watchdog_workflow_cn.png)

### Parrot Dog：快捷的 Slack 双向中转

![Parrot Dog 工作流程：Codex 请求帮助，Slack 转达消息，用户回复，然后 Codex 继续工作](images/parrot_workflow_cn.png)

## 设计理念

- **轻量且确定。** 只使用小而明确、容易检查和测试的机制。
- **人始终在环，操作尽量顺滑。** 关键决策仍由你掌握，WatchDog 只减少重复的
  观察和转发工作。
- **WatchDog observes Git; Codex owns Git。** WatchDog 只观察 Git；暂存、
  提交、拉取、合并、变基和推送都由 Codex 负责。
- **GitHub 是持久的管理与审阅平面。** 评论、提交和进度报告可以跨机器、跨时间
  保留完整上下文。
- **上游 manager 无关、下游当前 Codex 特定。** 管理端是可替换的：只要能把持久指令
  写进 GitHub，就可以是人、ChatGPT、其他 agent 或自动化脚本。当前执行端则依赖
  Codex 的准确线程队列、Hooks 与 rollout/完成事件等具体机制。
- **Slack 是快速且经过认证的转发平面。** 它适合通知和短回复，不代替持久的项目
  记录。
- **不增加额外 AI agent，也不过度编排。** WatchDog 把证据和指令送回准确的
  现有 Codex 线程，推理和工作仍由 Codex 完成。

## 平台状态

| 平台/路径 | 支持级别 |
| --- | --- |
| Windows x64 本地桌面 | **稳定、已完成端到端验证的打包参考实现** |
| Linux Remote-SSH 目标 | **真实远程路径已验证** |
| Linux 显式绑定的同线程源码工作流 | **已在 Ubuntu ARM64 完成真实端到端验证** |
| Linux ARM64 可执行安装包 | **已在 Ubuntu ARM64 实机完成安装包验收；使用显式同线程工作流** |
| Linux x64 可执行安装包 | **Ubuntu 与 RHEL 8.10 原生安装包验收通过；真实用户桌面端到端验证待完成** |
| Linux 本地桌面 | **CI 已验证的预览版；仍需真实桌面端到端验证** |
| macOS Apple Silicon 源码工作流 | **已完成真实端到端验证；仍有窗口拓扑限制** |
| macOS 15 ARM64 安装包 | **开发者预览；v0.2.3 在指定范围内通过真实用户 E2E 验证，需 CA 临时配置** |

Linux 与 macOS 共用 POSIX 锁与存储、标准 VS Code 路径、
原生 `code --status` 和 Codex 可执行文件发现，但仍是前台预览版。
Apple Silicon 和两种 Linux 架构均提供自带运行环境的 ZIP。Linux 还支持
[显式绑定的源码工作流](docs/LINUX_SOURCE_WORKFLOW.md)及独立的 Remote-SSH 辅助路径。
目前不提供后台服务安装器。
可运行 `codex-watchdog doctor` 进行只读检查，或用
`codex-watchdog doctor --export report.json` 生成不含隐私信息的诊断文件。
支持级别的具体含义和原生验证清单见[平台支持与诊断](docs/PLATFORM_SUPPORT.md)。

## 它能做什么

- 观察 Codex 的 Stop/完成事件，并可把最终输出放进通知。
- 继续或唤醒准确的现有 Codex 线程，不创建丢失上下文的新会话。
- 以只读的远端 Git OID 检查充当 GitHub 更新门铃，再由 Codex 完成同步。
- 发送 Slack 通知，支持 Outlook/SMTP 回退，并保留本地审计记录。
- 自动发现符合条件的本地与 VS Code Remote-SSH 工作区。
- 可选地把 WatchDog 创建的 Slack 线程中的白名单回复转发回 Codex，也就是
  **Parrot Dog（鹦鹉狗）**路径。
- 在所有运行位置强制遵守“WatchDog 不修改 Git”的边界。

## 快速开始

**超简单安装运行：** 让你的 Codex 扫描本仓库，并一步步引导你完成安装和启动。

### Windows x64 Beta

1. 从 [GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases)
   下载 `codex-watchdog-vX.Y.Z-windows-x64.zip` 和 `SHA256SUMS.txt`，校验后
   完整解压。无需安装 Python。
2. 如果要安装原生 Hook，请选择一个不含空格的固定解压路径。Git、带 Codex 的
   VS Code、Codex CLI 和 Windows OpenSSH 仍需单独安装。
3. 双击 `codex-watchdog.exe`。程序会创建或复用带版本标记的当前用户启动配置，
   并启动前台监控。按 Ctrl-C 或关闭控制台窗口即可停止。
4. 如需检查或使用高级选项，仍可打开 PowerShell：

   ```powershell
   .\codex-watchdog.exe --version
   .\watchdog.ps1 -DryRun
   ```

5. 生成并检查 Hook 配置，然后进行保守安装：

   ```powershell
   .\codex-watchdog.exe install-user-hooks
   .\codex-watchdog.exe install-user-hooks --install
   ```

   如果已有不同的 `hooks.json`，安装器会拒绝覆盖；请参考详细设置文档手动合并。
   随后在 Codex 中打开 `/hooks`，检查准确的定义并手动信任。

   Stop Hook 的默认宽限时间为 30 秒。更长的等待时间必须显式启用，普通完成
   通知不会再因此延迟十分钟。

> [!IMPORTANT]
> 升级时，程序会自动复用兼容的启动配置、现有 WatchDog Hook 所引用的运行时，
> 或相邻的最新旧版本运行时；不会复制或要求重新输入 Slack、Outlook、Duo、OAuth、
> 工作区或通知设置。在完成新 Hook 的检查、替换并在 Codex 中重新信任之前，请保留
> 旧版本目录。

通知、Slack 回复转发、Outlook OAuth、Remote-SSH、Duo 回退和源码安装都是
按需配置。需要时请阅读 [Windows 打包与安装说明](WINDOWS_PACKAGE.md)和
[详细设置与运行文档](docs/SETUP.md)。

### macOS Apple Silicon 开发者预览版

从 [GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases)
下载 `codex-watchdog-vX.Y.Z-macos-arm64-preview.zip`，核对 `SHA256SUMS.txt`
中的对应校验值后解压。安装包自带 Python，面向 macOS 15 上的 Apple Silicon，
仅使用 ad-hoc 签名，尚未公证。

升级前请先停止正在运行的 WatchDog。在解压目录中执行：

```sh
./codex-watchdog --version
./codex-watchdog macos-install
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" doctor
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" macos-tls-check
```

TLS 检查会连接 Slack，但不使用凭据，也不发送消息。从 v0.2.5 起，安装包会
自动选择系统 CA 证书包，并保留用户明确设置的证书配置。此前的实机验收使用
v0.2.3、单个手动注册的工作区和显式 CA 环境变量；原生安装包检查与在您自己的
Mac 上测试新版本仍是不同的验收步骤。

程序会复用已有运行目录、路由和 Keychain 设置。固定路径的 Hook 安装、手动信任、
Slack 前台运行、升级、回滚和手动测试步骤见
[Mac 安装包指南](docs/MACOS_PACKAGE.md)。

### Linux ARM64 与 x64 安装包

从 [GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases) 下载
`codex-watchdog-vX.Y.Z-linux-arm64.zip`（`aarch64`）或
`codex-watchdog-vX.Y.Z-linux-x64.zip`（`x86_64`），核对 `SHA256SUMS.txt` 后
完整解压。从 v0.2.6 起，x64 面向 RHEL 8.10 和 Ubuntu 22.04 及更新版本，
以 glibc 2.28 为基线；ARM64 仍面向 Ubuntu 22.04 及更新版本，需 glibc 2.35。
无需另装 Python、pip、
虚拟环境或源码；Git 和 Codex CLI/VS Code 仍需单独安装。

升级前请先释放或停止正在运行的 WatchDog。在解压目录中执行：

```sh
./codex-watchdog --version
./codex-watchdog linux-install
watchdog="${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog"
"$watchdog" doctor
"$watchdog" linux-hooks
```

安装时会复用现有 Hook 的运行目录或已保存的安装包配置，保留用户设置与凭据，
并备份被替换的文件。先检查生成的 Hook，再运行 `linux-hooks --install`，
随后在 Codex 中手动信任更改后的定义。固定可执行路径支持空格。准确的同线程绑定、
前台 `linux-run`、空闲时 `linux-release`、升级与回滚步骤见
[Linux 安装包指南](docs/LINUX_PACKAGE.md)。

ARM64 安装包已在真实 Ubuntu ARM64 机器上完成原生验收；x64 安装包已通过托管
Ubuntu/UBI 验收和真实 RHEL 8.10 机器上的隔离验收。这些检查包含 HTTPS 信任、
所有权与 Stop 测试。通用 Linux 桌面发现仍是 CI 已验证
的预览功能；安装包验收不能替代真实用户的 Hook 信任或桌面端到端验收。

#### 可选的 Linux 源码安装

源码运行需要 Python 3.9 或更新版本。在本仓库的源码目录中执行：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
codex-watchdog --version
```

按照 [Linux 源码指南](docs/LINUX_SOURCE_WORKFLOW.md)显式绑定准确的现有会话，
检查并信任其 Hook，再用 `linux-run` 启动前台控制进程。在 VS Code 中重新打开
同一会话前，请运行 `linux-release` 并等待释放完成。该显式工作流已在 Ubuntu ARM64
完成真实端到端验证。Remote-SSH 辅助程序是独立的执行路径，无需在每台远程主机上
再安装一个 WatchDog 所有权控制进程。

## 典型工作流

```text
人 / manager agent -> GitHub -> WatchDog -> 准确的 Codex 线程
                    进度/报告 <- Codex -> 通知

Codex -> Parrot Dog（Slack）-> 人 -> Parrot Dog -> 准确的 Codex 线程
```

人、ChatGPT、其他 agent 或自动化都可以在 GitHub 留下持久指令；WatchDog 发现变化
并为现有线程按门铃；Codex 负责实际工作和 Git 操作、写入进度记录，WatchDog 再发送
结果通知。

## AI 开发声明

这是一个**由人主导、广泛使用 AI 辅助的 vibe-coding 项目**：

- **人类维护者：** 决定产品方向、架构与安全边界，执行验收并承担发布责任。
- **ChatGPT：** 参与架构讨论与审阅、故障分析，以及指令和文档起草。
- **OpenAI Codex：** 完成大部分实现、测试、诊断、打包和迭代修复。

详细的 dogfooding 记录保持公开，让这套协作方式明确、可检查，而不是把项目包装
成完全由人手工完成的软件。

## 更多文档

- [Windows 打包与首次设置](WINDOWS_PACKAGE.md)
- [Mac 安装包、升级与手动测试](docs/MACOS_PACKAGE.md)
- [Linux ARM64/x64 安装包、升级与回滚](docs/LINUX_PACKAGE.md)
- [Linux 源码安装与同线程生命周期](docs/LINUX_SOURCE_WORKFLOW.md)
- [详细设置与运行](docs/SETUP.md)
- [平台支持与隐私安全诊断](docs/PLATFORM_SUPPORT.md)
- [安全策略与运行边界](SECURITY.md)
- [架构决策](doc/architecture.md)
- [图片来源](ASSETS.md)与[第三方声明](THIRD_PARTY_NOTICES.md)
- [实现计划](doc/codex_watchdog_implementation_plan.md)
- [历史可行性探测](doc/probe_report.md)
- [Dogfooding 与开发历史](doc/Progress/)

> [!NOTE]
> Codex WatchDog 是独立的社区项目，与 OpenAI、Microsoft、GitHub、Slack
> 及其关联方不存在隶属或背书关系。
