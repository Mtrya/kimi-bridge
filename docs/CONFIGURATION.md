# Configuration

kimi-bridge reads `~/.kimi-bridge/config.toml` by default. `--config PATH` selects another file, and `KIMI_BRIDGE_CONFIG` is used when the flag is absent. Adapter credentials are not read from environment variables; a voice ASR API key may be configured through `[voice.asr].api_key_env`. The config may contain several platform tables, but one process constructs exactly the adapter named by `platform`.

## Core schema

| Key | Type | Default | Rules |
| --- | --- | --- | --- |
| `platform` | string | `"feishu"` | One of `feishu`, `telegram`, `qq`, or `wechat`. |
| `log_level` | string | `"INFO"` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `default_workspace` | path | `~/.kimi-bridge/workspace` | Non-empty path; `~` is expanded. |
| `state_path` | path | `~/.kimi-bridge/state.json` | Bridge-owned versioned state; `~` is expanded. |
| `edit_throttle_seconds` | number | `1.5` | Positive and finite. |
| `max_output_seconds` | number | `300` | Positive and finite; must leave room for Feishu's edit schedule. |
| `interaction_timeout_seconds` | number | `600` | Positive; applies to approval/question requests. |
| `inbox_subdir` | relative path | `.kimi-bridge-inbox` | Must remain inside the session workspace. |
| `session_list_limit` | integer | `10` | Positive. |
| `kimi_server.port` | integer or omitted | omitted | If present, must be 1–65535; omitted selects an available port. |

Only these keys have an effect. Permission mode and thinking rendering are per-conversation settings controlled with `/mode` and `/render-thinking`. QQ and WeChat force `auto` because they cannot present interactive prompts.

## Credential and allowlist keys

| Table/key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `feishu.app_id` / `feishu.app_secret` | strings | empty | Optional complete TOML fallback when Feishu is selected. If either value is set, both must be set. |
| `feishu.allowed_users` | string array | empty | At runtime, at least one Feishu `open_id` or `user_id` is required. QR login adds the returned registration `open_id` automatically. |
| `feishu.storage_path` | path | `~/.kimi-bridge/feishu` | Managed Feishu credential directory; file is `credentials.json`. |
| `qq.app_id` / `qq.app_secret` | strings | empty | Optional complete TOML fallback when QQ is selected. If either value is set, both must be set. |
| `qq.allowed_users` | string array | empty | At runtime, at least one app-scoped QQ C2C `user_openid` is required. QR login adds the returned scanner `user_openid` automatically. |
| `qq.storage_path` | path | `~/.kimi-bridge/qq` | Managed QQ credential directory; file is `credentials.json`. |
| `wechat.allowed_users` | string array | empty | Empty only during QR bootstrap; runtime requires at least one stable scanner identity. |
| `wechat.storage_path` | path | `~/.kimi-bridge/wechat` | WeChat credential and adapter receive-state directory; credential file is `credentials.json`. |
| `telegram.bot_token` | string | empty | Required when Telegram is selected. |
| `telegram.allowed_users` | positive integer array | empty | Required when Telegram is selected. |

The default managed files are:

```text
~/.kimi-bridge/feishu/credentials.json
~/.kimi-bridge/qq/credentials.json
~/.kimi-bridge/wechat/credentials.json
```

WeChat also keeps adapter-owned receive state in the same storage directory. WeChat QR credentials are never written to TOML or environment variables.

## Managed credentials and TOML fallback

Feishu and QQ support either the QR-managed credential or a complete TOML pair. Startup uses this precedence:

1. No managed credential file: use the complete TOML `app_id` + `app_secret` pair.
2. Managed credential exists and is valid: use it in preference to TOML. Feishu also uses its stored tenant brand and API domain.
3. Managed credential exists but is unreadable, unsafe, malformed, or invalid: fail startup; repair the storage or run the matching `login --replace`.

`feishu logout` and `qq logout` remove only adapter-owned files in their configured `storage_path`; they do not remove `[feishu]` or `[qq]` TOML values. WeChat has no TOML credential fallback. See [QR onboarding](QR_ONBOARDING.md) for the human flow and command semantics.

## Feishu examples

### QR-managed application registration

```toml
platform = "feishu"
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[feishu]
storage_path = "~/.kimi-bridge/feishu"
allowed_users = []
```

Run `kimi-bridge feishu login` and open the URL printed by the command in a browser. The page lets the operator create a new application or select an existing one, and pre-fills the tenant permissions, `im.message.receive_v1` event, and `card.action.trigger` callback required by the bridge. The result is an application credential, not a user OAuth token.

The operator must approve the pre-filled settings, confirm bot capability and any console settings not represented by the registration add-ons, publish an application version, and make it available to the intended user/tenant. When registration returns `user_info.open_id`, login merges that identity into `feishu.allowed_users`; edit or remove it afterward if the registering user should not be authorized. If no identity is returned, obtain the target user's `open_id` in the same app/tenant context manually.

After those steps, review the generated array or replace it with the intended identity:

```toml
[feishu]
allowed_users = ["<real open_id from this app and tenant>"]
```

### Complete TOML fallback

```toml
platform = "feishu"

[feishu]
app_id = "cli_replace_me"
app_secret = "replace_me"
storage_path = "~/.kimi-bridge/feishu"
allowed_users = ["<real Feishu open_id>"]
```

Replace the marker with a real identity before startup. The pair is used only when `~/.kimi-bridge/feishu/credentials.json` (or the configured path) is absent. A fallback configured in TOML uses the standard Feishu API domain; a QR-managed record preserves the Feishu/Lark domain returned during registration.

Feishu accepts direct messages from allowlisted users. Use the stable identity issued for the same app and tenant, not a display name. Selecting Feishu also requires a working `ffmpeg` executable on PATH for inbound voice messages.

## QQ examples

### QR-managed bot credential bootstrap

```toml
platform = "qq"
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[qq]
storage_path = "~/.kimi-bridge/qq"
allowed_users = []
```

Run `kimi-bridge qq login`, scan and approve the official bot bind URL. When the flow returns a scanner `user_openid`, login merges it into `qq.allowed_users`. The QR result contains `bot_appid` and encrypted `bot_encrypt_secret`; the bridge decrypts the AppSecret locally and persists only the final managed credential. The temporary key, bind task, QR URL, and encrypted blob are not persisted. A scanner `user_openid` is app-scoped and must not be replaced by a QQ number or nickname.

If the flow does not return an identity, configure `qq.allowed_users` manually. QR completion does not automatically provide all sandbox/review/event/gateway prerequisites. Confirm current QQ platform requirements separately before starting the foreground bridge.

After the flow succeeds, review the generated array or replace it with the returned identity:

```toml
[qq]
allowed_users = ["<real user_openid returned by this flow>"]
```

### Complete TOML fallback

```toml
platform = "qq"

[qq]
app_id = "replace_me"
app_secret = "replace_me"
storage_path = "~/.kimi-bridge/qq"
allowed_users = ["<real QQ user_openid>"]
```

Replace the marker with a real identity before startup. The pair is used only when the managed file is absent. QQ handles C2C private messages.

### QQ runtime diagnostics

QQ gives gateway Hello and Identify/Resume setup 30 seconds to complete. If setup stalls, the bridge closes that attempt and reconnects through its existing backoff loop. At `INFO`, the QQ adapter writes one-line traces for accepted inbound C2C messages and successful outbound direct or streaming frames. Each trace includes a bounded preview of up to 60 characters after escaping non-printable characters. Previews can contain message content, so apply the same access and retention controls as the messages themselves. Set `log_level = "WARNING"` or higher to suppress these records; there is no separate QQ trace toggle.

## Telegram example

Telegram has no QR or terminal authorization command. Use the official Telegram Bot API and BotFather flow to create a bot and obtain its token, then store the token and the numeric user allowlist in TOML:

```toml
platform = "telegram"
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[telegram]
bot_token = "<token from BotFather>"
allowed_users = [123456789]
```

The token is `[telegram].bot_token`; `allowed_users` must contain positive numeric Telegram user IDs, not usernames or display names. Telegram remains experimental and accepts private chats only. At startup it takes over long polling and drops pending updates.

## WeChat example

```toml
platform = "wechat"
log_level = "INFO"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[wechat]
storage_path = "~/.kimi-bridge/wechat"
allowed_users = []
```

Leave the allowlist empty only while running `kimi-bridge wechat login`. Open the printed URL in WeChat, scan and approve the iLink bot authorization, enter a verification code only if requested, and then put the returned stable scanner identity in `wechat.allowed_users`. The credential is stored at `wechat.storage_path`, not in TOML.

Run `kimi-bridge wechat status` to inspect local storage; it does not perform a network check. Then run `kimi-bridge doctor` and start the bridge in the foreground. The runtime requires at least one allowlisted identity and one polling process per bot authorization. WeChat is private-chat only, emits immutable replies, and supports inbound image/voice/file/video plus outbound image/video/file; it has no group chat, proactive delivery, or separate thinking stream.

`kimi-bridge wechat login --replace` preserves the existing credential until a replacement is confirmed. `kimi-bridge wechat logout` removes only adapter-owned credential and receive-state files; it does not remove the remote bot binding.

## Voice messages

An optional `[voice.asr]` table configures an HTTP transcriber. The selected adapter tries it first, then uses its native transcription path when available. Voice is submitted as transcribed text, not as a generic inbox file.

```toml
[voice.asr]
base_url = "https://api.openai.com/v1"
api_key = "replace-me" # optional for a local endpoint
# api_key_env = "ASR_API_KEY" # alternative to api_key
model = "whisper-1"
request_format = "multipart"
# language = "en"
```

`api_key` and `api_key_env` are mutually exclusive. Do not put secrets on command lines or in source control.

## Files, state, and secret handling

- `~/.kimi-bridge/config.toml` contains platform configuration, the Telegram bot token, and optional complete Feishu/QQ TOML fallback credentials. Use mode `600` on Linux/macOS, or a current-user ACL on Windows.
- `~/.kimi-bridge/state.json` stores bridge-owned conversation bindings and preferences, not adapter credentials. Use `state_path` to relocate it.
- `feishu.storage_path`, `qq.storage_path`, and `wechat.storage_path` relocate private adapter-owned storage. The application creates POSIX directories/files with `700`/`600` permissions; Windows uses the host ACL model.
- `<session workspace>/<inbox_subdir>/` receives generic inbound files and native media that the selected model cannot consume directly.

Never commit adapter credentials or paste them into chat or issue reports. Adapter credentials are stored in the protected config or adapter-owned local storage; the voice ASR API key may instead use the supported `[voice.asr].api_key_env` setting. `kimi-bridge doctor` reports credential presence and safe metadata, not secret values.

## Validation and failures

`kimi-bridge doctor` does not start `kimi web`, connect to a chat platform, verify platform permissions, receive an event, or send a message. It checks local configuration, selected-adapter credential presence, paths, Kimi Code configuration, Feishu FFmpeg when selected, and WeChat's encrypted-media dependency when selected.

After every local check, run `kimi-bridge` in the foreground and verify a real allowlisted `/status` plus a normal prompt with a complete answer. Configure a persistent service only after this succeeds.

See [Commands](COMMANDS.md), [QR onboarding](QR_ONBOARDING.md), and [Architecture](ARCHITECTURE.md) for the related operator contracts.
