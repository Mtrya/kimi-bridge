# 安装 kimi-bridge

**English**: [INSTALL.en_US.md](INSTALL.en_US.md)

kimi-bridge 把一份本地的 Kimi Code 安装连接到一个受支持的聊天机器人。推荐的安装路径是把[智能体安装指南](INSTALL_AI.md)交给一个得力的编程智能体：平台机器人的配置涉及凭据、权限、事件投递、身份发现，以及一次真实的端到端测试，这些环节仅靠安装软件包无法验证。

## 支持情况

| 平台 | 状态 | 重要限制 |
| --- | --- | --- |
| 飞书 | 已支持并经过真实环境验证 | 需要 FFmpeg、已发布的自建应用、长连接事件、相应权限和白名单 |
| QQ | 已支持，C2C 场景经过真实环境验证 | 强制 `auto` 权限模式；没有审批提示、提问和独立的思考流 |
| Telegram | 实验性 | 仅限私聊；启动时会替换已有的 webhook 并丢弃待处理的更新 |
| Lark 国际版 | 不支持 | 当前适配器使用飞书的 API 和 WebSocket 域名 |

一个 kimi-bridge 进程只运行一个平台适配器。如需接入多个平台，请使用各自独立的进程、配置、状态文件、工作区、服务和机器人账号。

## 前置条件

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Python 3.11 或更高版本，由 uv 提供或选择
- 已完成认证的官方 [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started)
- 所选平台上的一个专用机器人应用
- 选择飞书时，需要在 `PATH` 中提供 [FFmpeg](https://ffmpeg.org/download.html)

旧的 Python `kimi-cli` 产品虽然也安装 `kimi` 命令，但并不兼容。官方 Kimi Code 的帮助信息中包含 `web`、`doctor` 和 `migrate`。

安装 bridge 之前先验证 Kimi Code：

```bash
kimi --version
kimi --help
kimi doctor config
```

必须保证 Kimi Code 是可以使用的，最简单的测试方法就是终端输入 `kimi -p "hi"`，有正常回答就说明可用。

## 安装

```bash
uv tool install kimi-bridge
kimi-bridge --version
kimi-bridge compat
```

`compat` 会把已安装的 Kimi Code 版本与 kimi-bridge 内置的兼容性历史进行对比。请尽量使用经过测试的版本组合。未测试的版本仍会尝试运行，但不保证兼容。

## 配置

kimi-bridge 默认读取 `~/.kimi-bridge/config.toml`。可以用 `--config PATH` 或 `KIMI_BRIDGE_CONFIG` 指定其他文件。

创建私有配置文件：

```bash
install -d -m 700 ~/.kimi-bridge
touch ~/.kimi-bridge/config.toml
chmod 600 ~/.kimi-bridge/config.toml
```

参照[完整的配置参考](docs/CONFIGURATION.md)填写一个适配器。请通过本地私有编辑器或密钥管理工具输入凭据；不要把凭据写在命令行里、提交进版本库，或粘贴到 issue 报告中。

平台侧的配置不止需要凭据：

- 飞书需要在 `PATH` 中提供 FFmpeg、启用机器人能力、授予包含 `speech_to_text:speech` 的精确消息/资源权限、配置长连接的消息和卡片事件、发布应用版本，并取得目标用户的 `open_id`。FFmpeg 会把飞书语音资源的 Opus 音频转换为原生语音识别要求的 16 kHz 单声道 PCM。
- QQ 需要 AppID/AppSecret、在适用时为目标沙箱测试用户开通权限，以及目标用户在该应用下的 `user_openid`。
- Telegram 需要 bot token、目标用户的数字 ID，以及一个专用机器人——它已有的 webhook 或更新消费者必须可以被安全替换。

智能体安装指南包含各平台具体的初始化与验证流程。手动操作者可以从该指南中找到官方平台文档的链接。

## 验证

运行不启动任何组件的诊断：

```bash
kimi-bridge doctor
```

启动前解决所有报错。`doctor` 检查本地配置、路径、Kimi Code、凭据是否存在，并在选择飞书时检查 FFmpeg。它不会连接聊天平台、验证机器人权限、接收事件或发送消息。

前台启动：

```bash
kimi-bridge
```

在白名单内的聊天账号中：

1. 发送 `/status`，确认收到回复；
2. 发送一条普通提示，确认流式回复完整结束；
3. 在飞书或 Telegram 上，实际操作一次审批或提问；
4. 在飞书或 QQ 上发送一条语音消息，确认智能体收到正确的转写；
5. 测试你的使用场景所依赖的文件类型。

以上真实链路全部通过之前，不要认为安装已完成。

## 常驻运行

前台运行是完全支持的方式。在 Linux 上，可以参照 [systemd 用户单元模板](docs/kimi-bridge.service)，把下列命令输出的绝对路径填入模板：

```bash
command -v kimi-bridge
command -v kimi
```

将单元文件放到 `~/.config/systemd/user/kimi-bridge.service` 之前请先检查其内容。不要在单元文件中写凭据。创建或启用常驻服务、开启用户 lingering 都属于独立的运维决策。

常用操作：

```bash
systemctl --user status kimi-bridge.service
journalctl --user -u kimi-bridge.service -f
kimi-bridge doctor
uv tool upgrade kimi-bridge
```

停止或卸载 kimi-bridge 时，`config.toml`、`state.json`、工作区、接收的文件、Kimi 会话以及平台机器人应用都应保留，除非你明确选择删除这些具名资源。

## 参考

- [配置](docs/CONFIGURATION.md)
- [聊天命令](docs/COMMANDS.md)
- [架构与兼容性策略](docs/ARCHITECTURE.md)
- [智能体安装指南](INSTALL_AI.md)
- [开发](README.md#开发)
