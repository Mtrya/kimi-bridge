from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from kimi_bridge.platforms.base import (
    ConversationRef,
    InboundAudio,
    InboundFile,
    InboundImage,
    InboundMessage,
    InboundVideo,
    OutboundFile,
)
from kimi_bridge.platforms.wechat import (
    WeChatAPI,
    WeChatAPIResult,
    WeChatAdapter,
    WeChatCDNMedia,
    WeChatCredential,
    WeChatFileItem,
    WeChatImageItem,
    WeChatInboundEvent,
    WeChatMediaClient,
    WeChatMediaError,
    WeChatMediaTooLarge,
    WeChatMessageItem,
    WeChatPollResult,
    WeChatProtocolError,
    WeChatRetryableError,
    WeChatRuntimeState,
    WeChatStorage,
    WeChatTypingConfig,
    WeChatUploadRequest,
    WeChatUploadTarget,
    WeChatVideoItem,
    WeChatVoiceItem,
)
from kimi_bridge.platforms.wechat.media import (
    aes_ecb_padded_size,
    classify_outbound_file,
    decrypt_aes_ecb,
    encrypt_aes_ecb,
    parse_aes_key,
)
from kimi_bridge.platforms.wechat.types import (
    MESSAGE_ITEM_TYPE_FILE,
    MESSAGE_ITEM_TYPE_IMAGE,
    MESSAGE_ITEM_TYPE_TEXT,
    MESSAGE_ITEM_TYPE_VIDEO,
    MESSAGE_ITEM_TYPE_VOICE,
    MESSAGE_TYPE_USER,
)


FIXED_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIXED_KEY_HEX = FIXED_KEY.hex()
FIXED_VECTOR_PLAINTEXT = bytes.fromhex("00112233445566778899aabbccddeeff")
FIXED_VECTOR_CIPHERTEXT = bytes.fromhex(
    "69c4e0d86a7b0430d8cdb78070b4c55a954f64f2e4e86e9eee82d20216684899"
)
CDN_ORIGIN = "https://novac2c.cdn.weixin.qq.com"


def _credential() -> WeChatCredential:
    return WeChatCredential(
        bot_token="BOT_TOKEN_SECRET",
        bot_id="bot-one@im.bot",
        base_url="https://ilinkai.weixin.qq.com",
        authorized_at="2026-08-08T12:00:00+00:00",
    )


def _raw_key_base64(key: bytes = FIXED_KEY) -> str:
    return base64.b64encode(key).decode("ascii")


def _hex_key_base64(key: bytes = FIXED_KEY) -> str:
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")


def _reference(path: str, *, key: str | None = None) -> WeChatCDNMedia:
    return WeChatCDNMedia(
        encrypt_query_param=f"DOWNLOAD_PARAM_SECRET_{path}",
        aes_key=key,
        encrypt_type=1,
        full_url=f"{CDN_ORIGIN}/c2c/download/{path}?signature=URL_SECRET",
    )


class RuntimeAPI:
    def __init__(self) -> None:
        self.poll_started = asyncio.Event()
        self.closed = False

    async def get_updates(
        self, get_updates_buf: str, *, timeout_seconds: float
    ) -> WeChatPollResult:
        assert isinstance(get_updates_buf, str)
        assert timeout_seconds > 0
        self.poll_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send_text(self, **_kwargs: Any) -> None:
        return None

    async def get_upload_url(self, request: WeChatUploadRequest) -> WeChatUploadTarget:
        raise AssertionError(f"unexpected upload: {request!r}")

    async def send_media(self, **_kwargs: Any) -> None:
        raise AssertionError("unexpected outbound media")

    async def get_config(
        self, *, ilink_user_id: str, context_token: str
    ) -> WeChatTypingConfig:
        assert ilink_user_id
        assert context_token
        return WeChatTypingConfig()

    async def send_typing(self, **_kwargs: Any) -> None:
        return None

    async def notify_start(self) -> WeChatAPIResult:
        return WeChatAPIResult()

    async def notify_stop(self) -> WeChatAPIResult:
        return WeChatAPIResult()

    async def close(self) -> None:
        self.closed = True


class UploadAPI:
    def __init__(self, target: WeChatUploadTarget) -> None:
        self.target = target
        self.requests: list[WeChatUploadRequest] = []

    async def get_upload_url(self, request: WeChatUploadRequest) -> WeChatUploadTarget:
        self.requests.append(request)
        return self.target


def _event(items: tuple[WeChatMessageItem, ...]) -> WeChatInboundEvent:
    return WeChatInboundEvent(
        message_id=4401,
        from_user_id="user-one",
        create_time_ms=1_754_640_000_123,
        message_type=MESSAGE_TYPE_USER,
        group_id=None,
        items=items,
        context_token="CONTEXT_TOKEN_SECRET",
    )


def test_actual_crypto_matches_fixed_openssl_compatible_fixture() -> None:
    assert aes_ecb_padded_size(0) == 16
    assert aes_ecb_padded_size(15) == 16
    assert aes_ecb_padded_size(16) == 32
    assert encrypt_aes_ecb(FIXED_VECTOR_PLAINTEXT, FIXED_KEY) == (
        FIXED_VECTOR_CIPHERTEXT
    )
    assert decrypt_aes_ecb(FIXED_VECTOR_CIPHERTEXT, FIXED_KEY) == (
        FIXED_VECTOR_PLAINTEXT
    )
    assert parse_aes_key(_raw_key_base64()) == FIXED_KEY
    assert parse_aes_key(_hex_key_base64()) == FIXED_KEY


def test_crypto_rejects_malformed_keys_lengths_and_padding() -> None:
    with pytest.raises(WeChatMediaError, match="AES key"):
        parse_aes_key("not-base64")
    with pytest.raises(WeChatMediaError, match="16 bytes"):
        encrypt_aes_ecb(b"payload", b"short")
    with pytest.raises(WeChatMediaError, match="invalid length"):
        decrypt_aes_ecb(b"not-a-block", FIXED_KEY)
    corrupted = FIXED_VECTOR_CIPHERTEXT[:-1] + bytes([FIXED_VECTOR_CIPHERTEXT[-1] ^ 1])
    with pytest.raises(WeChatMediaError, match="padding"):
        decrypt_aes_ecb(corrupted, FIXED_KEY)


async def test_runtime_parser_projects_all_pinned_inbound_media_fields() -> None:
    payload = {
        "ret": 0,
        "msgs": [
            {
                "message_id": 4401,
                "from_user_id": "user-one",
                "create_time_ms": 1_754_640_000_123,
                "message_type": 1,
                "context_token": "CONTEXT_TOKEN_SECRET",
                "item_list": [
                    {
                        "type": 2,
                        "image_item": {
                            "media": {
                                "encrypt_query_param": "IMAGE_PARAM_SECRET",
                                "aes_key": "IMAGE_MEDIA_KEY_SECRET",
                                "encrypt_type": 1,
                                "full_url": f"{CDN_ORIGIN}/image?secret=one",
                            },
                            "aeskey": FIXED_KEY_HEX,
                            "mid_size": 32,
                        },
                    },
                    {
                        "type": 3,
                        "voice_item": {
                            "media": {
                                "encrypt_query_param": "VOICE_PARAM_SECRET",
                                "aes_key": _hex_key_base64(),
                            },
                            "encode_type": 6,
                            "sample_rate": 16000,
                            "playtime": 420,
                            "text": "native transcript secret",
                        },
                    },
                    {
                        "type": 4,
                        "file_item": {
                            "media": {
                                "full_url": f"{CDN_ORIGIN}/file?secret=three",
                                "aes_key": _raw_key_base64(),
                            },
                            "file_name": "notes.txt",
                            "md5": "0123456789abcdef0123456789abcdef",
                            "len": "17",
                        },
                    },
                    {
                        "type": 5,
                        "video_item": {
                            "media": {
                                "encrypt_query_param": "VIDEO_PARAM_SECRET",
                                "aes_key": _raw_key_base64(),
                            },
                            "video_size": 48,
                            "video_md5": "fedcba9876543210fedcba9876543210",
                        },
                    },
                ],
            }
        ],
        "get_updates_buf": "CURSOR_SECRET",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        poll = await WeChatAPI(_credential(), client).get_updates("", timeout_seconds=1)

    image, voice, file_item, video = poll.messages[0].items
    assert image.image == WeChatImageItem(
        media=WeChatCDNMedia(
            encrypt_query_param="IMAGE_PARAM_SECRET",
            aes_key="IMAGE_MEDIA_KEY_SECRET",
            encrypt_type=1,
            full_url=f"{CDN_ORIGIN}/image?secret=one",
        ),
        aes_key_hex=FIXED_KEY_HEX,
        mid_size=32,
    )
    assert voice.voice is not None
    assert voice.voice.encode_type == 6
    assert voice.voice.text == "native transcript secret"
    assert file_item.file is not None
    assert file_item.file.length == "17"
    assert video.video is not None
    assert video.video.video_size == 48
    rendered = repr(poll)
    for secret in (
        "CONTEXT_TOKEN_SECRET",
        "CURSOR_SECRET",
        "IMAGE_PARAM_SECRET",
        "IMAGE_MEDIA_KEY_SECRET",
        "native transcript secret",
        FIXED_KEY_HEX,
    ):
        assert secret not in rendered


async def test_actual_adapter_delivers_all_inbound_media_and_restart_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "temporary-media"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    before = set(scratch.iterdir())
    image_data = b"\x89PNG\r\n\x1a\nfixed-image"
    voice_data = b"#!SILK_V3\x02fixed-voice"
    file_data = b"fixed file contents\n"
    video_data = b"\x00\x00\x00\x18ftypmp42fixed-video"
    encrypted = {
        "/image": encrypt_aes_ecb(image_data, FIXED_KEY),
        "/voice": encrypt_aes_ecb(voice_data, FIXED_KEY),
        "/file": encrypt_aes_ecb(file_data, FIXED_KEY),
        "/video": encrypt_aes_ecb(video_data, FIXED_KEY),
    }
    downloads: list[str] = []

    def cdn_handler(request: httpx.Request) -> httpx.Response:
        downloads.append(request.url.path)
        suffix = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=encrypted[f"/{suffix}"])

    items = (
        WeChatMessageItem(type=MESSAGE_ITEM_TYPE_TEXT, text="media caption"),
        WeChatMessageItem(
            type=MESSAGE_ITEM_TYPE_IMAGE,
            image=WeChatImageItem(
                media=_reference("image"),
                aes_key_hex=FIXED_KEY_HEX,
                mid_size=len(encrypted["/image"]),
            ),
        ),
        WeChatMessageItem(
            type=MESSAGE_ITEM_TYPE_VOICE,
            voice=WeChatVoiceItem(
                media=_reference("voice", key=_hex_key_base64()),
                encode_type=6,
                sample_rate=16000,
                playtime=700,
                text="native words",
            ),
        ),
        WeChatMessageItem(
            type=MESSAGE_ITEM_TYPE_FILE,
            file=WeChatFileItem(
                media=_reference("file", key=_raw_key_base64()),
                file_name="notes.txt",
                md5=hashlib.md5(file_data, usedforsecurity=False).hexdigest(),
                length=str(len(file_data)),
            ),
        ),
        WeChatMessageItem(
            type=MESSAGE_ITEM_TYPE_VIDEO,
            video=WeChatVideoItem(
                media=_reference("video", key=_raw_key_base64()),
                video_size=len(encrypted["/video"]),
                video_md5=hashlib.md5(video_data, usedforsecurity=False).hexdigest(),
            ),
        ),
    )
    event = _event(items)
    result = WeChatPollResult(messages=(event,), get_updates_buf="MEDIA_CURSOR")
    storage = WeChatStorage(tmp_path / "wechat")
    inbound: list[InboundMessage] = []
    api = RuntimeAPI()
    async with httpx.AsyncClient(transport=httpx.MockTransport(cdn_handler)) as client:
        media = WeChatMediaClient(api, client, max_bytes=1024)
        adapter = WeChatAdapter(
            "bot-one@im.bot",
            frozenset({"user-one"}),
            api=api,
            media=media,
            storage=storage,
        )

        async def on_message(_adapter: WeChatAdapter, message: InboundMessage) -> None:
            inbound.append(message)

        await adapter.start(on_message, _forbidden_interaction)
        await api.poll_started.wait()
        await adapter.handle_poll_result(result)
        await adapter.stop()

    assert len(inbound) == 1
    message = inbound[0]
    assert message.text == "media caption"
    assert message.images == (InboundImage(image_data, "image/png", "image.png"),)
    assert message.audios == (
        InboundAudio(voice_data, "audio/silk", "voice.silk", "native words"),
    )
    assert message.files == (InboundFile(file_data, "notes.txt", "text/plain"),)
    assert message.videos == (InboundVideo(video_data, "video/mp4", "video.mp4"),)
    assert storage.load_runtime_state().get_updates_buf == "MEDIA_CURSOR"
    assert set(scratch.iterdir()) == before
    assert api.closed

    second_api = RuntimeAPI()

    def forbidden_download(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("committed media was downloaded again")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(forbidden_download)
    ) as second_client:
        second = WeChatAdapter(
            "bot-one@im.bot",
            frozenset({"user-one"}),
            api=second_api,
            media=WeChatMediaClient(second_api, second_client, max_bytes=1024),
            storage=storage,
        )
        await second.start(on_message, _forbidden_interaction)
        await second_api.poll_started.wait()
        await second.handle_poll_result(result)
        await second.stop()

    assert len(inbound) == 1
    assert len(downloads) == 4


async def _forbidden_interaction(*_args: Any) -> None:
    raise AssertionError("unexpected interaction")


async def test_native_voice_transcript_survives_transient_download_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("URL_SECRET", request=request)

    api = UploadAPI(WeChatUploadTarget(upload_param="unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = WeChatMediaClient(api, client, max_bytes=1024)
        inbound = await media.download_item(
            WeChatMessageItem(
                type=MESSAGE_ITEM_TYPE_VOICE,
                voice=WeChatVoiceItem(
                    media=_reference("voice", key=_raw_key_base64()),
                    encode_type=6,
                    text="native transcript",
                ),
            )
        )

    assert inbound == InboundAudio(b"", "audio/silk", "voice.silk", "native transcript")


class ClosingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_download_bound_is_enforced_during_stream_and_response_closes() -> None:
    stream = ClosingStream((b"12345", b"6789"))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    api = UploadAPI(WeChatUploadTarget(upload_param="unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = WeChatMediaClient(api, client, max_bytes=8)
        with pytest.raises(WeChatMediaTooLarge, match="size limit"):
            await media.download_item(
                WeChatMessageItem(
                    type=MESSAGE_ITEM_TYPE_IMAGE,
                    image=WeChatImageItem(media=_reference("plain")),
                )
            )

    assert stream.closed


async def test_download_content_length_bound_fails_before_body_read() -> None:
    stream = ClosingStream((b"body-must-not-be-read",))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "9"},
            stream=stream,
        )

    api = UploadAPI(WeChatUploadTarget(upload_param="unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = WeChatMediaClient(api, client, max_bytes=8)
        with pytest.raises(WeChatMediaTooLarge, match="size limit"):
            await media.download_item(
                WeChatMessageItem(
                    type=MESSAGE_ITEM_TYPE_IMAGE,
                    image=WeChatImageItem(media=_reference("plain")),
                )
            )

    assert stream.closed


async def test_encrypted_image_size_hint_need_not_equal_ciphertext_length() -> None:
    plaintext = b"\x89PNG\r\n\x1a\nlive-size-hint"
    ciphertext = encrypt_aes_ecb(plaintext, FIXED_KEY)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ciphertext)

    api = UploadAPI(WeChatUploadTarget(upload_param="unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = WeChatMediaClient(api, client, max_bytes=1024)
        inbound = await media.download_item(
            WeChatMessageItem(
                type=MESSAGE_ITEM_TYPE_IMAGE,
                image=WeChatImageItem(
                    media=_reference("image", key=_raw_key_base64()),
                    mid_size=len(plaintext),
                ),
            )
        )

    assert inbound == InboundImage(plaintext, "image/png", "image.png")


async def test_declared_and_outbound_bounds_fail_before_transfer() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"unused")

    api = UploadAPI(WeChatUploadTarget(upload_param="UPLOAD_SECRET"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = WeChatMediaClient(api, client, max_bytes=8)
        with pytest.raises(WeChatMediaTooLarge, match="size limit"):
            await media.download_item(
                WeChatMessageItem(
                    type=MESSAGE_ITEM_TYPE_IMAGE,
                    image=WeChatImageItem(
                        media=_reference("image", key=_raw_key_base64()),
                        mid_size=17,
                    ),
                )
            )
        with pytest.raises(WeChatMediaTooLarge, match="size limit"):
            await media.upload_file(
                OutboundFile("large.bin", b"123456789", "application/octet-stream"),
                to_user_id="user-one",
            )

    assert requests == []
    assert api.requests == []


@pytest.mark.parametrize(
    "file",
    [
        OutboundFile("../photo.png", b"x", "image/png"),
        OutboundFile("photo.png", b"x", "not a mime"),
        OutboundFile("clip.mp4", b"x", "image/png"),
        OutboundFile("notes.txt", b"x", "image/png"),
        OutboundFile("photo.png", b"x", "video/mp4"),
    ],
)
def test_outbound_classification_rejects_malformed_names_and_mime(
    file: OutboundFile,
) -> None:
    with pytest.raises(WeChatMediaError):
        classify_outbound_file(file)


def test_outbound_audio_classifies_as_generic_file() -> None:
    classification = classify_outbound_file(
        OutboundFile("recording.mp3", b"audio", "audio/mpeg")
    )
    assert classification.message_item_type == MESSAGE_ITEM_TYPE_FILE
    assert classification.upload_media_type == 3


@pytest.mark.parametrize(
    "url",
    [
        "http://novac2c.cdn.weixin.qq.com/c2c/download?secret=one",
        "https://evil.example/download?secret=two",
        "https://user:pass@novac2c.cdn.weixin.qq.com/download",
    ],
)
async def test_cdn_download_rejects_unsafe_hosts_before_request(url: str) -> None:
    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsafe URL reached transport")

    api = UploadAPI(WeChatUploadTarget(upload_param="unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        media = WeChatMediaClient(api, client, max_bytes=1024)
        with pytest.raises(WeChatMediaError, match="URL"):
            await media.download_item(
                WeChatMessageItem(
                    type=MESSAGE_ITEM_TYPE_IMAGE,
                    image=WeChatImageItem(
                        media=WeChatCDNMedia(full_url=url),
                    ),
                )
            )


async def test_cdn_redirect_and_declared_integrity_fail_closed() -> None:
    plaintext = b"file contents"
    ciphertext = encrypt_aes_ecb(plaintext, FIXED_KEY)
    responses = iter(
        (
            httpx.Response(302, headers={"location": f"{CDN_ORIGIN}/other"}),
            httpx.Response(200, content=ciphertext),
            httpx.Response(200, content=ciphertext),
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    api = UploadAPI(WeChatUploadTarget(upload_param="unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = WeChatMediaClient(api, client, max_bytes=1024)
        item = WeChatMessageItem(
            type=MESSAGE_ITEM_TYPE_FILE,
            file=WeChatFileItem(
                media=_reference("file", key=_raw_key_base64()),
                file_name="notes.txt",
                length=str(len(plaintext)),
                md5=hashlib.md5(plaintext, usedforsecurity=False).hexdigest(),
            ),
        )
        with pytest.raises(WeChatMediaError, match="redirect"):
            await media.download_item(item)
        with pytest.raises(WeChatMediaError, match="length"):
            await media.download_item(
                WeChatMessageItem(
                    type=MESSAGE_ITEM_TYPE_FILE,
                    file=WeChatFileItem(
                        media=_reference("file", key=_raw_key_base64()),
                        file_name="notes.txt",
                        length="999",
                    ),
                )
            )
        with pytest.raises(WeChatMediaError, match="MD5"):
            await media.download_item(
                WeChatMessageItem(
                    type=MESSAGE_ITEM_TYPE_FILE,
                    file=WeChatFileItem(
                        media=_reference("file", key=_raw_key_base64()),
                        file_name="notes.txt",
                        length=str(len(plaintext)),
                        md5="0" * 32,
                    ),
                )
            )


async def test_actual_outbound_pipeline_uses_native_items_and_audio_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimi_bridge.platforms.wechat import media as media_module

    monkeypatch.setattr(media_module.secrets, "token_bytes", lambda _size: FIXED_KEY)
    requests: list[httpx.Request] = []
    upload_number = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_number
        requests.append(request)
        if request.url.path.endswith("/getuploadurl"):
            return httpx.Response(
                200,
                json={"ret": 0, "upload_param": "UPLOAD_PARAM_SECRET"},
            )
        if request.url.path.endswith("/upload"):
            upload_number += 1
            return httpx.Response(
                200,
                headers={"x-encrypted-param": f"DOWNLOAD_PARAM_SECRET_{upload_number}"},
            )
        if request.url.path.endswith("/sendmessage"):
            return httpx.Response(200, json={"ret": 0})
        raise AssertionError(f"unexpected path: {request.url.path}")

    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_runtime_state(
        WeChatRuntimeState(
            context_tokens={("bot-one@im.bot", "user-one"): "CONTEXT_TOKEN_SECRET"}
        )
    )
    files = (
        OutboundFile("photo.png", b"fixed image", "image/png"),
        OutboundFile("clip.mp4", b"fixed video", "video/mp4"),
        OutboundFile("notes.txt", b"fixed file", "text/plain"),
        OutboundFile("recording.mp3", b"fixed audio", "audio/mpeg"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        adapter = WeChatAdapter(
            "bot-one@im.bot",
            frozenset({"user-one"}),
            api=api,
            media=WeChatMediaClient(api, client, max_bytes=1024),
            storage=storage,
        )
        conversation = ConversationRef("wechat", "bot-one@im.bot", "user-one")
        references = [await adapter.send_file(conversation, file) for file in files]
        await adapter.stop()

    assert all(reference.conversation == conversation for reference in references)
    get_upload_requests = [
        request for request in requests if request.url.path.endswith("/getuploadurl")
    ]
    uploads = [request for request in requests if request.url.path.endswith("/upload")]
    sends = [
        request for request in requests if request.url.path.endswith("/sendmessage")
    ]
    assert [
        json.loads(request.content)["media_type"] for request in get_upload_requests
    ] == [
        1,
        2,
        3,
        3,
    ]
    for request, file in zip(get_upload_requests, files, strict=True):
        body = json.loads(request.content)
        assert body["rawsize"] == len(file.data)
        assert (
            body["rawfilemd5"]
            == hashlib.md5(file.data, usedforsecurity=False).hexdigest()
        )
        assert body["filesize"] == aes_ecb_padded_size(len(file.data))
        assert body["no_need_thumb"] is True
        assert body["aeskey"] == FIXED_KEY_HEX
        assert not any(key.startswith("thumb_") for key in body)
    assert [request.method for request in uploads] == ["POST"] * 4
    assert [request.content for request in uploads] == [
        encrypt_aes_ecb(file.data, FIXED_KEY) for file in files
    ]
    sent_items = [
        json.loads(request.content)["msg"]["item_list"][0] for request in sends
    ]
    assert [item["type"] for item in sent_items] == [2, 5, 4, 4]
    expected_encoded_key = base64.b64encode(FIXED_KEY_HEX.encode("ascii")).decode(
        "ascii"
    )
    media_objects = (
        sent_items[0]["image_item"]["media"],
        sent_items[1]["video_item"]["media"],
        sent_items[2]["file_item"]["media"],
        sent_items[3]["file_item"]["media"],
    )
    assert all(media["aes_key"] == expected_encoded_key for media in media_objects)
    assert sent_items[0]["image_item"]["mid_size"] == aes_ecb_padded_size(
        len(files[0].data)
    )
    assert sent_items[1]["video_item"]["video_size"] == aes_ecb_padded_size(
        len(files[1].data)
    )
    assert sent_items[2]["file_item"]["len"] == str(len(files[2].data))
    assert sent_items[3]["file_item"]["file_name"] == "recording.mp3"


async def test_cdn_upload_retries_only_transient_failures_and_redacts(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimi_bridge.platforms.wechat import media as media_module

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(media_module.secrets, "token_bytes", lambda _size: FIXED_KEY)
    attempts = 0
    signed_url = f"{CDN_ORIGIN}/c2c/upload?encrypted_query_param=UPLOAD_URL_SECRET"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="SERVER_BODY_SECRET")

    api = UploadAPI(WeChatUploadTarget(upload_full_url=signed_url))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = WeChatMediaClient(api, client, max_bytes=1024, upload_attempts=3)
        with pytest.raises(WeChatRetryableError, match="HTTP 503") as caught:
            await media.upload_file(
                OutboundFile("notes.txt", b"payload", "text/plain"),
                to_user_id="user-one",
            )

    assert attempts == 3
    rendered = caplog.text + str(caught.value)
    for secret in (
        "UPLOAD_URL_SECRET",
        "SERVER_BODY_SECRET",
        FIXED_KEY_HEX,
    ):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200), "download parameter"),
        (httpx.Response(400, text="CLIENT_BODY_SECRET"), "HTTP 400"),
        (
            httpx.Response(
                302,
                headers={"location": f"{CDN_ORIGIN}/c2c/other?secret=one"},
            ),
            "redirect",
        ),
    ],
)
async def test_cdn_upload_malformed_responses_are_not_retried(
    response: httpx.Response,
    message: str,
) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return response

    api = UploadAPI(WeChatUploadTarget(upload_param="UPLOAD_PARAM_SECRET"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = WeChatMediaClient(api, client, max_bytes=1024, upload_attempts=3)
        with pytest.raises(WeChatMediaError, match=message) as caught:
            await media.upload_file(
                OutboundFile("notes.txt", b"payload", "text/plain"),
                to_user_id="user-one",
            )

    assert attempts == 1
    assert "CLIENT_BODY_SECRET" not in str(caught.value)
    assert "UPLOAD_PARAM_SECRET" not in str(caught.value)


async def test_get_upload_url_rejects_missing_target_without_exposing_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ret": 0, "errmsg": "GET_UPLOAD_BODY_SECRET"},
        )

    request = WeChatUploadRequest(
        file_key="FILE_KEY_SECRET",
        media_type=3,
        to_user_id="user-one",
        raw_size=7,
        raw_file_md5="321c3cf486ed509164edec1e1981fec8",
        ciphertext_size=16,
        aes_key_hex=FIXED_KEY_HEX,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WeChatProtocolError, match="upload target") as caught:
            await WeChatAPI(_credential(), client).get_upload_url(request)

    assert "GET_UPLOAD_BODY_SECRET" not in str(caught.value)
    assert "FILE_KEY_SECRET" not in repr(request)
    assert FIXED_KEY_HEX not in repr(request)


async def test_final_send_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimi_bridge.platforms.wechat import media as media_module

    monkeypatch.setattr(media_module.secrets, "token_bytes", lambda _size: FIXED_KEY)
    calls = {"getuploadurl": 0, "upload": 0, "sendmessage": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        calls[endpoint] += 1
        if endpoint == "getuploadurl":
            return httpx.Response(200, json={"ret": 0, "upload_param": "PARAM"})
        if endpoint == "upload":
            return httpx.Response(200, headers={"x-encrypted-param": "DOWNLOAD"})
        raise httpx.ReadTimeout("UNCERTAIN_SEND_SECRET", request=request)

    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_runtime_state(
        WeChatRuntimeState(context_tokens={("bot-one@im.bot", "user-one"): "CONTEXT"})
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        adapter = WeChatAdapter(
            "bot-one@im.bot",
            frozenset({"user-one"}),
            api=api,
            media=WeChatMediaClient(api, client, max_bytes=1024),
            storage=storage,
        )
        with pytest.raises(WeChatRetryableError, match="sendMessage timeout"):
            await adapter.send_file(
                ConversationRef("wechat", "bot-one@im.bot", "user-one"),
                OutboundFile("notes.txt", b"payload", "text/plain"),
            )
        await adapter.stop()

    assert calls == {"getuploadurl": 1, "upload": 1, "sendmessage": 1}
