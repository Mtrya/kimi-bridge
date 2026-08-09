# QR onboarding

A QR result is a credential bootstrap, not a complete bridge setup or proof that messages can already travel end to end.

## Before any QR flow

First make sure official Kimi Code is ready:

```bash
kimi --version
kimi --help
kimi doctor config
kimi -p "Reply with OK only."
```

Create a config whose `platform` names the platform you are onboarding; a bridge runs one selected platform only. The controls below do not start Kimi Code or message polling. A `login` command offers to switch the selected platform when it does not match; `status` and `logout` require an exact match. These controls cover the three QR platforms only; Telegram is configured manually with a Bot API token and numeric user ID.

The three QR control groups are:

```text
kimi-bridge feishu login|status|logout [--replace on login]
kimi-bridge qq     login|status|logout [--replace on login]
kimi-bridge wechat login|status|logout [--replace on login]
```

`status` performs a local inspection and does not verify the network. `logout` removes only files owned by the selected adapter; it does not remotely delete a platform bot or invalidate a platform-side binding.

## What the three QR flows mean

| Platform | QR meaning | Successful local result | What QR does not do |
| --- | --- | --- | --- |
| Feishu/Lark | Official application registration or existing-application selection | `client_id`/`client_secret`, tenant brand/API domain, and the operator `open_id` when returned | Not user OAuth; publication and target-user availability require follow-up |
| QQ | Official bot credential bootstrap/bind | `bot_appid`, the AppSecret decrypted locally from `bot_encrypt_secret`, and the scanner `user_openid` when returned | Not user login or OAuth; sandbox/review/event/message readiness stays manual |
| WeChat | WeChat iLink bot authorization | A bot credential and a stable scanner identity stored locally | No TOML credentials; no automatic allowlist entry; one polling process |

The default managed files are `~/.kimi-bridge/feishu/credentials.json`, `~/.kimi-bridge/qq/credentials.json`, and `~/.kimi-bridge/wechat/credentials.json`. Set each platform's `storage_path` to relocate its directory. The files are adapter-owned and private.

## Feishu: application registration

### What it is

`kimi-bridge feishu login` starts the official Feishu/Lark application-registration flow. The person scanning the QR is approving registration of an application, not granting a user access token. The result supplies an application `client_id` and `client_secret`. The managed record also keeps the tenant brand and its API domain.

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

Open the short-lived URL printed by the command in a browser. The page lets the operator create or select an application and pre-fills the tenant permissions, `im.message.receive_v1` event, and `card.action.trigger` callback required by the bridge. Approve the requested settings in Feishu/Lark. The managed credential is written only after the command completes successfully; without `--replace`, an existing credential is protected from overwrite.

### What still has to happen

The remaining platform-side steps are:

- confirm bot capability and any console settings not represented by the registration add-ons;
- publish an application version and make it available to the intended tenant/user.

When registration returns the operator's `open_id`, the command adds it to `feishu.allowed_users`. Review the entry and edit it if a different sender should be authorized. If no identity is returned, add the `open_id` manually.

Check and test:

```bash
kimi-bridge feishu status
kimi-bridge doctor
kimi-bridge
```

The allowlisted user must send `/status` and a normal prompt while the bridge is in the foreground. Feishu inbound voice also requires `ffmpeg` on PATH.

## QQ: bot credential bootstrap

### What it is

`kimi-bridge qq login` starts the official QQ bot bind flow. The completed result returns `bot_appid` and an encrypted `bot_encrypt_secret`; kimi-bridge decrypts the AppSecret locally and stores only the final managed credential. The per-flow AES key, task ID/QR URL, and encrypted secret blob are not persisted. A QR scan is not QQ user login or OAuth.

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

Open the printed URL, scan it, and approve the official bot bind. When QQ returns the scanner `user_openid`, the command adds it to `qq.allowed_users`. Review or edit the generated entry if you want finer access control:

```toml
[qq]
storage_path = "~/.kimi-bridge/qq"
allowed_users = ["user_openid_returned_by_the_flow"]
```

Never replace it with a QQ number or nickname — the value is app-scoped to the bot application. If no identity is returned, discover the `user_openid` through the normal QQ message/allowlist procedure before starting production use.

QR completion does not establish sandbox tester access, production review, event Intents, or a working message path — confirm those separately.

Check and test:

```bash
kimi-bridge qq status
kimi-bridge doctor
kimi-bridge
```

The allowlisted user must send `/status` and a normal C2C prompt. QQ is always `auto` and cannot render approvals, questions, or a separate thinking stream.

## WeChat: iLink bot authorization

### What it is

`kimi-bridge wechat login` starts WeChat's iLink bot authorization flow. The person scanning the QR authorizes a bot for private-chat use. It is supported onboarding, not user OAuth. The credential is stored in adapter-owned local storage, never in TOML. The result also supplies a stable scanner identity for the bridge allowlist.

One bot authorization can be used by only one polling process. The scan authorizes the bot, not an account; `wechat.allowed_users` entries stay manual.

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

Do not use a nickname, QQ-style identifier, guessed account value, or bot identity.

Check and test:

```bash
kimi-bridge wechat status
kimi-bridge doctor
kimi-bridge
```

A real allowlisted `/status` message and a normal prompt verify the remote authorization and message path. WeChat supports private chats, inbound images/voice/files/videos, and outbound images/videos/files, forces `auto`, and has immutable replies; it has no approvals, questions, separate thinking output, groups, proactive delivery, or native outbound voice.

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

A valid managed file wins; without one, a complete TOML pair is used; a present but invalid managed file fails startup instead of silently falling back. Feishu managed credentials preserve the returned Feishu/Lark API domain. WeChat has no TOML fallback.

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

Logout leaves Feishu/QQ TOML fallback values in `config.toml`.

## After QR onboarding

Run `status`, then `kimi-bridge doctor`, then start the bridge in the foreground. `doctor` is bridge-local and does not prove platform permissions or message delivery. After the round trip succeeds, configure persistence: Linux systemd, macOS launchd, or Windows Task Scheduler.
