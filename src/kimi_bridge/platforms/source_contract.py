"""Shared result and fetch contracts for public platform-source inspection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast


SourceState = Literal["matched", "drift", "unavailable", "unverifiable"]


class SourceFetchError(RuntimeError):
    """A bounded public source could not be fetched safely."""


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """One allowlisted public HTTPS resource."""

    url: str
    reference: str
    max_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """Non-content provenance retained in a monitor report."""

    url: str
    reference: str
    sha256: str
    size: int
    content_type: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "reference": self.reference,
            "sha256": self.sha256,
            "size": self.size,
            "content_type": self.content_type,
        }


@dataclass(frozen=True, slots=True)
class FetchedSource:
    """One bounded source body and its report-safe provenance."""

    url: str
    reference: str
    body: bytes
    content_type: str | None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self) -> str:
        return self.body.decode("utf-8-sig")

    def evidence(self) -> SourceEvidence:
        return SourceEvidence(
            url=self.url,
            reference=self.reference,
            sha256=self.sha256,
            size=len(self.body),
            content_type=self.content_type,
        )


class SourceFetcher(Protocol):
    async def fetch(self, request: SourceRequest) -> FetchedSource: ...


@dataclass(frozen=True, slots=True)
class SourceCheck:
    identifier: str
    state: SourceState
    detail: str
    sources: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.identifier,
            "state": self.state,
            "detail": self.detail,
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class PlatformInspection:
    platform: str
    sources: tuple[SourceEvidence, ...]
    checks: tuple[SourceCheck, ...]

    @property
    def outcome(self) -> Literal["matched", "drift", "unavailable"]:
        states = {check.state for check in self.checks}
        if "drift" in states:
            return "drift"
        if "unavailable" in states:
            return "unavailable"
        return "matched"

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "outcome": self.outcome,
            "sources": [source.to_dict() for source in self.sources],
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class SourceMonitorReport:
    checked_at: str
    platforms: tuple[PlatformInspection, ...]

    @property
    def healthy(self) -> bool:
        return bool(self.platforms) and all(
            platform.outcome == "matched" for platform in self.platforms
        )

    @property
    def alert_digest(self) -> str:
        alerts: list[dict[str, str]] = []
        for platform in self.platforms:
            for check in platform.checks:
                if check.state not in {"drift", "unavailable"}:
                    continue
                alert = {
                    "platform": platform.platform,
                    "id": check.identifier,
                    "state": check.state,
                }
                if check.state == "drift":
                    alert["detail"] = check.detail
                alerts.append(alert)
        return _digest(alerts)

    @property
    def report_digest(self) -> str:
        return _digest([platform.to_dict() for platform in self.platforms])

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "checked_at": self.checked_at,
            "healthy": self.healthy,
            "alert_digest": self.alert_digest,
            "report_digest": self.report_digest,
            "platforms": [platform.to_dict() for platform in self.platforms],
        }


def token_check(
    identifier: str,
    source: FetchedSource,
    required: tuple[str, ...],
    *,
    matched_detail: str,
) -> SourceCheck:
    """Check protocol tokens without coupling the shared layer to their meaning."""

    try:
        text = source.text().casefold()
    except UnicodeDecodeError:
        return SourceCheck(
            identifier,
            "drift",
            "official source is not valid UTF-8 text",
            (source.url,),
        )
    missing = tuple(token for token in required if token.casefold() not in text)
    if missing:
        return SourceCheck(
            identifier,
            "drift",
            "official source omitted required public contract tokens: "
            + ", ".join(missing),
            (source.url,),
        )
    return SourceCheck(identifier, "matched", matched_detail, (source.url,))


def unavailable_check(
    identifier: str, request: SourceRequest, error: SourceFetchError
) -> SourceCheck:
    return SourceCheck(identifier, "unavailable", str(error), (request.url,))


def monitor_report_from_dict(payload: object) -> SourceMonitorReport:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("source monitor report has an unsupported schema")
    checked_at = payload.get("checked_at")
    raw_platforms = payload.get("platforms")
    if not isinstance(checked_at, str) or not isinstance(raw_platforms, list):
        raise ValueError("source monitor report is malformed")
    platforms: list[PlatformInspection] = []
    for raw_platform in raw_platforms:
        if not isinstance(raw_platform, dict):
            raise ValueError("platform result must be an object")
        platform = raw_platform.get("platform")
        raw_sources = raw_platform.get("sources")
        raw_checks = raw_platform.get("checks")
        if not (
            isinstance(platform, str)
            and isinstance(raw_sources, list)
            and isinstance(raw_checks, list)
        ):
            raise ValueError("platform result is malformed")
        sources = tuple(_source_evidence_from_dict(item) for item in raw_sources)
        checks = tuple(_source_check_from_dict(item) for item in raw_checks)
        platforms.append(PlatformInspection(platform, sources, checks))
    return SourceMonitorReport(checked_at, tuple(platforms))


def _source_evidence_from_dict(payload: object) -> SourceEvidence:
    if not isinstance(payload, dict):
        raise ValueError("source evidence must be an object")
    values: dict[str, Any] = payload
    if not (
        isinstance(values.get("url"), str)
        and isinstance(values.get("reference"), str)
        and isinstance(values.get("sha256"), str)
        and isinstance(values.get("size"), int)
        and (
            values.get("content_type") is None
            or isinstance(values.get("content_type"), str)
        )
    ):
        raise ValueError("source evidence is malformed")
    return SourceEvidence(
        values["url"],
        values["reference"],
        values["sha256"],
        values["size"],
        values.get("content_type"),
    )


def _source_check_from_dict(payload: object) -> SourceCheck:
    if not isinstance(payload, dict):
        raise ValueError("source check must be an object")
    identifier = payload.get("id")
    state = payload.get("state")
    detail = payload.get("detail")
    sources = payload.get("sources")
    if not (
        isinstance(identifier, str)
        and state in {"matched", "drift", "unavailable", "unverifiable"}
        and isinstance(detail, str)
        and isinstance(sources, list)
        and all(isinstance(source, str) for source in sources)
    ):
        raise ValueError("source check is malformed")
    return SourceCheck(
        identifier,
        cast(SourceState, state),
        detail,
        tuple(cast(list[str], sources)),
    )


async def fetch_for_check(
    fetcher: SourceFetcher,
    request: SourceRequest,
    identifier: str,
    sources: list[SourceEvidence],
    checks: list[SourceCheck],
) -> FetchedSource | None:
    try:
        source = await fetcher.fetch(request)
    except SourceFetchError as exc:
        checks.append(unavailable_check(identifier, request, exc))
        return None
    sources.append(source.evidence())
    return source


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
