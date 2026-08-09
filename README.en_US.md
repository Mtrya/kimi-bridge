# kimi-bridge

**中文**: [README.md](README.md)

Control a local [Kimi Code](https://github.com/MoonshotAI/kimi-code) agent from an instant-messaging conversation.

kimi-bridge connects the local Kimi Code server to one chat platform, preserves chat-to-session bindings, and provides streaming replies, files, session controls, and interactive features according to each platform's capabilities.

## Support

| Platform / capability | Status |
| --- | --- |
| Feishu, QQ, and WeChat private chats | Supported |
| Telegram private chats | Experimental |
| Linux, macOS, and Windows | Supported |
| Inbound voice messages | Supported on Feishu, QQ, and WeChat |
| Approval and question cards | Feishu only |

## Quick start

First install the official [Kimi Code](https://www.kimi.com/code/docs/kimi-code-cli/guides/getting-started.html) (not the same-named legacy Python `kimi-cli`), then confirm Kimi Code is configured and can complete a prompt:

```bash
kimi --version
kimi doctor config
kimi -p "Hello there"
```

Once Kimi Code works, install kimi-bridge:

```bash
uv tool install kimi-bridge
kimi-bridge --version
```

Then log in on the platform you use most:

```bash
kimi-bridge {feishu, qq, wechat} login
```

During setup you may need to open a web link or scan a QR code to authorize. Once authorization succeeds, start the bridge:

```bash
kimi-bridge
```

You can now chat with Kimi from the messaging platform.

Or let an AI assistant do the whole setup: open any CLI agent and say:

```text
Read https://github.com/Mtrya/kimi-bridge/blob/main/INSTALL_AI.md and help me configure kimi-bridge.
```

The agent interviews you and runs the setup end to end.

For more detailed installation steps, platform-side setup, or customization, see [Install and operate](INSTALL.en_US.md). References: [Configuration](docs/CONFIGURATION.md), [Commands](docs/COMMANDS.md), [Architecture](docs/ARCHITECTURE.md).

## Features

- Durable Kimi session management: create, list, switch, rename, inspect, compact, and undo.
- Edit-in-place streaming replies with router-side chunking; a separate thinking stream, interactive approvals, and questions where the platform supports them.
- Prompt steering and cancellation during busy turns, plus permission mode, model, effort, plan, goal, task, skill, and MCP inspection.
- Inbound images, videos, files, and transcribed voice, plus `/send` for outbound files.
- Private-chat allowlists, loopback-only Kimi server supervision, and a local `doctor` diagnostic.

## Commands

Chat commands include:

- sessions: `/new`, `/sessions`, `/switch`, `/status`, `/title`, `/usage`, `/compact`, `/undo`;
- control: `/mode`, `/model`, `/effort`, `/plan`, `/goal`, `/stop`, `/restart-server`;
- tasks and tools: `/tasks`, `/skills`, `/mcp`;
- output: `/send`, `/render-thinking`.

Type `/help` in chat or `/<command> ?` for usage; see the [command reference](docs/COMMANDS.md) for exact syntax and platform limits.

## Architecture and security

```text
Feishu, QQ, WeChat, or experimental Telegram
                  │
                  ▼
             chat router
                  │
                  ▼
           local `kimi web`
```

The supervised Kimi server binds only to loopback and uses a generated bearer token; adapter allowlists restrict chat access. kimi-bridge is designed for one trusted operator; an authorized Kimi agent can read, write, and execute with the host account's authority, so protect the host, configuration files, and chat credentials. See [Architecture and compatibility](docs/ARCHITECTURE.md) for component boundaries and the Kimi Code compatibility command.

## Development

```bash
uv sync --dev
uv run pytest -q
uv run ruff check .
```

## License

[MIT](LICENSE) © 2026 Mtrya
