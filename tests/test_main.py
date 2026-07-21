from __future__ import annotations

from typing import Any

import pytest

from kimi_bridge import __main__ as main_module
from kimi_bridge.config import Config, FeishuConfig, TelegramConfig


class _Adapter:
    name = "fake"
    message_limit = 1


def test_builds_only_selected_telegram_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def telegram_factory(*args: Any) -> _Adapter:
        calls.append(("telegram", args))
        return _Adapter()

    def feishu_factory(*args: Any) -> _Adapter:
        calls.append(("feishu", args))
        return _Adapter()

    monkeypatch.setattr(main_module, "TelegramAdapter", telegram_factory)
    monkeypatch.setattr(main_module, "FeishuAdapter", feishu_factory)
    config = Config(
        platform="telegram",
        feishu=FeishuConfig(app_id="unused"),
        telegram=TelegramConfig(
            bot_token="secret-token", allowed_users=frozenset({123})
        ),
    )

    adapter = main_module._build_adapter(config)

    assert isinstance(adapter, _Adapter)
    assert calls == [("telegram", ("secret-token", frozenset({123})))]


def test_selected_platform_requires_its_own_credentials() -> None:
    with pytest.raises(RuntimeError, match="Telegram bot token"):
        main_module._build_adapter(Config(platform="telegram"))

    with pytest.raises(RuntimeError, match="Feishu credentials"):
        main_module._build_adapter(Config(platform="feishu"))
