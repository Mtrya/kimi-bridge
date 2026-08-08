"""Private values for the pinned WeChat iLink authentication contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


PINNED_SOURCE_TAG = "v2.4.6"
PINNED_SOURCE_COMMIT = "cef0bfc390393f716903e16d50408118047f87e0"
CHANNEL_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (4 << 8) | 6
DEFAULT_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_ILINK_BOT_TYPE = "3"

QRStatusName: TypeAlias = Literal[
    "wait",
    "scaned",
    "confirmed",
    "expired",
    "need_verifycode",
    "verify_code_blocked",
    "scaned_but_redirect",
    "binded_redirect",
]


@dataclass(frozen=True, slots=True)
class QRCode:
    """One short-lived QR authorization challenge."""

    token: str = field(repr=False)
    authorization_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class QRStatus:
    """Typed projection of one QR status response."""

    status: QRStatusName
    bot_token: str | None = field(default=None, repr=False)
    bot_id: str | None = None
    base_url: str | None = None
    scanner_user_id: str | None = None
    redirect_host: str | None = None


@dataclass(frozen=True, slots=True)
class WeChatCredential:
    """One locally stored iLink bot authorization."""

    bot_token: str = field(repr=False)
    bot_id: str
    base_url: str
    authorized_at: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Successful local outcome of the QR flow."""

    credential: WeChatCredential
    scanner_user_id: str | None
    reused_existing: bool = False


class WeChatControlError(RuntimeError):
    """Expected, secret-safe WeChat control-plane failure."""


class WeChatAPIError(WeChatControlError):
    """Malformed or failed iLink authentication request."""


class WeChatStorageError(WeChatControlError):
    """Invalid or unsafe adapter-owned local state."""
