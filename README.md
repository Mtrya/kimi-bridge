# kimi-bridge

Bridge [Kimi Code CLI](https://github.com/MoonshotAI/kimi-cli) to instant messaging platforms so you can drive a full kimi-code agent from a chat window.

**Status: the Feishu bridge is implemented and live-validated. The experimental Telegram adapter is implementation-complete and fake-tested; live Telegram validation remains pending. Both adapters support durable session bindings, streamed edit-in-place replies, approvals/questions, prompt steering, inbound media, session control and inspection, and bridge commands through platform-neutral core contracts.** All major design decisions below are locked; remaining open questions are listed at the bottom.

## Architecture

```
Feishu / experimental Telegram
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
- **Platforms**: Feishu is implemented with the official `lark-oapi` SDK and a WebSocket long connection, so no public endpoint is needed. The runtime enables one adapter per process, selected by top-level configuration that defaults to Feishu; both platform credential tables may coexist, but only the selected one is validated and constructed. WeChat is out of scope.
- **Telegram**: the experimental adapter uses a narrow handwritten `httpx` Bot API client with private-chat long polling, stable numeric user-ID allowlisting, and no Telegram SDK. Startup discards queued updates instead of replaying instructions received while the bridge was offline. Text streams through one persistent message plus edits. Approvals use inline keyboards; questions use a sequential one-message wizard with immediate single choice, toggle-and-Done multi-select, per-question Skip, and explicit `ForceReply` for custom answers. Wizard state is memory-only, so old callbacks become stale after restart. The implementation is fake-tested; credentials are currently unavailable for live validation.
- **Sessions**: the shared kimi session store is fully exposed. Commands cover creation/switching, permission mode, model and thinking effort, plan mode, status/title/usage, background tasks, skill activation, read-only session-derived MCP inspection, context compaction, counted undo, and the public goal lifecycle. Anything not starting with `/` goes to the bound session. Handoff between chat and terminal CLI (same session store, `kimi -S <id>`) is an advertised feature.
- **Workspaces**: auto-created sessions use a scratch workspace (`~/.kimi-bridge/workspace/`); real project work goes through `/new <path>`.
- **Streaming**: assistant text is rendered edit-in-place with throttled edits (~1 per 1.5–2s); messages are chunked at platform limits by the router, not the adapters. Thinking deltas are hidden. Tool-call rendering is an open experiment (see below).
- **Permissions**: per-session permission mode via `/mode`. Approvals and `AskUserQuestion` are platform-neutral interactions rendered by each adapter; Feishu uses interactive cards and Telegram uses inline-keyboard flows. Unanswered interactions auto-deny after one fixed timeout for the whole request. New sessions default to `manual`. `auto` is fully autonomous and never asks questions; `yolo` auto-approves regular tools but may still ask questions.
- **Interrupt policy**: a new message during a running turn is submitted and steered into the active turn (delivered at the next step boundary, via `POST .../prompts:steer`). `/stop`, `/goal pause`, and `/goal cancel` abort the relevant active turn and close any pending approval or question as cancelled. Steer is a nudge, not a brake — in-flight tool calls complete.
- **Media**: inbound images are passed as image content parts; inbound files are saved into the session workspace with their path referenced in the prompt text. Telegram deliberately supports one photo or one document with an optional caption, rejects albums and other media, and enforces the Bot API's 20 MB download limit. No outbound media in v1.
- **State**: conversation→session mapping and bridge-owned per-chat settings persist to `~/.kimi-bridge/state.json` (atomic writes). Model, thinking effort, plan mode, and goal state remain authoritative in Kimi's public session surfaces; the bridge does not duplicate them in its state. Config is at `~/.kimi-bridge/config.toml`.
- **Client**: thin hand-written `httpx` + `websockets` client; no vendored SDK. Replies come from the assistant event stream (`assistant.delta`), not from a SendMessage-tool trick.

## Open questions

- **Tool-call rendering**: one-line status messages vs hidden — considered only after the second adapter is implemented and live-tested; dropping them entirely remains an option.
- **Thinking-delta display**: hidden now; a toggle may appear if missed.
- **Outbound file sending** (`/send <path>`): likely v1.1, additive.
- **Multi-user**: allowlist already supports several users technically, but no isolation/rate-limit design has been done.
- **WeChat**: revisit only if a maintained transport appears; porting the iLink protocol from `references/wechat-acp` was rejected as a foundation.

## References (read-only)

- `references/hakimi` — concise TS bridge (koishi + kimi-agent-sdk). Good shape: router + session-per-user. Not used as code.
- `references/wechat-acp` — TS WeChat↔ACP bridge. Kept only for its WeChat protocol code, which is currently out of scope.

## Development

Python 3.11 or newer and an authenticated kimi-code 0.28.1 installation are required. Run `kimi doctor` before starting the bridge, and ensure kimi-code's `config.toml` defines `default_model`. Other kimi-code versions are rejected because the managed-server command and 0.x API can change. Use `uv sync` for the core and experimental Telegram adapter, or `uv sync --extra feishu` when running Feishu.

```bash
uv run pytest
uv run python scripts/smoke_server.py
```

The unit suite uses fake kimi-server, Feishu, Telegram Bot API, WebSocket, state, and child-process dependencies. The smoke script checks the server boundary independently: it starts foreground `kimi web` on an ephemeral loopback port, creates a session in the persistent `<default_workspace>/.smoke` directory, submits `Reply with exactly: PONG` in fully autonomous `auto` mode, verifies streamed assistant deltas and a completed turn, and shuts the child down. The stable workspace keeps smoke-created sessions valid in the shared session store.

### Feishu app setup

Create a custom Feishu app, enable its bot capability, and make the app available to the intended user. In the developer console:

1. Grant tenant scopes `im:message.p2p_msg:readonly`, `im:message:readonly`, `im:message:send_as_bot`, and `im:message:update`.
2. Under Events and Callbacks, choose WebSocket long-connection mode and subscribe to `im.message.receive_v1` as the app.
3. Enable card callbacks (`card.action.trigger`) in Callback Configuration. They use the same WebSocket long connection; no public endpoint is required.
4. Publish the app version so its permissions and event/callback configuration take effect.

Feishu documents the [Python long-connection setup](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case) and [message permission prerequisites](https://open.feishu.cn/document/server-docs/im-v1/faq).

Create `~/.kimi-bridge/config.toml`:

```toml
platform = "feishu"
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

Available commands are:

- `/new [cwd]`, `/sessions`, `/switch <n|id>`, `/mode <manual|auto|yolo>`, `/stop`, and `/help` for bridge and session lifecycle.
- `/model [alias]`, `/effort [effort]`, and `/plan [on|off]` for session-profile controls. Model aliases and effort values are exact and come from the live model catalog; bare forms only report state. Changing the model preserves the current effort when supported, otherwise it uses that model's advertised default effort or `off` when thinking is unsupported.
- `/status`, `/title [text]`, and `/usage` for session inspection and renaming. Session token totals come from live server events and context occupancy comes from session status; unavailable values are reported as unknown. Account plan quota and reset times are not exposed by the public `kimi web` API.
- `/tasks [running|completed|failed|cancelled]`, `/tasks show <id>`, and `/tasks cancel <id>` for public background-task inspection and cancellation. Task detail requests at most an 8 KiB output tail.
- `/skills` and `/skills run <name> [args]` for exact-name skill listing and activation. Activation produces a normal streamed model turn.
- `/mcp` for a read-only view of MCP tools resolved for the bound session. It does not expose global MCP restart or mutation.
- `/compact` for manual context compaction. One progress message is edited in place with the correlated WebSocket result, including compacted prompt count and token totals before and after; unrelated automatic compactions do not update it.
- `/undo [count]` for immediate public history undo, defaulting to one step. Counts must be positive decimal integers, and Kimi remains authoritative for undo availability and compaction boundaries.
- `/goal`, `/goal status`, `/goal <objective>`, `/goal pause`, `/goal resume`, and `/goal cancel` for the public goal lifecycle. Goal creation sets the objective through the session profile and submits it as a normal turn under the current permission mode. A blocked goal must be explicitly reactivated with `/goal resume`; ordinary follow-up messages do not reactivate it. Objectives beginning with `status`, `pause`, `resume`, or `cancel` use `/goal -- <objective>`. Goal replacement and next-goal queues are not exposed.

Read commands, including goal status, remain available while a turn is busy. `/title <text>`, `/tasks cancel <id>`, `/mode` changes, `/goal pause`, and `/goal cancel` are also allowed while busy; model, effort, plan, skill activation, compact, undo, goal creation, and goal resume reject instead of queueing. A mode change during a goal affects subsequent permission checks but does not resolve an approval or question that has already been issued. A non-command message submitted while a turn is already active is queued and steered into that turn at the next step boundary. Conversation bindings and permission modes are atomically persisted in `~/.kimi-bridge/state.json`; model, effort, plan, and goal state remain authoritative in Kimi's public session surfaces.

### Telegram bot setup (experimental)

Create a bot with BotFather and obtain the stable numeric Telegram user ID that should be authorized. Telegram usernames are not authorization identities. Configure the same file with Telegram selected:

```toml
platform = "telegram"
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
edit_throttle_seconds = 1.5
interaction_timeout_seconds = 600
inbox_subdir = ".kimi-bridge-inbox"

[kimi_server]
# Omit port to select a free ephemeral port.
# port = 58628

[telegram]
bot_token = "replace-me"
allowed_users = [123456789]
```

The two platform tables may coexist; only the selected platform is started. Telegram uses private-chat long polling and deliberately drops pending updates at startup, so commands sent while the bridge is offline are not replayed. Groups, channels, topics, bots, and non-allowlisted users are ignored.

The Telegram adapter accepts plain text, one native photo, or one document with an optional caption. It rejects albums and other media and enforces the hosted Bot API's 20 MB download ceiling. Approvals use inline buttons. Questions advance through one editable message; multi-select requires Done, Other requires an explicit reply to the generated prompt, and `/stop` cancels the active turn and interaction. Old buttons become stale after restart.

Start the bridge with `uv run kimi-bridge`. The Telegram adapter is implementation-complete and covered by fake Bot API tests, but its real delivery, edit cadence, callback UX, and file transfer remain unverified until credentials are available.
