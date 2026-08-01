from __future__ import annotations

import httpx
import pytest

from kimi_bridge.platforms.base import InboundAudio
from kimi_bridge.speech import WhisperTranscriber


def _audio() -> InboundAudio:
    return InboundAudio(b"RIFF....", "audio/wav", "voice.wav")


def _transcriber(
    handler: httpx.MockTransport,
    *,
    api_key: str = "sk-test",
) -> WhisperTranscriber:
    client = httpx.AsyncClient(transport=handler)
    return WhisperTranscriber(
        base_url="https://asr.example/v1/",
        model="whisper-1",
        api_key=api_key,
        client=client,
    )


async def test_transcribe_posts_multipart_and_returns_stripped_text() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "  hello there  "})

    transcriber = _transcriber(httpx.MockTransport(handler))

    assert await transcriber.transcribe(_audio()) == "hello there"
    request = requests[0]
    assert request.url == "https://asr.example/v1/audio/transcriptions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = request.content.decode("utf-8", errors="replace")
    assert 'name="model"' in body
    assert "whisper-1" in body
    assert 'name="file"; filename="voice.wav"' in body
    assert "audio/wav" in body
    assert "RIFF...." in body


async def test_transcribe_omits_authorization_without_api_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "local"})

    transcriber = _transcriber(httpx.MockTransport(handler), api_key="")

    assert await transcriber.transcribe(_audio()) == "local"
    assert "Authorization" not in requests[0].headers


async def test_transcribe_empty_text_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "   "})

    transcriber = _transcriber(httpx.MockTransport(handler))

    assert await transcriber.transcribe(_audio()) == ""


async def test_transcribe_http_error_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    transcriber = _transcriber(httpx.MockTransport(handler))

    with caplog.at_level("WARNING", logger="kimi_bridge.speech"):
        assert await transcriber.transcribe(_audio()) == ""
    assert "speech transcription" in caplog.text


async def test_transcribe_transport_error_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    transcriber = _transcriber(httpx.MockTransport(handler))

    assert await transcriber.transcribe(_audio()) == ""


async def test_transcribe_malformed_response_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    transcriber = _transcriber(httpx.MockTransport(handler))
    assert await transcriber.transcribe(_audio()) == ""

    def handler_no_text(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"segments": []})

    transcriber = _transcriber(httpx.MockTransport(handler_no_text))
    assert await transcriber.transcribe(_audio()) == ""


def test_transcriber_requires_base_url_and_model() -> None:
    with pytest.raises(ValueError, match="base_url"):
        WhisperTranscriber(base_url=" ", model="whisper-1")
    with pytest.raises(ValueError, match="model"):
        WhisperTranscriber(base_url="https://asr.example/v1", model="")
