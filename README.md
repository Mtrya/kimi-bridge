# kimi-bridge

**English**: [README.en_US.md](README.en_US.md)

在即时通讯对话中控制本地的 [Kimi Code](https://github.com/MoonshotAI/kimi-code) 智能体。

kimi-bridge 负责控制 Kimi Code 的本地服务器，在重启后保持聊天与会话的绑定关系，以可编辑消息的形式流式输出回复，并把审批、提问、引导、文件、思考过程和会话控制带进你的聊天客户端。

## 支持情况

| 平台/能力 | 状态 |
| --- | --- |
| 飞书单聊 | 已支持 |
| 微信机器人 | 暂不支持 |
| Telegram 私聊 | 实验性 |
| QQ C2C（私聊） | 已支持 |
| Linux，Python ≥3.11 | 已支持 |
| macOS 和 Windows | 实验性 |
| 语音消息 | 飞书和 QQ 已支持 |

每个 bridge 进程只运行一个平台适配器。飞书使用官方 `lark-oapi` WebSocket 客户端；Telegram 和 QQ 使用手写的轻量 `httpx`/`websockets` 传输层，不依赖平台 SDK。

## 功能特性

- 持久的 Kimi 会话管理：创建、列表、切换、重命名、查看、压缩和撤销。
- 原地编辑的答案流式输出、路由侧分块，以及可选的独立思考过程输出。
- 交互式审批与提问，带超时处理和过期操作防护。
- 繁忙回合中的提示引导、取消、权限模式、模型/推理强度/计划控制、目标、任务、技能，以及 MCP 查看。
- 接收图片、文件和飞书/QQ 语音消息，以及 `/send` 文件外发。
- 私聊白名单、仅监听回环地址的 Kimi 服务器监管，以及 doctor 诊断命令。

## 快速开始

最简单的方式：打开任意 CLI 智能体，对它说：
```text
阅读 https://github.com/Mtrya/kimi-bridge/blob/main/INSTALL_AI.md 并帮我配置 kimi-bridge。
```
智能体会通过问答了解你的需求，并端到端完成全部配置。

手动安装概要——需要先安装并配置 [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started) ，然后安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)：

```bash
uv tool install 'kimi-bridge'     # 安装
# 为某个平台适配器创建 ~/.kimi-bridge/config.toml，并 chmod 600
kimi-bridge doctor                # 不启动任何组件，直接校验配置
kimi-bridge                       # 运行
```

完整步骤见 [INSTALL.md](INSTALL.md)；各适配器的完整示例见[配置参考](docs/CONFIGURATION.md)。

## 命令

命令涵盖：

- 会话：`/new`、`/sessions`、`/switch`、`/status`、`/title`、`/usage`、`/compact`、`/undo`；
- 控制：`/mode`、`/model`、`/effort`、`/plan`、`/goal`、`/stop`；
- 任务与工具：`/tasks`、`/skills`、`/mcp`；
- 输出：`/send`、`/render-thinking`。

在聊天中输入 `/help`，或阅读[命令参考](docs/COMMANDS.md)了解确切语法、行为以及不同平台的功能特性。发送 `/<command> ?` 可查看详细的聊天内用法。

## 架构与安全

```text
飞书、QQ 或 Telegram (实验性)
              │
              ▼
          聊天路由器
              │
              ▼
        本地 `kimi web`
```

受监管的 Kimi 服务器使用随机生成的 bearer token 并仅绑定回环地址，聊天访问由适配器的白名单限制。kimi-bridge 为单一可信操作者设计：获得许可的 Kimi 智能体可以以宿主机账户的权限读写文件、执行命令，因此请妥善保护宿主机和聊天凭据。组件边界与 Kimi Code 兼容性策略（`kimi-bridge compat`）见[架构文档](docs/ARCHITECTURE.md)。

## 文档

- [安装与运维](INSTALL.md)
- [智能体引导安装](INSTALL_AI.md)（英文）
- [配置](docs/CONFIGURATION.md)（英文）
- [命令与交互](docs/COMMANDS.md)（英文）
- [架构与兼容性](docs/ARCHITECTURE.md)（英文）
- [上游 Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started)
- [报告问题](https://github.com/Mtrya/kimi-bridge/issues)

## 开发

```bash
uv sync --dev
uv run pytest -q
uv run ruff check .
```

单元测试不使用真实的 Kimi、飞书、Telegram、QQ、WebSocket、状态和进程边界；CI 检查不使用任何凭据或模型推理。

## 许可证

[MIT](LICENSE) © 2026 Mtrya
