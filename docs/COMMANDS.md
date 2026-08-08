# Commands and interactions

Commands are case-insensitive before the first space; arguments retain their case. Any message not beginning with a recognized slash command is submitted to the bound Kimi session. If no session is bound, the bridge creates one in `default_workspace` and uses the message as its first prompt.

## Exact command grammar

`/help` shows the compact index below. Every command also answers `/<command> ?` with detailed usage, including arguments, defaults, side effects, and examples.

| Command | Behavior |
| --- | --- |
| `/help` | Show the in-chat command index. |
| `/new [cwd]` | Create and bind a session. Without `cwd`, use the configured default workspace. |
| `/sessions` | List recent Kimi sessions and remember their displayed indices for `/switch`. |
| `/sessions search <keyword>` | Search active sessions by title and workspace path. |
| `/switch <n\|id\|title>` | Bind a displayed index, explicit session ID, or exact title. |
| `/status` | Show session, workspace, busy state, pending interaction, model, effort, plan mode, permission mode, and Kimi Code version. |
| `/title [text]` | Show the current title or rename the session. |
| `/usage` | Show live token and context-window values when exposed by Kimi. |
| `/compact` | Start context compaction. |
| `/undo [count]` | Undo a positive number of history steps; default `1`. |
| `/mode <manual\|auto\|yolo>` | Set the session permission mode where the adapter supports it. |
| `/model [alias]` | Show the current model and catalog aliases, or select an alias. |
| `/effort [effort]` | Show or set an effort advertised for the active model. |
| `/plan [on\|off]` | Show or explicitly set plan mode. |
| `/goal` or `/goal status` | Show the public goal state and budgets. |
| `/goal <objective>` | Create a goal and submit its objective as a normal turn. |
| `/goal pause` | Pause the current goal and cancel its active prompt/interaction. |
| `/goal resume` | Reactivate a paused or blocked goal. |
| `/goal cancel` | Cancel the current goal and its active prompt/interaction. |
| `/stop` | Abort the active main-agent turn, discard queued prompts, and cancel its pending interaction. Detached tasks continue. |
| `/restart-server` | Gracefully restart the managed Kimi Code server while keeping the bridge and selected adapter online. |
| `/tasks [running\|completed\|failed\|cancelled]` | List all tasks or filter by status. |
| `/tasks show <id>` | Inspect a task with at most the last 8 KiB of output. |
| `/tasks cancel <id>` | Cancel a task. |
| `/skills` | List skills available to the bound session. |
| `/skills run <name> [args]` | Activate an exact skill name as a normal streamed turn. |
| `/mcp` | List MCP tools resolved for the session. This is read-only. |
| `/send <path>` | Send one regular file contained by the bound workspace. |
| `/render-thinking [on\|off]` | Show or set separate thinking output where supported. |

Model aliases and thinking efforts come from the live Kimi catalog. Plan usage and quota reset information are not exposed by the public local server.

## Terminal platform authorization commands

These are terminal commands, not chat commands. They do not start Kimi Code or message polling. Every command loads the config first and requires its `platform` value to match the command platform. Use `--config PATH` at the top level, on the platform command, or on the subcommand when the config is not at the default path.

The three QR groups below are not three versions of the same “login”: Feishu registers an application, QQ binds an official bot credential, and WeChat authorizes an iLink bot. Telegram has no QR or terminal authorization command; configure its Bot API token and numeric user allowlist in TOML.

| Platform | QR semantic | `login` | `login --replace` | `status` | `logout` |
| --- | --- | --- | --- | --- | --- |
| Feishu | Official application registration; returns application credentials, not user OAuth | Print a Feishu/Lark setup URL that can create or select an application and pre-fills the bridge permissions/event/callback, then save confirmed managed credentials | Register a replacement without discarding the old credential before confirmation | Inspect local managed storage and redacted metadata; report a complete TOML pair when no managed credential exists; no network check | Remove only adapter-owned Feishu managed files; TOML fallback remains |
| QQ | Official bot credential bootstrap; returns bot app ID and locally decrypted AppSecret, not QQ user login or OAuth | Print a QQ bind URL and save the confirmed managed credential | Run a new bind flow without replacing the old credential until success | Inspect local managed storage and redacted metadata; report a complete TOML pair when no managed credential exists; no network check | Remove only adapter-owned QQ managed files; TOML fallback remains |
| WeChat | WeChat iLink bot authorization for private chat | Print an authorization URL and save confirmed local bot credential | Authorize a replacement while retaining the previous credential until confirmation | Inspect local authorization and storage; no network check | Remove only adapter-owned WeChat credential and receive-state files |
| Telegram | No QR flow; manual Bot API configuration | — | — | — | — |

Commands:

```bash
kimi-bridge feishu login
kimi-bridge feishu login --replace
kimi-bridge feishu status
kimi-bridge feishu logout

kimi-bridge qq login
kimi-bridge qq login --replace
kimi-bridge qq status
kimi-bridge qq logout

kimi-bridge wechat login
kimi-bridge wechat login --replace
kimi-bridge wechat status
kimi-bridge wechat logout
```

Default managed credential files are `~/.kimi-bridge/feishu/credentials.json`, `~/.kimi-bridge/qq/credentials.json`, and `~/.kimi-bridge/wechat/credentials.json`; each platform's `storage_path` can relocate its directory. Feishu and QQ use a complete TOML `[feishu]`/`[qq]` `app_id` + `app_secret` pair only when the managed file is absent. A present but invalid managed file is a startup error, not a reason to use TOML silently. WeChat credentials never go into TOML.

After Feishu login, approve the pre-filled tenant permissions, `im.message.receive_v1` event, and `card.action.trigger` callback on the confirmation page; then confirm bot capability and any remaining console settings, publish the app, and populate the bridge-side `feishu.allowed_users` list yourself. After QQ binding, put the returned scanner `user_openid` into `qq.allowed_users` manually; it is not a QQ number or nickname. After WeChat authorization, put the returned stable scanner identity into `wechat.allowed_users` manually. If the running WeChat adapter reports expired authorization, stop it and run `kimi-bridge wechat login --replace`; QR completion never automatically adds an allowlist entry.

## Busy-session matrix

| While a turn is busy | Commands |
| --- | --- |
| Reads remain available | `/help` and `/<command> ?`, `/sessions`, `/status`, bare `/title`, `/usage`, task list/filter/show, bare `/skills`, `/mcp`, bare `/model`, bare `/effort`, bare `/plan`, `/goal`/`/goal status`, bare `/render-thinking` |
| Mutations execute immediately | `/new`, `/switch`, `/mode`, `/title <text>`, `/tasks cancel <id>`, `/goal pause`, `/goal cancel`, `/send`, `/render-thinking on\|off`, `/stop`, `/restart-server` |
| Mutations reject instead of queueing | `/model <alias>`, `/effort <effort>`, `/plan on\|off`, `/skills run ...`, `/compact`, `/undo`, goal creation, `/goal resume` |

A normal non-command message sent during a running turn is submitted and steered into that turn at Kimi's next step boundary. Steering is not an immediate interrupt; an in-flight tool call can finish.

Changing `/mode` affects later permission checks but does not answer a currently displayed approval or question. `/stop`, `/goal pause`, and `/goal cancel` close the relevant interaction as cancelled.

## Permission modes and interactions

- `manual` presents approvals and questions in chat. It is the default for a new session.
- `auto` is autonomous and does not ask questions.
- `yolo` auto-approves regular tools but may still ask questions.

Feishu renders approvals and questions as interactive cards. Telegram renders approval buttons and a sequential question wizard. QQ and WeChat cannot present approvals or questions: every session is forced into `auto` mode. `/mode` explains that the mode is fixed, and `/render-thinking on` explains that separate thinking rendering remains off for those adapters.

## Streaming and thinking

Answers stream through editable messages where the platform supports edits and are split at the platform text limit. Bridge-generated command, status, validation, and error replies are final messages.

`/render-thinking on` creates a separately labelled thinking stream where supported and the preference persists per conversation. QQ and WeChat never offer a separate thinking stream. QQ uses a stable-frontier `stream_messages` lifecycle with correction withdrawal when necessary. WeChat has no edit API and emits complete step-boundary output as immutable messages with a 4,000-character limit.

## Inbound and outbound media

Feishu accepts text, native images and videos, generic files, and voice messages. QQ accepts C2C text and HTTPS attachments; only supported native image/video values take the native-media route. WeChat accepts private-chat text, images, voice, files, and video. Native image/video input depends on the selected model's `image_in`/`video_in` capability; otherwise the bridge saves the item under the workspace inbox. Generic files always use the workspace inbox. Voice messages are transcribed using the configured `[voice.asr]` endpoint first and the platform-native fallback when available.

`/send <path>` can send one regular file contained by the bound workspace. Native rendering is platform-specific: Feishu, QQ, and WeChat support native image/video forms where documented; WeChat sends audio as a generic downloadable file and requires the latest inbound context token, so it is not proactive.

## Current operating limits

- One selected adapter per process and one trusted-operator security model.
- Feishu, QQ, and WeChat handle private chats; QQ is C2C and WeChat has no group or proactive delivery.
- QQ and WeChat force `auto` and do not provide approvals, questions, or separate thinking output.
- Telegram remains experimental and is private-chat only.
- There is no simultaneous multi-adapter process, remote Kimi server mode, generic plugin/UI framework, or remote platform logout operation.
