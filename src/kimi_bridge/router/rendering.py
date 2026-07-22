"""Kimi event dispatch and independent answer/thinking rendering."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Literal

from ..platforms.base import ConversationRef, PlatformAdapter
from .formatting import (
    _assistant_text_from_snapshot,
    _chunk_text,
    _thinking_text_from_snapshot,
)
from .models import THINKING_LABEL, _ActiveStream, _RenderState


class _RenderingMixin:
    async def dispatch_event(self, session_key: str, event: dict[str, Any]) -> None:
        """Translate one raw session event into throttled platform output."""

        active = self._active
        if active is None or active.conversation_key != session_key:
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type") or event.get("type")

        if isinstance(event_type, str) and event_type.startswith("compaction."):
            self._dispatch_compaction_event(active.session_id, event_type, payload)
            return

        if event_type == "resync_required":
            snapshot = event.get("snapshot")
            if isinstance(snapshot, dict):
                await self._render_resync_snapshot(active, snapshot)
            return
        if event_type == "turn.started":
            await self._reset_render(active)
            return
        if event_type == "turn.step.started":
            step = payload.get("step")
            if not isinstance(step, int) or isinstance(step, bool):
                return
            if active.step is not None and step != active.step:
                await self._advance_render_step(active)
            elif (
                active.step is None
                and step > 1
                and (active.render.text or active.thinking.text)
            ):
                await self._advance_render_step(active)
            active.step = step
            return
        if event_type == "assistant.delta":
            delta = payload.get("delta")
            if not isinstance(delta, str):
                return
            await self._apply_delta(
                active,
                active.render,
                "assistant_text",
                event.get("offset"),
                delta,
            )
            await self._maybe_flush(active, active.render)
            return
        if event_type == "thinking.delta":
            if not self._thinking_enabled(active):
                return
            delta = payload.get("delta")
            if not isinstance(delta, str):
                return
            await self._apply_delta(
                active,
                active.thinking,
                "thinking_text",
                event.get("offset"),
                delta,
            )
            await self._maybe_flush(active, active.thinking)
            return
        if event_type == "turn.ended":
            snapshot = await self._client.get_snapshot(active.session_id)
            answer_text = _assistant_text_from_snapshot(snapshot)
            if answer_text is not None:
                active.render.text = answer_text
            await self._flush(active, active.render)
            active.render.turn_active = False
            if self._thinking_enabled(active):
                thinking_text = _thinking_text_from_snapshot(snapshot)
                if thinking_text is not None:
                    active.thinking.text = thinking_text
                await self._flush(active, active.thinking)
            active.thinking.turn_active = False

    async def _reset_render(self, active: _ActiveStream) -> None:
        await self._cancel_delayed_flush(active.render)
        await self._cancel_delayed_flush(active.thinking)
        active.step = None
        active.render = _RenderState(turn_active=True)
        active.thinking = _RenderState(
            prefix=THINKING_LABEL,
            turn_active=True,
        )

    async def _advance_render_step(self, active: _ActiveStream) -> None:
        await self._cancel_delayed_flush(active.render)
        await self._flush(active, active.render)
        active.render.turn_active = False

        await self._cancel_delayed_flush(active.thinking)
        if self._thinking_enabled(active):
            await self._flush(active, active.thinking)
        active.thinking.turn_active = False

        active.render = _RenderState(turn_active=True)
        active.thinking = _RenderState(
            prefix=THINKING_LABEL,
            turn_active=True,
        )

    async def _cancel_delayed_flush(self, render: _RenderState) -> None:
        delayed = render.delayed_flush
        if delayed is None:
            return
        delayed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await delayed
        render.delayed_flush = None

    async def _apply_delta(
        self,
        active: _ActiveStream,
        render: _RenderState,
        snapshot_field: Literal["assistant_text", "thinking_text"],
        offset: Any,
        delta: str,
    ) -> None:
        render.turn_active = True
        if isinstance(offset, int) and not isinstance(offset, bool):
            if render.text[offset : offset + len(delta)] == delta:
                return
            if offset != len(render.text):
                snapshot_text = await self._snapshot_stream_text(
                    active.session_id, snapshot_field
                )
                if snapshot_text is not None:
                    render.text = snapshot_text
                    if render.text[offset : offset + len(delta)] == delta:
                        return
            if offset <= len(render.text):
                render.text = render.text[:offset] + delta
                return
        render.text += delta

    async def _maybe_flush(self, active: _ActiveStream, render: _RenderState) -> None:
        now = self._clock()
        if not render.messages or render.last_flush is None:
            await self._flush(active, render)
            return
        elapsed = now - render.last_flush
        if elapsed >= self._edit_throttle_seconds:
            await self._flush(active, render)
            return
        if render.delayed_flush is None or render.delayed_flush.done():
            render.delayed_flush = asyncio.create_task(
                self._flush_after(
                    active,
                    render,
                    self._edit_throttle_seconds - elapsed,
                ),
                name=(
                    "throttled-thinking-edit"
                    if render.prefix
                    else "throttled-message-edit"
                ),
            )

    async def _flush_after(
        self, active: _ActiveStream, render: _RenderState, delay: float
    ) -> None:
        try:
            await self._sleep(delay)
            if self._active is active and (
                active.render is render or active.thinking is render
            ):
                await self._flush(active, render)
        finally:
            if render.delayed_flush is asyncio.current_task():
                render.delayed_flush = None

    async def _flush(self, active: _ActiveStream, render: _RenderState) -> None:
        if not render.text:
            return
        async with render.lock:
            chunks = _chunk_text(
                f"{render.prefix}{render.text}", active.adapter.message_limit
            )
            for index, chunk in enumerate(chunks):
                if index >= len(render.messages):
                    message = await active.adapter.send_text(active.conversation, chunk)
                    render.messages.append(message)
                elif (
                    index >= len(render.rendered_chunks)
                    or render.rendered_chunks[index] != chunk
                ):
                    await active.adapter.edit_text(render.messages[index], chunk)
            render.rendered_chunks = chunks
            render.last_flush = self._clock()

    async def _snapshot_stream_text(
        self,
        session_id: str,
        field: Literal["assistant_text", "thinking_text"],
    ) -> str | None:
        snapshot = await self._client.get_snapshot(session_id)
        if field == "assistant_text":
            return _assistant_text_from_snapshot(snapshot)
        return _thinking_text_from_snapshot(snapshot)

    async def _backfill_thinking(self, active: _ActiveStream | None) -> None:
        if active is None or not self._thinking_enabled(active):
            return
        snapshot = await self._client.get_snapshot(active.session_id)
        in_flight = snapshot.get("in_flight_turn")
        if not isinstance(in_flight, dict):
            return
        text = in_flight.get("thinking_text")
        if not isinstance(text, str):
            return
        active.thinking.turn_active = True
        active.thinking.text = text
        await self._flush(active, active.thinking)

    def _thinking_enabled(self, active: _ActiveStream) -> bool:
        binding = self._state.bindings.get(active.conversation_key)
        return (
            binding is not None
            and binding.session_id == active.session_id
            and binding.render_thinking
        )

    async def _render_resync_snapshot(
        self, active: _ActiveStream, snapshot: dict[str, Any]
    ) -> None:
        in_flight = snapshot.get("in_flight_turn")
        if isinstance(in_flight, dict):
            answer_text = in_flight.get("assistant_text")
            thinking_text = in_flight.get("thinking_text")
            if not isinstance(answer_text, str):
                return
            if not active.render.turn_active:
                await self._reset_render(active)
            active.render.text = answer_text
            await self._flush(active, active.render)
            if self._thinking_enabled(active) and isinstance(thinking_text, str):
                active.thinking.text = thinking_text
                active.thinking.turn_active = True
                await self._flush(active, active.thinking)
            return

        if not active.render.turn_active and not active.thinking.turn_active:
            return
        answer_text = _assistant_text_from_snapshot(snapshot)
        if answer_text is not None:
            active.render.text = answer_text
            await self._flush(active, active.render)
        active.render.turn_active = False
        if self._thinking_enabled(active):
            thinking_text = _thinking_text_from_snapshot(snapshot)
            if thinking_text is not None:
                active.thinking.text = thinking_text
            await self._flush(active, active.thinking)
        active.thinking.turn_active = False

    async def _send_chunked(
        self, adapter: PlatformAdapter, conversation: ConversationRef, text: str
    ) -> None:
        for chunk in _chunk_text(text, adapter.message_limit):
            await adapter.send_text(conversation, chunk)
