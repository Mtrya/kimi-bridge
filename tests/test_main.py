from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any

import pytest

from kimi_bridge import __main__ as main_module
from kimi_bridge import doctor as doctor_module
from kimi_bridge.compatibility import COMPATIBILITY_MAP, kimi_code_version_sort_key
from kimi_bridge.config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    Config,
    FeishuConfig,
    QQConfig,
    TelegramConfig,
    WechatConfig,
)
from kimi_bridge.kimi_server import KimiServerAuthenticationError


class _Adapter:
    name = "fake"
    message_limit = 1


def test_runtime_suppresses_credential_bearing_library_debug_logs() -> None:
    logger_names = main_module._CREDENTIAL_BEARING_LIBRARY_LOGGERS
    original_levels = {
        logger_name: logging.getLogger(logger_name).level
        for logger_name in logger_names
    }
    try:
        for logger_name in logger_names:
            logging.getLogger(logger_name).setLevel(logging.DEBUG)

        main_module._configure_logging("DEBUG")

        assert all(
            logging.getLogger(logger_name).getEffectiveLevel()
            >= logging.WARNING
            for logger_name in logger_names
        )
    finally:
        for logger_name, level in original_levels.items():
            logging.getLogger(logger_name).setLevel(level)


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


def test_builds_feishu_adapter_with_resolved_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, Any] = {}

    class FakeAdapter(_Adapter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            received["args"] = args
            received["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "FeishuAdapter", FakeAdapter)
    monkeypatch.setattr(main_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    config = Config(
        platform="feishu",
        feishu=FeishuConfig(
            app_id="app-1",
            app_secret="secret-1",
            allowed_users=frozenset({"ou_one"}),
        ),
    )

    adapter = main_module._build_adapter(config)

    assert isinstance(adapter, FakeAdapter)
    assert received == {
        "args": ("app-1", "secret-1", frozenset({"ou_one"})),
        "kwargs": {"ffmpeg_executable": "/usr/bin/ffmpeg"},
    }


def test_selected_feishu_adapter_requires_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="FFmpeg is required"):
        main_module._build_adapter(
            Config(
                platform="feishu",
                feishu=FeishuConfig(
                    app_id="app-1",
                    app_secret="secret-1",
                    allowed_users=frozenset({"ou_one"}),
                ),
            )
        )


def test_builds_qq_adapter_with_wired_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    class FakeAdapter(_Adapter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.append(args)
            self.kwargs = kwargs

    monkeypatch.setattr(main_module, "QQAdapter", FakeAdapter)
    config = Config(
        platform="qq",
        qq=QQConfig(
            app_id="app-1", app_secret="secret-1", allowed_users=frozenset({"O1"})
        ),
    )

    adapter = main_module._build_adapter(config)

    assert isinstance(adapter, FakeAdapter)
    assert calls == [("app-1", frozenset({"O1"}))]
    assert isinstance(adapter.kwargs["api"], main_module.QQBotAPI)
    assert isinstance(adapter.kwargs["gateway"], main_module.QQGatewayClient)
    assert isinstance(
        adapter.kwargs["token_manager"], main_module.QQTokenManager
    )


def test_selected_platform_requires_its_own_credentials(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Telegram bot token"):
        main_module._build_adapter(Config(platform="telegram"))

    with pytest.raises(RuntimeError, match="Feishu credentials"):
        main_module._build_adapter(Config(platform="feishu"))

    with pytest.raises(RuntimeError, match="QQ credentials"):
        main_module._build_adapter(Config(platform="qq"))

    with pytest.raises(RuntimeError, match="qq.allowed_users"):
        main_module._build_adapter(
            Config(
                platform="qq",
                qq=QQConfig(app_id="app-1", app_secret="secret-1"),
            )
        )

    with pytest.raises(RuntimeError, match="wechat.allowed_users"):
        main_module._build_adapter(Config(platform="wechat"))

    with pytest.raises(RuntimeError, match="local authorization/state"):
        main_module._build_adapter(
            Config(
                platform="wechat",
                wechat=WechatConfig(
                    allowed_users=frozenset({"user-one"}),
                    storage_path=tmp_path / "empty-wechat",
                ),
            )
        )


def test_builds_selected_wechat_adapter_from_private_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kimi_bridge.platforms import wechat as wechat_module

    storage_path = tmp_path / "wechat"
    storage = wechat_module.WeChatStorage(storage_path)
    credential = wechat_module.WeChatCredential(
        bot_token="WECHAT_TOKEN_SECRET",
        bot_id="bot-one@im.bot",
        base_url="https://ilinkai.weixin.qq.com",
        authorized_at="2026-08-08T12:00:00+00:00",
    )
    storage.save_credential(credential)
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    api = object()

    def api_factory(received: object) -> object:
        calls.append(("api", (received,), {}))
        return api

    def adapter_factory(*args: Any, **kwargs: Any) -> _Adapter:
        calls.append(("adapter", args, kwargs))
        return _Adapter()

    def forbidden_factory(*_args: Any, **_kwargs: Any) -> _Adapter:
        raise AssertionError("unselected adapter was constructed")

    monkeypatch.setattr(wechat_module, "WeChatAPI", api_factory)
    monkeypatch.setattr(wechat_module, "WeChatAdapter", adapter_factory)
    monkeypatch.setattr(main_module, "TelegramAdapter", forbidden_factory)
    monkeypatch.setattr(main_module, "FeishuAdapter", forbidden_factory)
    monkeypatch.setattr(main_module, "QQAdapter", forbidden_factory)

    adapter = main_module._build_adapter(
        Config(
            platform="wechat",
            wechat=WechatConfig(
                allowed_users=frozenset({"user-one"}),
                storage_path=storage_path,
            ),
        )
    )

    assert isinstance(adapter, _Adapter)
    assert calls[0] == ("api", (credential,), {})
    assert calls[1][0:2] == (
        "adapter",
        (credential.bot_id, frozenset({"user-one"})),
    )
    assert calls[1][2]["api"] is api
    assert isinstance(calls[1][2]["storage"], wechat_module.WeChatStorage)
    assert calls[1][2]["runtime_state"] == wechat_module.WeChatRuntimeState()


def test_selected_wechat_requires_media_dependency_before_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kimi_bridge.platforms import wechat as wechat_module

    def missing_dependency() -> None:
        raise wechat_module.WeChatMediaDependencyError(
            "reinstall kimi-bridge"
        )

    monkeypatch.setattr(
        wechat_module,
        "require_wechat_media_dependency",
        missing_dependency,
    )

    with pytest.raises(RuntimeError, match="reinstall kimi-bridge"):
        main_module._build_adapter(
            Config(
                platform="wechat",
                wechat=WechatConfig(
                    allowed_users=frozenset({"user-one"}),
                    storage_path=tmp_path / "wechat",
                ),
            )
        )


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
    received: dict[str, object] = {}

    async def forbidden_run(_config_path: object) -> None:
        nonlocal runtime_started
        runtime_started = True

    def forbidden_adapter(_config: Config) -> _Adapter:
        nonlocal adapter_built
        adapter_built = True
        return _Adapter()

    def fake_doctor(*, config_path: object = None) -> int:
        nonlocal doctor_called
        doctor_called = True
        received["config_path"] = config_path
        return 1

    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
    monkeypatch.setattr(main_module, "run", forbidden_run)
    monkeypatch.setattr(main_module, "_build_adapter", forbidden_adapter)
    monkeypatch.setattr(doctor_module, "run_doctor", fake_doctor)

    assert main_module.main(["doctor"]) == 1
    assert doctor_called
    assert received["config_path"] == DEFAULT_CONFIG_PATH
    assert not runtime_started
    assert not adapter_built


def test_config_argument_reaches_doctor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received: list[object] = []

    def fake_doctor(*, config_path: object = None) -> int:
        received.append(config_path)
        return 0

    monkeypatch.setattr(doctor_module, "run_doctor", fake_doctor)

    custom = tmp_path / "custom.toml"
    assert main_module.main(["--config", str(custom), "doctor"]) == 0
    assert main_module.main(["doctor", "--config", str(custom)]) == 0
    assert received == [custom, custom]


def test_config_argument_reaches_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received: list[object] = []

    async def fake_run(config_path: object) -> None:
        received.append(config_path)

    monkeypatch.setattr(main_module, "run", fake_run)

    custom = tmp_path / "custom.toml"
    assert main_module.main(["--config", str(custom)]) == 0
    assert received == [custom]


def test_wechat_controls_do_not_start_runtime_or_build_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kimi_bridge.platforms import wechat as wechat_module

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'platform = "wechat"\n[wechat]\nallowed_users = []\n',
        encoding="utf-8",
    )
    runtime_started = False
    adapter_built = False
    calls: list[tuple[str, bool]] = []

    async def forbidden_run(_config_path: object) -> None:
        nonlocal runtime_started
        runtime_started = True

    def forbidden_adapter(_config: Config) -> _Adapter:
        nonlocal adapter_built
        adapter_built = True
        return _Adapter()

    def fake_login(_config: WechatConfig, *, replace: bool = False) -> int:
        calls.append(("login", replace))
        return 0

    def fake_status(_config: WechatConfig) -> int:
        calls.append(("status", False))
        return 0

    def fake_logout(_config: WechatConfig) -> int:
        calls.append(("logout", False))
        return 0

    monkeypatch.setattr(main_module, "run", forbidden_run)
    monkeypatch.setattr(main_module, "_build_adapter", forbidden_adapter)
    monkeypatch.setattr(wechat_module, "run_login", fake_login)
    monkeypatch.setattr(wechat_module, "run_status", fake_status)
    monkeypatch.setattr(wechat_module, "run_logout", fake_logout)

    assert (
        main_module.main(
            ["--config", str(config_path), "wechat", "login", "--replace"]
        )
        == 0
    )
    assert main_module.main(["wechat", "--config", str(config_path), "status"]) == 0
    assert main_module.main(["wechat", "logout", "--config", str(config_path)]) == 0
    assert calls == [("login", True), ("status", False), ("logout", False)]
    assert not runtime_started
    assert not adapter_built


def test_wechat_controls_require_selected_wechat_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kimi_bridge.platforms import wechat as wechat_module

    called = False

    def forbidden_status(_config: WechatConfig) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(wechat_module, "run_status", forbidden_status)
    config_path = tmp_path / "config.toml"
    config_path.write_text('platform = "qq"\n', encoding="utf-8")

    assert main_module.main(["--config", str(config_path), "wechat", "status"]) == 1
    assert not called
    assert "selected platform must be" in capsys.readouterr().err


def test_startup_authentication_failure_is_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing_run(_config_path: object) -> None:
        raise KimiServerAuthenticationError(
            "kimi-code is not authenticated; authenticate via /login"
        )

    monkeypatch.setattr(main_module, "run", failing_run)

    assert main_module.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip().splitlines() == [
        "kimi-bridge: kimi-code is not authenticated; authenticate via /login"
    ]


def test_stale_wechat_authorization_is_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing_run(_config_path: object) -> None:
        raise main_module.WeChatAuthenticationExpired("getUpdates")

    monkeypatch.setattr(main_module, "run", failing_run)

    assert main_module.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip().splitlines() == [
        "kimi-bridge: getUpdates reported expired WeChat authorization; run "
        "kimi-bridge wechat login --replace"
    ]


def test_startup_state_error_is_rendered_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing_run(_config_path: object) -> None:
        raise ValueError("unsupported bridge state format")

    monkeypatch.setattr(main_module, "run", failing_run)

    assert main_module.main([]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "kimi-bridge: unsupported bridge state format"
    assert "Traceback" not in captured.err


def test_startup_adapter_configuration_error_is_rendered_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing_run(_config_path: object) -> None:
        raise main_module.AdapterConfigurationError(
            "telegram.allowed_users must contain at least one user"
        )

    monkeypatch.setattr(main_module, "run", failing_run)

    assert main_module.main([]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == (
        "kimi-bridge: telegram.allowed_users must contain at least one user"
    )
    assert "Traceback" not in captured.err


def test_unexpected_runtime_error_keeps_its_traceback_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_run(_config_path: object) -> None:
        raise RuntimeError("unexpected defect")

    monkeypatch.setattr(main_module, "run", failing_run)

    with pytest.raises(RuntimeError, match="unexpected defect"):
        main_module.main([])


def test_startup_keyboard_interrupt_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def interrupted_run(_config_path: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module, "run", interrupted_run)

    assert main_module.main([]) == 0


def test_compat_supports_a_listed_kimi_code_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    version = main_module.__version__
    current = main_module._current_map_entry()
    kimi_code = current.kimi_code[-1]
    assert main_module.main(["compat", "--kimi-code", kimi_code]) == 0
    output = capsys.readouterr().out
    assert (
        f"kimi-bridge {version} tested Kimi Code versions: "
        f"{', '.join(current.kimi_code)}"
        in output
    )
    assert f"kimi-code {kimi_code} is supported by kimi-bridge {version}" in output


def test_compat_rejects_untested_versions_in_both_directions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main_module.main(["compat", "--kimi-code", "9999.0.0"]) == 1
    assert "untested: newer than every" in capsys.readouterr().out

    assert main_module.main(["compat", "--kimi-code", "0.0.1"]) == 1
    assert "untested: older than every" in capsys.readouterr().out


def test_compat_rejects_an_untested_version_inside_the_tested_range(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tested = sorted(
        {version for entry in COMPATIBILITY_MAP for version in entry.kimi_code},
        key=kimi_code_version_sort_key,
    )
    assert main_module.main(["compat", "--kimi-code", "0.28.2"]) == 1
    assert (
        "untested: not tested by any kimi-bridge release despite falling "
        f"inside the tested range {tested[0]}–{tested[-1]}"
    ) in capsys.readouterr().out


def test_compat_rejects_a_malformed_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main_module.main(["compat", "--kimi-code", "not-a-version"]) == 1
    captured = capsys.readouterr()
    assert "malformed Kimi Code version: not-a-version" in captured.err


def test_compat_prints_the_map_when_kimi_is_not_detected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_module, "_probe_kimi_code_version", lambda: None)

    assert main_module.main(["compat"]) == 0
    output = capsys.readouterr().out
    assert "could not detect a Kimi Code version" in output
    for entry in main_module.COMPATIBILITY_MAP:
        assert f"kimi-bridge {entry.bridge}: {', '.join(entry.kimi_code)}" in output


def test_compat_uses_the_detected_kimi_code_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    version = COMPATIBILITY_MAP[-1].kimi_code[-1]
    monkeypatch.setattr(main_module, "_probe_kimi_code_version", lambda: version)

    assert main_module.main(["compat"]) == 0
    assert f"kimi-code {version} is supported" in capsys.readouterr().out


def test_probe_parses_plain_version_output_without_printing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        returncode = 0
        stdout = "0.29.1\n"

    monkeypatch.setattr(main_module.shutil, "which", lambda _name: "/fake/kimi")
    monkeypatch.setattr(
        main_module.subprocess, "run", lambda *args, **kwargs: Completed()
    )

    assert main_module._probe_kimi_code_version() == "0.29.1"


def test_probe_returns_none_without_kimi_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module.shutil, "which", lambda _name: None)

    assert main_module._probe_kimi_code_version() is None
