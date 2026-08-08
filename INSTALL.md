# 安装 kimi-bridge

**English**: [INSTALL.en_US.md](INSTALL.en_US.md)

kimi-bridge 把本地 Kimi Code 连接到一个聊天平台。本文面向手动安装和运维，按顺序完成：先验证 Kimi Code，再选择一个平台，前台完成真实消息往返，最后再决定是否配置常驻运行。

## 支持情况

| 平台 | 状态 | 关键限制 |
| --- | --- | --- |
| 飞书 | 已支持 | 需要 FFmpeg、机器人能力、平台权限、事件订阅、已发布应用版本和白名单 |
| QQ | 已支持 | 仅 C2C 私聊；强制 `auto`，没有审批、提问或独立思考流 |
| 微信 | 已支持 | QR 授权私聊；一个机器人授权只能由一个进程轮询；强制 `auto`，没有审批、提问、独立思考流、群聊或主动推送 |
| Telegram | 实验性 | 仅私聊；启动时会接管长轮询并丢弃待处理更新 |

Linux、macOS 和 Windows 都是有意支持的平台。一个 kimi-bridge 进程只选择一个 `platform`；需要同时运行多个平台时，为每个平台准备独立的进程、配置、状态文件、工作区和机器人资源。

## 1. 先验证 Kimi Code

先安装并完成官方 [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started) 的登录或提供商配置。旧版 Python `kimi-cli` 也可能安装名为 `kimi` 的命令，但与本项目不兼容。依次运行：

```bash
kimi --version
kimi --help
kimi doctor config
kimi -p "Reply with OK only."
```

最后一条命令必须得到正常回答。`kimi doctor config` 只能验证本地配置，不能代替真实提示词；如果 `kimi --help` 不包含官方 Kimi Code 的 `web`、`doctor` 等命令，请先修复 `kimi` 的 PATH 或安装。

## 2. 安装 bridge

推荐使用 [uv](https://docs.astral.sh/uv/getting-started/installation/)：

```bash
uv tool install kimi-bridge
kimi-bridge --version
kimi-bridge compat
```

`compat` 只负责将当前 Kimi Code 版本与 bridge 的兼容性记录进行分类。它不能验证聊天平台权限。未列出的官方 Kimi Code 版本会给出警告并尝试运行，但兼容性未建立。

## 3. 选择平台并准备配置

默认配置文件是 `~/.kimi-bridge/config.toml`，也可以使用 `--config PATH` 或环境变量 `KIMI_BRIDGE_CONFIG` 指定其他文件。先只配置你要运行的平台。

### POSIX：Linux 和 macOS

下面的命令适用于 Linux/macOS；`chmod` 不适用于 Windows：

```bash
install -d -m 700 ~/.kimi-bridge
touch ~/.kimi-bridge/config.toml
chmod 600 ~/.kimi-bridge/config.toml
```

### Windows：PowerShell 和当前用户 ACL

下面的命令应在 PowerShell 中运行。Windows 不使用 `chmod`；用当前用户的 ACL 限制配置和托管凭据目录。`USERDOMAIN`/`USERNAME` 表示当前登录用户，若企业环境有额外管理员策略，请按本机策略保留必要的恢复主体。

```powershell
$root = Join-Path $HOME ".kimi-bridge"
$config = Join-Path $root "config.toml"
New-Item -ItemType Directory -Force $root | Out-Null
New-Item -ItemType File -Force $config | Out-Null
$principal = "${env:USERDOMAIN}\${env:USERNAME}"
icacls $root /inheritance:r /grant:r "${principal}:(OI)(CI)F"
icacls $config /inheritance:r /grant:r "${principal}:F"
```

Windows 的 `doctor` 不执行 POSIX mode 检查，但仍会检查路径是否可读写；不要把配置或凭据文件放在所有用户都可写的共享目录中。

## 4. 四个平台的最短配置与授权流程

前三个平台提供 QR 控制命令，但三种 QR 不是同一种“登录”：它们分别完成 Feishu/Lark 应用注册、QQ 官方机器人凭据绑定和 WeChat iLink 机器人授权。Telegram 不提供本项目的 QR 控制命令，使用手工 Bot API 配置。

所有 QR 控制命令都只操作对应平台的授权控制面：不会启动 Kimi Code，也不会开始消息轮询。`login` 可连接平台授权服务并等待扫码；`status` 只检查本地文件，不做网络验证；`logout` 只删除适配器拥有的托管文件。每条命令都要求配置中的 `platform` 与命令平台一致。

| 平台 | 命令 | 授权方式 |
| --- | --- | --- |
| 飞书 | `kimi-bridge feishu login` | 官方 Feishu/Lark 应用注册，得到应用 `client_id`/`client_secret`，不是用户 OAuth |
| QQ | `kimi-bridge qq login` | 官方 QQ 机器人凭据绑定，得到 `bot_appid` 和本地解密的 AppSecret，不是 QQ 用户登录/OAuth |
| 微信 | `kimi-bridge wechat login` | WeChat iLink 机器人授权，用微信扫码授权一个可轮询的机器人，不写入 TOML |
| Telegram | 无 QR 控制命令 | 通过官方 Telegram Bot API/BotFather 手工创建 bot，并在 TOML 中配置 token 和数值用户 ID |

如果同一平台已有托管凭据（managed credential），普通 `login` 会拒绝覆盖；确认要换绑时使用 `login --replace`。`status` 和 `logout` 也可用 `--config PATH`。Telegram 没有本项目的 `login`、`status` 或 `logout` 控制命令。

### 飞书：应用注册 QR

先创建 bootstrap 配置。扫码完成后，再把目标用户的真实身份填入 `allowed_users`；不要把扫码本身当作白名单授权。

```toml
platform = "feishu"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[feishu]
storage_path = "~/.kimi-bridge/feishu"
allowed_users = []
```

运行：

```bash
kimi-bridge feishu login
```

在浏览器中打开命令输出的 URL，然后按页面提示使用飞书/Lark 扫码或确认。该流程返回应用 `client_id` 和 `client_secret`，bridge 将其安全保存为 `~/.kimi-bridge/feishu/credentials.json`（或 `storage_path` 指定的路径），并记录返回的 Feishu/Lark tenant 与 API domain。它不是用户 OAuth，也不会自动完成下面的平台端步骤：

1. 启用机器人能力。
2. 按实际功能确认消息收发、资源上传下载和语音识别所需权限；例如当前功能涉及消息权限、资源权限和 `speech_to_text:speech`，具体可用项以当前控制台和官方文档为准。
3. 订阅 `im.message.receive_v1` 和 `card.action.trigger` 事件，并选择长连接事件投递。
4. 发布应用版本并确认目标用户可用。
5. 获取目标用户在同一应用和 tenant 下的 `open_id`，手动写入 `[feishu].allowed_users`。

完成上面步骤后，把 bootstrap 配置中的空数组改成包含真实身份的配置（下面的值只是位置说明，不能原样复制）：

```toml
[feishu]
allowed_users = ["<同一应用和 tenant 下的真实 open_id>"]
```

不要假设扫码用户已经被自动加入 `allowed_users`，也不要假设权限、事件或发布已经完成。Feishu 语音消息还需要 `ffmpeg` 在 PATH 中。

检查并验证：

```bash
kimi-bridge feishu status
kimi-bridge doctor
kimi-bridge
```

前台进程运行后，由白名单用户发送 `/status`，再发送一条普通提示并确认回复完整结束。只有真实消息往返通过后，才算完成平台配置。

### QQ：官方机器人凭据绑定 QR

准备配置。QR 登录期间可以暂时使用空白白名单，但启动 bridge 前必须填入真实的 `user_openid`：

```toml
platform = "qq"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[qq]
storage_path = "~/.kimi-bridge/qq"
allowed_users = []
```

运行：

```bash
kimi-bridge qq login
```

打开命令打印的 QQ 授权 URL，**扫码并批准官方机器人绑定**。成功后，bridge 取得 `bot_appid` 和加密的 `bot_encrypt_secret`，只在本地解密为 AppSecret，并保存最终凭据到 `~/.kimi-bridge/qq/credentials.json`（或自定义 `storage_path`）。临时密钥、绑定任务 ID、二维码 URL 和加密密文不会持久化；消息仍通过现有 QQ REST/token/WebSocket transport 运行。

如果命令返回扫码用户的 `user_openid`，请把它人工写入 `[qq].allowed_users`。它是该 bot 范围内的应用级身份，不是 QQ 号、昵称或显示名；不要自行转换格式，也不要把它写成其他账号的身份。QR 成功不代表沙箱测试用户、生产审核、事件权限/Intents 或消息链路已确认；按 QQ 控制台当前要求完成这些步骤，并确保机器人没有被另一个轮询进程使用。

成功后，把 bootstrap 配置中的空数组改成包含返回值的配置（下面的值只是位置说明，不能原样复制）：

```toml
[qq]
allowed_users = ["<此流程返回的真实 user_openid>"]
```

检查并验证：

```bash
kimi-bridge qq status
kimi-bridge doctor
kimi-bridge
```

由白名单用户发送 `/status` 和一条普通提示，确认 C2C 消息与完整回复均成功。QQ 始终使用 `auto`，因此不要把缺少审批/提问界面当作配置错误。

### 微信：iLink 机器人扫码授权

准备配置。微信凭据不进入 TOML；`allowed_users` 只保存允许发起私聊的稳定扫码身份。

```toml
platform = "wechat"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[wechat]
storage_path = "~/.kimi-bridge/wechat"
allowed_users = []
```

运行：

```bash
kimi-bridge wechat login
```

打开命令打印的授权 URL，在微信中**扫码并批准 iLink 机器人授权**；只有当微信明确要求时才输入验证码。成功后将返回的扫码账号稳定身份人工写入 `[wechat].allowed_users`，不要使用昵称、猜测的账号标识或机器人身份。凭据会保存到 `~/.kimi-bridge/wechat/credentials.json`（或自定义 `storage_path`），不会写入 TOML。

然后运行：

```bash
kimi-bridge wechat status
kimi-bridge doctor
kimi-bridge
```

`status` 只检查本地授权和存储状态，不验证微信网络侧是否仍有效。由白名单用户发送 `/status` 和普通提示，确认私聊真实往返。一个机器人授权只能由一个进程轮询；不要同时让其他 iLink 轮询进程连接它。微信支持接收图片、语音、文件和视频，发送图片、视频和文件；语音识别是尽力而为，外发音频作为普通文件，不是原生语音消息。微信固定 `auto`，没有审批、提问、独立思考流、群聊或主动推送。

### Telegram：手工 Bot API 配置

Telegram 没有本项目的 QR、`login`、`status` 或 `logout` 控制命令。请使用 Telegram 官方 Bot API 和 BotFather 创建机器人并取得 token，然后把 token 写入 `[telegram].bot_token`，把你的数值 Telegram user ID 写入 `[telegram].allowed_users`。`allowed_users` 不是用户名或昵称；请在本地确认真实的数值 ID，不要把下面的示例值原样复制。

```toml
platform = "telegram"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[telegram]
bot_token = "<token from BotFather>"
allowed_users = [123456789]
```

Telegram 适配器仍是实验性功能，仅支持私聊。配置完成后运行：

```bash
kimi-bridge doctor
kimi-bridge
```

前台启动后，由白名单用户发送聊天命令 `/status`，再发送一条普通提示并确认完整回复。Telegram 启动时会接管长轮询，并丢弃尚未处理的更新；它没有本项目的 QR 控制命令。

## 5. 配置文件回退凭据（TOML fallback）与托管凭据优先级

Feishu 和 QQ 仍支持完整的配置文件回退凭据。必须成对填写 `app_id` 与 `app_secret`，不能只填其中一个：

```toml
platform = "feishu" # 或 "qq"

[feishu]
app_id = "cli_replace_me"
app_secret = "replace_me"
storage_path = "~/.kimi-bridge/feishu"
allowed_users = ["<real Feishu open_id>"]

[qq]
app_id = "replace_me"
app_secret = "replace_me"
storage_path = "~/.kimi-bridge/qq"
allowed_users = ["<real QQ user_openid>"]
```

实际运行时只构造选中的平台；未选中的表可以保留，但其凭据不参与启动检查。Feishu/QQ 的选择规则是：

1. 托管 `credentials.json` 不存在：使用完整的配置文件回退凭据 `app_id` + `app_secret`。
2. 托管文件存在且有效：优先使用托管凭据；Feishu 同时使用其中记录的 Feishu/Lark API domain。
3. 托管文件存在但损坏、不安全或不可读：启动报错，不静默使用配置文件回退凭据；先修复或运行对应的 `login --replace`。

`feishu logout` 和 `qq logout` 只删除各自 `storage_path` 下适配器拥有的托管文件，配置文件回退凭据会保留。WeChat 没有配置文件回退凭据。

配置和凭据不要放进命令行、聊天、问题反馈或版本库。Linux/macOS 的配置和托管凭据目录/文件由程序按 `700`/`600` 保护；Windows 请使用当前用户 ACL。

## 6. 本地诊断与真实验证

```bash
kimi-bridge doctor
```

`kimi-bridge doctor` 是 bridge 的本地诊断：检查配置、路径、选中适配器的本地凭据、Kimi Code 配置，并在适用时检查 Feishu 的 FFmpeg 或微信的加密媒体依赖。它不会验证平台端权限、事件订阅、网络授权、消息接收或发送，因此通过 `doctor` 不等于真实平台链路已通过。

先前台运行：

```bash
kimi-bridge
```

完成至少以下真实检查：

1. 白名单用户发送 `/status` 并收到回复。
2. 白名单用户发送一条普通提示，例如“Reply with OK only.”，并收到完整回答。
3. 按实际需要测试文件和语音；飞书还应实际操作一次审批或提问。
4. 确认没有第二个进程轮询同一机器人授权（微信尤其如此）。

## 7. 常驻运行

前台运行是受支持且推荐用于首次验证的方式。只有真实消息往返通过后，再选择常驻方案。

- **Linux**：可以从 [systemd 用户单元模板](docs/kimi-bridge.service) 开始。systemd 仅指 Linux；先用 `command -v kimi-bridge` 和 `command -v kimi` 获取绝对路径，检查 unit 内容，不要把凭据写进 unit。创建、启用服务和开启 user lingering 都是独立的系统管理决定。
- **macOS**：可从用户级 `launchd` LaunchAgent 开始，使用实际的 `kimi-bridge`、`kimi` 和配置绝对路径；launchd 配置、加载和登录会话生命周期需要按 macOS 权限策略单独确认。
- **Windows**：可从当前用户的 Task Scheduler 任务开始，使用 `Get-Command kimi-bridge` / `Get-Command kimi` 得到的绝对路径，并确保任务使用保存凭据文件的同一用户账户。Task Scheduler 的登录、失败重启和电源策略需要按 Windows 管理策略配置。

无论使用何种常驻机制，都应保留独立的配置、状态、工作区和平台本地存储，并在服务启动后再次做一次真实 `/status` 与普通提示验证。

## 参考

- [配置](docs/CONFIGURATION.md)（英文）
- [聊天命令](docs/COMMANDS.md)（英文）
- [QR onboarding](docs/QR_ONBOARDING.md)（英文）
- [架构与兼容性策略](docs/ARCHITECTURE.md)（英文）
- [智能体安装指南](INSTALL_AI.md)（英文）
