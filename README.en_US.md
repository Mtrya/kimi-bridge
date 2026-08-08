# kimi-bridge

**中文**: [README.md](README.md)

Control a local [Kimi Code](https://github.com/MoonshotAI/kimi-code) agent from an instant-messaging conversation.

kimi-bridge connects the local Kimi Code server to one chat platform, preserves chat-to-session bindings, and provides streaming replies, files, session controls, and supported interactive surfaces according to each platform's capabilities.

## Support

| Platform / capability | Status |
| --- | --- |
| Feishu, QQ, and WeChat private chats | Supported |
| Telegram private chats | Experimental |
| Linux, macOS, and Windows | Supported |
| Voice messages | Supported on Feishu, QQ, and WeChat |

Each bridge process selects exactly one platform adapter. Feishu, QQ, and WeChat all support local QR onboarding, but the three QR flows have different meanings; see [QR onboarding](docs/QR_ONBOARDING.md). WeChat is a supported QR-authorized private-chat bot. It forces `auto` mode and has no approvals, questions, separate thinking stream, groups, or proactive delivery; it accepts inbound image, voice, file, and video messages and sends outbound images, videos, and files.

## Quick start

First verify that `kimi` is the official Kimi Code executable, not the older Python `kimi-cli` product that uses the same command name:

```bash
kimi --version
kimi --help
kimi doctor config
kimi -p "Reply with OK only."
```

After Kimi Code completes a real reply, install the bridge. Do not run `doctor` immediately after installation: first follow the “Choose one platform and prepare private configuration” section of [INSTALL.en_US.md](INSTALL.en_US.md) to create `~/.kimi-bridge/config.toml` and select one platform.

```bash
uv tool install kimi-bridge
kimi-bridge --version
```

Complete setup for the selected platform before running diagnostics:

- Feishu, QQ, or WeChat: start with the platform's bootstrap config, run its `login` command, then complete platform-side setup and fill `allowed_users`;
- Telegram: follow the manual Bot API flow in the installation guide and set the bot token and numeric user ID under `[telegram]`.

Then run the local diagnostic and start the bridge in the foreground:

```bash
kimi-bridge doctor
kimi-bridge
```

One process runs one platform. For the first start, complete a real message round trip, such as `/status` followed by a normal prompt. This confirms platform permissions, event delivery, allowlisting, and replies before you configure persistent operation.

### QR control-command index

The first three platforms have QR control commands, but these are three different authorization flows, not three forms of the same login:

```bash
kimi-bridge feishu login       # Feishu/Lark application registration QR
kimi-bridge feishu status      # Inspect local managed credential storage
kimi-bridge feishu logout      # Remove adapter-owned local managed files

kimi-bridge qq login           # QQ official-bot credential binding QR
kimi-bridge qq status          # Inspect local managed credential storage
kimi-bridge qq logout          # Remove adapter-owned local managed files

kimi-bridge wechat login       # WeChat iLink bot authorization QR
kimi-bridge wechat status      # Inspect local managed credential storage
kimi-bridge wechat logout      # Remove adapter-owned local managed files
```

All three QR login commands support `--replace`. Their `status` commands inspect only local managed credential storage and do not verify the network; `logout` removes only adapter-owned managed files and does not remove a platform-side bot binding. Control commands do not start Kimi Code or message polling, and each command requires the config's `platform` to match the command's platform. Telegram has no QR, `login`, `status`, or `logout` control command in this project; configure its Bot API token and `allowed_users` manually.

The default managed credential paths are:

- Feishu: `~/.kimi-bridge/feishu/credentials.json`
- QQ: `~/.kimi-bridge/qq/credentials.json`
- WeChat: `~/.kimi-bridge/wechat/credentials.json`

Set `storage_path` in the corresponding `[feishu]`, `[qq]`, or `[wechat]` table to relocate it. Feishu and QQ prefer a valid managed credential; a complete TOML `app_id` plus `app_secret` pair is used only when the managed credential file is absent. A present but damaged managed file is an error and never silently falls back. WeChat QR credentials are never written to TOML.

For the complete human walkthrough, read [Install and operate](INSTALL.en_US.md). For QR details, read [QR onboarding](docs/QR_ONBOARDING.md). The configuration, chat-command, and architecture references are currently in English: [Configuration](docs/CONFIGURATION.md), [Commands](docs/COMMANDS.md), and [Architecture](docs/ARCHITECTURE.md).

## Features

- Durable Kimi session creation, listing, switching, renaming, inspection, compaction, and undo.
- Edit-in-place answer streaming, router-side chunking, and separate thinking output where the platform supports it.
- Interactive approvals and questions on adapters that can present them.
- Busy-turn prompt steering, cancellation, permission modes, model, effort, plan, goal, task, skill, and MCP controls.
- Inbound images, videos, files, and transcribed voice messages, plus workspace-contained outbound `/send`.
- Private-chat allowlists, loopback-only Kimi server supervision, and a local `doctor` diagnostic that starts no services.

## Commands

Chat commands include:

- sessions: `/new`, `/sessions`, `/switch`, `/status`, `/title`, `/usage`, `/compact`, `/undo`;
- control: `/mode`, `/model`, `/effort`, `/plan`, `/goal`, `/stop`, `/restart-server`;
- tasks and tools: `/tasks`, `/skills`, `/mcp`;
- output: `/send`, `/render-thinking`.

Use `/help` in chat or read the [command reference](docs/COMMANDS.md) for exact syntax and platform limits. Send `/<command> ?` for detailed in-chat usage.

## Architecture and security

```text
Feishu, QQ, WeChat, or experimental Telegram
                  │
                  ▼
             semantic chat router
                  │
                  ▼
           supervised local `kimi web`
```

The managed Kimi server binds only to loopback and uses a generated bearer token; adapter allowlists restrict chat access. kimi-bridge is designed for one trusted operator. An authorized Kimi agent can read, write, and execute with the host account's authority, so protect the host, configuration files, and chat credentials. See [Architecture and compatibility](docs/ARCHITECTURE.md) for component boundaries and the compatibility command.

## Development

```bash
uv sync --dev
uv run pytest -q
uv run ruff check .
```

## License

[MIT](LICENSE) © 2026 Mtrya
