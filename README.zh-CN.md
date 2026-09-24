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
  <a href="docs/PLATFORM_SUPPORT.md#support-matrix"><img src="https://img.shields.io/badge/macOS-Apple%20Silicon-44627e" alt="支持 macOS Apple Silicon"></a>
  <br>
  <a href="#架构与设计理念"><img src="https://img.shields.io/badge/sessions-native-355c7d" alt="保留现有原生 Codex 会话"></a>
  <a href="#多人协作bring-your-own-agents"><img src="https://img.shields.io/badge/collaboration-multi--user%20%2F%20multi--agent-355c7d" alt="多用户、多 Agent 协作"></a>
  <a href="#多人协作bring-your-own-agents"><img src="https://img.shields.io/badge/central%20server-not%20required-355c7d" alt="无需中央服务器"></a>
  <br>
  <a href="docs/SETUP.md"><img src="https://img.shields.io/badge/supports-Slack-4A154B" alt="支持 Slack"></a>
  <a href="docs/FEISHU_LARK.md"><img src="https://img.shields.io/badge/supports-Feishu-3370FF" alt="支持飞书"></a>
  <a href="docs/FEISHU_LARK.md"><img src="https://img.shields.io/badge/supports-Lark-00B96B" alt="支持 Lark"></a>
  <a href="docs/ONEBOT_QQ.md"><img src="https://img.shields.io/badge/supports-OneBot%2011-2679b5" alt="支持 OneBot 11"></a>
  <a href="docs/ONEBOT_QQ.md"><img src="https://img.shields.io/badge/tested-QQ%20via%20NapCat-12b886" alt="已验证 NapCat 接入 QQ"></a>
  <a href="docs/SETUP.md"><img src="https://img.shields.io/badge/supports-SMTP-6c757d" alt="支持 SMTP"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> · <a href="#消息传输层">消息通信</a> · <a href="#架构与设计理念">架构</a> · <a href="https://github.com/yesunhuang/codex-watchdog/releases">发行版本</a>
</p>

Codex WatchDog 是一层面向**现有** VS Code Codex 会话的轻量协调与控制层。
它把人、Agent、机器和通信界面连接起来，但不会要求团队迁移到另一套 Agent 平台、
runtime、dashboard、数据库、scheduler 或强制性的中央 manager。

核心思想很简单：成熟工具已经把各自擅长的事情做得很好。GitHub 适合保存持久协作状态与
审计历史；Slack、飞书/Lark 和 QQ 适合做人机通信；SSH 适合连接远程机器；VS Code 已经是
开发工作区；Codex 本身已经拥有 conversation 与 execution context。
**WatchDog 不重新造这些工具的缩水版；它只补上它们之间缺失的连接。**

每个团队成员都可以继续使用自己的机器、credential、原生 Codex session 和管理方式。
用户只需要把愿意共享的 Agent 接入团队已有的 GitHub 与消息界面；在项目 policy 和回复
白名单允许时，其他成员或 manager 可以向那条**准确的现有 Agent session** 发出指令。
Manager 可以是人，也可以是 AI；可以集中，也可以分布式。整个系统不要求唯一 manager，
也不要求中央 WatchDog server。

## 架构与设计理念

- **WatchDog 观察，Codex 执行。** WatchDog 通过只读 Git 检查发现更新；Codex 读取指令并
  负责提交、拉取、合并等 Git 修改。WatchDog 不是另一个 AI Agent。
- **GitHub 是持久控制面；消息服务是快速控制面。** Parrot Dog 可以把 Slack、飞书/Lark
  或 OneBot 11 上经过白名单验证的回复送回**准确的现有 Codex 会话**。
- **OneBot 是传输边界，不是 QQ 的重实现。** WatchDog 通过带认证的正向 WebSocket
  说通用 OneBot 11；外部 backend 负责具体平台的登录与协议维护。**NapCat 是目前经过
  实机验证的 QQ reference backend；WatchDog 不内置 NapCat，也不携带 QQ runtime。**
- **每个会话只有一个执行所有者。** 归属、目标或投递状态不明确时，WatchDog 会拒绝猜测。
- **保持轻量。** 复用原生会话、文件、现有工具和成熟的协议适配器。团队角色与审查规则
  属于项目的 `AGENTS.md`，不由 WatchDog 决定。

![WatchDog 工作流程：讨论任务，在 GitHub 发布指令，检测更新，唤醒 Codex，执行任务并通知用户](images/watchdog_workflow_cn.png)

![Parrot Dog 工作流程：Codex 请求帮助，消息服务转达，用户回复，然后 Codex 继续工作](images/parrot_workflow_cn.png)

> 图中继续用 Slack 作为一种消息服务示例；飞书/Lark 和 OneBot 11 采用同样的 exact-thread
> relay 语义。

### 单用户、多 Agent 工作流

```text
                         Human / Manager
                               |
                        聚合控制界面
                               |
                 +-------------+--------------------------+
                 |                                        |
              GitHub                                  消息传输层
          持久指令 / 报告                    Slack / 飞书 / Lark / OneBot 11
                 |                                        |
                 +-------------------+--------------------+
                                     |
                                  WatchDog
                       observe / wake / route / relay
                         notify / handoff / fencing
                                     |
           +-------------------------+-------------------------+
           |                         |                         |
     Codex A（本地）          Codex B（Remote-SSH）      Codex C（detached）
       原生 session              原生 session               原生 session
           |                         |                         |
           +--------------- checkpoint reports -------------+
                                     |
                                   GitHub
```

Codex 负责 Git 与进度报告；WatchDog 检测更新、唤醒准确的 thread，并发送通知。
Parrot Dog 将获准用户从当前消息传输层发来的回复路由回同一条会话。

## 多人协作：Bring Your Own Agents

WatchDog 不要求团队先把所有机器和 Agent 注册到同一个中央 runtime。每个人都可以在自己的
机器上运行自己的 WatchDog，并只把愿意共享的 Agent 接到团队本来就在使用的协作界面。

```text
        Alice 的机器                           Bob 的机器
   +----------------------+               +----------------------+
   | Windows / Codex A1   |               | Linux / Codex B1     |
   | HPC / Codex A2       |               | macOS / Codex B2     |
   +----------+-----------+               +-----------+----------+
              |                                       |
          Alice 的狗                               Bob 的狗
              |                                       |
              +---------- GitHub + 消息界面 ----------+
                             团队共享控制面
```

通知会带上 machine identity，团队成员可以知道消息来自哪个 locality。只有 owner 主动配置到
共享消息界面的狗才会在那里出现。在项目规则和回复白名单允许时，团队成员可以直接回复，并让
消息回到 owner 机器上的**准确现有 Codex session**。

GitHub branch、repository permission 与 `AGENTS.md` 可以定义**谁应该被允许做什么**；
WatchDog 负责的是另一件事：**找到正确的机器/thread，并安全地把消息送过去。**
Policy 继续留在 transport layer 之外。

## 可组合的模型 Worker

WatchDog 的管理边界是**现有 Codex session**，而不是 Codex 内部可能调用的每一个模型或
工具。Codex agent 或 subagent 可以把边界明确的子任务交给已经认证的模型 CLI / tool，
例如 Claude Code、由 DeepSeek 驱动的 CLI，或其他本地/远程模型接口；这不需要再为每个
模型给 WatchDog 写一套 adapter。

```text
Human / Manager
      |
   WatchDog
      |
现有 Codex session
      |
Codex subagent
      |
模型 Worker / CLI
(Claude Code / DeepSeek / 其他)
```

下游模型 Worker **不是** WatchDog 的一等 Agent。WatchDog 不负责它的 session、
routing、progress report 或 merge authority；调用它的 Codex 仍然负责拆题、验收、
policy 与最终集成。这个边界是有意设计的：只要 Codex 能安全调用某个工具，并且用户的
认证、quota、隐私要求和仓库 policy 允许，它就可以作为 Codex 背后的能力参与工作，
而无需扩大 WatchDog 的 control plane。

仓库提供了一个可直接复制/改造的
[**codex-use-claude** skill 模板](examples/skills/codex-use-claude/SKILL.md)。
它采用很简单的分工：Claude 承担边界明确的实现劳动，Codex 负责定义 contract、验证结果，
并保留判断和集成权。相同模式也可以改造成 DeepSeek 或其他模型 CLI 的 worker。

这里说的是**可组合的间接兼容**，并不表示 WatchDog 原生管理 Claude、DeepSeek 或其他
下游模型的 session。

## 消息传输层

| 传输方式 | 作用 | 当前状态 |
| --- | --- | --- |
| Slack | 通知 + exact-thread 回复 relay | 已支持并经过生产环境验证 |
| 飞书 / Lark | 通知 + exact-thread 纯文本回复 relay | 已支持并经过生产环境验证 |
| OneBot 11 | 通用、带认证的正向 WebSocket transport | v1.1.0 起正式支持 |
| QQ via NapCat | OneBot 11 的 reference backend | Windows 上已用 NapCat 4.18.28 + 官方 QQ 引用回复完成真实验收 |
| 其他 OneBot 11 backend | 协议兼容路径 | 在逐个真实验收前按 best-effort 处理 |
| SMTP | 仅发送通知的 fallback | 已支持 |

对于 OneBot 11，WatchDog 只负责薄薄的 transport 与 exact-thread routing；具体聊天平台由
外部 backend 负责。QQ 推荐并已验证的路径是：

```text
现有 Codex thread
        |
     WatchDog
        |
   OneBot 11 WebSocket
        |
      NapCat
        |
        QQ
```

WatchDog **永远不会猜测回复应该送到哪个 thread**。官方 QQ 的引用回复已通过真实验收。
在当前测试 backend 中，TIM 3.4.5 没有保留可用的引用消息身份，因此不接受 TIM 作为控制
回复来源。具体配置、安全边界和验收范围见
[OneBot 11 / QQ 指南](docs/ONEBOT_QQ.md)。

回复已映射的 Slack 通知 `bind #channel`，即可将该会话的后续消息发送到指定频道；`unbind` 恢复默认收件箱。绑定成功后，新频道会收到一条可回复的欢迎消息。权限要求与历史消息轮询延迟见[会话消息目的地](docs/SESSION_DESTINATIONS.md)。

## 支持的部署方式

| 平台 | 推荐工作流 | 状态与限制 |
| --- | --- | --- |
| Windows x64 | 本地 VS Code，以及对 Linux Remote-SSH 目标的桌面控制 | 稳定桌面参考实现；已验证原生 E2E、升级、图标和 OneBot/QQ |
| Linux ARM64 / x64 | 原生服务器执行、Remote-SSH，以及断线后的继续运行 | 原生服务器/分离运行测试和用户验收通过；本地桌面按 best-effort 处理 |
| macOS Apple Silicon | 原生桌面 | 正式稳定桌面版本；安装、升级、回滚和消息通信均已验收；Remote-SSH/交接范围仍有限 |

**分离执行所有权目前仅在 Linux 上实现；Windows 和 macOS 的原生分离执行所有权尚未实现。**
Linux 包覆盖 ARM64 Ubuntu 和 x64 Ubuntu/RHEL 8；准确要求和验收边界见
[平台支持](docs/PLATFORM_SUPPORT.md)。交还控制后，VS Code 可能需要 Retry 或重新加载窗口。

## 快速开始

**超简单安装运行：** 让本地 Codex 扫描这个仓库，并一步步引导你完成安装和启动。

首次交互启动时可配置 **Slack**、**飞书/Lark**、**两者**、**OneBot (QQ)** 或**跳过**。
已有配置与兼容升级会保留现有 profile。手动配置运行 `codex-watchdog setup-messaging`；
服务说明见[消息配置](docs/MESSAGING_SETUP.md)、[飞书/Lark](docs/FEISHU_LARK.md) 与
[OneBot 11 / QQ](docs/ONEBOT_QQ.md)。

### Windows x64

1. 从 [GitHub Releases](https://github.com/yesunhuang/codex-watchdog/releases) 下载最新
   `codex-watchdog-vX.Y.Z-windows-x64.zip` 与 `SHA256SUMS.txt`。
2. 校验 checksum，完整解压到固定路径。
3. 双击 `codex-watchdog.exe`，创建/复用当前用户 launcher profile 并启动前台 monitor。
4. 如果需要原生 hook，先生成并检查：

   ```powershell
   .\codex-watchdog.exe install-user-hooks
   .\codex-watchdog.exe install-user-hooks --install
   ```

   然后在 Codex 中打开 `/hooks`，自己检查并信任准确的定义。

### macOS Apple Silicon

从 Releases 下载最新 macOS ARM64 ZIP，校验 `SHA256SUMS.txt` 后解压，然后双击
`Install and Start Codex WatchDog.command`（也可以从 Terminal 运行）。安装器会把可执行文件
放到稳定的当前用户目录，完成 WatchDog 设置，同时保留 hook 的人工信任流程。

hook 信任、消息通信、升级、回滚、签名/notarization 状态与当前平台限制见
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

审查 Linux hook 定义后运行 `"$watchdog" linux-hooks --install`，再在 Codex 中信任准确定义。
兼容升级会保留现有 profile、runtime 与 provider 设置。安装与回滚说明见
[Linux 安装指南](docs/LINUX_PACKAGE.md)。

### OneBot 11 / QQ 快速配置

1. 先运行一个外部 OneBot 11 backend。QQ 使用已验证的 NapCat，并开启带 access token 的
   forward WebSocket。
2. 运行 `codex-watchdog setup-messaging --onebot`，在隐藏输入中填写 WebSocket endpoint 和 token。
3. 将显示的 `PAIR_CODEX_ONEBOT_...` 配对码发送到目标私聊或群聊。WatchDog 会自动识别 bot、
   conversation 和获准 human，不需要手工查 QQ user ID。
4. 运行 `codex-watchdog onebot-check --connect`。
5. 引用/回复 WatchDog 发出的消息，即可把文本送回对应的准确 Codex thread。未知或歧义引用会被拒绝。

WatchDog 不负责安装、打包或管理 NapCat/QQ 本身。

## 当前用法

在 Linux 服务器上监控指定仓库中已纳管的会话：

```sh
"$watchdog" linux-auto-run --repo /absolute/repository/path \
  --interval 30 --renew-lease --continue-interrupted
```

重复 `--repo` 可添加仓库；增加 `--thread UUID` 可限制为一个现有会话。这些参数不会创建或纳管
新会话。

Remote-SSH/分离运行应通过独立于 SSH 连接的持久用户服务启动 WatchDog。空闲时仍保持监控；
安全时释放 detached writer，有新工作时再获取，让 VS Code 可以重新打开同一 thread。
详见[交接与启动](docs/AUTOMATIC_REMOTE_HANDOFF.md)。

共享主目录的集群应在**每个符合条件的登录节点上运行一个节点本地 WatchDog**。每个节点发现
本机原生 thread 并维护独立的 volatile runtime state；conversation 不会自动跨节点迁移。
详见[登录节点设置](docs/LINUX_NODE_SETUP.md)。

交互传输可选择 `slack`、`lark`、`both`、`onebot`、`slack+onebot`、`lark+onebot` 或 `all`。
已有显式选择会在兼容升级中保持稳定；修改生产 service 环境前请先查看对应 provider 文档。

## 文档

- 安装：[Windows](WINDOWS_PACKAGE.md)、[macOS](docs/MACOS_PACKAGE.md)、
  [Linux](docs/LINUX_PACKAGE.md)、[provider 配置](docs/SETUP.md)。
- 消息通信：[飞书/Lark](docs/FEISHU_LARK.md)、[OneBot 11 / QQ](docs/ONEBOT_QQ.md)、
  [OneBot 复用审计](docs/ONEBOT_REUSE.md)。
- Linux：[交接](docs/AUTOMATIC_REMOTE_HANDOFF.md)、[登录节点](docs/LINUX_NODE_SETUP.md)、
  [source workflow](docs/LINUX_SOURCE_WORKFLOW.md)。
- [平台状态与 doctor](docs/PLATFORM_SUPPORT.md)。
- [架构](doc/architecture.md)与[安全边界](SECURITY.md)。
- [构建与发布](docs/MANUAL_RELEASE.md)与
  [release history](https://github.com/yesunhuang/codex-watchdog/releases)。
- [可选多 Agent 项目契约](examples/AGENTS.multi-agent.md)。
- [Codex → Claude delegation skill 模板](examples/skills/codex-use-claude/SKILL.md)。
- [资源来源](ASSETS.md)与[第三方声明](THIRD_PARTY_NOTICES.md)。
- [开发与 dogfooding 历史](doc/Progress/)。

这是一个由人主导、AI 深度协助的项目。维护者负责产品方向、验收与发布；ChatGPT 支持设计与
review；OpenAI Codex 完成大量实现、测试和打包工作。

Codex WatchDog 是独立社区项目，与 OpenAI、Anthropic、DeepSeek、Microsoft、GitHub、Slack、
字节跳动、腾讯、NapCat 及其关联方均无隶属关系。
