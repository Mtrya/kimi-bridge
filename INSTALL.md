# Install from source

kimi-bridge requires Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and an authenticated kimi-code 0.28.1 installation. Run `kimi doctor` first and confirm `kimi --version` reports exactly `0.28.1`.

## Install the command

Clone the repository, enter its root directory, and choose one installation:

```bash
git clone <repository-url>
cd kimi-bridge
```

The core install includes the SDK-free experimental Telegram adapter:

```bash
uv tool install .
```

For Feishu, install the optional SDK extra:

```bash
uv tool install '.[feishu]'
```

`uv tool` creates an isolated environment and exposes the `kimi-bridge` command on uv's tool bin path. If the command is not found, run `uv tool update-shell` and open a new shell.

## Verify without starting services

These commands do not load credentials or start the managed Kimi server:

```bash
kimi-bridge --help
kimi-bridge --version
```

## Configure and run

Create `~/.kimi-bridge/config.toml`, select exactly one platform with the top-level `platform` field, and configure that platform's credentials and allowlist. See the [Feishu setup](README.md#feishu-app-setup) or [experimental Telegram setup](README.md#telegram-bot-setup-experimental) for complete examples and required platform settings.

Start the installed command with:

```bash
kimi-bridge
```

The bridge creates its default workspace and atomic state file below `~/.kimi-bridge/`. It starts one configured adapter and one supervised loopback-only `kimi web` child process.

## Reinstall or upgrade from the checkout

Pull the desired source revision, then force a fresh tool installation from that checkout:

```bash
git pull
uv tool install --force .
```

Use the extra again for a Feishu installation:

```bash
uv tool install --force '.[feishu]'
```

## Uninstall

```bash
uv tool uninstall kimi-bridge
```

Uninstalling the tool environment does not remove `~/.kimi-bridge/config.toml`, `state.json`, or workspaces.
