#!/usr/bin/env python3
"""Inspect public platform sources without platform credentials or API calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
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
_MAX_REDIRECTS = 3
_FETCH_ATTEMPTS = 3
_ISSUE_MARKER = "<!-- kimi-bridge:platform-source-monitor"
_ISSUE_LABEL = "upstream-drift"
_ISSUE_TITLE = "Platform source-contract drift detected"


class _RetryableFetchError(SourceFetchError):
    pass


class PublicSourceFetcher:
    """Bounded client that can reach only the monitor's official HTTPS hosts."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": "kimi-bridge-source-contract-monitor"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._sleep = sleep

    async def __aenter__(self) -> PublicSourceFetcher:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, request: SourceRequest) -> FetchedSource:
        _validate_source_request(request)
        last_error: _RetryableFetchError | None = None
        for attempt in range(_FETCH_ATTEMPTS):
            try:
                return await self._fetch_once(request)
            except _RetryableFetchError as exc:
                last_error = exc
                if attempt + 1 < _FETCH_ATTEMPTS:
                    await self._sleep(0.5 * (2**attempt))
        assert last_error is not None
        raise SourceFetchError(
            f"{_display_url(request.url)} remained unavailable after "
            f"{_FETCH_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    async def _fetch_once(self, request: SourceRequest) -> FetchedSource:
        url = request.url
        for redirect_count in range(_MAX_REDIRECTS + 1):
            _validate_https_url(url)
            try:
                response = await self._client.send(
                    self._client.build_request("GET", url), stream=True
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise _RetryableFetchError(
                    f"transport failure for {_display_url(url)}"
                ) from exc
            try:
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    raise _RetryableFetchError(
                        f"HTTP {response.status_code} from {_display_url(url)}"
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
                    raise SourceFetchError(
                        f"HTTP {response.status_code} from {_display_url(url)}"
                    )
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
    async with PublicSourceFetcher() as fetcher:
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
        f"{_ISSUE_MARKER} digest={report.alert_digest} -->",
        "The daily credential-free monitor found a public upstream contract that no longer matches, or could not be inspected after bounded retries.",
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
    lines.extend(
        [
            "",
            "This monitor does not call authenticated platform APIs, exercise message delivery, or prepare a release.",
        ]
    )
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
        return 0 if report.healthy else 1

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
    return 0 if report.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
