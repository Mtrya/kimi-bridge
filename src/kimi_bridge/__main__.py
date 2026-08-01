"""Long-lived bridge process entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .compatibility import (
    COMPATIBILITY_MAP,
    BridgeSupport,
    CompatibilityMapEntry,
    classify_bridge_compatibility,
    normalize_kimi_code_version,
)
from .config import CONFIG_PATH_ENV, Config, load_config, resolve_config_path
from .kimi_server import KimiServerClient, KimiServerError, KimiServerSupervisor
from .platforms.base import PlatformAdapter
from .platforms.feishu import FeishuAdapter
from .platforms.qq import (
    QQAdapter,
    QQBotAPI,
    QQCredentials,
    QQGatewayClient,
    QQTokenManager,
)
from .platforms.telegram import TelegramAdapter
from .router import ChatRouter
from .speech import HttpSpeechTranscriber
from .state import StateStore


_CREDENTIAL_BEARING_LIBRARY_LOGGERS = (
    "httpx",
    "httpcore",
    "websockets",
    "websockets.client",
)


class AdapterConfigurationError(RuntimeError):
    """Expected adapter configuration failure safe to render without traceback."""


def _configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for logger_name in _CREDENTIAL_BEARING_LIBRARY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


async def run(config_path: str | Path) -> None:
    config = load_config(config_path)
    _configure_logging(config.log_level)
    adapter = _build_adapter(config)

    config.default_workspace.mkdir(parents=True, exist_ok=True)

    supervisor = KimiServerSupervisor(preferred_port=config.kimi_server.port)
    stop_requested = asyncio.Event()
    _install_shutdown_signal_handlers(asyncio.get_running_loop(), stop_requested)

    async with supervisor:
        async with KimiServerClient(supervisor=supervisor) as client:
            await client.check_server_version()
            model = await client.get_default_model()
            transcriber = None
            if config.voice.asr is not None:
                transcriber = HttpSpeechTranscriber(
                    base_url=config.voice.asr.base_url,
                    model=config.voice.asr.model,
                    api_key=config.voice.asr.api_key,
                    request_format=config.voice.asr.request_format,
                    language=config.voice.asr.language,
                )
            router = ChatRouter(
                client,
                state_store=StateStore(config.state_path),
                default_workspace=config.default_workspace,
                model=model,
                edit_throttle_seconds=config.edit_throttle_seconds,
                max_output_seconds=config.max_output_seconds,
                interaction_timeout_seconds=(config.interaction_timeout_seconds),
                inbox_subdir=config.inbox_subdir,
                session_list_limit=config.session_list_limit,
                transcriber=transcriber,
            )
            adapter_wait: asyncio.Task[None] | None = None
            signal_wait: asyncio.Task[bool] | None = None
            try:
                await adapter.start(router.handle_inbound, router.handle_interaction)
                adapter_wait = asyncio.create_task(
                    adapter.wait(), name=f"{adapter.name}-adapter"
                )
                signal_wait = asyncio.create_task(
                    stop_requested.wait(), name="shutdown-signal"
                )
                done, _pending = await asyncio.wait(
                    {adapter_wait, signal_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if adapter_wait in done:
                    await adapter_wait
                    raise RuntimeError(
                        f"{adapter.name} adapter stopped unexpectedly"
                    )
            finally:
                if signal_wait is not None and not signal_wait.done():
                    signal_wait.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await signal_wait
                try:
                    await adapter.stop()
                    if adapter_wait is not None:
                        with contextlib.suppress(asyncio.CancelledError):
                            await adapter_wait
                finally:
                    await router.close()


def _install_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop_requested: asyncio.Event
) -> None:
    for watched_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(watched_signal, stop_requested.set)
        except NotImplementedError:
            # Windows event loops cannot register asyncio signal handlers;
            # call_soon_threadsafe also wakes the loop from its poll.
            signal.signal(
                watched_signal,
                lambda *_args: loop.call_soon_threadsafe(stop_requested.set),
            )


def _build_adapter(config: Config) -> PlatformAdapter:
    if config.platform == "feishu":
        # lark-oapi logs its WebSocket URL, including ephemeral connection
        # credentials, at INFO. Keep those credentials out of bridge logs.
        logging.getLogger("Lark").setLevel(logging.WARNING)
        if not config.feishu.app_id or not config.feishu.app_secret:
            raise AdapterConfigurationError(
                "Feishu credentials are missing from ~/.kimi-bridge/config.toml"
            )
        if not config.feishu.allowed_users:
            raise AdapterConfigurationError(
                "feishu.allowed_users must contain at least one user"
            )
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            raise AdapterConfigurationError(
                "FFmpeg is required for Feishu inbound voice; install ffmpeg "
                "and ensure it is on PATH"
            )
        return FeishuAdapter(
            config.feishu.app_id,
            config.feishu.app_secret,
            config.feishu.allowed_users,
            ffmpeg_executable=ffmpeg_path,
        )

    if config.platform == "qq":
        if not config.qq.app_id or not config.qq.app_secret:
            raise AdapterConfigurationError(
                "QQ credentials are missing from ~/.kimi-bridge/config.toml"
            )
        if not config.qq.allowed_users:
            raise AdapterConfigurationError(
                "qq.allowed_users must contain at least one user"
            )
        token_manager = QQTokenManager(
            QQCredentials(config.qq.app_id, config.qq.app_secret)
        )
        api = QQBotAPI(token_manager)
        gateway = QQGatewayClient(token_manager, api.get_gateway_url)
        return QQAdapter(
            config.qq.app_id,
            config.qq.allowed_users,
            api=api,
            gateway=gateway,
            token_manager=token_manager,
        )

    if not config.telegram.bot_token:
        raise AdapterConfigurationError(
            "Telegram bot token is missing from ~/.kimi-bridge/config.toml"
        )
    if not config.telegram.allowed_users:
        raise AdapterConfigurationError(
            "telegram.allowed_users must contain at least one user"
        )
    return TelegramAdapter(
        config.telegram.bot_token,
        config.telegram.allowed_users,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kimi-bridge",
        description="Bridge a local kimi-code server to one configured chat adapter.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            "path to the config file "
            f"(default: ${CONFIG_PATH_ENV} or ~/.kimi-bridge/config.toml)"
        ),
    )
    subcommands = parser.add_subparsers(dest="command")
    doctor_command = subcommands.add_parser(
        "doctor",
        help="validate local configuration without starting services",
        description="Validate bridge and Kimi Code configuration without starting services.",
    )
    doctor_command.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=(
            "path to the config file "
            f"(default: ${CONFIG_PATH_ENV} or ~/.kimi-bridge/config.toml)"
        ),
    )
    compat_command = subcommands.add_parser(
        "compat",
        help="show which Kimi Code versions each bridge release supports",
        description=(
            "Classify one Kimi Code version against the tested compatibility "
            "map of every kimi-bridge release."
        ),
    )
    compat_command.add_argument(
        "--kimi-code",
        metavar="VERSION",
        default=None,
        help="Kimi Code version to classify (default: detect the installed kimi)",
    )
    return parser


def _probe_kimi_code_version(executable: str = "kimi") -> str | None:
    """Return the installed Kimi Code version, or None when undetectable."""

    path = shutil.which(executable)
    if path is None:
        return None
    try:
        completed = subprocess.run(
            [path, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return normalize_kimi_code_version(completed.stdout)
    except ValueError:
        return None


def _current_map_entry() -> CompatibilityMapEntry:
    return next(
        (entry for entry in COMPATIBILITY_MAP if entry.bridge == __version__),
        COMPATIBILITY_MAP[-1],
    )


def _run_compat(kimi_code: str | None) -> int:
    current = _current_map_entry()
    print(
        f"kimi-bridge {__version__} tested Kimi Code versions: "
        f"{', '.join(current.kimi_code)}"
    )
    version = kimi_code if kimi_code is not None else _probe_kimi_code_version()
    if version is None:
        print("could not detect a Kimi Code version; full compatibility map:")
        for entry in COMPATIBILITY_MAP:
            print(f"  kimi-bridge {entry.bridge}: {', '.join(entry.kimi_code)}")
        return 0
    try:
        verdict = classify_bridge_compatibility(
            version, current_bridge=current.bridge
        )
    except ValueError:
        print(f"kimi-bridge: malformed Kimi Code version: {version}", file=sys.stderr)
        return 1
    if verdict.support is BridgeSupport.SUPPORTED_BY_CURRENT_BRIDGE:
        print(f"kimi-code {version} is supported by kimi-bridge {__version__}")
        return 0
    if verdict.support is BridgeSupport.SUPPORTED_BY_OTHER_RELEASES:
        print(
            f"kimi-code {version} is not supported by kimi-bridge {__version__}; "
            f"it is supported by: {', '.join(verdict.releases)}"
        )
        return 1
    if verdict.support is BridgeSupport.UNTESTED_OLDER_THAN_ALL:
        print(
            f"kimi-code {version} is untested: older than every Kimi Code "
            "version tested by any kimi-bridge release"
        )
        return 1
    if verdict.support is BridgeSupport.UNTESTED_WITHIN_TESTED_RANGE:
        assert verdict.tested_range is not None
        low, high = verdict.tested_range
        print(
            f"kimi-code {version} is untested: not tested by any kimi-bridge "
            f"release despite falling inside the tested range {low}–{high}"
        )
        return 1
    print(
        f"kimi-code {version} is untested: newer than every Kimi Code "
        "version tested by any kimi-bridge release"
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    config_path = resolve_config_path(arguments.config)
    if arguments.command == "doctor":
        from .doctor import run_doctor

        return run_doctor(config_path=config_path)
    if arguments.command == "compat":
        return _run_compat(arguments.kimi_code)
    try:
        asyncio.run(run(config_path))
    except KeyboardInterrupt:
        pass
    except (
        AdapterConfigurationError,
        KimiServerError,
        ValueError,
        TypeError,
    ) as exc:
        print(f"kimi-bridge: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
