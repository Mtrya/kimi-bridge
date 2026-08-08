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
from .platforms.feishu import FEISHU_API_DOMAIN, FeishuAdapter
from .platforms.feishu.storage import FeishuStorage, FeishuStorageError
from .platforms.qq import (
    QQAdapter,
    QQBotAPI,
    QQCredentials,
    QQGatewayClient,
    QQTokenManager,
)
from .platforms.qq.storage import QQStorage
from .platforms.telegram import TelegramAdapter
from .platforms.wechat import WeChatAuthenticationExpired
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
        if not config.feishu.allowed_users:
            raise AdapterConfigurationError(
                "feishu.allowed_users must contain at least one user"
            )
        storage = FeishuStorage(config.feishu.storage_path)
        inspection = storage.inspect(platform_name=sys.platform)
        if inspection.directory_error is not None:
            raise AdapterConfigurationError(
                "Feishu storage path is not usable "
                f"({inspection.directory_error}); fix or remove {storage.path} "
                "to use the [feishu] app_id/app_secret fallback"
            )
        if inspection.credential_error is not None:
            raise AdapterConfigurationError(
                "Feishu managed credentials are invalid or unavailable "
                f"({inspection.credential_error}); "
                "run kimi-bridge feishu login --replace"
            )
        if inspection.credential is not None:
            credential = inspection.credential
            app_id = credential.app_id
            app_secret = credential.app_secret
            api_domain = credential.api_domain
        elif config.feishu.app_id and config.feishu.app_secret:
            app_id = config.feishu.app_id
            app_secret = config.feishu.app_secret
            api_domain = FEISHU_API_DOMAIN
        else:
            raise AdapterConfigurationError(
                "Feishu credentials are unavailable; first run "
                "kimi-bridge feishu login, or configure a complete "
                "[feishu] app_id and app_secret in config.toml"
            )
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            raise AdapterConfigurationError(
                "FFmpeg is required for Feishu inbound voice; install ffmpeg "
                "and ensure it is on PATH"
            )
        return FeishuAdapter(
            app_id,
            app_secret,
            config.feishu.allowed_users,
            api_domain=api_domain,
            ffmpeg_executable=ffmpeg_path,
        )

    if config.platform == "qq":
        if not config.qq.allowed_users:
            raise AdapterConfigurationError(
                "qq.allowed_users must contain at least one user"
            )
        storage = QQStorage(config.qq.storage_path)
        inspection = storage.inspect(platform_name=sys.platform)
        if inspection.directory_error is not None:
            raise AdapterConfigurationError(
                "QQ storage path is not usable "
                f"({inspection.directory_error}); fix or remove {storage.path} "
                "to use the [qq] app_id/app_secret fallback"
            )
        if inspection.credential_error is not None:
            raise AdapterConfigurationError(
                "QQ managed credentials are invalid or unavailable "
                f"({inspection.credential_error}); "
                "run kimi-bridge qq login --replace"
            )
        if inspection.credential is not None:
            credential = inspection.credential
            app_id = credential.app_id
            app_secret = credential.app_secret
        elif config.qq.app_id and config.qq.app_secret:
            app_id = config.qq.app_id
            app_secret = config.qq.app_secret
        else:
            raise AdapterConfigurationError(
                "QQ credentials are unavailable; first run kimi-bridge qq login, "
                "or configure a complete [qq] app_id and app_secret in config.toml"
            )
        credentials = QQCredentials(app_id, app_secret)
        token_manager = QQTokenManager(credentials)
        api = QQBotAPI(token_manager)
        gateway = QQGatewayClient(token_manager, api.get_gateway_url)
        return QQAdapter(
            app_id,
            config.qq.allowed_users,
            api=api,
            gateway=gateway,
            token_manager=token_manager,
        )

    if config.platform == "wechat":
        if not config.wechat.allowed_users:
            raise AdapterConfigurationError(
                "wechat.allowed_users must contain at least one user"
            )
        from .platforms.wechat import (
            WeChatAPI,
            WeChatAdapter,
            WeChatMediaDependencyError,
            WeChatStorage,
            WeChatStorageError,
            require_wechat_media_dependency,
        )

        try:
            require_wechat_media_dependency()
        except WeChatMediaDependencyError as exc:
            raise AdapterConfigurationError(str(exc)) from exc

        storage = WeChatStorage(config.wechat.storage_path)
        try:
            credential = storage.load_credential()
            runtime_state = storage.load_runtime_state()
        except WeChatStorageError as exc:
            raise AdapterConfigurationError(
                f"WeChat local authorization/state is unavailable: {exc}; "
                "run kimi-bridge wechat login"
            ) from exc
        api = WeChatAPI(credential)
        return WeChatAdapter(
            credential.bot_id,
            config.wechat.allowed_users,
            api=api,
            storage=storage,
            runtime_state=runtime_state,
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
    wechat_command = subcommands.add_parser(
        "wechat",
        help="manage local WeChat QR authorization",
        description=(
            "Manage local WeChat QR authorization without starting Kimi Code "
            "or message polling."
        ),
    )
    wechat_command.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=(
            "path to the config file "
            f"(default: ${CONFIG_PATH_ENV} or ~/.kimi-bridge/config.toml)"
        ),
    )
    wechat_subcommands = wechat_command.add_subparsers(
        dest="wechat_command", required=True
    )
    for command_name, help_text in (
        ("login", "authorize an iLink bot by scanning a QR URL"),
        ("status", "inspect local WeChat authorization without network access"),
        ("logout", "remove only adapter-owned WeChat authorization files"),
    ):
        command = wechat_subcommands.add_parser(command_name, help=help_text)
        command.add_argument(
            "--config",
            metavar="PATH",
            default=argparse.SUPPRESS,
            help=(
                "path to the config file "
                f"(default: ${CONFIG_PATH_ENV} or ~/.kimi-bridge/config.toml)"
            ),
        )
        if command_name == "login":
            command.add_argument(
                "--replace",
                action="store_true",
                help="replace a stored authorization only after a new login succeeds",
            )

    for platform_name, description, command_dest, help_texts in (
        (
            "feishu",
            "Manage local Feishu QR application registration without starting "
            "Kimi Code or message polling.",
            "feishu_command",
            (
                ("login", "register a Feishu or Lark app by scanning a QR URL"),
                ("status", "inspect local Feishu authorization without network access"),
                ("logout", "remove only adapter-owned Feishu authorization files"),
            ),
        ),
        (
            "qq",
            "Manage local QQ QR authorization without starting Kimi Code or "
            "message polling.",
            "qq_command",
            (
                ("login", "authorize a QQ official bot by scanning a QR URL"),
                ("status", "inspect local QQ authorization without network access"),
                ("logout", "remove only adapter-owned QQ authorization files"),
            ),
        ),
    ):
        platform_command = subcommands.add_parser(
            platform_name,
            help=description,
            description=description,
        )
        platform_command.add_argument(
            "--config",
            metavar="PATH",
            default=argparse.SUPPRESS,
            help=(
                "path to the config file "
                f"(default: ${CONFIG_PATH_ENV} or ~/.kimi-bridge/config.toml)"
            ),
        )
        platform_subcommands = platform_command.add_subparsers(
            dest=command_dest, required=True
        )
        for command_name, help_text in help_texts:
            command = platform_subcommands.add_parser(command_name, help=help_text)
            command.add_argument(
                "--config",
                metavar="PATH",
                default=argparse.SUPPRESS,
                help=(
                    "path to the config file "
                    f"(default: ${CONFIG_PATH_ENV} or ~/.kimi-bridge/config.toml)"
                ),
            )
            if command_name == "login":
                command.add_argument(
                    "--replace",
                    action="store_true",
                    help="replace stored authorization only after a new login succeeds",
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
    if arguments.command == "feishu":
        from .platforms.feishu.auth import (
            FeishuControlError,
            run_login,
            run_logout,
            run_status,
        )

        try:
            config = load_config(config_path)
            if config.platform != "feishu":
                raise FeishuControlError(
                    'selected platform must be "feishu" for Feishu controls'
                )
            if arguments.feishu_command == "login":
                return run_login(config.feishu, replace=arguments.replace)
            if arguments.feishu_command == "status":
                return run_status(config.feishu)
            if arguments.feishu_command == "logout":
                return run_logout(config.feishu)
            raise AssertionError("unhandled Feishu command")
        except KeyboardInterrupt:
            print("kimi-bridge: Feishu authorization cancelled", file=sys.stderr)
            return 130
        except (
            OSError,
            TypeError,
            ValueError,
            FeishuControlError,
            FeishuStorageError,
        ) as exc:
            print(f"kimi-bridge: {exc}", file=sys.stderr)
            return 1
    if arguments.command == "qq":
        from .platforms.qq.auth import (
            QQControlError,
            run_login,
            run_logout,
            run_status,
        )

        try:
            config = load_config(config_path)
            if config.platform != "qq":
                raise QQControlError(
                    'selected platform must be "qq" for QQ controls'
                )
            if arguments.qq_command == "login":
                return run_login(config.qq, replace=arguments.replace)
            if arguments.qq_command == "status":
                return run_status(config.qq)
            if arguments.qq_command == "logout":
                return run_logout(config.qq)
            raise AssertionError("unhandled QQ command")
        except KeyboardInterrupt:
            print("kimi-bridge: QQ authorization cancelled", file=sys.stderr)
            return 130
        except (OSError, TypeError, ValueError, QQControlError) as exc:
            print(f"kimi-bridge: {exc}", file=sys.stderr)
            return 1
    if arguments.command == "wechat":
        from .platforms.wechat import (
            WeChatControlError,
            run_login,
            run_logout,
            run_status,
        )

        try:
            config = load_config(config_path)
            if config.platform != "wechat":
                raise WeChatControlError(
                    'selected platform must be "wechat" for WeChat controls'
                )
            if arguments.wechat_command == "login":
                return run_login(config.wechat, replace=arguments.replace)
            if arguments.wechat_command == "status":
                return run_status(config.wechat)
            if arguments.wechat_command == "logout":
                return run_logout(config.wechat)
            raise AssertionError("unhandled WeChat command")
        except KeyboardInterrupt:
            print("kimi-bridge: WeChat authorization cancelled", file=sys.stderr)
            return 130
        except (OSError, TypeError, ValueError, WeChatControlError) as exc:
            print(f"kimi-bridge: {exc}", file=sys.stderr)
            return 1
    try:
        asyncio.run(run(config_path))
    except KeyboardInterrupt:
        pass
    except (
        AdapterConfigurationError,
        KimiServerError,
        WeChatAuthenticationExpired,
        ValueError,
        TypeError,
    ) as exc:
        print(f"kimi-bridge: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
