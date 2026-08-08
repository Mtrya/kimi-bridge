"""Private WeChat iLink control and runtime adapter."""

from .adapter import WeChatAdapter, WeChatRuntimeAPI
from .api import WeChatAPI
from .auth import authorize_with_qr, run_login, run_logout, run_status
from .storage import WeChatRuntimeState, WeChatStorage
from .types import (
    LoginResult,
    WeChatAPIError,
    WeChatAPIResult,
    WeChatControlError,
    WeChatCredential,
    WeChatInboundEvent,
    WeChatMessageItem,
    WeChatPollResult,
    WeChatProtocolError,
    WeChatStorageError,
    WeChatUnsupportedOperation,
)


__all__ = [
    "LoginResult",
    "WeChatAPI",
    "WeChatAPIError",
    "WeChatAPIResult",
    "WeChatAdapter",
    "WeChatControlError",
    "WeChatCredential",
    "WeChatInboundEvent",
    "WeChatMessageItem",
    "WeChatPollResult",
    "WeChatProtocolError",
    "WeChatRuntimeAPI",
    "WeChatRuntimeState",
    "WeChatStorage",
    "WeChatStorageError",
    "WeChatUnsupportedOperation",
    "authorize_with_qr",
    "run_login",
    "run_logout",
    "run_status",
]
