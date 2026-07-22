# AGENTS.md

Guidance for AI agents (and humans) working in this repo.

## Project state

**The managed server client, Feishu bridge, and experimental Telegram adapter are implemented, including interactive approvals/questions, prompt steering, inbound media, semantic outbound files, and separately streamed optional thinking.** Feishu is live-validated; Telegram is fake-tested but not live-validated because credentials are unavailable. The core contracts are platform-neutral, and the runtime intentionally enables one selected adapter per process. All major decisions are locked in README.md ("Decisions (locked)") — treat them as requirements, not suggestions; changing one requires explicit user sign-off and a README update in the same change. The remaining open questions are listed in README.md; do not silently "fill them in" with assumptions — surface them to the user instead. The phased execution roadmap lives in `roadmap/` (local, gitignored working docs; `roadmap/reference/` holds snapshots of the kimi server OpenAPI/AsyncAPI specs). Each phase doc there is self-contained and names its own validation and exit criteria.

## Layout

- `src/kimi_bridge/kimi_server/` — the ONLY package that talks to kimi-code. Its facade, client, supervisor, types, wire/event helpers, probe, and semantic-contract modules form one boundary; platform adapters, automation, and the router must stay free of kimi-server API details.
- `src/kimi_bridge/interactions.py` — platform-neutral approval/question prompts, answers, responses, and outcomes.
- `src/kimi_bridge/router.py` — IM-conversation ↔ kimi-session mapping, workspace-contained outbound-file authorization, interaction lifecycle, and independent answer/thinking event rendering. It must not construct platform UI payloads or choose native media types.
- `src/kimi_bridge/platforms/` — one adapter per IM platform, behind the semantic `PlatformAdapter` protocol in `base.py`. Native text/file rendering, uploads, and callback decoding stay inside the platform package.
- `src/kimi_bridge/platforms/feishu.py` and `feishu_cards.py` — Feishu Markdown posts, native media uploads/messages, card JSON, and callback decoding. Bundled native-rendering assets live below `src/kimi_bridge/assets/` and must be loaded with package resources so wheel installs work.
- `src/kimi_bridge/platforms/telegram.py` — the handwritten Telegram Bot API transport and adapter. Telegram update dictionaries, multipart uploads, inline keyboards, callback tokens, `ForceReply` wizard state, retry policy, and file downloads stay here.
- `src/kimi_bridge/state.py` — versioned bridge-owned conversation state. New schema changes require an explicit migration that preserves existing bindings; unknown future versions must still fail loudly.
- `references/` — read-only reference repos (hakimi, wechat-acp). Never modify, never import from them.

## kimi server API

- Tested runtime versions are the immutable `src/kimi_bridge/supported-kimi-code-versions.json` manifest loaded by `compatibility.py`, initially kimi-code 0.28.1. Unknown official kimi-code versions warn and attempt the live contract; executable/server version mismatches and the legacy Python kimi-cli fail. Start the server with `kimi web --no-open --host 127.0.0.1 --port <p>`; it stays in the foreground and prints the bearer token at startup.
- Specs are served at runtime: `GET /openapi.json` (REST) and `GET /asyncapi.json` (WebSocket). Consult them instead of guessing field names; note the API is 0.x and may shift between kimi-code releases — check `server_version` in `/api/v1/meta`.
- Stored sessions must be materialized through `GET /api/v1/sessions/{session_id}/status` before each initial or reconnected WebSocket subscription. This lifecycle detail belongs only in the `kimi_server` package.
- Auth: `Authorization: Bearer <token>` header on REST and WS.

## Conventions

- Python ≥ 3.11, asyncio throughout, typed (dataclasses / Protocol, `from __future__ import annotations`).
- Minimal dependencies: core is `httpx` + `websockets` only. Feishu uses the optional `lark-oapi` SDK; Telegram reuses `httpx` and must not gain a framework dependency without an explicit design change.
- Keep the shared contracts semantic and platform-neutral. Do not introduce a generic UI schema, plugin framework, capability registry, or multi-adapter runtime without a concrete second platform requiring it.
- If a file is gitignored, it's gitignored for a reason — never force-add it.

## Testing

Unit-test the router against a fake `KimiServerClient` and fake adapters. Router tests assert semantic interactions, path authorization, state migration, and independent stream behavior; platform tests assert native rendering, uploads, and callback decoding. Keep server supervision, REST/WebSocket recovery, Feishu filtering, Telegram Bot API transport, configuration, and state persistence behind fakes in CI; do not require a live kimi server or real IM credentials. The standalone smoke script is the explicit live-server check. Distribution changes also require building the wheel, checking bundled assets, and exercising isolated core and Feishu-extra `uv tool` installs through non-starting `--help` and `--version` before uninstalling them.
