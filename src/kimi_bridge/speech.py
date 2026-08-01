"""Platform-neutral speech transcription against a Whisper-compatible API."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from .platforms.base import InboundAudio


LOGGER = logging.getLogger(__name__)
SPEECH_TIMEOUT_SECONDS = 60.0


class SpeechTranscriber(Protocol):
    """Turn inbound audio into text; an empty string means "no result"."""

    async def transcribe(self, audio: InboundAudio) -> str: ...


class WhisperTranscriber:
    """Transcribe audio through an OpenAI/Whisper-compatible HTTP endpoint.

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
        timeout: float = SPEECH_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must be non-empty")
        if not model.strip():
            raise ValueError("model must be non-empty")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
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
        try:
            response = await client.post(
                f"{self._base_url}/audio/transcriptions",
                headers=headers,
                data={"model": self._model},
                files={"file": (audio.name, audio.data, audio.media_type)},
                timeout=self._timeout,
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
        return text.strip()
