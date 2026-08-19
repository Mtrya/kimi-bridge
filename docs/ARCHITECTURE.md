# Architecture

kimi-bridge is a single-operator bridge between one instant-messaging adapter and the local Kimi Code server. Linux, macOS, and Windows are supported platforms. One process selects exactly one chat platform; running several platforms requires separate process/configuration/state/workspace and platform-owned storage.

```text
┌──────────────────────────────────────────────┐
│ One selected Feishu, QQ, WeChat, or Telegram  │
│ adapter: native messages and platform auth    │
└──────────────────────┬───────────────────────┘
                       │ semantic platform values
┌──────────────────────▼───────────────────────┐
│ ChatRouter                                    │
│ bindings, commands, interactions, streams,   │
│ workspace-contained outbound authorization    │
└──────────────────────┬───────────────────────┘
                       │ typed Kimi operations/events
┌──────────────────────▼───────────────────────┐
│ KimiServerClient + KimiServerSupervisor       │
│ REST, WebSocket, contract/version checks,     │
│ foreground `kimi web` lifecycle               │
└──────────────────────┬───────────────────────┘
                       │ loopback + bearer token
                 local Kimi Code
```

## Kimi Code boundary

`src/kimi_bridge/kimi_server/` is the only package that knows Kimi Code CLI commands, REST paths, WebSocket envelopes, server materialization, bearer authentication, product identification, and the semantic compatibility contract. It reduces user-visible session warnings and terminal failures to typed safe fields instead of passing raw error payloads across the boundary. The router receives typed session, prompt, interaction, task, goal, model, skill, tool, and runtime-notice values instead of Kimi wire details.

The supervisor identifies official Kimi Code before startup, launches `kimi web --no-open --host 127.0.0.1 --port <port>` as a foreground child, captures its generated bearer token without exposing it, and checks the live server version. `kimi-bridge compat` reports the relationship between the installed Kimi Code version and the bridge compatibility map. The bridge does not provide a remote Kimi server mode.

## Router boundary

`src/kimi_bridge/router/` exposes the `ChatRouter` facade and owns platform-neutral command orchestration, session and stream lifecycle, interaction lifecycle, answer/thinking rendering, outbound-file authorization, formatting, and private runtime state. Model output, nonterminal notices, and final bridge replies use separate semantic sends so an adapter can preserve an active rendering lifecycle. The router maps an IM conversation to a Kimi session and persists bridge-owned bindings and preferences.

The router does not construct Feishu cards, QQ message payloads, WeChat item payloads, Telegram dictionaries, or multipart bodies. Adapters classify native media and expose semantic values; the router uses the selected model's capabilities to decide whether native image/video input becomes a Kimi file-backed prompt part or a workspace-inbox path. Generic files always use the workspace inbox. Voice messages resolve through the optional `[voice.asr]` endpoint and then the selected adapter's native transcription path when available.

## Platform boundaries

`src/kimi_bridge/platforms/base.py` defines the semantic adapter protocol. Platform-native authentication, identity handling, events, uploads, callbacks, and rendering stay within the selected adapter package.

- `src/kimi_bridge/platforms/feishu/` is the Feishu/Lark adapter package. Its `__init__.py` owns API and WebSocket transport, direct-message filtering, identity checks, Markdown messages and edits, uploads, native media, FFmpeg voice conversion and video-cover extraction, native speech recognition, and adapter integration. `auth.py` owns application-registration QR controls, `storage.py` owns the versioned managed credential directory, and `cards.py` owns interactive-card rendering and callback decoding.
- `src/kimi_bridge/platforms/qq/` is the QQ official-bot adapter package. Its `__init__.py` owns REST/token transport, WebSocket gateway lifecycle, C2C filtering, app-scoped `user_openid` allowlisting, attachment handling, streaming, and native outbound media; `auth.py` owns QR credential binding and `storage.py` owns the versioned managed credential directory.
- `src/kimi_bridge/platforms/wechat/` owns WeChat iLink QR authorization, private credential and receive-state storage, HTTP polling, sender allowlisting, cursor/context persistence, retry behavior, immutable text, typing, and encrypted CDN media. It supports private chat only and has no proactive delivery.
- `src/kimi_bridge/platforms/telegram/` owns the experimental Telegram Bot API transport, private numeric identity checks, long polling, media transfers, interaction UI, and public source-contract projection.

Adapters separately expose whether text can be edited and whether interactive prompts are supported. For QQ and WeChat, the router emits immutable or platform-specific streaming messages and keeps permission mode fixed at `auto`; no adapter creates a generic UI schema.

## Authorization and storage lifecycle

Terminal authorization controls are architecturally separate from runtime startup: they operate only the selected adapter's credential control and storage path and never start Kimi Code or message polling. They exist for the Feishu, QQ, and WeChat QR flows; Telegram is configured manually in TOML. Command semantics — platform match/switch, `login --replace`, `status`, `logout` — are defined in [Commands](COMMANDS.md); managed file paths, TOML fallback precedence, and `storage_path` relocation are defined in [Configuration](CONFIGURATION.md).

Managed files and configuration are protected local data. On POSIX systems the bridge creates storage directories with mode `700` and JSON files with mode `600`; Windows relies on the current user's ACL. Adapter credentials are not copied into bridge state, logs, command arguments, or environment variables; the voice ASR API key may use the supported `[voice.asr].api_key_env` setting. Diagnostic output reports presence and redacted metadata only.

## Credential contracts (feasibility record)

This section records the authoritative credential flows behind the QR controls. Both the Feishu and the QQ flow are credential bootstraps for the existing application identity; neither substitutes a user access token for bot credentials.

### Feishu/Lark

- Flow: `kimi-bridge feishu login` runs `lark_oapi.aregister_app(..., addons=...)` without `create_only=True`, so the confirmation page can create or select an application. The fixed add-ons pre-fill the bridge's tenant permissions, `im.message.receive_v1` event, and `card.action.trigger` callback. The flow stores the returned application `client_id`/`client_secret` plus tenant brand, API domain, and optional operator `open_id` metadata. It is not a user OAuth flow, so no user token is stored.
- Token type: application-level long-lived credentials. The bridge still acts as the application, and `lark-oapi` exchanges them for `tenant_access_token` values server-side.
- Scopes: the operator must approve the pre-filled application settings on the confirmation page. Bot capability, publication, and target-user availability remain manual platform steps; when registration returns `user_info.open_id`, QR onboarding merges it into `feishu.allowed_users`.
- Refresh: application credentials have no expiry and no refresh token (none is needed without user OAuth).
- Transport: unchanged REST + WebSocket long connection. Feishu tenants use `https://open.feishu.cn` and Lark tenants `https://open.larksuite.com`; the managed record preserves the returned domain.

### QQ

- Flow: `kimi-bridge qq login` implements the official `@tencent-connect/qqbot-connector` bind flow against `q.qq.com`: create a bind task with a fresh random AES-256 key, show the connect URL, poll every two seconds, and on completed status decrypt `bot_encrypt_secret` locally. It is a bot bind, not QQ user login or OAuth; the temporary key, task ID, QR URL, and encrypted blob are never persisted.
- Token type: application-level `bot_appid` plus the locally decrypted AppSecret, stored as the managed credential.
- Scopes: no user OAuth scopes. Sandbox tester access, production review, and event Intents remain QQ-console prerequisites and are not established by QR completion.
- Refresh: the AppSecret is long-lived; the adapter refreshes the short-lived `access_token` (about 7200 seconds) from the token endpoint using the stored app credentials, exactly as with manually configured credentials.
- Transport: unchanged REST + WebSocket gateway; the QR result is a credential bootstrap only. The optional scanner `user_openid` is an app-scoped identity for the allowlist, not a QQ number or nickname.

### WeChat

`kimi-bridge wechat login` runs WeChat's iLink bot authorization and stores the bot credential plus a stable scanner identity locally. It is supported onboarding, not OAuth, has no TOML credential fallback, and one bot authorization supports one polling process.

## Runtime lifecycle

The foreground runtime loads the selected configuration, builds one adapter, creates the default workspace when needed, starts the supervised loopback Kimi server, and then starts that adapter. Shutdown stops the adapter, router tasks, client, and child process. Persisted conversation bindings live in `state_path`; Kimi owns its own sessions and model/profile state.

Always complete an allowlisted `/status` and normal-prompt message round trip in the foreground before creating a persistent service. Linux may use a systemd user unit; macOS may use a user `launchd` agent; Windows may use a current-user Task Scheduler task. Any service definition must reference local paths without embedding secrets.

## Security model

- The managed Kimi server binds only to `127.0.0.1` and requires its generated bearer token.
- Feishu authorizes stable `open_id`/`user_id` values, QQ authorizes app-scoped C2C `user_openid` values, and WeChat authorizes stable QR-scanner identities. None is a display-name or nickname substitute.
- Platform secrets remain in protected configuration or adapter-owned managed storage. `state.json` never contains adapter credentials.
- `/send` accepts only files within the bound workspace, including symlink containment checks.
- `ExitPlanMode` plan previews read only regular files under `~/.kimi-code/sessions/**/plans/`; any other wire-supplied path is ignored.
- The deployment model is one trusted host account and trusted operator, not tenant isolation. Kimi Code and its tools retain the host account's filesystem, process, and network authority.

## Compatibility policy

`src/kimi_bridge/compatibility-map.json` records Kimi Code versions associated with bridge releases. `kimi-bridge compat` reports whether the installed or requested version is supported by the current bridge, another recorded bridge release, or not established by the map. Startup and `doctor` distinguish official Kimi Code from the incompatible legacy Python `kimi-cli`; an executable/server version mismatch is fatal, while an unlisted official version is warned about and subjected to a live contract attempt.

Platform compatibility has three separate evidence levels. The daily credential-free source monitor checks bounded public official documentation, published SDK surfaces, and official reference source for the Feishu, QQ, WeChat, and Telegram operations used by the bridge. A successful source mismatch is drift, an exhausted fetch is unavailable, and behavior absent from public sources is unverifiable rather than assumed compatible. The monitor retains only source URLs, versions or commits, digests, and classified results; it never authenticates to a platform, advances a receive cursor, opens a gateway, uploads media, sends a message, or prepares a release. Authenticated API acceptance still requires isolated runtime validation, and user-visible delivery still requires a manual round trip on the selected platform.

Protocol-specific source projections live beside their platform adapters. `src/kimi_bridge/platforms/source_contract.py` owns platform-neutral source requests, fetched evidence, reports, validation, and digest logic, while `scripts/check_platform_source_contracts.py` owns bounded fetching, inspection execution, JSON report persistence, and rolling drift-issue synchronization. Pull-request tests use deterministic local fixtures; `.github/workflows/platform-source-contract-monitor.yml` performs the daily networked inspection on `main`.

See [Configuration](CONFIGURATION.md), [Commands](COMMANDS.md), and the [installation guide](../INSTALL.en_US.md) for operator-facing behavior.
