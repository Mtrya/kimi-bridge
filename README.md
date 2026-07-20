# kimi-bridge

Bridge [Kimi Code CLI](https://github.com/MoonshotAI/kimi-cli) to instant messaging platforms — Feishu first, Telegram second — so you can drive a full kimi-code agent from a chat window.

**Status: the Feishu text-message MVP is implemented, including durable session bindings, streamed edit-in-place replies, and bridge commands. Interactive approvals, media, and Telegram are not implemented yet.** All major design decisions below are locked; the remaining open questions are deliberately small and listed at the bottom.

## Architecture

```
Feishu / Telegram
        │  (platform adapters, one per IM)
        ▼
   ChatRouter  ── bridge commands, session binding, event→message rendering
        │  (REST + a single WebSocket)
        ▼
  local `kimi server`  ── spawned and supervised by the bridge; the actual kimi-code harness
```

Unlike ACP-based bridges, this talks to kimi-code's own server API (`kimi server`, REST + WS), which exposes the full harness feature set: streaming deltas, approvals, `AskUserQuestion`, skills, plan mode, background tasks, subagents, prompt steering, and more. The server self-documents via `/openapi.json` and `/asyncapi.json` on its HTTP port.

## Decisions (locked)

- **Language**: Python ≥ 3.11, asyncio throughout, typed.
- **Backend**: local `kimi server` REST + WebSocket — not ACP — specifically to keep harness-specific features.
- **Deployment**: always-on host, single user, authZ via a chat-user allowlist. Designed so multi-user could be added later, but not built.
- **Server lifecycle**: the bridge spawns and supervises `kimi server run --foreground --keep-alive` as a child process, parses the bearer token from its startup output, restarts it on crash, and warns on `server_version` mismatch at startup (the API is 0.x).
- **Platforms**: Feishu first (official `lark-oapi` SDK, WebSocket long-connection — no public endpoint needed); Telegram second (`aiogram`, polling). WeChat is out of scope.
- **Sessions**: the shared kimi session store is fully exposed. Bridge commands: `/new [cwd]`, `/sessions`, `/switch <n|id>`, `/stop`, `/mode <manual|auto|yolo>`. Anything not starting with `/` goes to the bound session. Handoff between chat and terminal CLI (same session store, `kimi -S <id>`) is an advertised feature.
- **Workspaces**: auto-created sessions use a scratch workspace (`~/.kimi-bridge/workspace/`); real project work goes through `/new <path>`.
- **Streaming**: assistant text is rendered edit-in-place with throttled edits (~1 per 1.5–2s); messages are chunked at platform limits by the router, not the adapters. Thinking deltas are hidden. Tool-call rendering is an open experiment (see below).
- **Permissions**: per-session permission mode via `/mode`. Approvals and `AskUserQuestion` surface as interactive cards (Feishu cards, Telegram inline keyboards); unanswered interactions auto-deny after a timeout. Default becomes `manual` once cards exist; earlier bring-up uses `auto` with a loud warning.
- **Interrupt policy**: a new message during a running turn is submitted and steered into the active turn (delivered at the next step boundary, via `POST .../prompts:steer`). `/stop` aborts the turn. Steer is a nudge, not a brake — in-flight tool calls complete.
- **Media**: inbound images are passed as image content parts; inbound files are saved into the session workspace with their path referenced in the prompt text. No outbound media in v1.
- **State**: conversation→session mapping and per-chat settings persist to `~/.kimi-bridge/state.json` (atomic writes). Config at `~/.kimi-bridge/config.toml`.
- **Client**: thin hand-written `httpx` + `websockets` client; no vendored SDK. Replies come from the assistant event stream (`assistant.delta`), not from a SendMessage-tool trick.

## Open questions (intentionally small)

- **Tool-call rendering**: one-line status messages vs hidden — decided by experiment once a second platform exists; dropping them entirely remains an option.
- **Thinking-delta display**: hidden now; a toggle may appear if missed.
- **Outbound file sending** (`/send <path>`): likely v1.1, additive.
- **Multi-user**: allowlist already supports several users technically, but no isolation/rate-limit design has been done.
- **WeChat**: revisit only if a maintained transport appears; porting the iLink protocol from `references/wechat-acp` was rejected as a foundation.

## References (read-only)

- `references/hakimi` — concise TS bridge (koishi + kimi-agent-sdk). Good shape: router + session-per-user. Not used as code.
- `references/wechat-acp` — TS WeChat↔ACP bridge. Kept only for its WeChat protocol code, which is currently out of scope.

## Development

Python 3.11 or newer and an authenticated kimi-code installation are required. Run `kimi doctor` before starting the bridge, and ensure kimi-code's `config.toml` defines `default_model`.

```bash
uv sync --extra feishu
uv run pytest
uv run python scripts/smoke_server.py
```

The unit suite uses fake kimi-server, Feishu, WebSocket, state, and child-process dependencies. The smoke script checks the server boundary independently: it starts `kimi server` on an ephemeral loopback port, creates a temporary-workspace session, submits `Reply with exactly: PONG`, verifies streamed assistant deltas and a completed turn, and shuts the child down.

### Feishu app setup

Create a custom Feishu app, enable its bot capability, and make the app available to the intended user. In the developer console:

1. Grant tenant scopes `im:message.p2p_msg:readonly`, `im:message:send_as_bot`, and `im:message:update`.
2. Under Events and Callbacks, choose WebSocket long-connection mode and subscribe to `im.message.receive_v1` as the app.
3. Publish the app version so its permissions and event subscription take effect.

Feishu documents the [Python long-connection setup](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case) and [message permission prerequisites](https://open.feishu.cn/document/server-docs/im-v1/faq).

Create `~/.kimi-bridge/config.toml`:

```toml
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
edit_throttle_seconds = 1.5

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

The bridge accepts direct text messages only. Group messages and non-allowlisted users are ignored; allowlisted non-text messages receive an unsupported-message notice. Until interactive approval controls are implemented, every session and prompt uses `permission_mode = "auto"`. Startup emits a prominent warning because tool calls can run without approval.

Available commands are `/new [cwd]`, `/sessions`, `/switch <n|id>`, `/stop`, and `/help`. Conversation bindings are atomically persisted in `~/.kimi-bridge/state.json`.
