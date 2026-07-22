"""Deterministic compatibility checks and quiet GitHub synchronization."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from .compatibility import (
    KimiProduct,
    SUPPORTED_KIMI_CODE_VERSIONS,
    identify_kimi_executable,
    kimi_code_version_sort_key,
    normalize_kimi_code_version,
)
from .kimi_server import (
    KIMI_REQUIRED_WEB_FLAGS,
    KIMI_SEMANTIC_CONTRACT_VERSION,
    KimiCompatibilityProbe,
    KimiContractCheck,
    KimiServerStartupError,
    KimiServerSupervisor,
    evaluate_kimi_semantic_contract,
    probe_kimi_compatibility,
)


REPORT_SCHEMA_VERSION = 1
OFFICIAL_KIMI_INSTALLER_URL = "https://code.kimi.com/kimi-code/install.sh"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
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

    def recover_drift_issue(self, report: CompatibilityReport) -> bool: ...

    def promote_version(self, report: CompatibilityReport) -> str | None: ...

    def record_drift(self, report: CompatibilityReport) -> str: ...


class CompatibilityCheckError(RuntimeError):
    def __init__(self, category: str, detail: str) -> None:
        super().__init__(redact(detail))
        self.category = category


def redact(value: str, *, limit: int = 2000) -> str:
    """Remove known credential forms and bound diagnostic text."""

    redacted = _BEARER_RE.sub(r"\1<redacted>", value)
    redacted = _FRAGMENT_TOKEN_RE.sub("<redacted>", redacted)
    redacted = _JSON_TOKEN_RE.sub(r"\1<redacted>", redacted)
    redacted = _QUERY_SECRET_RE.sub(r"\1<redacted>", redacted)
    if len(redacted) > limit:
        return redacted[:limit] + "...<truncated>"
    return redacted


def build_report(
    *,
    mode: str,
    product: str,
    version: str,
    checks: Sequence[KimiContractCheck],
    artifacts: Sequence[ArtifactMetadata] = (),
) -> CompatibilityReport:
    """Normalize checks and compute digests independently of map ordering."""

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
    installer_url: str = OFFICIAL_KIMI_INSTALLER_URL,
    runner: CommandRunner | None = None,
    startup_timeout: float = 30.0,
) -> CompatibilityReport:
    """Install and probe Kimi in a disposable credential-free home."""

    command_runner = runner or _run_command
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
            )
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
    )


def install_official_kimi(
    root: Path,
    *,
    version: str | None,
    installer_url: str,
    runner: CommandRunner,
) -> InstalledKimi:
    """Download an inspectable installer file and execute it in isolation."""

    installer = root / "install.sh"
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
    _run_checked(
        runner,
        ["bash", str(installer)],
        env=environment,
        timeout=180.0,
        category="installer-execution",
    )
    executable = install_directory / "bin" / "kimi"
    if not executable.is_file():
        raise CompatibilityCheckError(
            "installer-execution",
            "official installer completed without creating bin/kimi",
        )
    return InstalledKimi(executable, environment)


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


def synchronize_report(
    report: CompatibilityReport, automation: GitHubAutomation
) -> tuple[str, ...]:
    """Apply the quiet pass/promotion/drift/recovery decision tree."""

    actions: list[str] = []
    if report.compatible:
        if automation.recover_drift_issue(report):
            actions.append("closed-recovered-drift-issue")
        if not report.supported:
            promotion = automation.promote_version(report)
            if promotion is not None:
                actions.append(promotion)
    else:
        actions.append(automation.record_drift(report))
    return tuple(actions)


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

    def recover_drift_issue(self, report: CompatibilityReport) -> bool:
        issue = self._find_drift_issue()
        if issue is None or issue.get("state") != "open":
            return False
        number = int(issue["number"])
        self._request(
            "POST",
            f"/repos/{self.repository}/issues/{number}/comments",
            json={
                "body": (
                    f"Compatibility recovered with kimi-code {report.version}."
                    + self._run_link_suffix()
                )
            },
        )
        self._request(
            "PATCH",
            f"/repos/{self.repository}/issues/{number}",
            json={"state": "closed", "state_reason": "completed"},
        )
        return True

    def promote_version(self, report: CompatibilityReport) -> str | None:
        state_marker = (
            f"<!-- version:{report.version} "
            f"report-digest:{report.report_digest} -->"
        )
        pull = self._find_promotion_pull()
        if pull is not None and state_marker in str(pull.get("body", "")):
            return "unchanged-promotion-pr"
        manifest_path = "src/kimi_bridge/supported-kimi-code-versions.json"
        current = self._get_content(manifest_path, self.default_branch)
        payload = json.loads(_decode_content(current))
        versions = payload.get("versions")
        if not isinstance(versions, list):
            raise RuntimeError("supported-version manifest is malformed")
        if report.version in versions:
            return None
        base_sha = self._branch_sha(self.default_branch)
        self._set_automation_branch(base_sha)
        payload["versions"] = sorted(
            {*versions, report.version}, key=kimi_code_version_sort_key
        )
        branch_content = self._get_content(manifest_path, AUTOMATION_BRANCH)
        encoded = base64.b64encode(
            (json.dumps(payload, indent=2) + "\n").encode()
        ).decode()
        self._request(
            "PUT",
            f"/repos/{self.repository}/contents/{manifest_path}",
            json={
                "message": f"chore: support kimi-code {report.version}",
                "content": encoded,
                "sha": branch_content["sha"],
                "branch": AUTOMATION_BRANCH,
            },
        )
        title = f"chore: support kimi-code {report.version}"
        body = (
            f"{PROMOTION_MARKER}\n{state_marker}\n\n"
            f"The credential-free compatibility canary passed for kimi-code "
            f"{report.version}.\n\n"
            f"Report digest: `{report.report_digest}`."
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

    def record_drift(self, report: CompatibilityReport) -> str:
        issue = self._find_drift_issue()
        state_marker = (
            f"<!-- version:{report.version} "
            f"failure-digest:{report.failure_digest} -->"
        )
        if (
            issue is not None
            and issue.get("state") == "open"
            and state_marker in str(issue.get("body", ""))
        ):
            return "unchanged-drift-issue"
        self._ensure_drift_label()
        summary = "\n".join(
            f"- **{item['category']}** `{item['id']}`: {item['detail']}"
            for item in report.failures
        )
        body = (
            f"{DRIFT_MARKER}\n{state_marker}\n\n"
            f"The credential-free canary found required behavior drift in "
            f"kimi-code {report.version}.\n\n{summary}\n\n"
            f"Report digest: `{report.report_digest}`."
            + self._run_link_suffix()
        )
        title = f"Kimi compatibility drift: {report.version}"
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
    return subprocess.run(
        list(command),
        env=dict(env) if env is not None else None,
        timeout=timeout,
        check=False,
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
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompatibilityCheckError(category, str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "no output"
        raise CompatibilityCheckError(
            category,
            f"{command[0]} exited with status {result.returncode}: {detail}",
        )


def _isolated_environment(
    *,
    home: Path,
    install_directory: Path,
    kimi_home: Path,
    version: str | None,
) -> dict[str, str]:
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
    environment["PATH"] = (
        f"{install_directory / 'bin'}:{environment.get('PATH', '/usr/bin:/bin')}"
    )
    if version is not None:
        environment["KIMI_VERSION"] = version
    return environment


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
