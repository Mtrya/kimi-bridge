# 安装 kimi-bridge

**English**: [INSTALL.en_US.md](INSTALL.en_US.md)

kimi-bridge 把本地 Kimi Code 连接到一个聊天平台。最快路径见 [README 快速开始](README.md)；本文把同一条路径逐步展开，包括授权之后的平台侧设置和验证。也可以把 [INSTALL_AI.md](INSTALL_AI.md) 交给 AI 助手代为完成。

## 支持情况

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| 飞书 | 已支持 | 完整功能：审批/提问卡片、独立思考流、语音消息 |
| QQ | 已支持 | 仅 C2C 私聊；固定 `auto`，没有审批、提问或独立思考流 |
| 微信 | 已支持 | 仅私聊；固定 `auto`，没有审批、提问、独立思考流、群聊或主动推送 |
| Telegram | 实验性 | 仅私聊；手工 Bot API 配置 |

Linux、macOS 和 Windows 都已支持。

## 1. 验证 Kimi Code

安装官方 [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started) 并完成登录或提供商配置。注意旧版 Python `kimi-cli` 也提供名为 `kimi` 的命令，但与本项目不兼容——它的 `--help` 没有 `web`、`doctor` 子命令。依次运行：

```bash
kimi doctor config
kimi -p "Hello there"
```

第二条命令必须得到正常回答，再继续。

## 2. 安装 kimi-bridge

如果还没有安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，先按其官方指南安装。然后用 uv 安装 kimi-bridge：

```bash
uv tool install kimi-bridge
kimi-bridge --version
```

`kimi-bridge compat` 可以把当前 Kimi Code 版本与 bridge 的兼容性记录对照分类。

## 3. 选择平台并授权

飞书、QQ、微信直接运行各自的 `login`：配置文件不存在时会自动创建 `~/.kimi-bridge/config.toml` 并写入所选平台；授权流程返回用户身份时，会自动填入 `allowed_users` 白名单。如果已有配置选择了其他平台，`login` 会先询问是否切换。

### 飞书

```bash
kimi-bridge feishu login
```

在浏览器中打开命令输出的 URL。页面可以创建新应用或选择已有应用，并预填 bridge 所需的 tenant 权限、`im.message.receive_v1` 事件和 `card.action.trigger` 回调，按页面提示确认即可。这是应用注册，不是用户 OAuth；得到的应用凭据保存到 `~/.kimi-bridge/feishu/credentials.json`，注册人的 `open_id` 自动加入白名单。

授权后还需在飞书开放平台确认机器人能力和其余控制台设置，发布应用版本并确认目标用户可用。接收语音消息需要 `ffmpeg` 在 PATH 中。

### QQ

```bash
kimi-bridge qq login
```

打开命令输出的 URL，扫码并批准官方机器人绑定。成功后凭据保存到 `~/.kimi-bridge/qq/credentials.json`，扫码用户的 `user_openid` 自动加入白名单。

授权后按 QQ 控制台当前要求完成沙箱测试、生产审核、事件 Intents 等步骤，并确保机器人没有被另一个进程占用。QQ 固定 `auto`，没有审批/提问界面，这不是配置错误。

### 微信

```bash
kimi-bridge wechat login
```

在微信中打开命令输出的 URL，扫码并批准 iLink 机器人授权；只有微信明确要求时才输入验证码。凭据保存到 `~/.kimi-bridge/wechat/credentials.json`，不写入配置文件。授权返回扫码账号的稳定身份，`login` 会自动把它写入 `allowed_users` 白名单，确认它就是要授权的用户即可。

一个机器人授权只能由一个进程轮询，不要同时让其他 iLink 进程连接它。微信固定 `auto`，支持接收图片、语音、文件和视频，发送图片、视频和文件；外发音频作为普通文件。

### Telegram

Telegram 没有 `login` 命令。通过官方 BotFather 创建机器人取得 token，然后写入 `~/.kimi-bridge/config.toml`：

```toml
platform = "telegram"

[telegram]
bot_token = "<token from BotFather>"
allowed_users = [123456789]
```

`allowed_users` 是数值用户 ID，不是用户名。启动时 Telegram 适配器会接管长轮询并丢弃待处理更新。

## 4. 验证

```bash
kimi-bridge doctor
kimi-bridge
```

`doctor` 检查本地配置、凭据、路径和 Kimi Code 配置，但不验证平台侧权限和消息链路。前台启动后，用白名单账号发送 `/status` 和一条普通消息，收到完整回复才算安装完成。飞书可以再实际操作一次审批或提问；微信先确认没有第二个进程在轮询同一授权。

## 5. 常驻运行（可选）

前台验证通过后，再决定是否常驻：

- **Linux**：从 [systemd 用户单元模板](docs/kimi-bridge.service) 开始，用 `command -v kimi-bridge` 和 `command -v kimi` 取得的绝对路径填写；
- **macOS**：配置用户级 `launchd` LaunchAgent，使用实际的 `kimi-bridge`、`kimi` 和配置绝对路径；
- **Windows**：创建当前用户的 Task Scheduler 任务，使用与凭据文件相同的用户账户。

服务启动后再做一次 `/status` 真实验证。

## 定制化

- 手工 TOML 凭据（`app_id`/`app_secret` 回退）、`storage_path`、语音转写和全部配置项：[Configuration](docs/CONFIGURATION.md)；
- `status`、`logout`、`login --replace` 等授权控制命令的行为：[Commands](docs/COMMANDS.md)；
- 同时运行多个平台：为每个平台准备独立的进程、配置、状态文件、工作区和机器人资源；
- 交给 AI 助手安装：[INSTALL_AI.md](INSTALL_AI.md)。
