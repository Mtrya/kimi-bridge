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
from ..auth_formatting import write_qr_url
from .api import (
    WeChatAuthAPI,
    normalize_https_base_url,
    redirect_base_url,
)
from .storage import WeChatRuntimeState, WeChatStorage, redact_bot_id
from .types import (
    DEFAULT_ILINK_BASE_URL,
    LoginResult,
    QRCode,
    QRStatus,
    WeChatControlError,
    WeChatCredential,
    WeChatRetryableError,
)


DEFAULT_LOGIN_TIMEOUT_SECONDS = 480.0
MAX_QR_ATTEMPTS = 3
POLL_DELAY_SECONDS = 1.0
MAX_POLL_RETRY_DELAY_SECONDS = 8.0

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
    max_poll_retry_delay_seconds: float = MAX_POLL_RETRY_DELAY_SECONDS,
) -> LoginResult:
    """Complete one QR flow and atomically persist only a confirmed result."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_qr_attempts <= 0:
        raise ValueError("max_qr_attempts must be positive")
    if max_poll_retry_delay_seconds < POLL_DELAY_SECONDS:
        raise ValueError(
            "max_poll_retry_delay_seconds must be at least the poll delay"
        )

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
    retry_delay = POLL_DELAY_SECONDS

    while clock() < deadline:
        try:
            status = await api.get_qr_status(
                qr_code.token,
                base_url=current_base_url,
                verify_code=pending_verify_code,
            )
        except (
            WeChatRetryableError,
            httpx.TimeoutException,
            httpx.TransportError,
        ):
            await sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_poll_retry_delay_seconds)
            continue
        retry_delay = POLL_DELAY_SECONDS

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
            if pending_verify_code:
                stream.write("\nVerification code rejected\n\n")
            stream.write(
                "Verification required\n\nEnter the number shown in WeChat.\n\n"
            )
            stream.flush()
            code = read_verify_code("Verification code: ").strip()
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
            current_base_url = DEFAULT_ILINK_BASE_URL
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
            _persist_confirmed_credential(storage, existing, result.credential)
            return result

        raise AssertionError(f"unhandled QR status: {status.status}")

    raise WeChatControlError("WeChat QR authorization timed out")


def _persist_confirmed_credential(
    storage: WeChatStorage,
    existing: WeChatCredential | None,
    credential: WeChatCredential,
) -> None:
    reset_runtime_state = (
        existing is not None and existing.bot_id != credential.bot_id
    )
    if not reset_runtime_state:
        storage.save_credential(credential)
        return

    runtime_state_existed = storage.runtime_state_path.exists()
    previous_runtime_state = storage.load_runtime_state()
    storage.save_runtime_state(WeChatRuntimeState())
    try:
        storage.save_credential(credential)
    except BaseException:
        if runtime_state_existed:
            storage.save_runtime_state(previous_runtime_state)
        else:
            storage.runtime_state_path.unlink(missing_ok=True)
        raise


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
    except (httpx.TransportError, WeChatRetryableError) as exc:
        raise WeChatControlError("WeChat QR authorization request failed") from exc
    stream.write("\nWeChat authorization complete\n\n")
    if result.reused_existing:
        stream.write("  Existing local authorization: retained\n")
    else:
        stream.write("  Authorization: saved in private local storage\n")
    stream.write(f"  Bot identity: {result.credential.bot_id}\n")
    if not result.reused_existing:
        assert result.scanner_user_id is not None
        stream.write("\nAllowlist\n\n")
        stream.write(f"  Scanner user identity: {result.scanner_user_id}\n")
        stream.write("  Add the scanner identity to wechat.allowed_users.\n")
    stream.write("\n")
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


def _show_qr_url(stream: TextIO, qr_code: QRCode, *, refreshed: bool = False) -> None:
    title = (
        "Refreshed WeChat authorization URL"
        if refreshed
        else "WeChat authorization URL"
    )
    write_qr_url(
        stream,
        title,
        qr_code.authorization_url,
        instructions=(
            "Open the URL in WeChat and scan the QR code.",
            "Approve the iLink bot authorization and keep this terminal open.",
            "Waiting for WeChat authorization to complete.",
        ),
    )


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
