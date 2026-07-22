# Architecture

kimi-bridge is an always-on, single-operator Linux process that translates one instant-messaging adapter into Kimi Code's local server contract. It deliberately keeps Kimi protocol details, chat semantics, and platform-native UI in separate boundaries.

```text
┌──────────────────────────────────────────────┐
│ Feishu WebSocket or Telegram long polling    │
│ native messages, uploads, cards/keyboards    │
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

`src/kimi_bridge/kimi_server/` is the only package that knows Kimi Code CLI commands, REST paths, WebSocket envelopes, wire event shapes, server materialization, bearer authentication, product fingerprinting, or the semantic compatibility contract. Public client methods expose typed session, interaction, prompt, task, goal, model, skill, and tool operations to the router.

The supervisor fingerprints official Kimi Code before startup, launches `kimi web --no-open --host 127.0.0.1 --port <port>` as a foreground child, captures its generated bearer token without exposing it, and verifies the live `/api/v1/meta` version after startup. The client materializes a stored session through its public status endpoint before each initial or reconnected WebSocket subscription. These lifecycle rules do not leak into the router.

## Router boundary

`src/kimi_bridge/router/` exposes one `ChatRouter` facade and splits command orchestration, session/stream lifecycle, interaction lifecycle, answer/thinking rendering, outbound-file authorization, formatting, and private runtime state into focused modules. It maps an IM conversation to one Kimi session, persists the bridge-owned fields, and translates typed events into semantic platform operations.

The router never constructs Feishu cards, Telegram dictionaries, multipart bodies, or native media choices. Answer and thinking streams have independent buffers and edit lifecycles. Router-side chunking keeps platform limits out of the Kimi client. Outbound files are resolved and authorized against the bound workspace before an adapter chooses how to upload them.

## Platform boundary

`src/kimi_bridge/platforms/base.py` defines conversation, actor, message, image, file, interaction, and outcome values plus the semantic adapter protocol. The runtime selects exactly one adapter and constructs only that adapter's credentials and dependencies.

`platforms/feishu.py` and `feishu_cards.py` own the `lark-oapi` WebSocket lifecycle, p2p filtering, Feishu identity checks, Markdown posts, edits, uploads, native media messages, card JSON, and callback decoding. Packaged native-rendering assets are loaded through Python package resources so wheel installs work.

`platforms/telegram.py` owns a narrow handwritten `httpx` Bot API transport, private-chat numeric identity checks, long polling, startup-backlog removal, retry behavior, persistent send/edit streaming, multipart transfers, inline approval keyboards, sequential question state, callback tokens, and `ForceReply` custom answers. Its UI state is memory-only. The adapter is fake-tested but not live-validated.

## State and lifecycle

Bridge state is stored atomically at `~/.kimi-bridge/state.json`. Its versioned schema contains conversation bindings, workspace, permission mode, and thinking-rendering preference. Known older schemas migrate without losing bindings; an unknown future version fails loudly. Kimi remains authoritative for sessions, profiles, model/effort/plan settings, usage, tasks, and goals.

The config file is `~/.kimi-bridge/config.toml`; inbound files live under a configured subdirectory of the bound workspace. Startup creates the default workspace, starts the supervised local server, then starts one adapter. Shutdown stops the adapter, stream tasks, client, and child process. A crashed Kimi child can be restarted by the supervisor; session subscriptions are re-established through the client boundary.

## Security model

- The managed server listens only on `127.0.0.1` and requires its generated bearer token.
- Feishu accepts allowlisted direct-message users by `open_id` or `user_id`; Telegram accepts allowlisted positive numeric user IDs in private chats.
- Adapter secrets live in a local mode-`600` config file and are never persisted in bridge state. Doctor output projects only presence and counts.
- `/send` resolves the requested regular file and rejects paths or symlinks that escape the bound workspace.
- The deployment model is one trusted host account and trusted chat operator. It is not a tenant isolation boundary. Kimi Code and its tools retain the host account's authorized filesystem, process, and network capabilities.
- The per-user service does not add sandboxing that would silently break authorized coding workspaces or tools.

## Compatibility policy

The package contains a sorted manifest of official Kimi Code versions that passed the tracked semantic contract. Startup and `doctor` identify the product from both version and help surfaces:

- a listed official version is supported;
- an unlisted official version receives a loud warning and a live contract attempt;
- legacy Python `kimi-cli`, an unrecognized product, or an executable/server version mismatch fails;
- the daily credential-free canary installs the latest official Kimi Code in an empty home, exercises the CLI/server contract without model inference, and quietly proposes a manifest update when the contract still passes;
- contract failure uses one rolling issue rather than opening a new noisy issue every day.

All raw protocol knowledge and the tracked semantic contract stay in `kimi_server`. Hosted tests use no Kimi account, chat credential, or inference.

## Intentional limits

kimi-bridge does not currently provide mutually untrusted multi-user isolation, simultaneous adapters in one process, a generic capability/UI/plugin framework, remote Kimi server operation, tool-call or transcript rendering, Telegram webhooks/groups/topics/albums, a Telegram framework dependency, WeChat, or automatic semantic version selection. These are product decisions rather than missing abstractions to route around.

See [Configuration](CONFIGURATION.md), [Commands](COMMANDS.md), and the [installation runbook](../INSTALL.md) for operator-facing contracts.
