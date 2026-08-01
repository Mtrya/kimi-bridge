"""Platform-neutral speech transcription against a configured HTTP API."""

from __future__ import annotations

import base64
import logging
from pathlib import PurePath
from typing import Literal, Protocol

import httpx

from .platforms.base import InboundAudio


LOGGER = logging.getLogger(__name__)
SPEECH_TIMEOUT_SECONDS = 60.0
SpeechRequestFormat = Literal["multipart", "json"]
_JSON_AUDIO_FORMAT_BY_MEDIA_TYPE = {
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/mp3": "mp3",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/x-m4a": "m4a",
    "audio/x-wav": "wav",
}
_JSON_AUDIO_FORMATS = frozenset(_JSON_AUDIO_FORMAT_BY_MEDIA_TYPE.values())


class SpeechTranscriber(Protocol):
    """Turn inbound audio into text; an empty string means "no result"."""

    async def transcribe(self, audio: InboundAudio) -> str: ...


class HttpSpeechTranscriber:
    """Transcribe audio through a configured multipart or JSON HTTP endpoint.

    ``api_key`` may be empty for local servers that need no Bearer token.
    Any transport, HTTP, or response-shape failure is logged and reported
    as an empty string so callers can fall back to platform transcripts.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        request_format: SpeechRequestFormat = "multipart",
        language: str = "",
        timeout: float = SPEECH_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must be non-empty")
        if not model.strip():
            raise ValueError("model must be non-empty")
        if request_format not in {"multipart", "json"}:
            raise ValueError("request_format must be multipart or json")
        self._base_url = base_url.strip().rstrip("/")
        self._model = model
        self._api_key = api_key
        self._request_format = request_format
        self._language = language
        self._timeout = timeout
        self._client = client

    async def transcribe(self, audio: InboundAudio) -> str:
        if self._client is not None:
            return await self._transcribe_with(self._client, audio)
        async with httpx.AsyncClient() as client:
            return await self._transcribe_with(client, audio)

    async def _transcribe_with(
        self, client: httpx.AsyncClient, audio: InboundAudio
    ) -> str:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request_kwargs: dict[str, object]
        if self._request_format == "multipart":
            fields = {"model": self._model}
            if self._language:
                fields["language"] = self._language
            request_kwargs = {
                "data": fields,
                "files": {"file": (audio.name, audio.data, audio.media_type)},
            }
        else:
            try:
                audio_format = _json_audio_format(audio)
            except ValueError:
                LOGGER.warning(
                    "speech transcription of %r has no supported JSON audio format",
                    audio.name,
                )
                return ""
            payload: dict[str, object] = {
                "model": self._model,
                "input_audio": {
                    "data": base64.b64encode(audio.data).decode("ascii"),
                    "format": audio_format,
                },
            }
            if self._language:
                payload["language"] = self._language
            request_kwargs = {"json": payload}
        try:
            response = await client.post(
                f"{self._base_url}/audio/transcriptions",
                headers=headers,
                timeout=self._timeout,
                **request_kwargs,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning(
                "speech transcription of %r failed (%s)",
                audio.name,
                type(exc).__name__,
            )
            return ""
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            LOGGER.warning(
                "speech transcription of %r returned no text field", audio.name
            )
            return ""
        transcript = text.strip()
        if transcript:
            LOGGER.debug(
                "speech transcription of %r succeeded via configured endpoint",
                audio.name,
            )
        return transcript


def _json_audio_format(audio: InboundAudio) -> str:
    suffix = PurePath(audio.name).suffix.lower().removeprefix(".")
    if suffix in _JSON_AUDIO_FORMATS:
        return suffix
    media_type = audio.media_type.partition(";")[0].strip().lower()
    try:
        return _JSON_AUDIO_FORMAT_BY_MEDIA_TYPE[media_type]
    except KeyError as exc:
        raise ValueError("unsupported JSON audio format") from exc
