"""Non-starting diagnostics for a local kimi-bridge installation."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, TextIO

from .compatibility import (
    KIMI_CODE_INSTALL_URL,
    KimiProduct,
    KimiProductFingerprintError,
    VersionSupport,
    identify_kimi_executable,
    legacy_product_message,
    unknown_version_warning,
)
from .config import DEFAULT_CONFIG_PATH, Config, load_config
from .state import DEFAULT_STATE_PATH, StateStore


class CheckStatus(str, Enum):
    """Stable status classes used by human and agent-facing diagnostics."""

    OK = "OK"
    WARNING = "WARN"
    ERROR = "ERROR"
    SKIPPED = "SKIP"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One bounded diagnostic result."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Complete non-starting diagnostic report."""

    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int:
        return int(any(check.status is CheckStatus.ERROR for check in self.checks))

    def render(self) -> str:
        width = max(len(check.status.value) for check in self.checks)
        lines = [
            f"{check.status.value:<{width}}  {check.name}: {check.detail}"
            for check in self.checks
        ]
        errors = sum(check.status is CheckStatus.ERROR for check in self.checks)
        warnings = sum(check.status is CheckStatus.WARNING for check in self.checks)
        outcome = "passed" if errors == 0 else "failed"
        lines.append(
            f"Doctor {outcome}: {errors} blocking error(s), {warnings} warning(s)."
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured noninteractive command result."""

    returncode: int
    output: str


class CommandRunner(Protocol):
    def __call__(
        self, command: Sequence[str], *, timeout: float
    ) -> CommandResult: ...


def diagnose(
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    state_path: str | Path = DEFAULT_STATE_PATH,
    executable: str = "kimi",
    command_runner: CommandRunner | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform_name: str = sys.platform,
) -> DoctorReport:
    """Inspect local configuration without starting bridge-owned services."""

    runner = command_runner or _run_command
    checks: list[DoctorCheck] = []
    config = _check_config(Path(config_path).expanduser(), checks, platform_name)
    if config is None:
        checks.extend(
            [
                DoctorCheck("adapter", CheckStatus.SKIPPED, "configuration unavailable"),
                DoctorCheck("workspace", CheckStatus.SKIPPED, "configuration unavailable"),
                DoctorCheck("state", CheckStatus.SKIPPED, "configuration unavailable"),
            ]
        )
    else:
        checks.append(_check_selected_adapter(config))
        checks.append(_check_directory_target("workspace", config.default_workspace))
        checks.append(_check_state(Path(state_path).expanduser()))

    _check_kimi(executable, runner, which, checks)
    return DoctorReport(tuple(checks))


def run_doctor(*, stream: TextIO | None = None) -> int:
    """Run diagnostics, print their safe projection, and return an exit code."""

    report = diagnose()
    print(report.render(), file=stream or sys.stdout)
    return report.exit_code


def _check_config(
    path: Path, checks: list[DoctorCheck], platform_name: str
) -> Config | None:
    if not path.exists():
        checks.append(DoctorCheck("config", CheckStatus.ERROR, f"not found: {path}"))
        checks.append(
            DoctorCheck(
                "config permissions",
                CheckStatus.SKIPPED,
                "configuration unavailable",
            )
        )
        return None
    if not path.is_file():
        checks.append(
            DoctorCheck("config", CheckStatus.ERROR, f"not a regular file: {path}")
        )
        checks.append(
            DoctorCheck(
                "config permissions",
                CheckStatus.SKIPPED,
                "configuration unavailable",
            )
        )
        return None

    checks.append(_check_config_permissions(path, platform_name))
    try:
        config = load_config(path)
    except (OSError, TypeError, ValueError) as exc:
        checks.insert(
            len(checks) - 1,
            DoctorCheck(
                "config",
                CheckStatus.ERROR,
                f"invalid configuration ({type(exc).__name__}); values were not shown",
            ),
        )
        return None

    checks.insert(
        len(checks) - 1,
        DoctorCheck("config", CheckStatus.OK, f"loaded selected {config.platform} adapter"),
    )
    return config


def _check_config_permissions(path: Path, platform_name: str) -> DoctorCheck:
    if not platform_name.startswith("linux"):
        return DoctorCheck(
            "config permissions",
            CheckStatus.OK,
            "Linux readability check does not apply",
        )
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return DoctorCheck(
            "config permissions", CheckStatus.ERROR, "could not inspect file mode"
        )
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        return DoctorCheck(
            "config permissions",
            CheckStatus.WARNING,
            f"{path} is readable by group or others; use chmod 600",
        )
    return DoctorCheck(
        "config permissions", CheckStatus.OK, "not readable by group or others"
    )


def _check_selected_adapter(config: Config) -> DoctorCheck:
    if config.platform == "feishu":
        credentials_present = bool(config.feishu.app_id and config.feishu.app_secret)
        allowlist_size = len(config.feishu.allowed_users)
    else:
        credentials_present = bool(config.telegram.bot_token)
        allowlist_size = len(config.telegram.allowed_users)

    missing: list[str] = []
    if not credentials_present:
        missing.append("credentials")
    if allowlist_size == 0:
        missing.append("allowlist")
    if missing:
        return DoctorCheck(
            "adapter",
            CheckStatus.ERROR,
            f"selected {config.platform} adapter is missing " + " and ".join(missing),
        )
    return DoctorCheck(
        "adapter",
        CheckStatus.OK,
        f"selected {config.platform}; credentials present; "
        f"{allowlist_size} allowlisted user(s)",
    )


def _check_directory_target(name: str, path: Path) -> DoctorCheck:
    target = path.expanduser()
    if target.exists():
        if not target.is_dir():
            return DoctorCheck(name, CheckStatus.ERROR, f"not a directory: {target}")
        if not os.access(target, os.W_OK | os.X_OK):
            return DoctorCheck(name, CheckStatus.ERROR, f"not writable: {target}")
        return DoctorCheck(name, CheckStatus.OK, f"usable directory: {target}")

    ancestor = target.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        return DoctorCheck(
            name,
            CheckStatus.ERROR,
            f"cannot create {target} from its existing parent",
        )
    return DoctorCheck(name, CheckStatus.OK, f"can be created: {target}")


def _check_state(path: Path) -> DoctorCheck:
    parent_check = _check_directory_target("state", path.parent)
    if parent_check.status is CheckStatus.ERROR:
        return parent_check
    if not path.exists():
        return DoctorCheck("state", CheckStatus.OK, f"parent usable: {path.parent}")
    if not path.is_file():
        return DoctorCheck("state", CheckStatus.ERROR, f"not a regular file: {path}")
    try:
        StateStore(path).load()
    except (OSError, TypeError, ValueError):
        return DoctorCheck(
            "state",
            CheckStatus.ERROR,
            "existing state is unreadable or invalid; it was not modified",
        )
    return DoctorCheck(
        "state", CheckStatus.OK, "existing state is readable; it was not modified"
    )


def _check_kimi(
    executable: str,
    runner: CommandRunner,
    which: Callable[[str], str | None],
    checks: list[DoctorCheck],
) -> None:
    executable_path = which(executable)
    if executable_path is None:
        checks.append(
            DoctorCheck(
                "kimi",
                CheckStatus.ERROR,
                f"not found on PATH; install Kimi Code: {KIMI_CODE_INSTALL_URL}",
            )
        )
        checks.append(
            DoctorCheck("kimi config", CheckStatus.SKIPPED, "Kimi Code unavailable")
        )
        return

    version_result = _invoke(runner, (executable_path, "--version"))
    help_result = _invoke(runner, (executable_path, "--help"))
    if version_result is None or help_result is None:
        checks.append(
            DoctorCheck(
                "kimi",
                CheckStatus.ERROR,
                "non-starting identity probe failed or timed out; output was not shown",
            )
        )
        checks.append(
            DoctorCheck("kimi config", CheckStatus.SKIPPED, "Kimi identity unavailable")
        )
        return

    try:
        identity = identify_kimi_executable(
            version_result.output, help_result.output
        )
    except KimiProductFingerprintError:
        checks.append(
            DoctorCheck(
                "kimi",
                CheckStatus.ERROR,
                "unrecognized product fingerprint; captured output was not shown",
            )
        )
        checks.append(
            DoctorCheck("kimi config", CheckStatus.SKIPPED, "Kimi identity unavailable")
        )
        return

    if identity.product is KimiProduct.LEGACY_KIMI_CLI:
        checks.append(
            DoctorCheck(
                "kimi", CheckStatus.ERROR, legacy_product_message(identity.version)
            )
        )
        checks.append(
            DoctorCheck(
                "kimi config", CheckStatus.SKIPPED, "legacy product is incompatible"
            )
        )
        return

    if identity.support is VersionSupport.SUPPORTED:
        checks.append(
            DoctorCheck(
                "kimi",
                CheckStatus.OK,
                f"official kimi-code {identity.version} is supported ({executable_path})",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "kimi",
                CheckStatus.WARNING,
                unknown_version_warning(identity.version),
            )
        )

    config_result = _invoke(runner, (executable_path, "doctor", "config"))
    if config_result is None:
        checks.append(
            DoctorCheck(
                "kimi config",
                CheckStatus.ERROR,
                "noninteractive validation failed or timed out; output was not shown",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "kimi config",
                CheckStatus.OK,
                "validated noninteractively; captured output was not shown",
            )
        )


def _invoke(runner: CommandRunner, command: Sequence[str]) -> CommandResult | None:
    try:
        result = runner(command, timeout=15.0)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    if result.returncode != 0:
        return None
    return result


def _run_command(command: Sequence[str], *, timeout: float) -> CommandResult:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return CommandResult(completed.returncode, completed.stdout)
