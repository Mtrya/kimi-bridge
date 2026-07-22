"""Public router facade and inbound-message entry point."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..kimi_server import (
    KimiServerAPIError,
    KimiServerClient,
    KimiServerError,
)
from ..platforms.base import InboundMessage, PlatformAdapter
from ..state import BridgeState, ConversationBinding, StateStore
from .commands import _CommandMixin
from .files import _save_inbound_files
from .formatting import _conversation_key, _title_from_message
from .interactions import _InteractionMixin
from .models import _ActiveStream, _CompactionWaiter, _PendingInteraction
from .rendering import _RenderingMixin
from .sessions import _SessionMixin


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
        interaction_timeout_seconds: float = 600.0,
        inbox_subdir: str = ".kimi-bridge-inbox",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interaction_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not model:
            raise ValueError("model must be non-empty")
        if edit_throttle_seconds <= 0:
            raise ValueError("edit_throttle_seconds must be positive")
        if interaction_timeout_seconds <= 0:
            raise ValueError("interaction_timeout_seconds must be positive")
        inbox_path = Path(inbox_subdir)
        if not inbox_subdir or inbox_path.is_absolute() or ".." in inbox_path.parts:
            raise ValueError("inbox_subdir must stay inside the session workspace")
        self._client = client
        self._state_store = state_store
        self._state: BridgeState = state_store.load()
        self._default_workspace = Path(default_workspace).expanduser().resolve()
        self._model = model
        self._edit_throttle_seconds = edit_throttle_seconds
        self._interaction_timeout_seconds = interaction_timeout_seconds
        self._inbox_subdir = inbox_subdir
        self._sleep = sleep
        self._interaction_sleep = interaction_sleep
        self._poll_sleep = poll_sleep
        self._clock = clock
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._session_choices: dict[str, list[dict[str, Any]]] = {}
        self._active: _ActiveStream | None = None
        self._pending: dict[str, _PendingInteraction] = {}
        self._compaction_waiters: dict[str, _CompactionWaiter] = {}
        self._interaction_lock = asyncio.Lock()

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
        if not text and not msg.images and not msg.files:
            return
        conversation_key = _conversation_key(msg)
        lock = self._conversation_locks.setdefault(conversation_key, asyncio.Lock())
        async with lock:
            if text.startswith("/") and not msg.images and not msg.files:
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
                )
            await self._ensure_active_stream(
                conversation_key,
                binding.session_id,
                adapter,
                msg.conversation,
                msg.actor,
            )
            content = await self._build_prompt_content(binding, msg)
            result = await self._client.submit_prompt(
                binding.session_id,
                content,
                permission_mode=binding.permission_mode,
            )
            if result.get("status") in {"queued", "blocked"}:
                prompt_id = str(result["prompt_id"])
                try:
                    await self._client.steer_prompts(binding.session_id, [prompt_id])
                except KimiServerAPIError as exc:
                    if exc.code != 40001:
                        raise

    async def _build_prompt_content(
        self, binding: ConversationBinding, msg: InboundMessage
    ) -> list[dict[str, Any]]:
        text_parts: list[str] = []
        if msg.text.strip():
            text_parts.append(msg.text.strip())
        if msg.files:
            saved_paths = _save_inbound_files(
                Path(binding.workspace),
                self._inbox_subdir,
                msg.files,
            )
            text_parts.extend(f"Attached file saved at: {path}" for path in saved_paths)

        content: list[dict[str, Any]] = []
        if text_parts:
            content.append({"type": "text", "text": "\n\n".join(text_parts)})
        content.extend(
            {
                "type": "image",
                "source": {
                    "kind": "base64",
                    "media_type": image.media_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                },
            }
            for image in msg.images
        )
        return content
