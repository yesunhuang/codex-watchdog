# Codex WatchDog

<p align="center">
  <a href="README.md">English</a> | <strong>中文</strong> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog 与 Parrot Dog 标志" width="320">
</p>

**分布式执行，统一控制。**

Codex WatchDog 是一层面向**现有** VS Code Codex 会话的极轻量协调与控制层。
它可以跨机器、跨终端、跨通信界面观察、唤醒、路由、交接、转发和通知准确的
现有会话，但不会自己变成另一个 AI agent，也不会搭一套重量级 orchestration runtime。

这个项目最初只是一个 watchdog；现在真正有价值的是围绕它形成的工作流：
agent 可以分散在本地、Remote-SSH、detached 主机和不同 VS Code 窗口里，
但人类/manager 仍然可以通过 GitHub、Slack、progress report 和 exact-thread routing
保持一个统一的控制面。

## 工作流程一览

### WatchDog：持久的 GitHub 循环

![WatchDog 工作流程：讨论任务，在 GitHub 发布指令，检测更新，唤醒 Codex，执行任务并通知用户](images/watchdog_workflow_cn.png)

### Parrot Dog：快捷的 Slack 双向中转

![Parrot Dog 工作流程：Codex 请求帮助，Slack 转达消息，用户回复，然后 Codex 继续工作](images/parrot_workflow_cn.png)

## 这个项目真正优化的是什么

- **极轻量协调。** 核心工作流不需要 Redis、数据库、orchestration cluster、
  第二套 agent runtime 或中央 AI scheduler。
- **跨平台、跨机器。** Windows 是打包参考实现；Linux Remote-SSH 与 detached
  同线程交接已有真实验证路径；macOS 有原生开发者预览路径。
- **多终端，一个工作流。** VS Code、GitHub、Slack、本地 shell 与远程主机都能参与，
  用户不需要被锁死在某一个终端里。
- **多 Codex session，但不把它们压成一个 runtime。** 每个 agent 仍保留自己的原生
  会话、上下文、仓库和执行环境；WatchDog 只把消息路由回准确线程。
- **聚合的人类/manager interface。** GitHub 保存持久指令，Slack 承担快速中断与回复，
  progress report 把 agent 的工作状态压缩回 manager。
- **异步但可审计。** Git 历史、progress report、queue receipt、notification receipt
  与 exact-thread identity 让机器和人不必同时在线也能继续工作。
- **机制与策略分离。** WatchDog 不决定团队层级、单 session 工时、checkpoint 规则或
  merge 权限；这些属于每个项目自己的 `AGENTS.md` contract。

## 设计理念

- **让机制保持笨而薄。** WatchDog 主要负责 observe / wake / notify / relay / route，
  做完就让开。
- **保留原生 agent ownership。** 不为了方便 orchestration 就新建替代 chat；
  现有 Codex session 继续是权威执行上下文。
- **WatchDog observes Git; Codex owns Git。** WatchDog 不做 stage、commit、pull、merge、
  rebase、reset、checkout 或 push。
- **GitHub 是 durable management plane。** 评论、提交与 progress report 可以跨终端、
  跨机器、跨重启长期保留。
- **Slack 是 quick interrupt/relay plane。** 用于通知和短回复，不替代持久项目历史。
- **上游 manager 无关，下游当前 Codex 特定。** manager 可以是人、ChatGPT、其他 agent
  或自动化；执行侧目前依赖 Codex 的 exact-thread queue、hook、state 与 completion contract。
- **允许多个 observer，但同一时刻只有一个 actor。** 本地狗和 detached 狗共存时，
  通过 ownership/fencing 协调，而不是抢着做副作用。
- **能删机制就不要加机制。** 优先使用文件、Git、锁和已有 CLI，而不是再造一套控制平台。

## 多 Agent 项目：策略留在 `AGENTS.md`

WatchDog 自己**不会**给 agent 分配 Codex A/B/C，不会强制 2 小时工时，也不会决定谁有
merge 权。否则一个薄 control fabric 很快就会膨胀成 project-management framework。

本仓库只提供一个可选模板：

**[`examples/AGENTS.multi-agent.md`](examples/AGENTS.multi-agent.md)**

把它复制到新项目根目录并命名为 `AGENTS.md`，再按项目需要修改。默认模板实现四条
轻量 coordination rule：

1. **单 session 最长连续 active time：** 默认 2 小时，到边界必须写 checkpoint/report
   并停止，不能靠写个报告自动续杯。
2. **标准 progress report：** 每个 checkpoint 必须写带日期、checkpoint 编号和 agent
   后缀的报告，例如 `progress_2026_09_10_cpx071_codex_b.md`。
3. **先来先到的 agent 命名锁：** 第一只 agent 在 `AGENTS.md` registry 中原子 claim
   Codex A，后来的依次 claim B、C……；如果 Git push 因竞争失败，就 refetch 后 claim
   下一个空位，绝不能 force-push 覆盖别人的 claim。
4. **集成权限：** 默认只有 Manager 与 Codex A 能处理跨 agent conflict 和 merge；
   其他 agent 需要在 durable `## comment` 中获得明确、限定范围的一次性授权。

这故意只是一个模板。**Policy 跟着项目走，WatchDog 只提供 transport/control mechanism。**

## 一个典型的聚合工作流

```text
                         Human / Manager
                               |
                        统一控制界面
                               |
                 +-------------+-------------+
                 |                           |
              GitHub                       Slack
          持久指令 / 报告              快速通知 / 回复
                 |                           |
                 +-------------+-------------+
                               |
                           WatchDog
                    observe / wake / route
                    relay / notify / handoff
                               |
           +-------------------+-------------------+
           |                   |                   |
     Codex A（本地）     Codex B（Remote-SSH）   Codex C（detached）
       原生 session          原生 session           原生 session
           |                   |                   |
           +------------ checkpoint reports -------+
                               |
                             GitHub
```

manager 可以在 GitHub 留下持久指令；WatchDog 检测到更新后唤醒准确的现有 thread。
Codex 自己执行工作并拥有 Git，然后把状态压缩进 checkpoint progress report；WatchDog
再把结果通知出来。如果用户在 WatchDog 创建的 Slack thread 中回复，Parrot Dog 会把
白名单文本原样送回准确 session。

多 agent 时，可选 `AGENTS.md` 模板负责团队 contract；WatchDog 不需要理解或执行这些策略。

## 平台状态

| 平台/路径 | 支持级别 |
| --- | --- |
| Windows x64 本地桌面 | **稳定、完整 E2E 已验证、打包参考实现** |
| Linux Remote-SSH 目标 | **真实远程路径已验证** |
| Linux 显式同线程 owner | **Ubuntu ARM64 原生 E2E 已验证** |
| Linux 自动远程交接 | **原生 E2E 已验证；reattach 时可能需要重载 VS Code 窗口** |
| Linux ARM64 安装包 | **Ubuntu ARM64 原生安装包验收通过** |
| Linux x64 安装包 | **Ubuntu 与 RHEL 8.10 / glibc 2.28 验收通过** |
| Linux 本地桌面 | **CI 已验证预览；真实桌面 E2E 待完成** |
| macOS Apple Silicon 源码路径 | **原生 E2E 已验证，但仍有 topology 限制** |
| macOS 15 ARM64 安装包 | **开发者预览；安装包验收已存在，新版本设备级 E2E 可能滞后** |

我们会明确区分支持成熟度，而不是假装所有 topology 都一样。详细定义见
[平台支持与诊断](docs/PLATFORM_SUPPORT.md)。

## WatchDog 当前能做什么

- 观察 Codex Stop/completion，并把最终输出用于通知。
- 唤醒或继续**准确的现有 Codex thread**，而不是创建新上下文。
- 用只读 Git remote OID 作为 GitHub 更新门铃，把所有 Git mutation 留给 Codex。
- 发送 Slack 通知，支持 Outlook/SMTP fallback 与本地 audit trail。
- 通过 **Parrot Dog** 把白名单 Slack 回复转回对应的 exact thread。
- 自动发现本地与 VS Code Remote-SSH workspace，并保持不同 session identity 不串线。
- 在 Remote-SSH 关闭后支持 Linux persistent detached owner 接管同一 thread。
- 用 ownership/fencing 协调本地与 detached authority，阻止 stale owner 在交接后继续操作。
- 在所有 WatchDog locality 上坚持 zero-Git-mutation 边界。

## 快速开始

**超简单安装运行：** 让你的本地 Codex 扫描本仓库，并一步步引导你完成安装和启动。

### Windows x64

1. 从 [GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases) 下载最新
   `codex-watchdog-vX.Y.Z-windows-x64.zip` 与 `SHA256SUMS.txt`。
2. 校验 checksum，完整解压到固定路径。
3. 双击 `codex-watchdog.exe`，程序会创建/复用当前用户 launcher profile 并启动前台 monitor。
4. 如果需要原生 hook，先生成并检查：

   ```powershell
   .\codex-watchdog.exe install-user-hooks
   .\codex-watchdog.exe install-user-hooks --install
   ```

   然后在 Codex 中打开 `/hooks`，自己检查并信任准确的定义。

### macOS Apple Silicon 预览

从 Releases 下载 ARM64 preview ZIP，校验 `SHA256SUMS.txt` 后解压并运行：

```sh
./codex-watchdog --version
./codex-watchdog macos-install
"$HOME/Library/Application Support/CodexWatchdog/bin/codex-watchdog" doctor
```

hook 信任、Slack、升级、回滚和当前 preview 限制见
[Mac 安装包指南](docs/MACOS_PACKAGE.md)。

### Linux ARM64 / x64

下载对应 Linux ZIP，校验 `SHA256SUMS.txt` 后解压并运行：

```sh
./codex-watchdog --version
./codex-watchdog linux-install
watchdog="${XDG_DATA_HOME:-$HOME/.local/share}/codex-watchdog/bin/codex-watchdog"
"$watchdog" doctor
"$watchdog" linux-hooks
```

如果希望 Remote-SSH 关闭后仍由远程主机接管同一 thread，请看
[自动远程交接](docs/AUTOMATIC_REMOTE_HANDOFF.md)。persistent remote owner 目前仍是
**Linux-specific**；尚未宣称 macOS/Windows remote owner 已具备同等能力。

从 v0.2.17 起，运行中的 Linux WatchDog 优先监控本机已纳管的 thread，并在 VS Code
仍连接时发送完成通知。只有主机 WatchDog 不可用时，笔记本 WatchDog 才接管监控。

从 v0.2.18 起，可使用 `linux-auto-run --repo /absolute/repository/path` 监控工作区中
已纳管的会话，包括新打开的 thread；多个仓库可重复指定 `--repo`。`--thread UUID`
则明确只监控指定会话。后端退出后会释放失效的所有权；恢复时保留原 thread，且不会
干扰仍在运行的 VS Code writer。

从 v0.2.19 起，Linux 可通过 `CODEX_WATCHDOG_SLACK_REPLY_MODE=poll`、已有的 bot token/
频道和用户白名单接收 Slack 回复，不依赖笔记本的 Slack 连接。只有 bot token 和频道时，
仍然只能发送通知。详见[回复配置与限制](docs/AUTOMATIC_REMOTE_HANDOFF.md)。

在使用共享 home 目录的集群上，请让 WatchDog 服务与 VS Code 工作区运行在同一个选定节点。
安装文件可以共享，但这不代表同一会话可以安全地自动跨节点切换；详情见
[共享目录限制](docs/AUTOMATIC_REMOTE_HANDOFF.md)。

> [!IMPORTANT]
> 升级默认应保留兼容的用户状态。WatchDog 会尽量复用已有 runtime/profile/provider
> 设置，不把“重新配置一遍”当成正常升级步骤。任何 hook executable 改变后，在完成检查
> 与重新信任前都应保留旧版本。

## AI 开发声明

这是一个**由人主导、广泛使用 AI 协作的 vibe-coding 项目**：

- **Human maintainer：** 产品方向、架构与安全边界、验收决策和 release 责任。
- **ChatGPT：** 架构讨论与审查、失败分析、instruction/document drafting。
- **OpenAI Codex：** 大部分实现、测试、诊断、打包和迭代修复。

我们故意保留可检查的 dogfooding 历史，因为这个项目本身就是在持续用自己的工作流
开发自己的过程中长出来的；真实失败也属于设计证据的一部分。

## 更多文档

- [手动构建、测试与发布（已停用 GitHub Actions）](docs/MANUAL_RELEASE.md)
- [多 Agent 项目 contract 模板](examples/AGENTS.multi-agent.md)
- [Windows 安装与首次设置](WINDOWS_PACKAGE.md)
- [Mac 安装、升级与手动测试](docs/MACOS_PACKAGE.md)
- [Linux ARM64/x64 安装、升级与回滚](docs/LINUX_PACKAGE.md)
- [Linux 源码同线程工作流](docs/LINUX_SOURCE_WORKFLOW.md)
- [自动远程交接、常驻启动与安全 reattach](docs/AUTOMATIC_REMOTE_HANDOFF.md)
- [详细设置与运行](docs/SETUP.md)
- [平台支持与隐私安全 doctor](docs/PLATFORM_SUPPORT.md)
- [安全边界](SECURITY.md)
- [架构说明](doc/architecture.md)
- [资源来源](ASSETS.md)与[第三方声明](THIRD_PARTY_NOTICES.md)
- [实现计划](doc/codex_watchdog_implementation_plan.md)
- [历史 feasibility probe](doc/probe_report.md)
- [Dogfooding 与开发历史](doc/Progress/)

> [!NOTE]
> Codex WatchDog 是独立社区项目，与 OpenAI、Microsoft、GitHub、Slack 及其关联方
> 无隶属或官方背书关系。
