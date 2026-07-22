from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from kimi_bridge.doctor import (
    CheckStatus,
    CommandResult,
    DoctorReport,
    diagnose,
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
    version: str = "0.28.1\n",
    help_output: str = KIMI_CODE_HELP,
    config_result: CommandResult = CommandResult(0, "configuration valid\n"),
) -> FakeRunner:
    return FakeRunner(
        {
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
                f'default_workspace = "{workspace}"',
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
                f'default_workspace = "{workspace}"',
                "[telegram]",
                f'bot_token = "{token}"',
                f"allowed_users = [{user_id}]",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return token, user_id


def _diagnose(
    config_path: Path,
    state_path: Path,
    runner: FakeRunner,
    *,
    kimi_path: str | None = "/fake/kimi",
) -> DoctorReport:
    return diagnose(
        config_path=config_path,
        state_path=state_path,
        command_runner=runner,
        which=lambda _name: kimi_path,
        platform_name="linux",
    )


def _status(report: DoctorReport, name: str) -> CheckStatus:
    return next(check.status for check in report.checks if check.name == name)


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


def test_group_readable_config_warns_without_failing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_feishu_config(config_path, tmp_path / "workspace")
    config_path.chmod(0o640)

    report = _diagnose(
        config_path, tmp_path / "state.json", _runner()
    )

    assert report.exit_code == 0
    assert _status(report, "config permissions") is CheckStatus.WARNING


@pytest.mark.parametrize(
    "config_text",
    [
        'platform = "feishu"\n',
        'platform = "telegram"\n[telegram]\nbot_token = "token"\n',
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
    assert runner.calls == []


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
