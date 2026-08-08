"""Private values for the pinned WeChat iLink contract."""

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
DEFAULT_LONG_POLL_TIMEOUT_SECONDS = 35.0
DEFAULT_SEND_TIMEOUT_SECONDS = 15.0
DEFAULT_NOTIFY_TIMEOUT_SECONDS = 10.0
MIN_LONG_POLL_TIMEOUT_SECONDS = 1.0
MAX_LONG_POLL_TIMEOUT_SECONDS = 60.0

MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
MESSAGE_ITEM_TYPE_TEXT = 1
MESSAGE_ITEM_TYPE_IMAGE = 2
MESSAGE_ITEM_TYPE_VOICE = 3
MESSAGE_ITEM_TYPE_FILE = 4
MESSAGE_ITEM_TYPE_VIDEO = 5
MEDIA_ITEM_TYPES = frozenset(
    {
        MESSAGE_ITEM_TYPE_IMAGE,
        MESSAGE_ITEM_TYPE_VOICE,
        MESSAGE_ITEM_TYPE_FILE,
        MESSAGE_ITEM_TYPE_VIDEO,
    }
)

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


@dataclass(frozen=True, slots=True)
class WeChatMessageItem:
    """Typed subset of one inbound iLink message item."""

    type: int | None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class WeChatInboundEvent:
    """Typed inbound message fields used by the semantic adapter."""

    message_id: int | None
    from_user_id: str | None
    create_time_ms: float | None
    message_type: int | None
    group_id: str | None
    items: tuple[WeChatMessageItem, ...]
    context_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class WeChatPollResult:
    """One validated getupdates response."""

    messages: tuple[WeChatInboundEvent, ...]
    get_updates_buf: str = field(repr=False)
    long_poll_timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class WeChatAPIResult:
    """Non-secret status projection for lifecycle notification responses."""

    ret: int | None = None


class WeChatControlError(RuntimeError):
    """Expected, secret-safe WeChat control-plane failure."""


class WeChatAPIError(WeChatControlError):
    """Malformed or failed iLink authentication request."""


class WeChatStorageError(WeChatControlError):
    """Invalid or unsafe adapter-owned local state."""


class WeChatProtocolError(RuntimeError):
    """Malformed or failed runtime iLink protocol operation."""


class WeChatUnsupportedOperation(RuntimeError):
    """Operation intentionally unsupported by the current WeChat adapter."""
