from __future__ import annotations

import base64
import json

import httpx
import pytest

from kimi_bridge.platforms.base import InboundAudio
from kimi_bridge.speech import HttpSpeechTranscriber


def _audio() -> InboundAudio:
    return InboundAudio(b"RIFF....", "audio/wav", "voice.wav")


def _transcriber(
    handler: httpx.MockTransport,
    *,
    api_key: str = "sk-test",
    base_url: str = "https://asr.example/v1/",
) -> HttpSpeechTranscriber:
    client = httpx.AsyncClient(transport=handler)
    return HttpSpeechTranscriber(
        base_url=base_url,
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


async def test_transcribe_strips_surrounding_base_url_whitespace() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "local"})

    transcriber = _transcriber(
        httpx.MockTransport(handler),
        base_url="  https://asr.example/v1/  ",
    )

    assert await transcriber.transcribe(_audio()) == "local"
    assert requests[0].url == "https://asr.example/v1/audio/transcriptions"


async def test_transcribe_posts_json_base64_with_language() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "  external words  "})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transcriber = HttpSpeechTranscriber(
        base_url="https://zenmux.example/api/v1",
        model="qwen/qwen3-asr-flash",
        api_key="zen-test",
        request_format="json",
        language="en",
        client=client,
    )

    assert await transcriber.transcribe(_audio()) == "external words"
    request = requests[0]
    assert request.url == "https://zenmux.example/api/v1/audio/transcriptions"
    assert request.headers["Authorization"] == "Bearer zen-test"
    assert request.headers["Content-Type"] == "application/json"
    payload = json.loads(request.content)
    assert payload == {
        "model": "qwen/qwen3-asr-flash",
        "input_audio": {
            "data": base64.b64encode(b"RIFF....").decode("ascii"),
            "format": "wav",
        },
        "language": "en",
    }


async def test_json_audio_format_prefers_opus_filename() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "voice"})

    transcriber = HttpSpeechTranscriber(
        base_url="https://asr.example/v1",
        model="asr-model",
        request_format="json",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    audio = InboundAudio(b"OPUS", "audio/ogg", "voice.opus")

    assert await transcriber.transcribe(audio) == "voice"
    payload = json.loads(requests[0].content)
    assert payload["input_audio"]["format"] == "opus"
    assert "language" not in payload


async def test_unsupported_json_audio_format_makes_no_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "unexpected"})

    transcriber = HttpSpeechTranscriber(
        base_url="https://asr.example/v1",
        model="asr-model",
        request_format="json",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    audio = InboundAudio(b"SILK", "audio/silk", "voice.silk")

    assert await transcriber.transcribe(audio) == ""
    assert requests == []


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
        HttpSpeechTranscriber(base_url=" ", model="whisper-1")
    with pytest.raises(ValueError, match="model"):
        HttpSpeechTranscriber(base_url="https://asr.example/v1", model="")
    with pytest.raises(ValueError, match="request_format"):
        HttpSpeechTranscriber(
            base_url="https://asr.example/v1",
            model="asr-model",
            request_format="xml",  # type: ignore[arg-type]
        )
