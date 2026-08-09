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
| 语音消息 | 飞书、QQ 和微信支持 |

每个 bridge 进程只选择一个平台适配器。飞书、QQ 和微信支持二维码授权，详见 [QR onboarding（英文）](docs/QR_ONBOARDING.md)。

## 快速开始

第一步先确认你使用的是官方 Kimi Code，而不是同名的旧版 Python `kimi-cli`：

```bash
kimi --version
kimi --help
kimi doctor config
kimi -p "Reply with OK only."
```

确认 Kimi Code 能完成真实回答后，安装 bridge。安装完成后不要直接运行 `doctor`：先按 [INSTALL.md](INSTALL.md) 的“选择平台并准备配置”创建 `~/.kimi-bridge/config.toml`，并在其中选择一个平台。

```bash
uv tool install kimi-bridge
kimi-bridge --version
```

然后按所选平台完成授权和配置：

- 飞书、QQ 或微信：先填写对应的 bootstrap 配置，运行对应的 `login`，按页面提示确认平台设置，再完成剩余平台端设置并检查或补充 `allowed_users` 白名单；
- Telegram：按安装指南中的手工 Bot API 流程，将 bot token 和数值用户 ID 写入 `[telegram]`。

配置完成后再运行本地诊断并前台启动：

```bash
kimi-bridge doctor
kimi-bridge
```

### 二维码登录

前三个平台有 QR 控制命令；它们不是同一种“登录”：

```bash
kimi-bridge feishu login       # 飞书/Lark 应用注册二维码
kimi-bridge feishu status      # 检查本地托管凭据目录
kimi-bridge feishu logout      # 删除适配器拥有的本地托管文件

kimi-bridge qq login           # QQ 官方机器人凭据绑定二维码
kimi-bridge qq status          # 检查本地托管凭据目录
kimi-bridge qq logout          # 删除适配器拥有的本地托管文件

kimi-bridge wechat login       # 微信 iLink 机器人授权二维码
kimi-bridge wechat status      # 检查本地托管凭据目录
kimi-bridge wechat logout      # 删除适配器拥有的本地托管文件
```

三组 QR 命令都支持登录时使用 `--replace`。对应的 `status` 只检查本地托管凭据目录，不做网络验证；`logout` 只删除适配器拥有的托管文件，不会删除平台侧机器人绑定。控制命令不会启动 Kimi Code，也不会开始消息轮询；`login` 检测到平台不一致时会在确认后提供切换，`status` 和 `logout` 仍要求平台严格一致。Telegram 没有本项目的 QR、`login`、`status` 或 `logout` 控制命令，必须手工配置 Bot API token 和 `allowed_users`。

默认托管凭据目录为：

- Feishu：`~/.kimi-bridge/feishu/credentials.json`
- QQ：`~/.kimi-bridge/qq/credentials.json`
- WeChat：`~/.kimi-bridge/wechat/credentials.json`

可在各自的 `[feishu]`、`[qq]` 或 `[wechat]` 表中设置 `storage_path`。Feishu 和 QQ 优先使用有效的托管凭据（managed credential）；只有在托管文件不存在时，才使用 TOML 中成对填写的 `app_id` 与 `app_secret`。托管文件存在但损坏时会直接报错，不会静默回退。WeChat 的 QR 凭据不写入 TOML。

完整的人类安装步骤见 [安装与运维](INSTALL.md)，QR 细节见 [QR onboarding（英文）](docs/QR_ONBOARDING.md)。配置、聊天命令和架构参考目前以英文提供： [Configuration](docs/CONFIGURATION.md)、[Commands](docs/COMMANDS.md)、[Architecture](docs/ARCHITECTURE.md)。

## 功能特性

- 持久的 Kimi 会话管理：创建、列表、切换、重命名、查看、压缩和撤销。
- 原地编辑的答案流式输出、路由侧分块，以及在平台支持时提供独立思考输出。
- 在平台支持时提供交互式审批与提问。
- 忙碌回合中的提示引导、取消、权限模式、模型、推理强度、计划、目标、任务、技能和 MCP 查看。
- 接收图片、视频、文件和语音转写，以及 `/send` 文件外发。
- 私聊白名单、仅监听回环地址的 Kimi 服务器监管，以及不启动服务的本地 `doctor` 诊断。

## 命令

聊天命令包括：

- 会话：`/new`、`/sessions`、`/switch`、`/status`、`/title`、`/usage`、`/compact`、`/undo`；
- 控制：`/mode`、`/model`、`/effort`、`/plan`、`/goal`、`/stop`、`/restart-server`；
- 任务与工具：`/tasks`、`/skills`、`/mcp`；
- 输出：`/send`、`/render-thinking`。

在聊天中输入 `/help`，或阅读[命令参考](docs/COMMANDS.md)了解确切语法和平台限制。发送 `/<command> ?` 可查看详细的聊天内用法。

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
