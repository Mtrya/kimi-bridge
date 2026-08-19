"""Credential-free projections of the official QQ bot documentation source."""

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


_COMMIT = SourceRequest(
    "https://api.github.com/repos/tencent-connect/bot-docs/commits?per_page=1",
    "default-branch",
    512 * 1024,
)
_DOCUMENTS = (
    (
        "qq.access-token",
        "docs/develop/api-v2/dev-prepare/interface-framework/api-use.md",
        (
            "https://bots.qq.com/app/getappaccesstoken",
            "appid",
            "clientsecret",
            "access_token",
            "expires_in",
        ),
    ),
    (
        "qq.gateway",
        "docs/develop/api-v2/dev-prepare/interface-framework/event-emit.md",
        (
            "/gateway",
            "heartbeat_interval",
            "session_id",
            "qqbot",
        ),
    ),
    (
        "qq.c2c-message",
        "docs/develop/api-v2/server-inter/message/send-receive/send.md",
        ("msg_type", "content", "msg_id", "msg_seq"),
    ),
    (
        "qq.rich-media",
        "docs/develop/api-v2/server-inter/message/send-receive/rich-media.md",
        ("file_type", "file_info", "srv_send_msg", "msg_type", "media"),
    ),
)
_RAW_ROOT = "https://raw.githubusercontent.com/tencent-connect/bot-docs"


async def inspect(fetcher: SourceFetcher) -> PlatformInspection:
    sources: list[SourceEvidence] = []
    checks: list[SourceCheck] = []
    metadata = await fetch_for_check(
        fetcher, _COMMIT, "qq.source-reference", sources, checks
    )
    if metadata is None:
        return PlatformInspection("qq", tuple(sources), tuple(checks))
    commit = _commit_sha(metadata)
    if commit is None:
        checks.append(
            SourceCheck(
                "qq.source-reference",
                "drift",
                "official repository metadata omitted a full commit SHA",
                (metadata.url,),
            )
        )
        return PlatformInspection("qq", tuple(sources), tuple(checks))
    checks.append(
        SourceCheck(
            "qq.source-reference",
            "matched",
            f"official documentation default branch resolves to {commit[:12]}",
            (metadata.url,),
        )
    )

    rich_media: FetchedSource | None = None
    for identifier, path, required in _DOCUMENTS:
        request = SourceRequest(f"{_RAW_ROOT}/{commit}/{path}", commit)
        source = await fetch_for_check(
            fetcher, request, identifier, sources, checks
        )
        if source is None:
            continue
        checks.append(_document_check(identifier, source, required))
        if identifier == "qq.rich-media":
            rich_media = source
    if rich_media is not None:
        checks.append(_file_data_check(rich_media))
    return PlatformInspection("qq", tuple(sources), tuple(checks))


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


def _document_check(
    identifier: str, source: FetchedSource, required: tuple[str, ...]
) -> SourceCheck:
    check = token_check(
        identifier,
        source,
        required,
        matched_detail="official documentation source retains the required fields",
    )
    if check.state != "matched":
        return check
    text = source.text().casefold()
    endpoint, endpoint_label = (
        (r"/v2/users/\{[^}]+\}/messages", "C2C message")
        if identifier == "qq.c2c-message"
        else (r"/v2/users/\{[^}]+\}/files", "C2C rich-media file")
        if identifier == "qq.rich-media"
        else (None, "")
    )
    if endpoint is not None and re.search(endpoint, text) is None:
        return SourceCheck(
            identifier,
            "drift",
            f"official documentation source omitted the {endpoint_label} endpoint shape",
            (source.url,),
        )
    return check


def _file_data_check(source: FetchedSource) -> SourceCheck:
    try:
        text = source.text().casefold()
    except UnicodeDecodeError:
        text = ""
    if "file_data" in text and not any(
        "file_data" in line and "暂未支持" in line for line in text.splitlines()
    ):
        return SourceCheck(
            "qq.media.file-data",
            "matched",
            "official rich-media documentation now describes file_data",
            (source.url,),
        )
    return SourceCheck(
        "qq.media.file-data",
        "unverifiable",
        "file_data remains an authenticated runtime-only behavior without a supported public source contract",
        (source.url,),
    )
