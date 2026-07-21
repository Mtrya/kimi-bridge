"""Long-lived bridge process entry point."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from .config import load_config
from .kimi_server import KimiServerClient, KimiServerSupervisor
from .platforms.feishu import FeishuAdapter
from .router import ChatRouter
from .state import StateStore


async def run() -> None:
    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # lark-oapi logs its WebSocket URL, including ephemeral connection
    # credentials, at INFO. Keep those credentials out of bridge logs.
    logging.getLogger("Lark").setLevel(logging.WARNING)
    if not config.feishu.app_id or not config.feishu.app_secret:
        raise RuntimeError(
            "Feishu credentials are missing from ~/.kimi-bridge/config.toml"
        )
    if not config.feishu.allowed_users:
        raise RuntimeError("feishu.allowed_users must contain at least one user")

    config.default_workspace.mkdir(parents=True, exist_ok=True)

    supervisor = KimiServerSupervisor(preferred_port=config.kimi_server.port)
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for watched_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(watched_signal, stop_requested.set)

    async with supervisor:
        async with KimiServerClient(supervisor=supervisor) as client:
            await client.check_server_version()
            model = await client.get_default_model()
            router = ChatRouter(
                client,
                state_store=StateStore(),
                default_workspace=config.default_workspace,
                model=model,
                edit_throttle_seconds=config.edit_throttle_seconds,
                interaction_timeout_seconds=(config.interaction_timeout_seconds),
                inbox_subdir=config.inbox_subdir,
            )
            adapter = FeishuAdapter(
                config.feishu.app_id,
                config.feishu.app_secret,
                config.feishu.allowed_users,
            )
            adapter_wait: asyncio.Task[None] | None = None
            signal_wait: asyncio.Task[bool] | None = None
            try:
                await adapter.start(router.handle_inbound, router.handle_card_action)
                adapter_wait = asyncio.create_task(
                    adapter.wait(), name="feishu-adapter"
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
                    raise RuntimeError("Feishu WebSocket stopped unexpectedly")
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


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
