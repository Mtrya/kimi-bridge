from __future__ import annotations

import asyncio
import signal
from typing import Any

import pytest

from kimi_bridge import __main__ as main_module
from kimi_bridge import doctor as doctor_module
from kimi_bridge.config import Config, FeishuConfig, TelegramConfig


class _Adapter:
    name = "fake"
    message_limit = 1


async def test_signal_handlers_prefer_the_event_loop() -> None:
    class LoopRecorder:
        def __init__(self) -> None:
            self.registered: dict[int, Any] = {}

        def add_signal_handler(self, watched_signal: int, callback: Any) -> None:
            self.registered[watched_signal] = callback

    loop = LoopRecorder()
    stop_requested = asyncio.Event()

    main_module._install_shutdown_signal_handlers(loop, stop_requested)  # type: ignore[arg-type]

    assert set(loop.registered) == {signal.SIGINT, signal.SIGTERM}
    loop.registered[signal.SIGTERM]()
    assert stop_requested.is_set()


async def test_signal_handlers_fall_back_when_loop_registration_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_loop = asyncio.get_running_loop()

    class WindowsLikeLoop:
        def add_signal_handler(self, *_args: Any) -> None:
            raise NotImplementedError

        def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
            real_loop.call_soon_threadsafe(callback, *args)

    registered: dict[int, Any] = {}
    monkeypatch.setattr(
        main_module.signal,
        "signal",
        lambda watched_signal, handler: registered.setdefault(
            watched_signal, handler
        ),
    )
    stop_requested = asyncio.Event()

    main_module._install_shutdown_signal_handlers(
        WindowsLikeLoop(),  # type: ignore[arg-type]
        stop_requested,
    )

    assert set(registered) == {signal.SIGINT, signal.SIGTERM}
    registered[signal.SIGINT](signal.SIGINT, None)
    await asyncio.wait_for(stop_requested.wait(), timeout=1)


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
