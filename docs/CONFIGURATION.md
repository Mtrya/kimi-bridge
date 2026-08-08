# Configuration

kimi-bridge reads `~/.kimi-bridge/config.toml` by default. `--config <path>` selects another file, and the `KIMI_BRIDGE_CONFIG` environment variable provides an override when the flag is absent. It does not read adapter credentials from environment variables. Only the selected adapter is constructed, so Feishu, QQ, Telegram, and WeChat tables may coexist while one process runs exactly one of them.

## Complete schema

| Key | Type | Default | Rules |
| --- | --- | --- | --- |
| `platform` | string | `"feishu"` | Exactly `"feishu"`, `"telegram"`, `"qq"`, or `"wechat"`. |
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
| `wechat.allowed_users` | array of strings | empty | May be empty only during QR bootstrap; runtime requires at least one stable scanner identity. |
| `wechat.storage_path` | string path | `"~/.kimi-bridge/wechat"` | Private adapter-owned credentials and receive state; `~` is expanded and the result is resolved. |
| `voice.asr.base_url` | string | table omitted | Required non-empty when `[voice.asr]` is present; base URL before `/audio/transcriptions`. |
| `voice.asr.model` | string | table omitted | Required non-empty when `[voice.asr]` is present. |
| `voice.asr.api_key` | string | empty | Optional Bearer token; may stay empty for local servers that do not check one. |
| `voice.asr.api_key_env` | string | empty | Optional environment-variable name containing the Bearer token; mutually exclusive with `api_key`. |
| `voice.asr.request_format` | `"multipart"` or `"json"` | `"multipart"` | `multipart` sends an OpenAI/Whisper-compatible file upload; `json` sends base64 in `input_audio`. |
| `voice.asr.language` | string | empty | Optional language hint forwarded to the external service. |

Only the keys above have an effect. New sessions start in `manual` permission mode, and separate thinking rendering starts off; these are per-conversation state controlled with `/mode` and `/render-thinking`, not global config fields. QQ and WeChat force every session into `auto` permission mode because they cannot present interactive prompts (see [Commands](COMMANDS.md)).

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

Feishu app creation, the exact scopes and event/callback subscriptions the bridge requires, the app-version publish step, and `open_id` discovery are covered in the [Feishu bootstrap](../INSTALL_AI.md#5-feishu-bootstrap). Feishu documents [long-connection event setup](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case) and [message permission prerequisites](https://open.feishu.cn/document/server-docs/im-v1/faq).

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

Bot creation through Telegram's BotFather and numeric user-ID discovery via the bridge's own rejection log are covered in the [Telegram bootstrap](../INSTALL_AI.md#8-telegram-bootstrap). Usernames are mutable and are never accepted for authorization. The adapter uses private-chat long polling, ignores groups, channels, topics, bots, and non-allowlisted users, and drops the startup backlog so instructions sent while it was offline are not replayed. When a non-allowlisted user messages the bot, the bridge logs their numeric user ID with copy-paste configuration guidance. See the official [Telegram Bot API](https://core.telegram.org/bots/api).

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

Bot registration at [q.qq.com](https://q.qq.com/), sandbox tester access, and `user_openid` discovery are covered in the [QQ bootstrap](../INSTALL_AI.md#6-qq-bootstrap) and the dated [verified QQ setup path](setup-paths/qq.md). The current adapter receives C2C messages over QQ's WebSocket gateway; its verified setup does not configure callbacks, event webhooks, or an IP whitelist. Investigate those controls only if QQ returns a specific error that requires one. `allowed_users` holds the sender's `user_openid`, which the adapter logs with copy-paste guidance whenever an unrecognized sender messages the bot.

The supported QQ adapter is C2C (private-chat) only and its core lifecycle is live-validated in sandbox. Validation covered gateway heartbeat/resume, allowlisting and redelivery dedupe, 5,000-character append-monotonic streams, the four-reply passive budget and active fallback, native markdown, base64 outbound media uploads, native inbound media, and clean shutdown. Streaming exposes complete lines and closed fenced blocks through one non-expanding compact rendering strategy; immutable messages retain richer one-shot formatting. A correction to the rendered frontier uses QQ's message-withdrawal endpoint before sending the corrected final response. That exceptional path is contract-tested and requires a credentialed sandbox check before merge. The general OpenAPI host also served the sandbox app, so no sandbox/production URL setting is required.

QQ has no interactive approvals, questions, or separate thinking stream: every session runs in `auto` permission mode, `/mode` and `/render-thinking on` are rejected with an explanatory reply, and an unexpected interactive prompt is replaced by a short notice. `/send` delivers PNG/JPEG images and MP4 video as native media and every other file type as a file card (QQ's `file_type=4`, 200 MB hard limit). Inbound attachments must use HTTPS and are downloaded with a 20 MB limit. Sandbox accepted ordinary external Markdown links without returning `304003`; the adapter still retries once with defanged URLs if another deployment enforces that error. See the official [QQ bot documentation](https://bot.q.qq.com/wiki/) for full protocol detail.

## WeChat example (experimental)

```toml
platform = "wechat"
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"
inbox_subdir = ".kimi-bridge-inbox"

[kimi_server]
# port = 58628

[wechat]
# Leave empty only for the first QR login, then replace it with the scanner ID.
allowed_users = []
storage_path = "~/.kimi-bridge/wechat"
```

WeChat authorization is local and separate from chat commands. With the protected config above, run `kimi-bridge wechat login`, open the printed WeChat URL, complete any verification-code step shown by WeChat, and copy the returned stable scanner identity into `wechat.allowed_users`. Then run `kimi-bridge wechat status` and `kimi-bridge doctor` before foreground startup. `status` inspects only local files and never tests the network.

`kimi-bridge wechat login --replace` starts a new QR flow while preserving the prior credential until Tencent confirms a replacement. If Tencent reports the already-bound bot, the existing local credential is retained. An expired runtime authorization terminates with the same `login --replace` recovery instruction instead of retrying indefinitely. `kimi-bridge wechat logout` removes only the adapter-owned local credential and receive-state files; it does not remotely delete the WeChat bot binding.

The storage directory must remain private and its adapter-owned JSON files are written with mode `600` on POSIX systems. Exactly one process may poll a bot authorization. Multiple configurations must use distinct `storage_path`, `state_path`, and workspace values as well as distinct bot authorizations. Do not run OpenClaw or another iLink consumer against the same bot while kimi-bridge is active.

The WeChat adapter is experimental, private-chat only, QR-authorized, and was live-validated on 2026-08-08 against Tencent source tag `v2.4.6`. It forces `auto`, emits immutable replies in chunks of at most 4,000 characters, and provides no approvals, questions, separate thinking stream, editable messages, group chat, or proactive delivery. Live validation covered one allowlisted scanner; isolation between two simultaneous allowlisted senders remains contract-tested rather than project-live-validated.

## Inbound media

Feishu, QQ, and WeChat native image/video messages become model input when the bound session model advertises `image_in`/`video_in`: the bridge uploads the bytes through Kimi `/files` and submits a file-backed media prompt part. If the corresponding capability is absent, the media is saved under `<session workspace>/<inbox_subdir>/` and its path is included in prompt text.

Generic file messages always use the workspace inbox, even when the filename or media type indicates image or video content. The platform adapter's native classification is authoritative; the router does not sniff or reclassify generic files. Upload failures are reported as `Prompt failed` and do not silently select the inbox fallback. See [Inbound media policy](ARCHITECTURE.md#inbound-media-policy) for the decision table.

## Voice messages

Feishu audio messages and QQ/WeChat voice attachments are transcribed to text rather than delivered as files. Transcription resolves in layers: the configured `[voice.asr]` HTTP endpoint is tried first when present, and the selected adapter's native transcription method is called only when the external endpoint yields no transcript. QQ uses `asr_refer_text`, WeChat uses its native voice transcript, and Feishu converts the downloaded Opus resource to 16 kHz mono signed 16-bit PCM with FFmpeg and calls `speech_to_text` file recognition. When no layer yields text, a system notice inside the prompt tells the agent a voice message could not be transcribed; the user is never sent an error reply. The transcript enters the prompt prefixed with `[语音转写]` to mark it explicitly as machine-transcribed speech.

Speech recognition is best-effort text, not an exact command channel: native services may normalize punctuation, casing, abbreviations, or unfamiliar tokens even when the audio conversion and request succeed.

```toml
[voice.asr]
base_url = "https://api.openai.com/v1"
api_key = "sk-replace-me"  # optional for local servers
# api_key_env = "ASR_API_KEY"  # alternative to api_key
model = "whisper-1"
request_format = "multipart"
# language = "en"
```

For a JSON/base64 endpoint, use `request_format = "json"`; the bridge derives `input_audio.format` from the inbound filename or media type and sends `model`, `input_audio`, and the optional `language` field. Feishu audio is identified as Opus, QQ uses its WAV conversion when available, and WeChat preserves the native encoding identified by the event. For example:

```toml
[voice.asr]
base_url = "https://zenmux.ai/api/v1"
api_key_env = "ZENMUX_API_KEY"
model = "qwen/qwen3-asr-flash"
request_format = "json"
language = "en"
```

`api_key_env` resolves the external ASR token when the configuration is loaded and fails startup if the named variable is absent or empty. Feishu, Telegram, and QQ credentials remain in the protected TOML file; WeChat QR credentials remain in its private adapter storage.

Selecting Feishu requires a working `ffmpeg` executable on `PATH`; startup fails and `doctor` reports an error when it is missing. Feishu-native recognition also requires the exact tenant scope `speech_to_text:speech`. Without that scope the bridge logs a warning and relies on `[voice.asr]` when configured, or reports the message as untranscribable. See the [Feishu bootstrap](../INSTALL_AI.md#5-feishu-bootstrap) for prerequisite, permission, and live-validation steps. QQ voice attachments download the platform-provided WAV conversion (`voice_wav_url`) when available, otherwise the original encoding.

## Files and state

- `~/.kimi-bridge/config.toml` contains adapter configuration plus Feishu, Telegram, or QQ credentials and should be mode `600` on Linux and macOS. WeChat credentials are stored separately. `--config` or `KIMI_BRIDGE_CONFIG` selects a different file.
- `~/.kimi-bridge/state.json` is an atomically replaced, versioned bridge state file. It stores conversation-to-session bindings, workspaces, permission modes, and thinking-rendering preferences, but no adapter credentials. The `state_path` config key selects a different file.
- `~/.kimi-bridge/wechat/` is the default WeChat credential and durable receive-state directory. `wechat.storage_path` relocates it; do not share it between bot consumers.
- `~/.kimi-bridge/workspace/` is the default scratch workspace. Use `/new <absolute-or-relative-path>` to bind real project work to another directory.
- `<session workspace>/<inbox_subdir>/` receives generic inbound files and native images/videos unsupported by the bound model. The configured subdirectory cannot escape its workspace.
- Kimi Code owns its sessions and model/profile state in its own home directory. kimi-bridge does not copy that data into `state.json`.

Relative `default_workspace` values resolve from the bridge process's working directory; prefer `~` or an absolute path. The runtime creates the default workspace when needed. An explicit Kimi server port is normally unnecessary because the server is private to the bridge and binds to loopback.

## Secret handling

Create the parent directory with mode `700` and the file with mode `600`. Never commit the file, paste real values into issue reports, or put credentials on command lines. WeChat QR credentials stay in `wechat.storage_path`, not in TOML or environment variables. Feishu's SDK and low-level HTTP/WebSocket protocol loggers can include connection credentials, so the bridge suppresses those dependency loggers below warnings even when bridge-owned DEBUG logging is enabled. Signed inbound attachment URLs and WeChat context/CDN tokens are also omitted from bridge logs. Credential diagnostics report only credential presence and allowlist counts; allowlist rejection warnings separately log stable sender identities so the operator can add the intended user.

```bash
install -d -m 700 ~/.kimi-bridge
chmod 600 ~/.kimi-bridge/config.toml
kimi-bridge doctor
```

## Validation and failures

`kimi-bridge doctor` does not start `kimi web` or connect any adapter. It fails for a missing or malformed config, missing selected credentials or allowlist, unusable workspace/state paths, unusable WeChat storage or required media dependency, an unrecognized or legacy `kimi` executable, executable/config failures, or another blocking prerequisite. Group/other-readable config permissions, unknown configuration keys (silently ignored at runtime), and an unlisted official Kimi Code version are warnings.

TOML type, range, and containment violations raise explicit startup errors. A future unknown `state.json` schema fails loudly rather than discarding bindings. An official but unlisted Kimi Code version receives a warning and a live protocol attempt; an executable/server version mismatch is fatal. Run `kimi doctor config` and ensure Kimi Code has an authenticated provider and `default_model` before starting the bridge.
