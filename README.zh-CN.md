# Codex WatchDog

<p align="center">
  <a href="README.md">English</a> | <strong>中文</strong> | <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <img src="images/parrotDogLogo.png" alt="Codex WatchDog 与 Parrot Dog 标志" width="320">
</p>

**分布式执行，统一控制。**

Codex WatchDog 将本地电脑和 Remote-SSH 服务器上的**现有 VS Code Codex 会话**
与 GitHub、Slack 连接起来。它观察更新和任务完成，唤醒正确的会话，发送通知，
并转发获准的 Slack 回复。每个会话保留自己的上下文和工作区。

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
