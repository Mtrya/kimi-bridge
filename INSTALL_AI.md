# kimi-bridge setup agent guide

This guide is an execution contract for an agent installing and configuring kimi-bridge with a user. It is not a script to recite and not a user-facing tutorial. Humans installing manually should read [INSTALL.md](INSTALL.md) and [Configuration](docs/CONFIGURATION.md).

## Operating contract

Read this guide completely before changing the host or a platform app.

Classify each setup action by ownership:

- **Automatic:** inspect, research, install, write non-secret configuration, validate, diagnose, and repair without asking the user to do the work.
- **Question then automatic:** ask only for a consequential preference or fact that cannot be discovered safely, then complete the resulting work yourself.
- **Approval then automatic:** explain a concrete mutation or risk, obtain approval, then perform the action yourself.
- **User-only:** guide the user through an action that technically requires their identity, private secret entry, or console authority. Research the current official interface first and give a short, mature procedure for the exact action. Resume with agent-run verification.
- **External wait:** record what is pending, how completion is recognized, and the verification that will follow. Do not invent a workaround for review or publication delays.
- **Unsupported:** state the exact incompatible boundary and stop that path.

Minimize user-only work. An official link is a source for you to research, not a substitute for instructions. Never give the user a link and ask them to configure a platform unaided.

Additional rules:

- Detect before asking. Do not ask for information available from commands, files you may safely inspect, or platform APIs.
- Never recommend the user to paste credentials into chat. Prefer a private local editor, keychain, secret manager, or masked console input. 
- Preserve existing installations, configuration, state, workspaces, bot settings, webhooks, and event consumers unless the user explicitly approves changing them.
- Do not report success after `doctor`. Setup is complete only after a real allowlisted inbound message and a real completed reply on the selected platform.
- Before researching platform setup from scratch, inspect the [verified setup paths](docs/setup-paths/README.md). Use one only when its preconditions match and its `reverify_after` date has not passed. A stale path is evidence, not instructions: research current official sources before asking the user to perform platform-side actions.
- If console labels or platform requirements have changed, research current official sources and adapt the instructions. Do not guess.
- Keep implementation internals, release procedures, test history, and this guide's control model out of the user conversation unless they explain a user-visible limitation.
- If user actions are inevitable, give them actionable guides.

## Completion outcomes

End with one of these outcomes:

- **Done:** Kimi Code completed a real prompt, the platform delivered an allowlisted message, and kimi-bridge returned a complete reply.
- **Paused:** a named user-only action, approval, publication review, or external wait is outstanding. State exactly how to resume and what you will verify.
- **Unsupported:** the selected environment or platform cannot satisfy a documented requirement. You can still use your own knowledge to assist the user as per their requests.
- **Aborted:** setup stopped at the user's request. Remove only artifacts created during this setup. Never remove pre-existing configuration, state, workspaces, sessions, bot applications, webhooks, or service files without separate explicit approval for the named targets.

## 1. Host and Kimi Code preflight

### 1.1 Inspect the host

Automatically determine:

- operating system and shell;
- whether `uv`, `kimi`, and `kimi-bridge` are installed;
- how each command resolves on `PATH`;
- whether an existing config, state file, workspace, or service is present;
- whether the working paths are new or contain pre-existing data.

Do not install, upgrade, replace, or delete anything during inspection.

### 1.2 Require uv

Run:

```bash
uv --version
```

The tested installation path uses `uv`. If it is missing, research the current official [uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/) for the host and guide the user through only the identity- or privilege-bound step; perform and verify everything else yourself. Normally this step does not involve user actions, if not user approval.

Installing kimi-bridge without `uv` is feasible but is not tested by this project. If the user chooses another Python installation method, assist using your own packaging knowledge, state that the route is untested, and validate the resulting `kimi-bridge` executable and dependencies.

### 1.3 Require official Kimi Code

Run:

```bash
command -v kimi
kimi --version
kimi --help
```

kimi-bridge requires official Kimi Code. Its top-level help includes `web`, `doctor`, and `migrate`. The incompatible legacy Python `kimi-cli` uses a Click-style command surface and commonly prints `kimi, version ...`.

If Kimi Code is absent or the legacy product shadows it, research the current official [Kimi Code installation guide](https://moonshotai.github.io/kimi-code/en/guides/getting-started) and help the user install or expose the correct executable. Preserve legacy sessions; if migration is wanted, use Kimi Code's supported migration path.

### 1.4 Prove Kimi Code readiness

Run:

```bash
kimi doctor config
```

Then make Kimi Code complete a small, non-destructive prompt through its normal user-facing path. Configuration validation alone is insufficient: authentication, provider availability, and the default model must all work.

Authentication or provider selection may require the user's login or secret entry. Research the current official [Kimi Code configuration documentation](https://moonshotai.github.io/kimi-code/en/configuration/config-files), guide that user-only action precisely, and resume with both checks.

Do not proceed to platform setup until a real Kimi Code response succeeds.

## 2. Install and classify compatibility

### 2.1 Install or upgrade with approval

If kimi-bridge is absent, propose:

```bash
uv tool install kimi-bridge
```

If it is already installed, inspect its version and configuration before proposing an upgrade. Upgrading a working installation is a mutation and requires user approval.

After installation:

```bash
command -v kimi-bridge
kimi-bridge --version
kimi-bridge --help
```

### 2.2 Run compatibility classification

Run:

```bash
kimi-bridge compat
```

Handle every verdict:

- **Supported by the current bridge:** continue.
- **Supported only by other bridge releases:** explain the named release choices. Prefer upgrading Kimi Code or kimi-bridge toward the current supported pair; obtain approval before changing either.
- **Untested and older than every tested version:** recommend upgrading Kimi Code. Proceed only if the user explicitly accepts an unsupported-risk live attempt.
- **Untested within the tested range:** explain that no bridge release recorded this exact version despite testing versions on both sides. Recommend a tested Kimi Code version; proceed only with explicit risk acceptance.
- **Untested and newer than every tested version:** explain that the bridge will attempt live protocol validation but compatibility is not established. Proceed only with explicit risk acceptance.

The packaged compatibility map is the source of truth. Do not maintain or consult a second supported-version list.

## 3. Platform preflight

Ask which platform the user wants only after host readiness is established. One process runs one adapter.

Explain the relevant choices:

- **Feishu:** supported and live-validated. Provides interactive approvals and questions, optional thinking output, and file transfer. Requires a published custom app, permissions, event subscriptions, and long-connection delivery.
- **QQ:** supported and live-validated for C2C messaging. It has no interactive approvals, questions, or separate thinking stream, so sessions are forced into `auto` permission mode. Obtain explicit acceptance of this security posture.
- **Telegram:** experimental and not project-live-validated. It supports private chats only. Startup uses long polling and takes over any existing webhook.

Before configuring the selected platform, establish:

1. Is the user familiar with creating and operating bots on this platform?
2. Do they already have a bot application?
3. If so, is it dedicated to kimi-bridge, and may its configuration or event consumer be changed?
4. If not, are they willing and authorized to create and publish one?

Do not encourage reuse merely because a bot exists. Prefer a dedicated bot when kimi-bridge would displace another webhook, long poller, gateway connection, or event consumer.

If `lark-cli` is already installed, it may be used during Feishu setup as described below. If it is absent, do not mention, recommend, or install it.

## 4. Prepare private local configuration

The default file is `Path.home() / .kimi-bridge / config.toml`; `--config` and `KIMI_BRIDGE_CONFIG` select another path. Use [Configuration](docs/CONFIGURATION.md) as the schema authority.

Before writing:

- inspect whether the target file already exists;
- inspect, without displaying secrets, which platform and paths it defines;
- preserve unrelated adapter tables and existing custom settings;
- determine whether `default_workspace` and `state_path` already contain data.

Create the parent directory with private permissions when needed. Write the non-secret structure and placeholders yourself. Have the user enter only credentials that cannot be transferred through an existing secure local mechanism.

On POSIX systems:

```bash
chmod 600 ~/.kimi-bridge/config.toml
```

Do not start with a fabricated allowlist identity and call it configured. Use the selected platform's identity-discovery procedure below, then write the real identity.

## 5. Feishu bootstrap

Official starting points:

- [Feishu custom app development](https://open.feishu.cn/document/develop-process/self-built-application-development-process)
- [Long-connection event setup](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)
- [Card callback handling](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks)

Research these sources and the current console before guiding the user. Translate console labels when useful.

### 5.1 Inspect or create the app

If an app already exists, automatically establish through available CLI/API inspection or user-guided console inspection:

- tenant and app identity;
- whether the bot capability is enabled;
- current published version;
- current scopes;
- subscribed events and callback delivery mode;
- intended availability;
- whether another service consumes its events.

Ask for approval before changing an existing app.

If a new app is needed, app ownership, login, consent, and any enterprise authorization are user-only. Give a researched click-by-click guide for creating a custom app and enabling its bot capability. Perform all API- or CLI-capable follow-up yourself.

### 5.2 Configure required permissions

Ensure the app has these tenant permissions:

- `im:message.p2p_msg:readonly`
- `im:message:readonly`
- `im:message:send_as_bot`
- `im:message:update`
- `im:resource`, or current narrower permissions that together cover the bridge's uploads and downloads

Explain why each missing permission is needed. Do not ask for broad unrelated scopes.

### 5.3 Configure delivery

Configure WebSocket long-connection delivery, not a webhook. Subscribe to:

- `im.message.receive_v1`
- `card.action.trigger`

Check that the app is available to the intended user. Scope, event, or callback changes usually require a new app version and enterprise approval. Guide the user through the exact publication action, then pause for review when necessary.

### 5.4 Use lark-cli only when already present

When `command -v lark-cli` succeeds, use it where helpful to inspect authentication, app metadata, required schemas, published scopes, event subscriptions, and bounded event delivery.

Treat `lark-cli config init --new` as a mutating app/profile operation. Inspect its current help and profiles, explain the effect, and obtain approval before running it. Prefer a named profile when that preserves an existing setup.

lark-cli can create or inspect an app and diagnose scopes or events, but it does not prove that all console permissions are granted or that an app version is published. kimi-bridge also does not read its keychain directly. Never describe lark-cli as a required dependency or a complete bootstrap.

### 5.5 Obtain credentials and identity

App Secret retrieval or reset is user-only when the console requires the user's identity. Have the user place it directly into the protected configuration through a private local path. Do not display it.

Prefer the intended user's `open_id` from the same app and tenant. Discover it through an authenticated platform API, an already available lark-cli path, or a bounded inbound event listener. If console-side configuration makes those impossible before bridge startup, use a temporary non-matching allowlist, start the bridge in the foreground, have the intended user send one direct message, and take the logged `open_id` from the local process. Replace the temporary value immediately.

### 5.6 Validate Feishu

Run local validation, then perform a live round trip:

1. `kimi-bridge doctor`
2. start `kimi-bridge` in the foreground;
3. confirm the long connection becomes ready;
4. have the allowlisted user send `/status`;
5. confirm the expected reply;
6. send a normal prompt and confirm the streamed answer completes;
7. exercise one approval or question interaction;
8. if file support is needed, test one inbound and one outbound file;
9. stop cleanly.

Do not report Feishu ready if only a CLI event consumer or `doctor` passed.

## 6. QQ bootstrap

Use the [verified QQ C2C WebSocket path](docs/setup-paths/qq.md) when its preconditions and freshness metadata permit it.

Official starting points:

- [QQ bot getting started](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/getting-started.html)
- [Access tokens](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)
- [Events and intents](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)
- [Gateway discovery](https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html)
- [Message model](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)

If the verified path is stale or a checkpoint diverges, research the current console and official documentation before instructing the user; QQ's console, review rules, hosts, and availability can change.

### 6.1 Inspect or create the bot

Establish whether the user has a suitable bot, whether it is sandbox or production, who owns it, which testers can use it, and whether another gateway consumer is active.

Creating the bot, accepting platform terms, selecting its owner, and submitting production review are user-only. Give a researched procedure for only those actions. Prefer a dedicated bot.

### 6.2 Configure access

Ensure:

- AppID and AppSecret are available; do not use the deprecated Token credential;
- the intended account is available as a sandbox tester when applicable.

The adapter discovers the WebSocket gateway and identifies with the `GROUP_AND_C2C_EVENT` intent required for `C2C_MESSAGE_CREATE`. Do not ask the user to configure callbacks, event webhooks, or an IP whitelist for the verified WebSocket path. Investigate those controls only if QQ returns a specific error that requires one. A successful token request does not prove gateway authorization.

### 6.3 Discover the user identity

QQ authorization uses the app-specific C2C `user_openid`, not a QQ number or display name. Prefer discovery from a bounded gateway event. If the bridge must be used to receive it, configure a temporary non-matching allowlist, start in the foreground, have the intended tester send one C2C message, and copy the rejected sender's `user_openid` from the local warning. Replace the temporary value immediately.

### 6.4 Validate QQ

Validate each layer independently:

1. `kimi-bridge doctor`
2. access-token exchange;
3. gateway URL discovery;
4. WebSocket identify with the required intent;
5. an inbound C2C message from the real allowlisted `user_openid`;
6. a completed passive reply through the adapter;
7. one normal Kimi prompt with visible streaming and clean finalization;
8. any media types the installation actually needs;
9. clean shutdown.

Reconfirm that QQ runs in forced `auto` mode and cannot present approvals or questions. Respect the platform's passive-reply budget when designing repeated tests.

Do not change a working API origin solely because current documentation names a newer host. Verify the exact endpoint behavior before proposing a code or configuration change.

## 7. Telegram bootstrap

Official starting points:

- [Telegram bots](https://core.telegram.org/bots)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [BotFather](https://core.telegram.org/bots/features#botfather)

Telegram is experimental. State that before the user invests in setup.

### 7.1 Inspect takeover risk

Prefer a new dedicated bot. For an existing bot, use `getMe` and `getWebhookInfo` without exposing the token. Establish whether it has:

- an active webhook;
- queued updates;
- another `getUpdates` consumer;
- another production owner or integration.

kimi-bridge deletes the existing webhook with `drop_pending_updates=true` and then starts long polling. Removing a webhook, discarding queued updates, or displacing another consumer requires explicit approval. If the bot cannot be dedicated to kimi-bridge, stop this path.

### 7.2 Create and configure

Bot creation and token issuance through BotFather are user-only. Research the current BotFather flow and guide the user through the minimum actions. Have the user put the token directly into the protected local config.

Telegram authorization uses a positive numeric user ID. Prefer discovery from an authenticated Bot API update. If bridge startup is needed, use a temporary positive non-matching allowlist, have the intended user send a private message, read the rejected `user_id` locally, and replace the temporary value immediately.

The current adapter accepts private user chats only. Groups, channels, topics, and messages from bots are unsupported.

### 7.3 Validate Telegram

After takeover approval where applicable:

1. `kimi-bridge doctor`
2. `getMe`;
3. confirm the webhook state;
4. start the bridge;
5. receive a private message from the allowlisted numeric user ID;
6. return a complete reply;
7. test one approval interaction;
8. stop cleanly.

Report that this installation passed its own live check, while retaining the project's experimental label.

## 8. Doctor and startup diagnosis

Run:

```bash
kimi-bridge doctor
```

`doctor` validates local configuration, permissions, paths, Kimi Code identity, compatibility, and Kimi's non-starting configuration. It does not connect to Feishu, QQ, or Telegram, validate platform credentials, inspect console permissions, prove event delivery, or send a message.

Resolve every `ERROR`. Investigate every `WARN`; warnings do not make the platform ready. Common branches:

- missing or invalid config: repair the protected file;
- unknown keys: correct typos because unknown keys are ignored at runtime;
- unsafe config permissions: restrict the file;
- missing credentials or allowlist: return to the selected platform bootstrap;
- unusable workspace or state path: distinguish setup-created paths from pre-existing user data before changing anything;
- legacy or missing `kimi`: return to Kimi Code preflight;
- Kimi configuration failure: run `kimi doctor config` directly and prove a real prompt;
- untested Kimi Code: follow the compatibility decision above.

Start in the foreground before creating a persistent service. Route a clean `kimi-bridge: ...` startup error back to the relevant configuration or platform step. Preserve the traceback for unexpected implementation defects.

## 9. Persistence

After the foreground live test passes, ask whether the user wants the bridge to run persistently.

If no, setup is done.

If yes, inspect the host's native service mechanism. On Linux with systemd, adapt [the user-service template](docs/kimi-bridge.service) to the actual absolute executable and config paths. Creating, enabling, or starting a service is a mutation:

1. show the reviewed unit without secrets;
2. obtain approval to create it;
3. validate it before starting;
4. obtain approval to enable/start it;
5. inspect logs;
6. repeat a real platform round trip against the service.

Treat `loginctl enable-linger` as a separate host-lifecycle decision requiring explicit approval.

For multiple platforms, use one process, config, state path, workspace, service, and bot account per instance. Never let instances share a state file, workspace, or bot consumer.

## 10. Final report and safe abort

For a completed setup, report:

- selected platform and its support status;
- installed kimi-bridge and Kimi Code versions;
- compatibility verdict;
- config and service paths;
- which live checks passed;
- how to start, stop, and inspect logs;
- important platform limitations;
- useful chat commands from [Commands](docs/COMMANDS.md).

Do not include credentials or full allowlist identities.

On abort, inventory changes made during this setup. Offer to reverse only those changes. Uninstalling the executable or removing a newly created service must preserve existing config, state, workspaces, inbound files, Kimi sessions, and platform apps by default. Any deletion of user data or platform-side resources requires separate approval naming the exact targets.

## 11. Contribute setup evidence

After reaching a completion outcome, offer once to contribute only if the attempt revalidated a stale path, exposed a checkpoint divergence or useful recovery, or covered an environment without a verified path. An unchanged run through a current path needs no report.

Show the user a redacted summary and obtain explicit approval before creating or updating a GitHub issue under the [setup-evidence policy](docs/setup-paths/README.md#contributing-setup-evidence). Contribution is optional and never changes the setup outcome. The installation produces evidence; promoting it or advancing path dates is separate repository maintenance.
