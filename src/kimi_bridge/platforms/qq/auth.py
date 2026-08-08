"""Headless QQ official-bot QR registration and local authorization controls.

Implements the bind flow of ``@tencent-connect/qqbot-connector@1.2.0``
against ``q.qq.com``: create a bind task with a fresh random AES-256 key,
show the connect URL, poll the bind result every two seconds, and only when
the server reports a completed bind with a decryptable secret persist a
managed credential. The AES key, the ``task_id``/QR URL, and the encrypted
secret blob are never persisted.

Polling semantics follow the connector: transport failures and non-zero
retcodes are retried with capped backoff, while malformed response shapes
fail the flow immediately so contract drift surfaces instead of looping.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO
from urllib.parse import quote

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .storage import (
    QQControlError,
    QQManagedCredential,
    QQStorage,
    QQStorageError,
    redact_app_id,
    redact_openid,
)

BIND_STATUS_NONE = 0
BIND_STATUS_PENDING = 1
BIND_STATUS_COMPLETED = 2
BIND_STATUS_EXPIRED = 3

QQ_BIND_TASK_URL = "https://q.qq.com/lite/create_bind_task"
QQ_POLL_BIND_URL = "https://q.qq.com/lite/poll_bind_result"
QQ_CONNECT_PAGE_URL = "https://q.qq.com/qqbot/openclaw/connect.html"

POLL_INTERVAL_SECONDS = 2.0
MAX_POLL_RETRY_DELAY_SECONDS = 8.0
REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_LOGIN_TIMEOUT_SECONDS = 480.0
DEFAULT_MAX_QR_ATTEMPTS = 3
DEFAULT_SOURCE = "kimi-bridge"

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
AuthorizedAtFactory = Callable[[], str]
KeyFactory = Callable[[], str]


class QQRegistrationError(QQControlError):
    """The QQ QR registration flow failed without writing a credential."""


class QQPollRetryableError(QQRegistrationError):
    """A transient QQ bind-status failure; the poll loop retries it."""


class QQAuthConfig(Protocol):
    """Config projection consumed by the control-plane runners."""

    @property
    def storage_path(self) -> Path: ...

    @property
    def app_id(self) -> str: ...

    @property
    def app_secret(self) -> str: ...


@dataclass(frozen=True, slots=True)
class QQRegistrationResult:
    """Successful local outcome of the QR registration flow."""

    credential: QQManagedCredential
    user_openid: str | None


# ---------------------------------------------------------------------------
# Secret handling (mirrors qqbot-session.js decryptSecret)
# ---------------------------------------------------------------------------


def generate_qr_key() -> str:
    """Return a fresh base64 AES-256 key for one bind task."""

    return base64.b64encode(os.urandom(32)).decode("ascii")


def encrypt_app_secret(key: str, app_secret: str) -> str:
    """Encrypt an AppSecret in the connector wire layout (nonce||ct||tag)."""

    raw_key = _decode_key(key)
    nonce = os.urandom(12)
    ciphertext = AESGCM(raw_key).encrypt(nonce, app_secret.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_app_secret(key: str, encrypted: str) -> str:
    """Decrypt a connector-encrypted AppSecret; failures are secret-safe."""

    try:
        raw_key = base64.b64decode(key, validate=True)
        payload = base64.b64decode(encrypted, validate=True)
    except ValueError as exc:
        raise QQRegistrationError(
            "QQ bind result secret could not be decrypted"
        ) from exc
    if len(raw_key) != 32 or len(payload) < 28:
        raise QQRegistrationError("QQ bind result secret could not be decrypted")
    nonce = payload[:12]
    ciphertext = payload[12:-16]
    tag = payload[-16:]
    try:
        plaintext = AESGCM(raw_key).decrypt(nonce, ciphertext + tag, None)
    except InvalidTag as exc:
        raise QQRegistrationError(
            "QQ bind result secret could not be decrypted"
        ) from exc
    try:
        app_secret = plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QQRegistrationError(
            "QQ bind result secret could not be decrypted"
        ) from exc
    if not app_secret:
        raise QQRegistrationError("QQ bind result secret could not be decrypted")
    return app_secret


def _decode_key(key: str) -> bytes:
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty base64 string")
    raw_key = base64.b64decode(key, validate=True)
    if len(raw_key) != 32:
        raise ValueError("key must decode to 32 bytes")
    return raw_key


# ---------------------------------------------------------------------------
# Bind task HTTP contract
# ---------------------------------------------------------------------------


def build_connect_url(task_id: str, source: str) -> str:
    """Build the operator-facing QR connect URL for one bind task."""

    return (
        f"{QQ_CONNECT_PAGE_URL}?task_id={quote(task_id, safe='')}"
        f"&source={quote(source, safe='')}&_wv=2"
    )


def _response_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise QQRegistrationError("QQ bind response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise QQRegistrationError("QQ bind response is malformed")
    return payload


async def _create_bind_task(client: httpx.AsyncClient, key: str) -> str:
    """POST the bind task and return its ``task_id``; failures are fatal."""

    try:
        response = await client.post(QQ_BIND_TASK_URL, json={"key": key})
    except httpx.RequestError as exc:
        raise QQRegistrationError("QQ bind task creation request failed") from exc
    if response.status_code != 200:
        raise QQRegistrationError(
            f"QQ bind task creation failed (HTTP {response.status_code})"
        )
    envelope = _response_object(response)
    retcode = envelope.get("retcode")
    if isinstance(retcode, bool) or not isinstance(retcode, int):
        raise QQRegistrationError("QQ bind task creation response is malformed")
    if retcode != 0:
        raise QQRegistrationError(f"QQ bind task creation failed (retcode {retcode})")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise QQRegistrationError("QQ bind task creation response is malformed")
    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise QQRegistrationError("QQ bind task creation response is malformed")
    return task_id


async def _poll_bind_result(
    client: httpx.AsyncClient, task_id: str
) -> tuple[int, dict[str, Any]]:
    """POST one bind-status poll; transport and retcode failures are retryable."""

    try:
        response = await client.post(QQ_POLL_BIND_URL, json={"task_id": task_id})
    except httpx.RequestError as exc:
        raise QQPollRetryableError("QQ bind status polling request failed") from exc
    if response.status_code != 200:
        raise QQPollRetryableError(
            f"QQ bind status polling failed (HTTP {response.status_code})"
        )
    envelope = _response_object(response)
    retcode = envelope.get("retcode")
    if isinstance(retcode, bool) or not isinstance(retcode, int):
        raise QQRegistrationError("QQ bind status response is malformed")
    if retcode != 0:
        raise QQPollRetryableError(f"QQ bind status polling failed (retcode {retcode})")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise QQRegistrationError("QQ bind status response is malformed")
    status = data.get("status")
    valid_statuses = (
        BIND_STATUS_NONE,
        BIND_STATUS_PENDING,
        BIND_STATUS_COMPLETED,
        BIND_STATUS_EXPIRED,
    )
    if isinstance(status, bool) or not isinstance(status, int):
        raise QQRegistrationError("QQ bind status response is malformed")
    if status not in valid_statuses:
        raise QQRegistrationError("QQ bind status response is malformed")
    return status, data


async def _poll_task(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    deadline: float,
    clock: Clock,
    sleep: Sleep,
    poll_interval_seconds: float,
    max_poll_retry_delay_seconds: float,
) -> dict[str, Any] | None:
    """Poll until completed or expired; ``None`` means the task expired."""

    retry_delay = poll_interval_seconds
    while clock() < deadline:
        try:
            status, data = await _poll_bind_result(client, task_id)
        except QQPollRetryableError:
            await sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_poll_retry_delay_seconds)
            continue
        retry_delay = poll_interval_seconds
        if status in (BIND_STATUS_NONE, BIND_STATUS_PENDING):
            await sleep(poll_interval_seconds)
            continue
        if status == BIND_STATUS_EXPIRED:
            return None
        if status == BIND_STATUS_COMPLETED:
            return data
        raise AssertionError(f"unhandled QQ bind status: {status}")
    raise QQControlError("QQ QR authorization timed out")


def _completed_credential(
    data: dict[str, Any], key: str, *, authorized_at: AuthorizedAtFactory
) -> QQManagedCredential:
    app_id = data.get("bot_appid")
    if not isinstance(app_id, str) or not app_id:
        raise QQRegistrationError("QQ bind result is missing the bot app_id")
    encrypted = data.get("bot_encrypt_secret")
    if not isinstance(encrypted, str) or not encrypted:
        raise QQRegistrationError("QQ bind result is missing the bot secret")
    app_secret = decrypt_app_secret(key, encrypted)
    return QQManagedCredential(
        app_id=app_id, app_secret=app_secret, authorized_at=authorized_at()
    )


def _optional_openid(data: dict[str, Any]) -> str | None:
    value = data.get("user_openid")
    if value is None:
        return None
    if not isinstance(value, str):
        raise QQRegistrationError("QQ bind result user_openid is invalid")
    return value or None


def _show_qr_url(stream: TextIO, url: str, *, refreshed: bool = False) -> None:
    prefix = "Refreshed QQ authorization URL" if refreshed else "QQ authorization URL"
    stream.write(f"{prefix}:\n{url}\n")
    stream.flush()


# ---------------------------------------------------------------------------
# Registration flow
# ---------------------------------------------------------------------------


async def authorize_with_qr(
    storage: QQStorage,
    *,
    replace: bool = False,
    stream: TextIO = sys.stdout,
    http_client: httpx.AsyncClient | None = None,
    source: str = DEFAULT_SOURCE,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    authorized_at: AuthorizedAtFactory = lambda: datetime.now(timezone.utc).isoformat(),
    key_factory: KeyFactory = generate_qr_key,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    max_poll_retry_delay_seconds: float = MAX_POLL_RETRY_DELAY_SECONDS,
    max_qr_attempts: int = DEFAULT_MAX_QR_ATTEMPTS,
    timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
) -> QQRegistrationResult:
    """Complete one QR registration and persist only a confirmed credential.

    ``http_client`` is optional: when omitted a private client is created and
    closed for the duration of the flow; a caller-owned client stays open.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    if max_poll_retry_delay_seconds < poll_interval_seconds:
        raise ValueError(
            "max_poll_retry_delay_seconds must be at least the poll interval"
        )
    if max_qr_attempts <= 0:
        raise ValueError("max_qr_attempts must be positive")
    if not source:
        raise ValueError("source must be a non-empty string")

    if storage.has_credential() and not replace:
        raise QQControlError("QQ authorization already exists; use login --replace")

    deadline = clock() + timeout_seconds
    attempts = 0
    async with contextlib.AsyncExitStack() as stack:
        if http_client is None:
            client = await stack.enter_async_context(
                httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
            )
        else:
            client = http_client
        while True:
            if clock() >= deadline:
                raise QQControlError("QQ QR authorization timed out")
            key = key_factory()
            task_id = await _create_bind_task(client, key)
            _show_qr_url(
                stream,
                build_connect_url(task_id, source),
                refreshed=attempts > 0,
            )
            data = await _poll_task(
                client,
                task_id,
                deadline=deadline,
                clock=clock,
                sleep=sleep,
                poll_interval_seconds=poll_interval_seconds,
                max_poll_retry_delay_seconds=max_poll_retry_delay_seconds,
            )
            if data is None:
                attempts += 1
                if attempts >= max_qr_attempts:
                    raise QQControlError(
                        "QQ QR authorization stopped after repeated refreshes"
                    )
                stream.write("QR code expired; refreshing.\n")
                stream.flush()
                continue
            credential = _completed_credential(data, key, authorized_at=authorized_at)
            user_openid = _optional_openid(data)
            storage.save_credential(credential)
            return QQRegistrationResult(credential=credential, user_openid=user_openid)


# ---------------------------------------------------------------------------
# CLI runners
# ---------------------------------------------------------------------------


def run_login(
    config: QQAuthConfig,
    *,
    replace: bool = False,
    stream: TextIO = sys.stdout,
    http_client: httpx.AsyncClient | None = None,
    key_factory: KeyFactory = generate_qr_key,
) -> int:
    """Run the networked QR flow without starting Kimi Code or message polling."""

    result = asyncio.run(
        authorize_with_qr(
            QQStorage(config.storage_path),
            replace=replace,
            stream=stream,
            http_client=http_client,
            key_factory=key_factory,
        )
    )
    stream.write("QQ authorization saved locally.\n")
    stream.write(f"Bot app_id: {redact_app_id(result.credential.app_id)}\n")
    stream.write(f"Authorized at: {result.credential.authorized_at}\n")
    if result.user_openid is not None:
        stream.write(f"Scanner user openid: {result.user_openid}\n")
        stream.write("Add the scanner openid to qq.allowed_users.\n")
    stream.flush()
    return 0


def run_status(
    config: QQAuthConfig,
    *,
    stream: TextIO = sys.stdout,
    platform_name: str = sys.platform,
) -> int:
    """Render local authorization presence and hygiene without network access."""

    storage = QQStorage(config.storage_path)
    inspection = storage.inspect(platform_name=platform_name)
    stream.write(f"QQ storage: {storage.path}\n")
    if inspection.directory_error:
        stream.write(f"Storage error: {inspection.directory_error}.\n")
    elif inspection.directory_exists:
        stream.write("Storage directory: locally usable.\n")
    else:
        stream.write("Storage directory: not created.\n")

    if inspection.credential_error:
        stream.write(f"Authorization error: {inspection.credential_error}.\n")
    elif inspection.credential is not None:
        credential = inspection.credential
        stream.write(
            "Authorization: present locally; network status was not checked.\n"
        )
        stream.write(f"Bot app_id: {redact_app_id(credential.app_id)}\n")
        stream.write(f"Authorized at: {credential.authorized_at}\n")
    elif config.app_id and config.app_secret:
        stream.write("Managed authorization: not present locally.\n")
        stream.write(
            f"Legacy TOML authorization: present locally for {redact_app_id(config.app_id)}; "
            "network status was not checked.\n"
        )
    else:
        stream.write("Authorization: not configured locally.\n")
    stream.flush()
    return int(
        inspection.directory_error is not None
        or inspection.credential_error is not None
        or (
            inspection.credential is None
            and not (config.app_id and config.app_secret)
        )
    )


def run_logout(
    config: QQAuthConfig,
    *,
    stream: TextIO = sys.stdout,
) -> int:
    """Remove only files whose names are owned by the QQ adapter."""

    removed = QQStorage(config.storage_path).clear_owned_files()
    if removed:
        stream.write("Removed QQ adapter files: " + ", ".join(removed) + ".\n")
    else:
        stream.write("No QQ adapter files were present.\n")
    stream.flush()
    return 0


__all__ = [
    "BIND_STATUS_COMPLETED",
    "BIND_STATUS_EXPIRED",
    "BIND_STATUS_NONE",
    "BIND_STATUS_PENDING",
    "DEFAULT_LOGIN_TIMEOUT_SECONDS",
    "DEFAULT_MAX_QR_ATTEMPTS",
    "DEFAULT_SOURCE",
    "MAX_POLL_RETRY_DELAY_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "QQ_BIND_TASK_URL",
    "QQ_CONNECT_PAGE_URL",
    "QQ_POLL_BIND_URL",
    "QQAuthConfig",
    "QQControlError",
    "QQManagedCredential",
    "QQRegistrationError",
    "QQRegistrationResult",
    "QQStorage",
    "QQStorageError",
    "QQPollRetryableError",
    "authorize_with_qr",
    "build_connect_url",
    "decrypt_app_secret",
    "encrypt_app_secret",
    "generate_qr_key",
    "redact_app_id",
    "redact_openid",
    "run_login",
    "run_logout",
    "run_status",
]
