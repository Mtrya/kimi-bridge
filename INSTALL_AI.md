# kimi-bridge setup-agent guide

This guide is an execution contract for an agent installing and configuring kimi-bridge with a user. It is not a script to recite and not a replacement for the human [installation guide](INSTALL.en_US.md), [configuration reference](docs/CONFIGURATION.md), or [QR onboarding](docs/QR_ONBOARDING.md).

## Operating contract

Read this guide before changing the host or a platform application. Classify each action by ownership:

- **Automatic:** inspect, research, install, write non-secret configuration, diagnose, validate, and repair without asking the user to do the work.
- **Question then automatic:** ask only for a consequential preference or fact that cannot be discovered safely, then perform the resulting work.
- **Approval then automatic:** explain a concrete mutation or risk, obtain approval, then perform the mutation.
- **User-only:** guide the user through an action that requires their identity, private secret entry, platform-console authority, consent, or publication authority. Resume with agent-run verification.
- **External wait:** record the pending action, how completion is recognized, and the verification that follows. Do not invent a workaround for platform review or publication.
- **Unsupported:** state the exact incompatible boundary and stop that branch.

Detect before asking. Do not ask for information available from commands, local files that can be inspected safely, or an already authorized platform API. Never ask a user to paste credentials into chat. Prefer a private local editor, keychain, secret manager, or masked local input. Preserve existing configuration, state, workspaces, bot settings, webhooks, event consumers, and service files unless the user explicitly approves changing the named resource.

Do not report success after `doctor`. Setup is complete only after an allowlisted inbound message and a complete reply pass through the selected platform in the foreground. Keep implementation internals, test history, and maintenance procedures out of the user conversation unless they explain a visible limitation.

If user action is unavoidable, give a short actionable procedure, not just a link. Research current official platform documentation and console labels before instructing the user. The paths in `docs/setup-paths/` are operational evidence; use them only when their preconditions and freshness metadata match current behavior.

## Completion outcomes

End with one of these outcomes:

- **Done:** Kimi Code completed a real prompt, the selected platform delivered an allowlisted message, and kimi-bridge returned a complete reply.
- **Paused:** a named user-only action, platform publication/review, approval, or external wait is outstanding. State exactly how to resume and what you will verify.
- **Unsupported:** the selected environment or platform cannot satisfy a documented requirement.
- **Aborted:** setup stopped at the user's request. Remove only artifacts created during this setup. Never delete pre-existing config, state, workspaces, sessions, bot applications, bindings, or service files without separate approval for the exact targets.

## 1. Host and Kimi Code preflight

### 1.1 Inspect without mutating

Determine automatically:

- operating system and shell;
- whether `uv`, `kimi`, and `kimi-bridge` are installed;
- how each command resolves on PATH;
- whether config, state, workspace, platform storage, or a service already exists;
- whether the target paths contain pre-existing data.

Do not install, upgrade, replace, or delete anything during inspection.

### 1.2 Require uv

Run:

```bash
uv --version
```

If `uv` is missing, research the current official installation method for the host and guide only the identity- or privilege-bound step. Perform and verify the remaining setup. Installing without `uv` is possible but is not the project's tested path; if the user chooses another method, state that boundary and verify the resulting executable and dependencies.

### 1.3 Identify official Kimi Code

Run:

```bash
command -v kimi
kimi --version
kimi --help
```

Official Kimi Code's help includes `web`, `doctor`, and `migrate`. The incompatible legacy Python `kimi-cli` has a different command surface. If the legacy product shadows the expected executable, preserve its sessions and help the user expose or install official Kimi Code rather than overwriting it.

### 1.4 Prove Kimi Code readiness

Run:

```bash
kimi doctor config
kimi -p "Reply with OK only."
```

Do not proceed to platform setup until the real prompt completes. Authentication, provider selection, or default-model configuration may be user-only; guide that step precisely and resume with both checks.

## 2. Install and classify the bridge

If absent, propose the installation and obtain approval for the package mutation:

```bash
uv tool install kimi-bridge
```

After installation or an approved upgrade, verify:

```bash
command -v kimi-bridge
kimi-bridge --version
kimi-bridge --help
kimi-bridge compat
```

Treat `compat` as a Kimi Code version classification, not as platform permission validation. A current supported verdict may continue. An unlisted version is a risk boundary that should be explained before a live attempt.

## 3. Select one platform

Ask which platform only after host and Kimi Code readiness is established. One process runs exactly one adapter.

- **Feishu:** application-registration QR or complete TOML app credentials. It needs bot capability, platform permissions, long-connection events, app publication, a bridge-side user allowlist, and FFmpeg for inbound voice.
- **QQ:** official bot credential-bind QR or complete TOML AppID/AppSecret. It is C2C private chat, uses the existing REST/token/WebSocket transport, forces `auto`, and has no approvals, questions, or separate thinking stream.
- **WeChat:** supported iLink bot QR authorization, with the credential outside TOML. It handles private chats, forces `auto`, and has no approvals, questions, separate thinking stream, groups, or proactive delivery. It accepts inbound image/voice/file/video and sends outbound image/video/file. One bot authorization has one poller.
- **Telegram:** experimental private-chat adapter with its own bot-token flow; startup takes over long polling and discards pending updates.

Prefer a dedicated platform bot when another service, webhook, gateway connection, or poller would be displaced. For WeChat, the scanning human account need not be dedicated, but the resulting bot authorization must be polled exclusively by one process.

## 4. Prepare private local configuration

The default file is `Path.home() / .kimi-bridge / config.toml`; `--config` and `KIMI_BRIDGE_CONFIG` select another file. Before writing:

- inspect the existing file without displaying secrets;
- preserve unrelated tables and settings;
- preserve existing state, workspace, and platform storage;
- use the selected platform's `storage_path`, `state_path`, and workspace consistently;
- do not invent a real allowlist identity.

On Linux/macOS:

```bash
install -d -m 700 ~/.kimi-bridge
chmod 600 ~/.kimi-bridge/config.toml
chmod 700 ~/.kimi-bridge/feishu ~/.kimi-bridge/qq ~/.kimi-bridge/wechat 2>/dev/null || true
```

Create only the storage directory needed by the selected flow; the adapter creates it during a successful save. On Windows, do not issue `chmod`. In PowerShell, create the directory and file and grant the current user explicit ACL:

```powershell
$root = Join-Path $HOME ".kimi-bridge"
$config = Join-Path $root "config.toml"
New-Item -ItemType Directory -Force $root | Out-Null
New-Item -ItemType File -Force $config | Out-Null
$principal = "${env:USERDOMAIN}\${env:USERNAME}"
icacls $root /inheritance:r /grant:r "${principal}:(OI)(CI)F"
icacls $config /inheritance:r /grant:r "${principal}:F"
```

The default managed files are:

```text
~/.kimi-bridge/feishu/credentials.json
~/.kimi-bridge/qq/credentials.json
~/.kimi-bridge/wechat/credentials.json
```

Each platform table accepts `storage_path`. Feishu and QQ managed credentials take precedence over TOML fallback. A complete pair is required for fallback; if a managed file exists but is malformed or unsafe, startup fails and does not silently use TOML. WeChat QR credentials are never placed in TOML.

Keep secrets out of command arguments, environment variables, chat, logs, reports, and version control. Have the user enter only identity-bound secrets through a private local mechanism.

## 5. Shared QR control protocol

For the selected platform, run only the matching command:

```bash
kimi-bridge feishu login|status|logout
kimi-bridge qq login|status|logout
kimi-bridge wechat login|status|logout
```

`login --replace` is available for all three. The control commands load config and require a matching `platform`. They do not start Kimi Code or message polling. `status` is local-only and does not validate the network. `logout` removes only adapter-owned managed files; it does not remotely delete a bot binding. Feishu/QQ TOML fallback values remain after logout.

Do not call a QR flow complete until its platform-specific manual follow-up is done and the real foreground round trip passes.

## 6. Feishu bootstrap

### 6.1 Recommended QR branch: application registration

Use this branch first when the user can complete the Feishu/Lark identity-bound approval in a browser or mobile device. During bootstrap, keep the allowlist empty:

```toml
platform = "feishu"

[feishu]
storage_path = "~/.kimi-bridge/feishu"
allowed_users = []
```

Run:

```bash
kimi-bridge feishu login
```

Have the user open the URL printed by the command in a browser, then follow the page instructions to scan with or approve in Feishu/Lark. This is not user OAuth. The result returns `client_id` and `client_secret`; managed storage also records Feishu or Lark tenant brand and API domain. The QR flow does not automatically enable the bot, grant scopes, subscribe to events, publish an app, or add an allowlist user.

After the platform-side identity is known, put the real `open_id` in `feishu.allowed_users` before foreground startup. The following is a location marker, not a value to copy literally:

```toml
[feishu]
allowed_users = ["<real open_id from this app and tenant>"]
```

### 6.2 Manual fallback branch

If QR registration is unavailable or the user prefers the platform console, have the user create or inspect an official custom application and enter this pair through a private local editor:

```toml
platform = "feishu"

[feishu]
app_id = "cli_replace_privately"
app_secret = "replace_privately"
storage_path = "~/.kimi-bridge/feishu"
allowed_users = ["ou_replace_privately"]
```

The pair must be complete. A complete TOML pair is used only when the managed credential file is absent. If a managed file exists but is invalid, do not use this fallback silently; repair it or run `kimi-bridge feishu login --replace`.

### 6.3 Platform-side follow-up

Research the current official console and guide the user through the identity-bound steps. Confirm:

- bot capability is enabled;
- permissions cover the message prompts and sends, resource upload/download, and voice recognition required by the selected workflow; current code paths include message permissions, resource access, and `speech_to_text:speech`, but do not claim a QR scan granted a particular scope;
- `im.message.receive_v1` and `card.action.trigger` are subscribed with long-connection delivery;
- the application version is published and available to the intended user/tenant;
- the intended sender's `open_id` is discovered in the same app/tenant context and entered manually in `feishu.allowed_users`.

If the user scans but has not completed these console steps, pause rather than report readiness. Require `ffmpeg` on PATH for Feishu voice messages.

### 6.4 Feishu validation

Run:

```bash
kimi-bridge feishu status
kimi-bridge doctor
kimi-bridge
```

Then have the real allowlisted user send `/status`, a normal prompt, and any required voice/file test. Exercise an approval or question if the workflow needs one. Stop cleanly before persistence setup.

## 7. QQ bootstrap

### 7.1 Recommended QR branch: bot credential bootstrap

Configure an empty allowlist for the bootstrap only:

```toml
platform = "qq"

[qq]
storage_path = "~/.kimi-bridge/qq"
allowed_users = []
```

Run:

```bash
kimi-bridge qq login
```

Have the user open the printed URL, scan it, and approve the official bot bind. The completed result returns `bot_appid` and encrypted `bot_encrypt_secret`; the bridge decrypts the AppSecret locally and persists only the final managed credential. The temporary AES key, task ID/QR URL, and encrypted secret blob are not persisted. Runtime still uses the existing REST/token/WebSocket transport.

If the flow prints `user_openid`, write that exact app-scoped identity manually to `qq.allowed_users`. It is not a QQ number or nickname. QR success does not automatically establish sandbox tester access, production review, authorization for the required event Intents, or end-to-end delivery. After the flow succeeds, replace the empty array with the real value; the following is a location marker, not a value to copy literally:

```toml
[qq]
allowed_users = ["<real user_openid returned by this flow>"]
```

### 7.2 Manual fallback branch

If the user already has QQ bot credentials or QR bootstrap is unavailable, use:

```toml
platform = "qq"

[qq]
app_id = "replace_privately"
app_secret = "replace_privately"
storage_path = "~/.kimi-bridge/qq"
allowed_users = ["user_openid_replace_privately"]
```

AppID and AppSecret must be entered as a complete pair. Discover the intended app-scoped `user_openid` through a bounded gateway event or the adapter's local rejection warning, then replace any temporary non-matching value immediately. Never substitute a QQ number or display name.

### 7.3 QQ validation

Run `kimi-bridge qq status`, `kimi-bridge doctor`, then start `kimi-bridge` in the foreground. Validate a real allowlisted C2C `/status`, a complete normal reply, and only the media types the installation needs. QQ is forced to `auto` and cannot present approvals or questions. Ensure no second gateway consumer uses the bot.

## 8. WeChat bootstrap

WeChat is a supported QR-authorized private-chat adapter. Its credential is managed outside TOML.

### 8.1 Ownership and preflight

Determine whether another process polls the intended bot authorization. One bot authorization can have one poller only. A scanning human account need not be dedicated, but stopping or displacing another iLink consumer requires explicit approval. Use distinct config, `state_path`, workspace, and `wechat.storage_path` for distinct bridge instances.

### 8.2 QR authorization and allowlisting

Create:

```toml
platform = "wechat"
state_path = "~/.kimi-bridge/state.json"

[wechat]
storage_path = "~/.kimi-bridge/wechat"
allowed_users = []
```

Run:

```bash
kimi-bridge wechat login
```

Have the user open the printed URL in WeChat, scan and approve the iLink authorization, and enter a verification number only if explicitly requested. On success, copy the returned stable scanner identity privately into `wechat.allowed_users`; do not expose it, use a nickname, or put the credential in TOML, environment variables, command arguments, chat, or reports.

Run `kimi-bridge wechat status` to inspect local storage and redacted metadata. It performs no network check. Run `kimi-bridge doctor`, resolve local errors, and only then start the bridge.

### 8.3 WeChat validation and limits

From the real allowlisted private chat, send `/status` and a normal prompt and confirm a complete immutable reply. Test inbound image, voice, file, and video and outbound image, video, and file when needed. WeChat forces `auto`, has no approvals, questions, separate thinking stream, groups, proactive delivery, or native outbound voice messages. Exactly one process may poll the authorization.

### 8.4 Replacement and logout

When the authorization must be replaced or runtime reports expiry, stop the bridge and run:

```bash
kimi-bridge wechat login --replace
```

The previous local credential remains until a new authorization is confirmed. If the platform returns the already-bound bot, the existing credential may be retained. `kimi-bridge wechat logout` removes only adapter-owned credential and receive-state files; it does not remotely delete the bot binding.

## 9. Doctor and startup diagnosis

Run:

```bash
kimi-bridge doctor
```

`doctor` checks the selected local configuration, permissions/path usability, selected adapter credential presence and storage, Kimi Code identity and `kimi doctor config`, Feishu FFmpeg when selected, and WeChat's encrypted-media dependency when selected. It never starts `kimi web`, connects to a chat platform, checks platform permissions, or sends a message.

Resolve every `ERROR` and investigate every `WARN`. Common branches:

- missing or malformed config: repair the protected file;
- incomplete Feishu/QQ TOML pair: provide both values or use the matching QR login;
- malformed existing managed file: repair it or run `login --replace`; do not expect TOML fallback;
- missing/empty allowlist: obtain the real platform identity and add it manually;
- missing Feishu FFmpeg: install it and confirm it is on PATH;
- missing WeChat authorization or media dependency: return to the WeChat QR branch or repair the installation;
- legacy or missing `kimi`: return to Kimi Code preflight;
- Kimi configuration failure: run `kimi doctor config` directly and prove the real prompt again.

Start in the foreground before creating a persistent service:

```bash
kimi-bridge
```

Route a clean startup error to the relevant configuration or platform step. Keep unexpected tracebacks for diagnosis without exposing credentials.

## 10. Persistence

After the foreground live test passes, ask whether the user wants persistent operation. Use one independent process, config, state path, workspace, and platform storage per platform instance.

- Linux: adapt the systemd user-unit template with absolute paths; systemd is Linux-only.
- macOS: use a user-level `launchd` LaunchAgent with the actual executable and config paths.
- Windows: use a current-user Task Scheduler task with the same account that owns the local credential files.

Show service definitions without secrets, obtain approval before creating/enabling/starting them, inspect logs, and repeat a real platform round trip through the service. Keep host lifecycle decisions such as user lingering, login behavior, restart policy, and power behavior explicit.

## 11. Safe abort

On abort, inventory only setup-created changes and offer to reverse only those changes. Preserve pre-existing config, state, workspaces, sessions, platform applications, bot bindings, and service files by default. Removing remote resources or user data requires separate approval naming the exact target. Do not include credentials or complete allowlist identities in the final report.

A useful completion report names the selected platform, versions, compatibility result, config/storage/service paths, live checks that passed, startup/stop/log commands, and platform limitations without printing secrets.
