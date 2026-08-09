# Install kimi-bridge

**中文**: [INSTALL.md](INSTALL.md)

kimi-bridge connects one local Kimi Code installation to one chat platform. This human installation and operations guide follows the safe order: verify Kimi Code, choose one platform, complete a real foreground message round trip, and only then decide whether to run the bridge persistently.

## Support

| Platform | Status | Important limitations |
| --- | --- | --- |
| Feishu | Supported | Requires FFmpeg, bot capability, platform permissions, event subscriptions, a published app version, and an allowlist |
| QQ | Supported | C2C private chat only; forced `auto` mode with no approvals, questions, or separate thinking stream |
| WeChat | Supported | QR-authorized private chat; one bot authorization can be polled by only one process; forced `auto` with no approvals, questions, separate thinking stream, groups, or proactive delivery |
| Telegram | Experimental | Private chats only; startup takes over long polling and drops pending updates |

Linux, macOS, and Windows are supported platforms. Each kimi-bridge process selects exactly one `platform`. To run multiple platforms, use separate processes, configuration files, state files, workspaces, and bot resources.

## 1. Verify Kimi Code first

Install and configure the official [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started), including sign-in or provider configuration. The older Python `kimi-cli` product can also install a `kimi` command but is incompatible. Run:

```bash
kimi --version
kimi --help
kimi doctor config
kimi -p "Reply with OK only."
```

The final command must return a normal answer. `kimi doctor config` checks local configuration but cannot prove authentication or model availability. If `kimi --help` does not show the official Kimi Code surface, including `web` and `doctor`, fix PATH or installation before continuing.

## 2. Install the bridge

The recommended installation uses [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv tool install kimi-bridge
kimi-bridge --version
kimi-bridge compat
```

`compat` classifies the installed Kimi Code version against the bridge's compatibility record. It does not validate chat-platform permissions. An unlisted official Kimi Code version is warned about and attempted, but compatibility is not established.

## 3. Choose one platform and prepare private configuration

The default configuration file is `~/.kimi-bridge/config.toml`. Use `--config PATH` or `KIMI_BRIDGE_CONFIG` to select another file. Configure the platform you intend to run.

### POSIX: Linux and macOS

These commands apply to Linux/macOS; `chmod` is not a Windows command:

```bash
install -d -m 700 ~/.kimi-bridge
touch ~/.kimi-bridge/config.toml
chmod 600 ~/.kimi-bridge/config.toml
```

### Windows: PowerShell and the current user's ACL

Run this in PowerShell. Windows does not use `chmod`; restrict the configuration directory and file with the current user's ACL. `USERDOMAIN` and `USERNAME` identify the logged-in user. Keep any required administrative recovery principal according to local policy.

```powershell
$root = Join-Path $HOME ".kimi-bridge"
$config = Join-Path $root "config.toml"
New-Item -ItemType Directory -Force $root | Out-Null
New-Item -ItemType File -Force $config | Out-Null
$principal = "${env:USERDOMAIN}\${env:USERNAME}"
icacls $root /inheritance:r /grant:r "${principal}:(OI)(CI)F"
icacls $config /inheritance:r /grant:r "${principal}:F"
```

Windows `doctor` does not run POSIX mode checks, but it still checks that paths are usable. Do not put configuration or managed credentials in a directory writable by all users.

## 4. Choose one of four platforms and finish setup

The first three platforms provide QR control commands, but these are three different authorization flows, not three forms of the same login: Feishu/Lark application registration, QQ official-bot credential binding, and WeChat iLink bot authorization. Telegram has no QR control command in this project and uses manual Bot API configuration.

All QR control commands operate only the corresponding platform's authorization control plane; they do not start Kimi Code or message polling. `status` checks local files only and does not verify the network; `logout` removes only adapter-owned managed files. Behavior when the command platform does not match the configured `platform` is covered in [Commands](docs/COMMANDS.md).

| Platform | Command | Authorization method |
| --- | --- | --- |
| Feishu | `kimi-bridge feishu login` | Official Feishu/Lark application registration; returns application `client_id`/`client_secret`, not a user OAuth token |
| QQ | `kimi-bridge qq login` | Official QQ bot credential binding; returns `bot_appid` and a locally decrypted AppSecret, not QQ user login or OAuth |
| WeChat | `kimi-bridge wechat login` | WeChat iLink bot authorization; authorize a pollable bot by scanning in WeChat, credential stays out of TOML |
| Telegram | No QR control command | Create a bot through the official Telegram Bot API/BotFather flow, then configure its token and numeric user ID in TOML |

If a managed credential already exists for the platform, plain `login` refuses to overwrite it; use `login --replace` to re-bind. `status` and `logout` also accept `--config PATH`. Telegram has no project `login`, `status`, or `logout` control command.

### Feishu: application-registration QR

Start with a bootstrap config; the allowlist can stay empty for now.

```toml
platform = "feishu"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[feishu]
storage_path = "~/.kimi-bridge/feishu"
allowed_users = []
```

Run:

```bash
kimi-bridge feishu login
```

Open the URL printed by the command in a browser. The page lets the operator create a new application or select an existing one, and pre-fills the tenant permissions, `im.message.receive_v1` event, and `card.action.trigger` callback required by the bridge. Scan with or approve in Feishu/Lark and confirm those requested settings. The result is an application `client_id` and `client_secret`, saved to `~/.kimi-bridge/feishu/credentials.json` (or the configured `storage_path`). The managed record also preserves whether the tenant is Feishu or Lark and the API domain to use.

The operator must still confirm bot capability and any console settings not covered by the registration add-ons, publish an app version, and confirm the intended user's availability. When registration returns the operator's `open_id`, `login` adds it to `feishu.allowed_users`; check that it is the intended sender. If no identity is returned, obtain the intended user's `open_id` in the same app and tenant context and add it manually. Feishu inbound voice still requires `ffmpeg` on PATH.

When done, the allowlist should contain the real identity:

```toml
[feishu]
allowed_users = ["<real open_id from this app and tenant>"]
```

QR success does not mean the app is published.

Check locally and test in the foreground:

```bash
kimi-bridge feishu status
kimi-bridge doctor
kimi-bridge
```

After the foreground process runs, the allowlisted user sends `/status`, then a normal prompt, and confirms the reply completes fully. Platform setup is complete only after this real message round trip passes.

### QQ: official-bot credential-bind QR

Prepare the config; the allowlist can stay empty for now.

```toml
platform = "qq"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[qq]
storage_path = "~/.kimi-bridge/qq"
allowed_users = []
```

Run:

```bash
kimi-bridge qq login
```

Open the printed QQ authorization URL and **scan and approve the official-bot bind**. On success, the bridge obtains `bot_appid` and encrypted `bot_encrypt_secret`, decrypts the AppSecret only locally, and saves the final credential to `~/.kimi-bridge/qq/credentials.json` (or the configured `storage_path`). The temporary key, bind task ID, QR URL, and encrypted blob are not persisted; messages still run through the existing QQ REST/token/WebSocket transport.

When the flow returns the scanner's `user_openid`, `login` adds it to `qq.allowed_users`; check that it is the intended sender. It is an app-scoped identity for that bot, not a QQ number, nickname, or display name; do not transform the format. If no identity is returned, add it manually. QR success does not establish sandbox tester access, production review, event Intents, or the message path; complete the current QQ console requirements and ensure no other polling process uses the bot.

After success, the allowlist should contain the returned identity:

```toml
[qq]
allowed_users = ["<real user_openid returned by this flow>"]
```

Then run:

```bash
kimi-bridge qq status
kimi-bridge doctor
kimi-bridge
```

The allowlisted user sends `/status` and a normal prompt and confirms the C2C message and complete reply both succeed. QQ always uses `auto`; a missing approval or question UI is not a configuration error.

### WeChat: iLink bot QR authorization

Prepare the config. WeChat credentials never enter TOML; `allowed_users` holds the stable scanner identity allowed to start private chats.

```toml
platform = "wechat"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[wechat]
storage_path = "~/.kimi-bridge/wechat"
allowed_users = []
```

Run:

```bash
kimi-bridge wechat login
```

Open the printed authorization URL in WeChat and **scan and approve the iLink bot authorization**; enter a verification code only if WeChat explicitly requests one. On success, manually add the returned stable scanner identity to `wechat.allowed_users`; do not use a nickname, guessed account identifier, or bot identity. The credential is saved to `~/.kimi-bridge/wechat/credentials.json` (or the configured `storage_path`).

Then run:

```bash
kimi-bridge wechat status
kimi-bridge doctor
kimi-bridge
```

`status` checks local authorization and storage only; it does not test whether the remote authorization is still active. The allowlisted user sends `/status` and a normal prompt and confirms a complete private-chat round trip. One bot authorization can be polled by only one process; do not let another iLink polling process connect to it. WeChat receives images, voice, files, and videos and sends images, videos, and files; voice recognition is best-effort, and outbound audio is a generic file, not a native voice message. WeChat forces `auto` and has no approvals, questions, separate thinking stream, groups, or proactive delivery.

### Telegram: manual Bot API setup

Telegram has no QR, `login`, `status`, or `logout` control command in this project. Create a bot through the official Telegram Bot API and BotFather flow and obtain its token, then put the token under `[telegram].bot_token` and your numeric Telegram user ID under `[telegram].allowed_users`. `allowed_users` takes numeric IDs, not usernames or nicknames; confirm the real numeric ID locally.

```toml
platform = "telegram"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[telegram]
bot_token = "<token from BotFather>"
allowed_users = [123456789]
```

Telegram remains experimental and supports private chats only. After saving the config, run:

```bash
kimi-bridge doctor
kimi-bridge
```

Once the bridge runs in the foreground, the allowlisted user sends `/status`, then a normal prompt, and confirms the complete reply. Telegram takes over long polling at startup and discards pending updates.

## 5. TOML fallback and managed-credential precedence

Feishu and QQ also accept a complete TOML fallback. `app_id` and `app_secret` must be supplied together:

```toml
platform = "feishu" # or "qq"

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

Only the selected adapter is constructed. For Feishu and QQ, startup chooses credentials in this order:

1. If the managed `credentials.json` is absent, a complete TOML `app_id` + `app_secret` pair is used.
2. If the managed file is present and valid, it takes precedence. Feishu also uses the Feishu/Lark API domain stored with that credential.
3. If the managed file is present but unreadable, unsafe, or invalid, startup fails. It never silently falls back to TOML; repair it or run the matching `login --replace`.

`feishu logout` and `qq logout` remove only adapter-owned files under their configured storage path. A complete TOML fallback remains in `config.toml`. WeChat has no TOML credential fallback.

Keep configuration and credentials out of command lines, chat, issue reports, and version control. On Linux/macOS, the application creates managed directories/files with `700`/`600` permissions; on Windows, use the current user's ACL as shown above.

## 6. Diagnose and perform a real message round trip

```bash
kimi-bridge doctor
```

`kimi-bridge doctor` is the bridge's local diagnostic: it checks configuration, paths, the selected adapter's local credentials, and Kimi Code configuration, plus Feishu's FFmpeg or WeChat's encrypted-media dependency when applicable. It does not validate platform permissions, event subscriptions, network authorization, or message receiving and sending, so passing `doctor` is not proof of a working platform link.

Start in the foreground:

```bash
kimi-bridge
```

From an allowlisted chat account:

1. send `/status` and confirm a reply;
2. send a normal prompt, such as "Reply with OK only.", and confirm the complete answer arrives;
3. test files and voice as your use case requires; on Feishu, also complete one real approval or question.

## 7. Persistent operation

Foreground operation is fully supported and recommended for the first verification. Choose a persistent setup only after the real message round trip passes.

- **Linux only: systemd.** Adapt [the systemd user-unit template](docs/kimi-bridge.service) using the absolute paths from `command -v kimi-bridge` and `command -v kimi`. Review the unit, keep credentials out of it, and treat creating/enabling the service and enabling user lingering as separate administrative decisions.
- **macOS: launchd.** Start from a user-level LaunchAgent using the actual absolute paths for `kimi-bridge`, `kimi`, and the config. Loading a LaunchAgent and choosing its login-session behavior are macOS-specific operational decisions.
- **Windows: Task Scheduler.** Start from a task running under the same current user that owns the config and managed credential files. Use `Get-Command kimi-bridge` and `Get-Command kimi` to obtain absolute paths. Configure login, restart, and power behavior according to Windows policy.

After a persistent service starts, repeat `/status` and a normal prompt through the service. Keep one independent config, state path, workspace, and platform storage per process.

## 8. References

- [Configuration](docs/CONFIGURATION.md) (English)
- [Commands](docs/COMMANDS.md) (English)
- [QR onboarding](docs/QR_ONBOARDING.md) (English)
- [Architecture and compatibility](docs/ARCHITECTURE.md) (English)
- [Setup-agent guide](INSTALL_AI.md) (English)
