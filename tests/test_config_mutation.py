from __future__ import annotations

from pathlib import Path

import pytest

from kimi_bridge.config import load_config
from kimi_bridge.config_mutation import (
    ConfigMutationError,
    merge_allowed_user,
    set_platform,
    update_config_after_login,
)


def test_set_platform_replaces_only_top_level_value(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '# Keep this comment.\nplatform = "feishu" # selected adapter\n\n'
        "[qq]\n"
        'allowed_users = ["OPENID-1"]\n',
        encoding="utf-8",
    )

    set_platform(path, "qq")

    rendered = path.read_text(encoding="utf-8")
    assert "# Keep this comment." in rendered
    assert 'platform = "qq" # selected adapter' in rendered
    assert 'allowed_users = ["OPENID-1"]' in rendered
    assert load_config(path).platform == "qq"


def test_set_platform_inserts_value_into_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('log_level = "INFO"\n', encoding="utf-8")

    set_platform(path, "wechat")

    assert path.read_text(encoding="utf-8") == (
        'platform = "wechat"\nlog_level = "INFO"\n'
    )
    assert load_config(path).platform == "wechat"


def test_set_platform_creates_missing_config(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"

    set_platform(path, "qq")

    assert path.read_text(encoding="utf-8") == 'platform = "qq"\n'
    assert load_config(path).platform == "qq"


def test_merge_allowed_user_preserves_other_toml_and_existing_entries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'platform = "qq"\n'
        "\n"
        "[qq]\n"
        'app_id = "app-id"\n'
        'app_secret = "secret"\n'
        'allowed_users = ["OPENID-1"] # preserve this setting\n'
        "\n"
        "[feishu]\n"
        "# another platform\n"
        'allowed_users = ["ou_one"]\n',
        encoding="utf-8",
    )

    assert merge_allowed_user(path, "qq", "OPENID-2") is True

    raw = load_config(path)
    assert raw.qq.allowed_users == frozenset({"OPENID-1", "OPENID-2"})
    rendered = path.read_text(encoding="utf-8")
    assert 'app_secret = "secret"' in rendered
    assert "# preserve this setting" in rendered
    assert "# another platform" in rendered
    assert 'allowed_users = ["ou_one"]' in rendered

    assert merge_allowed_user(path, "qq", "OPENID-2") is False


def test_merge_allowed_user_handles_multiline_arrays(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'platform = "feishu"\n\n[feishu]\nallowed_users = [\n  "ou_one",\n]\n',
        encoding="utf-8",
    )

    assert merge_allowed_user(path, "feishu", "ou_two") is True
    assert load_config(path).feishu.allowed_users == frozenset({"ou_one", "ou_two"})


def test_merge_allowed_user_creates_missing_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('platform = "qq"\n', encoding="utf-8")

    merge_allowed_user(path, "qq", "OPENID-1")

    assert load_config(path).qq.allowed_users == frozenset({"OPENID-1"})
    assert '[qq]\nallowed_users = ["OPENID-1"]\n' in path.read_text(encoding="utf-8")


def test_merge_allowed_user_creates_missing_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    merge_allowed_user(path, "qq", "OPENID-1")

    assert load_config(path).qq.allowed_users == frozenset({"OPENID-1"})


@pytest.mark.parametrize(
    ("platform", "user_id"),
    [
        ("feishu", "ou_one"),
        ("qq", "OPENID-1"),
        ("wechat", "scanner-user@im.wechat"),
    ],
)
def test_update_config_after_login_creates_selected_platform_and_allowlist(
    tmp_path: Path,
    platform: str,
    user_id: str,
) -> None:
    path = tmp_path / "nested" / "config.toml"

    assert update_config_after_login(path, platform, user_id, create=True) is True

    config = load_config(path)
    assert config.platform == platform
    assert getattr(config, platform).allowed_users == frozenset({user_id})


def test_update_config_after_login_creates_config_without_returned_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"

    assert update_config_after_login(path, "qq", None, create=True) is False

    assert path.read_text(encoding="utf-8") == 'platform = "qq"\n'


def test_update_config_after_login_does_not_overwrite_a_concurrent_config(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    original = 'platform = "feishu"\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigMutationError, match="created while login was running"):
        update_config_after_login(path, "wechat", "scanner", create=True)

    assert path.read_text(encoding="utf-8") == original


def test_mutations_reject_invalid_toml_without_overwriting(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = "platform = [not valid\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigMutationError):
        set_platform(path, "qq")
    with pytest.raises(ConfigMutationError):
        merge_allowed_user(path, "qq", "OPENID-1")

    assert path.read_text(encoding="utf-8") == original
