# Install kimi-bridge

**中文**: [INSTALL.md](INSTALL.md)

kimi-bridge connects one local Kimi Code installation to one supported chat bot. The recommended setup path is to give [the setup-agent guide](INSTALL_AI.md) to a capable coding agent: platform bot setup involves credentials, permissions, event delivery, identity discovery, and a real end-to-end test that cannot be validated by package installation alone.

## Support

| Platform | Status | Important limitation |
| --- | --- | --- |
| Feishu | Supported and live-validated | Requires a published custom app, long-connection events, permissions, and an allowlist |
| QQ | Supported and live-validated for C2C | Forced `auto` permission mode; no approval prompts, questions, or separate thinking stream |
| Telegram | Experimental | Private chats only; startup replaces any webhook and drops pending updates |
| Lark International | Unsupported | The current adapter uses Feishu API and WebSocket domains |

One kimi-bridge process runs one platform adapter. Use separate processes, configs, state files, workspaces, services, and bot accounts for multiple platforms.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Python 3.11 or newer, supplied or selected by uv
- authenticated official [Kimi Code](https://moonshotai.github.io/kimi-code/en/guides/getting-started)
- a dedicated bot application on the selected platform

The older Python `kimi-cli` product is incompatible even though it also installs a `kimi` command. Official Kimi Code's help includes `web`, `doctor`, and `migrate`.

Verify Kimi Code before installing the bridge:

```bash
kimi --version
kimi --help
kimi doctor config
```

Kimi Code must also complete a real prompt. A configuration check alone does not prove authentication or model availability.

Installing without uv is feasible but is not tested by this project.

## Install

```bash
uv tool install kimi-bridge
kimi-bridge --version
kimi-bridge compat
```

`compat` compares the installed Kimi Code version with the compatibility history packaged in kimi-bridge. Use a tested pair when possible. An untested version may still be attempted, but it is not established as compatible.

## Configure

kimi-bridge reads `~/.kimi-bridge/config.toml` by default. A different file can be selected with `--config PATH` or `KIMI_BRIDGE_CONFIG`.

Create a private file:

```bash
install -d -m 700 ~/.kimi-bridge
touch ~/.kimi-bridge/config.toml
chmod 600 ~/.kimi-bridge/config.toml
```

Populate one adapter using [the complete configuration reference](docs/CONFIGURATION.md). Enter credentials through a private local editor or secret manager; do not put them on command lines, commit them, or paste them into issue reports.

Platform setup requires more than credentials:

- Feishu needs the bot capability, exact message/resource permissions, long-connection message and card events, a published app version, and the intended user's `open_id`.
- QQ needs AppID/AppSecret, access for the intended sandbox tester when applicable, and the intended user's app-specific `user_openid`.
- Telegram needs a bot token, the intended user's numeric ID, and a dedicated bot whose existing webhook or update consumer may safely be replaced.

The setup agent guide contains the platform-specific bootstrap and verification procedures. Official platform references are linked from there for manual operators.

## Validate

Run the non-starting diagnostic:

```bash
kimi-bridge doctor
```

Resolve every error before startup. `doctor` checks local configuration, paths, Kimi Code, and credential presence. It does not connect to the chat platform, validate bot permissions, receive an event, or send a message.

Start in the foreground:

```bash
kimi-bridge
```

From the allowlisted chat account:

1. send `/status` and confirm a reply;
2. send a normal prompt and confirm the streamed response completes;
3. on Feishu or Telegram, exercise an approval or question;
4. test any file types your installation depends on.

Do not consider setup complete until this live round trip passes.

## Run persistently

Foreground operation is fully supported. On Linux, [the systemd user-unit template](docs/kimi-bridge.service) can be adapted to the absolute paths reported by:

```bash
command -v kimi-bridge
command -v kimi
```

Review the unit before placing it at `~/.config/systemd/user/kimi-bridge.service`. Do not put credentials in the unit. Creating or enabling a persistent service and enabling user lingering are separate administrative decisions.

Useful operations:

```bash
systemctl --user status kimi-bridge.service
journalctl --user -u kimi-bridge.service -f
kimi-bridge doctor
uv tool upgrade kimi-bridge
```

Stopping or uninstalling kimi-bridge should preserve `config.toml`, `state.json`, workspaces, inbound files, Kimi sessions, and platform bot applications unless you explicitly choose to remove those named resources.

## References

- [Configuration](docs/CONFIGURATION.md)
- [Chat commands](docs/COMMANDS.md)
- [Architecture and compatibility policy](docs/ARCHITECTURE.md)
- [Setup-agent guide](INSTALL_AI.md)
- [Development](README.en_US.md#development)
