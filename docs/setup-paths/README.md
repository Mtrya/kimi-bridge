# Verified setup paths

These files are agent/automation-oriented operational paths that supplement [INSTALL_AI.md](../../INSTALL_AI.md). They preserve detect-before-ask, ownership, checkpoint, freshness, and safe-abort rules that should not be copied into ordinary human documentation.

## Available paths

- [QQ C2C over WebSocket](qq.md)
- [WeChat iLink private chat](wechat.md)

Feishu's recommended QR application-registration path and its complete TOML fallback are documented in [QR onboarding](../QR_ONBOARDING.md) and [the Feishu setup-agent branch](../../INSTALL_AI.md#6-feishu-bootstrap). There is no separate dated Feishu path in this directory.

## Current control-plane facts

All three supported QR controls load config first and require its `platform` to match the command platform:

```bash
kimi-bridge feishu login|status|logout
kimi-bridge qq login|status|logout
kimi-bridge wechat login|status|logout
```

Every `login` supports `--replace`. These controls do not start Kimi Code or message polling. `status` is local-only and never proves that platform authorization or message delivery is active. `logout` removes only adapter-owned local files; it does not remotely delete a bot or binding. Feishu/QQ TOML fallback remains after logout.

The QR semantics are deliberately distinct:

- Feishu performs official application registration and stores application credentials plus the Feishu/Lark API domain. It is not user OAuth.
- QQ performs official bot credential bootstrap and stores the final bot AppID/AppSecret after local decryption. It is not QQ user login or OAuth.
- WeChat performs supported iLink bot authorization and stores the credential outside TOML.

Default managed files are `~/.kimi-bridge/feishu/credentials.json`, `~/.kimi-bridge/qq/credentials.json`, and `~/.kimi-bridge/wechat/credentials.json`. Each platform table may set `storage_path`. For Feishu/QQ, valid managed credentials take precedence over a complete TOML pair; a present but invalid managed file is an error and does not silently fall back. WeChat has no TOML credential fallback.

## Freshness and use

Each path states its platform mode, preconditions, last complete verification date, and `reverify_after` date. Use a path directly only when its preconditions match and current behavior agrees with it. If the date is stale or a checkpoint diverges, research current official sources before directing a platform-side action.

Follow one checkpoint at a time. Stop applying later steps when a checkpoint diverges. Detect host state, existing bot ownership, platform consumers, config, and storage before asking. Do not invent console controls or recovery actions from another integration.

A path is considered reverified only after official Kimi Code completes a real prompt and a real allowlisted platform message receives a complete bridge reply. `doctor`, QR success, local `status`, token exchange, or a gateway connection alone cannot establish an end-to-end setup.

## Secret and abort rules

Never include credentials, complete allowlist identities, signed URLs, QR task data, private tokens, or unredacted logs in reports. On abort, remove only files created during the current setup and preserve pre-existing config, state, storage, workspaces, sessions, platform bots, and service definitions unless the user separately approves exact targets.
