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
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import websockets
from websockets.exceptions import ConnectionClosed


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


class QQError(RuntimeError):
    """Base exception for the QQ boundary."""


class QQProtocolError(QQError):
    """QQ returned a shape that violates the documented contract."""


class QQTransportError(QQError):
    """A QQ HTTP request failed after transient retries."""


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
    """Retry transport failures, 429, and 5xx; return the final response."""

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
        if retryable and attempt < max_retries:
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

    async def upload_c2c_media(
        self,
        openid: str,
        *,
        file_type: int,
        url: str | None = None,
        file_data: str | None = None,
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

    async def _run_connection(self, ws: QQGatewaySocket) -> None:
        heartbeat_interval = await self._expect_hello(ws)
        if self._session_id is not None and self._last_seq is not None:
            await self._send_resume(ws)
        else:
            await self._send_identify(ws)
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(ws, heartbeat_interval),
            name="qq-gateway-heartbeat",
        )
        try:
            while True:
                frame = _decode_frame(await ws.recv())
                op = frame.get("op")
                if op == OP_DISPATCH:
                    await self._handle_dispatch(frame)
                elif op == OP_HEARTBEAT:
                    await self._send_heartbeat(ws)
                elif op == OP_HEARTBEAT_ACK:
                    continue
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
        await self._on_event(
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
        self, ws: QQGatewaySocket, interval: float
    ) -> None:
        try:
            while True:
                await self._sleep(interval)
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
