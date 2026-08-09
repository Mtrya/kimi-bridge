# kimi-bridge

**English**: [README.en_US.md](README.en_US.md)

在即时通讯对话中控制本地的 [Kimi Code](https://github.com/MoonshotAI/kimi-code) 智能体。

kimi-bridge 连接本地 Kimi Code 服务器与一个聊天平台，保持聊天和会话的绑定关系，并按平台能力提供流式回复、文件、会话控制和交互功能。

## 支持情况

| 平台/能力 | 状态 |
| --- | --- |
| 飞书、QQ、微信私聊 | 已支持 |
| Telegram 私聊 | 实验性 |
| Linux、macOS 和 Windows | 已支持 |
| 语音消息接收 | 飞书、QQ 和微信支持 |
| 审批和提问卡片 | 仅飞书支持 |

## 快速开始

第一步先安装官方 [Kimi Code](https://www.kimi.com/code/docs/kimi-code-cli/guides/getting-started.html)（注意不是同名的旧版 Python `kimi-cli`），然后确认 Kimi Code 配置正常，且能够完成问答：

```bash
kimi --version
kimi doctor config
kimi -p "Hello there"
```

确认 Kimi Code 可用后，安装 kimi-bridge：

```bash
uv tool install kimi-bridge
kimi-bridge --version
```

然后选择你最常用的平台登录：

```bash
kimi-bridge {feishu, qq, wechat} login
```

在配置过程中可能需要点击网页链接或扫描二维码完成授权。配置成功后启动 `kimi-bridge`：

```bash
kimi-bridge
```

就可以在聊天平台中和 Kimi 对话啦！

也可以让 AI 助手全程代办：打开任意 CLI 智能体，对它说：

```text
阅读 https://github.com/Mtrya/kimi-bridge/blob/main/INSTALL_AI.md 并帮我配置 kimi-bridge。
```

它会向你提问并跑完整个安装流程。

需要更详细的安装步骤、平台侧设置或定制化配置时，参考 [安装与运维](INSTALL.md)。配置、聊天命令和架构参考为英文文档：[Configuration](docs/CONFIGURATION.md)、[Commands](docs/COMMANDS.md)、[Architecture](docs/ARCHITECTURE.md)。

## 功能特性

- 持久的 Kimi 会话管理：创建、列表、切换、重命名、查看、压缩和撤销。
- 原地编辑的流式回复与路由侧分块；平台支持时另有独立思考流、交互式审批与提问。
- 忙碌回合中的提示引导、取消、权限模式、模型、推理强度、计划、目标、任务、技能和 MCP 查看。
- 接收图片、视频、文件和语音转写，以及 `/send` 文件外发。
- 私聊白名单、仅监听回环地址的 Kimi 服务器监管，以及本地 `doctor` 诊断。

## 命令

聊天命令包括：

- 会话：`/new`、`/sessions`、`/switch`、`/status`、`/title`、`/usage`、`/compact`、`/undo`；
- 控制：`/mode`、`/model`、`/effort`、`/plan`、`/goal`、`/stop`、`/restart-server`；
- 任务与工具：`/tasks`、`/skills`、`/mcp`；
- 输出：`/send`、`/render-thinking`。

在聊天中输入 `/help` 或 `/<command> ?` 查看用法；确切语法和平台限制见[命令参考](docs/COMMANDS.md)。

## 架构与安全

```text
飞书、QQ、微信或实验性的 Telegram
                 │
                 ▼
             聊天路由器
                 │
                 ▼
           本地 `kimi web`
```

受监管的 Kimi 服务器只绑定回环地址并使用随机 bearer token，聊天访问由适配器白名单限制。kimi-bridge 面向一个受信任的操作者；获授权的 Kimi 智能体可以以宿主机账户的权限读写文件和执行命令，请保护宿主机、配置文件和聊天凭据。组件边界与 Kimi Code 兼容性命令见[架构与兼容性](docs/ARCHITECTURE.md)。

## 开发

```bash
uv sync --dev
uv run pytest -q
uv run ruff check .
```

## 许可证

[MIT](LICENSE) © 2026 Mtrya
