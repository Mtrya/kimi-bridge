# Configuration

kimi-bridge reads `~/.kimi-bridge/config.toml` by default. `--config <path>` selects another file, and the `KIMI_BRIDGE_CONFIG` environment variable provides an override when the flag is absent. It does not read adapter credentials from environment variables. Only the selected adapter is constructed, so Feishu, QQ and Telegram tables may coexist while one process runs exactly one of them.

## Complete schema

| Key | Type | Default | Rules |
| --- | --- | --- | --- |
| `platform` | string | `"feishu"` | Exactly `"feishu"`, `"telegram"`, or `"qq"`. |
| `log_level` | string | `"INFO"` | Case-insensitive `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `default_workspace` | string path | `"~/.kimi-bridge/workspace"` | Non-empty; `~` is expanded and the result is resolved. |
| `state_path` | string path | `"~/.kimi-bridge/state.json"` | Non-empty; `~` is expanded and the result is resolved. |
| `edit_throttle_seconds` | number | `1.5` | Must be positive and finite. Controls the minimum cadence of streamed message edits. |
| `max_output_seconds` | number | `300.0` | Must be finite and at least `46 * edit_throttle_seconds`, the shortest feasible window for Feishu's 20-edit schedule. |
| `interaction_timeout_seconds` | number | `600.0` | Must be positive. Applies to each approval or question request. |
| `inbox_subdir` | relative string path | `".kimi-bridge-inbox"` | Non-empty, not absolute, and may not contain `..`. |
| `session_list_limit` | integer | `10` | Must be positive. Controls the `/sessions` listing size and the page size of `/sessions search` results. |
| `kimi_server.port` | integer or omitted | omitted | When omitted, an available ephemeral port is selected. An explicit port must be 1–65535. |
| `feishu.app_id` | string | empty | Required with `app_secret` when Feishu is selected. |
| `feishu.app_secret` | string | empty | Required with `app_id` when Feishu is selected. |
| `feishu.allowed_users` | array of strings | empty | At least one non-empty Feishu `open_id` or `user_id` is required at runtime. |
| `telegram.bot_token` | string | empty | Required when Telegram is selected. |
| `telegram.allowed_users` | array of integers | empty | At least one positive numeric Telegram user ID is required at runtime. |
| `qq.app_id` | string | empty | Required with `app_secret` when QQ is selected. |
| `qq.app_secret` | string | empty | Required with `app_id` when QQ is selected. |
| `qq.allowed_users` | array of strings | empty | At least one non-empty QQ C2C `user_openid` is required at runtime. |

Only the keys above have an effect. New sessions start in `manual` permission mode, and separate thinking rendering starts off; these are per-conversation state controlled with `/mode` and `/render-thinking`, not global config fields. QQ forces every session into `auto` permission mode because it cannot present interactive prompts (see [Commands](COMMANDS.md)).

## Feishu example

```toml
platform = "feishu"
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"
edit_throttle_seconds = 1.5
max_output_seconds = 300
interaction_timeout_seconds = 600
inbox_subdir = ".kimi-bridge-inbox"
session_list_limit = 10

[kimi_server]
# Omit port to select an available ephemeral port.
# port = 58628

[feishu]
app_id = "cli_replace_me"
app_secret = "replace-me"
allowed_users = ["ou_replace_me"]
```

Create a custom Feishu app, enable its bot, and make the app available to the intended user. Grant tenant scopes `im:message.p2p_msg:readonly`, `im:message:readonly`, `im:message:send_as_bot`, `im:message:update`, and `im:resource` or the narrower resource upload/download scopes available to the app. Configure a WebSocket long connection, subscribe to `im.message.receive_v1`, enable `card.action.trigger` callbacks on that connection, and publish the app version. Feishu documents [long-connection event setup](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case) and [message permission prerequisites](https://open.feishu.cn/document/server-docs/im-v1/faq).

Feishu accepts only user-sent `p2p` events. Authorization compares the sender's `open_id` and `user_id` with `allowed_users`; group messages and non-allowlisted users are ignored. Use the stable identity issued for the same app/tenant context instead of a display name. When a direct-message sender is not allowlisted, the bridge logs both identities with copy-paste configuration guidance.

## Telegram example (experimental)

```toml
platform = "telegram"
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"
edit_throttle_seconds = 1.5
max_output_seconds = 300
interaction_timeout_seconds = 600
inbox_subdir = ".kimi-bridge-inbox"
session_list_limit = 10

[kimi_server]
# port = 58628

[telegram]
bot_token = "replace-me"
allowed_users = [123456789]
```

Create a bot through Telegram's [BotFather](https://core.telegram.org/bots/features#botfather) and obtain the intended user's stable numeric ID through a trusted Telegram account or Bot API flow. Usernames are mutable and are never accepted for authorization. The adapter uses private-chat long polling, ignores groups, channels, topics, bots, and non-allowlisted users, and drops the startup backlog so instructions sent while it was offline are not replayed. See the official [Telegram Bot API](https://core.telegram.org/bots/api).

The Telegram adapter is experimental and covered by fake Bot API tests, not project live validation. A local installation must complete its own private-chat checks before reporting it as working.

## QQ example

```toml
platform = "qq"
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"
edit_throttle_seconds = 1.5
max_output_seconds = 300
interaction_timeout_seconds = 600
inbox_subdir = ".kimi-bridge-inbox"

[kimi_server]
# port = 58628

[qq]
app_id = "replace-me"
app_secret = "replace-me"
allowed_users = ["replace-me"]
```

Register an app at [q.qq.com](https://q.qq.com/) (individual developers are allowed, up to 5 bots), obtain its `AppID`/`AppSecret`, and whitelist the intended tester's QQ number under the console's sandbox (沙箱) configuration — sandbox allows up to 20 test users and is the practical mode for a personal bridge. The console's developer settings also require an **IP whitelist**: OpenAPI calls fail until this host's egress IP is added there; `kimi-bridge doctor` only reminds you of this requirement and cannot verify it live. A dynamic egress IP must be added again after it changes. `allowed_users` holds the sender's `user_openid`, which the adapter logs with copy-paste guidance the first time an unrecognized sender messages the bot.

The supported QQ adapter is C2C (private-chat) only and is live-validated in sandbox. Validation covered gateway heartbeat/resume, allowlisting and redelivery dedupe, 5,000-character append-monotonic streams, the four-reply passive budget and active fallback, native markdown, base64 media uploads, inbound vision, and clean shutdown. The general OpenAPI host also served the sandbox app, so no sandbox/production URL setting is required.

QQ has no interactive approvals, questions, or separate thinking stream: every session runs in `auto` permission mode, `/mode` and `/render-thinking on` are rejected with an explanatory reply, and an unexpected interactive prompt is replaced by a short notice. `/send` only delivers PNG/JPEG images and MP4 video; every other file type is rejected. Inbound attachments must use HTTPS and are downloaded with a 20 MB limit. Small inbound images reach Kimi as image prompt parts; an image whose base64-expanded prompt exceeds Kimi Code's request-body limit returns an in-chat `Prompt failed` error without stopping the bridge. Sandbox accepted ordinary external Markdown links without returning `304003`; the adapter still retries once with defanged URLs if another deployment enforces that error. See the official [QQ bot documentation](https://bot.q.qq.com/wiki/) for full protocol detail.

## Files and state

- `~/.kimi-bridge/config.toml` contains adapter credentials and should be mode `600` on Linux and macOS. `--config` or `KIMI_BRIDGE_CONFIG` selects a different file.
- `~/.kimi-bridge/state.json` is an atomically replaced, versioned bridge state file. It stores conversation-to-session bindings, workspaces, permission modes, and thinking-rendering preferences, but no adapter credentials. The `state_path` config key selects a different file.
- `~/.kimi-bridge/workspace/` is the default scratch workspace. Use `/new <absolute-or-relative-path>` to bind real project work to another directory.
- `<session workspace>/<inbox_subdir>/` receives inbound files. The configured subdirectory cannot escape its workspace.
- Kimi Code owns its sessions and model/profile state in its own home directory. kimi-bridge does not copy that data into `state.json`.

Relative `default_workspace` values resolve from the bridge process's working directory; prefer `~` or an absolute path. The runtime creates the default workspace when needed. An explicit Kimi server port is normally unnecessary because the server is private to the bridge and binds to loopback.

## Secret handling

Create the parent directory with mode `700` and the file with mode `600`. Never commit the file, paste real values into issue reports, or put credentials on command lines. Feishu's SDK and low-level HTTP/WebSocket protocol loggers can include connection credentials, so the bridge suppresses those dependency loggers below warnings even when bridge-owned DEBUG logging is enabled. Signed inbound attachment URLs are also omitted from bridge logs. Credential diagnostics report only credential presence and allowlist counts; allowlist rejection warnings separately log stable sender identities so the operator can add the intended user.

```bash
install -d -m 700 ~/.kimi-bridge
chmod 600 ~/.kimi-bridge/config.toml
kimi-bridge doctor
```

## Validation and failures

`kimi-bridge doctor` does not start `kimi web` or connect either adapter. It fails for a missing or malformed config, missing selected credentials or allowlist, unusable workspace/state paths, an unrecognized or legacy `kimi` executable, executable/config failures, or another blocking prerequisite. Group/other-readable config permissions and an unlisted official Kimi Code version are warnings.

TOML type, range, and containment violations raise explicit startup errors. A future unknown `state.json` schema fails loudly rather than discarding bindings. An official but unlisted Kimi Code version receives a warning and a live protocol attempt; an executable/server version mismatch is fatal. Run `kimi doctor config` and ensure Kimi Code has an authenticated provider and `default_model` before starting the bridge.
