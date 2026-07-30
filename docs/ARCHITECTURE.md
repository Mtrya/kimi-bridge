# Architecture

kimi-bridge is an always-on, single-operator process that translates one instant-messaging adapter into Kimi Code's local server contract. Linux is the supported platform; macOS and Windows are experimental and CI-tested with fakes, not live-validated. It deliberately keeps Kimi protocol details, chat semantics, and platform-native UI in separate boundaries.

```text
┌──────────────────────────────────────────────┐
│ Feishu WebSocket, Telegram long polling, or  │
│ QQ WS gateway — native messages, uploads     │
└──────────────────────┬───────────────────────┘
                       │ semantic platform values
┌──────────────────────▼───────────────────────┐
│ ChatRouter                                    │
│ bindings, commands, interactions, streams,   │
│ workspace-contained outbound authorization   │
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

## Kimi boundary

`src/kimi_bridge/kimi_server/` is the only package that knows Kimi Code CLI commands, REST paths, WebSocket envelopes, wire event shapes, server materialization, bearer authentication, product fingerprinting, or the semantic compatibility contract. Public client methods expose typed session, interaction, prompt, task, goal, model, skill, and tool operations to the router. For native prompt media, the client uploads bytes through Kimi's file endpoint and translates semantic image/video values into file-backed prompt parts; multipart fields, file IDs, and prompt source objects do not escape this boundary. Kimi can re-emit uploaded media as data URLs in session events, so the authenticated loopback WebSocket accepts frames larger than the transport library's default 1 MiB limit.

The supervisor fingerprints official Kimi Code before startup, launches `kimi web --no-open --host 127.0.0.1 --port <port>` as a foreground child, captures its generated bearer token without exposing it, and verifies the live `/api/v1/meta` version after startup. The client materializes a stored session through its public status endpoint before each initial or reconnected WebSocket subscription. These lifecycle rules do not leak into the router.

## Router boundary

`src/kimi_bridge/router/` exposes one `ChatRouter` facade and splits command orchestration, session/stream lifecycle, interaction lifecycle, answer/thinking rendering, outbound-file authorization, formatting, and private runtime state into focused modules. It maps an IM conversation to one Kimi session, persists the bridge-owned fields, and translates typed events into semantic platform operations.

The router never constructs Feishu cards, Telegram dictionaries, multipart bodies, or native platform payloads. Answer and thinking streams have independent buffers and edit lifecycles. Adapters expose their text and per-message edit limits so router-side chunking and edit budgeting keep platform limits out of the Kimi client. Outbound files are resolved and authorized against the bound workspace before an adapter chooses how to upload them. Prompt-submission and upload transport failures are reported in chat without terminating the selected adapter.

### Inbound media policy

The adapter classifies each inbound payload from platform-native message or attachment metadata. The router then uses the bound session's actual model capabilities; it never reclassifies a generic file from its extension or MIME type.

| Adapter value | Bound model capability | Router action |
| --- | --- | --- |
| Native image | `image_in` | Upload through Kimi `/files`; submit a file-backed image prompt part. |
| Native video | `video_in` | Upload through Kimi `/files`; submit a file-backed video prompt part. |
| Native image or video | Corresponding capability absent | Save to the workspace inbox and include the path in prompt text. |
| Generic file, including image/video content | Any | Always save to the workspace inbox and include the path in prompt text. |

Only capability absence selects the inbox fallback. A Kimi upload failure remains a visible `Prompt failed` error rather than silently changing the semantic route.

## Platform boundary

`src/kimi_bridge/platforms/base.py` defines conversation, actor, message, native image, native video, generic file, interaction, and outcome values plus the semantic adapter protocol. `send_text` opens text that model rendering may edit, while `send_final_text` delivers immutable bridge-generated replies without a streaming-finalization delay. Each adapter declares two capability booleans on the protocol: `supports_edits` (whether `send_text` results can later be replaced in place with `edit_text`) and `supports_interactions` (whether the adapter can present approval/question prompts at all). The runtime selects exactly one adapter and constructs only that adapter's credentials and dependencies. For an edit-less adapter, the router's rendering module buffers and flushes complete segments as new messages instead of editing a tail message in place; for an interaction-less adapter, the router forces sessions into `auto` permission mode and rejects commands that would otherwise need a prompt.

`platforms/feishu.py` and `feishu_cards.py` own the `lark-oapi` WebSocket lifecycle, p2p filtering, Feishu identity checks, Markdown posts, edits, uploads, native image/video messages, generic file messages, card JSON, and callback decoding. Packaged native-rendering assets are loaded through Python package resources so wheel installs work.

`platforms/telegram.py` owns a narrow handwritten `httpx` Bot API transport, private-chat numeric identity checks, long polling, startup-backlog removal, retry behavior, persistent send/edit streaming, multipart transfers, inline approval keyboards, sequential question state, callback tokens, and `ForceReply` custom answers. Its UI state is memory-only. The adapter is fake-tested but not live-validated.

`platforms/qq.py` owns the entire QQ official-bot boundary: access-token refresh, the REST client, the WebSocket gateway client (identify/heartbeat/resume), inbound dedupe and allowlisting, attachment classification, the markdown sanitizer, and outbound delivery. QQ-declared image and video attachment media types become native image/video values; every other attachment remains a generic file. It sets `supports_edits = True` by mapping the router's edit contract onto QQ's C2C-only `stream_messages` API. Live behavior requires every continuation, including DONE, to strictly extend the delivered prefix even though the API names its mode `replace`; the adapter therefore renders only a stable frontier of complete lines and closed fenced blocks, uses one non-expanding compact rendering strategy for the lifetime of each editable stream, serializes stream finalization, and uses an invisible DONE suffix. Immutable messages retain the richer one-shot sanitizer. A snapshot revision is accepted while it changes only the buffered tail; if it changes the rendered frontier, the adapter withdraws the partial response and sends the corrected final response as a fresh active message. If withdrawal fails or has expired, it finishes the retained partial when possible, logs the failure, and still sends the corrected response. Opening a stream consumes one passive-reply slot, continuing it does not, and snapshots are coalesced into one active final send once the four-slot budget or its 60-minute window is exhausted. An exhausted transport failure during deferred delivery is retried after another idle interval; permanent API and protocol errors are logged without an automatic retry loop. It sets `supports_interactions = False` and answers an unexpected interaction prompt with a defensive notice instead of rendering one. Outbound images (PNG/JPEG) and video (MP4) upload through the same REST client and send as native media; every other outbound file type raises so the router's existing `/send` failure path surfaces it. The C2C adapter is supported and its core lifecycle is live-validated in QQ sandbox; the correction-withdrawal path is contract-tested and requires a credentialed sandbox check before merge.

## State and lifecycle

Bridge state is stored atomically at `~/.kimi-bridge/state.json` (relocatable with the `state_path` config key). Its versioned schema contains conversation bindings, workspace, permission mode, and thinking-rendering preference. Known older schemas migrate without losing bindings; an unknown future version fails loudly. Kimi remains authoritative for sessions, profiles, model/effort/plan settings, usage, tasks, and goals.

The config file is `~/.kimi-bridge/config.toml` by default; `--config` or the `KIMI_BRIDGE_CONFIG` environment variable selects another file. Generic inbound files and native media unsupported by the selected model live under a configured subdirectory of the bound workspace. Startup creates the default workspace, starts the supervised local server, then starts one adapter. Shutdown stops the adapter, stream tasks, client, and child process. A crashed Kimi child can be restarted by the supervisor; session subscriptions are re-established through the client boundary.

## Security model

- The managed server listens only on `127.0.0.1` and requires its generated bearer token.
- Feishu accepts allowlisted direct-message users by `open_id` or `user_id`; Telegram accepts allowlisted positive numeric user IDs in private chats; QQ accepts allowlisted C2C sender `user_openid` values.
- Adapter secrets live in a local mode-`600` config file and are never persisted in bridge state. Doctor output projects only presence and counts.
- `/send` resolves the requested regular file and rejects paths or symlinks that escape the bound workspace.
- The deployment model is one trusted host account and trusted chat operator. It is not a tenant isolation boundary. Kimi Code and its tools retain the host account's authorized filesystem, process, and network capabilities.
- The per-user service does not add sandboxing that would silently break authorized coding workspaces or tools.

## Compatibility policy

The packaged `compatibility-map.json` records the Kimi Code versions that passed the tracked semantic contract for each bridge release; its latest entry is the current bridge's supported set. `kimi-bridge compat` reports whether an installed or given Kimi Code version is tested with this bridge, which other bridge releases tested it, or that it is newer/older than every tested version. Startup and `doctor` identify the product from both version and help surfaces:

- a listed official version is supported;
- an unlisted official version receives a loud warning and a live contract attempt;
- legacy Python `kimi-cli`, an unrecognized product, or an executable/server version mismatch fails;
- the daily credential-free canary installs the latest official Kimi Code in an empty home on Linux, macOS, and Windows and exercises the CLI/server contract without model inference; when every platform passes with the same unlisted version, automation prepares one PR that bumps the bridge patch version and appends a compatibility-map release entry containing the promoted Kimi Code version, then required checks gate auto-merge;
- a marked compatibility promotion PR creates its GitHub Release after merge and directly invokes the reusable release workflow; hourly reconciliation covers GitHub token event suppression, while the protected PyPI environment retains its separate approval boundary;
- pull requests touching compatibility or release surfaces run the same canary and predict the synchronization decision in dry-run mode, so only main mutates promotion or drift state;
- contract failure uses one rolling issue rather than opening a new noisy issue every day.

All raw protocol knowledge and the tracked semantic contract stay in `kimi_server`. Hosted tests use no Kimi account, chat credential, or inference.

## Intentional limits

kimi-bridge does not currently provide mutually untrusted multi-user isolation, simultaneous adapters in one process, a generic capability/UI/plugin framework, remote Kimi server operation, tool-call or transcript rendering, Telegram webhooks/groups/topics/albums, a Telegram framework dependency, QQ group chat or webhook transport, WeChat, or automatic semantic version selection. These are product decisions rather than missing abstractions to route around.

See [Configuration](CONFIGURATION.md), [Commands](COMMANDS.md), and the [installation runbook](../INSTALL.md) for operator-facing contracts.
