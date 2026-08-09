# WeChat iLink private chat

| Field | Value |
| --- | --- |
| Applies to | One WeChat iLink bot authorization polled by one kimi-bridge private-chat process |
| Last complete verification | `2026-08-08` |
| `reverify_after` | `2026-11-06` |

Read [INSTALL_AI.md](../../INSTALL_AI.md) and the [setup-path rules](README.md) before using this path. One bot authorization can have only one polling process; the allowlist is an array and may authorize more than one stable private-chat identity.

## Preconditions

- Official Kimi Code is authenticated and has passed `kimi --version`, `kimi --help`, `kimi doctor config`, and `kimi -p "Reply with OK only."`.
- `kimi-bridge` is installed and compatibility has been classified.
- The scanning human account can approve a WeChat iLink bot authorization.
- No other iLink consumer polls the resulting bot authorization while kimi-bridge runs.
- Distinct bridge instances use distinct bot authorizations, configs, `state_path`, workspaces, and `wechat.storage_path` directories.
- The operator can edit local config privately and apply current-user ACL protection on Windows or POSIX `700`/`600` permissions on Linux/macOS.

## Supported QR path

1. Create protected configuration with `platform = "wechat"`, an isolated storage path, and an empty allowlist only during bootstrap:

```toml
platform = "wechat"
default_workspace = "~/.kimi-bridge/workspace"
state_path = "~/.kimi-bridge/state.json"

[wechat]
storage_path = "~/.kimi-bridge/wechat"
allowed_users = []
```

WeChat credentials never belong in TOML.

2. Run:

```bash
kimi-bridge wechat login
```

It prints a short-lived authorization URL.

3. Have the user open the URL in WeChat, scan and approve the iLink bot authorization, and enter a verification code only when the flow explicitly asks for one.

4. On success, the command stores the managed credential at `~/.kimi-bridge/wechat/credentials.json` or the configured `storage_path` and prints a stable scanner identity. Copy that identity privately into `wechat.allowed_users`. Do not use a nickname, guessed account identifier, QQ-style identifier, or bot identity; additional entries must be real stable identities.

5. Run:

```bash
kimi-bridge wechat status
kimi-bridge doctor
```

`status` inspects local credential metadata and storage only, with no network check. `doctor` checks local config, storage, allowlist, paths, Kimi Code, and the encrypted-media dependency without starting runtime.

6. Confirm that no other process polls the bot authorization, then start `kimi-bridge` in the foreground.

7. From an allowlisted private chat, send `/status` and a normal prompt. Confirm forced `auto`, no separate thinking rendering, and a complete immutable reply.

8. Test the media types required by the installation. Supported input is image, voice, generic file, and video; supported native output is image, video, and file. Outbound audio is a generic downloadable file, not a native voice message.

9. Stop cleanly before creating a persistent service.

Native images and videos become Kimi media only when the selected model advertises `image_in` or `video_in`; otherwise the bridge saves them under the workspace inbox and supplies the path. Native voice transcription is best effort. The adapter has no group chat or proactive delivery path.

## Replacement, expiry, and cleanup

- `kimi-bridge wechat login --replace` preserves the old local credential until a new authorization is confirmed. If WeChat reports the already-bound bot, the previous credential may be retained.
- If runtime reports expired authorization, stop it and use `login --replace`; do not repeatedly restart the stale credential.
- `kimi-bridge wechat status` cannot prove that the remote authorization is active.
- `kimi-bridge wechat logout` removes only adapter-owned `credentials.json` and receive-state files. It does not remotely delete the bot binding.
- There is no TOML credential fallback for WeChat.

## Checkpoints and divergence

- Authorization URL opens an existing binding: inspect the CLI result and confirm whether the existing local credential was retained; do not describe that as a rotation unless a new credential was confirmed.
- Local storage error: inspect path safety, current-user ACL on Windows, or exact `700`/`600` POSIX permissions before replacing anything.
- Foreground bridge cannot receive messages: confirm the process is still running, inspect its first secret-safe exception, and verify no competing poller exists.
- Non-allowlisted sender: obtain the stable platform identity from observed local metadata before adding it; never use a display name.
- Media failure: preserve the sanitized media type and exception, stop repeated delivery, and diagnose the protocol/storage boundary.
- Old messages replay after a clean restart: preserve redacted cursor/state evidence and diagnose the durable receive boundary.
- Do not add callback URLs, webhooks, group settings, or proactive-delivery configuration; this adapter uses private polling and inbound context tokens.

A local `status`, successful QR flow, or `doctor` result is not completion. Require the real allowlisted foreground message and complete reply before persistence setup.
