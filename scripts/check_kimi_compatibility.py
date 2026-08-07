#!/usr/bin/env python3
"""Credential-free Kimi compatibility checks and quiet GitHub synchronization."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from kimi_bridge.compatibility import (
    KimiProduct,
    SUPPORTED_KIMI_CODE_VERSIONS,
    identify_kimi_executable,
    kimi_code_version_sort_key,
    normalize_kimi_code_version,
)
from kimi_bridge.kimi_server import (
    KIMI_REQUIRED_WEB_FLAGS,
    KIMI_SEMANTIC_CONTRACT_VERSION,
    KimiCompatibilityProbe,
    KimiContractCheck,
    KimiServerStartupError,
    KimiServerSupervisor,
    evaluate_kimi_semantic_contract,
    kimi_semantic_contract,
    probe_kimi_compatibility,
)


REPORT_SCHEMA_VERSION = 2
CANARY_PLATFORMS = ("linux", "macos", "windows")
OFFICIAL_KIMI_INSTALLER_URL = "https://code.kimi.com/kimi-code/install.sh"
# /install.ps1 is the deprecated Python kimi-cli installer; Kimi Code lives
# under /kimi-code/ on both platforms.
OFFICIAL_KIMI_WINDOWS_INSTALLER_URL = "https://code.kimi.com/kimi-code/install.ps1"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
PROBE_MODEL_ALIAS = "compatibility/probe"
PROBE_PROVIDER_ID = "compatibility"
AUTOMATION_BRANCH = "automation/kimi-code-compatibility"
PROMOTION_MARKER = "<!-- kimi-bridge:compatibility-promotion -->"
DRIFT_MARKER = "<!-- kimi-bridge:upstream-drift -->"
DRIFT_LABEL = "upstream-drift"
_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+")
_FRAGMENT_TOKEN_RE = re.compile(r"(?<=#token=)[A-Za-z0-9_-]+")
_JSON_TOKEN_RE = re.compile(
    r'(?i)(["\'](?:token|secret|app_secret|api[_-]?key|password)'
    r'["\']\s*:\s*["\'])[^"\']+'
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|secret|api[_-]?key|password)=)[^&\s]+"
)


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Bounded, secret-safe file included beside a compatibility report."""

    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    """Stable machine-readable compatibility result."""

    mode: str
    platform: str
    product: str
    version: str
    supported: bool
    compatible: bool
    checks: tuple[KimiContractCheck, ...]
    failures: tuple[dict[str, str], ...]
    failure_digest: str
    report_digest: str
    artifacts: tuple[ArtifactMetadata, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "contract_schema_version": KIMI_SEMANTIC_CONTRACT_VERSION,
            "mode": self.mode,
            "platform": self.platform,
            "product": self.product,
            "version": self.version,
            "supported": self.supported,
            "compatible": self.compatible,
            "checks": [
                {
                    "id": item.id,
                    "category": item.category,
                    "status": item.status,
                    "detail": item.detail,
                    "source": item.source,
                }
                for item in self.checks
            ],
            "failures": list(self.failures),
            "failure_digest": self.failure_digest,
            "report_digest": self.report_digest,
            "artifacts": [
                {
                    "name": item.name,
                    "size": item.size,
                    "sha256": item.sha256,
                }
                for item in self.artifacts
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CompatibilityReport:
        if value.get("schema_version") != REPORT_SCHEMA_VERSION:
            raise ValueError("unsupported compatibility report schema")
        if (
            value.get("contract_schema_version")
            != KIMI_SEMANTIC_CONTRACT_VERSION
        ):
            raise ValueError("unsupported Kimi semantic contract schema")
        checks = tuple(
            KimiContractCheck(
                id=str(item["id"]),
                category=str(item["category"]),
                status=str(item["status"]),  # type: ignore[arg-type]
                detail=str(item["detail"]),
                source=str(item["source"]),
            )
            for item in value.get("checks", [])
        )
        artifacts = tuple(
            ArtifactMetadata(
                name=str(item["name"]),
                size=int(item["size"]),
                sha256=str(item["sha256"]),
            )
            for item in value.get("artifacts", [])
        )
        failures = tuple(
            {
                "id": str(item["id"]),
                "category": str(item["category"]),
                "detail": str(item["detail"]),
            }
            for item in value.get("failures", [])
        )
        return cls(
            mode=str(value["mode"]),
            platform=str(value["platform"]),
            product=str(value["product"]),
            version=str(value["version"]),
            supported=bool(value["supported"]),
            compatible=bool(value["compatible"]),
            checks=checks,
            failures=failures,
            failure_digest=str(value["failure_digest"]),
            report_digest=str(value["report_digest"]),
            artifacts=artifacts,
        )


@dataclass(frozen=True, slots=True)
class CompatibilitySummary:
    """Strict cross-platform verdict combining one report per platform."""

    version: str
    supported: bool
    compatible: bool
    platforms: tuple[str, ...]
    failures: tuple[dict[str, str], ...]
    failure_digest: str
    report_digest: str


@dataclass(frozen=True, slots=True)
class InstalledKimi:
    executable: Path
    environment: dict[str, str]


class CommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


class GitHubAutomation(Protocol):
    """Semantic operations needed by the synchronization decision."""

    def recover_drift_issue(
        self, summary: CompatibilitySummary
    ) -> Literal["closed", "recorded"] | None: ...

    def promote_version(self, summary: CompatibilitySummary) -> str | None: ...

    def record_drift(self, summary: CompatibilitySummary) -> str: ...


class CompatibilityCheckError(RuntimeError):
    def __init__(self, category: str, detail: str) -> None:
        super().__init__(redact(detail))
        self.category = category


def _parse_toml(document: str, name: str) -> dict[str, Any]:
    try:
        return tomllib.loads(document)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"{name} is not valid TOML") from exc


def _plain_version(value: object, source: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d+\.\d+\.\d+", value) is None
    ):
        raise RuntimeError(f"{source} has no plain project version")
    return value


def _replace_version_assignment(
    document: str,
    *,
    start: int,
    end: int,
    current_version: str,
    next_version: str,
    source: str,
) -> str:
    section = document[start:end]
    pattern = re.compile(
        rf'(?m)^(\s*version\s*=\s*)(["\']){re.escape(current_version)}'
        rf'(\2\s*(?:#.*)?)$'
    )
    updated, count = pattern.subn(
        rf"\g<1>\g<2>{next_version}\g<3>", section, count=1
    )
    if count != 1:
        raise RuntimeError(f"{source} project version could not be updated")
    return document[:start] + updated + document[end:]


def _update_pyproject_version(
    document: str, current_version: str, next_version: str
) -> str:
    project = re.search(r"(?m)^\[project\]\s*(?:#.*)?$", document)
    if project is None:
        raise RuntimeError("pyproject.toml has no [project] table")
    next_table = re.search(r"(?m)^\[", document[project.end() :])
    end = (
        len(document)
        if next_table is None
        else project.end() + next_table.start()
    )
    return _replace_version_assignment(
        document,
        start=project.end(),
        end=end,
        current_version=current_version,
        next_version=next_version,
        source="pyproject.toml",
    )


def _update_lock_version(
    document: str, current_version: str, next_version: str
) -> str:
    starts = [
        match.start()
        for match in re.finditer(
            r"(?m)^\[\[package\]\]\s*(?:#.*)?$", document
        )
    ]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(document)
        package = _parse_toml(document[start:end], "uv.lock package")
        entries = package.get("package")
        if (
            isinstance(entries, list)
            and len(entries) == 1
            and isinstance(entries[0], dict)
            and entries[0].get("name") == "kimi-bridge"
        ):
            return _replace_version_assignment(
                document,
                start=start,
                end=end,
                current_version=current_version,
                next_version=next_version,
                source="uv.lock",
            )
    raise RuntimeError("uv.lock has no kimi-bridge package")


def prepare_compatibility_release(
    *,
    kimi_code_version: str,
    pyproject: str,
    uv_lock: str,
    compatibility_map: str,
) -> tuple[str, dict[str, str]]:
    """Build the three files for the next patch compatibility release."""

    project_payload = _parse_toml(pyproject, "pyproject.toml")
    project = project_payload.get("project")
    if not isinstance(project, dict):
        raise RuntimeError("pyproject.toml has no [project] table")
    current_version = _plain_version(
        project.get("version"), "pyproject.toml"
    )
    major, minor, patch = (int(item) for item in current_version.split("."))
    next_version = f"{major}.{minor}.{patch + 1}"

    lock_payload = _parse_toml(uv_lock, "uv.lock")
    packages = lock_payload.get("package")
    if not isinstance(packages, list):
        raise RuntimeError("uv.lock has no package list")
    locked_versions = [
        package.get("version")
        for package in packages
        if isinstance(package, dict) and package.get("name") == "kimi-bridge"
    ]
    if locked_versions != [current_version]:
        raise RuntimeError("uv.lock project version does not match pyproject.toml")

    payload = json.loads(compatibility_map)
    releases = payload.get("releases")
    if not isinstance(releases, list) or not releases:
        raise RuntimeError("compatibility map is malformed")
    latest = releases[-1]
    if not isinstance(latest, dict) or latest.get("bridge") != current_version:
        raise RuntimeError(
            "latest compatibility-map release does not match project version"
        )
    versions = latest.get("kimi_code")
    if not isinstance(versions, list):
        raise RuntimeError("compatibility map is malformed")
    if kimi_code_version in versions:
        raise RuntimeError("Kimi Code version is already supported")
    releases.append(
        {
            "bridge": next_version,
            "kimi_code": sorted(
                {*versions, kimi_code_version},
                key=kimi_code_version_sort_key,
            ),
        }
    )

    return next_version, {
        "pyproject.toml": _update_pyproject_version(
            pyproject, current_version, next_version
        ),
        "uv.lock": _update_lock_version(
            uv_lock, current_version, next_version
        ),
        "src/kimi_bridge/compatibility-map.json": (
            json.dumps(payload, indent=2) + "\n"
        ),
    }


def redact(value: str, *, limit: int = 2000) -> str:
    """Remove known credential forms and bound diagnostic text."""

    redacted = _BEARER_RE.sub(r"\1<redacted>", value)
    redacted = _FRAGMENT_TOKEN_RE.sub("<redacted>", redacted)
    redacted = _JSON_TOKEN_RE.sub(r"\1<redacted>", redacted)
    redacted = _QUERY_SECRET_RE.sub(r"\1<redacted>", redacted)
    if len(redacted) > limit:
        return redacted[:limit] + "...<truncated>"
    return redacted


def normalize_platform_name(platform_name: str = sys.platform) -> str:
    """Map a sys.platform value onto the canary platform vocabulary."""

    if platform_name.startswith("win"):
        return "windows"
    if platform_name.startswith("darwin"):
        return "macos"
    return "linux"


def build_report(
    *,
    mode: str,
    product: str,
    version: str,
    checks: Sequence[KimiContractCheck],
    artifacts: Sequence[ArtifactMetadata] = (),
    platform: str | None = None,
) -> CompatibilityReport:
    """Normalize checks and compute digests independently of map ordering."""

    report_platform = platform or normalize_platform_name()
    normalized_checks = tuple(
        sorted(
            (
                KimiContractCheck(
                    item.id,
                    item.category,
                    item.status,
                    redact(item.detail),
                    item.source,
                )
                for item in checks
            ),
            key=lambda item: item.id,
        )
    )
    failures = tuple(
        {
            "id": item.id,
            "category": item.category,
            "detail": item.detail,
        }
        for item in normalized_checks
        if item.status == "fail"
    )
    failure_digest = _digest(failures)
    supported = (
        product == KimiProduct.KIMI_CODE.value
        and version in SUPPORTED_KIMI_CODE_VERSIONS
    )
    core = {
        "mode": mode,
        "platform": report_platform,
        "product": product,
        "version": version,
        "supported": supported,
        "compatible": not failures,
        "checks": [
            (item.id, item.category, item.status, item.detail, item.source)
            for item in normalized_checks
        ],
        "failures": failures,
    }
    return CompatibilityReport(
        mode=mode,
        platform=report_platform,
        product=product,
        version=version,
        supported=supported,
        compatible=not failures,
        checks=normalized_checks,
        failures=failures,
        failure_digest=failure_digest,
        report_digest=_digest(core),
        artifacts=tuple(sorted(artifacts, key=lambda item: item.name)),
    )


def check_fixture(fixture_directory: Path) -> CompatibilityReport:
    """Evaluate recorded CLI/spec surfaces without starting a process."""

    version_output = _read_fixture(fixture_directory, "version.txt")
    help_output = _read_fixture(fixture_directory, "help.txt")
    web_help = _read_fixture(fixture_directory, "web-help.txt")
    openapi = _read_json_fixture(fixture_directory, "openapi.json")
    asyncapi = _read_json_fixture(fixture_directory, "asyncapi.json")
    checks: list[KimiContractCheck] = []
    product = "unknown"
    version = "unknown"
    try:
        identity = identify_kimi_executable(version_output, help_output)
        product = identity.product.value
        version = identity.version
        checks.append(
            _check(
                "cli.product",
                "cli",
                identity.product is KimiProduct.KIMI_CODE,
                "the executable has the official kimi-code fingerprint",
                "identify_kimi_executable",
            )
        )
    except ValueError as exc:
        checks.append(
            _check(
                "cli.product",
                "cli",
                False,
                str(exc),
                "identify_kimi_executable",
            )
        )
    checks.extend(
        evaluate_kimi_semantic_contract(
            openapi,
            asyncapi,
            expected_version=version if version != "unknown" else None,
        )
    )
    missing_flags = sorted(
        flag for flag in KIMI_REQUIRED_WEB_FLAGS if flag not in web_help
    )
    checks.append(
        _check(
            "cli.web.flags",
            "cli",
            not missing_flags,
            (
                "kimi web exposes the required managed-server flags"
                if not missing_flags
                else "missing kimi web flags: " + ", ".join(missing_flags)
            ),
            "KimiServerSupervisor._check_executable_identity",
        )
    )
    return build_report(
        mode="fixture",
        product=product,
        version=version,
        checks=checks,
    )


async def check_live(
    *,
    version: str | None = None,
    artifact_directory: Path | None = None,
    installer_url: str | None = None,
    runner: CommandRunner | None = None,
    startup_timeout: float = 30.0,
    platform_name: str = sys.platform,
) -> CompatibilityReport:
    """Install and probe Kimi in a disposable credential-free home."""

    command_runner = runner or _run_command
    report_platform = normalize_platform_name(platform_name)
    if installer_url is None:
        installer_url = (
            OFFICIAL_KIMI_WINDOWS_INSTALLER_URL
            if platform_name.startswith("win")
            else OFFICIAL_KIMI_INSTALLER_URL
        )
    product = "unknown"
    temporary_root: str | None = None
    if version is not None:
        try:
            version = normalize_kimi_code_version(version)
        except ValueError as exc:
            return build_report(
                mode="live",
                product=product,
                version="invalid",
                checks=(
                    _check(
                        "input.version",
                        "input",
                        False,
                        str(exc),
                        "check_live",
                    ),
                ),
                platform=report_platform,
            )
    reported_version = version or "latest"
    try:
        with tempfile.TemporaryDirectory(
            prefix="kimi-bridge-compatibility-"
        ) as raw_root:
            temporary_root = raw_root
            root = Path(raw_root)
            installed = install_official_kimi(
                root,
                version=version,
                installer_url=installer_url,
                runner=command_runner,
                platform_name=platform_name,
            )
            _write_probe_config(Path(installed.environment["KIMI_CODE_HOME"]))
            supervisor = KimiServerSupervisor(
                executable=str(installed.executable),
                process_env=installed.environment,
                startup_timeout=startup_timeout,
                shutdown_timeout=min(5.0, max(0.1, startup_timeout)),
            )
            async with supervisor:
                product = supervisor.executable_identity.product.value
                reported_version = supervisor.executable_identity.version
                probe = await probe_kimi_compatibility(
                    supervisor, root / "workspace"
                )
            artifacts = _write_probe_artifacts(probe, artifact_directory)
            return build_report(
                mode="live",
                product=probe.product,
                version=probe.version,
                checks=probe.checks,
                artifacts=artifacts,
                platform=report_platform,
            )
    except CompatibilityCheckError as exc:
        category = exc.category
        detail = str(exc)
    except (TimeoutError, KimiServerStartupError) as exc:
        category = "startup"
        detail = str(exc) or "Kimi startup timed out"
    except Exception as exc:  # boundary: convert a failed canary into a report
        category = "runtime"
        detail = f"{type(exc).__name__}: {exc}"
    if temporary_root is not None:
        detail = detail.replace(temporary_root, "<temporary-directory>")
    return build_report(
        mode="live",
        product=product,
        version=reported_version,
        checks=(
            _check(
                f"{category}.probe",
                category,
                False,
                detail,
                "check_live",
            ),
        ),
        platform=report_platform,
    )


def install_official_kimi(
    root: Path,
    *,
    version: str | None,
    installer_url: str,
    runner: CommandRunner,
    platform_name: str = sys.platform,
) -> InstalledKimi:
    """Download an inspectable installer file and execute it in isolation."""

    windows = platform_name.startswith("win")
    installer = root / ("install.ps1" if windows else "install.sh")
    install_directory = root / "install"
    home = root / "home"
    kimi_home = root / "kimi-home"
    for directory in (install_directory, home, kimi_home):
        directory.mkdir(parents=True, exist_ok=True)
    environment = _isolated_environment(
        home=home,
        install_directory=install_directory,
        kimi_home=kimi_home,
        version=version,
        platform_name=platform_name,
    )
    _run_checked(
        runner,
        [
            "curl",
            "--fail",
            "--show-error",
            "--silent",
            "--location",
            "--retry",
            "3",
            "--retry-all-errors",
            "--retry-delay",
            "2",
            "--connect-timeout",
            "20",
            "--output",
            str(installer),
            installer_url,
        ],
        env=environment,
        timeout=180.0,
        category="installer-download",
    )
    if windows:
        # -Command instead of -File so progress rendering can be disabled;
        # Windows PowerShell 5.1 downloads are pathologically slow otherwise.
        install_command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "$ProgressPreference = 'SilentlyContinue'; "
            f"& '{installer}'; exit $LASTEXITCODE",
        ]
    else:
        install_command = ["bash", str(installer)]
    _run_checked(
        runner,
        install_command,
        env=environment,
        timeout=600.0,
        category="installer-execution",
    )
    executable_names = ("kimi.exe", "kimi.cmd") if windows else ("kimi",)
    for name in executable_names:
        executable = install_directory / "bin" / name
        if executable.is_file():
            return InstalledKimi(executable, environment)
    raise CompatibilityCheckError(
        "installer-execution",
        "official installer completed without creating "
        + " or ".join(f"bin/{name}" for name in executable_names),
    )


def write_report(report: CompatibilityReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_report(path: Path) -> CompatibilityReport:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("compatibility report must be a JSON object")
    return CompatibilityReport.from_dict(payload)


def summarize_reports(
    reports: Sequence[CompatibilityReport],
) -> CompatibilitySummary:
    """Combine per-platform reports into one strict all-platform verdict."""

    if not reports:
        raise ValueError("at least one compatibility report is required")
    ordered = sorted(reports, key=lambda item: item.platform)
    platforms = tuple(item.platform for item in ordered)
    if len(set(platforms)) != len(platforms):
        raise ValueError("each compatibility report must cover a distinct platform")
    missing = tuple(
        platform for platform in CANARY_PLATFORMS if platform not in platforms
    )
    failures: list[dict[str, str]] = []
    for report in ordered:
        failures.extend(
            {**failure, "platform": report.platform}
            for failure in report.failures
        )
    for platform in missing:
        failures.append(
            {
                "id": "aggregation.platform",
                "category": "aggregation",
                "detail": f"no compatibility report for {platform}",
                "platform": platform,
            }
        )
    versions = sorted({item.version for item in ordered})
    if len(versions) > 1:
        observed = ", ".join(
            f"{item.platform}={item.version}" for item in ordered
        )
        failures.append(
            {
                "id": "aggregation.version",
                "category": "aggregation",
                "detail": "platforms observed different kimi-code versions: "
                + observed,
                "platform": "all",
            }
        )
    version = versions[0] if len(versions) == 1 else "mixed"
    compatible = not failures
    return CompatibilitySummary(
        version=version,
        supported=all(item.supported for item in ordered),
        compatible=compatible,
        platforms=platforms,
        failures=tuple(failures),
        failure_digest=_digest(failures),
        report_digest=_digest(
            [(item.platform, item.report_digest) for item in ordered]
        ),
    )


def synchronize_reports(
    reports: Sequence[CompatibilityReport], automation: GitHubAutomation
) -> tuple[str, ...]:
    """Apply the quiet pass/promotion/drift/recovery decision tree.

    Promotion and recovery require every canary platform to be compatible
    with the same kimi-code version; any platform failure records drift.
    A version that recovers from recorded drift requires manual release
    preparation because the fix may have dropped older Kimi compatibility.
    """

    summary = summarize_reports(reports)
    actions: list[str] = []
    if summary.compatible:
        recovery = automation.recover_drift_issue(summary)
        if recovery == "closed":
            actions.append("closed-recovered-drift-issue")
        if recovery is None and not summary.supported:
            promotion = automation.promote_version(summary)
            if promotion is not None:
                actions.append(promotion)
    else:
        actions.append(automation.record_drift(summary))
    return tuple(actions)


class DryRunAutomation:
    """Predict the synchronization decision without touching GitHub."""

    def recover_drift_issue(
        self, summary: CompatibilitySummary
    ) -> Literal["closed", "recorded"] | None:
        return None

    def promote_version(self, summary: CompatibilitySummary) -> str | None:
        return f"would-promote-{summary.version}"

    def record_drift(self, summary: CompatibilitySummary) -> str:
        return "would-record-drift"


class GitHubApiAutomation:
    """Small GitHub API client for one repository's automation state."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        default_branch: str = "main",
        api_url: str = "https://api.github.com",
        run_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if repository.count("/") != 1:
            raise ValueError("repository must have owner/name form")
        self.repository = repository
        self.owner, self.name = repository.split("/", 1)
        self.default_branch = default_branch
        self.run_url = run_url
        self._client = client or httpx.Client(
            base_url=api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GitHubApiAutomation:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def recover_drift_issue(
        self, summary: CompatibilitySummary
    ) -> Literal["closed", "recorded"] | None:
        issue = self._find_drift_issue()
        if issue is None:
            return None
        body = str(issue.get("body", ""))
        if f"<!-- version:{summary.version} " not in body:
            return None
        if issue.get("state") != "open":
            return "recorded"
        number = int(issue["number"])
        self._request(
            "POST",
            f"/repos/{self.repository}/issues/{number}/comments",
            json={
                "body": (
                    f"Compatibility recovered with kimi-code {summary.version} "
                    f"on {', '.join(summary.platforms)}. A bridge release for "
                    f"this recovered version must be prepared manually."
                    + self._run_link_suffix()
                )
            },
        )
        self._request(
            "PATCH",
            f"/repos/{self.repository}/issues/{number}",
            json={"state": "closed", "state_reason": "completed"},
        )
        return "closed"

    def promote_version(self, summary: CompatibilitySummary) -> str | None:
        state_marker = (
            f"<!-- version:{summary.version} "
            f"report-digest:{summary.report_digest} -->"
        )
        pull = self._find_promotion_pull()
        if pull is not None and state_marker in str(pull.get("body", "")):
            return "unchanged-promotion-pr"
        paths = (
            "pyproject.toml",
            "uv.lock",
            "src/kimi_bridge/compatibility-map.json",
        )
        base_sha = self._branch_sha(self.default_branch)
        current = {
            path: self._get_content(path, base_sha) for path in paths
        }
        map_payload = json.loads(
            _decode_content(
                current["src/kimi_bridge/compatibility-map.json"]
            )
        )
        releases = map_payload.get("releases")
        if (
            not isinstance(releases, list)
            or not releases
            or not isinstance(releases[-1], dict)
        ):
            raise RuntimeError("compatibility map is malformed")
        latest = releases[-1]
        versions = latest.get("kimi_code")
        if not isinstance(versions, list):
            raise RuntimeError("compatibility map is malformed")
        if summary.version in versions:
            return None
        next_version, release_files = prepare_compatibility_release(
            kimi_code_version=summary.version,
            pyproject=_decode_content(current["pyproject.toml"]),
            uv_lock=_decode_content(current["uv.lock"]),
            compatibility_map=_decode_content(
                current["src/kimi_bridge/compatibility-map.json"]
            ),
        )
        self._set_automation_branch(base_sha)
        for path, content in release_files.items():
            self._request(
                "PUT",
                f"/repos/{self.repository}/contents/{path}",
                json={
                    "message": (
                        f"Prepare kimi-bridge {next_version} for "
                        f"Kimi Code {summary.version}"
                    ),
                    "content": base64.b64encode(content.encode()).decode(),
                    "sha": current[path]["sha"],
                    "branch": AUTOMATION_BRANCH,
                },
            )
        title = (
            f"Prepare kimi-bridge {next_version} for Kimi Code {summary.version}"
        )
        body = (
            f"{PROMOTION_MARKER}\n{state_marker}\n\n"
            f"The credential-free compatibility canary passed for kimi-code "
            f"{summary.version} on {', '.join(summary.platforms)}. This PR "
            f"prepares kimi-bridge {next_version} so the promoted version is "
            f"available through the packaged compatibility map.\n\n"
            f"Report digest: `{summary.report_digest}`."
            + self._run_link_suffix()
        )
        if pull is None:
            pull = self._request(
                "POST",
                f"/repos/{self.repository}/pulls",
                json={
                    "title": title,
                    "body": body,
                    "head": AUTOMATION_BRANCH,
                    "base": self.default_branch,
                },
            )
            action = "created-promotion-pr"
        else:
            self._request(
                "PATCH",
                f"/repos/{self.repository}/pulls/{pull['number']}",
                json={"title": title, "body": body},
            )
            action = "updated-promotion-pr"
        self._dispatch_ci()
        self._enable_auto_merge(str(pull["node_id"]))
        return action

    def record_drift(self, summary: CompatibilitySummary) -> str:
        issue = self._find_drift_issue()
        state_marker = (
            f"<!-- version:{summary.version} "
            f"failure-digest:{summary.failure_digest} -->"
        )
        if (
            issue is not None
            and issue.get("state") == "open"
            and state_marker in str(issue.get("body", ""))
        ):
            return "unchanged-drift-issue"
        self._ensure_drift_label()
        failure_lines = "\n".join(
            f"- **{item['platform']}** **{item['category']}** "
            f"`{item['id']}`: {item['detail']}"
            for item in summary.failures
        )
        body = (
            f"{DRIFT_MARKER}\n{state_marker}\n\n"
            f"The credential-free canary found required behavior drift in "
            f"kimi-code {summary.version}.\n\n{failure_lines}\n\n"
            f"Report digest: `{summary.report_digest}`."
            + self._run_link_suffix()
        )
        title = f"Kimi compatibility drift: {summary.version}"
        if issue is None:
            self._request(
                "POST",
                f"/repos/{self.repository}/issues",
                json={"title": title, "body": body, "labels": [DRIFT_LABEL]},
            )
            return "created-drift-issue"
        self._request(
            "PATCH",
            f"/repos/{self.repository}/issues/{issue['number']}",
            json={
                "title": title,
                "body": body,
                "labels": [DRIFT_LABEL],
                "state": "open",
            },
        )
        return "updated-drift-issue"

    def _branch_sha(self, branch: str) -> str:
        payload = self._request(
            "GET", f"/repos/{self.repository}/git/ref/heads/{branch}"
        )
        return str(payload["object"]["sha"])

    def _set_automation_branch(self, base_sha: str) -> None:
        path = f"/repos/{self.repository}/git/refs/heads/{AUTOMATION_BRANCH}"
        response = self._client.get(path)
        if response.status_code == 404:
            self._request(
                "POST",
                f"/repos/{self.repository}/git/refs",
                json={
                    "ref": f"refs/heads/{AUTOMATION_BRANCH}",
                    "sha": base_sha,
                },
            )
            return
        response.raise_for_status()
        self._request("PATCH", path, json={"sha": base_sha, "force": True})

    def _get_content(self, path: str, ref: str) -> dict[str, Any]:
        value = self._request(
            "GET",
            f"/repos/{self.repository}/contents/{path}",
            params={"ref": ref},
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub content response was not an object")
        return value

    def _find_promotion_pull(self) -> dict[str, Any] | None:
        pulls = self._request(
            "GET",
            f"/repos/{self.repository}/pulls",
            params={
                "state": "open",
                "head": f"{self.owner}:{AUTOMATION_BRANCH}",
                "base": self.default_branch,
            },
        )
        return pulls[0] if isinstance(pulls, list) and pulls else None

    def _find_drift_issue(self) -> dict[str, Any] | None:
        issues = self._request(
            "GET",
            f"/repos/{self.repository}/issues",
            params={"state": "all", "labels": DRIFT_LABEL, "per_page": 100},
        )
        if not isinstance(issues, list):
            return None
        for issue in issues:
            if (
                isinstance(issue, dict)
                and "pull_request" not in issue
                and DRIFT_MARKER in str(issue.get("body", ""))
            ):
                return issue
        return None

    def _ensure_drift_label(self) -> None:
        path = f"/repos/{self.repository}/labels/{DRIFT_LABEL}"
        response = self._client.get(path)
        if response.status_code != 404:
            response.raise_for_status()
            return
        self._request(
            "POST",
            f"/repos/{self.repository}/labels",
            json={
                "name": DRIFT_LABEL,
                "color": "d73a4a",
                "description": "Required upstream Kimi behavior changed",
            },
        )

    def _enable_auto_merge(self, pull_request_id: str) -> None:
        self._request(
            "POST",
            "/graphql",
            json={
                "query": (
                    "mutation($id:ID!){enablePullRequestAutoMerge(input:{"
                    "pullRequestId:$id,mergeMethod:SQUASH}){pullRequest{id}}}"
                ),
                "variables": {"id": pull_request_id},
            },
        )

    def _dispatch_ci(self) -> None:
        response = self._client.post(
            f"/repos/{self.repository}/actions/workflows/ci.yml/dispatches",
            json={"ref": AUTOMATION_BRANCH},
        )
        response.raise_for_status()

    def _run_link_suffix(self) -> str:
        return f"\n\n[Workflow run]({self.run_url})" if self.run_url else ""

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()


def run_cli_check(
    *,
    fixture_directory: Path | None,
    version: str | None,
    report_path: Path,
    artifact_directory: Path | None,
) -> CompatibilityReport:
    report = (
        check_fixture(fixture_directory)
        if fixture_directory is not None
        else asyncio.run(
            check_live(
                version=version,
                artifact_directory=artifact_directory,
            )
        )
    )
    write_report(report, report_path)
    return report


def _run_command(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    # Closed stdin keeps interactive installer prompts from hanging forever.
    return subprocess.run(
        list(command),
        env=dict(env) if env is not None else None,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def _run_checked(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    category: str,
) -> None:
    try:
        result = runner(command, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        tail = "\n".join(
            part
            for part in (_decoded_tail(exc.stdout), _decoded_tail(exc.stderr))
            if part
        )
        detail = f"{command[0]} timed out after {timeout:g} seconds"
        if tail:
            detail = f"{detail}; output before timeout:\n{tail}"
        raise CompatibilityCheckError(category, detail) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompatibilityCheckError(category, str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "no output"
        raise CompatibilityCheckError(
            category,
            f"{command[0]} exited with status {result.returncode}: {detail}",
        )


def _decoded_tail(value: str | bytes | None, *, limit: int = 1500) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value.strip()[-limit:]


def _isolated_environment(
    *,
    home: Path,
    install_directory: Path,
    kimi_home: Path,
    version: str | None,
    platform_name: str = sys.platform,
) -> dict[str, str]:
    windows = platform_name.startswith("win")
    retained = (
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    )
    if windows:
        # CreateProcess and PowerShell need these to function at all.
        retained += ("SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    environment = {name: os.environ[name] for name in retained if name in os.environ}
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "KIMI_INSTALL_DIR": str(install_directory),
            "KIMI_NO_MODIFY_PATH": "1",
            "KIMI_CODE_HOME": str(kimi_home),
        }
    )
    if windows:
        environment["USERPROFILE"] = str(home)
        environment["APPDATA"] = str(home / "AppData" / "Roaming")
        environment["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    path_separator = ";" if windows else ":"
    fallback_path = "" if windows else "/usr/bin:/bin"
    environment["PATH"] = path_separator.join(
        part
        for part in (
            str(install_directory / "bin"),
            environment.get("PATH", fallback_path),
        )
        if part
    )
    if version is not None:
        environment["KIMI_VERSION"] = version
    return environment


def _write_probe_config(kimi_home: Path) -> None:
    kimi_home.mkdir(parents=True, exist_ok=True)
    (kimi_home / "config.toml").write_text(
        f"""default_model = "{PROBE_MODEL_ALIAS}"

[providers.{PROBE_PROVIDER_ID}]
type = "openai"
base_url = "http://127.0.0.1:1/v1"
api_key = "unused"

[models."{PROBE_MODEL_ALIAS}"]
provider = "{PROBE_PROVIDER_ID}"
model = "probe"
max_context_size = 1024
""",
        encoding="utf-8",
    )


def _write_probe_artifacts(
    probe: KimiCompatibilityProbe,
    directory: Path | None,
) -> tuple[ArtifactMetadata, ...]:
    if directory is None:
        return ()
    directory.mkdir(parents=True, exist_ok=True)
    metadata = []
    for name, document in (
        ("openapi.json", probe.openapi),
        ("asyncapi.json", probe.asyncapi),
    ):
        content = (json.dumps(document, sort_keys=True) + "\n").encode()
        if len(content) > MAX_ARTIFACT_BYTES:
            raise CompatibilityCheckError(
                "artifact", f"{name} exceeds the {MAX_ARTIFACT_BYTES}-byte limit"
            )
        path = directory / name
        path.write_bytes(content)
        metadata.append(
            ArtifactMetadata(name, len(content), hashlib.sha256(content).hexdigest())
        )
    return tuple(metadata)


def _read_fixture(directory: Path, name: str) -> str:
    return (directory / name).read_text(encoding="utf-8")


def _read_json_fixture(directory: Path, name: str) -> dict[str, Any]:
    value = json.loads(_read_fixture(directory, name))
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return value


def _check(
    identifier: str,
    category: str,
    passed: bool,
    detail: str,
    source: str,
) -> KimiContractCheck:
    return KimiContractCheck(
        identifier, category, "pass" if passed else "fail", detail, source
    )


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _decode_content(value: Mapping[str, Any]) -> str:
    content = value.get("content")
    if not isinstance(content, str):
        raise RuntimeError("GitHub content response has no encoded content")
    return base64.b64decode(content).decode()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check", help="run a fixture or isolated live compatibility check"
    )
    check.add_argument(
        "--fixture",
        type=Path,
        help="directory containing CLI text and OpenAPI/AsyncAPI fixtures",
    )
    check.add_argument(
        "--version",
        help="install an explicit kimi-code version instead of latest",
    )
    check.add_argument(
        "--report",
        type=Path,
        default=Path("compatibility-report.json"),
    )
    check.add_argument("--artifacts", type=Path)

    contract = subparsers.add_parser(
        "contract", help="print the tracked semantic contract as JSON"
    )
    contract.add_argument("--output", type=Path)

    sync = subparsers.add_parser(
        "sync", help="quietly synchronize per-platform reports with GitHub"
    )
    sync.add_argument(
        "--report",
        type=Path,
        action="append",
        required=True,
        dest="reports",
        help="path to one per-platform report; repeat for each platform",
    )
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="print the predicted decision without touching GitHub",
    )
    sync.add_argument(
        "--repository", default=os.environ.get("GITHUB_REPOSITORY")
    )
    sync.add_argument(
        "--default-branch",
        default=os.environ.get("GITHUB_DEFAULT_BRANCH", "main"),
    )
    sync.add_argument("--run-url", default=os.environ.get("GITHUB_RUN_URL"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "contract":
        rendered = json.dumps(
            kimi_semantic_contract(), indent=2, sort_keys=True
        ) + "\n"
        if args.output is None:
            print(rendered, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        return 0

    if args.command == "check":
        if args.fixture is not None and args.version is not None:
            raise SystemExit("--version cannot be combined with --fixture")
        report = run_cli_check(
            fixture_directory=args.fixture,
            version=args.version,
            report_path=args.report,
            artifact_directory=args.artifacts,
        )
        outcome = "compatible" if report.compatible else "incompatible"
        print(
            f"kimi-code {report.version}: {outcome}; "
            f"report {report.report_digest}"
        )
        return 0 if report.compatible else 1

    reports = [read_report(path) for path in args.reports]
    summary = summarize_reports(reports)
    if args.dry_run:
        actions = synchronize_reports(reports, DryRunAutomation())
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not args.repository or not token:
            raise SystemExit("sync requires GITHUB_REPOSITORY and GITHUB_TOKEN")
        with GitHubApiAutomation(
            args.repository,
            token,
            default_branch=args.default_branch,
            run_url=args.run_url,
        ) as automation:
            actions = synchronize_reports(reports, automation)
    outcome = "compatible" if summary.compatible else "incompatible"
    print(
        f"kimi-code {summary.version} on "
        f"{', '.join(summary.platforms)}: {outcome}"
    )
    if actions:
        print("\n".join(actions))
    return 0 if summary.compatible else 1


if __name__ == "__main__":
    raise SystemExit(main())
