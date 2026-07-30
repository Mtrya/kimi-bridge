# QQ C2C over WebSocket

| Field | Value |
| --- | --- |
| Applies to | A dedicated QQ bot using kimi-bridge's C2C WebSocket gateway adapter |
| Last live verification | `2026-07-29` |
| `reverify_after` | `2026-10-27` |
| Evidence | [Issue #43](https://github.com/Mtrya/kimi-bridge/issues/43) |

Read the [setup agent contract](../../INSTALL_AI.md) before using this path. Follow the [freshness rules](README.md#freshness-and-use); the dates above record evidence, not a promise that QQ's console is unchanged.

## Preconditions

- The bot is dedicated to this kimi-bridge instance and no other gateway consumer is active.
- The intended user can access the bot directly, as a sandbox tester when applicable.
- Kimi Code and kimi-bridge have already passed their host-side preflight.
- The user can perform identity-bound QQ console actions and enter credentials privately.

## Verified path

1. Inspect the existing bot or guide the user through creating a QQ bot. Creating the bot, accepting platform terms, selecting its owner, and submitting production review are user-only actions.
2. Have the user obtain the bot's AppID and AppSecret. Do not request the deprecated Token credential and do not ask the user to paste either secret into chat.
3. Have the user enter the credentials directly into the protected configuration file. Set a temporary non-matching `allowed_users` value because the selected adapter requires a non-empty allowlist:

```toml
platform = "qq"

[qq]
app_id = "replace-privately"
app_secret = "replace-privately"
allowed_users = ["temporary-non-matching-openid"]
```

4. Run `kimi-bridge doctor`, resolve every error, and start `kimi-bridge` in the foreground.
5. Ask the intended user to send one C2C message to the bot.
6. Read the rejected sender's `user_openid` from the local warning, stop the bridge, replace the temporary allowlist value, and do not expose the identifier in chat or an issue.
7. Restart the bridge and have the allowlisted user send `/status`. Confirm that the bridge replies.
8. Send one normal prompt and confirm that the streamed answer completes cleanly. Test only the media types the installation needs, then stop cleanly or continue to persistence setup.

The verified path does not configure a callback URL, event webhook, manual C2C event checkbox, or IP whitelist. The adapter obtains an access token with AppID/AppSecret, discovers QQ's gateway, identifies with the `GROUP_AND_C2C_EVENT` intent, and receives `C2C_MESSAGE_CREATE` dispatches over WebSocket.

## Checkpoints and divergence

- If access-token exchange fails, verify the AppID/AppSecret pair and the current token endpoint contract.
- If gateway discovery or identify fails, preserve the exact QQ error and research the current gateway and intent requirements. Do not translate the adapter's intent bit into an invented console action.
- If QQ explicitly rejects an OpenAPI request because of an IP restriction, research the current whitelist control and guide the user through only the required change. Do not add an IP proactively.
- If the gateway becomes ready but no C2C event arrives, confirm bot availability, sandbox tester access when applicable, the intended chat type, and the absence of another gateway consumer.
- If an unrecognized sender warning appears, use only the app-specific `user_openid` from that event; a QQ number or display name cannot authorize the user.
- Callback and webhook settings do not repair this adapter's inbound WebSocket path.

After a complete live round trip, update `Last live verification` and `reverify_after` in the same change only when the observed path still matches this document. With the user's permission, report useful divergences under the [setup-evidence policy](README.md#contributing-setup-evidence).
