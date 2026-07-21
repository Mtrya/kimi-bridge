# kimi-bridge

Bridge [Kimi Code CLI](https://github.com/MoonshotAI/kimi-cli) to instant messaging platforms so you can drive a full kimi-code agent from a chat window.

**Status: the Feishu bridge is implemented, including durable session bindings, streamed edit-in-place replies, approval/question cards, prompt steering, inbound images/files, and bridge commands. The core message and interaction contracts are platform-neutral; additional platform adapters are deferred until their user experience is designed.** All major design decisions below are locked; remaining open questions are listed at the bottom.

## Architecture

```
Feishu / future adapters
        │  (one enabled platform adapter per process)
        ▼
   ChatRouter  ── bridge commands, session binding, event→message rendering
        │  (REST + a single WebSocket)
        ▼
  local `kimi web` server  ── spawned and supervised by the bridge; the actual kimi-code harness
```

Unlike ACP-based bridges, this talks to kimi-code's own local server API (`kimi web`, REST + WS), which exposes the full harness feature set: streaming deltas, approvals, `AskUserQuestion`, skills, plan mode, background tasks, subagents, prompt steering, and more. The server self-documents via `/openapi.json` and `/asyncapi.json` on its HTTP port.

The router deals only in platform-neutral conversation, actor, message, prompt, response, and outcome values. Each adapter owns its native rendering and callback decoding; for example, Feishu card JSON never enters the router. The runtime intentionally enables one adapter per process today rather than introducing multi-adapter lifecycle machinery before it is needed.

## Decisions (locked)

- **Language**: Python ≥ 3.11, asyncio throughout, typed.
- **Backend**: local kimi-code server REST + WebSocket — not ACP — specifically to keep harness-specific features.
- **Deployment**: always-on host, single user, authZ via a chat-user allowlist. Designed so multi-user could be added later, but not built.
- **Server lifecycle**: the bridge supports exactly kimi-code 0.28.1, verifies both `kimi --version` and `/api/v1/meta`, then supervises `kimi web --no-open --host 127.0.0.1 --port <port>` as a foreground child process. It parses the bearer token from startup output, restarts the child on crash, and rejects version mismatches because the API is 0.x.
- **Platforms**: Feishu is implemented with the official `lark-oapi` SDK and a WebSocket long connection, so no public endpoint is needed. The core contracts are platform-neutral, but the runtime enables one adapter per process. Telegram is deferred until its transport and interaction semantics are designed; no Telegram SDK is selected. WeChat is out of scope.
- **Sessions**: the shared kimi session store is fully exposed. Bridge commands: `/new [cwd]`, `/sessions`, `/switch <n|id>`, `/stop`, `/mode <manual|auto|yolo>`. Anything not starting with `/` goes to the bound session. Handoff between chat and terminal CLI (same session store, `kimi -S <id>`) is an advertised feature.
- **Workspaces**: auto-created sessions use a scratch workspace (`~/.kimi-bridge/workspace/`); real project work goes through `/new <path>`.
- **Streaming**: assistant text is rendered edit-in-place with throttled edits (~1 per 1.5–2s); messages are chunked at platform limits by the router, not the adapters. Thinking deltas are hidden. Tool-call rendering is an open experiment (see below).
- **Permissions**: per-session permission mode via `/mode`. Approvals and `AskUserQuestion` are platform-neutral interactions rendered by each adapter; Feishu uses interactive cards. Unanswered interactions auto-deny after a timeout. New sessions default to `manual`. `auto` is fully autonomous and never asks questions; `yolo` auto-approves regular tools but may still ask questions.
- **Interrupt policy**: a new message during a running turn is submitted and steered into the active turn (delivered at the next step boundary, via `POST .../prompts:steer`). `/stop` aborts the turn. Steer is a nudge, not a brake — in-flight tool calls complete.
- **Media**: inbound images are passed as image content parts; inbound files are saved into the session workspace with their path referenced in the prompt text. No outbound media in v1.
- **State**: conversation→session mapping and per-chat settings persist to `~/.kimi-bridge/state.json` (atomic writes). Config at `~/.kimi-bridge/config.toml`.
- **Client**: thin hand-written `httpx` + `websockets` client; no vendored SDK. Replies come from the assistant event stream (`assistant.delta`), not from a SendMessage-tool trick.

## Open questions

- **Telegram**: transport, identity/conversation mapping, edit behavior, and the degraded interaction experience must be designed before choosing an SDK or implementing an adapter.
- **Tool-call rendering**: one-line status messages vs hidden — decided by experiment once a second platform exists; dropping them entirely remains an option.
- **Thinking-delta display**: hidden now; a toggle may appear if missed.
- **Outbound file sending** (`/send <path>`): likely v1.1, additive.
- **Multi-user**: allowlist already supports several users technically, but no isolation/rate-limit design has been done.
- **WeChat**: revisit only if a maintained transport appears; porting the iLink protocol from `references/wechat-acp` was rejected as a foundation.

## References (read-only)

- `references/hakimi` — concise TS bridge (koishi + kimi-agent-sdk). Good shape: router + session-per-user. Not used as code.
- `references/wechat-acp` — TS WeChat↔ACP bridge. Kept only for its WeChat protocol code, which is currently out of scope.

## Development

Python 3.11 or newer and an authenticated kimi-code 0.28.1 installation are required. Run `kimi doctor` before starting the bridge, and ensure kimi-code's `config.toml` defines `default_model`. Other kimi-code versions are rejected because the managed-server command and 0.x API can change.

```bash
uv sync --extra feishu
uv run pytest
uv run python scripts/smoke_server.py
```

The unit suite uses fake kimi-server, Feishu, WebSocket, state, and child-process dependencies. The smoke script checks the server boundary independently: it starts foreground `kimi web` on an ephemeral loopback port, creates a session in the persistent `<default_workspace>/.smoke` directory, submits `Reply with exactly: PONG` in fully autonomous `auto` mode, verifies streamed assistant deltas and a completed turn, and shuts the child down. The stable workspace keeps smoke-created sessions valid in the shared session store.

### Feishu app setup

Create a custom Feishu app, enable its bot capability, and make the app available to the intended user. In the developer console:

1. Grant tenant scopes `im:message.p2p_msg:readonly`, `im:message:readonly`, `im:message:send_as_bot`, and `im:message:update`.
2. Under Events and Callbacks, choose WebSocket long-connection mode and subscribe to `im.message.receive_v1` as the app.
3. Enable card callbacks (`card.action.trigger`) in Callback Configuration. They use the same WebSocket long connection; no public endpoint is required.
4. Publish the app version so its permissions and event/callback configuration take effect.

Feishu documents the [Python long-connection setup](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case) and [message permission prerequisites](https://open.feishu.cn/document/server-docs/im-v1/faq).

Create `~/.kimi-bridge/config.toml`:

```toml
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
edit_throttle_seconds = 1.5
interaction_timeout_seconds = 600
inbox_subdir = ".kimi-bridge-inbox"

[kimi_server]
# Omit port to select a free ephemeral port.
# port = 58628

[feishu]
app_id = "cli_xxx"
app_secret = "replace-me"
# Feishu user IDs and open IDs are both accepted.
allowed_users = ["ou_xxx"]
```

Then start the bridge:

```bash
uv run kimi-bridge
```

The bridge accepts direct text, image, file, and rich-post image messages. Group messages and non-allowlisted users are ignored; other message types receive an unsupported-message notice. Images become base64 image prompt parts. Files are saved below `<session workspace>/.kimi-bridge-inbox/` (or the configured inbox subdirectory) and their absolute paths are included in the prompt.

New sessions default to `permission_mode = "manual"`. Approval and `AskUserQuestion` requests arrive as interactive cards; unanswered approvals are rejected and unanswered questions are dismissed after `interaction_timeout_seconds`. Card callbacks are accepted only from the allowlisted user and original conversation. Cards that outlive a bridge restart are marked stale when clicked.

Available commands are `/new [cwd]`, `/sessions`, `/switch <n|id>`, `/mode <manual|auto|yolo>`, `/stop`, and `/help`. A non-command message submitted while a turn is already active is queued and steered into that turn at the next step boundary. Conversation bindings and their permission modes are atomically persisted in `~/.kimi-bridge/state.json`.
