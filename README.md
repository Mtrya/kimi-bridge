# kimi-bridge

Control a local [Kimi Code](https://github.com/MoonshotAI/kimi-code) agent from an instant-messaging conversation.

kimi-bridge supervises Kimi Code's local server, keeps chat-to-session bindings across restarts, streams editable replies, and brings approvals, questions, steering, files, thinking output, and session controls into your chat client.

## Support

| Surface | Status | Validation |
| --- | --- | --- |
| Feishu direct messages | Supported | Live-validated end to end |
| Telegram private chats | Experimental | Fake Bot API tests only; not live-validated |
| Linux, Python ≥3.11 | Supported | Hosted tests on 3.11 and 3.13 |
| macOS and Windows | Not currently supported | — |

Only one adapter runs in each bridge process. Feishu uses the official `lark-oapi` WebSocket client. Telegram uses a small handwritten `httpx` Bot API transport without a Telegram framework.

## Features

- Durable Kimi session creation, listing, switching, renaming, inspection, compaction, and undo.
- Edit-in-place answer streaming, router-side chunking, and optional separate thinking output.
- Interactive approvals and questions with timeout handling and stale-action protection.
- Busy-turn prompt steering, cancellation, permission modes, model/effort/plan controls, goals, tasks, skills, and read-only MCP inspection.
- Inbound images and files plus workspace-contained outbound `/send`.
- Private-chat allowlists, loopback-only Kimi server supervision, and a secret-safe non-starting doctor command.

## Quick start

Install and authenticate official [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started), then install [uv](https://docs.astral.sh/uv/getting-started/installation/) and kimi-bridge.

For Feishu:

```bash
uv tool install 'kimi-bridge[feishu]'
```

For the experimental Telegram adapter:

```bash
uv tool install kimi-bridge
```

Create `~/.kimi-bridge/config.toml` for one adapter, protect it with `chmod 600`, and validate the installation without starting the server or connecting to chat:

```bash
kimi-bridge doctor
```

Then run:

```bash
kimi-bridge
```

Start with the [installation runbook](INSTALL.md), especially when asking a coding agent to configure the bridge. The [configuration reference](docs/CONFIGURATION.md) contains complete Feishu and Telegram examples.

## Commands

Commands cover:

- sessions: `/new`, `/sessions`, `/switch`, `/status`, `/title`, `/usage`, `/compact`, `/undo`;
- control: `/mode`, `/model`, `/effort`, `/plan`, `/goal`, `/stop`;
- tasks and tools: `/tasks`, `/skills`, `/mcp`;
- output: `/send`, `/render-thinking`.

Use `/help` in chat or read the [command reference](docs/COMMANDS.md) for exact grammar, busy-session behavior, and platform media semantics.

## Architecture and security

```text
Feishu or experimental Telegram
              │
              ▼
       semantic chat router
              │
              ▼
  supervised local `kimi web`
```

The Kimi client owns all REST, WebSocket, version, and process-lifecycle details. The router owns platform-neutral session and interaction behavior. Each adapter owns its native transport and UI payloads. See [Architecture](docs/ARCHITECTURE.md) for the full boundary.

The managed Kimi server binds to loopback and uses its generated bearer token. Chat access is restricted by the selected adapter's allowlist, but kimi-bridge is designed for one trusted operator, not mutually untrusted tenants. A permitted Kimi agent can read, write, and execute within the authority of the host account, so protect both the host and chat credentials.

Tested Kimi Code versions are recorded in a packaged compatibility manifest. An unlisted official version emits a loud warning and is attempted against the live contract; legacy Python `kimi-cli`, an unrecognized product, or an executable/server version mismatch fails. Run `kimi-bridge doctor` after every Kimi or bridge upgrade.

## Documentation

- [Install and operate](INSTALL.md)
- [Configure](docs/CONFIGURATION.md)
- [Commands and interactions](docs/COMMANDS.md)
- [Architecture and compatibility](docs/ARCHITECTURE.md)
- [Upstream Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started)
- [Report an issue](https://github.com/Mtrya/kimi-bridge/issues)

## Development

```bash
uv sync --all-extras --dev
uv run pytest -q
uv run ruff check .
uv run python scripts/check_docs.py
uv run python scripts/smoke_server.py
```

Unit tests use fake Kimi, Feishu, Telegram, WebSocket, state, and process boundaries. The smoke script is the explicit authenticated local-server check; hosted checks use no credentials or inference.

## License

[MIT](LICENSE) © 2026 Mtrya
