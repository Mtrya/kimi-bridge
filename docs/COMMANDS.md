# Commands and interactions

Commands are case-insensitive before the first space; arguments retain their case. Any message not beginning with a recognized slash command is submitted to the bound Kimi session. If no session is bound, the bridge creates one in `default_workspace` and uses the message as its first prompt.

## Exact command grammar

`/help` shows the compact index below. Every command also answers `/<command> ?` with detailed usage — syntax, arguments, defaults, side effects, and examples — including sub-forms such as `/tasks show ?`.

| Command | Behavior |
| --- | --- |
| `/help` | Show the in-chat command index. |
| `/new [cwd]` | Create and bind a session. Without `cwd`, use the configured default workspace. |
| `/sessions` | List recent Kimi sessions and remember their displayed indices for `/switch`. The list size is `session_list_limit`. |
| `/sessions search <keyword>` | Case-insensitive substring search over all active (non-archived) sessions by title and workspace path, ranked by recency and capped at `session_list_limit`. Results are remembered for `/switch <n>`. |
| `/switch <n\|id\|title>` | Bind a displayed one-based index, an explicit session ID, or an exact case-insensitive title among active (non-archived) sessions. Ambiguous titles produce a numbered candidate list for `/switch <n>`. |
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
| `/stop` | Abort the active main-agent turn, discard active and queued prompts, and cancel its pending interaction. Detached tasks continue. |
| `/restart-server` | Gracefully restart the managed Kimi Code server while keeping the bridge and IM adapter online. |
| `/tasks [running\|completed\|failed\|cancelled]` | List all tasks or filter by status. |
| `/tasks show <id>` | Inspect a task with at most the last 8 KiB of output. |
| `/tasks cancel <id>` | Cancel a task. |
| `/skills` | List skills available to the bound session. |
| `/skills run <name> [args]` | Activate an exact skill name as a normal streamed turn. |
| `/mcp` | List MCP tools resolved for the session. This is read-only. |
| `/send <path>` | Send one regular file contained by the bound workspace. |
| `/render-thinking [on\|off]` | Show or set separate thinking output for this conversation. |

Model aliases and thinking efforts come from the live Kimi catalog. A model change preserves the current effort when supported, otherwise it selects the model's advertised default or `off` when thinking is unsupported. Plan usage/quota reset information is not exposed by the public local server and is not part of `/usage`.

Goal replacement and goal queues are not exposed. A blocked goal remains blocked until `/goal resume`; an ordinary follow-up does not reactivate it. Global MCP mutation is not exposed.

## Local WeChat authorization commands

These are terminal commands, not chat commands, and never start Kimi Code or message polling:

| Command | Behavior |
| --- | --- |
| `kimi-bridge wechat login` | Print a short-lived WeChat authorization URL and save a confirmed QR authorization locally. Refuses to overwrite an existing credential. |
| `kimi-bridge wechat login --replace` | Start a replacement QR flow while retaining the prior credential until confirmation; an already-bound redirect retains the existing credential. |
| `kimi-bridge wechat status` | Inspect local storage permissions and redacted authorization metadata without network access. |
| `kimi-bridge wechat logout` | Remove only adapter-owned local credential and receive-state files; it does not remotely delete the bot binding. |

Use `--config <path>` when the WeChat config is not at the default path. After a first login, copy the printed stable scanner identity into `wechat.allowed_users` before running `doctor` or starting the bridge. An expired runtime authorization stops with a `login --replace` instruction.

## Busy-session matrix

| While a turn is busy | Commands |
| --- | --- |
| Reads remain available | `/help` and `/<command> ?`, `/sessions`, `/status`, bare `/title`, `/usage`, task list/filter/show, bare `/skills`, `/mcp`, bare `/model`, bare `/effort`, bare `/plan`, `/goal`/`/goal status`, bare `/render-thinking` |
| Mutations execute immediately | `/new`, `/switch`, `/mode`, `/title <text>`, `/tasks cancel <id>`, `/goal pause`, `/goal cancel`, `/send`, `/render-thinking on\|off`, `/stop`, `/restart-server` |
| Mutations reject instead of queueing | `/model <alias>`, `/effort <effort>`, `/plan on\|off`, `/skills run ...`, `/compact`, `/undo`, goal creation, `/goal resume` |

A normal non-command message sent during a running turn is submitted and steered into that turn at Kimi's next step boundary. Steering is a nudge, not an immediate interrupt; an in-flight tool call can finish. `/new` and `/switch` move the conversation binding without aborting work already running in the previous Kimi session.

Changing `/mode` affects later permission checks but does not answer a currently displayed approval or question. `/stop`, `/goal pause`, and `/goal cancel` close the relevant interaction as cancelled.

`/stop` discards queued prompts before aborting the active prompt and main-agent turn, so a queued follow-up cannot start immediately after cancellation. It also works when the turn was started outside the prompt service. The command is idempotent and reports `Stopped.` after Kimi accepts the session abort, including when the session is already idle. Detached tasks are not cancelled and remain available through `/tasks`.

`/restart-server` takes no arguments and intentionally recycles the entire managed Kimi Code server even while sessions are busy. The bridge and selected IM adapter stay online, and success is reported only after the replacement server is ready. Persisted sessions, conversation history, and bridge bindings survive, while active turns, approvals, questions, detached tasks, live usage totals, and other in-memory server state are terminated. Existing event subscriptions reconnect and rematerialize their sessions against the new server generation.

## Permission modes and interactions

- `manual` presents approvals and questions in chat. This is the default for a new session.
- `auto` is fully autonomous and does not ask questions.
- `yolo` auto-approves regular tools but may still ask questions.

Feishu renders approvals and questions as interactive cards. Telegram renders approval buttons and a sequential question wizard. A single-choice answer completes immediately; multi-select requires explicit completion; custom text uses the platform's reply flow. Callbacks are accepted only from the authorized actor and original conversation.

Each request uses `interaction_timeout_seconds`. An unanswered approval is rejected and an unanswered question is dismissed. The existing card or keyboard is moved to a terminal state. In-memory interaction handles intentionally do not survive restart, so a later callback is reported as stale instead of being applied to a new turn.

QQ and WeChat cannot present approvals or questions: every session is forced into `auto` mode. `/mode <anything>` explains that permission mode remains fixed, and `/render-thinking on` explains that separate thinking rendering remains off. This affects only presentation; the selected model's thinking support and configured effort are unchanged. If Kimi still raises an unexpected interactive prompt, the adapter sends a short unsupported notice and lets the request time out normally.

## Streaming and thinking

Answers stream into editable messages at the configured throttle and are split by the router at the selected platform's text limit. Bridge-generated command, status, validation, and error replies are final messages and bypass streaming finalization. For Feishu, the first 15 scheduled edits use the base throttle and the final five use progressively longer intervals targeting `max_output_seconds`; platform or event-loop latency can delay delivery, while final reconciliation can use the remaining edit budget sooner. The router stops editing a message after Feishu's 20-edit limit without stopping the Kimi event stream. Text separated by an interleaved tool-call boundary starts a new visible message instead of overwriting earlier answer text.

`/render-thinking on` creates a separately labelled thinking stream with independent buffering, edits, chunking, resynchronization, and finalization. Enabling it during a live turn backfills the current thinking snapshot. Disabling it freezes the visible thinking while the answer continues. The preference persists per conversation. Tool-call and transcript rendering are intentionally absent. QQ streams complete lines and closed fenced blocks through one compact, prefix-extending `stream_messages` rendering strategy and finalizes each bubble before opening the next. Revisions confined to the buffered tail remain in the same stream. A correction to the rendered frontier withdraws the partial response before sending the corrected final response; failure to withdraw does not suppress the correction. WeChat has no edit API: it preserves complete step-boundary output as immutable messages, uses a 4,000-character message limit, and marks only the last chunk final so best-effort typing cancellation follows the completed answer. QQ and WeChat never offer a separate thinking stream.

## Inbound and outbound media

Feishu accepts direct text, native images and videos, generic files, audio (voice) messages, and images embedded in rich posts. Native images and videos become file-backed prompt parts when the bound model advertises the corresponding input capability; otherwise they are saved below the bound workspace's configured inbox subdirectory and their absolute paths are included in prompt text. Generic files always use that inbox path. Audio messages are transcribed and submitted as `[语音转写]`-prefixed prompt text: the configured `[voice.asr]` endpoint is tried first when present, then the Feishu adapter uses FFmpeg to convert Opus to 16 kHz mono PCM for native file recognition. Untranscribable audio produces a prompt-only system notice. Unsupported direct-message types receive a notice; group messages are ignored.

The experimental Telegram adapter accepts plain text, one photo, or one document with an optional caption. Albums and other media are rejected. Hosted Bot API downloads are capped at 20 MB. Startup discards pending updates rather than replaying old instructions.

The supported QQ adapter accepts direct C2C text and HTTPS-hosted attachments up to 20 MB. QQ-declared image and video attachments follow the same capability-aware native-media route as Feishu; voice attachments (`content_type` `voice` or `audio/*`) are transcribed — the configured `[voice.asr]` endpoint first, QQ's own `asr_refer_text` transcript as fallback — and submitted as `[语音转写]`-prefixed prompt text; every other attachment is a generic file and always uses the workspace inbox. Upload failures are reported in chat without terminating the bridge. Only C2C (private) messages are handled; group chat is not implemented.

The experimental WeChat adapter accepts private-chat text, native images, voice messages, generic files, and video. Images and videos use the capability-aware model-input route; generic files always use the workspace inbox. Voice uses the configured `[voice.asr]` endpoint first and Tencent's native transcript as fallback. Encrypted CDN downloads are bounded to 100 MiB and validate their cryptography and content integrity where the protocol supplies a digest. The adapter durably advances its opaque receive cursor only after each handled message, so ordinary restarts avoid replay; a crash after Kimi accepted a prompt but before local completion recording can still replay it under the documented at-least-once contract.

`/send <path>` accepts a relative path resolved from the bound workspace or an absolute path whose resolved target remains inside it. Missing paths, directories, globs, multiple files, and symlinks escaping the workspace are rejected. Feishu sends JPEG/PNG images natively, MP4 as native media with a neutral cover, and other files as native files. Telegram sends JPEG/PNG through `sendPhoto` and every other type, including MP4, through `sendDocument`. QQ uploads JPEG/PNG images and MP4 video and sends them as native media; every other file type uploads as an arbitrary file (`file_type=4`, up to QQ's 200 MB hard limit) and arrives as a downloadable file card. WeChat encrypts and uploads image and video types as native items and sends every other type as a generic file, with a 100 MiB plaintext bound. Audio therefore arrives as a downloadable file, not a native WeChat voice message. WeChat sends require the latest inbound context token and are never proactive.

## Current operating limits

- One selected adapter per process and one trusted-operator security model.
- Feishu direct messages only; Telegram private chats only; QQ C2C and WeChat private chats only.
- Telegram remains experimental and has not been project live-validated; QQ is supported and live-validated in sandbox; WeChat is experimental and was live-validated on 2026-08-08 against Tencent tag `v2.4.6` with one allowlisted scanner.
- WeChat two-sender context isolation is contract-tested but was not project-live-validated, and the adapter provides no group chat or proactive delivery.
- No tool-call or transcript rendering, multi-tenant isolation, generic plugin/UI framework, webhooks, Telegram groups/topics/albums, QQ group chat, or remote Kimi server mode.
