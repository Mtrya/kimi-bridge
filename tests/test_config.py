from __future__ import annotations

from pathlib import Path

import pytest

from kimi_bridge.config import (
    Config,
    FeishuConfig,
    KimiServerConfig,
    TelegramConfig,
    load_config,
)


def test_missing_config_uses_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "missing.toml") == Config()


def test_loads_log_level_and_server_port(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'log_level = "debug"\n\n[kimi_server]\nport = 43123\n',
        encoding="utf-8",
    )

    assert load_config(path) == Config(
        log_level="DEBUG", kimi_server=KimiServerConfig(port=43123)
    )


def test_loads_full_runtime_schema_without_exposing_secret(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                f'default_workspace = "{workspace}"',
                "edit_throttle_seconds = 2.25",
                "interaction_timeout_seconds = 42",
                'inbox_subdir = ".bridge-files"',
                "",
                "[feishu]",
                'app_id = "cli_test"',
                'app_secret = "secret-value"',
                'allowed_users = ["ou_one", "user_two"]',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.default_workspace == workspace
    assert config.edit_throttle_seconds == 2.25
    assert config.interaction_timeout_seconds == 42
    assert config.inbox_subdir == ".bridge-files"
    assert config.feishu == FeishuConfig(
        app_id="cli_test",
        app_secret="secret-value",
        allowed_users=frozenset({"ou_one", "user_two"}),
    )
    assert "secret-value" not in repr(config)


def test_loads_telegram_and_ignores_partial_unselected_feishu(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                'platform = "telegram"',
                "",
                "[feishu]",
                'app_id = "unused"',
                "",
                "[telegram]",
                'bot_token = "123456:secret-token"',
                "allowed_users = [123456789, 987654321]",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.platform == "telegram"
    assert config.telegram == TelegramConfig(
        bot_token="123456:secret-token",
        allowed_users=frozenset({123456789, 987654321}),
    )
    assert config.feishu.app_id == "unused"
    assert "123456:secret-token" not in repr(config)


def test_rejects_unknown_platform(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('platform = "auto"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="platform must be one of"):
        load_config(path)


@pytest.mark.parametrize(
    "allowed_users",
    ['["123"]', "[0]", "[-1]", "[true]"],
)
def test_rejects_non_positive_or_non_numeric_telegram_users(
    tmp_path: Path, allowed_users: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                'platform = "telegram"',
                "[telegram]",
                'bot_token = "token"',
                f"allowed_users = {allowed_users}",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="positive integers"):
        load_config(path)


def test_rejects_partial_feishu_credentials(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[feishu]\napp_id = "cli_test"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="must be set together"):
        load_config(path)


def test_rejects_inbox_path_that_escapes_workspace(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('inbox_subdir = "../outside"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="inside the session workspace"):
        load_config(path)


@pytest.mark.parametrize("port", [0, 65536])
def test_rejects_out_of_range_server_port(tmp_path: Path, port: int) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[kimi_server]\nport = {port}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="between 1 and 65535"):
        load_config(path)
