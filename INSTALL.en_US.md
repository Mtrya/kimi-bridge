# Install kimi-bridge

**中文**: [INSTALL.md](INSTALL.md)

kimi-bridge connects a local Kimi Code installation to one chat platform. The fastest path is in the [README quick start](README.en_US.md); this guide expands the same path step by step, including the platform-side follow-ups after authorization and final verification. You can also hand [INSTALL_AI.md](INSTALL_AI.md) to an AI assistant to perform the installation.

## Support

| Platform | Status | Notes |
| --- | --- | --- |
| Feishu | Supported | Full features: approval/question cards, separate thinking stream, voice messages |
| QQ | Supported | C2C private chat only; forces `auto`, no approvals, questions, or separate thinking stream |
| WeChat | Supported | Private chat only; forces `auto`, no approvals, questions, thinking stream, groups, or proactive delivery |
| Telegram | Experimental | Private chat only; manual Bot API configuration |

Linux, macOS, and Windows are supported.

## 1. Verify Kimi Code

Install the official [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started) and complete sign-in or provider configuration. The legacy Python `kimi-cli` also ships a command named `kimi` but is incompatible with this project — its `--help` has no `web` or `doctor` subcommands. Run:

```bash
kimi doctor config
kimi -p "Hello there"
```

The second command must return a normal answer before you continue.

## 2. Install kimi-bridge

If you do not have [uv](https://docs.astral.sh/uv/getting-started/installation/) yet, install it first, then install kimi-bridge with it:

```bash
uv tool install kimi-bridge
kimi-bridge --version
```

`kimi-bridge compat` classifies the installed Kimi Code version against the bridge's compatibility record.

## 3. Choose a platform and authorize

Feishu, QQ, and WeChat each have a `login` command; run it directly. If `~/.kimi-bridge/config.toml` does not exist, `login` creates it and writes the selected platform; when the flow returns a user identity, `login` merges it into the `allowed_users` allowlist. If an existing config selects a different platform, `login` asks whether to switch first.

### Feishu

```bash
kimi-bridge feishu login
```

Open the URL printed by the command in a browser. The page can create a new application or select an existing one, and pre-fills the tenant permissions, `im.message.receive_v1` event, and `card.action.trigger` callback the bridge needs; confirm as the page directs. This is application registration, not user OAuth. The resulting application credential is saved to `~/.kimi-bridge/feishu/credentials.json`, and the registering user's `open_id` is added to the allowlist automatically.

Afterward, confirm bot capability and the remaining console settings on the Feishu open platform, publish an app version, and confirm the intended user can reach the bot. Receiving voice messages requires `ffmpeg` on PATH.

### QQ

```bash
kimi-bridge qq login
```

Open the URL printed by the command, scan, and approve the official bot bind. On success the credential is saved to `~/.kimi-bridge/qq/credentials.json`, and the scanning user's `user_openid` is added to the allowlist automatically.

Afterward, complete the steps the QQ console currently requires — sandbox tester access, production review, event Intents — and make sure the bot is not claimed by another process. QQ forces `auto` and has no approval or question UI; that is not a misconfiguration.

### WeChat

```bash
kimi-bridge wechat login
```

Open the URL printed by the command in WeChat, scan, and approve the iLink bot authorization; enter a verification code only if WeChat explicitly asks for one. The credential is saved to `~/.kimi-bridge/wechat/credentials.json` and never written to the config file. The flow returns the scanning account's stable identity, and `login` adds it to `wechat.allowed_users` automatically; check that it is the intended user.

One bot authorization can be polled by exactly one process; do not let another iLink process connect to it. WeChat forces `auto`. It receives images, voice, files, and video, and sends images, videos, and files; outbound audio goes as a generic file.

### Telegram

Telegram has no `login` command. Create a bot through the official BotFather flow to obtain a token, then write it into `~/.kimi-bridge/config.toml`:

```toml
platform = "telegram"

[telegram]
bot_token = "<token from BotFather>"
allowed_users = [123456789]
```

`allowed_users` takes numeric user IDs, not usernames. At startup the Telegram adapter takes over long polling and discards pending updates.

## 4. Verify

```bash
kimi-bridge doctor
kimi-bridge
```

`doctor` checks local configuration, credentials, paths, and Kimi Code configuration; it does not verify platform-side permissions or the message path. After starting in the foreground, send `/status` and a normal message from an allowlisted account — only a complete reply means the installation is done. On Feishu, also exercise one real approval or question; on WeChat, first confirm no second process is polling the same authorization.

## 5. Persistent operation (optional)

Decide whether to run persistently only after the foreground round trip passes:

- **Linux**: start from the [systemd user-unit template](docs/kimi-bridge.service), filling in the absolute paths from `command -v kimi-bridge` and `command -v kimi`;
- **macOS**: configure a user-level `launchd` LaunchAgent with the actual absolute paths for `kimi-bridge`, `kimi`, and the config;
- **Windows**: create a current-user Task Scheduler task running under the same user account that owns the credential files.

Repeat the real `/status` verification after the service starts.

## Customization

- Hand-written TOML credentials (`app_id`/`app_secret` fallback), `storage_path`, voice transcription, and every configuration key: [Configuration](docs/CONFIGURATION.md);
- Behavior of the authorization control commands `status`, `logout`, and `login --replace`: [Commands](docs/COMMANDS.md);
- Running several platforms at once: give each platform its own process, config, state file, workspace, and bot resources;
- Installation by an AI assistant: [INSTALL_AI.md](INSTALL_AI.md).
