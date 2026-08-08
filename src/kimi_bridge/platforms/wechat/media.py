"""Bounded encrypted CDN media support for the pinned WeChat contract."""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from ..base import (
    InboundAudio,
    InboundFile,
    InboundImage,
    InboundVideo,
    OutboundFile,
)
from .types import (
    DEFAULT_CDN_BASE_URL,
    MESSAGE_ITEM_TYPE_FILE,
    MESSAGE_ITEM_TYPE_IMAGE,
    MESSAGE_ITEM_TYPE_VIDEO,
    UPLOAD_MEDIA_TYPE_FILE,
    UPLOAD_MEDIA_TYPE_IMAGE,
    UPLOAD_MEDIA_TYPE_VIDEO,
    WeChatCDNMedia,
    WeChatFileItem,
    WeChatImageItem,
    WeChatMessageItem,
    WeChatProtocolError,
    WeChatRetryableError,
    WeChatUploadRequest,
    WeChatUploadedMedia,
    WeChatUploadTarget,
    WeChatVideoItem,
    WeChatVoiceItem,
)


LOGGER = logging.getLogger(__name__)
WECHAT_MEDIA_MAX_BYTES = 100 * 1024 * 1024
WECHAT_CDN_TIMEOUT_SECONDS = 60.0
WECHAT_CDN_UPLOAD_ATTEMPTS = 3
_AES_BLOCK_BYTES = 16
_MAX_SECRET_FIELD_BYTES = 16 * 1024
_MAX_MEDIA_NAME_BYTES = 255
_MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_MD5 = re.compile(r"^[0-9a-fA-F]{32}$")


class WeChatMediaError(WeChatProtocolError):
    """Malformed or failed WeChat CDN media operation."""


class WeChatMediaTooLarge(WeChatMediaError):
    """Media exceeds the adapter's explicit plaintext bound."""


class WeChatMediaDependencyError(RuntimeError):
    """The installation is missing WeChat's required media dependency."""


class WeChatMediaAPI(Protocol):
    async def get_upload_url(
        self, request: WeChatUploadRequest
    ) -> WeChatUploadTarget: ...


@dataclass(frozen=True, slots=True)
class WeChatOutboundClassification:
    message_item_type: int
    upload_media_type: int
    name: str


def cryptography_available() -> bool:
    """Return whether the required WeChat media primitive can be imported."""

    try:
        from cryptography.hazmat.primitives import padding as _padding  # noqa: F401
        from cryptography.hazmat.primitives.ciphers import (  # noqa: F401
            Cipher as _Cipher,
        )
    except ImportError:
        return False
    return True


def require_wechat_media_dependency() -> None:
    if not cryptography_available():
        raise WeChatMediaDependencyError(
            "WeChat requires its encrypted-media dependency; reinstall kimi-bridge"
        )


def aes_ecb_padded_size(plaintext_size: int) -> int:
    if isinstance(plaintext_size, bool) or not isinstance(plaintext_size, int):
        raise TypeError("plaintext_size must be an integer")
    if plaintext_size < 0:
        raise ValueError("plaintext_size cannot be negative")
    return ((plaintext_size // _AES_BLOCK_BYTES) + 1) * _AES_BLOCK_BYTES


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt with AES-128-ECB and PKCS#7, matching Tencent v2.4.6."""

    _validate_key(key)
    if not isinstance(plaintext, bytes):
        raise TypeError("plaintext must be bytes")
    Cipher, algorithms, modes, padding = _crypto_components()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-ECB and reject malformed PKCS#7 padding."""

    _validate_key(key)
    if not isinstance(ciphertext, bytes):
        raise TypeError("ciphertext must be bytes")
    if not ciphertext or len(ciphertext) % _AES_BLOCK_BYTES:
        raise WeChatMediaError("WeChat CDN ciphertext has an invalid length")
    Cipher, algorithms, modes, padding = _crypto_components()
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise WeChatMediaError("WeChat CDN ciphertext has invalid padding") from exc


def parse_aes_key(value: str) -> bytes:
    """Decode either tagged base64 key representation without exposing it."""

    if not isinstance(value, str) or not value or len(value) > 64:
        raise WeChatMediaError("WeChat CDN AES key is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WeChatMediaError("WeChat CDN AES key is invalid") from exc
    if len(decoded) == _AES_BLOCK_BYTES:
        return decoded
    if len(decoded) == 32 and _MD5.fullmatch(decoded.decode("ascii", "ignore")):
        return bytes.fromhex(decoded.decode("ascii"))
    raise WeChatMediaError("WeChat CDN AES key is invalid")


def classify_outbound_file(file: OutboundFile) -> WeChatOutboundClassification:
    name = _validate_media_name(file.name)
    media_type = _validate_media_type(file.media_type)
    suffix = _suffix(name)
    if media_type.startswith("image/"):
        if suffix in _VIDEO_SUFFIXES or suffix in _FILE_MEDIA_TYPES:
            raise WeChatMediaError("outbound media MIME type conflicts with its name")
        return WeChatOutboundClassification(
            MESSAGE_ITEM_TYPE_IMAGE, UPLOAD_MEDIA_TYPE_IMAGE, name
        )
    if media_type.startswith("video/"):
        if suffix in _IMAGE_SUFFIXES or suffix in _FILE_MEDIA_TYPES:
            raise WeChatMediaError("outbound media MIME type conflicts with its name")
        return WeChatOutboundClassification(
            MESSAGE_ITEM_TYPE_VIDEO, UPLOAD_MEDIA_TYPE_VIDEO, name
        )
    return WeChatOutboundClassification(
        MESSAGE_ITEM_TYPE_FILE, UPLOAD_MEDIA_TYPE_FILE, name
    )


class WeChatMediaClient:
    """In-memory CDN pipeline; no temporary files survive success or failure."""

    def __init__(
        self,
        api: WeChatMediaAPI,
        client: httpx.AsyncClient | None = None,
        *,
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
        max_bytes: int = WECHAT_MEDIA_MAX_BYTES,
        timeout_seconds: float = WECHAT_CDN_TIMEOUT_SECONDS,
        upload_attempts: int = WECHAT_CDN_UPLOAD_ATTEMPTS,
    ) -> None:
        if max_bytes <= 0 or timeout_seconds <= 0 or upload_attempts <= 0:
            raise ValueError("WeChat media limits and attempts must be positive")
        self._api = api
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._cdn_base_url = _normalize_cdn_base_url(cdn_base_url)
        parsed = urlsplit(self._cdn_base_url)
        self._cdn_host = parsed.hostname or ""
        self._cdn_port = parsed.port
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._max_bytes = max_bytes
        self._max_ciphertext_bytes = aes_ecb_padded_size(max_bytes)
        self._timeout_seconds = timeout_seconds
        self._upload_attempts = upload_attempts
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def download_item(
        self, item: WeChatMessageItem
    ) -> InboundImage | InboundVideo | InboundFile | InboundAudio:
        if item.image is not None:
            return await self._download_image(item.image)
        if item.voice is not None:
            return await self._download_voice(item.voice)
        if item.file is not None:
            return await self._download_file(item.file)
        if item.video is not None:
            return await self._download_video(item.video)
        raise WeChatMediaError("WeChat media item is missing its native payload")

    async def upload_file(
        self, file: OutboundFile, *, to_user_id: str
    ) -> tuple[WeChatOutboundClassification, WeChatUploadedMedia]:
        if not isinstance(file.data, bytes):
            raise TypeError("outbound media data must be bytes")
        if len(file.data) > self._max_bytes:
            raise WeChatMediaTooLarge("outbound WeChat media exceeds the size limit")
        if not isinstance(to_user_id, str) or not to_user_id.strip():
            raise ValueError("WeChat upload recipient must be non-empty")
        classification = classify_outbound_file(file)
        ciphertext_size = aes_ecb_padded_size(len(file.data))
        if ciphertext_size > self._max_ciphertext_bytes:
            raise WeChatMediaTooLarge("outbound WeChat media exceeds the size limit")
        key = secrets.token_bytes(_AES_BLOCK_BYTES)
        file_key = secrets.token_hex(_AES_BLOCK_BYTES)
        key_hex = key.hex()
        request = WeChatUploadRequest(
            file_key=file_key,
            media_type=classification.upload_media_type,
            to_user_id=to_user_id.strip(),
            raw_size=len(file.data),
            raw_file_md5=hashlib.md5(file.data, usedforsecurity=False).hexdigest(),
            ciphertext_size=ciphertext_size,
            aes_key_hex=key_hex,
        )
        target = await self._api.get_upload_url(request)
        url = self._upload_url(target, file_key)
        ciphertext = encrypt_aes_ecb(file.data, key)
        if len(ciphertext) != ciphertext_size:
            raise WeChatMediaError("WeChat CDN encryption produced an invalid size")
        download_param = await self._upload(url, ciphertext)
        return classification, WeChatUploadedMedia(
            download_query_param=download_param,
            aes_key_hex=key_hex,
            plaintext_size=len(file.data),
            ciphertext_size=ciphertext_size,
        )

    async def _download_image(self, item: WeChatImageItem) -> InboundImage:
        reference = _required_reference(item.media)
        key = (
            _parse_hex_key(item.aes_key_hex)
            if item.aes_key_hex is not None
            else _optional_reference_key(reference)
        )
        data = await self._download_reference(
            reference,
            key=key,
            declared_size=_optional_nonnegative_size(item.mid_size, "image size"),
            allow_plain=True,
        )
        media_type, name = _classify_image(data)
        return InboundImage(data=data, media_type=media_type, name=name)

    async def _download_voice(self, item: WeChatVoiceItem) -> InboundAudio:
        reference = _required_reference(item.media)
        key = _required_reference_key(reference)
        media_type, name = _voice_format(item.encode_type)
        try:
            data = await self._download_reference(reference, key=key)
        except WeChatRetryableError:
            if item.text is None or not item.text.strip():
                raise
            data = b""
        return InboundAudio(
            data=data,
            media_type=media_type,
            name=name,
            transcript=item.text,
        )

    async def _download_file(self, item: WeChatFileItem) -> InboundFile:
        reference = _required_reference(item.media)
        key = _required_reference_key(reference)
        name = _validate_media_name(item.file_name or "file.bin")
        declared_length = _parse_declared_length(item.length)
        if declared_length is not None and declared_length > self._max_bytes:
            raise WeChatMediaTooLarge("inbound WeChat file exceeds the size limit")
        data = await self._download_reference(reference, key=key)
        if declared_length is not None and len(data) != declared_length:
            raise WeChatMediaError("inbound WeChat file length does not match")
        _validate_md5(data, item.md5, "file")
        media_type = _FILE_MEDIA_TYPES.get(_suffix(name), "application/octet-stream")
        return InboundFile(data=data, name=name, media_type=media_type)

    async def _download_video(self, item: WeChatVideoItem) -> InboundVideo:
        reference = _required_reference(item.media)
        key = _required_reference_key(reference)
        data = await self._download_reference(
            reference,
            key=key,
            declared_size=_optional_nonnegative_size(item.video_size, "video size"),
        )
        _validate_md5(data, item.video_md5, "video")
        return InboundVideo(data=data, media_type="video/mp4", name="video.mp4")

    async def _download_reference(
        self,
        reference: WeChatCDNMedia,
        *,
        key: bytes | None,
        declared_size: int | None = None,
        allow_plain: bool = False,
    ) -> bytes:
        encrypted = key is not None
        if not encrypted and not allow_plain:
            raise WeChatMediaError("WeChat CDN AES key is missing")
        maximum = self._max_ciphertext_bytes if encrypted else self._max_bytes
        if declared_size is not None and declared_size > maximum:
            raise WeChatMediaTooLarge("inbound WeChat media exceeds the size limit")
        url = self._download_url(reference)
        payload = await self._download(url, maximum=maximum)
        data = decrypt_aes_ecb(payload, key) if key is not None else payload
        if len(data) > self._max_bytes:
            raise WeChatMediaTooLarge("inbound WeChat media exceeds the size limit")
        return data

    async def _download(self, url: str, *, maximum: int) -> bytes:
        try:
            async with self._client.stream(
                "GET",
                url,
                timeout=self._timeout_seconds,
                follow_redirects=False,
            ) as response:
                _validate_cdn_response(response, operation="download")
                _validate_content_length(response, maximum)
                total = 0
                chunks: list[bytes] = []
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > maximum:
                        raise WeChatMediaTooLarge(
                            "inbound WeChat media exceeds the size limit"
                        )
                    chunks.append(chunk)
        except httpx.InvalidURL:
            raise WeChatMediaError("WeChat CDN download URL is invalid") from None
        except httpx.TransportError:
            raise WeChatRetryableError("CDN download", "transport") from None
        return b"".join(chunks)

    async def _upload(self, url: str, ciphertext: bytes) -> str:
        for attempt in range(self._upload_attempts):
            try:
                response = await self._client.post(
                    url,
                    headers={"Content-Type": "application/octet-stream"},
                    content=ciphertext,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                )
                _validate_cdn_response(response, operation="upload", exact_200=True)
                value = response.headers.get("x-encrypted-param")
                if (
                    value is None
                    or not value.strip()
                    or len(value.encode("utf-8")) > _MAX_SECRET_FIELD_BYTES
                ):
                    raise WeChatMediaError(
                        "WeChat CDN upload response is missing its download parameter"
                    )
                return value.strip()
            except httpx.InvalidURL:
                raise WeChatMediaError("WeChat CDN upload URL is invalid") from None
            except httpx.TransportError:
                error = WeChatRetryableError("CDN upload", "transport")
            except WeChatRetryableError as exc:
                error = exc
            if attempt + 1 >= self._upload_attempts:
                raise error
            LOGGER.warning("WeChat CDN upload transient failure; retrying")
        raise AssertionError("unreachable WeChat CDN retry state")

    def _download_url(self, reference: WeChatCDNMedia) -> str:
        if reference.full_url is not None:
            return self._validate_cdn_url(reference.full_url)
        query = _validate_secret_field(
            reference.encrypt_query_param, "WeChat CDN download parameter"
        )
        return f"{self._cdn_base_url}/download?{urlencode({'encrypted_query_param': query})}"

    def _upload_url(self, target: WeChatUploadTarget, file_key: str) -> str:
        if target.upload_full_url is not None:
            return self._validate_cdn_url(target.upload_full_url)
        query = _validate_secret_field(
            target.upload_param, "WeChat CDN upload parameter"
        )
        return (
            f"{self._cdn_base_url}/upload?"
            f"{urlencode({'encrypted_query_param': query, 'filekey': file_key})}"
        )

    def _validate_cdn_url(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise WeChatMediaError("WeChat CDN URL is invalid")
        parsed = urlsplit(value.strip())
        try:
            port = parsed.port
        except ValueError as exc:
            raise WeChatMediaError("WeChat CDN URL is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != self._cdn_host
            or port != self._cdn_port
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path
            or parsed.fragment
        ):
            raise WeChatMediaError("WeChat CDN URL is invalid")
        return value.strip()


def _crypto_components():
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise WeChatMediaDependencyError(
            "WeChat requires its encrypted-media dependency; reinstall kimi-bridge"
        ) from exc
    return Cipher, algorithms, modes, padding


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != _AES_BLOCK_BYTES:
        raise WeChatMediaError("WeChat CDN AES key must contain 16 bytes")


def _parse_hex_key(value: str) -> bytes:
    if not isinstance(value, str) or not _MD5.fullmatch(value):
        raise WeChatMediaError("WeChat CDN AES key is invalid")
    return bytes.fromhex(value)


def _required_reference(value: WeChatCDNMedia | None) -> WeChatCDNMedia:
    if value is None:
        raise WeChatMediaError("WeChat media item is missing its CDN reference")
    if value.full_url is None and value.encrypt_query_param is None:
        raise WeChatMediaError("WeChat CDN reference has no download location")
    return value


def _optional_reference_key(reference: WeChatCDNMedia) -> bytes | None:
    return parse_aes_key(reference.aes_key) if reference.aes_key is not None else None


def _required_reference_key(reference: WeChatCDNMedia) -> bytes:
    if reference.aes_key is None:
        raise WeChatMediaError("WeChat CDN AES key is missing")
    return parse_aes_key(reference.aes_key)


def _validate_secret_field(value: str | None, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > _MAX_SECRET_FIELD_BYTES
    ):
        raise WeChatMediaError(f"{name} is invalid")
    return value.strip()


def _normalize_cdn_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeChatMediaError("WeChat CDN base URL is invalid")
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise WeChatMediaError("WeChat CDN base URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise WeChatMediaError("WeChat CDN base URL is invalid")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", netloc, path, "", ""))


def _validate_cdn_response(
    response: httpx.Response, *, operation: str, exact_200: bool = False
) -> None:
    if 300 <= response.status_code < 400:
        raise WeChatMediaError(f"WeChat CDN {operation} redirect was rejected")
    expected = response.status_code == 200 if exact_200 else response.is_success
    if expected:
        encoding = response.headers.get("content-encoding")
        if encoding is not None and encoding.lower() != "identity":
            raise WeChatMediaError(
                f"WeChat CDN {operation} response has unsupported encoding"
            )
        return
    if response.status_code in {408, 425, 429} or response.status_code >= 500:
        raise WeChatRetryableError(
            f"CDN {operation}", "HTTP", status_code=response.status_code
        )
    raise WeChatMediaError(
        f"WeChat CDN {operation} failed with HTTP {response.status_code}"
    )


def _validate_content_length(response: httpx.Response, maximum: int) -> None:
    raw = response.headers.get("content-length")
    if raw is None:
        return
    if not raw.isascii() or not raw.isdigit():
        raise WeChatMediaError("WeChat CDN response has invalid Content-Length")
    if int(raw) > maximum:
        raise WeChatMediaTooLarge("inbound WeChat media exceeds the size limit")


def _optional_nonnegative_size(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WeChatMediaError(f"WeChat {name} is invalid")
    return value


def _parse_declared_length(value: str | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise WeChatMediaError("inbound WeChat file length is invalid")
    return int(value)


def _validate_md5(data: bytes, expected: str | None, label: str) -> None:
    if expected is None:
        return
    if not isinstance(expected, str) or not _MD5.fullmatch(expected):
        raise WeChatMediaError(f"inbound WeChat {label} MD5 is invalid")
    actual = hashlib.md5(data, usedforsecurity=False).hexdigest()
    if not secrets.compare_digest(actual, expected.lower()):
        raise WeChatMediaError(f"inbound WeChat {label} MD5 does not match")


def _validate_media_name(value: str) -> str:
    if not isinstance(value, str):
        raise WeChatMediaError("WeChat media name is invalid")
    name = value.strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or len(name.encode("utf-8")) > _MAX_MEDIA_NAME_BYTES
    ):
        raise WeChatMediaError("WeChat media name is invalid")
    return name


def _validate_media_type(value: str) -> str:
    if not isinstance(value, str) or not _MIME_TYPE.fullmatch(value.strip()):
        raise WeChatMediaError("outbound WeChat media MIME type is invalid")
    return value.strip().lower()


def _suffix(name: str) -> str:
    marker = name.rfind(".")
    return name[marker:].lower() if marker >= 0 else ""


_IMAGE_SUFFIXES = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
_VIDEO_SUFFIXES = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})
_FILE_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".json": "application/json",
    ".md": "text/markdown",
    ".mp3": "audio/mpeg",
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
}


def _classify_image(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "image.jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "image.png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "image.gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", "image.webp"
    if data.startswith(b"BM"):
        return "image/bmp", "image.bmp"
    raise WeChatMediaError("inbound WeChat image format is unsupported")


def _voice_format(encode_type: int | None) -> tuple[str, str]:
    if encode_type in {None, 6}:
        return "audio/silk", "voice.silk"
    formats = {
        1: ("audio/l16", "voice.pcm"),
        2: ("audio/adpcm", "voice.adpcm"),
        4: ("audio/amr", "voice.amr"),
        5: ("audio/speex", "voice.spx"),
        7: ("audio/mpeg", "voice.mp3"),
        8: ("audio/ogg", "voice.ogg"),
    }
    try:
        return formats[encode_type]
    except KeyError as exc:
        raise WeChatMediaError("inbound WeChat voice encoding is unsupported") from exc
