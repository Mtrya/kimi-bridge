# QR onboarding

This page explains what each QR flow does, what command to run, what the person scanning the code is approving, and what still has to be configured afterward. A QR result is a credential bootstrap, not a complete bridge setup or a proof that messages can already travel end to end.

## Before any QR flow

First make sure official Kimi Code is ready:

```bash
kimi --version
kimi --help
kimi doctor config
kimi -p "Reply with OK only."
```

Create a config whose `platform` names the platform you are onboarding. A bridge process runs one selected platform only. The terminal controls below do not start Kimi Code or message polling, and they refuse to run when the config's selected platform does not match the command platform. These controls cover the three QR platforms only; Telegram has no QR or terminal authorization command and is configured manually with a Bot API token and numeric user ID.

The three QR control groups are:

```text
kimi-bridge feishu login|status|logout [--replace on login]
kimi-bridge qq     login|status|logout [--replace on login]
kimi-bridge wechat login|status|logout [--replace on login]
```

`status` performs a local inspection and does not verify the network. `logout` removes only files owned by the selected adapter. It does not remotely delete a platform bot or invalidate a platform-side binding. If a Feishu or QQ TOML fallback remains in `config.toml`, logout does not remove it.

## What the three QR flows mean

| Platform | QR meaning | Successful local result | What QR does not do |
| --- | --- | --- | --- |
| Feishu/Lark | Official application registration | `client_id`/`client_secret`, plus the tenant brand and API domain | It is not user OAuth; it does not configure bot capability, permissions, events, publication, or `feishu.allowed_users` |
| QQ | Official bot credential bootstrap/bind | `bot_appid` and the AppSecret decrypted locally from `bot_encrypt_secret` | It is not QQ user login or OAuth; it does not establish every sandbox/review/event/message prerequisite or configure `qq.allowed_users` |
| WeChat | WeChat iLink bot authorization | A bot credential and a stable scanner identity stored locally | It does not write credentials to TOML, add an arbitrary user to the allowlist, or permit a second polling process |

The default managed files are `~/.kimi-bridge/feishu/credentials.json`, `~/.kimi-bridge/qq/credentials.json`, and `~/.kimi-bridge/wechat/credentials.json`. Set each platform's `storage_path` to relocate its directory. The files are adapter-owned and private.

## Feishu: application registration

### What it is

`kimi-bridge feishu login` starts the official Feishu/Lark application-registration flow. The person scanning the QR is approving registration of an application, not granting a user access token. The result supplies an application `client_id` and `client_secret`. The managed record also keeps whether the tenant is Feishu or Lark and the API domain required for that tenant.

### Run it

Set `platform = "feishu"` and choose the storage path if needed. Leave the allowlist empty during bootstrap:

```toml
platform = "feishu"

[feishu]
storage_path = "~/.kimi-bridge/feishu"
allowed_users = []
```

Then run:

```bash
kimi-bridge feishu login
```

Open the short-lived URL printed by the command in a browser, then follow the page instructions to scan with or approve in Feishu/Lark. The managed credential is written only after the command completes successfully; without `--replace`, an existing credential is protected from overwrite.

### What still has to happen

In the Feishu/Lark developer console, complete and confirm the platform-side setup required by the features you intend to use:

- enable the bot capability;
- grant the message, resource, and voice-recognition permissions required by the bridge features you will use; the exact available permissions can change, so follow the current console and official documentation;
- subscribe to `im.message.receive_v1` and `card.action.trigger` and configure long-connection delivery;
- publish an application version and make it available to the intended tenant/user;
- obtain the intended sender's `open_id` for the same application and tenant, then manually add it to `feishu.allowed_users`.

QR registration does not automatically complete any of those steps. Do not add the scanner to the bridge allowlist merely because they approved registration.

Check and test:

```bash
kimi-bridge feishu status
kimi-bridge doctor
kimi-bridge
```

The allowlisted user must send `/status` and a normal prompt while the bridge is in the foreground. Feishu inbound voice also requires `ffmpeg` on PATH.

## QQ: bot credential bootstrap

### What it is

`kimi-bridge qq login` starts the official QQ bot bind flow. The completed result returns `bot_appid` and encrypted `bot_encrypt_secret`. The bridge decrypts the AppSecret locally and stores only the final managed credential. The per-flow AES key, task ID/QR URL, and encrypted secret blob are not persisted. Runtime continues to use the existing QQ REST, token, and WebSocket gateway transport.

A QR scan is not QQ user login or OAuth. The flow may also return a scanner `user_openid`. That value is scoped to the bot application and must be treated as an app-specific identity: it is not a QQ number, nickname, or display name.

### Run it

Set `platform = "qq"` and leave the allowlist empty only for this bootstrap step:

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

Open the printed URL, scan it, and approve the official bot bind. If a scanner `user_openid` is printed, manually copy that exact value into `qq.allowed_users`:

```toml
[qq]
storage_path = "~/.kimi-bridge/qq"
allowed_users = ["user_openid_returned_by_the_flow"]
```

Never replace it with a QQ number or a nickname. If the flow does not return an identity for the intended sender, discover the app-scoped `user_openid` through the normal QQ message/allowlist procedure before starting production use.

QR completion does not by itself prove sandbox tester access, production review, authorization for the required event Intents, gateway readiness, or a complete message path. Confirm the current QQ platform requirements separately and do not share the bot with another polling process.

Check and test:

```bash
kimi-bridge qq status
kimi-bridge doctor
kimi-bridge
```

The allowlisted user must send `/status` and a normal C2C prompt. QQ is always `auto` and cannot render approvals, questions, or a separate thinking stream.

## WeChat: iLink bot authorization

### What it is

`kimi-bridge wechat login` starts WeChat's iLink bot authorization flow. The person scanning the QR authorizes a bot for private-chat use. This is supported onboarding, not a user OAuth flow. The credential is stored in adapter-owned local storage, never in TOML. The result also supplies a stable scanner identity for the bridge allowlist.

One bot authorization can be used by only one polling process. The human WeChat account that scans the QR does not automatically grant access to every account; the bridge still requires manual `wechat.allowed_users` entries.

### Run it

Set `platform = "wechat"`, a private storage directory, and an empty allowlist while bootstrapping:

```toml
platform = "wechat"

[wechat]
storage_path = "~/.kimi-bridge/wechat"
allowed_users = []
```

Run:

```bash
kimi-bridge wechat login
```

Open the printed authorization URL in WeChat, scan and approve the iLink bot authorization, and enter a verification code only if the flow explicitly asks for one. After confirmation, copy the returned stable scanner identity into `wechat.allowed_users`:

```toml
[wechat]
storage_path = "~/.kimi-bridge/wechat"
allowed_users = ["stable_scanner_identity_returned_by_login"]
```

Do not use a nickname, QQ-style identifier, guessed account value, or bot identity. The credential is saved at `~/.kimi-bridge/wechat/credentials.json` by default. It is not written to TOML.

Check and test:

```bash
kimi-bridge wechat status
kimi-bridge doctor
kimi-bridge
```

`status` checks only local authorization metadata and storage hygiene. A real allowlisted `/status` message and a normal prompt are required to verify the remote authorization and message path. WeChat supports private chats, inbound images/voice/files/videos, and outbound images/videos/files. It forces `auto`, has immutable replies, and does not support approvals, questions, separate thinking output, groups, proactive delivery, or native outbound voice messages.

## Managed credentials, fallback, replacement, and logout

Feishu and QQ can use either managed QR credentials or a complete TOML pair:

```toml
[feishu]
app_id = "cli_replace_me"
app_secret = "replace_me"
storage_path = "~/.kimi-bridge/feishu"
allowed_users = ["<real Feishu open_id>"]

[qq]
app_id = "replace_me"
app_secret = "replace_me"
storage_path = "~/.kimi-bridge/qq"
allowed_users = ["<real QQ user_openid>"]
```

The priority is simple: a valid managed file wins; if no managed file exists, a complete TOML pair is used; if a managed file exists but is bad, startup fails instead of silently falling back. Feishu managed credentials preserve the returned Feishu/Lark API domain. WeChat has no TOML credential fallback.

To replace an existing managed credential:

```bash
kimi-bridge feishu login --replace
kimi-bridge qq login --replace
kimi-bridge wechat login --replace
```

Replacement keeps the previous credential until the new flow is confirmed. To remove local adapter-owned files:

```bash
kimi-bridge feishu logout
kimi-bridge qq logout
kimi-bridge wechat logout
```

Logout does not remove a remote bot binding, and Feishu/QQ TOML fallback values remain in `config.toml`.

## After QR onboarding

Run `status`, then `kimi-bridge doctor`, then start the bridge in the foreground. `doctor` is a bridge-local diagnostic; it does not prove platform permissions, event delivery, or message round trips. Once `/status` and a normal prompt have completed successfully, configure Linux systemd, macOS launchd, or Windows Task Scheduler as appropriate. Systemd is a Linux mechanism only.
