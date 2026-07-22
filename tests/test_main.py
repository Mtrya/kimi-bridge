from __future__ import annotations

from typing import Any

import pytest

from kimi_bridge import __main__ as main_module
from kimi_bridge import doctor as doctor_module
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


@pytest.mark.parametrize("argument", ["--help", "--version"])
def test_metadata_flags_do_not_start_runtime(
    argument: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started = False

    async def forbidden_run() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(main_module, "run", forbidden_run)

    with pytest.raises(SystemExit) as caught:
        main_module.main([argument])

    assert caught.value.code == 0
    assert not started
    output = capsys.readouterr().out
    assert "kimi-bridge" in output


def test_doctor_dispatch_does_not_start_runtime_or_build_an_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_started = False
    adapter_built = False
    doctor_called = False

    async def forbidden_run() -> None:
        nonlocal runtime_started
        runtime_started = True

    def forbidden_adapter(_config: Config) -> _Adapter:
        nonlocal adapter_built
        adapter_built = True
        return _Adapter()

    def fake_doctor() -> int:
        nonlocal doctor_called
        doctor_called = True
        return 1

    monkeypatch.setattr(main_module, "run", forbidden_run)
    monkeypatch.setattr(main_module, "_build_adapter", forbidden_adapter)
    monkeypatch.setattr(doctor_module, "run_doctor", fake_doctor)

    assert main_module.main(["doctor"]) == 1
    assert doctor_called
    assert not runtime_started
    assert not adapter_built
