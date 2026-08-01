from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from kimi_bridge.config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    Config,
    FeishuConfig,
    KimiServerConfig,
    QQConfig,
    TelegramConfig,
    VoiceAsrConfig,
    load_config,
    resolve_config_path,
    unknown_config_keys,
)


def test_unknown_config_keys_accepts_generic_mappings() -> None:
    raw = MappingProxyType(
        {
            "platfrom": "feishu",
            "feishu": MappingProxyType({"app_id": "id", "ap_id": "typo"}),
            "kimi_server": "not-a-table",
        }
    )

    assert unknown_config_keys(raw) == ("feishu.ap_id", "platfrom")


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
                f"default_workspace = '{workspace}'",
                "edit_throttle_seconds = 2.25",
                "max_output_seconds = 180",
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
    assert config.max_output_seconds == 180
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


def test_loads_qq_and_ignores_partial_unselected_feishu(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                'platform = "qq"',
                "",
                "[feishu]",
                'app_id = "unused"',
                "",
                "[qq]",
                'app_id = "app-1"',
                'app_secret = "secret-1"',
                'allowed_users = ["OPENID-1", "OPENID-2"]',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.platform == "qq"
    assert config.qq == QQConfig(
        app_id="app-1",
        app_secret="secret-1",
        allowed_users=frozenset({"OPENID-1", "OPENID-2"}),
    )
    assert config.feishu.app_id == "unused"
    assert "secret-1" not in repr(config)


def test_rejects_partial_qq_credentials(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'platform = "qq"\n[qq]\napp_id = "app-1"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="must be set together"):
        load_config(path)


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


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("true", TypeError),
        ('"300"', TypeError),
        ("0", ValueError),
        ("-1", ValueError),
        ("nan", ValueError),
        ("inf", ValueError),
        ("-inf", ValueError),
    ],
)
def test_rejects_invalid_max_output_seconds(
    tmp_path: Path, value: str, error: type[Exception]
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"max_output_seconds = {value}\n", encoding="utf-8")

    with pytest.raises(error, match="max_output_seconds"):
        load_config(path)


def test_rejects_infeasible_feishu_edit_budget(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "edit_throttle_seconds = 2\nmax_output_seconds = 91.9\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="at least 46 times"):
        load_config(path)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_rejects_non_finite_edit_throttle_seconds(
    tmp_path: Path, value: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"edit_throttle_seconds = {value}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="edit_throttle_seconds"):
        load_config(path)


@pytest.mark.parametrize("port", [0, 65536])
def test_rejects_out_of_range_server_port(tmp_path: Path, port: int) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[kimi_server]\nport = {port}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="between 1 and 65535"):
        load_config(path)


def test_loads_custom_state_path(tmp_path: Path) -> None:
    state = tmp_path / "custom" / "state.json"
    path = tmp_path / "config.toml"
    path.write_text(f"state_path = '{state}'\n", encoding="utf-8")

    assert load_config(path).state_path == state


def test_rejects_blank_state_path(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('state_path = "   "\n', encoding="utf-8")

    with pytest.raises(TypeError, match="state_path"):
        load_config(path)


def test_resolve_config_path_prefers_explicit_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_PATH_ENV, str(tmp_path / "env.toml"))

    assert resolve_config_path(tmp_path / "cli.toml") == tmp_path / "cli.toml"


def test_resolve_config_path_uses_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_PATH_ENV, str(tmp_path / "env.toml"))

    assert resolve_config_path() == tmp_path / "env.toml"


def test_resolve_config_path_defaults_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)

    assert resolve_config_path() == DEFAULT_CONFIG_PATH


def test_loads_session_list_limit(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("session_list_limit = 25\n", encoding="utf-8")

    assert load_config(path).session_list_limit == 25


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("0", ValueError),
        ("-3", ValueError),
        ("1.5", TypeError),
        ('"many"', TypeError),
        ("true", TypeError),
    ],
)
def test_rejects_invalid_session_list_limit(
    tmp_path: Path, value: str, error: type[Exception]
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"session_list_limit = {value}\n", encoding="utf-8")

    with pytest.raises(error, match="session_list_limit"):
        load_config(path)


def test_loads_voice_asr_and_hides_api_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[voice.asr]",
                'base_url = "https://asr.example/v1"',
                'api_key = "sk-secret"',
                'model = "whisper-1"',
                'request_format = "json"',
                'language = "en"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.voice.asr == VoiceAsrConfig(
        base_url="https://asr.example/v1",
        model="whisper-1",
        api_key="sk-secret",
        request_format="json",
        language="en",
    )
    assert "sk-secret" not in repr(config)


def test_voice_asr_api_key_is_optional(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[voice.asr]\n"
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'model = "whisper-1"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.voice.asr == VoiceAsrConfig(
        base_url="http://127.0.0.1:8080/v1",
        model="whisper-1",
    )


def test_voice_asr_api_key_can_come_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_ASR_KEY", "secret-from-env")
    path = tmp_path / "config.toml"
    path.write_text(
        "[voice.asr]\n"
        'base_url = "https://asr.example/v1"\n'
        'model = "asr-model"\n'
        'api_key_env = "TEST_ASR_KEY"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.voice.asr == VoiceAsrConfig(
        base_url="https://asr.example/v1",
        model="asr-model",
        api_key="secret-from-env",
    )
    assert "secret-from-env" not in repr(config)


@pytest.mark.parametrize(
    ("api_key_lines", "error"),
    [
        (
            'api_key = "inline"\napi_key_env = "TEST_ASR_KEY"',
            "mutually exclusive",
        ),
        ('api_key_env = "MISSING_ASR_KEY"', "unset or empty"),
    ],
)
def test_rejects_invalid_voice_asr_api_key_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_key_lines: str,
    error: str,
) -> None:
    monkeypatch.delenv("MISSING_ASR_KEY", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        "[voice.asr]\n"
        'base_url = "https://asr.example/v1"\n'
        'model = "asr-model"\n'
        f"{api_key_lines}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=error):
        load_config(path)


def test_absent_voice_table_defaults_to_no_asr(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('platform = "qq"\n', encoding="utf-8")

    assert load_config(path).voice.asr is None


@pytest.mark.parametrize(
    "table",
    [
        'api_key = "sk-secret"\nmodel = "whisper-1"',
        'base_url = "https://asr.example/v1"\napi_key = "sk-secret"',
        'base_url = "  "\nmodel = "whisper-1"',
    ],
)
def test_rejects_incomplete_voice_asr(tmp_path: Path, table: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[voice.asr]\n{table}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="voice.asr"):
        load_config(path)


def test_rejects_non_string_voice_asr_values(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[voice.asr]\nbase_url = 1\nmodel = 2\n", encoding="utf-8"
    )

    with pytest.raises(TypeError, match="voice.asr"):
        load_config(path)


def test_rejects_unknown_voice_asr_request_format(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[voice.asr]\n"
        'base_url = "https://asr.example/v1"\n'
        'model = "asr-model"\n'
        'request_format = "xml"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="request_format"):
        load_config(path)


def test_unknown_config_keys_reports_nested_voice_asr_keys() -> None:
    raw = MappingProxyType(
        {
            "voice": MappingProxyType(
                {
                    "asr": MappingProxyType(
                        {"base_url": "x", "model": "y", "modle": "typo"}
                    ),
                    "aser": "typo",
                }
            )
        }
    )

    assert unknown_config_keys(raw) == ("voice.aser", "voice.asr.modle")
