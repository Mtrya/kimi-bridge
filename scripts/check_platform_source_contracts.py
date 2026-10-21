#!/usr/bin/env python3
"""Inspect public platform sources without platform credentials or API calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from kimi_bridge.platforms.feishu import source_contract as feishu_contract
from kimi_bridge.platforms.qq import source_contract as qq_contract
from kimi_bridge.platforms.source_contract import (
    FetchedSource,
    SourceFetchError,
    SourceFetcher,
    SourceMonitorReport,
    SourceRequest,
    monitor_report_from_dict,
)
from kimi_bridge.platforms.telegram.source_contract import (
    inspect as inspect_telegram,
)
from kimi_bridge.platforms.wechat import source_contract as wechat_contract


_ALLOWED_SOURCE_HOSTS = frozenset(
    {
        "api.github.com",
        "core.telegram.org",
        "files.pythonhosted.org",
        "open.feishu.cn",
        "pypi.org",
        "raw.githubusercontent.com",
    }
)
_MAX_SOURCE_BYTES = 12 * 1024 * 1024
_MAX_ERROR_BYTES = 8 * 1024
_MAX_REDIRECTS = 3
_FETCH_ATTEMPTS = 3
_RETRY_DELAY_BUDGET = 60.0
_ISSUE_MARKER = "<!-- kimi-bridge:platform-source-monitor"
_ISSUE_LABEL = "upstream-drift"
_ISSUE_TITLE = "Platform source-contract drift detected"


class _RetryableFetchError(SourceFetchError):
    def __init__(self, message: str, *, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PublicSourceFetcher:
    """Bounded client that can reach only the monitor's official HTTPS hosts."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        github_token: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": "kimi-bridge-source-contract-monitor"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._github_token = github_token
        self._sleep = sleep

    async def __aenter__(self) -> PublicSourceFetcher:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, request: SourceRequest) -> FetchedSource:
        _validate_source_request(request)
        last_error: _RetryableFetchError | None = None
        remaining_delay = _RETRY_DELAY_BUDGET
        for attempt in range(_FETCH_ATTEMPTS):
            try:
                return await self._fetch_once(request)
            except _RetryableFetchError as exc:
                last_error = exc
                if attempt + 1 < _FETCH_ATTEMPTS:
                    delay = max(0.5 * (2**attempt), exc.retry_after)
                    if delay > remaining_delay:
                        raise SourceFetchError(
                            f"{exc}; retry delay {delay:g}s exceeds the remaining "
                            f"{remaining_delay:g}s retry budget"
                        ) from exc
                    await self._sleep(delay)
                    remaining_delay -= delay
        assert last_error is not None
        raise SourceFetchError(
            f"{_display_url(request.url)} remained unavailable after "
            f"{_FETCH_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    async def _fetch_once(self, request: SourceRequest) -> FetchedSource:
        url = request.url
        for redirect_count in range(_MAX_REDIRECTS + 1):
            _validate_https_url(url)
            headers = {}
            if urlsplit(url).hostname == "api.github.com" and self._github_token:
                headers["Authorization"] = f"Bearer {self._github_token}"
            try:
                response = await self._client.send(
                    self._client.build_request("GET", url, headers=headers),
                    stream=True,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise _RetryableFetchError(
                    f"transport failure for {_display_url(url)}"
                ) from exc
            try:
                retry_after = _rate_limit_delay(response)
                if retry_after is None and await _is_github_secondary_limit(response):
                    retry_after = 60.0
                if retry_after is not None or 500 <= response.status_code < 600:
                    raise _RetryableFetchError(
                        _http_failure_detail(response),
                        retry_after=retry_after or 0.0,
                    )
                if response.is_redirect:
                    location = response.headers.get("location")
                    if location is None:
                        raise SourceFetchError(
                            f"redirect from {_display_url(url)} omitted Location"
                        )
                    if redirect_count == _MAX_REDIRECTS:
                        raise SourceFetchError(
                            f"too many redirects from {_display_url(request.url)}"
                        )
                    url = urljoin(url, location)
                    _validate_https_url(url)
                    continue
                if response.status_code != 200:
                    raise SourceFetchError(_http_failure_detail(response))
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        announced_size = int(content_length)
                    except ValueError as exc:
                        raise SourceFetchError(
                            f"invalid Content-Length from {_display_url(url)}"
                        ) from exc
                    if announced_size > request.max_bytes:
                        raise SourceFetchError(
                            f"{_display_url(url)} exceeds the {request.max_bytes}-byte limit"
                        )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > request.max_bytes:
                        raise SourceFetchError(
                            f"{_display_url(url)} exceeds the {request.max_bytes}-byte limit"
                        )
                content_type = response.headers.get("content-type")
                return FetchedSource(
                    url,
                    request.reference,
                    bytes(body),
                    content_type.split(";", 1)[0] if content_type else None,
                )
            finally:
                await response.aclose()
        raise AssertionError("redirect loop must return or raise")


def _http_failure_detail(response: httpx.Response) -> str:
    detail = f"HTTP {response.status_code} from {_display_url(str(response.url))}"
    diagnostics = ", ".join(
        f"{name}={response.headers[name]}"
        for name in (
            "retry-after",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
            "x-github-request-id",
        )
        if name in response.headers
    )
    return f"{detail} ({diagnostics})" if diagnostics else detail


async def _is_github_secondary_limit(response: httpx.Response) -> bool:
    if response.url.host != "api.github.com" or response.status_code != 403:
        return False
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_ERROR_BYTES:
                return False
            body.extend(chunk)
    except httpx.TransportError as exc:
        raise _RetryableFetchError(_http_failure_detail(response)) from exc
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    message = payload.get("message") if isinstance(payload, dict) else None
    return isinstance(message, str) and "secondary rate limit" in message.lower()


def _rate_limit_delay(response: httpx.Response) -> float | None:
    github = response.url.host == "api.github.com"
    remaining = response.headers.get("x-ratelimit-remaining")
    retry_after = response.headers.get("retry-after")
    if response.status_code != 429 and not (
        github
        and response.status_code == 403
        and (remaining == "0" or retry_after is not None)
    ):
        return None

    delays = []
    if retry_after is not None:
        try:
            seconds = int(retry_after)
            if seconds >= 0:
                delays.append(float(seconds))
        except (ValueError, OverflowError):
            try:
                delays.append(
                    parsedate_to_datetime(retry_after).timestamp() - time.time()
                )
            except (TypeError, ValueError, OverflowError):
                pass
    if github and remaining == "0":
        try:
            # GitHub requires waiting until after the reset timestamp.
            delays.append(float(int(response.headers["x-ratelimit-reset"])) - time.time() + 1)
        except (KeyError, ValueError, OverflowError):
            pass
    # GitHub asks for at least one minute when a limit has no retry timing.
    return max(0.0, *delays) if delays else (60.0 if github else 0.0)


async def run_inspections(fetcher: SourceFetcher) -> SourceMonitorReport:
    inspections = await asyncio.gather(
        feishu_contract.inspect(fetcher),
        qq_contract.inspect(fetcher),
        wechat_contract.inspect(fetcher),
        inspect_telegram(fetcher),
    )
    checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return SourceMonitorReport(checked_at, tuple(inspections))


async def run_live_check() -> SourceMonitorReport:
    async with PublicSourceFetcher(
        github_token=os.environ.get("GITHUB_TOKEN")
    ) as fetcher:
        report = await run_inspections(fetcher)
        if any(
            check.state == "drift"
            for platform in report.platforms
            for check in platform.checks
        ):
            await asyncio.sleep(2.0)
            report = await run_inspections(fetcher)
        return report


def write_report(report: SourceMonitorReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_report(path: Path) -> SourceMonitorReport:
    return monitor_report_from_dict(json.loads(path.read_text(encoding="utf-8")))


class GitHubIssueAutomation:
    def __init__(
        self,
        repository: str,
        token: str,
        *,
        run_url: str | None = None,
        api_url: str = "https://api.github.com",
        client: httpx.Client | None = None,
    ) -> None:
        if repository.count("/") != 1:
            raise ValueError("repository must have owner/name form")
        self.repository = repository
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

    def __enter__(self) -> GitHubIssueAutomation:
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._owns_client:
            self._client.close()

    def synchronize(self, report: SourceMonitorReport) -> str:
        if report.healthy:
            issue = self._find_issue()
            if issue is None or issue.get("state") != "open":
                return "healthy-no-action"
            number = int(issue["number"])
            self._request(
                "POST",
                f"/repos/{self.repository}/issues/{number}/comments",
                json={"body": "Public source contracts recovered." + self._run_suffix()},
            )
            self._request(
                "PATCH",
                f"/repos/{self.repository}/issues/{number}",
                json={"state": "closed", "state_reason": "completed"},
            )
            return "closed-recovered-source-drift"

        self._ensure_label()
        issue = self._find_issue()
        body = render_issue_body(report, run_url=self.run_url)
        if issue is None:
            self._request(
                "POST",
                f"/repos/{self.repository}/issues",
                json={"title": _ISSUE_TITLE, "body": body, "labels": [_ISSUE_LABEL]},
            )
            return "opened-source-drift-issue"
        number = int(issue["number"])
        existing_body = issue.get("body")
        if (
            issue.get("state") == "open"
            and isinstance(existing_body, str)
            and f"digest={report.alert_digest}" in existing_body
            and (
                not any(
                    check.state == "unavailable"
                    for platform in report.platforms
                    for check in platform.checks
                )
                or f"report={report.report_digest}" in existing_body
            )
        ):
            return "source-drift-unchanged"
        self._request(
            "PATCH",
            f"/repos/{self.repository}/issues/{number}",
            json={
                "title": _ISSUE_TITLE,
                "body": body,
                "labels": [_ISSUE_LABEL],
                "state": "open",
            },
        )
        return "updated-source-drift-issue"

    def _find_issue(self) -> dict[str, Any] | None:
        issues = self._request(
            "GET",
            f"/repos/{self.repository}/issues",
            params={"state": "all", "labels": _ISSUE_LABEL, "per_page": 100},
        )
        if not isinstance(issues, list):
            raise RuntimeError("GitHub issues response is not a list")
        return next(
            (
                issue
                for issue in issues
                if isinstance(issue, dict)
                and "pull_request" not in issue
                and isinstance(issue.get("body"), str)
                and _ISSUE_MARKER in issue["body"]
            ),
            None,
        )

    def _ensure_label(self) -> None:
        path = f"/repos/{self.repository}/labels/{quote(_ISSUE_LABEL, safe='')}"
        response = self._client.get(path)
        if response.status_code == 404:
            self._request(
                "POST",
                f"/repos/{self.repository}/labels",
                json={
                    "name": _ISSUE_LABEL,
                    "color": "B60205",
                    "description": "Confirmed drift in a public upstream contract",
                },
            )
            return
        response.raise_for_status()

    def _run_suffix(self) -> str:
        return f"\n\n[Workflow run]({self.run_url})" if self.run_url else ""

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()


def render_issue_body(
    report: SourceMonitorReport, *, run_url: str | None = None
) -> str:
    lines = [
        f"{_ISSUE_MARKER} digest={report.alert_digest} report={report.report_digest} -->",
        "The daily public-source monitor found a public upstream contract that no longer matches, or could not be inspected after bounded retries.",
        "",
        f"Checked: `{report.checked_at}`",
        "",
        "| Platform | Check | Result | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for platform in report.platforms:
        for check in platform.checks:
            if check.state not in {"drift", "unavailable"}:
                continue
            lines.append(
                f"| {platform.platform} | `{check.identifier}` | {check.state} | {_table_text(check.detail)} |"
            )
    unverifiable = [
        (platform.platform, check)
        for platform in report.platforms
        for check in platform.checks
        if check.state == "unverifiable"
    ]
    if unverifiable:
        lines.extend(["", "Unverifiable public-source gaps:"])
        lines.extend(
            f"- {platform}: `{check.identifier}` — {check.detail}"
            for platform, check in unverifiable
        )
    lines.extend(["", "Source provenance:"])
    for platform in report.platforms:
        for source in platform.sources:
            lines.append(
                f"- {platform.platform}: [{source.url}]({source.url}) at `{source.reference}` (`sha256:{source.sha256[:16]}…`, {source.size} bytes)"
            )
    if run_url:
        lines.extend(["", f"[Workflow run]({run_url})"])
    return "\n".join(lines) + "\n"


def _validate_source_request(request: SourceRequest) -> None:
    if request.max_bytes <= 0 or request.max_bytes > _MAX_SOURCE_BYTES:
        raise SourceFetchError("source byte limit is outside the monitor allowance")
    _validate_https_url(request.url)


def _validate_https_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SourceFetchError("source URL has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_SOURCE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise SourceFetchError("source URL is not an allowlisted HTTPS resource")


def _display_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _table_text(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="inspect the public official sources")
    check.add_argument(
        "--report", type=Path, default=Path("platform-source-report.json")
    )
    check.add_argument(
        "--report-only",
        action="store_true",
        help="return success after writing the report even when it contains alerts",
    )
    sync = commands.add_parser("sync", help="synchronize the rolling drift issue")
    sync.add_argument("--report", type=Path, required=True)
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    sync.add_argument("--run-url", default=os.environ.get("GITHUB_RUN_URL"))
    sync.add_argument(
        "--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "check":
        report = asyncio.run(run_live_check())
        write_report(report, args.report)
        for platform in report.platforms:
            print(f"{platform.platform}: {platform.outcome}")
        print(f"report: {report.report_digest}")
        return 0 if args.report_only or report.healthy else 1

    report = read_report(args.report)
    if args.dry_run:
        action = "healthy-no-action" if report.healthy else "would-record-source-drift"
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not args.repository or not token:
            raise SystemExit("sync requires GITHUB_REPOSITORY and GITHUB_TOKEN")
        with GitHubIssueAutomation(
            args.repository,
            token,
            run_url=args.run_url,
            api_url=args.api_url,
        ) as automation:
            action = automation.synchronize(report)
    print(action)
    # Contract health belongs to `check`; reaching here means synchronization worked.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
