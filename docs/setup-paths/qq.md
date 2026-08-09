# QQ C2C over WebSocket

| Field | Value |
| --- | --- |
| Applies to | A QQ official bot dedicated to one kimi-bridge C2C WebSocket process |
| Last complete verification | `2026-07-29` |
| `reverify_after` | `2026-10-27` |

Read [INSTALL_AI.md](../../INSTALL_AI.md) and the [setup-path rules](README.md) before using this path. Detect existing bot ownership, sandbox/production status, tester access, consumers, config, and storage before asking the user to repeat work.

## Preconditions

- Official Kimi Code is authenticated and has passed `kimi --version`, `kimi --help`, `kimi doctor config`, and `kimi -p "Reply with OK only."`.
- `kimi-bridge` is installed and `kimi-bridge compat` has been classified.
- The QQ bot is dedicated to this instance and no other gateway consumer is active.
- The intended account can use the bot, including sandbox tester access when applicable.
- The operator can perform identity-bound QR/console actions and edit private local configuration.

## Recommended path: QR bot credential bootstrap

1. Create a protected config with `platform = "qq"`, an isolated state/workspace, and the default or explicit storage path. Keep the allowlist empty only while onboarding:

```toml
platform = "qq"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[qq]
storage_path = "~/.kimi-bridge/qq"
allowed_users = []
```

2. Run:

```bash
kimi-bridge qq login
```

The command does not start Kimi Code or message polling. It creates a short-lived official bind task, prints the QQ authorization URL, and waits for confirmation.

3. Have the user scan and approve the bot bind. The completed result provides `bot_appid` and encrypted `bot_encrypt_secret`; kimi-bridge decrypts the AppSecret locally and stores only the final managed credential at `~/.kimi-bridge/qq/credentials.json` or the configured `storage_path`. The temporary AES key, task/QR URL, and encrypted blob are not persisted.

4. If the command returns scanner `user_openid`, it automatically merges that exact app-scoped value into `qq.allowed_users` while preserving existing entries. It is not a QQ number, nickname, or display name. Review or remove the generated entry in the TOML if you want finer access control. If no identity is returned, configure the allowlist manually.

5. Confirm current QQ platform prerequisites, including bot availability, sandbox tester access or production status as applicable, and the absence of another gateway consumer. QR completion is not proof that all event/gateway permissions or the message path are ready.

6. Run:

```bash
kimi-bridge qq status
kimi-bridge doctor
```

`status` checks only local storage. Resolve every local error before startup.

7. Start `kimi-bridge` in the foreground. From the real allowlisted `user_openid`, send `/status`, then a normal prompt. Confirm the C2C reply completes. Test only the media types needed by the installation, then stop cleanly or proceed to persistence.

The runtime obtains an access token, discovers the QQ WebSocket gateway, identifies for C2C events, and continues to use its existing REST/token/WebSocket transport. The QR flow does not create a second transport.

## Manual TOML fallback

If the user already owns a suitable bot credential or QR bootstrap is unavailable, use a complete pair:

```toml
platform = "qq"

[qq]
app_id = "replace-privately"
app_secret = "replace-privately"
storage_path = "~/.kimi-bridge/qq"
allowed_users = ["app-scoped-user-openid"]
```

Both fields are required together. If managed `credentials.json` is absent, runtime uses this pair. If managed storage exists but is unreadable, unsafe, or malformed, runtime fails instead of silently using the TOML pair; repair it or run `kimi-bridge qq login --replace`.

If the intended `user_openid` is not known, use a bounded foreground gateway observation or the adapter's local rejected-sender warning, then replace any temporary non-matching value immediately. Never use a QQ number or display name.

## Replacement, status, and logout

- `kimi-bridge qq login --replace` keeps the old credential until a new bind succeeds.
- `kimi-bridge qq status` inspects local storage and redacted metadata only; it does not call QQ.
- `kimi-bridge qq logout` removes only adapter-owned managed files. It does not remotely reset the bot credential, and a TOML AppID/AppSecret pair remains available.

## Checkpoints and divergence

- Access-token failure: verify the active managed/TOML AppID-AppSecret pair and current token endpoint contract.
- Managed storage error: do not assume TOML fallback; inspect permissions/format and use `login --replace` when appropriate.
- Gateway discovery or identify failure: preserve the secret-safe QQ error and research current gateway/intent requirements; do not invent a webhook or console checkbox.
- Ready gateway but no C2C event: confirm bot availability, tester access, chat type, and exclusive gateway ownership.
- Rejected sender: use only the app-scoped `user_openid` from the event.
- Callback/webhook controls do not repair this adapter's WebSocket receive path unless QQ returns a specific documented requirement.

QQ forces `auto` and does not present approvals, questions, or separate thinking output. A successful local status or gateway connection is not completion; require the real foreground message round trip.
