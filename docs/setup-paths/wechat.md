# WeChat iLink private chat

| Field | Value |
| --- | --- |
| Applies to | One QR-authorized WeChat Clawbot used by one kimi-bridge private-chat process |
| Last live verification | `2026-08-08` |
| `reverify_after` | `2026-11-06` |
| Evidence | [Issue #66](https://github.com/Mtrya/kimi-bridge/issues/66), [Tencent source tag v2.4.6](https://github.com/Tencent/openclaw-weixin/tree/v2.4.6) |

Read the [setup agent contract](../../INSTALL_AI.md) before using this path. Follow the [freshness rules](README.md#freshness-and-use); the dates above record one real allowlisted scanner and complete text/media round trips, not a promise that WeChat's authorization service is unchanged.

## Preconditions

- Official Kimi Code is authenticated and has completed a real prompt.
- The scanning human account may be an ordinary WeChat account, but its resulting Clawbot authorization is available for exclusive polling by kimi-bridge.
- OpenClaw and every other iLink consumer for that bot are stopped before kimi-bridge starts.
- The operator can open a short-lived authorization URL in WeChat and privately edit a mode-`600` config file.
- Distinct kimi-bridge instances use distinct bot authorizations, config files, `state_path`, workspaces, and `wechat.storage_path` values.

## Verified path

1. Create a protected config with `platform = "wechat"`, a private `wechat.storage_path`, and `wechat.allowed_users = []`. The empty list is valid only for QR bootstrap.
2. Run `kimi-bridge wechat login`. Open the printed URL in WeChat, approve the authorization, and enter a verification number only if WeChat explicitly requests one.
3. Copy the returned stable scanner identity into `wechat.allowed_users` through the private local editor. Do not use a display name, bot identity, or guessed account identifier.
4. Run `kimi-bridge wechat status`. Confirm local authorization is present and the storage is usable; remember that this command performs no network check.
5. Run `kimi-bridge doctor` and resolve every error. It must report the required encrypted-media dependency, private storage, local authorization, allowlist, state, workspace, and Kimi checks without starting runtime.
6. Confirm that no other process polls this bot, then start `kimi-bridge` in the foreground.
7. From the allowlisted scanner, send `/status` and one normal prompt. Confirm forced `auto`, no separate thinking rendering, and one complete immutable reply. Model thinking effort remains independent of rendering.
8. Validate the media types the installation needs. The project-live-validated surface is inbound image, voice, generic file, and video plus outbound image, video, and generic file. Outbound audio is a generic downloadable file, not a native voice message.
9. Stop with Ctrl-C. Confirm the typing indicator clears and the process exits cleanly.

Native images and videos become model media only when the selected Kimi model advertises `image_in` or `video_in`; otherwise the bridge saves them in the workspace inbox and supplies the path. Native voice transcription is best effort and may contain recognition errors. A `typing...` indication can refresh intermittently while a turn or tool call is active, but it must clear after the final answer or shutdown.

The live verification used one allowlisted scanner. The bridge keeps cursor context and Kimi bindings per sender, but only one Kimi response stream may be active: an overlapping model prompt from another sender is rejected with retry guidance instead of cancelling the active response. Do not report two-sender interleaving as live-validated. Delivery is at-least-once across the narrow crash window between Kimi acceptance and local completion recording.

## Replacement, expiry, and cleanup

- `kimi-bridge wechat login --replace` preserves the existing local credential until Tencent confirms a replacement. If WeChat redirects to the already-bound bot, the command retains the existing credential rather than overwriting it.
- If runtime reports expired authorization, stop it and use `login --replace`; do not keep restarting a stale credential or manufacture an expiry condition.
- `kimi-bridge wechat logout` removes only the local adapter-owned credential and receive-state files. It does not remotely delete the WeChat bot binding.

## Checkpoints and divergence

- If opening the authorization URL jumps directly to an existing Clawbot, check whether the CLI reports that the prior local authorization was retained. Do not call that a successful rotation.
- If the bot says it cannot connect while the foreground bridge should be active, verify that the selected process is still running and inspect its first exception before sending more test messages.
- If an attachment crashes or terminates the poller, preserve the sanitized exception and media type, stop promotion, and add a deterministic protocol regression before repair.
- If old replies replay after an ordinary clean restart, preserve the cursor/state evidence without exposing tokens and diagnose the durable receive boundary.
- If `typing...` remains after a final reply or shutdown, treat it as a lifecycle defect; intermittent refresh while a turn remains active is expected.
- Do not add callback URLs, webhooks, group settings, or proactive-delivery configuration. This adapter uses private HTTP polling and inbound context tokens.

After a complete live round trip, update `Last live verification` and `reverify_after` in the same change only when the observed path still matches this document. With the user's permission, report useful divergences under the [setup-evidence policy](README.md#contributing-setup-evidence).
