# kimi-bridge setup — agent guide

This file is a setup playbook for a coding agent helping a user install and configure kimi-bridge. Humans looking for the short version should read `INSTALL.md` instead.

## How to use this file

This file is your internal mental model, not a script to recite. Run a natural interview; never mention node IDs, branches, or this file's mechanics to the user.

- **Detect first, ask only at decision nodes.** Run every check yourself before asking the user anything. Ask the user only when you need a decision, a console login, or a secret.
- **Do the legwork.** Read and fetch official documentation yourself and follow it. Never hand the user a link and ask them to bring back values you could discover or verify yourself. The user is involved only for things only they can do: logging into web consoles, approving prompts in their chat app, and typing secrets.
- **Never touch secrets.** Never ask the user to paste credentials into chat, never read or print the config file's secret values, never echo a credential you happen to see. Verification always goes through `kimi-bridge doctor`, which reports presence and counts only.
- **Every setup ends in exactly one outcome**: *done* (the smoke test passed — never claim done before it), *abort* (stop, clean up per §X2, report what was removed), or *unsupported* (state the blocker and the workaround hint, then stop).
- **Official consoles and docs change.** When a console menu, doc page, or command in this file no longer matches reality, fetch the current official documentation and adapt. Do not fail, and do not guess menu names.

Node IDs (`P2`, `CF3`, …) exist only so this file can cross-reference itself.

## R — Start

1. Read this entire file before doing anything.
2. Work through preflight (§P), install (§I), platform choice (§PL), settings (§S), credentials (§C), verification (§D), and the smoke test (§T), in order. §PL and §S involve user decisions; everything else is detection.
3. External handoffs are written as **EXTERNAL(url) → return when the user has X → verification step**. The link replaces instructions you would otherwise have to maintain; the checkpoint is what you verify on return.

## P — Preflight

### P1 — uv (or the pipx branch)

Run `uv --version`. If present, stay on the uv path; uv manages Python, so no Python check is needed.

If uv is missing: **EXTERNAL(https://docs.astral.sh/uv/getting-started/installation/) → return with `uv --version` working.**

If the user cannot or will not install uv, take the pipx branch:

- Verify `python3 --version` is ≥ 3.11 (real requirement on this branch — pipx does not manage Python). Older → *unsupported*: upgrade Python or use uv.
- Wherever this file says `uv tool install/upgrade kimi-bridge`, use `pipx install/upgrade kimi-bridge` instead. pipx is a supported fallback but less tested than uv; say so once.

### P2 — Kimi Code identity

kimi-bridge drives the official Kimi Code CLI (the `kimi` binary). The legacy Python `kimi-cli` shipped a command with the same name and is incompatible.

Run `command -v kimi`, then `kimi --version` and `kimi --help`.

- **Official Kimi Code**: `--version` prints a plain semantic version and `--help` lists `web`, `doctor`, and `migrate` commands (usage line `Usage: kimi [options] [command]`). Proceed to P3.
- **Legacy kimi-cli**: `--version` prints `kimi, version X.Y.Z` and `--help` shows Click-style usage (`Usage: kimi [OPTIONS] COMMAND [ARGS]...`). This is a hard blocker, not a warning. Install official Kimi Code (**EXTERNAL(https://moonshotai.github.io/kimi-code/en/guides/getting-started) → return with `kimi --version` printing a plain semver**), and fix PATH shadowing if both are installed — whichever `kimi` comes first on PATH wins. If the user has legacy sessions worth keeping, official Kimi Code ships `kimi migrate` to import them; mention it.
- **Not found**: same EXTERNAL link, same checkpoint.

Trap to warn about: the bare `https://code.kimi.com/` URL still serves the *legacy* kimi-cli installer. Only the `/kimi-code/install.sh` path (or npm `@moonshot-ai/kimi-code`) installs the right product — this is exactly what the fingerprint check above catches.

### P3 — Kimi Code authentication

An unauthenticated Kimi Code lets `kimi-bridge doctor` pass but kills the bridge at startup. Two authentication paths; let the user pick:

- **Official OAuth**: run `kimi`, then `/login` inside the TUI. The flow is device-code — it works headless (open the link on any device, sign in, enter the code).
- **Own provider configuration**: an API key from `platform.kimi.com` / `platform.kimi.ai`, or a custom provider, configured in `~/.kimi-code/config.toml`. Kimi Code does **not** read shell environment variables like `KIMI_API_KEY` on its own — keys must live in the config file. **EXTERNAL(https://moonshotai.github.io/kimi-code/en/configuration/config-files) → return with a working config.**

Verification gate: `kimi doctor config` exits 0. If the bridge later fails at startup with `kimi-bridge: kimi-code is not authenticated; …`, loop back here. If it fails with `kimi server configuration has no default_model`, the config needs a top-level `default_model` pointing at a configured model — fix it in the same config file and re-verify.

### P4 — Network reachability (after platform choice)

Do these after §PL, before credentials:

- **Telegram**: probe `https://api.telegram.org` with a bounded request, e.g. `curl -sS -m 10 -o /dev/null -w '%{http_code}\n' https://api.telegram.org`. Any HTTP status at all (even 4xx) means the network path works. Only a connection, DNS, or TLS failure means *unsupported*: Telegram is blocked from this network; the user needs a different network or a different platform.
- **QQ**: REST calls require this host's public egress IP on the bot's console IP whitelist. Determine it with a bounded HTTPS request (`curl -fsS -m 10 https://ifconfig.me`) and hand it to the user for the console step in §CQ. Warn: on a dynamic-IP home connection this whitelist entry will rot.
- **Feishu/Lark**: ask whether the tenant is Feishu (feishu.cn) or Lark international (larksuite.com) — consoles and API hosts differ. The bridge requires the WebSocket long-connection event mode; see §CF for the Lark caveat.

## I — Install

### I1 — Install or upgrade

Check `kimi-bridge --version` (or `uv tool list`).
- Already installed → `uv tool upgrade kimi-bridge`, then rejoin at §D (config and credentials already exist).
- Fresh → `uv tool install kimi-bridge`.

### I2 — Bridge ↔ Kimi Code compatibility

Run `kimi-bridge compat --kimi-code "$(kimi --version)"` (plain `kimi-bridge compat` auto-detects the installed kimi). Verdicts:

- **Supported** → proceed.
- **Untested, newer than every tested version** → live protocol checks are attempted; this usually works but is not guaranteed. Surface this as an explicit risk statement to the user and let them decide.
- **Tested only by older bridge releases** → the verdict names them; either upgrade Kimi Code or install the named bridge release (`uv tool install kimi-bridge==<version>`).

The maintained mapping of bridge releases to tested Kimi Code versions lives at `https://github.com/Mtrya/kimi-bridge/raw/main/src/kimi_bridge/compatibility-map.json`; consult it when the local verdict is stale. The happy path is latest kimi-bridge + latest Kimi Code.

### I3 — Downgrade abort leaf

If the user downgraded kimi-bridge after a newer version had run, startup fails with `unsupported bridge state format` — the newer `state.json` schema is intentionally not downgradable. *Abort* options, using the active instance's configured `state_path` (default `~/.kimi-bridge/state.json`; multi-instance setups have one per config): restore a backup of that file, or delete it (loses session bindings and per-conversation settings; credentials in `config.toml` are untouched). Then re-run doctor for the matching bridge version and installation (uv or pipx).

## PL — Platform choice

Ask which chat app the user actually lives in, then present the tradeoffs. One adapter runs per bridge process.

- **Feishu** (recommended default): live-validated. Richest feature set — interactive approvals and questions, optional thinking stream, file upload/download. Heaviest setup: console scopes, event subscriptions, and an app-version publish.
- **QQ**: live-validated in sandbox. Streams answers, but permission mode is forced to `auto` — **every tool call is auto-approved, and there are no approval prompts, questions, or thinking stream**. Make sure the user explicitly accepts that security posture. Console is Chinese-only; sandbox/review overhead applies.
- **Telegram**: experimental — full interactivity (approvals, questions) but **not yet live-validated**. Simplest credentials (one bot token). Blocked in some networks (§P4).

If the user wants more than one platform, use the multi-instance pattern: one bridge process per platform, each with its own config file (`--config`), distinct `state_path` and `default_workspace` in each config, and (on Linux) its own systemd unit. Never point two instances at the same `state_path` or workspace, and never run two instances against the same bot account — inbound messages would be split between them. Distinct platforms only.

## S — Settings

Create `~/.kimi-bridge/config.toml` (TOML; custom path possible via `--config` or `KIMI_BRIDGE_CONFIG`). Full key reference: `docs/CONFIGURATION.md`.

Defaults are right for almost everyone. The interview normally sets only three things: `platform`, the platform credentials, and `allowed_users`. Mention, don't interrogate:

- `max_output_seconds` must be at least 46 × `edit_throttle_seconds` (Feishu's 20-edit budget) — only relevant if the user customizes either; the error names both keys.
- `kimi_server.port`: leave unset. It only pins the loopback port of the internal Kimi server; the default is ephemeral and correct, including multi-instance.
- Permission mode and thinking rendering are **not** config keys — they are per-conversation runtime state changed with `/mode` and `/render-thinking` in chat. Defer them to the closing report.

Secret-entry protocol, all platforms:

1. You write the config file with placeholder credential values and a placeholder allowlist entry.
2. The user fills in real secrets in their own editor. You do not watch, read, or receive them.
3. `chmod 600` the config file.
4. Verification is `kimi-bridge doctor` (§D) — never direct inspection.

## C — Credentials

Each platform section ends at the same verification gate: `kimi-bridge doctor` reports `OK adapter:` for the selected platform (§D). Wrong credentials discovered later loop back here.

### C0 — Allowlist bootstrap (all platforms)

`allowed_users` must be non-empty or the adapter refuses to build, but the user's platform ID is only discoverable after the bridge runs. Bootstrap with a placeholder:

1. Write the config with a placeholder (`allowed_users = ["placeholder"]` for Feishu/QQ, `[1]` for Telegram).
2. Start the bridge in the foreground (`kimi-bridge`).
3. The user messages the bot from their own account. The bridge drops the message and logs a WARNING containing their ID with copy-paste guidance (exact formats per platform below).
4. The user pastes the real ID into `allowed_users` (replacing the placeholder), restarts the bridge.

### CF — Feishu

1. **CF1 — App and bot**: **EXTERNAL(https://open.feishu.cn/document/develop-process/self-built-application-development-process) → return with an App ID, App Secret, the bot capability enabled, and the app made available to the intended user.** The console (open.feishu.cn/app) is login-walled and its menus change; if a menu name doesn't match, fetch current official docs instead of guessing.
2. **CF2 — Scopes** (bridge-specific; upstream docs won't tell you this list): grant tenant scopes `im:message.p2p_msg:readonly`, `im:message:readonly`, `im:message:send_as_bot`, `im:message:update`, and `im:resource` (or the narrower resource upload/download scopes available to the app).
3. **CF3 — Events and callbacks**: configure the WebSocket long connection (not a webhook), subscribe to `im.message.receive_v1`, and enable `card.action.trigger` callbacks on that connection. References: https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case and https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks.
4. **CF4 — Publish**: scope/event changes take effect only after creating a new app version and passing enterprise-admin review. If approval is pending, this is a resumable wait — note where you stopped and resume here; do not improvise a workaround.
5. **CF5 — User identity**: §C0 placeholder flow. The log line is `ignored a message from a non-allowlisted Feishu user (open_id='ou_…', user_id='…'); add either identity to [feishu].allowed_users`. Prefer `open_id` (stable per app); `user_id` also works.
6. **Lark international tenants**: same flow under open.larksuite.com, but the bridge requires the long-connection mode (CF3). If the Lark console does not offer it for the app → *unsupported*, no workaround claimed.

### CQ — QQ

1. **CQ1 — Bot registration**: **EXTERNAL(https://bot.q.qq.com/wiki/) → return with an AppID and AppSecret.** Console: q.qq.com (login-walled). The AppSecret is shown once at creation — have the user copy it immediately. The old `Token` credential is deprecated upstream. Console and wiki are Chinese-only; offer to walk the user through with translated menu names, fetching the current wiki if menus drifted.
2. **CQ2 — IP whitelist**: put this host's egress IP (§P4) on the console IP whitelist, or REST sends fail.
3. **CQ3 — User identity**: §C0 placeholder flow. The log line is `unauthorized sender openid: … — add it to [qq].allowed_users`. `allowed_users` entries are these `user_openid` values.
4. **CQ4 — Sandbox vs production**: develop against the sandbox first (the bridge is live-validated there; specifics shift — check the current console/wiki rather than trusting memory). Production launch requires console review; documented timeline is same-day for submissions before 16:00 (China time), next day otherwise (FAQ: https://q.qq.com/wiki/FAQ/robot/ — volatile). Review waits are resumable: note where you stopped and resume here.

### CT — Telegram

Experimental adapter — repeat the not-live-validated caveat once, here.

1. **CT1 — Bot token**: **EXTERNAL(https://core.telegram.org/bots/features#the-botfather) → return with a bot token from BotFather (`/newbot`).**
2. **CT2 — User ID**: §C0 placeholder flow (`allowed_users = [1]`). The log line is `ignored a message from a non-allowlisted Telegram user (user_id=…); add it to [telegram].allowed_users`. `allowed_users` entries are numeric Telegram user IDs. Do not recommend third-party "ID bots" — the bridge log is the safe discovery path.

## D — Doctor reference

`kimi-bridge doctor` validates without starting services. Branch on check names paired with statuses — never on the exit code alone (exit 1 just means "some check errored"). Parse output whitespace-tolerantly: statuses are padded (e.g. `ERROR  config:` with two spaces), and a check name may appear more than once (a `config` WARN for unknown keys follows the `config` OK). Any `ERROR` entry is blocking.

Checks, in emitted order: `config`, `config permissions`, `adapter`, `workspace`, `state`, `kimi`, `kimi config`. Statuses: `OK`, `WARN`, `ERROR`, `SKIP`. Exit code is 1 if and only if at least one `ERROR` exists; `WARN` and `SKIP` never fail the run.

SKIP cascades are expected behavior, not extra failures: a `config` failure skips `adapter`/`workspace`/`state` (`configuration unavailable`); a `kimi` failure skips `kimi config`. Fix the root check and re-run.

Per-check branches:

- `ERROR config: not found: …` → create the file (§S). `invalid configuration (…)` → fix the TOML; values are deliberately not shown.
- `WARN config: unknown configuration keys ignored: …` → typos in key names (e.g. `feishu.ap_id`); fix them — the keys are silently ignored at runtime otherwise.
- `WARN config permissions: … readable by group or others` → must-fix: `chmod 600`. Other WARNs are acceptable to proceed.
- `ERROR adapter: selected … adapter is missing credentials [and allowlist]` → back to §C.
- `ERROR workspace:` / `ERROR state:` → fix the named path problem; `state: existing state is unreadable or invalid` means `state.json` is corrupt or from a newer bridge (§I3).
- `ERROR kimi:` → §P2 problems (not found, legacy product, unrecognized fingerprint). `WARN … UNTESTED KIMI CODE VERSION …` → §I2 risk statement; acceptable to proceed only with the user's explicit go-ahead.
- `ERROR kimi config: noninteractive validation failed or timed out` → the check has a 15s timeout and false-fails on slow machines. Run `kimi doctor config` yourself; if it passes, treat the doctor error as environmental and continue. If it genuinely fails → §P3.

## T — Smoke test

Only an allowlisted account can drive these. Expected behavior, so you don't "fix" a working bridge:

- Fresh install: `/status` replies `No bound session.` — that **is** success.
- The first real message silently auto-creates a session. No confirmation is expected.

Sequence (Feishu / Telegram):

1. Start the bridge in the foreground; wait for `kimi server is ready on 127.0.0.1:…`. Startup failures with `kimi-bridge: …` loop back to §P or §C.
2. Send `/status` → `No bound session.`
3. Send a normal message → a streamed reply arrives. `/status` now shows a bound session.
4. Approval round-trip: with the default `manual` mode, ask the agent to create a file (e.g. `smoke-test.txt`) → an interactive approval card/keyboard appears → approve → the file is created.
5. Negative test: message the bot from a **non**-allowlisted account → no reply, exactly one WARNING log line. This proves the allowlist, not just the bot.

QQ variant: steps 1–3 and 5 only, with visible streaming in step 3. Step 4 is structurally impossible (forced `auto`, no interaction prompts) — never attempt it on QQ, and re-confirm the user accepted the forced-`auto` posture (§PL).

Closing report (the *done* outcome): platform, config path, where logs go, how to stop the bridge, and the runtime commands worth knowing — `/status`, `/mode`, `/render-thinking`, `/sessions`. No secrets, no mechanics of this file.

## X — Persistence and rollback

### X1 — Persistence

One question: does the user want kimi-bridge to start on boot?

- No → done.
- Yes → recommend a per-user systemd unit on Linux (sample: `docs/kimi-bridge.service`); multi-instance means one unit per config file. Configure it with your own competence for the platform at hand — including SSH/linger quirks on Linux and launchd / Task Scheduler equivalents elsewhere — rather than expecting instructions here.

### X2 — Rollback on abort

Artifacts a setup may have created, in removal order: systemd unit (disable and remove), the bridge installation (via whichever tool manager installed it — `uv tool uninstall kimi-bridge` or `pipx uninstall kimi-bridge`), the configured `state_path` and `default_workspace` (defaults `~/.kimi-bridge/state.json` and `~/.kimi-bridge/workspace/`; multi-instance setups have one set per config), and the config file (default `~/.kimi-bridge/config.toml`). **Never delete the config file without explicit user confirmation — it holds the credentials.** Report exactly which paths were removed. Platform-side leftovers (Feishu app versions, QQ sandbox bots, Telegram bots) are removed in their respective consoles; point the user at them.
