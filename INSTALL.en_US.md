# Install kimi-bridge

**中文**: [INSTALL.md](INSTALL.md)

kimi-bridge connects one local Kimi Code installation to one chat platform. This human installation and operations guide follows the safe order: verify Kimi Code, choose one platform, complete a real foreground message round trip, and only then decide whether to run the bridge persistently.

## Support

| Platform | Status | Important limitations |
| --- | --- | --- |
| Feishu | Supported | Requires FFmpeg, bot capability, platform permissions, event subscriptions, a published app version, and an allowlist |
| QQ | Supported | C2C private chat only; forced `auto` mode with no approvals, questions, or separate thinking stream |
| WeChat | Supported | QR-authorized private chat; one bot authorization can have only one polling process; forced `auto` with no approvals, questions, separate thinking stream, groups, or proactive delivery |
| Telegram | Experimental | Private chats only; startup takes over long polling and drops pending updates |

Linux, macOS, and Windows are intentionally supported. Each kimi-bridge process selects exactly one `platform`. To run multiple platforms, use separate processes, configuration files, state files, workspaces, and bot resources.

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

These terminal controls operate only the selected platform's authorization control plane. They do not start Kimi Code or message polling. `login` connects to the platform authorization service and waits for the QR flow; `status` checks local files only and does not verify the network; `logout` removes only adapter-owned managed files. Each command requires the config's `platform` to match the command platform.

| Platform | Command | Authorization method |
| --- | --- | --- |
| Feishu | `kimi-bridge feishu login` | Official Feishu/Lark application registration; returns application `client_id`/`client_secret`, not a user OAuth token |
| QQ | `kimi-bridge qq login` | Official QQ bot credential binding; returns `bot_appid` and a locally decrypted AppSecret, not QQ user login or OAuth |
| WeChat | `kimi-bridge wechat login` | WeChat iLink bot authorization; stores the QR credential outside TOML |
| Telegram | No QR control command | Create a bot through the official Telegram Bot API/BotFather flow, then configure its token and numeric user ID in TOML |

If managed authorization already exists, plain `login` refuses to overwrite it. Use `login --replace` when you intentionally want a new authorization. The three QR platforms provide matching `status` and `logout` commands; add `--config PATH` when using a non-default file. Telegram has no project `login`, `status`, or `logout` control command.

### Feishu: application-registration QR

Start with a bootstrap config that names the selected platform and leaves the sender allowlist empty. QR registration is a separate authorization step and does not silently populate the allowlist.

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

Open the URL printed by the command in a browser, then follow the page instructions to scan with or approve in Feishu/Lark. The result is an application `client_id` and `client_secret`, saved to `~/.kimi-bridge/feishu/credentials.json` (or the configured `storage_path`). The managed record also preserves whether the tenant is Feishu or Lark and the API domain to use.

QR registration does not enable the bot capability, grant the permissions needed for message prompts/resources/voice recognition, subscribe to events, publish an app version, or add a bridge user to `feishu.allowed_users`. In the platform console, confirm the requirements for the features you will use, including `im.message.receive_v1` and `card.action.trigger` event subscriptions with long-connection delivery, then publish the app and confirm the intended user's availability. Obtain the user's `open_id` in the same app and tenant context and enter it manually in `feishu.allowed_users`. Do not claim that scanning completed any of these platform-side steps. Feishu inbound voice also requires `ffmpeg` on PATH.

After those steps, replace the empty array with the real identity (the value below is a location marker, not a value to copy literally):

```toml
[feishu]
allowed_users = ["<real open_id from this app and tenant>"]
```

Check locally and test in the foreground:

```bash
kimi-bridge feishu status
kimi-bridge doctor
kimi-bridge
```

The allowlisted user must send `/status` and a normal prompt, and you must confirm a complete reply before moving to persistent operation.

### QQ: official-bot credential-bind QR

Prepare a config with an empty allowlist only for the bootstrap phase:

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

Open the printed QQ URL and **scan and approve the official-bot bind flow**. The completed result contains `bot_appid` and encrypted `bot_encrypt_secret`; the bridge decrypts the secret only in memory and stores the final AppSecret with the app ID at `~/.kimi-bridge/qq/credentials.json` (or the configured `storage_path`). The temporary key, task, QR URL, and encrypted blob are not persisted. Runtime continues to use the existing QQ REST/token/WebSocket transport.

If the flow returns a scanner `user_openid`, manually copy it into `qq.allowed_users`. This is an app-scoped identifier for that bot, not a QQ number, nickname, or display name. Never convert it to one of those values. QR success does not by itself establish sandbox tester access, production review, authorization for the required event Intents, or a complete message path; complete any current QQ platform requirements and ensure no other polling process uses the bot.

After the flow succeeds, replace the empty array with the returned identity (the value below is a location marker, not a value to copy literally):

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

The allowlisted user must send `/status` and a normal prompt and receive a complete C2C reply. QQ always uses `auto`; missing approval or question UI is an intentional platform limitation.

### WeChat: iLink bot QR authorization

Prepare a config with an empty allowlist only during QR bootstrap. The QR credential is never placed in TOML.

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

Open the printed authorization URL in WeChat and **scan and approve the iLink bot authorization**. Enter a verification code only if WeChat explicitly requests one. On success, manually copy the returned stable scanner identity into `wechat.allowed_users`; do not use a nickname, guessed account identifier, or bot identity. The credential is saved to `~/.kimi-bridge/wechat/credentials.json` (or the configured `storage_path`) and is not written to TOML.

Then run:

```bash
kimi-bridge wechat status
kimi-bridge doctor
kimi-bridge
```

`status` checks local authorization and storage only; it does not test whether the remote authorization is still active. The allowlisted user must send `/status` and a normal prompt for the real test. One bot authorization can have only one polling process. WeChat supports private chats, inbound images/voice/files/videos, and outbound images/videos/files. It forces `auto`, has immutable replies, and does not provide approvals, questions, separate thinking output, groups, proactive delivery, or native outbound voice messages.

### Telegram: manual Bot API setup

Telegram has no QR, `login`, `status`, or `logout` control command in this project. Use the official Telegram Bot API and BotFather flow to create a bot and obtain its token. Put the token under `[telegram].bot_token`, and put your numeric Telegram user ID under `[telegram].allowed_users`; use a numeric user ID, not a username or display name.

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

Once the bridge is running in the foreground, send the chat command `/status`, then a normal prompt, and confirm the complete reply. Telegram takes over long polling at startup and drops pending updates; it has no project QR control command.

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

Run the local bridge diagnostic that starts no services:

```bash
kimi-bridge doctor
```

This command checks local configuration, paths, selected-adapter credential presence, Kimi Code configuration, Feishu's FFmpeg prerequisite when selected, and WeChat's encrypted-media dependency when selected. It does not validate platform permissions, event subscriptions, network authorization, inbound messages, or outbound messages. Passing `kimi-bridge doctor` is not proof of a working platform integration.

Start in the foreground:

```bash
kimi-bridge
```

From an allowlisted chat account:

1. send `/status` and confirm a reply;
2. send a normal prompt and confirm that the complete answer arrives;
3. test the media types your use case needs;
4. on Feishu, exercise an approval or question if your workflow uses them.

Do not move to persistent operation until this real message round trip passes.

## 7. Persistent operation

Foreground operation is fully supported and should be used for the first real test.

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
