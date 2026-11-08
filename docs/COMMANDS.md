# Commands and interactions

Commands are case-insensitive before the first space; arguments retain their case. Any message not beginning with a recognized slash command is submitted to the bound Kimi session. If no session is bound, the bridge creates one in `default_workspace` and uses the message as its first prompt. Local path arguments (`/new`, `/files`) treat a standalone fullwidth `～` (U+FF5E, common from mobile IMEs), or one followed by the platform path separator, as `~` when the literal path does not exist; forms such as `～notes` remain literal.

## Exact command grammar

`/help` shows the compact index below. Every command also answers `/<command> ?` with detailed usage, including arguments, defaults, side effects, and examples.

| Command | Behavior |
| --- | --- |
| `/help` | Show the in-chat command index. |
| `/new [workspace] [--env <id> [--cwd <environment-path>]]` | Create and bind a session with swarm mode off. The workspace belongs to the bridge host; target cwd belongs to the selected environment. |
| `/environments` | List native configured local, SSH, Docker, and command targets and their connection state. |
| `/archive` | Archive the bound session through Kimi, preserve its history, and clear every bridge chat binding to it. |
| `/sessions` | List recent Kimi sessions and remember their displayed indices for `/switch`. |
| `/sessions search <keyword>` | Search active sessions by title and workspace path. |
| `/sessions archived` | List archived sessions and remember their displayed indices for `/restore`. |
| `/switch <n\|id\|title>` | Bind a displayed index, explicit session ID, or exact title; the confirmation recaps the newest 2 messages. |
| `/fork` | Fork and bind the current session, preserving its workspace and native settings. Source work continues; its in-progress answer and queued work are excluded. |
| `/restore <n\|id>` | Restore and bind an archived session by its displayed number or explicit ID. |
| `/status` | Show session, workspace, busy state, pending interaction, model, effort, plan mode, permission mode, and Kimi Code version. |
| `/title [text]` | Show the current title or rename the session. |
| `/usage` | Show cumulative session tokens/live context alongside available managed-account quota windows and reset times. |
| `/compact [instruction]` | Start context compaction; optional instructions tell Kimi what to preserve in the summary. |
| `/history [count] [full]` | Show the newest visible session messages (default `10`, max `50`); thinking and tool traffic are omitted, attachments become placeholders, and `full` removes the default 500-character per-message limit. `full` may precede or follow the count. |
| `/undo [count]` | Undo a positive number of history steps; default `1`; the confirmation recaps the newest 2 messages. |
| `/mode <manual\|auto\|yolo>` | Set the session permission mode where the adapter supports it. |
| `/model [alias]` | Show the current model and catalog aliases, or select an alias. |
| `/secondary-model [alias]` | Show the default, pool descriptions, and catalog aliases for newly spawned subagents, or select a default globally. |
| `/secondary-model set <alias> <description>` | Add a subagent pool entry or replace its description; the description is the remaining text, with no quoting required. |
| `/effort [effort]` | Show or set an effort advertised for the active model. |
| `/plan [on\|off]` | Show or explicitly set plan mode. |
| `/swarm [on\|off]` | Show or set native swarm mode without submitting a prompt. |
| `/queue` | Show the active prompt and numbered waiting prompts. |
| `/queue <message>` | Submit a prompt as queued work without steering the active turn. |
| `/queue cancel <n\|id>` | Cancel the selected prompt; if it has started, Kimi cancels that active prompt. |
| `/queue steer <n\|id>` | Steer a selected waiting prompt into the active turn at Kimi’s next step boundary. |
| `/goal` or `/goal status` | Show the public goal state and budgets. |
| `/goal <objective>` | Create a goal and submit its objective as a normal turn. |
| `/goal edit <objective>` | Edit the current active, paused, or blocked goal in place, preserving its identity, progress, and budget. Running work continues. |
| `/goal pause` | Pause the current goal and cancel its active prompt/interaction. |
| `/goal resume` | Reactivate a paused or blocked goal. |
| `/goal cancel` | Cancel the current goal and its active prompt/interaction. |
| `/stop` | Abort the active main-agent turn, discard queued prompts, and cancel its pending interaction. Detached tasks continue. |
| `/reload` | Reload Kimi settings and refresh the bound session while the server and bridge stay online. Requires an idle session. |
| `/tasks [all\|completed\|failed\|cancelled]` | List numbered running tasks. `all` also lists only running tasks; terminal-status filters return no tasks because Kimi omits ended tasks from discovery. |
| `/tasks show <n\|id>` | Inspect a task by displayed number or explicit ID, including a known ended task, with at most the last 8 KiB of output. |
| `/tasks cancel <n\|id>` | Cancel a task by displayed number or explicit ID. |
| `/tasks background <n\|id>` | Release a supported foreground task wait, allowing its work to continue. |
| `/btw <question>` | Ask a native read-only side question, with a separate labelled answer while main work continues. |
| `/agents` | Show the main agent and discoverable subagent hierarchy, assignment, model, and state. |
| `/agents show <n\|id>` | Inspect a live agent without changing where ordinary messages are sent. |
| `/agents background <n\|id>` | Background the selected agent’s owning task when supported. |
| `/todos [all]` | Show main-agent todos, or group available checklists by agent; counts show completed/total with ✓ done, → in progress, and ○ pending. |
| `/skills` | List skills available to the bound session. |
| `/skills run <name> [args]` | Activate an exact skill name as a normal streamed turn. |
| `/plugins [show <name>]` | Inspect installed plugin state, declared contributions, diagnostic counts, and available bound-session tools. |
| `/mcp` | List MCP tools currently available in the bound session. |
| `/mcp servers` | Inspect native runtime connection states and bound-session configuration authentication states. |
| `/mcp show <server>` | Inspect one server and its available bound-session tools. |
| `/mcp restart <server>` | Request a native connection restart; acknowledgement does not establish connection health. |
| `/workspace [add\|remove\|primary <path>]` | Inspect or change shared native workspace folders; existing sessions retain their working directories. |
| `/files [directory]` | List files and folders in the session working directory or a specified approved directory. |
| `/files search <query>` | Search filenames in the native workspace roots. |
| `/files show <n\|path>` | Preview up to 8 KiB of text, or identify a binary file. |
| `/files send <n\|path>` | Send a workspace file as a platform attachment. |
| `/files changes` | List the latest finished main turn’s recorded file changes, with addition/deletion counts and binary/oversize indicators. |
| `/files diff <n\|path>` | Preview a recorded start/end diff, including deleted files. |
| `/render-thinking [on\|off]` | Show or set separate thinking output where supported. |

Task numbers refer to your latest list in the same chat and bound session, including any status filter. For example, `/tasks all` followed by `/tasks show 2` inspects the second displayed task. Status changes do not renumber that list; another task listing replaces it. Changing the bound session clears your task numbers across chats, including when you later switch back. Explicit IDs remain valid without a prior listing. Bare numbers always select list positions; use `/tasks show id <id>` or `/tasks cancel id <id>` for an exact ID, including numeric IDs such as `/tasks show id 123`.

Agent numbers follow the same chat/session selection scope as task numbers; use `/agents show id 123` or `/agents background id 123` for a numeric native ID. Discovery shows current native activity rather than a complete task history. Backgrounding reports newly backgrounded, already backgrounded, ended, or unsupported work. Native swarm members cannot be detached individually. Output remains available on demand through `/tasks show`.

Queue numbers refer to the latest `/queue` display in the same chat and session and are cleared on a session change. The active prompt is not numbered. Use `/queue cancel id 123` or `/queue steer id 123` for a numeric native ID, and `/queue -- <message>` to queue text beginning with `cancel` or `steer`. Kimi resolves stale prompt IDs and execution races. Use `/goal -- <objective>` when a new objective begins with a reserved goal action such as `edit`, and `/restore id 123` for a numeric archived session ID. `/switch` requires archived sessions to be restored first.

Model and effort changes are accepted while busy and affect later native requests; an already sent request keeps its settings.

`/workspace` shows the shared primary/additional folders separately from the session working directory. Folder changes affect sessions sharing that native workspace. Promoting a folder keeps the old primary as an additional folder; existing sessions keep their original working directories and access to them even after removal from shared membership. The native session’s directory and current additional folders determine file access, including symlink containment checks.

`/files` lists up to 100 entries; `/files search` returns up to 50 matches under native ignore rules. A successful list or search installs one current numbered selection for that chat and session. A changes view uses the same list; its entries retain their recorded turn. Another view replaces it; binding or folder changes invalidate it. Use explicit paths with spaces directly, `./123` for a numeric filename, and `/files -- search` for a directory named `search`. Every open/send revalidates native access. Truncated listings and previews are labelled; directories and binary previews follow native results. Recorded changes use the latest finished main turn while a new turn is running. A diff selected from a changes view keeps that turn; an explicit path uses the latest finished turn. Unrecorded, missing, binary, and oversize results are reported separately from an empty comparison.

Model aliases and thinking efforts come from the live Kimi catalog. `/secondary-model` updates Kimi Code's global `[secondary_model]` configuration; it affects later subagent spawns in every session without changing existing subagents or primary session models. Pool descriptions guide Kimi's choice of model for a subtask. Adding the first entry sets the default; later additions and description edits keep it. Selecting a default adds that alias to an explicit pool if needed. Pool edits preserve Kimi's `force` setting; when enabled, subagent selection remains forced to the default. To remove entries, reset the pool, or disable `force`, edit Kimi Code's configuration and run `/reload` in the session to refresh.

Use `/new /host/workspace --env target --cwd "/target/project with spaces"` to select an existing Kimi environment. Without flags, `/new` retains its usual workspace behavior and native default target. Target definitions and credentials stay in Kimi. `/status` shows the selected environment and target working directory; resuming a session restores its native target. File browsing, search, preview, delivery, and recorded history address that session’s target. A target failure is reported without a host-filesystem fallback.

`/plugins` shows installed native state; `show` separates declared skills/MCP contributions from tools currently available to the bound session. Diagnostic severity counts are displayed; inspect detailed diagnostics and manage configuration or authentication in Kimi.

MCP runtime inspection and restart follow the native v1 scope: the most recently created session, only if it is live. The bound session may differ. Tool availability and configuration authentication are labelled for the bound session. `/mcp restart` confirms the selected native restart request; inspect again with `/mcp show` for connection state. These commands do not install or reconfigure integrations.

`/usage` keeps session counters separate from managed-account limits. Available quota windows show percentage used and reset times in UTC; a timestamp without a supplied time zone is labelled accordingly. An unavailable account/provider or failed quota lookup leaves valid session statistics visible. Account usage is read on demand through Kimi’s managed account endpoint.

`/reload` refreshes Kimi Code's configuration, plugins, skills, and agent definitions and rebuilds the bound session with its existing history. It preserves the session ID, selected model, and bridge permission mode. Kimi rejects reload during running agent turns or compaction. Global Kimi settings are refreshed, and new sessions use the latest default model; the bridge's own configuration is loaded at bridge startup.

`/archive` takes no arguments and sends the archive request directly to Kimi, including while the bound session is busy. Kimi stops the session's running work when archiving. The bridge closes that session's event stream and, after confirmation, clears its pending controls and bindings. The next ordinary message creates a session in `default_workspace`; use `/new <cwd>` to choose another workspace. Cached listings containing the archived session are invalidated, so run `/sessions` again before switching by number. A rejected archive leaves bindings intact. If its acknowledgement is lost, the bridge checks Kimi’s stored archive state; when that check is also unavailable, it keeps the binding and asks you to retry `/archive`. Stored bindings are checked before their first prompt after a bridge restart, so an archive confirmed by Kimi still takes effect after a crash.

## Terminal platform authorization commands

These are terminal commands, not chat commands. They do not start Kimi Code or message polling. When the config does not exist, `login` creates it with the selected platform. Otherwise every command loads the config first; if the config's `platform` does not match the command platform, `login` offers to switch the setting after confirmation, while `status` and `logout` require an exact match. Use `--config PATH` at the top level, on the platform command, or on the subcommand when the config is not at the default path.

The three QR groups below are not three versions of the same “login”: Feishu registers an application, QQ binds an official bot credential, and WeChat authorizes an iLink bot.

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

Default managed credential files are `~/.kimi-bridge/feishu/credentials.json`, `~/.kimi-bridge/qq/credentials.json`, and `~/.kimi-bridge/wechat/credentials.json`; each platform's `storage_path` can relocate its directory. Feishu and QQ use a complete TOML `[feishu]`/`[qq]` `app_id` + `app_secret` pair only when the managed file is absent. A present but invalid managed file is a startup error. WeChat credentials never go into TOML.

After Feishu login, approve the pre-filled tenant permissions, `im.message.receive_v1` event, and `card.action.trigger` callback on the confirmation page; then confirm bot capability and any remaining console settings and publish the app. When a flow returns a user identity, `login` merges it into `allowed_users` (Feishu `open_id`, QQ `user_openid`, WeChat scanner identity). See the [installation guide](../INSTALL.en_US.md) for the full walkthrough. If the running WeChat adapter reports expired authorization, stop it and run `kimi-bridge wechat login --replace`.

## Busy-session matrix

| While a turn is busy | Commands |
| --- | --- |
| Reads remain available | `/help` and `/<command> ?`, `/sessions`, `/status`, bare `/title`, `/usage`, task list/filter/show, `/agents`/`/agents show`, `/todos [all]`, bare `/skills`, `/plugins`, `/mcp` tool/server/show inspection, bare `/model`, bare `/secondary-model`, bare `/effort`, bare `/plan`, bare `/swarm`, `/queue`, `/workspace`, `/files` list/search/show/changes/diff, `/environments`, `/goal`/`/goal status`, bare `/render-thinking` |
| Mutations execute immediately | `/new`, `/switch`, `/fork`, `/restore`, `/mode`, `/model <alias>`, `/effort <effort>`, `/swarm on\|off`, `/title <text>`, `/secondary-model <alias>`, `/secondary-model set ...`, `/tasks cancel <n\|id>`, `/tasks background`, `/agents background`, `/btw`, queue submission/cancel/steer, `/goal edit`, `/goal pause`, `/goal cancel`, `/files send`, `/mcp restart`, `/workspace add`, `/workspace remove`, `/workspace primary`, `/render-thinking on\|off`, `/stop`, `/archive` |
| Mutations reject instead of queueing | `/plan on\|off`, `/skills run ...`, `/compact`, `/undo`, goal creation, `/goal resume`, `/reload` |

A normal non-command message sent during a running turn is submitted and steered into that turn at Kimi's next step boundary. Steering is not an immediate interrupt; an in-flight tool call can finish.

If the turn finishes before steering, the submitted prompt remains under Kimi's normal execution lifecycle. A prompt blocked before execution is reported in chat. If submission or steering cannot be confirmed, check `/status` or `/history` before resending; the bridge does not automatically resubmit. Goal creation submits its initial prompt as separate work and reports when the goal was created but its initial prompt failed or was blocked.

Changing `/mode` affects later permission checks but does not answer a currently displayed approval or question. `/stop`, `/goal pause`, and `/goal cancel` address the bound session and close its relevant interaction as cancelled. If another session needs input after switching away, one labelled notice asks you to switch back. Its old controls cannot answer from the new session; returning presents the current native request.

## Permission modes and interactions

- `manual` presents approvals and questions in chat. It is the default for a new session.
- `auto` approves ordinary tools and skips questions. Plan review still requires a response on Feishu and Telegram.
- `yolo` auto-approves regular tools but may still ask questions.

Feishu renders approvals and questions as interactive cards. Telegram renders approval buttons and a sequential question wizard. An `ExitPlanMode` approval additionally shows a bounded (4,000-character) preview of the plan file — read only from `~/.kimi-code/sessions/**/plans/` — and sends the full plan as a file when the preview truncates. QQ and WeChat cannot present approvals or questions: every session is forced into `auto` mode, and the bridge automatically approves `ExitPlanMode` requests so `/plan on` can finish without interaction. `/mode` explains that the mode is fixed, and `/render-thinking on` explains that separate thinking rendering remains off for those adapters.

Plan mode is enabled explicitly with `/plan on` or Kimi configuration; the agent cannot enter it itself.

## Streaming and thinking

Answers stream through editable messages where the platform supports edits and are split at the platform text limit. Bridge-generated command, status, validation, and runtime error replies are final messages. A post-submission turn failure is sent after any partial answer, and a retryable failure says that the request can be retried. Kimi session warnings are relayed as independent nonterminal messages without ending an active answer stream. If event delivery terminates after internal recovery is exhausted, the bridge sends a final error reply with retry guidance. Background-task lifecycle events remain available through `/tasks` and do not produce unsolicited chat notices.

`/render-thinking on` creates a separately labelled thinking stream where supported and the preference persists per conversation. WeChat has no edit API and emits complete step-boundary output as immutable messages with a 4,000-character limit.

QQ uses a stable-frontier `stream_messages` lifecycle with correction withdrawal when necessary. If a stream frame fails after applicable URL correction or transport retries, the bridge buffers subsequent edits and sends the latest text as a regular message at the next idle or segment boundary. After two failed regular-message delivery attempts, it abandons that response and attempts one in-chat warning; failures are logged, and later segments can proceed.

Goal complete/blocked transitions and background task/subagent outcomes produce short session-labelled notices. Leaving work running by switching, creating, or forking sessions retains observation and its original allowed delivery route across restarts. A notice leaves the current binding unchanged. Initial/reconnect snapshots establish baselines; absent tasks do not imply a completion. The bridge attempts each notice once, logs failed delivery, and drops it. Adapter transport retry behavior still applies.

`/btw` uses Kimi’s native read-only side agent and returns one answer labelled “Side question”. Main work, thinking output, and queued prompts remain independent; ordinary messages continue targeting main. Another `/btw` replaces the previous side question. Switching sessions, archiving, and shutdown close it; a disconnected subscription resumes its journal without resubmitting the question.

## Inbound and outbound media

Feishu accepts text, native images and videos, generic files, and voice messages. QQ accepts C2C text and HTTPS attachments; only supported native image/video values take the native-media route. WeChat accepts private-chat text, images, voice, files, and video. Native image/video input depends on the selected model's `image_in`/`video_in` capability. Local generic files and capability fallbacks use the configured workspace inbox; remote sessions stage those uploads through Kimi in the selected target. Voice messages are transcribed using the configured `[voice.asr]` endpoint first and the platform-native fallback when available.

`/files send <n|path>` sends one regular file authorized by the native session workspace. Native rendering is platform-specific: Feishu, QQ, and WeChat support native image/video forms where documented; WeChat sends audio as a generic downloadable file and cannot send it proactively.

## Current operating limits

- One selected adapter per process and one trusted-operator security model.
- Feishu, QQ, and WeChat handle private chats; QQ is C2C and WeChat has no group or proactive delivery.
- QQ and WeChat force `auto` and do not provide approvals, questions, or separate thinking output.
- Telegram remains experimental and is private-chat only.
- There is no simultaneous multi-adapter process, remote Kimi server mode, generic plugin/UI framework, or remote platform logout operation.
