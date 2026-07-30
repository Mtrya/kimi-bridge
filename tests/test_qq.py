from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from websockets.exceptions import ConnectionClosed

from kimi_bridge.platforms import qq as qq_module
from kimi_bridge.platforms.base import ConversationRef, MessageRef
from kimi_bridge.platforms.qq import (
    GROUP_AND_C2C_EVENT_INTENT,
    MSG_TYPE_MARKDOWN,
    QQ_API_BASE_URL,
    QQ_PASSIVE_REPLY_LIMIT,
    QQ_TEXT_LIMIT,
    QQ_TOKEN_URL,
    STREAM_INPUT_STATE_DONE,
    STREAM_INPUT_STATE_GENERATING,
    QQAdapter,
    QQAPIError,
    QQBotAPI,
    QQCredentials,
    QQGatewayClient,
    QQGatewayEvent,
    QQProtocolError,
    QQTokenManager,
    QQTransportError,
    defang_urls,
    sanitize_markdown,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class FakeTokenProvider:
    def __init__(self) -> None:
        self.closed = False

    async def authorization(self) -> str:
        return "QQBot fake-token"

    async def close(self) -> None:
        self.closed = True


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


class AutoAckGatewaySocket(FakeGatewaySocket):
    """Script dispatch frames, then acknowledge every emitted heartbeat."""

    def __init__(self, frames: list[Any]) -> None:
        super().__init__(frames)
        self._replies: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def recv(self) -> str:
        if self._frames:
            return await super().recv()
        return json.dumps(await self._replies.get())

    async def send(self, message: str) -> None:
        await super().send(message)
        if self.sent[-1].get("op") == 1:
            self._replies.put_nowait({"op": 11})


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


async def test_bot_api_withdraws_c2c_message() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    api, manager, http = _api_pair(handler)
    try:
        await api.delete_c2c_message("OPENID-USER", "SENT_MSG_ID")
    finally:
        await api.close()
        await manager.close()
        await http.aclose()

    request = requests[0]
    assert request.method == "DELETE"
    assert str(request.url) == (
        f"{QQ_API_BASE_URL}/v2/users/OPENID-USER/messages/SENT_MSG_ID"
    )
    assert request.headers["authorization"] == "QQBot tok-1"


async def test_bot_api_rejects_empty_c2c_message_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("validation should happen before the request")

    api, manager, http = _api_pair(handler)
    try:
        with pytest.raises(ValueError, match="message_id must be non-empty"):
            await api.delete_c2c_message("OPENID-USER", "")
    finally:
        await api.close()
        await manager.close()
        await http.aclose()


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


async def test_bot_api_raises_transport_error_after_server_retries_exhausted() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, content=b"unavailable")

    async def no_sleep(_delay: float) -> None:
        pass

    api, manager, http = _api_pair(
        handler,
        sleep=no_sleep,
        max_retries=1,
    )
    try:
        with pytest.raises(QQTransportError, match="HTTP 503"):
            await api.get_gateway_url()
    finally:
        await api.close()
        await manager.close()
        await http.aclose()

    assert attempts == 2


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


async def test_gateway_reconnects_when_heartbeat_is_not_acknowledged() -> None:
    first = FakeGatewaySocket(
        [
            _hello(interval_ms=1),
            _ready(seq=7, session_id="sess-7"),
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

    assert len([frame for frame in first.sent if frame["op"] == 1]) == 1
    assert second.sent[0] == {
        "op": 6,
        "d": {"token": "QQBot fake-token", "session_id": "sess-7", "seq": 7},
    }


async def test_gateway_heartbeats_continue_while_event_handler_is_busy() -> None:
    socket = AutoAckGatewaySocket(
        [
            _hello(interval_ms=5),
            _ready(seq=1),
            _c2c(2, "slow attachment", "evt-1"),
        ]
    )
    connect = FakeGatewayConnect([socket])
    entered = asyncio.Event()
    release = asyncio.Event()
    events: list[QQGatewayEvent] = []

    async def get_gateway_url() -> str:
        return "wss://api.sgroup.qq.com/websocket/"

    async def on_event(event: QQGatewayEvent) -> None:
        entered.set()
        await release.wait()
        events.append(event)

    client = QQGatewayClient(
        FakeTokenProvider(),
        get_gateway_url,
        ws_connect=connect,
        initial_backoff=0.01,
        max_backoff=0.02,
    )
    await client.start(on_event)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await _wait_for(
            lambda: len([frame for frame in socket.sent if frame["op"] == 1])
            >= 3
        )
        assert connect.urls == ["wss://api.sgroup.qq.com/websocket/"]
        release.set()
        await _wait_for(lambda: len(events) == 1)
    finally:
        await client.stop()


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


# ---------------------------------------------------------------------------
# QQAdapter
# ---------------------------------------------------------------------------

from datetime import datetime  # noqa: E402

from kimi_bridge.platforms.qq import (  # noqa: E402
    QQ_PASSIVE_REPLY_WINDOW_SECONDS,
    _ReplyAnchor,
)


def _c2c_payload(
    msg_id: str,
    content: str,
    *,
    openid: str = "OPENID-USER",
    timestamp: str = "2026-07-25T13:37:18+08:00",
    attachments: list[dict[str, Any]] | None = None,
    ext: list[str] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": msg_id,
        "content": content,
        "timestamp": timestamp,
        "author": {"user_openid": openid},
    }
    if attachments is not None:
        data["attachments"] = attachments
    if ext is not None:
        data["message_scene"] = {"ext": ext}
    return data


def _anchor(
    msg_id: str = "MSGID-ANCHOR",
    event_id: str = "evt-anchor",
    received_at: float = 1_000.0,
    used: int = 0,
) -> _ReplyAnchor:
    return _ReplyAnchor(
        msg_id=msg_id, event_id=event_id, received_at=received_at, used=used
    )


async def _noop_on_interaction(_sender: Any, _interaction: Any) -> None:
    pass


class FakeQQBotAPI:
    """Fakes the outbound REST surface `QQAdapter` calls."""

    def __init__(self) -> None:
        self.active_sends: list[dict[str, Any]] = []
        self.stream_frames: list[dict[str, Any]] = []
        self.typing_calls: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.withdrawals: list[dict[str, str]] = []
        self.upload_file_info = "OPAQUE-FILE-INFO"
        self.fail_url_once = False
        self.fail_done_once = False
        self.fail_stream_once = False
        self.fail_withdraw = False
        self.block_done = False
        self.done_started = asyncio.Event()
        self.release_done = asyncio.Event()
        self.closed = False
        self._next_id = 1
        self._stream_contents: dict[str, str] = {}
        self._stream_indexes: dict[str, int] = {}
        self._open_streams: dict[str, str] = {}

    async def upload_c2c_media(
        self,
        openid: str,
        *,
        file_type: int,
        url: str | None = None,
        file_data: str | None = None,
        srv_send_msg: bool = False,
    ) -> dict[str, Any]:
        self.uploads.append(
            {
                "openid": openid,
                "file_type": file_type,
                "url": url,
                "file_data": file_data,
                "srv_send_msg": srv_send_msg,
            }
        )
        return {"file_uuid": "uuid-1", "file_info": self.upload_file_info, "ttl": 0}

    async def send_c2c_message(
        self,
        openid: str,
        *,
        msg_type: int,
        content: str | None = None,
        markdown: dict[str, Any] | None = None,
        media: dict[str, Any] | None = None,
        msg_id: str | None = None,
        event_id: str | None = None,
        msg_seq: int | None = None,
    ) -> dict[str, Any]:
        self.active_sends.append(
            {
                "openid": openid,
                "msg_type": msg_type,
                "content": content,
                "markdown": markdown,
                "media": media,
                "msg_id": msg_id,
                "event_id": event_id,
                "msg_seq": msg_seq,
            }
        )
        message_id = f"active-{self._next_id}"
        self._next_id += 1
        return {"id": message_id, "timestamp": "t"}

    async def send_c2c_stream_message(
        self,
        openid: str,
        *,
        input_state: int,
        content_raw: str,
        msg_id: str,
        msg_seq: int,
        index: int,
        event_id: str | None = None,
        stream_msg_id: str | None = None,
        input_mode: str = "replace",
        content_type: str = "markdown",
    ) -> dict[str, Any]:
        if self.block_done and input_state == STREAM_INPUT_STATE_DONE:
            self.done_started.set()
            await self.release_done.wait()
        if self.fail_stream_once and input_state == STREAM_INPUT_STATE_GENERATING:
            self.fail_stream_once = False
            raise QQAPIError("stream_messages", 40000, "stream failed")
        if self.fail_url_once:
            self.fail_url_once = False
            raise QQAPIError("stream_messages", 304003, "url \u672a\u62a5\u5907")
        if self.fail_done_once and input_state == STREAM_INPUT_STATE_DONE:
            self.fail_done_once = False
            raise QQAPIError("stream_messages", 40006, "\u9519\u8bef\u7684\u7d22\u5f15")
        message_id = stream_msg_id
        if message_id is None:
            message_id = f"stream-{self._next_id}"
            self._next_id += 1
            self._open_streams[msg_id] = message_id
        else:
            previous = self._stream_contents[message_id]
            if self._open_streams.get(msg_id) != message_id:
                raise QQAPIError("stream_messages", 40006, "\u9519\u8bef\u7684\u7d22\u5f15")
            if index != self._stream_indexes[message_id] + 1:
                raise QQAPIError("stream_messages", 40006, "\u9519\u8bef\u7684\u7d22\u5f15")
            if content_raw == previous:
                raise QQAPIError("stream_messages", 40006, "\u9519\u8bef\u7684\u7d22\u5f15")
            if not content_raw.startswith(previous):
                raise QQAPIError(
                    "stream_messages",
                    40007,
                    "\u5df2\u4e0b\u53d1\u5185\u5bb9\u524d\u7f00\u4e0d\u53ef\u4fee\u6539",
                )
        if input_state == STREAM_INPUT_STATE_GENERATING:
            self._open_streams[msg_id] = message_id
        else:
            self._open_streams.pop(msg_id, None)
        self.stream_frames.append(
            {
                "openid": openid,
                "input_state": input_state,
                "content_raw": content_raw,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
                "index": index,
                "event_id": event_id,
                "stream_msg_id": stream_msg_id,
            }
        )
        self._stream_contents[message_id] = content_raw
        self._stream_indexes[message_id] = index
        return {"id": message_id, "timestamp": "t"}

    async def delete_c2c_message(self, openid: str, message_id: str) -> None:
        self.withdrawals.append({"openid": openid, "message_id": message_id})
        if self.fail_withdraw:
            raise QQAPIError("delete message", 304027, "message expired")
        for anchor_id, stream_id in tuple(self._open_streams.items()):
            if stream_id == message_id:
                self._open_streams.pop(anchor_id)

    async def send_c2c_typing(
        self,
        openid: str,
        *,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        input_second: int = 60,
    ) -> dict[str, Any]:
        self.typing_calls.append(
            {"openid": openid, "msg_id": msg_id, "input_second": input_second}
        )
        return {"id": "typing", "timestamp": "t"}

    async def close(self) -> None:
        self.closed = True


class FakeQQGateway:
    """Minimal `QQGatewayClient` stand-in: tests push events directly."""

    def __init__(self) -> None:
        self._on_event: Any = None
        self.stopped = False

    async def start(self, on_event: Any) -> None:
        self._on_event = on_event

    async def wait(self) -> None:
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stopped = True

    async def emit(self, event: QQGatewayEvent) -> None:
        assert self._on_event is not None
        await self._on_event(event)


class _GatedSleep:
    """Fake sleep: records each call's delay and blocks until released."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._gates: list[asyncio.Event] = []

    async def __call__(self, delay: float) -> None:
        gate = asyncio.Event()
        self.calls.append(delay)
        self._gates.append(gate)
        await gate.wait()

    def release(self, index: int) -> None:
        self._gates[index].set()


def _make_qq_adapter(
    api: Any,
    gateway: Any,
    *,
    allowed_users: frozenset[str] = frozenset({"OPENID-USER"}),
    clock: Any = None,
    sleep: Any = None,
    idle_timeout: float = 6.0,
    typing_keepalive: float = 50.0,
    http_client: httpx.AsyncClient | None = None,
    token_manager: Any = None,
) -> QQAdapter:
    return QQAdapter(
        "app-1",
        allowed_users,
        api=api,
        gateway=gateway,
        token_manager=token_manager,
        http_client=http_client,
        clock=clock or (lambda: 1_000.0),
        sleep=sleep or asyncio.sleep,
        idle_timeout=idle_timeout,
        typing_keepalive=typing_keepalive,
    )


def test_qq_adapter_declares_streaming_capabilities() -> None:
    adapter = _make_qq_adapter(FakeQQBotAPI(), FakeQQGateway())

    assert adapter.supports_edits is True
    assert adapter.supports_interactions is False
    assert adapter.message_edit_limit is None


# --- sanitizer ---------------------------------------------------------


def test_sanitize_markdown_flattens_fenced_code() -> None:
    result = sanitize_markdown("```python\ndef f():\n    return 1\n```")
    assert "```" not in result
    assert "    def f():" in result
    assert "        return 1" in result


def test_sanitize_markdown_strips_inline_code() -> None:
    assert sanitize_markdown("use `foo()` now") == "use foo() now"


def test_sanitize_markdown_flattens_table() -> None:
    result = sanitize_markdown("| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert "---" not in result
    assert "a | b" in result
    assert "1 | 2" in result


def test_sanitize_markdown_preserves_heading_and_bold() -> None:
    result = sanitize_markdown("# Title\n**bold** and *italic*")
    assert result.startswith("# Title")
    assert "**bold**\u200b " in result
    assert "*italic*" in result


def test_sanitize_markdown_forces_single_line_breaks() -> None:
    result = sanitize_markdown("line1\nline2\n\nline3")
    assert result == "line1\u200b\nline2\n\nline3"


def test_sanitize_markdown_preserves_content_within_qq_limit() -> None:
    lines = 1_000
    source = "```\n" + "\n".join("x" for _ in range(lines)) + "\n```"

    result = sanitize_markdown(source)

    assert len(source) < QQ_TEXT_LIMIT
    assert len(result) <= QQ_TEXT_LIMIT
    assert result.count("x") == lines


def test_defang_urls_strips_scheme_and_brackets_dots() -> None:
    result = defang_urls("see https://example.com/path and done")
    assert result == "see example[.]com/path and done"
    assert "http" not in result


# --- inbound pipeline ----------------------------------------------------


async def test_adapter_delivers_authorized_message() -> None:
    api = FakeQQBotAPI()
    gateway = FakeQQGateway()
    adapter = _make_qq_adapter(api, gateway)
    delivered: list[Any] = []

    async def on_message(_sender: Any, message: Any) -> None:
        delivered.append(message)

    await adapter.start(on_message, _noop_on_interaction)
    try:
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload("MSGID-1", "hello there"),
                seq=1,
                event_id="evt-1",
            )
        )
    finally:
        await adapter.stop()

    assert len(delivered) == 1
    message = delivered[0]
    assert message.text == "hello there"
    assert message.message_id == "MSGID-1"
    assert message.actor.id == "OPENID-USER"
    assert message.conversation == ConversationRef("qq", "app-1", "OPENID-USER")
    assert message.timestamp == pytest.approx(
        datetime.fromisoformat("2026-07-25T13:37:18+08:00").timestamp()
    )


async def test_adapter_drops_unauthorized_sender(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeQQBotAPI()
    gateway = FakeQQGateway()
    adapter = _make_qq_adapter(api, gateway, allowed_users=frozenset({"OTHER-USER"}))
    delivered: list[Any] = []

    async def on_message(_sender: Any, message: Any) -> None:
        delivered.append(message)

    caplog.set_level(logging.WARNING, logger="kimi_bridge.platforms.qq")
    await adapter.start(on_message, _noop_on_interaction)
    try:
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload("MSGID-1", "hello"),
                seq=1,
                event_id="evt-1",
            )
        )
    finally:
        await adapter.stop()

    assert not delivered
    assert "unauthorized sender openid: OPENID-USER" in caplog.text


async def test_adapter_dedupes_by_message_id() -> None:
    api = FakeQQBotAPI()
    gateway = FakeQQGateway()
    adapter = _make_qq_adapter(api, gateway)
    delivered: list[Any] = []

    async def on_message(_sender: Any, message: Any) -> None:
        delivered.append(message)

    await adapter.start(on_message, _noop_on_interaction)
    try:
        event = QQGatewayEvent(
            type="C2C_MESSAGE_CREATE",
            data=_c2c_payload("MSGID-1", "hello"),
            seq=1,
            event_id="evt-1",
        )
        await gateway.emit(event)
        await gateway.emit(event)
    finally:
        await adapter.stop()

    assert len(delivered) == 1


async def test_adapter_dedupes_by_msg_idx_ext() -> None:
    api = FakeQQBotAPI()
    gateway = FakeQQGateway()
    adapter = _make_qq_adapter(api, gateway)
    delivered: list[Any] = []

    async def on_message(_sender: Any, message: Any) -> None:
        delivered.append(message)

    await adapter.start(on_message, _noop_on_interaction)
    try:
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload("MSGID-1", "hello", ext=["msg_idx=7"]),
                seq=1,
                event_id="evt-1",
            )
        )
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload("MSGID-2", "hello again", ext=["msg_idx=7"]),
                seq=2,
                event_id="evt-2",
            )
        )
    finally:
        await adapter.stop()

    assert len(delivered) == 1


async def test_adapter_classifies_attachments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image.png":
            return httpx.Response(200, content=b"PNGDATA")
        return httpx.Response(200, content=b"FILEDATA")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = FakeQQBotAPI()
    gateway = FakeQQGateway()
    adapter = _make_qq_adapter(api, gateway, http_client=http_client)
    delivered: list[Any] = []

    async def on_message(_sender: Any, message: Any) -> None:
        delivered.append(message)

    await adapter.start(on_message, _noop_on_interaction)
    try:
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload(
                    "MSGID-1",
                    "look",
                    attachments=[
                        {
                            "url": "https://qq.example/image.png",
                            "filename": "image.png",
                            "content_type": "image/png",
                        },
                        {
                            "url": "https://qq.example/doc.txt",
                            "filename": "doc.txt",
                            "content_type": "text/plain",
                        },
                    ],
                ),
                seq=1,
                event_id="evt-1",
            )
        )
    finally:
        await adapter.stop()

    assert len(delivered) == 1
    message = delivered[0]
    assert len(message.images) == 1
    assert message.images[0].data == b"PNGDATA"
    assert message.images[0].media_type == "image/png"
    assert len(message.files) == 1
    assert message.files[0].name == "doc.txt"
    assert message.files[0].data == b"FILEDATA"


async def test_adapter_attachment_download_failure_logs_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    signed_url = "https://qq.example/x.png?rkey=ephemeral-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = FakeQQBotAPI()
    gateway = FakeQQGateway()
    adapter = _make_qq_adapter(api, gateway, http_client=http_client)
    delivered: list[Any] = []

    async def on_message(_sender: Any, message: Any) -> None:
        delivered.append(message)

    caplog.set_level(logging.WARNING, logger="kimi_bridge.platforms.qq")
    await adapter.start(on_message, _noop_on_interaction)
    try:
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload(
                    "MSGID-1",
                    "look",
                    attachments=[
                        {
                            "url": signed_url,
                            "filename": "x.png",
                            "content_type": "image/png",
                        }
                    ],
                ),
                seq=1,
                event_id="evt-1",
            )
        )
    finally:
        await adapter.stop()

    assert len(delivered) == 1
    message = delivered[0]
    assert message.images == ()
    assert message.files == ()
    records = [
        record
        for record in caplog.records
        if record.name == "kimi_bridge.platforms.qq"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].args == ("x.png", "ConnectError")
    assert signed_url not in caplog.text
    assert "ephemeral-secret" not in caplog.text


async def test_adapter_rejects_non_https_attachment_without_fetching() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"PRIVATE")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = FakeQQGateway()
    adapter = _make_qq_adapter(
        FakeQQBotAPI(),
        gateway,
        http_client=http_client,
    )
    delivered: list[Any] = []

    async def on_message(_sender: Any, message: Any) -> None:
        delivered.append(message)

    await adapter.start(on_message, _noop_on_interaction)
    try:
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload(
                    "MSGID-1",
                    "look",
                    attachments=[
                        {
                            "url": "http://127.0.0.1/private",
                            "content_type": "image/png",
                        }
                    ],
                ),
                seq=1,
                event_id="evt-1",
            )
        )
    finally:
        await adapter.stop()

    assert requests == []
    assert len(delivered) == 1
    assert delivered[0].text == "look"
    assert delivered[0].images == ()
    assert delivered[0].files == ()


async def test_adapter_stops_attachment_download_at_size_limit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(qq_module, "QQ_ATTACHMENT_LIMIT_BYTES", 4)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = FakeQQGateway()
    adapter = _make_qq_adapter(
        FakeQQBotAPI(),
        gateway,
        http_client=http_client,
    )
    delivered: list[Any] = []

    async def on_message(_sender: Any, message: Any) -> None:
        delivered.append(message)

    await adapter.start(on_message, _noop_on_interaction)
    try:
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload(
                    "MSGID-1",
                    "look",
                    attachments=[
                        {
                            "url": "https://qq.example/image.png",
                            "content_type": "image/png",
                        }
                    ],
                ),
                seq=1,
                event_id="evt-1",
            )
        )
    finally:
        await adapter.stop()

    assert len(delivered) == 1
    assert delivered[0].text == "look"
    assert delivered[0].images == ()
    assert delivered[0].files == ()
    records = [
        record
        for record in caplog.records
        if record.name == "kimi_bridge.platforms.qq"
    ]
    assert len(records) == 1
    configured_reason = str(qq_module.QQAttachmentTooLarge(4))
    assert configured_reason != str(qq_module.QQAttachmentTooLarge(5))
    assert records[0].args[1] == configured_reason


# --- outbound streaming --------------------------------------------------


async def test_send_text_opens_stream_with_seq_one_index_zero() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(conversation, "hello\n")

    assert len(api.stream_frames) == 1
    frame = api.stream_frames[0]
    assert frame["msg_seq"] == 1
    assert frame["index"] == 0
    assert frame["stream_msg_id"] is None
    assert frame["input_state"] == STREAM_INPUT_STATE_GENERATING
    assert ref == MessageRef(conversation, "text-1")
    assert adapter._streams[ref].stream_msg_id == "stream-1"


async def test_send_text_finishes_previous_stream_before_opening_next() -> None:
    api = FakeQQBotAPI()
    sleep = _GatedSleep()
    adapter = _make_qq_adapter(api, FakeQQGateway(), sleep=sleep)
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    first = await adapter.send_text(conversation, "first\n")
    await _wait_for(lambda: len(sleep.calls) == 1)
    second = await adapter.send_text(conversation, "second\n")
    try:
        assert [frame["input_state"] for frame in api.stream_frames] == [
            STREAM_INPUT_STATE_GENERATING,
            STREAM_INPUT_STATE_DONE,
            STREAM_INPUT_STATE_GENERATING,
        ]
        assert first not in adapter._streams
        assert not adapter._streams[second].finalized
    finally:
        await adapter.stop()


async def test_edit_text_continues_stream_reusing_seq_incrementing_index() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(conversation, "hello\n")
    await adapter.edit_text(ref, "hello\nworld\n")
    await adapter.edit_text(ref, "hello\nworld\nagain\n")

    assert len(api.stream_frames) == 3
    assert [frame["msg_seq"] for frame in api.stream_frames] == [1, 1, 1]
    assert [frame["index"] for frame in api.stream_frames] == [0, 1, 2]
    assert all(
        frame["stream_msg_id"] in (None, "stream-1") for frame in api.stream_frames
    )
    assert api.stream_frames[-1]["content_raw"] == (
        "hello\u200b\nworld\u200b\nagain\u200b\n"
    )


async def test_failed_stream_edit_does_not_commit_source_snapshot() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()
    first = "hello\n"
    final = "hello\nworld\n"

    ref = await adapter.send_text(conversation, first)
    api.fail_stream_once = True

    with pytest.raises(QQAPIError, match="stream failed"):
        await adapter.edit_text(ref, final)

    assert adapter._streams[ref].last_source_text == first

    await adapter.edit_text(ref, final)

    assert adapter._streams[ref].last_source_text == final
    assert len(api.stream_frames) == 2


async def test_stream_buffers_incomplete_line_until_it_becomes_stable() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(conversation, "hello")
    await adapter.edit_text(ref, "hello world")

    assert not api.stream_frames

    await adapter.edit_text(ref, "hello world\n")

    assert len(api.stream_frames) == 1
    assert api.stream_frames[0]["content_raw"] == "hello world\u200b\n"


async def test_stream_buffers_unclosed_fence_until_the_block_closes() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(conversation, "before\n```python\n")
    await adapter.edit_text(ref, "before\n```python\nprint('hi')\n")

    assert len(api.stream_frames) == 1
    assert api.stream_frames[0]["content_raw"] == "before\u200b\n"

    await adapter.edit_text(
        ref, "before\n```python\nprint('hi')\n```"
    )

    assert len(api.stream_frames) == 2
    assert api.stream_frames[1]["content_raw"].startswith(
        api.stream_frames[0]["content_raw"]
    )
    assert "    print('hi')" in api.stream_frames[1]["content_raw"]


async def test_streaming_list_rendering_remains_prefix_stable() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()
    first = "- **Stiction**: static friction\n"
    final = (
        first
        + "- **Hysteresis**: delayed response\n"
        + "- **Dead zone**: no response near zero\n"
    )

    ref = await adapter.send_text(conversation, first)
    await adapter.edit_text(ref, final)
    await adapter.stop()

    assert len(api.stream_frames) == 3
    assert api.stream_frames[1]["content_raw"].startswith(
        api.stream_frames[0]["content_raw"]
    )
    assert api.stream_frames[-1]["input_state"] == STREAM_INPUT_STATE_DONE
    assert not api.active_sends
    assert not api.withdrawals


async def test_stream_withdraws_when_rendering_switches_to_compact_mode() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()
    first = "```\n" + "\n".join("x" for _ in range(600)) + "\n```\n"
    final = first + "tail\n" * 200

    ref = await adapter.send_text(conversation, first)
    await adapter.edit_text(ref, final)
    await adapter.stop()

    assert api.withdrawals == [
        {"openid": "OPENID-USER", "message_id": "stream-1"}
    ]
    assert len(api.stream_frames) == 1
    assert len(api.stream_frames[0]["content_raw"]) <= QQ_TEXT_LIMIT
    assert api.active_sends == [
        {
            "openid": "OPENID-USER",
            "msg_type": MSG_TYPE_MARKDOWN,
            "content": None,
            "markdown": {"content": sanitize_markdown(final)},
            "media": None,
            "msg_id": None,
            "event_id": None,
            "msg_seq": None,
        }
    ]


async def test_final_text_uses_regular_reply_without_closing_model_stream() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    model_message = await adapter.send_text(conversation, "partial\n")
    final_message = await adapter.send_final_text(conversation, "Status: ready")
    await adapter.edit_text(model_message, "partial\nanswer\n")
    try:
        assert [frame["input_state"] for frame in api.stream_frames] == [
            STREAM_INPUT_STATE_GENERATING,
            STREAM_INPUT_STATE_GENERATING,
        ]
        assert api.active_sends == [
            {
                "openid": "OPENID-USER",
                "msg_type": MSG_TYPE_MARKDOWN,
                "content": None,
                "markdown": {"content": "Status: ready"},
                "media": None,
                "msg_id": "MSGID-ANCHOR",
                "event_id": "evt-anchor",
                "msg_seq": 2,
            }
        ]
        assert not adapter._streams[model_message].finalized
        assert final_message not in adapter._streams
        assert set(adapter._streams) == {model_message}
    finally:
        await adapter.stop()


async def test_send_text_falls_back_to_active_after_budget_exhausted() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor(used=QQ_PASSIVE_REPLY_LIMIT)

    ref = await adapter.send_text(conversation, "hello")
    await adapter.stop()

    assert not api.stream_frames
    assert len(api.active_sends) == 1
    assert api.active_sends[0]["markdown"] == {"content": "hello"}
    assert api.active_sends[0]["msg_id"] is None
    assert ref == MessageRef(conversation, "text-1")


async def test_send_text_falls_back_when_anchor_window_expired() -> None:
    api = FakeQQBotAPI()
    clock = FakeClock()
    adapter = _make_qq_adapter(api, FakeQQGateway(), clock=clock)
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _ReplyAnchor(
        msg_id="m1", event_id="e1", received_at=clock.now
    )
    clock.now += QQ_PASSIVE_REPLY_WINDOW_SECONDS + 1

    await adapter.send_text(conversation, "hello")
    await adapter.stop()

    assert not api.stream_frames
    assert len(api.active_sends) == 1


async def test_active_fallback_coalesces_snapshots_into_one_final_send() -> None:
    api = FakeQQBotAPI()
    sleep = _GatedSleep()
    adapter = _make_qq_adapter(
        api, FakeQQGateway(), sleep=sleep, idle_timeout=5.0
    )
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor(used=QQ_PASSIVE_REPLY_LIMIT)
    ref = await adapter.send_text(conversation, "hello")

    await adapter.edit_text(ref, "hello again")
    await _wait_for(lambda: len(sleep.calls) == 1)
    await adapter.edit_text(ref, "hello again!")
    await _wait_for(lambda: len(sleep.calls) == 2)
    sleep.release(1)
    await _wait_for(lambda: len(api.active_sends) == 1)

    assert not api.stream_frames
    assert api.active_sends[-1]["markdown"] == {"content": "hello again!"}


async def test_non_monotonic_source_withdraws_partial_before_corrected_final() -> None:
    api = FakeQQBotAPI()
    sleep = _GatedSleep()
    adapter = _make_qq_adapter(
        api, FakeQQGateway(), sleep=sleep, idle_timeout=5.0
    )
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(conversation, "prefix one\n")
    await _wait_for(lambda: len(sleep.calls) == 1)
    await adapter.edit_text(ref, "prefix two")
    await _wait_for(lambda: len(sleep.calls) == 2)
    sleep.release(1)
    await _wait_for(lambda: len(api.active_sends) == 1)

    assert [frame["input_state"] for frame in api.stream_frames] == [
        STREAM_INPUT_STATE_GENERATING,
    ]
    assert api.withdrawals == [
        {"openid": "OPENID-USER", "message_id": "stream-1"}
    ]
    assert api.active_sends[0]["markdown"] == {"content": "prefix two"}


async def test_withdrawal_failure_retains_partial_and_sends_corrected_final(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeQQBotAPI()
    api.fail_withdraw = True
    sleep = _GatedSleep()
    adapter = _make_qq_adapter(
        api, FakeQQGateway(), sleep=sleep, idle_timeout=5.0
    )
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(conversation, "prefix one\n")
    await _wait_for(lambda: len(sleep.calls) == 1)
    await adapter.edit_text(ref, "corrected final")
    await _wait_for(lambda: len(sleep.calls) == 2)
    caplog.set_level(logging.WARNING, logger="kimi_bridge.platforms.qq")
    sleep.release(1)
    await _wait_for(lambda: len(api.active_sends) == 1)

    assert api.withdrawals == [
        {"openid": "OPENID-USER", "message_id": "stream-1"}
    ]
    assert [frame["input_state"] for frame in api.stream_frames] == [
        STREAM_INPUT_STATE_GENERATING,
        STREAM_INPUT_STATE_DONE,
    ]
    assert api.active_sends[0]["markdown"] == {"content": "corrected final"}
    assert any(
        "could not withdraw superseded partial response" in record.message
        for record in caplog.records
    )


async def test_stream_defang_retry_once_on_304003() -> None:
    api = FakeQQBotAPI()
    api.fail_url_once = True
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    await adapter.send_text(conversation, "visit https://example.com now\n")

    assert len(api.stream_frames) == 1
    assert "https://" not in api.stream_frames[0]["content_raw"]
    assert "example[.]com" in api.stream_frames[0]["content_raw"]


async def test_stream_defang_retry_preserves_the_delivered_prefix() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(
        conversation, "accepted https://example.com\n"
    )
    delivered_prefix = api.stream_frames[0]["content_raw"]
    api.fail_url_once = True

    await adapter.edit_text(
        ref,
        "accepted https://example.com\nthen https://blocked.example\n",
    )

    continuation = api.stream_frames[1]["content_raw"]
    assert continuation.startswith(delivered_prefix)
    assert "https://example.com" in continuation
    assert "blocked[.]example" in continuation


async def test_stream_edit_waits_for_in_progress_idle_finalization() -> None:
    api = FakeQQBotAPI()
    api.block_done = True
    sleep = _GatedSleep()
    adapter = _make_qq_adapter(
        api, FakeQQGateway(), sleep=sleep, idle_timeout=5.0
    )
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(conversation, "hello")
    await _wait_for(lambda: len(sleep.calls) == 1)
    sleep.release(0)
    await asyncio.wait_for(api.done_started.wait(), 1)

    edit = asyncio.create_task(adapter.edit_text(ref, "hello again"))
    await asyncio.sleep(0)
    assert not edit.done()

    api.release_done.set()
    await edit
    await adapter.stop()

    assert [frame["index"] for frame in api.stream_frames] == [0, 1]
    assert [frame["input_state"] for frame in api.stream_frames] == [
        STREAM_INPUT_STATE_GENERATING,
        STREAM_INPUT_STATE_DONE,
    ]
    assert api.active_sends[-1]["markdown"] == {"content": "hello again"}


async def test_stop_finalizes_an_open_stream_before_closing_transport() -> None:
    api = FakeQQBotAPI()
    sleep = _GatedSleep()
    token_manager = FakeTokenProvider()
    adapter = _make_qq_adapter(
        api,
        FakeQQGateway(),
        sleep=sleep,
        idle_timeout=5.0,
        token_manager=token_manager,
    )
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    await adapter.send_text(conversation, "hello")
    await _wait_for(lambda: len(sleep.calls) == 1)
    await adapter.stop()

    assert api.stream_frames[-1]["input_state"] == STREAM_INPUT_STATE_DONE
    assert api.closed
    assert token_manager.closed


async def test_idle_timeout_sends_done_frame() -> None:
    api = FakeQQBotAPI()
    sleep = _GatedSleep()
    adapter = _make_qq_adapter(api, FakeQQGateway(), sleep=sleep, idle_timeout=5.0)
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(conversation, "hello")
    await _wait_for(lambda: len(sleep.calls) == 1)
    assert sleep.calls[0] == 5.0

    sleep.release(0)
    await _wait_for(lambda: len(api.stream_frames) == 2)

    assert api.stream_frames[1]["input_state"] == STREAM_INPUT_STATE_DONE
    assert api.stream_frames[1]["index"] == 1
    assert api.stream_frames[1]["stream_msg_id"] == (
        adapter._streams[ref].stream_msg_id
    )
    assert api.stream_frames[1]["content_raw"].startswith("hello")
    assert api.stream_frames[1]["content_raw"] != "hello"


async def test_idle_timeout_contains_done_api_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeQQBotAPI()
    api.fail_done_once = True
    sleep = _GatedSleep()
    adapter = _make_qq_adapter(api, FakeQQGateway(), sleep=sleep, idle_timeout=5.0)
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_text(conversation, "hello")
    await _wait_for(lambda: len(sleep.calls) == 1)
    caplog.set_level(logging.ERROR, logger="kimi_bridge.platforms.qq")

    sleep.release(0)
    await _wait_for(lambda: adapter._streams[ref].idle_task is None)

    records = [
        record
        for record in caplog.records
        if record.name == "kimi_bridge.platforms.qq"
    ]
    assert [record.args for record in records] == [(40006,)]


async def test_typing_indicator_sent_on_inbound_and_keepalive() -> None:
    api = FakeQQBotAPI()
    gateway = FakeQQGateway()
    sleep = _GatedSleep()
    adapter = _make_qq_adapter(api, gateway, sleep=sleep, typing_keepalive=50.0)
    delivered: list[Any] = []

    async def on_message(_sender: Any, message: Any) -> None:
        delivered.append(message)

    await adapter.start(on_message, _noop_on_interaction)
    try:
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload("MSGID-1", "hi"),
                seq=1,
                event_id="evt-1",
            )
        )

        assert len(delivered) == 1
        await _wait_for(lambda: len(api.typing_calls) == 1)
        assert api.typing_calls[0]["msg_id"] == "MSGID-1"
        await _wait_for(lambda: len(sleep.calls) >= 1)
        assert sleep.calls[0] == 50.0

        sleep.release(0)
        await _wait_for(lambda: len(api.typing_calls) == 2)
    finally:
        await adapter.stop()


async def test_typing_indicator_stops_when_a_reply_starts() -> None:
    api = FakeQQBotAPI()
    gateway = FakeQQGateway()
    sleep = _GatedSleep()
    adapter = _make_qq_adapter(api, gateway, sleep=sleep, typing_keepalive=50.0)

    async def on_message(sender: QQAdapter, message: Any) -> None:
        await _wait_for(lambda: len(api.typing_calls) == 1)
        await sender.send_final_text(message.conversation, "ready")

    await adapter.start(on_message, _noop_on_interaction)
    try:
        await gateway.emit(
            QQGatewayEvent(
                type="C2C_MESSAGE_CREATE",
                data=_c2c_payload("MSGID-1", "hi"),
                seq=1,
                event_id="evt-1",
            )
        )
        await asyncio.sleep(0)

        conversation = ConversationRef("qq", "app-1", "OPENID-USER")
        assert conversation not in adapter._typing_tasks
        assert len(api.typing_calls) == 1
        assert api.active_sends[-1]["markdown"] == {"content": "ready"}
    finally:
        await adapter.stop()


# --- defensive interactions -----------------------------------------------


async def test_present_interaction_sends_defensive_notice() -> None:
    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.present_interaction(conversation, object())

    assert not api.stream_frames
    assert len(api.active_sends) == 1
    assert "not supported on QQ" in api.active_sends[0]["markdown"]["content"]
    assert ref not in adapter._streams
    assert await adapter.finish_interaction(ref, object()) is None


async def test_send_file_uploads_image_and_sends_media() -> None:
    import base64

    from kimi_bridge.platforms.base import OutboundFile

    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor()

    ref = await adapter.send_file(
        conversation,
        OutboundFile(name="pic.png", data=b"PNGDATA", media_type="image/png"),
    )

    assert len(api.uploads) == 1
    assert api.uploads[0]["file_type"] == 1
    assert api.uploads[0]["file_data"] == base64.b64encode(b"PNGDATA").decode()
    assert api.uploads[0]["srv_send_msg"] is False
    assert len(api.active_sends) == 1
    send = api.active_sends[0]
    assert send["msg_type"] == 7
    assert send["media"] == {"file_info": "OPAQUE-FILE-INFO"}
    assert send["msg_id"] == "MSGID-ANCHOR"
    assert send["msg_seq"] == 1
    assert ref == MessageRef(conversation, "active-1")


async def test_send_file_uploads_video_via_file_type_two() -> None:
    from kimi_bridge.platforms.base import OutboundFile

    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")
    adapter._anchors[conversation] = _anchor(used=QQ_PASSIVE_REPLY_LIMIT)

    await adapter.send_file(
        conversation,
        OutboundFile(name="clip.mp4", data=b"MP4DATA", media_type="video/mp4"),
    )

    assert api.uploads[0]["file_type"] == 2
    assert api.active_sends[0]["msg_id"] is None


async def test_send_file_rejects_unsupported_media_type() -> None:
    from kimi_bridge.platforms.base import OutboundFile

    api = FakeQQBotAPI()
    adapter = _make_qq_adapter(api, FakeQQGateway())
    conversation = ConversationRef("qq", "app-1", "OPENID-USER")

    with pytest.raises(ValueError, match="png/jpg images and mp4 video"):
        await adapter.send_file(
            conversation,
            OutboundFile(name="a.txt", data=b"x", media_type="text/plain"),
        )

    assert not api.uploads


# --- end-to-end: real WS gateway -> real QQAdapter ------------------------


async def test_end_to_end_gateway_delivers_and_streams_with_budget_fallback() -> None:
    api = FakeQQBotAPI()
    socket = FakeGatewaySocket(
        [_hello(), _ready(seq=1, session_id="sess-1"), _c2c(2, "hi", "evt-1")]
    )
    connect = FakeGatewayConnect([socket])

    async def get_gateway_url() -> str:
        return "wss://api.sgroup.qq.com/websocket/"

    gateway = QQGatewayClient(
        FakeTokenProvider(),
        get_gateway_url,
        ws_connect=connect,
        initial_backoff=0.01,
        max_backoff=0.02,
    )
    adapter = QQAdapter(
        "app-1",
        frozenset({"OPENID-USER"}),
        api=api,
        gateway=gateway,
        clock=lambda: 1_000.0,
    )
    refs: list[Any] = []

    async def on_message(sender: Any, message: Any) -> None:
        for chunk in ("first", "second", "third", "fourth", "fifth"):
            refs.append(await sender.send_text(message.conversation, chunk))

    async def on_interaction(_sender: Any, _interaction: Any) -> None:
        pass

    await adapter.start(on_message, on_interaction)
    try:
        await _wait_for(lambda: len(refs) == 5)
    finally:
        await adapter.stop()

    assert [frame["input_state"] for frame in api.stream_frames] == [
        state
        for _ in range(4)
        for state in (STREAM_INPUT_STATE_GENERATING, STREAM_INPUT_STATE_DONE)
    ]
    assert [frame["msg_seq"] for frame in api.stream_frames] == [
        sequence
        for sequence in range(1, 5)
        for _ in range(2)
    ]
    assert [frame["index"] for frame in api.stream_frames] == [0, 1] * 4
    assert len(api.active_sends) == 1
