"""Load bridge configuration from TOML."""

from __future__ import annotations

import logging
import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias, cast

from .edit_budget import minimum_output_seconds
from .platforms.feishu import FEISHU_MESSAGE_EDIT_LIMIT
from .state import DEFAULT_STATE_PATH


DEFAULT_CONFIG_PATH = Path.home() / ".kimi-bridge" / "config.toml"
CONFIG_PATH_ENV = "KIMI_BRIDGE_CONFIG"
DEFAULT_WORKSPACE = Path.home() / ".kimi-bridge" / "workspace"
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
PlatformName: TypeAlias = Literal["feishu", "telegram", "qq"]
_PLATFORMS = {"feishu", "telegram", "qq"}
_KNOWN_SUB_KEYS = {
    "kimi_server": frozenset({"port"}),
    "feishu": frozenset({"app_id", "app_secret", "allowed_users"}),
    "telegram": frozenset({"bot_token", "allowed_users"}),
    "qq": frozenset({"app_id", "app_secret", "allowed_users"}),
    "voice": frozenset({"asr"}),
}
_KNOWN_VOICE_ASR_KEYS = frozenset({"base_url", "api_key", "model"})
_KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "platform",
        "log_level",
        "default_workspace",
        "state_path",
        "edit_throttle_seconds",
        "max_output_seconds",
        "interaction_timeout_seconds",
        "inbox_subdir",
        "session_list_limit",
        *_KNOWN_SUB_KEYS,
    }
)


def unknown_config_keys(raw: Mapping[str, object]) -> tuple[str, ...]:
    """Return dotted names of config keys the loader silently ignores."""

    unknown: list[str] = []
    for key, value in raw.items():
        if key not in _KNOWN_TOP_LEVEL_KEYS:
            unknown.append(key)
            continue
        sub_keys = _KNOWN_SUB_KEYS.get(key)
        if sub_keys is None or not isinstance(value, Mapping):
            continue
        unknown.extend(f"{key}.{sub}" for sub in value if sub not in sub_keys)
        if key == "voice":
            asr_raw = value.get("asr")
            if isinstance(asr_raw, Mapping):
                unknown.extend(
                    f"voice.asr.{sub}"
                    for sub in asr_raw
                    if sub not in _KNOWN_VOICE_ASR_KEYS
                )
    return tuple(sorted(unknown))


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the config file: explicit path, then env override, then default."""

    if explicit is not None:
        return Path(explicit).expanduser()
    override = os.environ.get(CONFIG_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


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
class QQConfig:
    """Credentials and authorization policy for the QQ official bot."""

    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    allowed_users: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class VoiceAsrConfig:
    """External Whisper-compatible transcription endpoint for voice messages.

    ``base_url`` and ``model`` are required; ``api_key`` may stay empty for
    local servers that do not check a Bearer token.
    """

    base_url: str
    model: str
    api_key: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """Voice-message handling; ``asr`` is None when no external ASR is set."""

    asr: VoiceAsrConfig | None = None


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime configuration for the single-user bridge."""

    platform: PlatformName = "feishu"
    log_level: str = "INFO"
    default_workspace: Path = DEFAULT_WORKSPACE
    state_path: Path = DEFAULT_STATE_PATH
    edit_throttle_seconds: float = 1.5
    max_output_seconds: float = 300.0
    interaction_timeout_seconds: float = 600.0
    inbox_subdir: str = ".kimi-bridge-inbox"
    session_list_limit: int = 10
    kimi_server: KimiServerConfig = field(default_factory=KimiServerConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    qq: QQConfig = field(default_factory=QQConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load settings, using safe local defaults when the file is absent.

    The supported schema is::

        platform = "feishu"
        log_level = "INFO"
        default_workspace = "~/.kimi-bridge/workspace"
        state_path = "~/.kimi-bridge/state.json"
        edit_throttle_seconds = 1.5
        max_output_seconds = 300
        interaction_timeout_seconds = 600
        inbox_subdir = ".kimi-bridge-inbox"
        session_list_limit = 10

        [kimi_server]
        port = 58628

        [feishu]
        app_id = "cli_..."
        app_secret = "..."
        allowed_users = ["ou_..."]

        [telegram]
        bot_token = "123456:..."
        allowed_users = [123456789]

        [qq]
        app_id = "..."
        app_secret = "..."
        allowed_users = ["..."]

        [voice.asr]
        base_url = "https://api.openai.com/v1"
        api_key = "sk-..."
        model = "whisper-1"
    """

    config_path = Path(path).expanduser()
    if not config_path.exists():
        return Config()

    with config_path.open("rb") as config_file:
        raw = tomllib.load(config_file)
    logging.getLogger(__name__).debug("Loaded configuration from %s", config_path)
    return _load_config_from_raw(raw)


def _load_config_from_raw(raw: Mapping[str, object]) -> Config:
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

    state_raw = raw.get("state_path", str(DEFAULT_STATE_PATH))
    if not isinstance(state_raw, str) or not state_raw.strip():
        raise TypeError("state_path must be a non-empty string")
    state_path = Path(state_raw).expanduser().resolve()

    throttle = raw.get("edit_throttle_seconds", 1.5)
    if isinstance(throttle, bool) or not isinstance(throttle, (int, float)):
        raise TypeError("edit_throttle_seconds must be a number")
    edit_throttle_seconds = float(throttle)
    if not math.isfinite(edit_throttle_seconds) or edit_throttle_seconds <= 0:
        raise ValueError("edit_throttle_seconds must be positive and finite")

    max_output = raw.get("max_output_seconds", 300.0)
    if isinstance(max_output, bool) or not isinstance(max_output, (int, float)):
        raise TypeError("max_output_seconds must be a number")
    max_output_seconds = float(max_output)
    if not math.isfinite(max_output_seconds) or max_output_seconds <= 0:
        raise ValueError("max_output_seconds must be positive and finite")
    minimum_output = minimum_output_seconds(
        edit_throttle_seconds,
        FEISHU_MESSAGE_EDIT_LIMIT,
    )
    if max_output_seconds < minimum_output:
        raise ValueError(
            "max_output_seconds must be at least 46 times "
            "edit_throttle_seconds for Feishu's 20-edit budget"
        )

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

    list_limit = raw.get("session_list_limit", 10)
    if isinstance(list_limit, bool) or not isinstance(list_limit, int):
        raise TypeError("session_list_limit must be an integer")
    if list_limit <= 0:
        raise ValueError("session_list_limit must be positive")
    session_list_limit = list_limit

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

    qq_raw = raw.get("qq", {})
    if not isinstance(qq_raw, dict):
        raise TypeError("qq must be a TOML table")
    qq_app_id = qq_raw.get("app_id", "")
    qq_app_secret = qq_raw.get("app_secret", "")
    if not isinstance(qq_app_id, str) or not isinstance(qq_app_secret, str):
        raise TypeError("qq.app_id and qq.app_secret must be strings")
    if platform == "qq" and bool(qq_app_id) != bool(qq_app_secret):
        raise ValueError("qq.app_id and qq.app_secret must be set together")

    qq_allowed_raw = qq_raw.get("allowed_users", [])
    if not isinstance(qq_allowed_raw, list):
        raise TypeError("qq.allowed_users must be an array of strings")
    if any(not isinstance(user, str) or not user.strip() for user in qq_allowed_raw):
        raise TypeError("qq.allowed_users must contain non-empty strings")
    qq_allowed_users = frozenset(qq_allowed_raw)

    voice_raw = raw.get("voice", {})
    if not isinstance(voice_raw, dict):
        raise TypeError("voice must be a TOML table")
    voice_asr: VoiceAsrConfig | None = None
    asr_raw = voice_raw.get("asr")
    if asr_raw is not None:
        if not isinstance(asr_raw, dict):
            raise TypeError("voice.asr must be a TOML table")
        asr_base_url = asr_raw.get("base_url", "")
        asr_api_key = asr_raw.get("api_key", "")
        asr_model = asr_raw.get("model", "")
        if not all(
            isinstance(value, str)
            for value in (asr_base_url, asr_api_key, asr_model)
        ):
            raise TypeError(
                "voice.asr.base_url, voice.asr.api_key, and voice.asr.model "
                "must be strings"
            )
        if not asr_base_url.strip() or not asr_model.strip():
            raise ValueError(
                "voice.asr.base_url and voice.asr.model must be non-empty; "
                "voice.asr.api_key is optional for local servers"
            )
        voice_asr = VoiceAsrConfig(
            base_url=asr_base_url,
            model=asr_model,
            api_key=asr_api_key,
        )

    return Config(
        platform=cast(PlatformName, platform),
        log_level=log_level,
        default_workspace=default_workspace,
        state_path=state_path,
        edit_throttle_seconds=edit_throttle_seconds,
        max_output_seconds=max_output_seconds,
        interaction_timeout_seconds=interaction_timeout_seconds,
        inbox_subdir=inbox_subdir,
        session_list_limit=session_list_limit,
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
        qq=QQConfig(
            app_id=qq_app_id,
            app_secret=qq_app_secret,
            allowed_users=qq_allowed_users,
        ),
        voice=VoiceConfig(asr=voice_asr),
    )
