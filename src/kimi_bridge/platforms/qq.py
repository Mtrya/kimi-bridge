"""QQ official bot transport: access tokens, REST client, WS gateway client.

WS gateway contract, verified 2026-07-25 against the official docs source
(`github.com/tencent-connect/bot-docs`, docs/develop/api-v2), the archived
`tencent-connect/botpy` SDK, and the maintained `nonebot/adapter-qq`:

- REST base is ``https://api.sgroup.qq.com`` (sandbox:
  ``https://sandbox.api.sgroup.qq.com``). The ``api.bot.qq.com`` host from the
  early research handoff does not exist in any primary source.
- ``GET /gateway`` returns ``{"url": "wss://api.sgroup.qq.com/websocket/"}``.
  ``GET /gateway/bot`` additionally returns ``shards`` and
  ``session_start_limit`` — unnecessary for a single-shard bridge.
- Envelope: ``{"id", "op", "s", "t", "d"}``. ``s`` is the downstream sequence
  echoed in heartbeats and resume; ``t`` names the event when ``op`` is 0;
  ``id`` appears on dispatch frames and doubles as the passive ``event_id``.
- Op codes: 0 Dispatch, 1 Heartbeat (both directions), 2 Identify, 6 Resume,
  7 Reconnect, 9 Invalid Session, 10 Hello, 11 Heartbeat ACK. Codes 12/13 are
  webhook-only; 3-5 and 8 do not exist.
- Handshake: the server opens with Hello
  ``{"op": 10, "d": {"heartbeat_interval": <milliseconds>}}``; the client
  sends Identify ``{"op": 2, "d": {"token": "QQBot {access_token}",
  "intents": <bitmask>, "shard": [n, total], "properties": {}}}`` and receives
  a ``READY`` dispatch whose ``d`` carries ``session_id``.
- Heartbeat: ``{"op": 1, "d": <last seq or null>}`` every interval; the
  server ACKs with op 11 and may itself send op 1 to request an immediate
  beat.
- Resume: ``{"op": 6, "d": {"token", "session_id", "seq"}}``; the server
  replays events after ``seq`` then dispatches ``t: "RESUMED"`` (whose ``d``
  is an empty string). Op 9 invalidates the session (identify from scratch);
  op 7 asks the client to reconnect (resume allowed).
- Intents: ``GROUP_AND_C2C_EVENT = 1 << 25`` covers ``C2C_MESSAGE_CREATE``
  (and friend/group membership events). Unauthorized intents make the server
  error and close the connection.
- Access token: ``POST https://bots.qq.com/app/getAppAccessToken`` with
  ``{"appId", "clientSecret"}`` returns ``{"access_token", "expires_in"}``
  (``expires_in`` arrives as a JSON string in practice, TTL 7200 s).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from ..interactions import InteractionOutcome, InteractionPrompt
from .base import (
    ActorRef,
    ConversationRef,
    InboundAudio,
    InboundFile,
    InboundHandler,
    InboundImage,
    InboundMessage,
    InboundVideo,
    InteractionHandler,
    MessageRef,
    OutboundFile,
)


LOGGER = logging.getLogger(__name__)

QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_API_BASE_URL = "https://api.sgroup.qq.com"
QQ_TOKEN_REFRESH_MARGIN = 60.0
GROUP_AND_C2C_EVENT_INTENT = 1 << 25

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
MSG_TYPE_TYPING = 6
MSG_TYPE_MEDIA = 7

# QQ's stream_messages input_state enum (protocol/types.ts StreamInputState).
STREAM_INPUT_STATE_GENERATING = 1
STREAM_INPUT_STATE_DONE = 10

# `file_type` on `POST /v2/users/{openid}/files`.
# Voice (3) is not supported. Arbitrary files (4) were once closed in C2C but
# are now open: any format, 200 MB hard limit, delivered as a file card.
QQ_FILE_TYPE_IMAGE = 1
QQ_FILE_TYPE_VIDEO = 2
QQ_FILE_TYPE_FILE = 4
_QQ_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg"})
_QQ_VIDEO_MEDIA_TYPE = "video/mp4"

# Live validation accepted 5,000 visible characters plus the invisible DONE suffix.
QQ_TEXT_LIMIT = 5000
QQ_PASSIVE_REPLY_LIMIT = 4
QQ_PASSIVE_REPLY_WINDOW_SECONDS = 60 * 60
QQ_TYPING_KEEPALIVE_SECONDS = 50
QQ_TYPING_INPUT_SECONDS = 60
QQ_STREAM_IDLE_TIMEOUT_SECONDS = 6.0
QQ_URL_DEFANG_ERROR_CODE = 304003
QQ_ATTACHMENT_LIMIT_BYTES = 20 * 1024 * 1024
_QQ_DEDUPE_MEMORY = 512
_QQ_STREAM_DONE_SUFFIX = "\u200b"


class QQError(RuntimeError):
    """Base exception for the QQ boundary."""


class QQProtocolError(QQError):
    """QQ returned a shape that violates the documented contract."""


class QQTransportError(QQError):
    """A QQ HTTP request failed after transient retries."""


class QQAttachmentTooLarge(QQError):
    """A QQ inbound attachment exceeds the bridge's download limit."""

    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        if limit_bytes % (1024 * 1024) == 0:
            limit = f"{limit_bytes // (1024 * 1024)} MB"
        else:
            limit = f"{limit_bytes} bytes"
        super().__init__(f"QQ attachment exceeds the {limit} download limit")


class QQAPIError(QQError):
    """QQ returned a structured API failure."""

    def __init__(self, context: str, code: int, message: str) -> None:
        self.context = context
        self.code = code
        self.message = message
        super().__init__(f"QQ API {context} failed ({code}): {message}")


@dataclass(frozen=True, slots=True)
class QQCredentials:
    app_id: str
    app_secret: str

    def __post_init__(self) -> None:
        if not self.app_id:
            raise ValueError("app_id must be non-empty")
        if not self.app_secret:
            raise ValueError("app_secret must be non-empty")


class QQTokenProvider(Protocol):
    async def authorization(self) -> str: ...


def _validate_retry_policy(
    max_retries: int, initial_backoff: float, max_backoff: float
) -> None:
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    if initial_backoff <= 0 or max_backoff <= 0:
        raise ValueError("retry backoff must be positive")
    if initial_backoff > max_backoff:
        raise ValueError("initial_backoff cannot exceed max_backoff")


async def _send_with_retries(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    context: str,
    sleep: Callable[[float], Awaitable[None]],
    max_retries: int,
    initial_backoff: float,
    max_backoff: float,
) -> httpx.Response:
    """Retry transport failures, 429, and 5xx; raise after exhaustion."""

    attempt = 0
    while True:
        try:
            response = await send()
        except asyncio.CancelledError:
            raise
        except httpx.RequestError:
            if attempt >= max_retries:
                raise QQTransportError(f"QQ {context} request failed") from None
            await sleep(min(initial_backoff * (2**attempt), max_backoff))
            attempt += 1
            continue
        retryable = response.status_code == 429 or response.status_code >= 500
        if retryable:
            if attempt >= max_retries:
                raise QQTransportError(
                    f"QQ {context} request failed with HTTP {response.status_code}"
                )
            await sleep(min(initial_backoff * (2**attempt), max_backoff))
            attempt += 1
            continue
        return response


def _response_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return None


def _error_from_envelope(context: str, envelope: object) -> QQAPIError | None:
    if not isinstance(envelope, dict):
        return None
    code = envelope.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    message = envelope.get("message")
    if not isinstance(message, str) or not message:
        message = "unknown error"
    return QQAPIError(context, code, message)


def _raise_for_error(
    context: str, response: httpx.Response, envelope: object
) -> None:
    if 200 <= response.status_code < 300:
        return
    error = _error_from_envelope(context, envelope)
    if error is not None:
        raise error
    raise QQAPIError(
        context, response.status_code, f"HTTP {response.status_code}"
    )


def _expires_in_seconds(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None
    if isinstance(value, int) and value > 0:
        return float(value)
    return None


class QQTokenManager:
    """Fetch and cache the shared bot access token for REST and WS identify."""

    def __init__(
        self,
        credentials: QQCredentials,
        *,
        http_client: httpx.AsyncClient | None = None,
        token_url: str = QQ_TOKEN_URL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 4,
        initial_backoff: float = 0.5,
        max_backoff: float = 8.0,
    ) -> None:
        _validate_retry_policy(max_retries, initial_backoff, max_backoff)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._credentials = credentials
        self._http = http_client or httpx.AsyncClient()
        self._owns_http = http_client is None
        self._token_url = token_url
        self._clock = clock
        self._sleep = sleep
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._closed = False

    async def access_token(self) -> str:
        if self._closed:
            raise RuntimeError("QQ token manager is closed")
        async with self._lock:
            if (
                self._token is None
                or self._clock() >= self._expires_at - QQ_TOKEN_REFRESH_MARGIN
            ):
                await self._refresh()
            assert self._token is not None
            return self._token

    async def authorization(self) -> str:
        return f"QQBot {await self.access_token()}"

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            await self._http.aclose()

    async def _refresh(self) -> None:
        response = await _send_with_retries(
            lambda: self._http.post(
                self._token_url,
                json={
                    "appId": self._credentials.app_id,
                    "clientSecret": self._credentials.app_secret,
                },
                timeout=30.0,
            ),
            context="getAppAccessToken",
            sleep=self._sleep,
            max_retries=self._max_retries,
            initial_backoff=self._initial_backoff,
            max_backoff=self._max_backoff,
        )
        envelope = _response_json(response)
        _raise_for_error("getAppAccessToken", response, envelope)
        if not isinstance(envelope, dict):
            raise QQProtocolError("QQ access token response must be an object")
        token = envelope.get("access_token")
        ttl = _expires_in_seconds(envelope.get("expires_in"))
        if not isinstance(token, str) or not token or ttl is None:
            error = _error_from_envelope("getAppAccessToken", envelope)
            if error is not None:
                raise error
            raise QQProtocolError("QQ access token response is invalid")
        self._token = token
        self._expires_at = self._clock() + ttl


class QQBotAPI:
    """Small async client for the QQ OpenAPI methods used by the bridge."""

    def __init__(
        self,
        token_manager: QQTokenProvider,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = QQ_API_BASE_URL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 4,
        initial_backoff: float = 0.5,
        max_backoff: float = 8.0,
    ) -> None:
        _validate_retry_policy(max_retries, initial_backoff, max_backoff)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._token_manager = token_manager
        self._http = http_client or httpx.AsyncClient()
        self._owns_http = http_client is None
        self._base_url = api_base_url.rstrip("/")
        self._sleep = sleep
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._closed = False

    async def get_gateway_url(self) -> str:
        data = await self._request("GET", "/gateway")
        if not isinstance(data, dict):
            raise QQProtocolError("QQ gateway response must be an object")
        url = data.get("url")
        if not isinstance(url, str) or not url:
            raise QQProtocolError("QQ gateway response omitted url")
        return url

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
        _require_openid(openid)
        body: dict[str, Any] = {"msg_type": msg_type}
        if content is not None:
            body["content"] = content
        if markdown is not None:
            body["markdown"] = markdown
        if media is not None:
            body["media"] = media
        if msg_id is not None:
            body["msg_id"] = msg_id
        if event_id is not None:
            body["event_id"] = event_id
        if msg_seq is not None:
            body["msg_seq"] = msg_seq
        data = await self._request(
            "POST", f"/v2/users/{openid}/messages", json_body=body
        )
        if not isinstance(data, dict):
            raise QQProtocolError("QQ send message response must be an object")
        return data

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
        _require_openid(openid)
        body: dict[str, Any] = {
            "input_mode": input_mode,
            "input_state": input_state,
            "content_type": content_type,
            "content_raw": content_raw,
            "msg_id": msg_id,
            "msg_seq": msg_seq,
            "index": index,
        }
        if event_id is not None:
            body["event_id"] = event_id
        if stream_msg_id is not None:
            body["stream_msg_id"] = stream_msg_id
        data = await self._request(
            "POST", f"/v2/users/{openid}/stream_messages", json_body=body
        )
        if not isinstance(data, dict):
            raise QQProtocolError("QQ stream message response must be an object")
        return data

    async def delete_c2c_message(self, openid: str, message_id: str) -> None:
        _require_openid(openid)
        if not message_id:
            raise ValueError("message_id must be non-empty")
        await self._request(
            "DELETE", f"/v2/users/{openid}/messages/{message_id}"
        )

    async def send_c2c_typing(
        self,
        openid: str,
        *,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        input_second: int = QQ_TYPING_INPUT_SECONDS,
    ) -> dict[str, Any]:
        _require_openid(openid)
        body: dict[str, Any] = {
            "msg_type": MSG_TYPE_TYPING,
            "input_notify": {"input_type": 1, "input_second": input_second},
        }
        if msg_seq is not None:
            body["msg_seq"] = msg_seq
        if msg_id is not None:
            body["msg_id"] = msg_id
        data = await self._request(
            "POST", f"/v2/users/{openid}/messages", json_body=body
        )
        if not isinstance(data, dict):
            raise QQProtocolError("QQ typing indicator response must be an object")
        return data

    async def upload_c2c_media(
        self,
        openid: str,
        *,
        file_type: int,
        url: str | None = None,
        file_data: str | None = None,
        file_name: str | None = None,
        srv_send_msg: bool = False,
    ) -> dict[str, Any]:
        _require_openid(openid)
        if url is None and file_data is None:
            raise ValueError("QQ media upload requires url or file_data")
        body: dict[str, Any] = {
            "file_type": file_type,
            "srv_send_msg": srv_send_msg,
        }
        if url is not None:
            body["url"] = url
        if file_data is not None:
            body["file_data"] = file_data
        if file_name is not None:
            body["file_name"] = file_name
        data = await self._request(
            "POST", f"/v2/users/{openid}/files", json_body=body
        )
        if not isinstance(data, dict):
            raise QQProtocolError("QQ media upload response must be an object")
        return data

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            await self._http.aclose()

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> Any:
        if self._closed:
            raise RuntimeError("QQ Bot API client is closed")
        context = f"{method} {path}"

        async def send() -> httpx.Response:
            headers = {
                "Authorization": await self._token_manager.authorization()
            }
            return await self._http.request(
                method,
                f"{self._base_url}{path}",
                json=json_body,
                headers=headers,
                timeout=30.0,
            )

        response = await _send_with_retries(
            send,
            context=context,
            sleep=self._sleep,
            max_retries=self._max_retries,
            initial_backoff=self._initial_backoff,
            max_backoff=self._max_backoff,
        )
        envelope = _response_json(response)
        _raise_for_error(context, response, envelope)
        return envelope


def _require_openid(openid: str) -> None:
    if not isinstance(openid, str) or not openid:
        raise ValueError("QQ openid must be non-empty")


@dataclass(frozen=True, slots=True)
class QQGatewayEvent:
    """One decoded gateway dispatch, minus connection-lifecycle events."""

    type: str
    data: Any
    seq: int | None
    event_id: str | None


GatewayEventHandler = Callable[[QQGatewayEvent], Awaitable[None]]


class QQGatewaySocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


class QQGatewayClient:
    """Resilient WS gateway loop: identify, heartbeat, dispatch, resume."""

    def __init__(
        self,
        token_manager: QQTokenProvider,
        get_gateway_url: Callable[[], Awaitable[str]],
        *,
        ws_connect: Callable[..., Any] = websockets.connect,
        intents: int = GROUP_AND_C2C_EVENT_INTENT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        if initial_backoff <= 0 or max_backoff <= 0:
            raise ValueError("reconnect backoff must be positive")
        if initial_backoff > max_backoff:
            raise ValueError("initial_backoff cannot exceed max_backoff")
        self._token_manager = token_manager
        self._get_gateway_url = get_gateway_url
        self._ws_connect = ws_connect
        self._intents = intents
        self._sleep = sleep
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._backoff = initial_backoff
        self._on_event: GatewayEventHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._event_queue: asyncio.Queue[QQGatewayEvent] | None = None
        self._event_worker: asyncio.Task[None] | None = None
        self._closed = False
        self._session_id: str | None = None
        self._last_seq: int | None = None
        self._last_event_id: str | None = None

    @property
    def last_seq(self) -> int | None:
        return self._last_seq

    @property
    def last_event_id(self) -> str | None:
        return self._last_event_id

    async def start(self, on_event: GatewayEventHandler) -> None:
        if self._task is not None:
            raise RuntimeError("QQ gateway client is already started")
        if self._closed:
            raise RuntimeError("QQ gateway client is closed")
        self._on_event = on_event
        self._task = asyncio.create_task(self._run(), name="qq-gateway")

    async def wait(self) -> None:
        if self._task is None:
            raise RuntimeError("QQ gateway client has not been started")
        await self._task

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        self._event_queue = asyncio.Queue()
        self._event_worker = asyncio.create_task(
            self._dispatch_events(),
            name="qq-gateway-event-worker",
        )
        try:
            while not self._closed:
                try:
                    url = await self._get_gateway_url()
                    async with self._ws_connect(url) as ws:
                        await self._run_connection(ws)
                except asyncio.CancelledError:
                    raise
                except (ConnectionClosed, OSError, QQTransportError) as exc:
                    LOGGER.warning("QQ gateway connection lost: %s", exc)
                if self._closed:
                    return
                delay = self._backoff
                self._backoff = min(self._backoff * 2, self._max_backoff)
                await self._sleep(delay)
        finally:
            event_worker = self._event_worker
            if event_worker is not None and not event_worker.done():
                event_worker.cancel()
            if event_worker is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await event_worker
            self._event_worker = None
            self._event_queue = None

    async def _dispatch_events(self) -> None:
        queue = self._event_queue
        on_event = self._on_event
        assert queue is not None
        assert on_event is not None
        while True:
            event = await queue.get()
            try:
                await on_event(event)
            finally:
                queue.task_done()

    async def _run_connection(self, ws: QQGatewaySocket) -> None:
        event_worker = self._event_worker
        if event_worker is None:
            raise RuntimeError("QQ gateway event worker is not running")
        heartbeat_interval = await self._expect_hello(ws)
        if self._session_id is not None and self._last_seq is not None:
            await self._send_resume(ws)
        else:
            await self._send_identify(ws)
        heartbeat_acknowledged = asyncio.Event()
        heartbeat_acknowledged.set()
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(
                ws,
                heartbeat_interval,
                heartbeat_acknowledged,
            ),
            name="qq-gateway-heartbeat",
        )
        receive: asyncio.Task[str | bytes] | None = None
        try:
            while True:
                receive = asyncio.create_task(
                    ws.recv(), name="qq-gateway-receive"
                )
                done, _pending = await asyncio.wait(
                    {receive, heartbeat, event_worker},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if event_worker in done:
                    if not receive.done():
                        receive.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await receive
                    try:
                        event_worker.result()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        raise RuntimeError(
                            "QQ gateway event handler failed"
                        ) from exc
                    raise RuntimeError(
                        "QQ gateway event worker stopped unexpectedly"
                    )
                if heartbeat in done:
                    if not receive.done():
                        receive.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await receive
                    heartbeat.result()
                    return
                frame = _decode_frame(receive.result())
                receive = None
                op = frame.get("op")
                if op == OP_DISPATCH:
                    await self._handle_dispatch(frame)
                elif op == OP_HEARTBEAT:
                    await self._send_heartbeat(ws)
                elif op == OP_HEARTBEAT_ACK:
                    heartbeat_acknowledged.set()
                elif op == OP_RECONNECT:
                    LOGGER.info("QQ gateway requested a reconnect")
                    return
                elif op == OP_INVALID_SESSION:
                    LOGGER.warning(
                        "QQ gateway invalidated the session; re-identifying"
                    )
                    self._session_id = None
                    self._last_seq = None
                    return
                else:
                    LOGGER.warning("QQ gateway sent unknown op code %r", op)
        finally:
            if receive is not None and not receive.done():
                receive.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _handle_dispatch(self, frame: dict[str, Any]) -> None:
        seq = frame.get("s")
        if isinstance(seq, bool) or not isinstance(seq, int):
            seq = None
        if seq is not None:
            self._last_seq = seq
        event_type = frame.get("t")
        if not isinstance(event_type, str) or not event_type:
            raise QQProtocolError("QQ gateway dispatch omitted its event type")
        data = frame.get("d")
        if event_type == "READY":
            if not isinstance(data, dict):
                raise QQProtocolError("QQ gateway READY payload must be an object")
            session_id = data.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise QQProtocolError("QQ gateway READY omitted session_id")
            self._session_id = session_id
            self._backoff = self._initial_backoff
            return
        if event_type == "RESUMED":
            self._backoff = self._initial_backoff
            return
        event_id = frame.get("id")
        if not isinstance(event_id, str) or not event_id:
            event_id = None
        else:
            self._last_event_id = event_id
        assert self._on_event is not None
        queue = self._event_queue
        if queue is None:
            raise RuntimeError("QQ gateway event queue is not available")
        queue.put_nowait(
            QQGatewayEvent(
                type=event_type, data=data, seq=seq, event_id=event_id
            )
        )

    async def _expect_hello(self, ws: QQGatewaySocket) -> float:
        frame = _decode_frame(await ws.recv())
        if frame.get("op") != OP_HELLO:
            raise QQProtocolError("QQ gateway did not open with hello")
        data = frame.get("d")
        interval = data.get("heartbeat_interval") if isinstance(data, dict) else None
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or interval <= 0
        ):
            raise QQProtocolError(
                "QQ gateway hello omitted a valid heartbeat_interval"
            )
        return interval / 1000.0

    async def _send_identify(self, ws: QQGatewaySocket) -> None:
        token = await self._token_manager.authorization()
        await ws.send(
            json.dumps(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": token,
                        "intents": self._intents,
                        "shard": [0, 1],
                        "properties": {},
                    },
                }
            )
        )

    async def _send_resume(self, ws: QQGatewaySocket) -> None:
        token = await self._token_manager.authorization()
        await ws.send(
            json.dumps(
                {
                    "op": OP_RESUME,
                    "d": {
                        "token": token,
                        "session_id": self._session_id,
                        "seq": self._last_seq,
                    },
                }
            )
        )

    async def _send_heartbeat(self, ws: QQGatewaySocket) -> None:
        await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._last_seq}))

    async def _heartbeat_loop(
        self,
        ws: QQGatewaySocket,
        interval: float,
        acknowledged: asyncio.Event,
    ) -> None:
        try:
            while True:
                await self._sleep(interval)
                if not acknowledged.is_set():
                    raise QQTransportError(
                        "QQ gateway heartbeat acknowledgement timed out"
                    )
                acknowledged.clear()
                await self._send_heartbeat(ws)
        except ConnectionClosed:
            return


def _decode_frame(raw: str | bytes) -> dict[str, Any]:
    try:
        frame = json.loads(raw)
    except ValueError as exc:
        raise QQProtocolError("QQ gateway frame is not valid JSON") from exc
    if not isinstance(frame, dict):
        raise QQProtocolError("QQ gateway frame must be an object")
    return frame


_ZERO_WIDTH_SPACE = "\u200b"
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"```[^\n]*\n")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_URL_RE = re.compile(r"(https?://)(\S+)")
_EMPHASIS_SPACE_RE = re.compile(r"(?<=\S)(\*{1,2}|_{1,2})(?= )")


def sanitize_markdown(text: str) -> str:
    """Rewrite text into QQ's supported markdown subset.

    QQ renders four-space-indented text as a code block but not inline code
    or tables; a single newline also collapses into the surrounding paragraph
    unless forced with a trailing zero-width space. Headings, bold/italic,
    lists, and quotes pass through unchanged. If those rendering aids would
    exceed QQ's message limit, formatting degrades without dropping content.
    """

    return _render_markdown(text, force_line_breaks=_force_line_breaks)


def _render_markdown(
    source: str, *, force_line_breaks: Callable[[str], str]
) -> str:
    rendered = _flatten_fenced_code(source)
    rendered = _flatten_tables(rendered)
    rendered = _strip_inline_code(rendered)
    rendered = _preserve_emphasis_spacing(rendered)
    rendered = force_line_breaks(rendered)
    if len(rendered) <= QQ_TEXT_LIMIT:
        return rendered

    return _render_compact_markdown(source)


def _render_compact_markdown(source: str) -> str:
    compact = _strip_fenced_code(source)
    compact = _strip_table_separators(compact)
    compact = _strip_inline_code(compact)
    if len(compact) <= QQ_TEXT_LIMIT:
        return compact
    raise ValueError(
        f"QQ text exceeds {QQ_TEXT_LIMIT} characters after rendering"
    )


def _sanitize_stable_markdown(text: str, *, final: bool = False) -> str:
    source = text if final else _stable_markdown_source(text)
    return _render_compact_markdown(source)


def _stable_markdown_source(text: str) -> str:
    """Return complete lines and closed fenced blocks from a growing snapshot."""

    stable_end = text.rfind("\n") + 1
    search_from = 0
    while opening := _FENCE_OPEN_RE.search(text, search_from):
        closing = text.find("```", opening.end())
        if closing < 0:
            return text[: min(stable_end, opening.start())]
        closing_end = closing + 3
        if closing_end > stable_end:
            stable_end = closing_end
        search_from = closing_end
    return text[:stable_end]


def defang_urls(text: str) -> str:
    """Strip the scheme and bracket dots so QQ's 304003 URL filter passes."""

    return _URL_RE.sub(lambda match: match.group(2).replace(".", "[.]"), text)


def _flatten_fenced_code(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        body = match.group(1).rstrip("\n")
        return "\n".join(f"    {line}" for line in body.splitlines())

    return _FENCE_RE.sub(_replace, text)


def _strip_fenced_code(text: str) -> str:
    return _FENCE_RE.sub(lambda match: match.group(1).rstrip("\n"), text)


def _strip_inline_code(text: str) -> str:
    return _INLINE_CODE_RE.sub(lambda match: match.group(1), text)


def _preserve_emphasis_spacing(text: str) -> str:
    return _EMPHASIS_SPACE_RE.sub(
        lambda match: match.group(1) + _ZERO_WIDTH_SPACE,
        text,
    )


def _flatten_tables(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        if "|" in line and _TABLE_SEPARATOR_RE.match(line):
            continue
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            lines.append(" | ".join(cell for cell in cells if cell))
            continue
        lines.append(line)
    return "\n".join(lines)


def _strip_table_separators(text: str) -> str:
    return "\n".join(
        line
        for line in text.split("\n")
        if not ("|" in line and _TABLE_SEPARATOR_RE.match(line))
    )


def _force_line_breaks(text: str) -> str:
    lines = text.split("\n")
    last = len(lines) - 1
    forced = [
        line + _ZERO_WIDTH_SPACE
        if index != last and line and lines[index + 1] != ""
        else line
        for index, line in enumerate(lines)
    ]
    return "\n".join(forced)


def _require_response_id(data: dict[str, Any], context: str) -> str:
    message_id = data.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise QQProtocolError(f"QQ {context} response omitted id")
    return message_id


def _parse_qq_timestamp(value: object) -> float:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    return time.time()


def _dedupe_key(data: dict[str, Any], msg_id: str) -> str:
    scene = data.get("message_scene")
    if isinstance(scene, dict):
        ext = scene.get("ext")
        if isinstance(ext, list):
            for item in ext:
                if isinstance(item, str) and item.startswith("msg_idx="):
                    return item
    return msg_id


@dataclass(slots=True)
class _ReplyAnchor:
    """Passive-reply budget for the most recent inbound message per chat."""

    msg_id: str
    event_id: str | None
    received_at: float
    used: int = 0

    def reserve(self, *, now: float) -> int | None:
        if now - self.received_at > QQ_PASSIVE_REPLY_WINDOW_SECONDS:
            return None
        if self.used >= QQ_PASSIVE_REPLY_LIMIT:
            return None
        self.used += 1
        return self.used


@dataclass(slots=True)
class _StreamState:
    """Per-`MessageRef` `stream_messages` bookkeeping."""

    conversation: ConversationRef
    openid: str
    anchor_msg_id: str
    event_id: str | None
    msg_seq: int
    streamable: bool
    stream_msg_id: str | None = None
    delivered_message_id: str | None = None
    next_index: int = 0
    last_text: str = ""
    last_rendered_text: str = ""
    last_source_text: str = ""
    pending_text: str | None = None
    finalized: bool = False
    idle_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class QQAdapter:
    """C2C-only QQ official bot adapter: gateway inbound, streamed outbound."""

    name = "qq"
    message_limit = QQ_TEXT_LIMIT
    supports_edits = True
    supports_interactions = False
    message_edit_limit = None

    def __init__(
        self,
        app_id: str,
        allowed_users: set[str] | frozenset[str],
        *,
        api: QQBotAPI,
        gateway: QQGatewayClient,
        token_manager: QQTokenManager | None = None,
        http_client: httpx.AsyncClient | None = None,
        idle_timeout: float = QQ_STREAM_IDLE_TIMEOUT_SECONDS,
        typing_keepalive: float = QQ_TYPING_KEEPALIVE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not app_id:
            raise ValueError("app_id must be non-empty")
        if not allowed_users:
            raise ValueError("allowed_users must be non-empty")
        self._app_id = app_id
        self._allowed_users = frozenset(allowed_users)
        self._api = api
        self._gateway = gateway
        self._token_manager = token_manager
        self._http = http_client or httpx.AsyncClient()
        self._owns_http = http_client is None
        self._idle_timeout = idle_timeout
        self._typing_keepalive = typing_keepalive
        self._sleep = sleep
        self._clock = clock
        self._on_message: InboundHandler | None = None
        self._on_interaction: InteractionHandler | None = None
        self._anchors: dict[ConversationRef, _ReplyAnchor] = {}
        self._streams: dict[MessageRef, _StreamState] = {}
        self._typing_tasks: dict[ConversationRef, asyncio.Task[None]] = {}
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._next_text_handle = 1
        self._closed = False

    async def start(
        self,
        on_message: InboundHandler,
        on_interaction: InteractionHandler,
    ) -> None:
        if self._on_message is not None:
            raise RuntimeError("QQ adapter is already started")
        if self._closed:
            raise RuntimeError("QQ adapter is closed")
        self._on_message = on_message
        self._on_interaction = on_interaction
        await self._gateway.start(self._handle_gateway_event)

    async def wait(self) -> None:
        if self._on_message is None:
            raise RuntimeError("QQ adapter has not been started")
        await self._gateway.wait()

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._gateway.stop()
            for conversation in tuple(self._typing_tasks):
                await self._stop_typing(conversation)
            for state in tuple(self._streams.values()):
                idle_task = state.idle_task
                if idle_task is not None and not idle_task.done():
                    idle_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await idle_task
            for message, state in tuple(self._streams.items()):
                async with state.lock:
                    if self._streams.get(message) is not state:
                        continue
                    await self._flush_stream_state(state)
                    self._streams.pop(message, None)
        finally:
            closers = [self._api.close()]
            if self._token_manager is not None:
                closers.append(self._token_manager.close())
            if self._owns_http:
                closers.append(self._http.aclose())
            await asyncio.gather(*closers)

    async def send_text(self, conversation: ConversationRef, text: str) -> MessageRef:
        self._validate_conversation(conversation)
        await self._flush_conversation_streams(conversation)
        openid = conversation.conversation_id
        anchor = self._anchors.get(conversation)
        reply_seq = anchor.reserve(now=self._clock()) if anchor is not None else None

        ref = MessageRef(conversation, f"text-{self._next_text_handle}")
        self._next_text_handle += 1
        state = _StreamState(
            conversation=conversation,
            openid=openid,
            anchor_msg_id=anchor.msg_id if anchor is not None else "",
            event_id=anchor.event_id if anchor is not None else None,
            msg_seq=reply_seq or 0,
            streamable=anchor is not None and reply_seq is not None,
            last_source_text=text,
        )
        self._streams[ref] = state
        rendered = _sanitize_stable_markdown(text)
        if state.streamable and rendered:
            try:
                await self._send_stream_rendered(state, rendered)
            except BaseException:
                self._streams.pop(ref, None)
                raise
        self._schedule_idle_finalize(ref)
        return ref

    async def send_final_text(
        self, conversation: ConversationRef, text: str
    ) -> MessageRef:
        self._validate_conversation(conversation)
        await self._stop_typing(conversation)
        openid = conversation.conversation_id
        sanitized = sanitize_markdown(text)
        anchor = self._anchors.get(conversation)
        reply_seq = anchor.reserve(now=self._clock()) if anchor is not None else None
        result, _sent_text = await self._with_defang_retry(
            lambda content: self._api.send_c2c_message(
                openid,
                msg_type=MSG_TYPE_MARKDOWN,
                markdown={"content": content},
                msg_id=anchor.msg_id
                if anchor is not None and reply_seq is not None
                else None,
                event_id=anchor.event_id
                if anchor is not None and reply_seq is not None
                else None,
                msg_seq=reply_seq,
            ),
            sanitized,
        )
        message_id = _require_response_id(result, "messages")
        return MessageRef(conversation, message_id)

    async def edit_text(self, message: MessageRef, text: str) -> None:
        self._validate_conversation(message.conversation)
        state = self._streams.get(message)
        if state is None:
            LOGGER.debug("QQ edit_text ignored for an unknown message")
            return
        async with state.lock:
            if self._streams.get(message) is not state:
                LOGGER.debug("QQ edit_text ignored for a completed message")
                return
            if state.pending_text is None and text == state.last_source_text:
                return
            if state.pending_text is not None:
                state.pending_text = text
                self._schedule_idle_finalize(message)
                return
            if state.finalized:
                state.pending_text = text
                self._schedule_idle_finalize(message)
                return
            rendered = _sanitize_stable_markdown(text)
            if not rendered.startswith(state.last_rendered_text):
                state.pending_text = text
                self._schedule_idle_finalize(message)
                return
            if state.streamable and rendered != state.last_rendered_text:
                await self._send_stream_rendered(state, rendered)
            state.last_source_text = text
            self._schedule_idle_finalize(message)

    async def send_file(
        self, conversation: ConversationRef, file: OutboundFile
    ) -> MessageRef:
        self._validate_conversation(conversation)
        await self._stop_typing(conversation)
        if file.media_type in _QQ_IMAGE_MEDIA_TYPES:
            file_type = QQ_FILE_TYPE_IMAGE
        elif file.media_type == _QQ_VIDEO_MEDIA_TYPE:
            file_type = QQ_FILE_TYPE_VIDEO
        else:
            file_type = QQ_FILE_TYPE_FILE

        openid = conversation.conversation_id
        upload = await self._api.upload_c2c_media(
            openid,
            file_type=file_type,
            file_data=base64.b64encode(file.data).decode("ascii"),
            file_name=file.name,
            srv_send_msg=False,
        )
        file_info = upload.get("file_info")
        if not isinstance(file_info, str) or not file_info:
            raise QQProtocolError("QQ media upload response omitted file_info")

        anchor = self._anchors.get(conversation)
        reply_seq = anchor.reserve(now=self._clock()) if anchor is not None else None
        if anchor is not None and reply_seq is not None:
            result = await self._api.send_c2c_message(
                openid,
                msg_type=MSG_TYPE_MEDIA,
                media={"file_info": file_info},
                msg_id=anchor.msg_id,
                event_id=anchor.event_id,
                msg_seq=reply_seq,
            )
        else:
            result = await self._api.send_c2c_message(
                openid, msg_type=MSG_TYPE_MEDIA, media={"file_info": file_info}
            )
        message_id = _require_response_id(result, "messages")
        return MessageRef(conversation, message_id)

    async def present_interaction(
        self, conversation: ConversationRef, prompt: InteractionPrompt
    ) -> MessageRef:
        self._validate_conversation(conversation)
        return await self.send_final_text(
            conversation,
            "Interactive prompt not supported on QQ; it will expire — steer "
            "with a normal message.",
        )

    async def finish_interaction(
        self, message: MessageRef, outcome: InteractionOutcome
    ) -> None:
        return None

    def _validate_conversation(self, conversation: ConversationRef) -> None:
        if conversation.platform != "qq":
            raise ValueError(
                f"unexpected platform for QQ adapter: {conversation.platform}"
            )

    async def _with_defang_retry(
        self,
        send: Callable[[str], Awaitable[dict[str, Any]]],
        content_raw: str,
        *,
        preserved_prefix: str = "",
    ) -> tuple[dict[str, Any], str]:
        if not content_raw.startswith(preserved_prefix):
            raise ValueError("QQ retry prefix is not present in rendered text")
        try:
            return await send(content_raw), content_raw
        except QQAPIError as error:
            if error.code != QQ_URL_DEFANG_ERROR_CODE:
                raise
            LOGGER.warning("QQ rejected an unregistered URL; retrying defanged")
            defanged = preserved_prefix + defang_urls(
                content_raw[len(preserved_prefix) :]
            )
            return await send(defanged), defanged

    def _schedule_idle_finalize(self, message: MessageRef) -> None:
        state = self._streams.get(message)
        if state is None:
            return
        if state.idle_task is not None and not state.idle_task.done():
            state.idle_task.cancel()
        state.idle_task = asyncio.create_task(
            self._finalize_after_idle(message), name="qq-stream-idle-finalize"
        )

    async def _finalize_after_idle(self, message: MessageRef) -> None:
        retry = False
        try:
            await self._sleep(self._idle_timeout)
            state = self._streams.get(message)
            if state is None:
                return
            async with state.lock:
                if self._streams.get(message) is not state:
                    return
                await self._flush_stream_state(state)
        except asyncio.CancelledError:
            raise
        except QQTransportError:
            retry = True
            LOGGER.error("QQ stream idle flush transport failed; retrying")
        except QQAPIError as error:
            LOGGER.error("QQ stream idle flush failed (code %d)", error.code)
        except Exception:
            LOGGER.exception("QQ stream idle flush failed")
        finally:
            state = self._streams.get(message)
            if state is not None and state.idle_task is asyncio.current_task():
                state.idle_task = None
            if retry and state is not None and not self._closed:
                self._schedule_idle_finalize(message)

    async def _flush_conversation_streams(
        self, conversation: ConversationRef
    ) -> None:
        states = [
            (message, state)
            for message, state in self._streams.items()
            if message.conversation == conversation
        ]
        for message, state in states:
            idle_task = state.idle_task
            if idle_task is not None and not idle_task.done():
                idle_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await idle_task
            async with state.lock:
                if self._streams.get(message) is not state:
                    continue
                await self._flush_stream_state(state)
                self._streams.pop(message, None)

    async def _flush_stream_state(self, state: _StreamState) -> None:
        if state.pending_text is not None:
            await self._replace_with_final(state, state.pending_text)
            return
        if state.finalized:
            return
        if not state.streamable:
            await self._send_active_final(state, state.last_source_text)
            return

        rendered = _sanitize_stable_markdown(
            state.last_source_text, final=True
        )
        if not rendered.startswith(state.last_rendered_text):
            await self._replace_with_final(state, state.last_source_text)
            return
        if rendered != state.last_rendered_text:
            await self._send_stream_rendered(state, rendered)
        if state.stream_msg_id is None:
            await self._send_active_final(
                state, state.last_source_text, use_reserved_reply=True
            )
            return
        await self._finish_stream(state)

    async def _send_stream_rendered(
        self, state: _StreamState, rendered: str
    ) -> None:
        if not rendered.startswith(state.last_rendered_text):
            raise QQProtocolError("QQ rendered stream prefix changed")
        continuation = (
            state.last_text
            + rendered[len(state.last_rendered_text) :]
        )
        await self._stop_typing(state.conversation)
        result, sent_text = await self._with_defang_retry(
            lambda content: self._api.send_c2c_stream_message(
                state.openid,
                input_state=STREAM_INPUT_STATE_GENERATING,
                content_raw=content,
                msg_id=state.anchor_msg_id,
                msg_seq=state.msg_seq,
                index=state.next_index,
                event_id=state.event_id,
                stream_msg_id=state.stream_msg_id,
            ),
            continuation,
            preserved_prefix=state.last_text,
        )
        message_id = _require_response_id(result, "stream_messages")
        if state.stream_msg_id is None:
            state.stream_msg_id = message_id
            state.delivered_message_id = message_id
        elif message_id != state.stream_msg_id:
            raise QQProtocolError("QQ stream message response changed id")
        state.next_index += 1
        state.last_text = sent_text
        state.last_rendered_text = rendered

    async def _send_active_final(
        self,
        state: _StreamState,
        source: str,
        *,
        use_reserved_reply: bool = False,
    ) -> None:
        await self._stop_typing(state.conversation)
        sanitized = sanitize_markdown(source)
        result, sent_text = await self._with_defang_retry(
            lambda content: self._api.send_c2c_message(
                state.openid,
                msg_type=MSG_TYPE_MARKDOWN,
                markdown={"content": content},
                msg_id=state.anchor_msg_id if use_reserved_reply else None,
                event_id=state.event_id if use_reserved_reply else None,
                msg_seq=state.msg_seq if use_reserved_reply else None,
            ),
            sanitized,
        )
        state.streamable = False
        state.stream_msg_id = None
        state.delivered_message_id = _require_response_id(result, "messages")
        state.last_text = sent_text
        state.last_rendered_text = sanitized
        state.last_source_text = source
        state.pending_text = None
        state.finalized = True

    async def _replace_with_final(
        self, state: _StreamState, source: str
    ) -> None:
        withdrawn = state.delivered_message_id is None
        if state.delivered_message_id is not None:
            try:
                await self._api.delete_c2c_message(
                    state.openid, state.delivered_message_id
                )
                withdrawn = True
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning(
                    "QQ could not withdraw superseded partial response; "
                    "sending the corrected final response",
                    exc_info=True,
                )
        if (
            not withdrawn
            and state.stream_msg_id is not None
            and not state.finalized
        ):
            try:
                await self._finish_stream(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning(
                    "QQ could not finish retained partial response",
                    exc_info=True,
                )
        await self._send_active_final(
            state,
            source,
            use_reserved_reply=(
                state.streamable and state.delivered_message_id is None
            ),
        )

    async def _finish_stream(self, state: _StreamState) -> None:
        if state.stream_msg_id is None:
            return
        await self._with_defang_retry(
            lambda content: self._api.send_c2c_stream_message(
                state.openid,
                input_state=STREAM_INPUT_STATE_DONE,
                content_raw=content,
                msg_id=state.anchor_msg_id,
                msg_seq=state.msg_seq,
                index=state.next_index,
                event_id=state.event_id,
                stream_msg_id=state.stream_msg_id,
            ),
            state.last_text + _QQ_STREAM_DONE_SUFFIX,
            preserved_prefix=state.last_text,
        )
        state.next_index += 1
        state.finalized = True

    async def _handle_gateway_event(self, event: QQGatewayEvent) -> None:
        if event.type != "C2C_MESSAGE_CREATE":
            return
        data = event.data
        if not isinstance(data, dict):
            return
        await self._handle_c2c_message(data, event.event_id)

    async def _handle_c2c_message(
        self, data: dict[str, Any], event_id: str | None
    ) -> None:
        msg_id = data.get("id")
        if not isinstance(msg_id, str) or not msg_id:
            LOGGER.warning("QQ C2C message omitted id; dropping")
            return
        dedupe_key = _dedupe_key(data, msg_id)
        if dedupe_key in self._seen_ids:
            return
        self._remember_dedupe_key(dedupe_key)

        author = data.get("author")
        openid = author.get("user_openid") if isinstance(author, dict) else None
        if not isinstance(openid, str) or not openid:
            LOGGER.warning("QQ C2C message omitted author.user_openid; dropping")
            return
        if openid not in self._allowed_users:
            LOGGER.warning(
                "unauthorized sender openid: %s — add it to [qq].allowed_users",
                openid,
            )
            return

        content = data.get("content")
        text = content if isinstance(content, str) else ""
        timestamp = _parse_qq_timestamp(data.get("timestamp"))
        conversation = ConversationRef(
            platform="qq", bot_id=self._app_id, conversation_id=openid
        )
        images, videos, files, audios = await self._collect_attachments(
            data.get("attachments")
        )

        self._anchors[conversation] = _ReplyAnchor(
            msg_id=msg_id, event_id=event_id, received_at=self._clock()
        )
        await self._start_typing(conversation, msg_id)

        message = InboundMessage(
            conversation=conversation,
            actor=ActorRef(id=openid),
            message_id=msg_id,
            text=text,
            timestamp=timestamp,
            images=images,
            videos=videos,
            files=files,
            audios=audios,
        )
        assert self._on_message is not None
        await self._on_message(self, message)

    async def _collect_attachments(
        self, raw: object
    ) -> tuple[
        tuple[InboundImage, ...],
        tuple[InboundVideo, ...],
        tuple[InboundFile, ...],
        tuple[InboundAudio, ...],
    ]:
        if not isinstance(raw, list):
            return (), (), (), ()
        images: list[InboundImage] = []
        videos: list[InboundVideo] = []
        files: list[InboundFile] = []
        audios: list[InboundAudio] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            content_type = item.get("content_type")
            media_type = (
                content_type
                if isinstance(content_type, str) and content_type
                else "application/octet-stream"
            )
            is_voice = media_type == "voice" or media_type.startswith("audio/")
            download_url = url
            if is_voice:
                # QQ pre-converts SILK voice to WAV, which suits ASR better
                # than the original encoding.
                wav_url = item.get("voice_wav_url")
                if isinstance(wav_url, str) and wav_url:
                    download_url = wav_url
                    media_type = "audio/wav"
            parsed_url = urlsplit(download_url)
            if parsed_url.scheme != "https" or parsed_url.hostname is None:
                LOGGER.warning("QQ attachment rejected for non-HTTPS URL")
                continue
            name = item.get("filename")
            name = name if isinstance(name, str) and name else "attachment"
            try:
                data = await self._download_attachment(download_url)
            except (httpx.HTTPError, QQAttachmentTooLarge) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, QQAttachmentTooLarge)
                    else type(exc).__name__
                )
                LOGGER.warning(
                    "QQ attachment %r download failed (%s)",
                    name,
                    reason,
                )
                continue
            if is_voice:
                asr_refer_text = item.get("asr_refer_text")
                audios.append(
                    InboundAudio(
                        data=data,
                        media_type=media_type,
                        name=name,
                        transcript=(
                            asr_refer_text
                            if isinstance(asr_refer_text, str) and asr_refer_text
                            else None
                        ),
                    )
                )
            elif media_type.startswith("image/"):
                images.append(
                    InboundImage(data=data, media_type=media_type, name=name)
                )
            elif media_type.startswith("video/"):
                videos.append(
                    InboundVideo(data=data, media_type=media_type, name=name)
                )
            else:
                files.append(InboundFile(data=data, name=name, media_type=media_type))
        return tuple(images), tuple(videos), tuple(files), tuple(audios)

    async def _download_attachment(self, url: str) -> bytes:
        chunks: list[bytes] = []
        received = 0
        async with self._http.stream(
            "GET",
            url,
            timeout=30.0,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                received += len(chunk)
                if received > QQ_ATTACHMENT_LIMIT_BYTES:
                    raise QQAttachmentTooLarge(QQ_ATTACHMENT_LIMIT_BYTES)
                chunks.append(chunk)
        return b"".join(chunks)

    def _remember_dedupe_key(self, key: str) -> None:
        self._seen_ids.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > _QQ_DEDUPE_MEMORY:
            oldest = self._seen_order.popleft()
            self._seen_ids.discard(oldest)

    async def _stop_typing(self, conversation: ConversationRef) -> None:
        task = self._typing_tasks.pop(conversation, None)
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _start_typing(
        self, conversation: ConversationRef, msg_id: str
    ) -> None:
        await self._stop_typing(conversation)
        self._typing_tasks[conversation] = asyncio.create_task(
            self._typing_loop(conversation, msg_id), name="qq-typing-indicator"
        )

    async def _typing_loop(self, conversation: ConversationRef, msg_id: str) -> None:
        try:
            while True:
                with contextlib.suppress(QQAPIError, QQTransportError):
                    await self._api.send_c2c_typing(
                        conversation.conversation_id,
                        msg_id=msg_id,
                        input_second=QQ_TYPING_INPUT_SECONDS,
                    )
                await self._sleep(self._typing_keepalive)
        except asyncio.CancelledError:
            raise
