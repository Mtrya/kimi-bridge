from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from kimi_bridge.compatibility import SUPPORTED_KIMI_CODE_VERSIONS
from kimi_bridge.doctor import (
    CheckStatus,
    CommandResult,
    DoctorReport,
    diagnose,
)


requires_posix_modes = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes are not enforceable here"
)


KIMI_CODE_HELP = """Usage: kimi [options] [command]
The Starting Point for Next-Gen Agents
web [options]  Run the local Kimi server and open the web UI.
doctor  Validate Kimi Code configuration files.
migrate  Migrate data from a legacy kimi-cli installation into kimi-code.
"""

LEGACY_KIMI_CLI_HELP = """Usage: kimi [OPTIONS] COMMAND [ARGS]...
Kimi, your next CLI agent.
--mcp-config-file PATH
Documentation: https://moonshotai.github.io/kimi-cli/
"""
CURRENT_KIMI_CODE_VERSION = next(iter(SUPPORTED_KIMI_CODE_VERSIONS))


class FakeRunner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, command: Sequence[str], *, timeout: float
    ) -> CommandResult:
        assert timeout > 0
        key = tuple(command)
        self.calls.append(key)
        return self.results[key]


def _runner(
    *,
    version: str = f"{CURRENT_KIMI_CODE_VERSION}\n",
    help_output: str = KIMI_CODE_HELP,
    config_result: CommandResult = CommandResult(0, "configuration valid\n"),
) -> FakeRunner:
    return FakeRunner(
        {
            ("/fake/ffmpeg", "-version"): CommandResult(0, "ffmpeg version 8.1\n"),
            ("/fake/kimi", "--version"): CommandResult(0, version),
            ("/fake/kimi", "--help"): CommandResult(0, help_output),
            ("/fake/kimi", "doctor", "config"): config_result,
        }
    )


def _write_feishu_config(path: Path, workspace: Path) -> tuple[str, str, str]:
    app_id = "DO_NOT_PRINT_FEISHU_APP_ID"
    app_secret = "DO_NOT_PRINT_FEISHU_SECRET"
    open_id = "DO_NOT_PRINT_FEISHU_USER"
    path.write_text(
        "\n".join(
            [
                f"default_workspace = '{workspace}'",
                "[feishu]",
                f'app_id = "{app_id}"',
                f'app_secret = "{app_secret}"',
                f'allowed_users = ["{open_id}"]',
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return app_id, app_secret, open_id


def _write_telegram_config(path: Path, workspace: Path) -> tuple[str, str]:
    token = "DO_NOT_PRINT_TELEGRAM_TOKEN"
    user_id = "123456789"
    path.write_text(
        "\n".join(
            [
                'platform = "telegram"',
                f"default_workspace = '{workspace}'",
                "[telegram]",
                f'bot_token = "{token}"',
                f"allowed_users = [{user_id}]",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return token, user_id


def _write_qq_config(path: Path, workspace: Path) -> tuple[str, str, str]:
    app_id = "DO_NOT_PRINT_QQ_APP_ID"
    app_secret = "DO_NOT_PRINT_QQ_SECRET"
    openid = "DO_NOT_PRINT_QQ_OPENID"
    path.write_text(
        "\n".join(
            [
                'platform = "qq"',
                f"default_workspace = '{workspace}'",
                "[qq]",
                f'app_id = "{app_id}"',
                f'app_secret = "{app_secret}"',
                f'allowed_users = ["{openid}"]',
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return app_id, app_secret, openid


def _diagnose(
    config_path: Path,
    state_path: Path,
    runner: FakeRunner,
    *,
    kimi_path: str | None = "/fake/kimi",
    ffmpeg_path: str | None = "/fake/ffmpeg",
    platform_name: str | None = None,
) -> DoctorReport:
    if platform_name is None:
        platform_name = "linux" if os.name == "posix" else "win32"
    return diagnose(
        config_path=config_path,
        state_path=state_path,
        command_runner=runner,
        which=lambda name: ffmpeg_path if name == "ffmpeg" else kimi_path,
        platform_name=platform_name,
    )


def _status(report: DoctorReport, name: str) -> CheckStatus:
    return next(check.status for check in report.checks if check.name == name)


def _detail(report: DoctorReport, name: str) -> str:
    return next(check.detail for check in report.checks if check.name == name)


def test_state_check_uses_configured_state_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    state_path = tmp_path / "custom" / "state.json"
    config_path.write_text(
        "\n".join(
            [
                f"default_workspace = '{tmp_path / 'workspace'}'",
                f"state_path = '{state_path}'",
                "[feishu]",
                'app_id = "id"',
                'app_secret = "secret"',
                'allowed_users = ["ou_one"]',
            ]
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    report = diagnose(
        config_path=config_path,
        command_runner=_runner(),
        which=lambda name: "/fake/ffmpeg" if name == "ffmpeg" else "/fake/kimi",
        platform_name="linux" if os.name == "posix" else "win32",
    )

    assert _status(report, "state") is CheckStatus.OK
    assert str(state_path.parent) in _detail(report, "state")


def test_valid_feishu_config_and_supported_kimi_are_secret_safe(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    secrets = _write_feishu_config(config_path, tmp_path / "workspace")
    runner = _runner()

    report = _diagnose(config_path, tmp_path / "state" / "state.json", runner)
    rendered = report.render()

    assert report.exit_code == 0
    assert all(check.status is CheckStatus.OK for check in report.checks)
    assert all(secret not in rendered for secret in secrets)
    assert runner.calls == [
        ("/fake/ffmpeg", "-version"),
        ("/fake/kimi", "--version"),
        ("/fake/kimi", "--help"),
        ("/fake/kimi", "doctor", "config"),
    ]


def test_valid_telegram_config_and_unknown_kimi_warn_but_pass(
    tmp_path: Path,
    unlisted_kimi_code_version: str,
) -> None:
    config_path = tmp_path / "config.toml"
    secrets = _write_telegram_config(config_path, tmp_path / "workspace")

    report = _diagnose(
        config_path,
        tmp_path / "state.json",
        _runner(version=f"{unlisted_kimi_code_version}\n"),
    )
    rendered = report.render()

    assert report.exit_code == 0
    assert _status(report, "adapter") is CheckStatus.OK
    assert _status(report, "kimi") is CheckStatus.WARNING
    assert f"UNTESTED KIMI CODE VERSION {unlisted_kimi_code_version}" in rendered
    assert all(secret not in rendered for secret in secrets)


def test_valid_qq_config_is_secret_safe(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    secrets = _write_qq_config(config_path, tmp_path / "workspace")
    runner = _runner()

    report = _diagnose(config_path, tmp_path / "state.json", runner)
    rendered = report.render()

    assert report.exit_code == 0
    assert _status(report, "adapter") is CheckStatus.OK
    assert all(secret not in rendered for secret in secrets)
    assert ("/fake/ffmpeg", "-version") not in runner.calls


def test_missing_ffmpeg_is_blocking_only_for_feishu(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_feishu_config(config_path, tmp_path / "workspace")
    runner = _runner()

    report = _diagnose(
        config_path,
        tmp_path / "state.json",
        runner,
        ffmpeg_path=None,
    )

    assert report.exit_code == 1
    assert _status(report, "ffmpeg") is CheckStatus.ERROR
    assert "required for Feishu inbound voice" in _detail(report, "ffmpeg")
    assert ("/fake/ffmpeg", "-version") not in runner.calls


def test_unknown_config_keys_warn_without_failing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                f"default_workspace = '{tmp_path / 'workspace'}'",
                'loglevel = "INFO"',
                "[feishu]",
                'app_id = "id"',
                'app_secret = "secret"',
                'allowed_users = ["ou_one"]',
                'ap_id = "typo"',
            ]
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    report = _diagnose(config_path, tmp_path / "state.json", _runner())
    names = [check.name for check in report.checks]
    config_checks = [check for check in report.checks if check.name == "config"]

    assert report.exit_code == 0
    assert names[:3] == ["config", "config", "config permissions"]
    assert [check.status for check in config_checks] == [
        CheckStatus.OK,
        CheckStatus.WARNING,
    ]
    assert (
        config_checks[1].detail
        == "unknown configuration keys ignored: feishu.ap_id, loglevel"
    )


def test_missing_config_is_blocking_but_kimi_is_still_checked(tmp_path: Path) -> None:
    runner = _runner()

    report = _diagnose(
        tmp_path / "missing.toml", tmp_path / "state.json", runner
    )

    assert report.exit_code == 1
    assert _status(report, "config") is CheckStatus.ERROR
    assert _status(report, "adapter") is CheckStatus.SKIPPED
    assert _status(report, "kimi") is CheckStatus.OK


def test_malformed_toml_does_not_echo_contents(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    secret = "DO_NOT_PRINT_MALFORMED_SECRET"
    config_path.write_text(f'app_secret = "{secret}\n', encoding="utf-8")
    config_path.chmod(0o600)

    report = _diagnose(
        config_path, tmp_path / "state.json", _runner()
    )

    assert report.exit_code == 1
    assert _status(report, "config") is CheckStatus.ERROR
    assert secret not in report.render()


@requires_posix_modes
def test_group_readable_config_warns_without_failing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_feishu_config(config_path, tmp_path / "workspace")
    config_path.chmod(0o640)

    report = _diagnose(
        config_path, tmp_path / "state.json", _runner()
    )

    assert report.exit_code == 0
    assert _status(report, "config permissions") is CheckStatus.WARNING


@requires_posix_modes
def test_macos_group_readable_config_warns_like_linux(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_feishu_config(config_path, tmp_path / "workspace")
    config_path.chmod(0o640)

    report = _diagnose(
        config_path, tmp_path / "state.json", _runner(), platform_name="darwin"
    )

    assert report.exit_code == 0
    assert _status(report, "config permissions") is CheckStatus.WARNING


def test_windows_skips_posix_permission_check(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_feishu_config(config_path, tmp_path / "workspace")

    report = _diagnose(
        config_path, tmp_path / "state.json", _runner(), platform_name="win32"
    )

    assert report.exit_code == 0
    assert _status(report, "config permissions") is CheckStatus.OK
    assert "does not apply on Windows" in report.render()


@pytest.mark.parametrize(
    "config_text",
    [
        'platform = "feishu"\n',
        'platform = "telegram"\n[telegram]\nbot_token = "token"\n',
        'platform = "qq"\n[qq]\napp_id = "app-1"\napp_secret = "secret-1"\n',
    ],
)
def test_selected_adapter_requires_credentials_and_an_allowlist(
    tmp_path: Path, config_text: str
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")
    config_path.chmod(0o600)

    report = _diagnose(
        config_path, tmp_path / "state.json", _runner()
    )

    assert report.exit_code == 1
    assert _status(report, "adapter") is CheckStatus.ERROR


def test_missing_kimi_is_blocking_without_running_commands(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_feishu_config(config_path, tmp_path / "workspace")
    runner = _runner()

    report = _diagnose(
        config_path, tmp_path / "state.json", runner, kimi_path=None
    )

    assert report.exit_code == 1
    assert _status(report, "kimi") is CheckStatus.ERROR
    assert _status(report, "kimi config") is CheckStatus.SKIPPED
    assert runner.calls == [("/fake/ffmpeg", "-version")]


def test_legacy_kimi_cli_is_actionable_and_does_not_run_its_doctor(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_feishu_config(config_path, tmp_path / "workspace")
    runner = _runner(
        version="kimi, version 1.49.0\n", help_output=LEGACY_KIMI_CLI_HELP
    )

    report = _diagnose(config_path, tmp_path / "state.json", runner)

    assert report.exit_code == 1
    assert _status(report, "kimi") is CheckStatus.ERROR
    assert "legacy Python kimi-cli 1.49.0" in report.render()
    assert ("/fake/kimi", "doctor", "config") not in runner.calls


def test_kimi_config_failure_is_blocking_and_captured_output_is_hidden(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    bridge_secrets = _write_feishu_config(config_path, tmp_path / "workspace")
    kimi_secret = "DO_NOT_PRINT_KIMI_SECRET"
    runner = _runner(
        config_result=CommandResult(1, f"invalid api_key = {kimi_secret}\n")
    )

    report = _diagnose(config_path, tmp_path / "state.json", runner)
    rendered = report.render()

    assert report.exit_code == 1
    assert _status(report, "kimi config") is CheckStatus.ERROR
    assert kimi_secret not in rendered
    assert all(secret not in rendered for secret in bridge_secrets)


def test_existing_state_is_read_without_being_rewritten(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_feishu_config(config_path, tmp_path / "workspace")
    state_path = tmp_path / "state.json"
    state = '{"version": 2, "bindings": {}}\n'
    state_path.write_text(state, encoding="utf-8")

    report = _diagnose(config_path, state_path, _runner())

    assert report.exit_code == 0
    assert _status(report, "state") is CheckStatus.OK
    assert state_path.read_text(encoding="utf-8") == state


@pytest.mark.parametrize(
    ("version", "help_output"),
    [
        ("0.28.1\n", "Usage: kimi [options]\n"),
        ("DO_NOT_PRINT_VERSION_OUTPUT\n", KIMI_CODE_HELP),
    ],
)
def test_unrecognized_kimi_output_is_blocking_and_not_echoed(
    tmp_path: Path, version: str, help_output: str
) -> None:
    config_path = tmp_path / "config.toml"
    _write_feishu_config(config_path, tmp_path / "workspace")

    report = _diagnose(
        config_path,
        tmp_path / "state.json",
        _runner(version=version, help_output=help_output),
    )

    assert report.exit_code == 1
    assert _status(report, "kimi") is CheckStatus.ERROR
    assert version.strip() not in report.render()
