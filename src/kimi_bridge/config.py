"""Load bridge configuration from TOML."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias, cast


DEFAULT_CONFIG_PATH = Path.home() / ".kimi-bridge" / "config.toml"
DEFAULT_WORKSPACE = Path.home() / ".kimi-bridge" / "workspace"
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
PlatformName: TypeAlias = Literal["feishu", "telegram"]
_PLATFORMS = {"feishu", "telegram"}


@dataclass(frozen=True, slots=True)
class KimiServerConfig:
    """Configuration that affects the bridge-managed kimi server."""

    port: int | None = None


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    """Credentials and authorization policy for the Feishu bot."""

    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    allowed_users: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Credentials and authorization policy for the Telegram bot."""

    bot_token: str = field(default="", repr=False)
    allowed_users: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime configuration for the single-user bridge."""

    platform: PlatformName = "feishu"
    log_level: str = "INFO"
    default_workspace: Path = DEFAULT_WORKSPACE
    edit_throttle_seconds: float = 1.5
    interaction_timeout_seconds: float = 600.0
    inbox_subdir: str = ".kimi-bridge-inbox"
    kimi_server: KimiServerConfig = field(default_factory=KimiServerConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load settings, using safe local defaults when the file is absent.

    The supported schema is::

        platform = "feishu"
        log_level = "INFO"
        default_workspace = "~/.kimi-bridge/workspace"
        edit_throttle_seconds = 1.5
        interaction_timeout_seconds = 600
        inbox_subdir = ".kimi-bridge-inbox"

        [kimi_server]
        port = 58628

        [feishu]
        app_id = "cli_..."
        app_secret = "..."
        allowed_users = ["ou_..."]

        [telegram]
        bot_token = "123456:..."
        allowed_users = [123456789]
    """

    config_path = Path(path).expanduser()
    if not config_path.exists():
        return Config()

    with config_path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    platform = raw.get("platform", "feishu")
    if not isinstance(platform, str) or platform not in _PLATFORMS:
        choices = ", ".join(sorted(_PLATFORMS))
        raise ValueError(f"platform must be one of: {choices}")

    log_level = raw.get("log_level", "INFO")
    if not isinstance(log_level, str):
        raise TypeError("log_level must be a string")
    log_level = log_level.upper()
    if log_level not in _LOG_LEVELS:
        choices = ", ".join(sorted(_LOG_LEVELS))
        raise ValueError(f"log_level must be one of: {choices}")

    workspace_raw = raw.get("default_workspace", str(DEFAULT_WORKSPACE))
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        raise TypeError("default_workspace must be a non-empty string")
    default_workspace = Path(workspace_raw).expanduser().resolve()

    throttle = raw.get("edit_throttle_seconds", 1.5)
    if isinstance(throttle, bool) or not isinstance(throttle, (int, float)):
        raise TypeError("edit_throttle_seconds must be a number")
    edit_throttle_seconds = float(throttle)
    if edit_throttle_seconds <= 0:
        raise ValueError("edit_throttle_seconds must be positive")

    interaction_timeout = raw.get("interaction_timeout_seconds", 600.0)
    if isinstance(interaction_timeout, bool) or not isinstance(
        interaction_timeout, (int, float)
    ):
        raise TypeError("interaction_timeout_seconds must be a number")
    interaction_timeout_seconds = float(interaction_timeout)
    if interaction_timeout_seconds <= 0:
        raise ValueError("interaction_timeout_seconds must be positive")

    inbox_subdir = raw.get("inbox_subdir", ".kimi-bridge-inbox")
    if not isinstance(inbox_subdir, str) or not inbox_subdir.strip():
        raise TypeError("inbox_subdir must be a non-empty string")
    inbox_path = Path(inbox_subdir)
    if inbox_path.is_absolute() or ".." in inbox_path.parts:
        raise ValueError("inbox_subdir must stay inside the session workspace")

    server_raw = raw.get("kimi_server", {})
    if not isinstance(server_raw, dict):
        raise TypeError("kimi_server must be a TOML table")
    port = server_raw.get("port")
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int):
            raise TypeError("kimi_server.port must be an integer")
        if not 1 <= port <= 65535:
            raise ValueError("kimi_server.port must be between 1 and 65535")

    feishu_raw = raw.get("feishu", {})
    if not isinstance(feishu_raw, dict):
        raise TypeError("feishu must be a TOML table")
    app_id = feishu_raw.get("app_id", "")
    app_secret = feishu_raw.get("app_secret", "")
    if not isinstance(app_id, str) or not isinstance(app_secret, str):
        raise TypeError("feishu.app_id and feishu.app_secret must be strings")
    if platform == "feishu" and bool(app_id) != bool(app_secret):
        raise ValueError("feishu.app_id and feishu.app_secret must be set together")

    allowed_raw = feishu_raw.get("allowed_users", [])
    if not isinstance(allowed_raw, list):
        raise TypeError("feishu.allowed_users must be an array of strings")
    if any(not isinstance(user, str) or not user.strip() for user in allowed_raw):
        raise TypeError("feishu.allowed_users must contain non-empty strings")
    allowed_users = frozenset(allowed_raw)

    telegram_raw = raw.get("telegram", {})
    if not isinstance(telegram_raw, dict):
        raise TypeError("telegram must be a TOML table")
    bot_token = telegram_raw.get("bot_token", "")
    if not isinstance(bot_token, str):
        raise TypeError("telegram.bot_token must be a string")

    telegram_allowed_raw = telegram_raw.get("allowed_users", [])
    if not isinstance(telegram_allowed_raw, list):
        raise TypeError("telegram.allowed_users must be an array of integers")
    if any(
        isinstance(user, bool) or not isinstance(user, int) or user <= 0
        for user in telegram_allowed_raw
    ):
        raise TypeError("telegram.allowed_users must contain positive integers")
    telegram_allowed_users = frozenset(telegram_allowed_raw)

    logging.getLogger(__name__).debug("Loaded configuration from %s", config_path)
    return Config(
        platform=cast(PlatformName, platform),
        log_level=log_level,
        default_workspace=default_workspace,
        edit_throttle_seconds=edit_throttle_seconds,
        interaction_timeout_seconds=interaction_timeout_seconds,
        inbox_subdir=inbox_subdir,
        kimi_server=KimiServerConfig(port=port),
        feishu=FeishuConfig(
            app_id=app_id,
            app_secret=app_secret,
            allowed_users=allowed_users,
        ),
        telegram=TelegramConfig(
            bot_token=bot_token,
            allowed_users=telegram_allowed_users,
        ),
    )
