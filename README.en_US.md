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

- Durable Kimi sessions: create, switch, fork, archive, restore, inspect history, compact, and undo.
- Edit-in-place streaming replies with router-side chunking; a separate thinking stream, interactive approvals, and questions where the platform supports them.
- Native prompt queues, goal editing, busy model/effort changes, swarm controls, and agent/todo inspection; background work can report outcomes after you switch sessions.
- Read-only side questions through `/btw`, with answers separate from the main turn.
- Shared workspace folders, file browsing/search/preview/delivery, recorded changes and diffs, and selection of Kimi-configured execution environments.
- Inbound images, videos, files, and transcribed voice; plugin/MCP inspection, MCP restart requests, and available managed-account quota.
- Private-chat allowlists, loopback-only Kimi server supervision, and a local `doctor` diagnostic.

## Commands

Chat commands include:

- sessions: `/new`, `/archive`, `/sessions`, `/switch`, `/fork`, `/restore`, `/status`, `/title`, `/history`, `/usage`, `/compact`, `/undo`;
- control: `/mode`, `/model`, `/secondary-model`, `/effort`, `/plan`, `/swarm`, `/queue`, `/goal`, `/stop`, `/reload`;
- activity and tools: `/agents`, `/todos`, `/tasks`, `/btw`, `/skills`, `/plugins`, `/mcp`;
- workspace and output: `/workspace`, `/files`, `/environments`, `/render-thinking`.

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
