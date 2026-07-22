# Commands and interactions

Commands are case-insensitive before the first space; arguments retain their case. Any message not beginning with a recognized slash command is submitted to the bound Kimi session. If no session is bound, the bridge creates one in `default_workspace` and uses the message as its first prompt.

## Exact command grammar

| Command | Behavior |
| --- | --- |
| `/help` | Show in-chat command help. |
| `/new [cwd]` | Create and bind a session. Without `cwd`, use the configured default workspace. |
| `/sessions` | List recent Kimi sessions and remember their displayed indices for `/switch`. |
| `/switch <n\|id>` | Bind a displayed one-based index or an explicit session ID. |
| `/status` | Show session ID, workspace, busy state, pending interaction, model, effort, plan mode, permission mode, and Kimi Code version. |
| `/title [text]` | Show the current title or rename the session. |
| `/usage` | Show live input, output, cache-read, cache-creation, and context-window token values when exposed by Kimi. |
| `/compact` | Start context compaction and edit one progress message with correlated event metrics. |
| `/undo [count]` | Undo a positive number of history steps; default `1`. Kimi enforces history and compaction boundaries. |
| `/mode <manual\|auto\|yolo>` | Set the session permission mode. |
| `/model [alias]` | Show the current model and exact catalog aliases, or select an alias. |
| `/effort [effort]` | Show current/valid efforts, or set one advertised for the active model. |
| `/plan [on\|off]` | Show or explicitly set plan mode. |
| `/goal` or `/goal status` | Show the public goal state and budgets. |
| `/goal <objective>` | Create a goal and submit its objective as a normal turn. |
| `/goal -- <objective>` | Create an objective beginning with `status`, `pause`, `resume`, or `cancel`. |
| `/goal pause` | Pause the current goal and cancel its active prompt/interaction. |
| `/goal resume` | Reactivate a paused or blocked goal. |
| `/goal cancel` | Cancel the current goal and its active prompt/interaction. |
| `/stop` | Abort the active prompt and cancel its pending interaction. |
| `/tasks [running\|completed\|failed\|cancelled]` | List all tasks or filter by status. |
| `/tasks show <id>` | Inspect a task with at most the last 8 KiB of output. |
| `/tasks cancel <id>` | Cancel a task. |
| `/skills` | List skills available to the bound session. |
| `/skills run <name> [args]` | Activate an exact skill name as a normal streamed turn. |
| `/mcp` | List MCP tools resolved for the session. This is read-only. |
| `/send <path>` | Send one regular file contained by the bound workspace. |
| `/render-thinking [on\|off]` | Show or set separate thinking output for this conversation. |

Model aliases and thinking efforts come from the live Kimi catalog. A model change preserves the current effort when supported, otherwise it selects the model's advertised default or `off` when thinking is unsupported. Plan usage/quota reset information is not exposed by the public local server and is not part of `/usage`.

Goal replacement and goal queues are not exposed. A blocked goal remains blocked until `/goal resume`; an ordinary follow-up does not reactivate it. Global MCP mutation/restart is not exposed.

## Busy-session matrix

| While a turn is busy | Commands |
| --- | --- |
| Reads remain available | `/help`, `/sessions`, `/status`, bare `/title`, `/usage`, task list/filter/show, bare `/skills`, `/mcp`, bare `/model`, bare `/effort`, bare `/plan`, `/goal`/`/goal status`, bare `/render-thinking` |
| Mutations execute immediately | `/new`, `/switch`, `/mode`, `/title <text>`, `/tasks cancel <id>`, `/goal pause`, `/goal cancel`, `/send`, `/render-thinking on\|off`, `/stop` |
| Mutations reject instead of queueing | `/model <alias>`, `/effort <effort>`, `/plan on\|off`, `/skills run ...`, `/compact`, `/undo`, goal creation, `/goal resume` |

A normal non-command message sent during a running turn is submitted and steered into that turn at Kimi's next step boundary. Steering is a nudge, not an immediate interrupt; an in-flight tool call can finish. `/new` and `/switch` move the conversation binding without aborting work already running in the previous Kimi session.

Changing `/mode` affects later permission checks but does not answer a currently displayed approval or question. `/stop`, `/goal pause`, and `/goal cancel` close the relevant interaction as cancelled.

## Permission modes and interactions

- `manual` presents approvals and questions in chat. This is the default for a new session.
- `auto` is fully autonomous and does not ask questions.
- `yolo` auto-approves regular tools but may still ask questions.

Feishu renders approvals and questions as interactive cards. Telegram renders approval buttons and a sequential question wizard. A single-choice answer completes immediately; multi-select requires explicit completion; custom text uses the platform's reply flow. Callbacks are accepted only from the authorized actor and original conversation.

Each request uses `interaction_timeout_seconds`. An unanswered approval is rejected and an unanswered question is dismissed. The existing card or keyboard is moved to a terminal state. In-memory interaction handles intentionally do not survive restart, so a later callback is reported as stale instead of being applied to a new turn.

## Streaming and thinking

Answers stream into editable messages at the configured throttle and are split by the router at the selected platform's text limit. Text separated by an interleaved tool-call boundary starts a new visible message instead of overwriting earlier answer text.

`/render-thinking on` creates a separately labelled thinking stream with independent buffering, edits, chunking, resynchronization, and finalization. Enabling it during a live turn backfills the current thinking snapshot. Disabling it freezes the visible thinking while the answer continues. The preference persists per conversation. Tool-call and transcript rendering are intentionally absent.

## Inbound and outbound media

Feishu accepts direct text, native images, files, and images embedded in rich posts. Images become image prompt parts. Files are saved below the bound workspace's configured inbox subdirectory, and their absolute paths are included in prompt text. Unsupported direct-message types receive a notice; group messages are ignored.

The experimental Telegram adapter accepts plain text, one photo, or one document with an optional caption. Albums and other media are rejected. Hosted Bot API downloads are capped at 20 MB. Startup discards pending updates rather than replaying old instructions.

`/send <path>` accepts a relative path resolved from the bound workspace or an absolute path whose resolved target remains inside it. Missing paths, directories, globs, multiple files, and symlinks escaping the workspace are rejected. Feishu sends JPEG/PNG images natively, MP4 as native media with a neutral cover, and other files as native files. Telegram sends JPEG/PNG through `sendPhoto` and every other type, including MP4, through `sendDocument`.

## Current operating limits

- One selected adapter per process and one trusted-operator security model.
- Feishu direct messages only; Telegram private chats only.
- Telegram remains experimental and has not been project live-validated.
- No tool-call or transcript rendering, multi-tenant isolation, generic plugin/UI framework, webhooks, Telegram groups/topics/albums, or remote Kimi server mode.
