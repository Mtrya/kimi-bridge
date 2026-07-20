# AGENTS.md

Guidance for AI agents (and humans) working in this repo.

## Project state

**The managed server client and Feishu text-message bridge are implemented.** Interactive approvals, media handling, and Telegram remain future work. All major decisions are locked in README.md ("Decisions (locked)") — treat them as requirements, not suggestions; changing one requires explicit user sign-off and a README update in the same change. The few remaining open questions are listed in README.md; do not silently "fill them in" with assumptions — surface them to the user instead. The phased execution roadmap lives in `roadmap/` (local, gitignored working docs; `roadmap/reference/` holds snapshots of the kimi server OpenAPI/AsyncAPI specs). Each phase doc there is self-contained and names its own validation and exit criteria.

## Layout

- `src/kimi_bridge/kimi_server.py` — the ONLY module that talks to kimi-code. Platform adapters and the router must stay free of kimi-server API details.
- `src/kimi_bridge/router.py` — IM-conversation ↔ kimi-session mapping and event dispatch.
- `src/kimi_bridge/platforms/` — one adapter per IM platform, behind the `PlatformAdapter` protocol in `base.py`.
- `references/` — read-only reference repos (hakimi, wechat-acp). Never modify, never import from them.

## kimi server API

- Start with `kimi server run --foreground --port <p> --keep-alive`; the bearer token is printed at startup.
- Specs are served at runtime: `GET /openapi.json` (REST) and `GET /asyncapi.json` (WebSocket). Consult them instead of guessing field names; note the API is 0.x and may shift between kimi-code releases — check `server_version` in `/api/v1/meta`.
- Auth: `Authorization: Bearer <token>` header on REST and WS.

## Conventions

- Python ≥ 3.11, asyncio throughout, typed (dataclasses / Protocol, `from __future__ import annotations`).
- Minimal dependencies: core is `httpx` + `websockets` only. Platform SDKs are extras and each addition is a deliberate decision, not a drive-by.
- Keep it stupidly simple: no abstraction until a second platform forces it.
- If a file is gitignored, it's gitignored for a reason — never force-add it.

## Testing

Unit-test the router against a fake `KimiServerClient` and fake adapters. Keep server supervision, REST/WebSocket recovery, Feishu filtering, configuration, and state persistence behind fakes in CI; do not require a live kimi server or real IM credentials. The standalone smoke script is the explicit live-server check.
