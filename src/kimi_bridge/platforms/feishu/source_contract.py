"""Credential-free projections of Feishu documentation and SDK surfaces."""

from __future__ import annotations

import io
import json
import zipfile

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


_DOCUMENTATION = (
    (
        "feishu.docs.image-create",
        SourceRequest(
            "https://open.feishu.cn/document/server-docs/im-v1/image/create.md",
            "live",
        ),
        (
            "/open-apis/im/v1/images",
            "post",
            "tenant_access_token",
            "image_type",
            "image_key",
        ),
    ),
    (
        "feishu.docs.file-create",
        SourceRequest(
            "https://open.feishu.cn/document/server-docs/im-v1/file/create.md",
            "live",
        ),
        (
            "/open-apis/im/v1/files",
            "post",
            "tenant_access_token",
            "file_type",
            "file_key",
        ),
    ),
    (
        "feishu.docs.message-create",
        SourceRequest(
            "https://open.feishu.cn/document/server-docs/im-v1/message/create.md",
            "live",
        ),
        (
            "/open-apis/im/v1/messages",
            "post",
            "receive_id_type",
            "receive_id",
            "msg_type",
            "content",
            "message_id",
        ),
    ),
)
_PYPI_METADATA = SourceRequest(
    "https://pypi.org/pypi/lark-oapi/json", "latest-metadata"
)
_WHEEL_LIMIT = 12 * 1024 * 1024
_SDK_SURFACES = {
    "image": (
        "/open-apis/im/v1/images",
        "image_type",
        "image_key",
    ),
    "file": (
        "/open-apis/im/v1/files",
        "file_type",
        "file_key",
    ),
    "message": (
        "/open-apis/im/v1/messages",
        "receive_id_type",
        "receive_id",
        "msg_type",
        "content",
        "message_id",
    ),
}


async def inspect(fetcher: SourceFetcher) -> PlatformInspection:
    sources: list[SourceEvidence] = []
    checks: list[SourceCheck] = []
    for identifier, request, required in _DOCUMENTATION:
        source = await fetch_for_check(
            fetcher, request, identifier, sources, checks
        )
        if source is not None:
            checks.append(
                token_check(
                    identifier,
                    source,
                    required,
                    matched_detail="official Markdown documents the required operation",
                )
            )

    metadata = await fetch_for_check(
        fetcher, _PYPI_METADATA, "feishu.sdk-metadata", sources, checks
    )
    if metadata is not None:
        wheel_request, expected_digest = _published_wheel(metadata, checks)
        if wheel_request is not None:
            wheel = await fetch_for_check(
                fetcher, wheel_request, "feishu.sdk-wheel", sources, checks
            )
            if wheel is not None:
                if wheel.sha256 != expected_digest:
                    checks.append(
                        SourceCheck(
                            "feishu.sdk-wheel",
                            "drift",
                            "published wheel digest does not match PyPI metadata",
                            (metadata.url, wheel.url),
                        )
                    )
                else:
                    checks.extend(_inspect_wheel(wheel))
    return PlatformInspection("feishu", tuple(sources), tuple(checks))


def _published_wheel(
    source: FetchedSource, checks: list[SourceCheck]
) -> tuple[SourceRequest | None, str]:
    try:
        payload = json.loads(source.text())
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        checks.append(
            SourceCheck(
                "feishu.sdk-metadata",
                "drift",
                "PyPI metadata is not a JSON object",
                (source.url,),
            )
        )
        return None, ""
    info = payload.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    releases = payload.get("urls")
    candidates = (
        [
            item
            for item in releases
            if isinstance(item, dict)
            and item.get("packagetype") == "bdist_wheel"
            and isinstance(item.get("filename"), str)
            and item["filename"].endswith("-py3-none-any.whl")
        ]
        if isinstance(releases, list)
        else []
    )
    wheel = min(candidates, key=lambda item: str(item["filename"])) if candidates else None
    url = wheel.get("url") if isinstance(wheel, dict) else None
    digests = wheel.get("digests") if isinstance(wheel, dict) else None
    digest = digests.get("sha256") if isinstance(digests, dict) else None
    if not (
        isinstance(version, str)
        and version
        and isinstance(url, str)
        and url
        and isinstance(digest, str)
        and len(digest) == 64
    ):
        checks.append(
            SourceCheck(
                "feishu.sdk-metadata",
                "drift",
                "PyPI metadata omitted the latest universal wheel or its digest",
                (source.url,),
            )
        )
        return None, ""
    checks.append(
        SourceCheck(
            "feishu.sdk-metadata",
            "matched",
            f"PyPI publishes lark-oapi {version} as a digest-addressed wheel",
            (source.url,),
        )
    )
    return SourceRequest(url, version, _WHEEL_LIMIT), digest.lower()


def _inspect_wheel(source: FetchedSource) -> tuple[SourceCheck, ...]:
    checks: list[SourceCheck] = []
    try:
        with zipfile.ZipFile(io.BytesIO(source.body)) as archive:
            names = set(archive.namelist())
            for operation, required in _SDK_SURFACES.items():
                paths = _sdk_paths(operation)
                missing_paths = tuple(path for path in paths if path not in names)
                if missing_paths:
                    checks.append(
                        SourceCheck(
                            f"feishu.sdk.{operation}",
                            "drift",
                            "published SDK wheel omitted generated surfaces: "
                            + ", ".join(missing_paths),
                            (source.url,),
                        )
                    )
                    continue
                combined = b"\n".join(_bounded_member(archive, path) for path in paths)
                projection = FetchedSource(
                    source.url,
                    source.reference,
                    combined,
                    "text/x-python",
                )
                checks.append(
                    token_check(
                        f"feishu.sdk.{operation}",
                        projection,
                        ("async def acreate", "httpmethod.post", "accesstokentype.tenant", *required),
                        matched_detail="published SDK retains the async builder and response surface",
                    )
                )
    except (ValueError, zipfile.BadZipFile, RuntimeError):
        return (
            SourceCheck(
                "feishu.sdk-wheel",
                "drift",
                "published SDK artifact is not a bounded readable wheel",
                (source.url,),
            ),
        )
    return tuple(checks)


def _sdk_paths(operation: str) -> tuple[str, ...]:
    prefix = "lark_oapi/api/im/v1"
    return (
        f"{prefix}/resource/{operation}.py",
        f"{prefix}/model/create_{operation}_request.py",
        f"{prefix}/model/create_{operation}_request_body.py",
        f"{prefix}/model/create_{operation}_response.py",
        f"{prefix}/model/create_{operation}_response_body.py",
    )


def _bounded_member(archive: zipfile.ZipFile, path: str) -> bytes:
    info = archive.getinfo(path)
    if info.file_size > 1024 * 1024:
        raise RuntimeError("generated SDK source member exceeds inspection limit")
    return archive.read(info)
