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
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_ILINK_BOT_TYPE = "3"
DEFAULT_LONG_POLL_TIMEOUT_SECONDS = 35.0
DEFAULT_SEND_TIMEOUT_SECONDS = 15.0
DEFAULT_NOTIFY_TIMEOUT_SECONDS = 10.0
TYPING_TICKET_TTL_SECONDS = 24 * 60 * 60.0
TYPING_REFRESH_SECONDS = 5.0
MIN_LONG_POLL_TIMEOUT_SECONDS = 1.0
MAX_LONG_POLL_TIMEOUT_SECONDS = 60.0
STALE_TOKEN_ERROR_CODE = -14
TYPING_STATUS_ACTIVE = 1
TYPING_STATUS_CANCEL = 2

MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
MESSAGE_ITEM_TYPE_TEXT = 1
MESSAGE_ITEM_TYPE_IMAGE = 2
MESSAGE_ITEM_TYPE_VOICE = 3
MESSAGE_ITEM_TYPE_FILE = 4
MESSAGE_ITEM_TYPE_VIDEO = 5
UPLOAD_MEDIA_TYPE_IMAGE = 1
UPLOAD_MEDIA_TYPE_VIDEO = 2
UPLOAD_MEDIA_TYPE_FILE = 3
UPLOAD_MEDIA_TYPE_VOICE = 4
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
    text: str | None = field(default=None, repr=False)
    image: WeChatImageItem | None = None
    voice: WeChatVoiceItem | None = None
    file: WeChatFileItem | None = None
    video: WeChatVideoItem | None = None


@dataclass(frozen=True, slots=True)
class WeChatCDNMedia:
    """One secret-bearing CDN reference from an inbound item."""

    encrypt_query_param: str | None = field(default=None, repr=False)
    aes_key: str | None = field(default=None, repr=False)
    encrypt_type: int | None = None
    full_url: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class WeChatImageItem:
    media: WeChatCDNMedia | None = None
    aes_key_hex: str | None = field(default=None, repr=False)
    mid_size: int | None = None


@dataclass(frozen=True, slots=True)
class WeChatVoiceItem:
    media: WeChatCDNMedia | None = None
    encode_type: int | None = None
    sample_rate: int | None = None
    playtime: int | None = None
    text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class WeChatFileItem:
    media: WeChatCDNMedia | None = None
    file_name: str | None = None
    md5: str | None = field(default=None, repr=False)
    length: str | None = None


@dataclass(frozen=True, slots=True)
class WeChatVideoItem:
    media: WeChatCDNMedia | None = None
    video_size: int | None = None
    video_md5: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class WeChatUploadRequest:
    """Metadata required by ``getuploadurl`` before encrypted upload."""

    file_key: str = field(repr=False)
    media_type: int
    to_user_id: str
    raw_size: int
    raw_file_md5: str = field(repr=False)
    ciphertext_size: int
    aes_key_hex: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class WeChatUploadTarget:
    """Secret-bearing upload target returned by iLink."""

    upload_full_url: str | None = field(default=None, repr=False)
    upload_param: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class WeChatUploadedMedia:
    """Validated result of one encrypted CDN upload."""

    download_query_param: str = field(repr=False)
    aes_key_hex: str = field(repr=False)
    plaintext_size: int
    ciphertext_size: int


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


@dataclass(frozen=True, slots=True)
class WeChatTypingConfig:
    """Adapter-private projection of the typing ticket response."""

    typing_ticket: str | None = field(default=None, repr=False)


class WeChatControlError(RuntimeError):
    """Expected, secret-safe WeChat control-plane failure."""


class WeChatAPIError(WeChatControlError):
    """Malformed or failed iLink authentication request."""


class WeChatStorageError(WeChatControlError):
    """Invalid or unsafe adapter-owned local state."""


class WeChatProtocolError(RuntimeError):
    """Malformed or failed runtime iLink protocol operation."""


class WeChatRetryableError(RuntimeError):
    """Redacted transient failure; callers decide whether repetition is safe."""

    def __init__(
        self,
        endpoint: str,
        category: str,
        *,
        status_code: int | None = None,
    ) -> None:
        detail = f"{endpoint} {category} failure"
        if status_code is not None:
            detail += f" (HTTP {status_code})"
        super().__init__(detail)
        self.endpoint = endpoint
        self.category = category
        self.status_code = status_code


class WeChatAuthenticationExpired(WeChatProtocolError):
    """The stored iLink bot authorization is stale and must be replaced."""

    def __init__(self, endpoint: str) -> None:
        super().__init__(
            f"{endpoint} reported expired WeChat authorization; run "
            "kimi-bridge wechat login --replace"
        )


class WeChatUnsupportedOperation(RuntimeError):
    """Operation intentionally unsupported by the current WeChat adapter."""
