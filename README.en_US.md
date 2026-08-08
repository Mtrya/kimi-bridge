# kimi-bridge

**中文**: [README.md](README.md)

Control a local [Kimi Code](https://github.com/MoonshotAI/kimi-code) agent from an instant-messaging conversation.

kimi-bridge supervises Kimi Code's local server, keeps chat-to-session bindings across restarts, renders model replies according to each platform's capabilities, and brings steering, files, session controls, and supported interactive surfaces into your chat client.

## Support

| Surface | Status |
| --- | --- |
| Feishu, WeChat, and QQ direct messages | Supported |
| Telegram private chats | Experimental |
| Linux, macOS, and Windows | Supported |
| Voice messages | Supported on Feishu, QQ, and WeChat |

Only one adapter runs in each bridge process. Feishu uses the official `lark-oapi` WebSocket client. Telegram, QQ, and WeChat use small handwritten `httpx`/`websockets` transports without a platform SDK dependency.

WeChat support is private-chat and QR-authorized, and was live-validated on 2026-08-08 against Tencent tag `v2.4.6`. It forces `auto`, sends immutable replies, and has no approvals, questions, separate thinking stream, groups, or proactive delivery. It accepts inbound image/voice/file/video and sends outbound image/video/file; outbound audio is a generic downloadable file rather than a native voice message. One bot authorization must have exactly one poller.

## Features

- Durable Kimi session creation, listing, switching, renaming, inspection, compaction, and undo.
- Edit-in-place answer streaming, router-side chunking, and optional separate thinking output.
- Interactive approvals and questions on adapters that can present them, with timeout handling and stale-action protection.
- Busy-turn prompt steering, cancellation, permission modes, model/effort/plan controls, goals, tasks, skills, and read-only MCP inspection.
- Inbound images, videos, files, and transcribed voice messages plus workspace-contained outbound `/send`.
- Private-chat allowlists, loopback-only Kimi server supervision, and a secret-safe non-starting doctor command.

## Quick start

The easiest path: open any CLI agent and say:
```text
Read https://github.com/Mtrya/kimi-bridge/blob/main/INSTALL_AI.md and help me configure kimi-bridge.
```
The agent interviews you and runs the setup end to end.

Manual skeleton — requires authenticated [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started) and [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv tool install 'kimi-bridge'     # install every adapter dependency
# create ~/.kimi-bridge/config.toml for one adapter, chmod 600
kimi-bridge doctor                # validate without starting anything
kimi-bridge                       # run
```

Full walkthrough in [INSTALL.en_US.md](INSTALL.en_US.md); complete adapter examples in the [configuration reference](docs/CONFIGURATION.md).

## Commands

Commands cover:

- sessions: `/new`, `/sessions`, `/switch`, `/status`, `/title`, `/usage`, `/compact`, `/undo`;
- control: `/mode`, `/model`, `/effort`, `/plan`, `/goal`, `/stop`, `/restart-server`;
- tasks and tools: `/tasks`, `/skills`, `/mcp`;
- output: `/send`, `/render-thinking`.

Use `/help` in chat or read the [command reference](docs/COMMANDS.md) for exact grammar, busy-session behavior, and platform media semantics. Send `/<command> ?` for detailed in-chat usage, including sub-forms such as `/tasks show ?`.

## Architecture and security

```text
Feishu, QQ, experimental Telegram, or experimental WeChat
              │
              ▼
       semantic chat router
              │
              ▼
  supervised local `kimi web`
```

The managed Kimi server binds to loopback with a generated bearer token, and chat access is restricted by the adapter's allowlist. kimi-bridge is designed for one trusted operator, not mutually untrusted tenants: a permitted Kimi agent can read, write, and execute with the host account's authority, so protect both the host and chat credentials. Component boundaries and the Kimi Code compatibility policy (`kimi-bridge compat`) live in [Architecture](docs/ARCHITECTURE.md).

## Documentation

- [Install and operate](INSTALL.en_US.md)
- [Agent-driven setup](INSTALL_AI.md)
- [Configure](docs/CONFIGURATION.md)
- [Commands and interactions](docs/COMMANDS.md)
- [Architecture and compatibility](docs/ARCHITECTURE.md)
- [Upstream Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started)
- [Report an issue](https://github.com/Mtrya/kimi-bridge/issues)

## Development

```bash
uv sync --dev
uv run pytest -q
uv run ruff check .
```

Unit tests use fake Kimi, Feishu, Telegram, QQ, WeChat, WebSocket, state, and process boundaries; hosted checks use no credentials or inference.

## License

[MIT](LICENSE) © 2026 Mtrya
