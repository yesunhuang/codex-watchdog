# Codex WatchDog

<p align="center">
  <a href="README.md">English</a> | <strong>中文</strong> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog 与 Parrot Dog 标志" width="320">
</p>

**带上自己的 Agent，继续用熟悉的工具，组成一个分布式团队。**

*一个无需迁移工作流的超轻量多人、多 Agent、跨机器、跨平台协作层。*

Codex WatchDog 是一层面向**现有** VS Code Codex 会话的轻量协调与控制层。
它负责把人、Agent、机器和通信界面连接起来，但不会要求团队迁移到另一套 agent 平台、
runtime、dashboard、数据库、scheduler 或强制性的中央 manager。

核心思想很简单：很多成熟工具已经把各自擅长的事情做得很好。GitHub 适合持久协作状态与
审计历史，Slack 适合团队通信，SSH 适合连接远程机器，VS Code 已经是开发工作区，Codex
本身已经拥有 conversation 与 execution context。**WatchDog 不重新造这些工具的缩水版；
它只补上它们之间缺失的连接。**

每个团队成员都可以继续拥有自己的机器、credential、原生 Codex session 和管理方式。
用户只需要把愿意共享的 agent 接入团队已有的 GitHub/Slack 协作界面；在项目 policy 和
回复白名单允许时，其他成员或 manager 也可以向这条准确的现有 agent session 发出指令。
Manager 可以是人，也可以是 AI；可以集中，也可以分布式。整个系统不要求唯一 manager，
也不要求中央 WatchDog server。

## 工作流程一览

### WatchDog：持久的 GitHub 循环

![WatchDog 工作流程：讨论任务，在 GitHub 发布指令，检测更新，唤醒 Codex，执行任务并通知用户](images/watchdog_workflow_cn.png)

### Parrot Dog：快捷的 Slack 双向中转

![Parrot Dog 工作流程：Codex 请求帮助，Slack 转达消息，用户回复，然后 Codex 继续工作](images/parrot_workflow_cn.png)

## 这个项目真正优化的是什么

- **无需迁移工作流。** 团队继续使用原来的 VS Code window、Codex thread、repository、
  SSH host、GitHub project、Slack channel 和工作习惯。
- **极轻量协调。** 核心工作流不需要 Redis、数据库、orchestration cluster、第二套
  agent runtime、强制中央服务或中央 AI scheduler。
- **天然支持多人协作。** 不同成员可以继续拥有自己的 agent 和机器，同时接入同一个
  GitHub/Slack 协作空间；共享 agent 不等于交出它的原生 session 或主机所有权。
- **跨平台、跨机器。** Windows 是打包参考实现；Linux Remote-SSH 与 detached 同线程交接
  已有真实验证路径；macOS 有原生开发者预览路径。
- **多 Codex session，但不把它们压成一个 runtime。** 每个 agent 仍保留自己的原生会话、
  上下文、仓库和执行环境；WatchDog 只把消息路由回准确线程。
- **Manager 可选且可分布。** Human、ChatGPT、其他 Codex 或自动化都可以成为 manager。
  团队可以只有一个 manager、多个 manager，或者直接 human-to-agent，而无需改变底层 transport。
- **异步但可审计。** Git 历史、progress report、queue receipt、notification receipt、
  machine identity 与 exact-thread identity 让机器和人不必同时在线也能继续协作。
- **机制与策略分离。** WatchDog 不决定谁能命令谁、团队层级、单 session 工时、checkpoint
  规则或 merge 权限；这些属于 GitHub permission、branch 与项目自己的 `AGENTS.md` contract。

## 设计理念

- **复用成熟基础设施，而不是重造。** GitHub、Slack、SSH、VS Code、Git、Codex 和操作系统
  已经解决了大量困难问题；WatchDog 应该连接它们，而不是自己做一套更不成熟的复制品。
- **只实现缺失的边。** identity/routing、exact-thread wakeup、handoff、fencing、notification、
  relay 只有在周边成熟工具没有提供时才属于 WatchDog。
- **让机制保持笨而薄。** WatchDog 主要负责 observe / wake / notify / relay / route，做完就让开。
- **保留原生 agent ownership。** 不为了方便 orchestration 就新建替代 chat；现有 Codex session
  继续是权威执行上下文。
- **WatchDog observes Git; Codex owns Git。** WatchDog 不做 stage、commit、pull、merge、rebase、
  reset、checkout 或 push。
- **GitHub 是 durable coordination plane。** 评论、提交、branch 与 progress report 可以跨终端、
  跨机器、跨 manager、跨重启长期保留。
- **Slack 是 shared fast interaction plane。** 它已经提供成熟的用户、channel、thread、通知与
  可见性边界，适合团队快速交互；但它不替代持久项目历史。
- **没有强制中央节点。** 每台机器/locality 可以保留自己的 WatchDog 和原生 session；manager
  也可以分布式存在，不要求唯一权威 manager session。
- **上游 manager 无关，下游当前 Codex 特定。** manager 可以是人、ChatGPT、其他 agent 或自动化；
  执行侧目前依赖 Codex 的 exact-thread queue、hook、state 与 completion contract。
- **允许多个 observer，但同一时刻只有一个 actor。** 本地狗和 detached 狗共存时，通过
  ownership/fencing 协调，而不是抢着做副作用。
- **Reuse before rebuilding. Integrate before inventing.** 优先使用文件、Git、GitHub、Slack、SSH、
  lock 和已有 CLI，而不是再造一套平台。

## 多 Agent 项目：策略留在 `AGENTS.md`

WatchDog 自己**不会**给 agent 分配 Codex A/B/C，不会强制 2 小时工时，不会决定谁可以命令
别人的 agent，也不会决定谁有 merge 权。否则一个薄 control fabric 很快就会膨胀成
project-management framework。

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

## 多人协作：Bring Your Own Agents

WatchDog 不要求团队先把所有机器和 agent 注册到同一个中央 runtime。每个人都可以在自己的
机器上运行自己的 WatchDog，并把愿意共享的 agent 接到团队本来就在使用的协作界面上。

```text
        Alice 的机器                           Bob 的机器
   +----------------------+               +----------------------+
   | Windows / Codex A1   |               | Linux / Codex B1     |
   | HPC / Codex A2       |               | macOS / Codex B2     |
   +----------+-----------+               +-----------+----------+
              |                                       |
          Alice 的狗                               Bob 的狗
              |                                       |
              +------------- GitHub + Slack ----------+
                            团队共享协作界面
```

Slack 通知会带上 machine identity，团队成员可以知道消息来自哪个 locality。只有 owner 主动
把某只狗配置到共享 Slack 界面，它才会在那里出现；如果 owner 不愿共享，就不需要加入这个 channel。
在项目规则和回复白名单允许时，团队成员可以直接回复那只狗的 Slack thread，消息会被路由回 owner
机器上的**准确现有 Codex session**。

GitHub branch、repository permission 与 `AGENTS.md` 可以定义**谁应该被允许做什么**；
WatchDog 负责的是另一件事：**找到正确的机器/thread，并安全地把消息送过去。**
Policy 继续留在 transport layer 之外。

## 一种可选拓扑：聚合 Manager

```text
                         Human / Manager
                               |
                        聚合控制界面
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

这只是其中一种拓扑。Manager 可以是一条常驻 AWS 上的 Codex session，也可以是多个分布式
manager、人类直接介入单个 agent，或者这些模式的任意组合；底层 WatchDog 不需要改变。

## 平台状态

| 平台 | 总体成熟度 |
| --- | --- |
| Windows x64 | **稳定的桌面参考实现**，已验证原生端到端流程和安装包升级 |
| Linux ARM64 / x64 | **服务器／脱离连接流程已有原生验证及成功的用户实测**；本地桌面仍为预览 |
| macOS Apple Silicon | **已有部分原生端到端验证的预览版**；仍有窗口发现及安装包限制 |

### 工作流支持矩阵

| 工作流 | Windows x64 | Linux ARM64 / x64 | macOS Apple Silicon |
| --- | --- | --- | --- |
| 本地桌面 | 已验证原生端到端流程 | 预览；原生桌面端到端验证待完成 | 可明确识别窗口归属的场景已验证 |
| Remote-SSH | 连接 Linux 的控制端已验证 | 原生执行端已验证 | 控制端验证范围仍有限 |
| 脱离连接后接管同一线程 | 可控制 Linux 执行端 | 原生端到端验证及 Spark 手动实测成功 | 无原生 macOS 脱离连接接管器 |
| 自动远程交接 | 桌面端已验证 | 原生生命周期已验证；有时需重载 VS Code | 完整交接端到端验证尚未建立 |
| 可执行安装包 | 启动、图标、升级已验收 | ARM64 Ubuntu；x64 Ubuntu 与 RHEL 8/glibc 2.28 已验收 | 开发者预览；设备验收需对应具体版本 |

用户已在 Spark 完成手动脱离连接实测，未观察到问题。这是服务器工作流的原生证据，
不等同于 Linux 本地桌面验收。旧日志缺失时，Windows/Linux 可使用原生写入进程 PID
验证；macOS 仍需要可解析的路由证据。详见[平台支持与限制](docs/PLATFORM_SUPPORT.md)。

## WatchDog 当前能做什么

- 观察 Codex Stop/completion，并把最终输出用于通知。
- 唤醒或继续**准确的现有 Codex thread**，而不是创建新上下文。
- 用只读 Git remote OID 作为 GitHub 更新门铃，把所有 Git mutation 留给 Codex。
- 发送 Slack 通知，支持 Outlook/SMTP fallback 与本地 audit trail。
- 通过 **Parrot Dog** 把白名单 Slack 回复转回对应的 exact thread。
- Slack 路由通知中包含 machine identity，让共享 channel 中的分布式 session 仍然可区分。
- 自动发现本地与 VS Code Remote-SSH workspace，并保持不同 session identity 不串线。
- 本地 VS Code 的旧路由日志消失后，仍可通过当前窗口的原生写入进程验证准确的现有线程。
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

从 v0.2.20 起，Linux 会话空闲一小段时间，并确认实时空闲状态和队列为空后，会自动释放写入锁。
监控和 Slack 回复仍然启用，VS Code 可以重新打开同一会话。`--repo` 会包含仓库内所有已纳管的
thread，因此一次 Git 更新可能唤醒多个独立会话。如只需监控一个会话，请使用 `--thread UUID`。

在使用共享 home 目录的集群上，每个可运行工作区的登录节点各自运行一个本地 WatchDog，
运行时状态按主机名隔离。每只狗只发现本节点的原生工作，不会自动迁移会话。详见
[节点安装与旧版部署限制](docs/LINUX_NODE_SETUP.md)。

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