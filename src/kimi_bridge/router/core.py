"""Public router facade and inbound-message entry point."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..kimi_server import (
    KimiServerAPIError,
    KimiServerClient,
    KimiServerError,
    PromptContent,
    PromptMedia,
)
from ..platforms.base import InboundFile, InboundMessage, PlatformAdapter
from ..speech import SpeechTranscriber
from ..state import BridgeState, ConversationBinding, StateStore
from .commands import _CommandMixin
from .files import _save_inbound_files
from .formatting import _conversation_key, _title_from_message
from .interactions import _InteractionMixin
from .models import _ActiveStream, _CompactionWaiter, _PendingInteraction
from .rendering import _RenderingMixin
from .sessions import _SessionMixin


# ASR output inevitably contains recognition errors; the prefix marks the
# text as machine-transcribed speech so the agent does not nitpick or
# "correct" transcription mistakes in its reply.
VOICE_TRANSCRIPT_PREFIX = "[语音转写]"
VOICE_UNTRANSCRIBED_NOTICE = (
    "[System: A voice message was received but could not be transcribed.]"
)


class ChatRouter(_CommandMixin, _InteractionMixin, _SessionMixin, _RenderingMixin):
    """Own conversation bindings, bridge commands, and stream rendering."""

    def __init__(
        self,
        client: KimiServerClient,
        *,
        state_store: StateStore,
        default_workspace: str | Path,
        model: str,
        edit_throttle_seconds: float = 1.5,
        first_flush_min_chars: int = 40,
        first_flush_max_delay_seconds: float = 15.0,
        max_output_seconds: float = 300.0,
        interaction_timeout_seconds: float = 600.0,
        inbox_subdir: str = ".kimi-bridge-inbox",
        session_list_limit: int = 5,
        transcriber: SpeechTranscriber | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interaction_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not model:
            raise ValueError("model must be non-empty")
        if edit_throttle_seconds <= 0:
            raise ValueError("edit_throttle_seconds must be positive")
        if first_flush_min_chars < 0:
            raise ValueError("first_flush_min_chars must be non-negative")
        if first_flush_max_delay_seconds < 0:
            raise ValueError("first_flush_max_delay_seconds must be non-negative")
        if max_output_seconds <= 0:
            raise ValueError("max_output_seconds must be positive")
        if interaction_timeout_seconds <= 0:
            raise ValueError("interaction_timeout_seconds must be positive")
        inbox_path = Path(inbox_subdir)
        if not inbox_subdir or inbox_path.is_absolute() or ".." in inbox_path.parts:
            raise ValueError("inbox_subdir must stay inside the session workspace")
        if session_list_limit <= 0:
            raise ValueError("session_list_limit must be positive")
        self._client = client
        self._state_store = state_store
        self._state: BridgeState = state_store.load()
        self._default_workspace = Path(default_workspace).expanduser().resolve()
        self._model = model
        self._edit_throttle_seconds = edit_throttle_seconds
        self._first_flush_min_chars = first_flush_min_chars
        self._first_flush_max_delay_seconds = first_flush_max_delay_seconds
        self._max_output_seconds = max_output_seconds
        self._interaction_timeout_seconds = interaction_timeout_seconds
        self._inbox_subdir = inbox_subdir
        self._session_list_limit = session_list_limit
        self._transcriber = transcriber
        self._sleep = sleep
        self._interaction_sleep = interaction_sleep
        self._poll_sleep = poll_sleep
        self._clock = clock
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._session_choices: dict[str, list[dict[str, Any]]] = {}
        self._verified_session_profiles: set[str] = set()
        self._active: _ActiveStream | None = None
        self._pending: dict[str, _PendingInteraction] = {}
        self._compaction_waiters: dict[str, _CompactionWaiter] = {}
        self._interaction_lock = asyncio.Lock()
        self._interaction_polling_suspended = False

    async def close(self) -> None:
        self._fail_all_compaction_waiters(KimiServerError("kimi event stream stopped"))
        await self._stop_active_stream()
        for pending in tuple(self._pending.values()):
            if pending.timeout_task is not None:
                pending.timeout_task.cancel()
        if self._pending:
            await asyncio.gather(
                *(
                    pending.timeout_task
                    for pending in self._pending.values()
                    if pending.timeout_task is not None
                ),
                return_exceptions=True,
            )
        self._pending.clear()

    async def handle_inbound(
        self, adapter: PlatformAdapter, msg: InboundMessage
    ) -> None:
        text = msg.text.strip()
        if (
            not text
            and not msg.images
            and not msg.videos
            and not msg.files
            and not msg.audios
        ):
            return
        conversation_key = _conversation_key(msg)
        lock = self._conversation_locks.setdefault(conversation_key, asyncio.Lock())
        async with lock:
            self._coerce_binding_capabilities(conversation_key, adapter)
            if (
                text.startswith("/")
                and not msg.images
                and not msg.videos
                and not msg.files
                and not msg.audios
            ):
                try:
                    await self._handle_command(
                        conversation_key,
                        adapter,
                        msg.conversation,
                        msg.actor,
                        text,
                    )
                except KimiServerError as exc:
                    await self._send_chunked(
                        adapter,
                        conversation=msg.conversation,
                        text=f"Command failed: {exc}",
                    )
                return

            binding = self._state.bindings.get(conversation_key)
            if binding is None:
                self._default_workspace.mkdir(parents=True, exist_ok=True)
                binding = await self._create_and_bind(
                    conversation_key,
                    self._default_workspace,
                    _title_from_message(msg),
                    adapter,
                )
            try:
                await self._ensure_active_stream(
                    conversation_key,
                    binding.session_id,
                    adapter,
                    msg.conversation,
                    msg.actor,
                )
                content = await self._build_prompt_content(binding, msg, adapter)
                result = await self._client.submit_prompt(
                    binding.session_id,
                    content,
                    permission_mode=binding.permission_mode,
                )
            except KimiServerError as exc:
                await self._send_chunked(
                    adapter,
                    conversation=msg.conversation,
                    text=f"Prompt failed: {exc}",
                )
                return
            if result.get("status") in {"queued", "blocked"}:
                prompt_id = str(result["prompt_id"])
                try:
                    await self._client.steer_prompts(binding.session_id, [prompt_id])
                except KimiServerAPIError as exc:
                    if exc.code != 40001:
                        raise

    async def _build_prompt_content(
        self,
        binding: ConversationBinding,
        msg: InboundMessage,
        adapter: PlatformAdapter,
    ) -> PromptContent:
        text_parts: list[str] = []
        if msg.text.strip():
            text_parts.append(msg.text.strip())
        # Voice messages resolve their transcript in layers: the configured
        # ASR first, then the platform-native transcript; the failure notice
        # goes into the prompt text, never to the platform as a user-visible
        # message. Audio never goes through the inbox-file path.
        for audio in msg.audios:
            transcript = ""
            if self._transcriber is not None and audio.data:
                transcript = await self._transcriber.transcribe(audio)
            if not transcript:
                transcript = await adapter.transcribe_audio(audio)
            if transcript:
                text_parts.append(f"{VOICE_TRANSCRIPT_PREFIX} {transcript}")
            else:
                text_parts.append(VOICE_UNTRANSCRIBED_NOTICE)
        prompt_media: list[PromptMedia] = []
        inbox_files = list(msg.files)
        if msg.images or msg.videos:
            capabilities = set(
                (await self._client.get_session_model(binding.session_id)).capabilities
            )
            for image in msg.images:
                if "image_in" in capabilities:
                    prompt_media.append(
                        PromptMedia(
                            kind="image",
                            data=image.data,
                            name=image.name,
                            media_type=image.media_type,
                        )
                    )
                else:
                    inbox_files.append(
                        InboundFile(image.data, image.name, image.media_type)
                    )
            for video in msg.videos:
                if "video_in" in capabilities:
                    prompt_media.append(
                        PromptMedia(
                            kind="video",
                            data=video.data,
                            name=video.name,
                            media_type=video.media_type,
                        )
                    )
                else:
                    inbox_files.append(
                        InboundFile(video.data, video.name, video.media_type)
                    )
        if inbox_files:
            saved_paths = _save_inbound_files(
                Path(binding.workspace),
                self._inbox_subdir,
                tuple(inbox_files),
            )
            text_parts.extend(f"Attached file saved at: {path}" for path in saved_paths)
        return PromptContent(
            text="\n\n".join(text_parts) if text_parts else None,
            media=tuple(prompt_media),
        )
