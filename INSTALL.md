# Install and operate kimi-bridge

This is the starting runbook for both humans and coding agents. An agent should inspect the machine first, explain what it found, and pause at every user decision or credential boundary. Do not claim success until the selected chat adapter has been exercised by its allowlisted user.

## 1. Inspect before changing anything

Confirm the host platform and inventory existing tools without installing or replacing them:

```bash
uname -s
python3 --version
command -v uv || true
command -v kimi || true
kimi --version
kimi --help
kimi doctor config
```

kimi-bridge requires Python 3.11 or newer, [uv](https://docs.astral.sh/uv/getting-started/installation/), and authenticated official [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started). Linux is the supported platform; macOS and Windows are experimental, so state that clearly before continuing on them. Official Kimi Code has `web`, `doctor`, and `migrate` commands. The older Python product prints a `kimi, version ...` banner and is incompatible; do not install or retain legacy `kimi-cli` as a workaround. Follow Moonshot AI's current installation or migration guide when Kimi Code is absent or legacy.

Ask the user which adapter they want before configuring anything:

- Feishu requires an app ID, app secret, and at least one Feishu `open_id` or `user_id`.
- Experimental Telegram requires a bot token and at least one stable numeric Telegram user ID. Usernames are not authorization identities.
- QQ requires an app ID, app secret, and at least one QQ C2C `user_openid`. Register the app at [q.qq.com](https://q.qq.com/), whitelist the tester's QQ number in the console's sandbox (沙箱) configuration (up to 20 test users), and add this host's egress IP to the console's OpenAPI IP whitelist — REST calls fail until that IP is allowlisted, and a dynamic IP needs re-whitelisting after it changes.

Do not ask the user to paste credentials into a repository file. Do not print, log, commit, or echo credentials or real allowlist values. If a secret already exists in a protected environment file or secret manager, transfer it locally without displaying it. Otherwise ask the user to enter it through a private local editor or another secret-safe channel. Avoid commands that leave secrets in shell history.

## 2. Install from PyPI

One package covers all adapters:

```bash
uv tool install kimi-bridge
```

Check the exposed command:

```bash
command -v kimi-bridge
kimi-bridge --version
kimi-bridge --help
```

If `uv` reports that its tool bin directory is not on `PATH`, run `uv tool update-shell`, then start a fresh shell. Do not replace a working installation merely because its resolved path differs from an example in this guide.

## 3. Create protected configuration

Create the private configuration directory, then let the user populate the selected adapter's values using the [configuration reference](docs/CONFIGURATION.md):

```bash
install -d -m 700 ~/.kimi-bridge
touch ~/.kimi-bridge/config.toml
chmod 600 ~/.kimi-bridge/config.toml
```

On Windows, create `%USERPROFILE%\.kimi-bridge\config.toml` in the user profile instead; `doctor` skips the POSIX permission check there.

Do not place secrets in environment files inside the project. After the file is populated, inspect only its ownership, mode, and redacted structure. Never include its contents in tool output or a diagnostic report.

Run the non-starting diagnostic:

```bash
kimi-bridge doctor
```

`doctor` loads configuration, checks secret-file permissions and writable paths, fingerprints the `kimi` executable, classifies its version, and runs Kimi's non-starting configuration check. It reports only credential presence and allowlist counts. Warnings return success; blocking errors return nonzero. Resolve every error before continuing and explain any warning rather than suppressing it.

## 4. Verify in the foreground

Start the bridge from a normal shell:

```bash
kimi-bridge
```

Ask the allowlisted user to send `/status`, then send one small prompt and confirm that its reply streams and completes without a duplicate message. Exercise an approval or question if the selected permission mode requires it. Stop with `Ctrl-C` and confirm clean shutdown.

For Feishu, do not call the setup complete until direct messages, editable replies, and any configured card callbacks work. For QQ, confirm C2C answer streaming and clean finalization, then test a small inbound image and every `/send` media type the installation needs; QQ is project-live-validated in sandbox, while production review, rate enforcement, and IP-whitelist state remain deployment-specific. For Telegram, state plainly that the adapter remains experimental and was not project-live-validated unless this installation's own private-chat test passed.

## 5. Optional per-user systemd service (Linux)

Do not create a service automatically. First ask: “Do you want kimi-bridge to run persistently as your user?” A foreground-only installation is complete if the answer is no. This section applies to Linux with systemd; on macOS and Windows, run the bridge in the foreground.

If the user says yes, resolve the actual executable locations before creating anything:

```bash
command -v kimi-bridge
command -v kimi
uv tool dir --bin
```

The repository includes [a user-unit template](docs/kimi-bridge.service). Its default paths match uv's usual user tool directory and Kimi Code's usual installer directory, but an agent must replace `ExecStart` and the `PATH` entries when the commands above report different absolute paths. Do not put credentials in the unit.

After receiving approval to create the service, place the reviewed unit at `~/.config/systemd/user/kimi-bridge.service`, then validate and reload it without starting it:

```bash
install -d -m 700 ~/.config/systemd/user
install -m 600 docs/kimi-bridge.service ~/.config/systemd/user/kimi-bridge.service
systemd-analyze --user verify ~/.config/systemd/user/kimi-bridge.service
systemctl --user daemon-reload
```

The copy command assumes a source checkout. For a PyPI-only installation, create the same reviewed unit from the linked template. If `systemd-analyze --user` is unavailable, report that syntax validation remains pending rather than skipping it silently.

Pause again and ask for explicit approval before enabling or starting the unit. Only after approval:

```bash
systemctl --user enable --now kimi-bridge.service
systemctl --user status kimi-bridge.service
journalctl --user -u kimi-bridge.service --since today
```

Review the journal for startup success and confirm it contains no credential values. Repeat the same `/status` and streamed-reply chat check against the service.

By default, a user service follows the user's login session. `loginctl enable-linger "$USER"` makes the user manager start at boot and remain available after logout. Treat lingering as a separate user decision because it changes host lifecycle behavior; never enable it silently. Disabling it later uses `loginctl disable-linger "$USER"`.

## Operations

Inspect and follow logs:

```bash
systemctl --user status kimi-bridge.service
journalctl --user -u kimi-bridge.service -f
```

Restart after an approved configuration change:

```bash
kimi-bridge doctor
systemctl --user restart kimi-bridge.service
```

Upgrade, validate, and restart:

```bash
uv tool upgrade kimi-bridge
kimi-bridge --version
kimi-bridge doctor
systemctl --user restart kimi-bridge.service
```

Pin a known release for rollback:

```bash
uv tool install --force 'kimi-bridge==0.1.0'
```

Then rerun `doctor` and restart the service.

Stop and disable the service without deleting user data:

```bash
systemctl --user disable --now kimi-bridge.service
```

After the user confirms removal, delete only `~/.config/systemd/user/kimi-bridge.service`, run `systemctl --user daemon-reload`, and uninstall the tool:

```bash
uv tool uninstall kimi-bridge
```

Uninstalling the tool or service must preserve `~/.kimi-bridge/config.toml`, `state.json`, workspaces, inbound files, and Kimi sessions unless the user separately asks to remove those specific paths.

## Install from a checkout

Contributors can use an isolated tool directly from a trusted checkout:

```bash
uv tool install .
```

For development and validation commands, see [README](README.md#development).
