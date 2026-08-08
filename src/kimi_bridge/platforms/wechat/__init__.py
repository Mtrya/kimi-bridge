"""Private WeChat iLink control and runtime adapter."""

from .adapter import WeChatAdapter, WeChatRuntimeAPI
from .api import WeChatAPI
from .auth import authorize_with_qr, run_login, run_logout, run_status
from .storage import WeChatRuntimeState, WeChatStorage
from .types import (
    LoginResult,
    WeChatAPIError,
    WeChatAPIResult,
    WeChatAuthenticationExpired,
    WeChatControlError,
    WeChatCredential,
    WeChatInboundEvent,
    WeChatMessageItem,
    WeChatPollResult,
    WeChatProtocolError,
    WeChatRetryableError,
    WeChatStorageError,
    WeChatTypingConfig,
    WeChatUnsupportedOperation,
)


__all__ = [
    "LoginResult",
    "WeChatAPI",
    "WeChatAPIError",
    "WeChatAPIResult",
    "WeChatAdapter",
    "WeChatAuthenticationExpired",
    "WeChatControlError",
    "WeChatCredential",
    "WeChatInboundEvent",
    "WeChatMessageItem",
    "WeChatPollResult",
    "WeChatProtocolError",
    "WeChatRetryableError",
    "WeChatRuntimeAPI",
    "WeChatRuntimeState",
    "WeChatStorage",
    "WeChatStorageError",
    "WeChatTypingConfig",
    "WeChatUnsupportedOperation",
    "authorize_with_qr",
    "run_login",
    "run_logout",
    "run_status",
]
