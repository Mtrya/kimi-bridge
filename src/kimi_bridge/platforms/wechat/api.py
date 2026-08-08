"""Handwritten HTTP boundary for the pinned WeChat iLink contract."""

from __future__ import annotations

import base64
import math
import secrets
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from ... import __version__
from .types import (
    CHANNEL_VERSION,
    DEFAULT_ILINK_BASE_URL,
    DEFAULT_ILINK_BOT_TYPE,
    DEFAULT_NOTIFY_TIMEOUT_SECONDS,
    DEFAULT_SEND_TIMEOUT_SECONDS,
    ILINK_APP_CLIENT_VERSION,
    ILINK_APP_ID,
    MESSAGE_ITEM_TYPE_TEXT,
    MESSAGE_STATE_FINISH,
    MESSAGE_TYPE_BOT,
    QRCode,
    QRStatus,
    QRStatusName,
    WeChatAPIResult,
    WeChatAPIError,
    WeChatCredential,
    WeChatInboundEvent,
    WeChatMessageItem,
    WeChatPollResult,
    WeChatProtocolError,
)


_QR_STATUSES = frozenset(
    {
        "wait",
        "scaned",
        "confirmed",
        "expired",
        "need_verifycode",
        "verify_code_blocked",
        "scaned_but_redirect",
        "binded_redirect",
    }
)
_BOT_AGENT_VERSION_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.+-"
)


def build_base_info(version: str = __version__) -> dict[str, str]:
    """Build truthful metadata for later authenticated iLink requests."""

    normalized = version.strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > 32
        or any(character not in _BOT_AGENT_VERSION_CHARS for character in normalized)
    ):
        normalized = "unknown"
    return {
        "channel_version": CHANNEL_VERSION,
        "bot_agent": f"kimi-bridge/{normalized}",
    }


def normalize_https_base_url(value: str, *, field: str) -> str:
    """Validate and normalize an iLink base origin without leaking its value."""

    if not isinstance(value, str) or not value.strip():
        raise WeChatAPIError(f"{field} is missing or invalid")
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise WeChatAPIError(f"{field} is missing or invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise WeChatAPIError(f"{field} is missing or invalid")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit(("https", netloc, "", "", ""))


def redirect_base_url(host: str) -> str:
    """Convert the tagged protocol's redirect host into a validated HTTPS origin."""

    if not isinstance(host, str) or not host.strip() or "://" in host:
        raise WeChatAPIError("QR redirect host is missing or invalid")
    return normalize_https_base_url(f"https://{host.strip()}", field="QR redirect host")


def _random_wechat_uin() -> str:
    decimal = str(secrets.randbits(32)).encode("ascii")
    return base64.b64encode(decimal).decode("ascii")


def _common_headers() -> dict[str, str]:
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }


def _post_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
        **_common_headers(),
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class WeChatAuthAPI:
    """Small async client for the two QR authorization endpoints."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        request_timeout_seconds: float = 35.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._request_timeout_seconds = request_timeout_seconds

    async def __aenter__(self) -> WeChatAuthAPI:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_qr_code(
        self, *, local_tokens: Sequence[str] = ()
    ) -> QRCode:
        tokens = [token for token in local_tokens if token.strip()][-10:]
        response = await self._client.post(
            f"{DEFAULT_ILINK_BASE_URL}/ilink/bot/get_bot_qrcode",
            params={"bot_type": DEFAULT_ILINK_BOT_TYPE},
            headers=_post_headers(),
            json={"local_token_list": tokens},
            timeout=self._request_timeout_seconds,
            follow_redirects=False,
        )
        payload = _response_object(response, "QR creation")
        token = _required_string(payload, "qrcode", "QR creation")
        authorization_url = _required_string(
            payload, "qrcode_img_content", "QR creation"
        )
        return QRCode(token=token, authorization_url=authorization_url)

    async def get_qr_status(
        self,
        qr_token: str,
        *,
        base_url: str = DEFAULT_ILINK_BASE_URL,
        verify_code: str | None = None,
    ) -> QRStatus:
        origin = normalize_https_base_url(base_url, field="QR polling base URL")
        params = {"qrcode": qr_token}
        if verify_code:
            params["verify_code"] = verify_code
        response = await self._client.get(
            f"{origin}/ilink/bot/get_qrcode_status",
            params=params,
            headers=_common_headers(),
            timeout=self._request_timeout_seconds,
            follow_redirects=False,
        )
        payload = _response_object(response, "QR status")
        status = _required_string(payload, "status", "QR status")
        if status not in _QR_STATUSES:
            raise WeChatAPIError("QR status response has an unsupported status")
        return QRStatus(
            status=cast(QRStatusName, status),
            bot_token=_optional_string(payload, "bot_token", "QR status"),
            bot_id=_optional_string(payload, "ilink_bot_id", "QR status"),
            base_url=_optional_string(payload, "baseurl", "QR status"),
            scanner_user_id=_optional_string(
                payload, "ilink_user_id", "QR status"
            ),
            redirect_host=_optional_string(
                payload, "redirect_host", "QR status"
            ),
        )


class WeChatAPI:
    """Authenticated iLink runtime endpoints used by the WeChat adapter."""

    def __init__(
        self,
        credential: WeChatCredential,
        client: httpx.AsyncClient | None = None,
        *,
        send_timeout_seconds: float = DEFAULT_SEND_TIMEOUT_SECONDS,
        notify_timeout_seconds: float = DEFAULT_NOTIFY_TIMEOUT_SECONDS,
    ) -> None:
        if not credential.bot_token.strip() or not credential.bot_id.strip():
            raise ValueError("WeChat credential identity must be non-empty")
        if send_timeout_seconds <= 0 or notify_timeout_seconds <= 0:
            raise ValueError("WeChat runtime request timeouts must be positive")
        self._base_url = normalize_https_base_url(
            credential.base_url, field="WeChat runtime base URL"
        )
        self._bot_token = credential.bot_token.strip()
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._send_timeout_seconds = send_timeout_seconds
        self._notify_timeout_seconds = notify_timeout_seconds

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_updates(
        self, get_updates_buf: str, *, timeout_seconds: float
    ) -> WeChatPollResult:
        if not isinstance(get_updates_buf, str):
            raise ValueError("get_updates_buf must be a string")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            payload = await self._post(
                "ilink/bot/getupdates",
                {"get_updates_buf": get_updates_buf},
                timeout_seconds=timeout_seconds,
                endpoint_name="getUpdates",
            )
        except httpx.TimeoutException:
            return WeChatPollResult(
                messages=(), get_updates_buf=get_updates_buf
            )
        _raise_runtime_code(payload, "getUpdates", check_errcode=True)
        raw_messages = payload.get("msgs")
        if raw_messages is None:
            messages: tuple[WeChatInboundEvent, ...] = ()
        elif isinstance(raw_messages, list):
            messages = tuple(
                _parse_inbound_message(message) for message in raw_messages
            )
        else:
            raise WeChatProtocolError("getUpdates response has invalid msgs")
        cursor = _runtime_optional_string(
            payload, "get_updates_buf", "getUpdates", trim=False
        )
        timeout_ms = _runtime_optional_number(
            payload, "longpolling_timeout_ms", "getUpdates"
        )
        suggested_timeout = (
            timeout_ms / 1000
            if timeout_ms is not None and timeout_ms > 0
            else None
        )
        return WeChatPollResult(
            messages=messages,
            get_updates_buf=cursor or "",
            long_poll_timeout_seconds=suggested_timeout,
        )

    async def send_text(
        self,
        *,
        to_user_id: str,
        context_token: str,
        text: str,
        client_id: str,
    ) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (to_user_id, context_token, client_id)
        ):
            raise ValueError("WeChat send identity and context must be non-empty")
        if not isinstance(text, str):
            raise TypeError("WeChat message text must be a string")
        payload = await self._post(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id,
                    "message_type": MESSAGE_TYPE_BOT,
                    "message_state": MESSAGE_STATE_FINISH,
                    "item_list": [
                        {
                            "type": MESSAGE_ITEM_TYPE_TEXT,
                            "text_item": {"text": text},
                        }
                    ],
                    "context_token": context_token,
                }
            },
            timeout_seconds=self._send_timeout_seconds,
            endpoint_name="sendMessage",
        )
        _raise_runtime_code(payload, "sendMessage")

    async def notify_start(self) -> WeChatAPIResult:
        return await self._notify("notifystart", "notifyStart")

    async def notify_stop(self) -> WeChatAPIResult:
        return await self._notify("notifystop", "notifyStop")

    async def _notify(self, endpoint: str, name: str) -> WeChatAPIResult:
        payload = await self._post(
            f"ilink/bot/msg/{endpoint}",
            {},
            timeout_seconds=self._notify_timeout_seconds,
            endpoint_name=name,
        )
        return WeChatAPIResult(ret=_runtime_optional_int(payload, "ret", name))

    async def _post(
        self,
        endpoint: str,
        body: Mapping[str, Any],
        *,
        timeout_seconds: float,
        endpoint_name: str,
    ) -> Mapping[str, Any]:
        response = await self._client.post(
            f"{self._base_url}/{endpoint}",
            headers=_post_headers(self._bot_token),
            json={**body, "base_info": build_base_info()},
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        try:
            return _response_object(response, endpoint_name)
        except WeChatAPIError as exc:
            raise WeChatProtocolError(str(exc)) from exc


def _response_object(response: httpx.Response, endpoint: str) -> Mapping[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise WeChatAPIError(f"{endpoint} request failed with HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WeChatAPIError(f"{endpoint} response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WeChatAPIError(f"{endpoint} response must be an object")
    return payload


def _required_string(
    payload: Mapping[str, Any], key: str, endpoint: str
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WeChatAPIError(f"{endpoint} response is missing {key}")
    return value.strip()


def _optional_string(
    payload: Mapping[str, Any], key: str, endpoint: str
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WeChatAPIError(f"{endpoint} response has invalid {key}")
    return value.strip()


def _parse_inbound_message(value: object) -> WeChatInboundEvent:
    if not isinstance(value, dict):
        raise WeChatProtocolError("getUpdates response has an invalid message")
    raw_items = value.get("item_list")
    if raw_items is None:
        items: tuple[WeChatMessageItem, ...] = ()
    elif isinstance(raw_items, list):
        items = tuple(_parse_message_item(item) for item in raw_items)
    else:
        raise WeChatProtocolError(
            "getUpdates response has an invalid message item list"
        )
    return WeChatInboundEvent(
        message_id=_runtime_optional_int(value, "message_id", "getUpdates"),
        from_user_id=_runtime_optional_string(
            value, "from_user_id", "getUpdates"
        ),
        create_time_ms=_runtime_optional_number(
            value, "create_time_ms", "getUpdates"
        ),
        message_type=_runtime_optional_int(
            value, "message_type", "getUpdates"
        ),
        group_id=_runtime_optional_string(value, "group_id", "getUpdates"),
        items=items,
        context_token=_runtime_optional_string(
            value, "context_token", "getUpdates"
        ),
    )


def _parse_message_item(value: object) -> WeChatMessageItem:
    if not isinstance(value, dict):
        raise WeChatProtocolError(
            "getUpdates response has an invalid message item"
        )
    item_type = _runtime_optional_int(value, "type", "getUpdates")
    text: str | None = None
    if item_type == MESSAGE_ITEM_TYPE_TEXT:
        raw_text_item = value.get("text_item")
        if raw_text_item is not None:
            if not isinstance(raw_text_item, dict):
                raise WeChatProtocolError(
                    "getUpdates response has an invalid text item"
                )
            text = _runtime_optional_string(
                raw_text_item,
                "text",
                "getUpdates",
                trim=False,
            )
    return WeChatMessageItem(type=item_type, text=text)


def _raise_runtime_code(
    payload: Mapping[str, Any], endpoint: str, *, check_errcode: bool = False
) -> None:
    ret = _runtime_optional_int(payload, "ret", endpoint)
    errcode = (
        _runtime_optional_int(payload, "errcode", endpoint)
        if check_errcode
        else None
    )
    if ret not in {None, 0} or errcode not in {None, 0}:
        parts = []
        if ret not in {None, 0}:
            parts.append(f"ret={ret}")
        if errcode not in {None, 0}:
            parts.append(f"errcode={errcode}")
        raise WeChatProtocolError(
            f"{endpoint} response reported " + ", ".join(parts)
        )


def _runtime_optional_int(
    payload: Mapping[str, Any], key: str, endpoint: str
) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise WeChatProtocolError(f"{endpoint} response has invalid {key}")
    return value


def _runtime_optional_number(
    payload: Mapping[str, Any], key: str, endpoint: str
) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise WeChatProtocolError(f"{endpoint} response has invalid {key}")
    return float(value)


def _runtime_optional_string(
    payload: Mapping[str, Any],
    key: str,
    endpoint: str,
    *,
    trim: bool = True,
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WeChatProtocolError(f"{endpoint} response has invalid {key}")
    return value.strip() if trim else value
