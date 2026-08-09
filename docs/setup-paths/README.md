# Verified setup paths

These files are agent/automation-oriented operational paths that supplement [INSTALL_AI.md](../../INSTALL_AI.md) and preserve detect-before-ask, ownership, checkpoint, freshness, and safe-abort rules.

## Available paths

- [QQ C2C over WebSocket](qq.md)
- [WeChat iLink private chat](wechat.md)

Feishu's recommended QR application-registration path is walked through in the [installation guide](../../INSTALL.en_US.md#feishu) and the [Feishu setup-agent branch](../../INSTALL_AI.md#6-feishu-bootstrap); its complete TOML fallback is defined in [Configuration](../CONFIGURATION.md). There is no separate dated Feishu path in this directory.

## Current control-plane facts

The terminal authorization commands (`login`, `status`, `logout`, `--replace`) and their semantics are authoritative in [Commands](../COMMANDS.md); managed paths and TOML fallback precedence are authoritative in [Configuration](../CONFIGURATION.md). A local `status` pass never proves that platform authorization or message delivery is active.

## Freshness and use

Each path states its platform mode, preconditions, last complete verification date, and `reverify_after` date. Use a path directly only when its preconditions match and current behavior agrees with it. If the date is stale or a checkpoint diverges, research current official sources before directing a platform-side action.

Follow one checkpoint at a time. Stop applying later steps when a checkpoint diverges. Detect host state, existing bot ownership, platform consumers, config, and storage before asking. Do not invent console controls or recovery actions from another integration.

A path is considered reverified only after official Kimi Code completes a real prompt and a real allowlisted platform message receives a complete bridge reply. `doctor`, QR success, local `status`, token exchange, or a gateway connection alone cannot establish an end-to-end setup.

## Secret and abort rules

Never include credentials, complete allowlist identities, signed URLs, QR task data, private tokens, or unredacted logs in reports. On abort, remove only files created during the current setup and preserve pre-existing config, state, storage, workspaces, sessions, platform bots, and service definitions unless the user separately approves exact targets.
