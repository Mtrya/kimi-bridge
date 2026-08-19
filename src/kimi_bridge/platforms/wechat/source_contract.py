"""Credential-free projections of pinned and latest WeChat iLink source."""

from __future__ import annotations

import json
import re

from ..source_contract import (
    FetchedSource,
    PlatformInspection,
    SourceCheck,
    SourceEvidence,
    SourceFetcher,
    SourceRequest,
    fetch_for_check,
    token_check,
)
from .types import CHANNEL_VERSION, PINNED_SOURCE_COMMIT, PINNED_SOURCE_TAG


_REPOSITORY = "Tencent/openclaw-weixin"
_RAW_ROOT = f"https://raw.githubusercontent.com/{_REPOSITORY}"
_SOURCE_PATHS = ("src/api/api.ts", "src/api/types.ts", "package.json")
_API_TOKENS = (
    "authorizationtype",
    "ilink_bot_token",
    "x-wechat-uin",
    "ilink-app-id",
    "ilink-app-clientversion",
    "authorization",
    "bearer",
    "ilink/bot/getupdates",
    "ilink/bot/sendmessage",
    "ilink/bot/getuploadurl",
    "ilink/bot/getconfig",
    "ilink/bot/sendtyping",
)
_STATE_MEDIA_TOKENS = (
    "get_updates_buf",
    "context_token",
    "item_list",
    "encrypt_query_param",
    "aes_key",
    "encrypt_type",
    "filekey",
    "media_type",
    "filesize",
    "aeskey",
)


async def inspect(fetcher: SourceFetcher) -> PlatformInspection:
    sources: list[SourceEvidence] = []
    checks: list[SourceCheck] = []
    for label, reference, metadata_url in (
        (
            "pinned",
            PINNED_SOURCE_TAG,
            f"https://api.github.com/repos/{_REPOSITORY}/commits/{PINNED_SOURCE_TAG}",
        ),
        (
            "latest",
            "default-branch",
            f"https://api.github.com/repos/{_REPOSITORY}/commits?per_page=1",
        ),
    ):
        metadata_request = SourceRequest(
            metadata_url,
            reference,
            512 * 1024,
        )
        metadata = await fetch_for_check(
            fetcher,
            metadata_request,
            f"wechat.{label}.source-reference",
            sources,
            checks,
        )
        if metadata is None:
            continue
        commit = _commit_sha(metadata)
        if commit is None:
            checks.append(
                SourceCheck(
                    f"wechat.{label}.source-reference",
                    "drift",
                    "official repository metadata omitted a full commit SHA",
                    (metadata.url,),
                )
            )
            continue
        if label == "pinned" and commit != PINNED_SOURCE_COMMIT:
            checks.append(
                SourceCheck(
                    "wechat.pinned.source-reference",
                    "drift",
                    f"{PINNED_SOURCE_TAG} resolved to an unexpected commit",
                    (metadata.url,),
                )
            )
            continue
        checks.append(
            SourceCheck(
                f"wechat.{label}.source-reference",
                "matched",
                f"official {reference} reference resolves to {commit[:12]}",
                (metadata.url,),
            )
        )
        artifacts: dict[str, FetchedSource] = {}
        for path in _SOURCE_PATHS:
            request = SourceRequest(f"{_RAW_ROOT}/{commit}/{path}", commit)
            source = await fetch_for_check(
                fetcher,
                request,
                f"wechat.{label}.source.{path}",
                sources,
                checks,
            )
            if source is not None:
                artifacts[path] = source
        if len(artifacts) == len(_SOURCE_PATHS):
            checks.extend(
                _inspect_reference(
                    label,
                    [artifacts[path] for path in _SOURCE_PATHS],
                    artifacts["package.json"],
                )
            )
    return PlatformInspection("wechat", tuple(sources), tuple(checks))


def _commit_sha(source: FetchedSource) -> str | None:
    try:
        payload = json.loads(source.text())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0]
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha
    return None


def _inspect_reference(
    label: str,
    sources: list[FetchedSource],
    package_manifest: FetchedSource,
) -> tuple[SourceCheck, ...]:
    combined = FetchedSource(
        sources[0].url,
        sources[0].reference,
        b"\n".join(source.body for source in sources),
        "text/plain",
    )
    source_urls = tuple(source.url for source in sources)
    checks = [
        _with_sources(
            token_check(
                f"wechat.{label}.headers-endpoints",
                combined,
                _API_TOKENS,
                matched_detail="official source retains the required headers and endpoints",
            ),
            source_urls,
        ),
        _with_sources(
            token_check(
                f"wechat.{label}.state-media",
                combined,
                _STATE_MEDIA_TOKENS,
                matched_detail="official source retains cursor, context, and media metadata",
            ),
            source_urls,
        ),
    ]
    version = _package_version(package_manifest)
    if version is None:
        checks.append(
            SourceCheck(
                f"wechat.{label}.version",
                "drift",
                "official package metadata omitted a semantic version",
                (package_manifest.url,),
            )
        )
    elif label == "pinned" and version != CHANNEL_VERSION:
        checks.append(
            SourceCheck(
                "wechat.pinned.version",
                "drift",
                f"pinned source reports {version}, expected {CHANNEL_VERSION}",
                (package_manifest.url,),
            )
        )
    else:
        checks.append(
            SourceCheck(
                f"wechat.{label}.version",
                "matched",
                f"official source reports channel version {version}",
                (package_manifest.url,),
            )
        )
    return tuple(checks)


def _package_version(source: FetchedSource) -> str | None:
    try:
        payload = json.loads(source.text())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    if isinstance(version, str) and re.fullmatch(
        r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version
    ):
        return version
    return None


def _with_sources(check: SourceCheck, sources: tuple[str, ...]) -> SourceCheck:
    return SourceCheck(check.identifier, check.state, check.detail, sources)
