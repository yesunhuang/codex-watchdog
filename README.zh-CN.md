<p align="center">
  <a href="README.md">English</a> | <strong>中文</strong> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog 与 Parrot Dog 标志" width="160">
</p>

<h1 align="center">Codex WatchDog</h1>

<p align="center">
  <strong>带上自己的 Agent，继续用熟悉的工具，组成一个分布式团队。</strong><br>
  <em>一个无需迁移工作流的超轻量多人、多 Agent、跨机器、跨平台协作层。</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2e7d32" alt="MIT 许可证"></a>
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/Windows-x64-44627e" alt="支持 Windows x64"></a>
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/Linux-ARM64%20%2F%20x64-44627e" alt="支持 Linux ARM64 和 x64"></a>
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/macOS-preview-a66b00" alt="macOS 预览版"></a>
  <br>
  <a href="#架构与设计理念"><img src="https://img.shields.io/badge/sessions-native-355c7d" alt="保留现有原生 Codex 会话"></a>
  <a href="#多人协作bring-your-own-agents"><img src="https://img.shields.io/badge/collaboration-multi--user%20%2F%20multi--agent-355c7d" alt="多用户、多 Agent 协作"></a>
  <a href="#多人协作bring-your-own-agents"><img src="https://img.shields.io/badge/central%20server-not%20required-355c7d" alt="无需中央服务器"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> · <a href="#架构与设计理念">架构</a> · <a href="docs/SETUP.md">配置</a> · <a href="https://github.com/yesunhuang/codex-watchdog/releases">发行版本</a>
</p>

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

## 架构与设计理念

- **WatchDog 观察，Codex 执行。** WatchDog 通过只读 Git 检查发现更新；Codex
  读取指令并负责提交、拉取、合并等 Git 修改。WatchDog 不是另一个 AI agent。
- **GitHub 保存持久指令，Slack 承担快速回复。** **Parrot Dog** 将获准用户在
  WatchDog 创建的 Slack 消息串中的回复送回准确的现有 Codex 会话。
- **每个会话只有一个执行所有者。** Linux 主机的 WatchDog 优先负责本机已纳管的
  会话；主机观察者不可用时由桌面后备处理。归属或投递状态不明确时暂停操作。
- **保持轻量。** 复用原生会话、文件和现有工具。团队角色与审查规则属于项目的
  `AGENTS.md`，不由 WatchDog 决定。

![WatchDog 工作流程：讨论任务，在 GitHub 发布指令，检测更新，唤醒 Codex，执行任务并通知用户](images/watchdog_workflow_cn.png)

![Parrot Dog 工作流程：Codex 请求帮助，Slack 转达消息，用户回复，然后 Codex 继续工作](images/parrot_workflow_cn.png)

### 单用户、多 Agent 工作流

一个用户可以通过 GitHub 和 Slack 协调多个现有 Agent，也可以选择由人或 AI
担任 manager。

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

Codex 负责 Git 操作并撰写进度报告；WatchDog 检测更新、唤醒准确的会话并发送通知。
Parrot Dog 将白名单用户的 Slack 回复送回同一会话。这只是一种可选结构，不要求中央
manager 或 WatchDog 服务器。

## 多人协作：Bring Your Own Agents

WatchDog 不要求团队先把所有机器和 agent 注册到同一个中央 runtime。每个人都可以在自己的
机器上运行自己的 WatchDog，并把愿意共享的 agent 接到团队本来就在使用的协作界面上。

### 多用户、多 Agent 工作流

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

## 支持的部署方式

| 平台 | 推荐工作流 | 状态与限制 |
| --- | --- | --- |
| Windows x64 | 本地 VS Code，以及对 Linux Remote-SSH 目标的桌面控制 | 稳定的桌面参考实现；已验证原生端到端流程、包升级与图标 |
| Linux ARM64 / x64 | 原生服务器执行、Remote-SSH，以及断开后的继续运行 | 原生服务器/分离运行测试和用户验收通过；本地桌面仍为预览 |
| macOS Apple Silicon | 原生桌面预览 | 在可解析归属的窗口结构中有有限的原生端到端证据；Remote-SSH/交接验收范围仍有限 |

断开连接后的原生执行所有者目前仅支持 Linux，不支持 macOS/Windows 原生分离执行。
Linux 包覆盖 ARM64 Ubuntu 和 x64 Ubuntu/RHEL 8；准确要求和验收边界见
[平台支持](docs/PLATFORM_SUPPORT.md)。交还控制后，VS Code 可能需要重试或重新加载窗口。

## 快速开始

**超简单安装运行：** 让你的本地 Codex 扫描本仓库，并一步步引导你完成安装和启动。

首次交互启动且没有任何消息配置时，可选择 **Slack**、**飞书/Lark**、**两者**或**跳过**。
向目标机器人会话发送显示的确认短语，即可配对会话和账户，无需查询聊天或用户 ID。
已有配置及升级不会自动弹出配置向导。也可运行 `codex-watchdog setup-messaging`；
服务启动和恢复方法见[消息配置指南](docs/MESSAGING_SETUP.md)。

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

审查 Linux 钩子定义后，运行 `"$watchdog" linux-hooks --install`，再在 Codex 中信任
准确的定义。兼容升级保留配置、运行时和通知渠道设置；修改过的钩子命令需重新审查和信任。
安装与回滚说明见 [Linux 安装指南](docs/LINUX_PACKAGE.md)。

## 当前用法

在 Linux 服务器上监控指定仓库中已纳管的会话：

```sh
"$watchdog" linux-auto-run --repo /absolute/repository/path \
  --interval 30 --renew-lease --continue-interrupted
```

重复 `--repo` 可添加仓库；增加 `--thread UUID` 可限制为一个现有会话。
一次 Git 更新可能唤醒同一仓库中多个已纳管会话。这些参数不会创建或纳管新会话。

此命令在前台运行。Remote-SSH/分离运行应通过独立于 SSH 连接的持久用户服务启动
WatchDog。上述选项启用中断后的自动继续。空闲时仍保持监控；安全时释放分离执行的
写入所有权，有新工作时再获取，让 VS Code 可以重新打开同一会话。
详见[交接与启动](docs/AUTOMATIC_REMOTE_HANDOFF.md)。

Linux 的 Slack 回复使用 `CODEX_WATCHDOG_SLACK_REPLY_MODE=poll`，并配置已有的
机器人令牌、频道和获准用户列表，无需依赖笔记本的 Slack 连接。只有令牌和频道时仅支持
发送通知。通知渠道设置与限制见交接指南。

共享主目录的集群应在**每个符合条件的登录节点上运行一个节点本地 WatchDog**。
各节点发现本机原生会话并维护独立运行时状态。切换节点前先完成或暂停工作；会话不会
自动迁移。持久启动、原生运行时隔离和已有安装的限制见[节点设置](docs/LINUX_NODE_SETUP.md)。

飞书和国际版 Lark 也支持发送通知，并将纯文本回复转发到原有 Codex 会话。
设置 `CODEX_WATCHDOG_INTERACTIVE_TRANSPORT=both` 可与 Slack 同时使用。
请按照[飞书/Lark 配置指南](docs/FEISHU_LARK.md)选择正确的区域并设置获准用户。
回复默认由各机器轮询读取，可共享同一个应用和聊天。
安装包已包含 SDK；切换通知渠道时保留原有 Slack 设置。

## 文档

- 安装与配置：[Windows](WINDOWS_PACKAGE.md)、[macOS](docs/MACOS_PACKAGE.md)、
  [Linux](docs/LINUX_PACKAGE.md)、[通知渠道设置](docs/SETUP.md)。
- Linux：[交接](docs/AUTOMATIC_REMOTE_HANDOFF.md)、[登录节点](docs/LINUX_NODE_SETUP.md)、
  [源码工作流](docs/LINUX_SOURCE_WORKFLOW.md)。
- [平台状态与 doctor](docs/PLATFORM_SUPPORT.md)。
- [架构](doc/architecture.md)与[安全边界](SECURITY.md)。
- [手动构建与发布](docs/MANUAL_RELEASE.md)及
  [发布历史](https://github.com/yesunhuang/codex-watchdog/releases)。
- [可选的多 Agent 项目契约](examples/AGENTS.multi-agent.md)。
- [素材来源](ASSETS.md)与[第三方声明](THIRD_PARTY_NOTICES.md)。
- [开发与实际使用记录](doc/Progress/)。

本项目由人类主导并广泛使用 AI 辅助。维护者负责产品方向、验收和发布；ChatGPT 协助设计
与审查；OpenAI Codex 完成大量实现、测试和打包工作。

Codex WatchDog 是独立社区项目，与 OpenAI、Microsoft、GitHub、Slack 及其关联方没有
隶属关系或官方背书。
