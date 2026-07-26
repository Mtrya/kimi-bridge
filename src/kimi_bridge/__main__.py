"""Long-lived bridge process entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import CONFIG_PATH_ENV, Config, load_config, resolve_config_path
from .kimi_server import KimiServerClient, KimiServerSupervisor
from .platforms.base import PlatformAdapter
from .platforms.feishu import FeishuAdapter
from .platforms.telegram import TelegramAdapter
from .router import ChatRouter
from .state import StateStore


async def run(config_path: str | Path) -> None:
    config = load_config(config_path)
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    adapter = _build_adapter(config)

    config.default_workspace.mkdir(parents=True, exist_ok=True)

    supervisor = KimiServerSupervisor(preferred_port=config.kimi_server.port)
    stop_requested = asyncio.Event()
    _install_shutdown_signal_handlers(asyncio.get_running_loop(), stop_requested)

    async with supervisor:
        async with KimiServerClient(supervisor=supervisor) as client:
            await client.check_server_version()
            model = await client.get_default_model()
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
            raise RuntimeError(
                "Feishu credentials are missing from ~/.kimi-bridge/config.toml"
            )
        if not config.feishu.allowed_users:
            raise RuntimeError(
                "feishu.allowed_users must contain at least one user"
            )
        return FeishuAdapter(
            config.feishu.app_id,
            config.feishu.app_secret,
            config.feishu.allowed_users,
        )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not config.telegram.bot_token:
        raise RuntimeError(
            "Telegram bot token is missing from ~/.kimi-bridge/config.toml"
        )
    if not config.telegram.allowed_users:
        raise RuntimeError(
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    config_path = resolve_config_path(arguments.config)
    if arguments.command == "doctor":
        from .doctor import run_doctor

        return run_doctor(config_path=config_path)
    try:
        asyncio.run(run(config_path))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
