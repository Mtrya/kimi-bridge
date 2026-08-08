"""Headless WeChat QR login and local authorization controls."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TextIO
from urllib.parse import urlsplit

import httpx

from ...config import WechatConfig
from .api import (
    WeChatAuthAPI,
    normalize_https_base_url,
    redirect_base_url,
)
from .storage import WeChatStorage, redact_bot_id
from .types import (
    DEFAULT_ILINK_BASE_URL,
    LoginResult,
    QRCode,
    QRStatus,
    WeChatControlError,
    WeChatCredential,
)


DEFAULT_LOGIN_TIMEOUT_SECONDS = 480.0
MAX_QR_ATTEMPTS = 3
POLL_DELAY_SECONDS = 1.0

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
VerifyCodeReader = Callable[[str], str]
AuthorizedAtFactory = Callable[[], str]


async def authorize_with_qr(
    storage: WeChatStorage,
    api: WeChatAuthAPI,
    *,
    replace: bool = False,
    stream: TextIO = sys.stdout,
    read_verify_code: VerifyCodeReader = input,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    authorized_at: AuthorizedAtFactory = lambda: datetime.now(
        timezone.utc
    ).isoformat(),
    timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    max_qr_attempts: int = MAX_QR_ATTEMPTS,
) -> LoginResult:
    """Complete one QR flow and atomically persist only a confirmed result."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_qr_attempts <= 0:
        raise ValueError("max_qr_attempts must be positive")

    existing: WeChatCredential | None = None
    if storage.has_credential():
        existing = storage.load_credential()
        if not replace:
            raise WeChatControlError(
                "WeChat authorization already exists; use login --replace"
            )

    local_tokens = (existing.bot_token,) if existing is not None else ()
    qr_code = await api.fetch_qr_code(local_tokens=local_tokens)
    _show_qr_url(stream, qr_code)
    attempts = 1
    current_base_url = DEFAULT_ILINK_BASE_URL
    pending_verify_code: str | None = None
    scanned_reported = False
    deadline = clock() + timeout_seconds

    while clock() < deadline:
        try:
            status = await api.get_qr_status(
                qr_code.token,
                base_url=current_base_url,
                verify_code=pending_verify_code,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            await sleep(POLL_DELAY_SECONDS)
            continue

        if status.status == "wait":
            await sleep(POLL_DELAY_SECONDS)
            continue

        if status.status == "scaned":
            pending_verify_code = None
            if not scanned_reported:
                stream.write("QR code scanned; waiting for confirmation.\n")
                stream.flush()
                scanned_reported = True
            await sleep(POLL_DELAY_SECONDS)
            continue

        if status.status == "need_verifycode":
            prompt = (
                "Verification code was not accepted; enter the number again: "
                if pending_verify_code
                else "Enter the number shown in WeChat: "
            )
            code = read_verify_code(prompt).strip()
            if not code:
                raise WeChatControlError("WeChat verification code cannot be empty")
            pending_verify_code = code
            continue

        if status.status in {"expired", "verify_code_blocked"}:
            pending_verify_code = None
            if attempts >= max_qr_attempts:
                raise WeChatControlError(
                    "WeChat QR authorization stopped after repeated refreshes"
                )
            attempts += 1
            qr_code = await api.fetch_qr_code(local_tokens=local_tokens)
            scanned_reported = False
            _show_qr_url(stream, qr_code, refreshed=True)
            continue

        if status.status == "scaned_but_redirect":
            if status.redirect_host is None:
                raise WeChatControlError(
                    "WeChat QR redirect response did not include a host"
                )
            current_base_url = redirect_base_url(status.redirect_host)
            await sleep(POLL_DELAY_SECONDS)
            continue

        if status.status == "binded_redirect":
            if existing is None:
                raise WeChatControlError(
                    "WeChat reports an existing binding, but no local authorization "
                    "can be retained; run login again after resolving the binding"
                )
            return LoginResult(
                credential=existing,
                scanner_user_id=None,
                reused_existing=True,
            )

        if status.status == "confirmed":
            result = _confirmed_result(status, authorized_at=authorized_at)
            storage.save_credential(result.credential)
            return result

        raise AssertionError(f"unhandled QR status: {status.status}")

    raise WeChatControlError("WeChat QR authorization timed out")


def run_login(
    config: WechatConfig,
    *,
    replace: bool = False,
    stream: TextIO = sys.stdout,
) -> int:
    """Run the networked QR flow without starting Kimi Code or message polling."""

    async def login() -> LoginResult:
        async with WeChatAuthAPI() as api:
            return await authorize_with_qr(
                WeChatStorage(config.storage_path),
                api,
                replace=replace,
                stream=stream,
            )

    try:
        result = asyncio.run(login())
    except httpx.TransportError as exc:
        raise WeChatControlError("WeChat QR authorization request failed") from exc
    if result.reused_existing:
        stream.write("Existing local WeChat authorization was retained.\n")
        stream.write(f"Bot identity: {result.credential.bot_id}\n")
    else:
        stream.write("WeChat authorization saved locally.\n")
        stream.write(f"Bot identity: {result.credential.bot_id}\n")
        assert result.scanner_user_id is not None
        stream.write(f"Scanner user identity: {result.scanner_user_id}\n")
        stream.write("Add the scanner identity to wechat.allowed_users.\n")
    stream.flush()
    return 0


def run_status(
    config: WechatConfig,
    *,
    stream: TextIO = sys.stdout,
    platform_name: str = sys.platform,
) -> int:
    """Render local authorization presence and hygiene without network access."""

    storage = WeChatStorage(config.storage_path)
    inspection = storage.inspect(platform_name=platform_name)
    stream.write(f"WeChat storage: {storage.path}\n")
    if inspection.directory_error:
        stream.write(f"Storage error: {inspection.directory_error}.\n")
    elif inspection.directory_exists:
        stream.write("Storage directory: locally usable.\n")
    else:
        stream.write("Storage directory: not created.\n")

    if inspection.credential_error:
        stream.write(f"Authorization error: {inspection.credential_error}.\n")
    elif inspection.credential is None:
        stream.write("Authorization: not authorized locally.\n")
    else:
        credential = inspection.credential
        stream.write("Authorization: present locally; network status was not checked.\n")
        stream.write(f"Bot identity: {redact_bot_id(credential.bot_id)}\n")
        stream.write(f"Base host: {urlsplit(credential.base_url).hostname}\n")
        stream.write(f"Authorized at: {credential.authorized_at}\n")
    stream.flush()
    return int(
        inspection.directory_error is not None
        or inspection.credential_error is not None
        or inspection.credential is None
    )


def run_logout(
    config: WechatConfig,
    *,
    stream: TextIO = sys.stdout,
) -> int:
    """Remove only files whose names are owned by the WeChat adapter."""

    removed = WeChatStorage(config.storage_path).clear_owned_files()
    if removed:
        stream.write("Removed WeChat adapter files: " + ", ".join(removed) + ".\n")
    else:
        stream.write("No WeChat adapter files were present.\n")
    stream.flush()
    return 0


def _show_qr_url(
    stream: TextIO, qr_code: QRCode, *, refreshed: bool = False
) -> None:
    prefix = "Refreshed WeChat authorization URL" if refreshed else "WeChat authorization URL"
    stream.write(f"{prefix}:\n{qr_code.authorization_url}\n")
    stream.flush()


def _confirmed_result(
    status: QRStatus, *, authorized_at: AuthorizedAtFactory
) -> LoginResult:
    if not status.bot_token or not status.bot_id or not status.base_url:
        raise WeChatControlError(
            "WeChat confirmed authorization without complete bot credentials"
        )
    if not status.scanner_user_id:
        raise WeChatControlError(
            "WeChat confirmed authorization without a stable scanner identity"
        )
    base_url = normalize_https_base_url(
        status.base_url, field="confirmed WeChat base URL"
    )
    credential = WeChatCredential(
        bot_token=status.bot_token,
        bot_id=status.bot_id,
        base_url=base_url,
        authorized_at=authorized_at(),
    )
    return LoginResult(
        credential=credential,
        scanner_user_id=status.scanner_user_id,
    )
