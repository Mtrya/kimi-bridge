from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from kimi_bridge.platforms.feishu import source_contract as feishu_contract
from kimi_bridge.platforms.qq import source_contract as qq_contract
from kimi_bridge.platforms.source_contract import (
    FetchedSource,
    PlatformInspection,
    SourceCheck,
    SourceEvidence,
    SourceFetchError,
    SourceMonitorReport,
    SourceRequest,
)
from kimi_bridge.platforms.telegram.source_contract import (
    inspect as inspect_telegram,
)
from kimi_bridge.platforms.wechat import source_contract as wechat_contract
from kimi_bridge.platforms.wechat.types import (
    CHANNEL_VERSION,
    PINNED_SOURCE_COMMIT,
    PINNED_SOURCE_TAG,
)


def _load_checker() -> Any:
    script = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "check_platform_source_contracts.py"
    )
    spec = importlib.util.spec_from_file_location(
        "check_platform_source_contracts", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


class MemoryFetcher:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.requests: list[SourceRequest] = []

    async def fetch(self, request: SourceRequest) -> FetchedSource:
        self.requests.append(request)
        body = self.bodies.get(request.url)
        if body is None:
            raise AssertionError(f"unexpected source request: {request.url}")
        return FetchedSource(
            request.url, request.reference, body, "application/octet-stream"
        )


def _encoded(value: object) -> bytes:
    return json.dumps(value).encode()


def _feishu_wheel() -> bytes:
    buffer = io.BytesIO()
    surfaces = {
        "image": ("/open-apis/im/v1/images", "image_type", "image_key"),
        "file": ("/open-apis/im/v1/files", "file_type", "file_key"),
        "message": (
            "/open-apis/im/v1/messages",
            "receive_id_type",
            "receive_id",
            "msg_type",
            "content",
            "message_id",
        ),
    }
    with zipfile.ZipFile(buffer, "w") as archive:
        for operation, fields in surfaces.items():
            root = "lark_oapi/api/im/v1"
            common = "\n".join(fields)
            archive.writestr(
                f"{root}/resource/{operation}.py", "async def acreate(): pass"
            )
            archive.writestr(
                f"{root}/model/create_{operation}_request.py",
                f"HttpMethod.POST AccessTokenType.TENANT {common}",
            )
            archive.writestr(
                f"{root}/model/create_{operation}_request_body.py", common
            )
            archive.writestr(
                f"{root}/model/create_{operation}_response.py", common
            )
            archive.writestr(
                f"{root}/model/create_{operation}_response_body.py", common
            )
    return buffer.getvalue()


async def test_feishu_inspects_public_markdown_and_published_wheel() -> None:
    wheel = _feishu_wheel()
    wheel_url = "https://files.pythonhosted.org/packages/lark_oapi-1.7.2-py3-none-any.whl"
    bodies = {
        "https://open.feishu.cn/document/server-docs/im-v1/image/create.md": b"POST /open-apis/im/v1/images tenant_access_token image_type image_key",
        "https://open.feishu.cn/document/server-docs/im-v1/file/create.md": b"POST /open-apis/im/v1/files tenant_access_token file_type file_key",
        "https://open.feishu.cn/document/server-docs/im-v1/message/create.md": b"POST /open-apis/im/v1/messages receive_id_type receive_id msg_type content message_id",
        "https://pypi.org/pypi/lark-oapi/json": _encoded(
            {
                "info": {"version": "1.7.2"},
                "urls": [
                    {
                        "packagetype": "bdist_wheel",
                        "filename": "lark_oapi-1.7.2-py3-none-any.whl",
                        "url": wheel_url,
                        "digests": {"sha256": hashlib.sha256(wheel).hexdigest()},
                    }
                ],
            }
        ),
        wheel_url: wheel,
    }

    result = await feishu_contract.inspect(MemoryFetcher(bodies))

    assert result.outcome == "matched"
    assert {check.identifier for check in result.checks} >= {
        "feishu.docs.image-create",
        "feishu.sdk.image",
        "feishu.sdk.file",
        "feishu.sdk.message",
    }


async def test_feishu_reports_public_document_shape_loss_as_drift() -> None:
    wheel = _feishu_wheel()
    wheel_url = "https://files.pythonhosted.org/packages/lark_oapi-1.7.2-py3-none-any.whl"
    bodies = {
        "https://open.feishu.cn/document/server-docs/im-v1/image/create.md": b"POST /open-apis/im/v1/images tenant_access_token image_type",
        "https://open.feishu.cn/document/server-docs/im-v1/file/create.md": b"POST /open-apis/im/v1/files tenant_access_token file_type file_key",
        "https://open.feishu.cn/document/server-docs/im-v1/message/create.md": b"POST /open-apis/im/v1/messages receive_id_type receive_id msg_type content message_id",
        "https://pypi.org/pypi/lark-oapi/json": _encoded(
            {
                "info": {"version": "1.7.2"},
                "urls": [
                    {
                        "packagetype": "bdist_wheel",
                        "filename": "lark_oapi-1.7.2-py3-none-any.whl",
                        "url": wheel_url,
                        "digests": {"sha256": hashlib.sha256(wheel).hexdigest()},
                    }
                ],
            }
        ),
        wheel_url: wheel,
    }

    result = await feishu_contract.inspect(MemoryFetcher(bodies))

    check = next(
        check
        for check in result.checks
        if check.identifier == "feishu.docs.image-create"
    )
    assert check.state == "drift"


async def test_qq_keeps_undocumented_file_data_unverifiable() -> None:
    commit = "b" * 40
    root = f"https://raw.githubusercontent.com/tencent-connect/bot-docs/{commit}"
    bodies = {
        "https://api.github.com/repos/tencent-connect/bot-docs/commits?per_page=1": _encoded(
            [{"sha": commit}]
        ),
        f"{root}/docs/develop/api-v2/dev-prepare/access-token.md": b"https://bots.qq.com/app/getAppAccessToken appId clientSecret access_token expires_in",
        f"{root}/docs/develop/api-v2/dev-prepare/event-emit/websocket.md": b"QQBot /gateway heartbeat_interval session_id",
        f"{root}/docs/develop/api-v2/server-inter/message/send-receive/send.md": b"POST /v2/users/{user_openid}/messages msg_type content msg_id msg_seq",
        f"{root}/docs/develop/api-v2/server-inter/message/rich-media.md": b"POST /v2/users/{user_openid}/files file_type file_info srv_send_msg msg_type media",
    }

    result = await qq_contract.inspect(MemoryFetcher(bodies))

    assert result.outcome == "matched"
    file_data = next(
        check for check in result.checks if check.identifier == "qq.media.file-data"
    )
    assert file_data.state == "unverifiable"


def _wechat_source_bodies(commit: str, version: str) -> dict[str, bytes]:
    root = f"https://raw.githubusercontent.com/Tencent/openclaw-weixin/{commit}"
    return {
        f"{root}/src/api/api.ts": b"AuthorizationType ilink_bot_token X-WECHAT-UIN iLink-App-Id iLink-App-ClientVersion Authorization Bearer ilink/bot/getupdates ilink/bot/sendmessage ilink/bot/getuploadurl ilink/bot/getconfig ilink/bot/sendtyping get_updates_buf filekey media_type filesize aeskey",
        f"{root}/src/api/types.ts": b"get_updates_buf context_token item_list encrypt_query_param aes_key encrypt_type",
        f"{root}/package.json": _encoded({"version": version}),
    }


async def test_wechat_inspects_pinned_and_latest_official_source() -> None:
    latest = "c" * 40
    bodies = {
        f"https://api.github.com/repos/Tencent/openclaw-weixin/commits/{PINNED_SOURCE_TAG}": _encoded(
            {"sha": PINNED_SOURCE_COMMIT}
        ),
        "https://api.github.com/repos/Tencent/openclaw-weixin/commits?per_page=1": _encoded(
            [{"sha": latest}]
        ),
        **_wechat_source_bodies(PINNED_SOURCE_COMMIT, CHANNEL_VERSION),
        **_wechat_source_bodies(latest, "2.5.0"),
    }

    result = await wechat_contract.inspect(MemoryFetcher(bodies))

    assert result.outcome == "matched"
    assert {check.identifier for check in result.checks} >= {
        "wechat.pinned.headers-endpoints",
        "wechat.pinned.state-media",
        "wechat.latest.headers-endpoints",
        "wechat.latest.state-media",
    }


async def test_wechat_rejects_a_moved_pinned_tag() -> None:
    moved = "d" * 40
    latest = "c" * 40
    bodies = {
        f"https://api.github.com/repos/Tencent/openclaw-weixin/commits/{PINNED_SOURCE_TAG}": _encoded(
            {"sha": moved}
        ),
        "https://api.github.com/repos/Tencent/openclaw-weixin/commits?per_page=1": _encoded(
            [{"sha": latest}]
        ),
        **_wechat_source_bodies(latest, "2.5.0"),
    }

    result = await wechat_contract.inspect(MemoryFetcher(bodies))

    pinned = next(
        check
        for check in result.checks
        if check.identifier == "wechat.pinned.source-reference"
    )
    assert pinned.state == "drift"


def _telegram_html(*, include_allowed_updates: bool = True) -> bytes:
    allowed_updates = "allowed_updates" if include_allowed_updates else ""
    return f"""
    <html><body>
    <p>Responses contain ok, result, description, and error_code.</p>
    <h4>getMe</h4><p>Returns bot identity.</p>
    <h4>getWebhookInfo</h4><p>Returns a WebhookInfo object.</p>
    <h4>getUpdates</h4><p>offset timeout {allowed_updates}</p>
    <h4>sendMessage</h4><p>chat_id text</p>
    <h4>sendDocument</h4><p>chat_id document</p>
    </body></html>
    """.encode()


async def test_telegram_parses_method_sections_not_incidental_page_text() -> None:
    url = "https://core.telegram.org/bots/api"
    result = await inspect_telegram(MemoryFetcher({url: _telegram_html()}))

    assert result.outcome == "matched"
    assert all(check.state == "matched" for check in result.checks)

    drifted = await inspect_telegram(
        MemoryFetcher({url: _telegram_html(include_allowed_updates=False)})
    )
    get_updates = next(
        check
        for check in drifted.checks
        if check.identifier == "telegram.method.getupdates"
    )
    assert get_updates.state == "drift"


async def test_public_fetcher_retries_and_enforces_bounds() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    async def no_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = checker.PublicSourceFetcher(client, sleep=no_sleep)
        source = await fetcher.fetch(
            SourceRequest("https://pypi.org/example", "test", 2)
        )
        assert source.body == b"ok"
        assert attempts == 3

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"large", request=request))
    ) as client:
        fetcher = checker.PublicSourceFetcher(client, sleep=no_sleep)
        with pytest.raises(SourceFetchError, match="exceeds"):
            await fetcher.fetch(
                SourceRequest("https://pypi.org/example", "test", 2)
            )


async def test_public_fetcher_rejects_non_allowlisted_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/escape"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = checker.PublicSourceFetcher(client)
        with pytest.raises(SourceFetchError, match="allowlisted"):
            await fetcher.fetch(
                SourceRequest("https://pypi.org/example", "test")
            )


async def test_exhausted_public_source_is_unavailable_not_drift() -> None:
    class UnavailableFetcher:
        async def fetch(self, request: SourceRequest) -> FetchedSource:
            raise SourceFetchError(f"{request.url} timed out after bounded retries")

    result = await inspect_telegram(UnavailableFetcher())
    report = SourceMonitorReport("2026-08-19T00:00:00Z", (result,))

    assert result.outcome == "unavailable"
    assert result.checks[0].state == "unavailable"
    assert not report.healthy


class FakeGitHub:
    def __init__(self) -> None:
        self.label_exists = False
        self.issues: list[dict[str, Any]] = []
        self.comments: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content) if request.content else None
        if request.method == "GET" and path.endswith("/labels/upstream-drift"):
            return httpx.Response(
                200 if self.label_exists else 404,
                json={"name": "upstream-drift"} if self.label_exists else {},
                request=request,
            )
        if request.method == "POST" and path.endswith("/labels"):
            self.label_exists = True
            return httpx.Response(201, json=payload, request=request)
        if request.method == "GET" and path.endswith("/issues"):
            return httpx.Response(200, json=self.issues, request=request)
        if request.method == "POST" and path.endswith("/issues"):
            issue = {
                "number": len(self.issues) + 1,
                "state": "open",
                "body": payload["body"],
                "title": payload["title"],
            }
            self.issues.append(issue)
            return httpx.Response(201, json=issue, request=request)
        if request.method == "POST" and path.endswith("/comments"):
            self.comments.append(payload["body"])
            return httpx.Response(201, json={"id": 1}, request=request)
        if request.method == "PATCH" and "/issues/" in path:
            number = int(path.rsplit("/", 1)[-1])
            issue = self.issues[number - 1]
            issue.update(payload)
            return httpx.Response(200, json=issue, request=request)
        raise AssertionError(f"unexpected GitHub request: {request.method} {path}")


def _report(state: str) -> SourceMonitorReport:
    source = SourceEvidence("https://pypi.org/example", "live", "a" * 64, 10, "text/plain")
    check = SourceCheck(
        "example.contract",
        state,
        "example detail",
        (source.url,),
    )
    return SourceMonitorReport(
        "2026-08-19T00:00:00Z",
        (PlatformInspection("example", (source,), (check,)),),
    )


def test_rolling_issue_deduplicates_and_closes_on_recovery() -> None:
    fake = FakeGitHub()
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(fake.handle),
    )
    automation = checker.GitHubIssueAutomation(
        "Mtrya/kimi-bridge", "token", client=client
    )
    drift = _report("drift")

    assert automation.synchronize(drift) == "opened-source-drift-issue"
    assert automation.synchronize(drift) == "source-drift-unchanged"
    assert len(fake.issues) == 1

    assert automation.synchronize(_report("matched")) == "closed-recovered-source-drift"
    assert fake.issues[0]["state"] == "closed"
    assert len(fake.comments) == 1


def test_report_round_trip_preserves_unverifiable_as_healthy(tmp_path: Path) -> None:
    report = _report("unverifiable")
    path = tmp_path / "report.json"

    checker.write_report(report, path)
    restored = checker.read_report(path)

    assert restored == report
    assert restored.healthy
