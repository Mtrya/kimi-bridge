from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from websockets.exceptions import ConnectionClosed

from kimi_bridge.platforms.qq import (
    GROUP_AND_C2C_EVENT_INTENT,
    QQ_API_BASE_URL,
    QQ_TOKEN_URL,
    QQAPIError,
    QQBotAPI,
    QQCredentials,
    QQGatewayClient,
    QQGatewayEvent,
    QQProtocolError,
    QQTokenManager,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class FakeTokenProvider:
    async def authorization(self) -> str:
        return "QQBot fake-token"


class FakeGatewaySocket:
    """One scripted WS connection: dicts become frames, exceptions raise."""

    def __init__(self, frames: list[Any]) -> None:
        self._frames = list(frames)
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeGatewaySocket:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    async def recv(self) -> str:
        if not self._frames:
            await asyncio.Event().wait()
        item = self._frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))


class FakeGatewayConnect:
    def __init__(self, sockets: list[FakeGatewaySocket]) -> None:
        self._sockets = list(sockets)
        self.urls: list[str] = []

    def __call__(self, url: str, **_kwargs: Any) -> FakeGatewaySocket:
        self.urls.append(url)
        return self._sockets.pop(0)


async def _wait_for(predicate: Any, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout)


def _hello(interval_ms: int = 60_000) -> dict[str, Any]:
    return {"op": 10, "d": {"heartbeat_interval": interval_ms}}


def _ready(seq: int = 1, session_id: str = "sess-1") -> dict[str, Any]:
    return {
        "op": 0,
        "s": seq,
        "t": "READY",
        "d": {
            "version": 1,
            "session_id": session_id,
            "user": {"id": "6158788878435714165", "bot": True},
            "shard": [0, 0],
        },
    }


def _c2c(seq: int, content: str, event_id: str) -> dict[str, Any]:
    return {
        "op": 0,
        "s": seq,
        "t": "C2C_MESSAGE_CREATE",
        "id": event_id,
        "d": {
            "id": f"ROBOT1.0_{content}!",
            "content": content,
            "timestamp": "2026-07-25T13:37:18+08:00",
            "author": {"user_openid": "OPENID-USER"},
        },
    }


def _token_response(index: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": f"tok-{index}", "expires_in": "7200"},
    )


def _token_manager(
    handler: Any, clock: FakeClock | None = None, **kwargs: Any
) -> tuple[QQTokenManager, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = QQTokenManager(
        QQCredentials("app-1", "secret-1"),
        http_client=http,
        clock=clock or FakeClock(),
        **kwargs,
    )
    return manager, http


async def _start_gateway(
    connect: FakeGatewayConnect,
    events: list[QQGatewayEvent],
    *,
    gateway_url: str = "wss://api.sgroup.qq.com/websocket/",
) -> QQGatewayClient:
    async def get_gateway_url() -> str:
        return gateway_url

    async def on_event(event: QQGatewayEvent) -> None:
        events.append(event)

    client = QQGatewayClient(
        FakeTokenProvider(),
        get_gateway_url,
        ws_connect=connect,
        initial_backoff=0.01,
        max_backoff=0.02,
    )
    await client.start(on_event)
    return client


async def test_token_manager_caches_until_refresh_margin() -> None:
    clock = FakeClock()
    issued = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        assert str(request.url) == QQ_TOKEN_URL
        assert json.loads(request.content) == {
            "appId": "app-1",
            "clientSecret": "secret-1",
        }
        issued += 1
        return _token_response(issued)

    manager, http = _token_manager(handler, clock)
    try:
        assert await manager.access_token() == "tok-1"
        clock.now += 7_000.0
        assert await manager.access_token() == "tok-1"
        assert issued == 1
        clock.now += 141.0
        assert await manager.authorization() == "QQBot tok-2"
        assert issued == 2
    finally:
        await manager.close()
        await http.aclose()


async def test_token_manager_retries_transport_and_server_errors() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("unavailable", request=request)
        if attempts == 2:
            return httpx.Response(503, content=b"unavailable")
        return _token_response(1)

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    manager, http = _token_manager(
        handler,
        sleep=fake_sleep,
        max_retries=2,
        initial_backoff=0.25,
        max_backoff=0.5,
    )
    try:
        assert await manager.access_token() == "tok-1"
    finally:
        await manager.close()
        await http.aclose()

    assert attempts == 3
    assert delays == [0.25, 0.5]


async def test_token_manager_semantic_error_is_not_retried() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            400, json={"code": 100007, "message": "appid invalid"}
        )

    manager, http = _token_manager(handler)
    try:
        with pytest.raises(QQAPIError) as caught:
            await manager.access_token()
    finally:
        await manager.close()
        await http.aclose()

    assert attempts == 1
    assert caught.value.code == 100007
    assert "secret-1" not in str(caught.value)


async def test_token_manager_rejects_error_envelope_with_200() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 11244, "message": "invalid secret"}
        )

    manager, http = _token_manager(handler)
    try:
        with pytest.raises(QQAPIError) as caught:
            await manager.access_token()
    finally:
        await manager.close()
        await http.aclose()

    assert caught.value.code == 11244


def _api_pair(
    api_handler: Any, **api_kwargs: Any
) -> tuple[QQBotAPI, QQTokenManager, httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bots.qq.com":
            return _token_response(1)
        return api_handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = QQTokenManager(
        QQCredentials("app-1", "secret-1"), http_client=http
    )
    api = QQBotAPI(manager, http_client=http, **api_kwargs)
    return api, manager, http


async def test_bot_api_sends_passive_text_message() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "SENT_MSG_ID", "timestamp": "2026-07-25T13:37:20+08:00"},
        )

    api, manager, http = _api_pair(handler)
    try:
        result = await api.send_c2c_message(
            "OPENID-USER",
            msg_type=0,
            content="hello",
            msg_id="ROBOT1.0_inbound!",
            msg_seq=1,
        )
    finally:
        await api.close()
        await manager.close()
        await http.aclose()

    assert result["id"] == "SENT_MSG_ID"
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{QQ_API_BASE_URL}/v2/users/OPENID-USER/messages"
    assert request.headers["authorization"] == "QQBot tok-1"
    assert json.loads(request.content) == {
        "msg_type": 0,
        "content": "hello",
        "msg_id": "ROBOT1.0_inbound!",
        "msg_seq": 1,
    }


async def test_bot_api_uploads_media_for_passive_send() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"file_uuid": "uuid-1", "file_info": "OPAQUE", "ttl": 0},
        )

    api, manager, http = _api_pair(handler)
    try:
        result = await api.upload_c2c_media(
            "OPENID-USER",
            file_type=1,
            url="https://example.com/x.png",
        )
        with pytest.raises(ValueError, match="url or file_data"):
            await api.upload_c2c_media("OPENID-USER", file_type=1)
    finally:
        await api.close()
        await manager.close()
        await http.aclose()

    assert result == {"file_uuid": "uuid-1", "file_info": "OPAQUE", "ttl": 0}
    request = requests[0]
    assert str(request.url) == f"{QQ_API_BASE_URL}/v2/users/OPENID-USER/files"
    assert json.loads(request.content) == {
        "file_type": 1,
        "srv_send_msg": False,
        "url": "https://example.com/x.png",
    }


async def test_bot_api_retries_server_errors_but_not_semantic_codes() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(502, content=b"bad gateway")
        if attempts == 2:
            return httpx.Response(200, json={"id": "SENT", "timestamp": "t"})
        return httpx.Response(
            400, json={"code": 304003, "message": "url \u672a\u62a5\u5907"}
        )

    async def no_sleep(_delay: float) -> None:
        pass

    api, manager, http = _api_pair(handler, sleep=no_sleep, max_retries=2)
    try:
        result = await api.send_c2c_message(
            "OPENID-USER", msg_type=0, content="first"
        )
        assert result["id"] == "SENT"
        assert attempts == 2
        with pytest.raises(QQAPIError) as caught:
            await api.send_c2c_message(
                "OPENID-USER", msg_type=0, content="https://blocked.example"
            )
    finally:
        await api.close()
        await manager.close()
        await http.aclose()

    assert attempts == 3
    assert caught.value.code == 304003


async def test_bot_api_fetches_gateway_url() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"url": "wss://api.sgroup.qq.com/websocket/"}
        )

    api, manager, http = _api_pair(handler)
    try:
        url = await api.get_gateway_url()
    finally:
        await api.close()
        await manager.close()
        await http.aclose()

    assert url == "wss://api.sgroup.qq.com/websocket/"
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == f"{QQ_API_BASE_URL}/gateway"
    assert request.headers["authorization"] == "QQBot tok-1"


async def test_gateway_identifies_dispatches_then_resumes_after_drop() -> None:
    first = FakeGatewaySocket(
        [
            _hello(),
            _ready(seq=1, session_id="sess-1"),
            _c2c(2, "hi", "evt-1"),
            ConnectionClosed(None, None),
        ]
    )
    second = FakeGatewaySocket(
        [
            _hello(),
            {"op": 0, "s": 2, "t": "RESUMED", "d": ""},
            _c2c(3, "again", "evt-2"),
            {"op": 0, "s": 4, "t": "FRIEND_ADD", "id": "evt-3", "d": {}},
        ]
    )
    connect = FakeGatewayConnect([first, second])
    events: list[QQGatewayEvent] = []
    client = await _start_gateway(connect, events)
    try:
        await _wait_for(lambda: len(events) == 3)
    finally:
        await client.stop()

    assert connect.urls == ["wss://api.sgroup.qq.com/websocket/"] * 2
    assert first.sent[0] == {
        "op": 2,
        "d": {
            "token": "QQBot fake-token",
            "intents": GROUP_AND_C2C_EVENT_INTENT,
            "shard": [0, 1],
            "properties": {},
        },
    }
    assert second.sent[0] == {
        "op": 6,
        "d": {"token": "QQBot fake-token", "session_id": "sess-1", "seq": 2},
    }
    assert events[0] == QQGatewayEvent(
        type="C2C_MESSAGE_CREATE",
        data={
            "id": "ROBOT1.0_hi!",
            "content": "hi",
            "timestamp": "2026-07-25T13:37:18+08:00",
            "author": {"user_openid": "OPENID-USER"},
        },
        seq=2,
        event_id="evt-1",
    )
    assert events[1].data["content"] == "again"
    assert events[2].type == "FRIEND_ADD"
    assert client.last_seq == 4
    assert client.last_event_id == "evt-3"


async def test_gateway_heartbeats_carry_last_seq() -> None:
    socket = FakeGatewaySocket(
        [
            _hello(interval_ms=10),
            _ready(seq=7),
            {"op": 1, "d": None},
            {"op": 11},
        ]
    )
    connect = FakeGatewayConnect([socket])
    events: list[QQGatewayEvent] = []
    client = await _start_gateway(connect, events)
    try:
        await _wait_for(
            lambda: len([f for f in socket.sent if f["op"] == 1]) >= 2
        )
    finally:
        await client.stop()

    heartbeats = [frame for frame in socket.sent if frame["op"] == 1]
    assert all(frame == {"op": 1, "d": 7} for frame in heartbeats)
    assert events == []


async def test_gateway_invalid_session_reidentifies_from_scratch() -> None:
    first = FakeGatewaySocket(
        [
            _hello(),
            _ready(seq=1, session_id="sess-1"),
            _c2c(2, "hi", "evt-1"),
            {"op": 9, "d": False},
        ]
    )
    second = FakeGatewaySocket([_hello(), _ready(seq=1, session_id="sess-2")])
    connect = FakeGatewayConnect([first, second])
    events: list[QQGatewayEvent] = []
    client = await _start_gateway(connect, events)
    try:
        await _wait_for(lambda: len(second.sent) >= 1)
    finally:
        await client.stop()

    assert second.sent[0]["op"] == 2
    assert second.sent[0]["d"]["token"] == "QQBot fake-token"


async def test_gateway_reconnect_request_resumes_with_kept_session() -> None:
    first = FakeGatewaySocket(
        [
            _hello(),
            _ready(seq=4, session_id="sess-9"),
            {"op": 7},
        ]
    )
    second = FakeGatewaySocket([_hello()])
    connect = FakeGatewayConnect([first, second])
    events: list[QQGatewayEvent] = []
    client = await _start_gateway(connect, events)
    try:
        await _wait_for(lambda: len(second.sent) >= 1)
    finally:
        await client.stop()

    assert second.sent[0] == {
        "op": 6,
        "d": {"token": "QQBot fake-token", "session_id": "sess-9", "seq": 4},
    }


async def test_gateway_requires_hello_first() -> None:
    socket = FakeGatewaySocket([_ready()])
    connect = FakeGatewayConnect([socket])
    events: list[QQGatewayEvent] = []
    client = await _start_gateway(connect, events)
    try:
        with pytest.raises(QQProtocolError, match="hello"):
            await asyncio.wait_for(client.wait(), 1)
    finally:
        await client.stop()
