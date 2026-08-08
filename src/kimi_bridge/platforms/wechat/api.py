"""Handwritten HTTP boundary for WeChat iLink QR authorization."""

from __future__ import annotations

import base64
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
    ILINK_APP_CLIENT_VERSION,
    ILINK_APP_ID,
    QRCode,
    QRStatus,
    QRStatusName,
    WeChatAPIError,
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


def _post_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
        **_common_headers(),
    }


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
