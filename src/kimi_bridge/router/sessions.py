"""Conversation binding and managed session-stream lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Literal, cast

from ..kimi_server import (
    KimiServerAPIError,
    KimiServerError,
    KimiServerProtocolError,
    SessionStatus,
)
from ..platforms.base import (
    ActorRef,
    ConversationRef,
    PlatformAdapter,
)
from ..state import PERMISSION_MODES, ConversationBinding
from .formatting import _session_recency_key
from .models import _ActiveStream, _CompactionOutcome


LOGGER = logging.getLogger(__name__)
DEFAULT_PERMISSION_MODE = "manual"
SESSION_LIST_LIMIT = 10


class _SessionMixin:
    async def _require_binding(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
    ) -> ConversationBinding | None:
        binding = self._state.bindings.get(conversation_key)
        if binding is None:
            await self._send_chunked(adapter, conversation, "No bound session.")
        return binding

    async def _require_idle(
        self,
        binding: ConversationBinding,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        operation: str,
    ) -> SessionStatus | None:
        status = await self._client.get_session_status(binding.session_id)
        if status.busy:
            await self._send_chunked(
                adapter,
                conversation,
                f"Session is busy. {operation} can only run while idle.",
            )
            return None
        return status

    async def _list_recent_sessions(self) -> list[dict[str, Any]]:
        idle, busy = await asyncio.gather(
            self._client.list_sessions(busy=False, page_size=SESSION_LIST_LIMIT),
            self._client.list_sessions(busy=True, page_size=SESSION_LIST_LIMIT),
        )
        by_id = {str(session["id"]): session for session in [*idle, *busy]}
        sessions = list(by_id.values())
        sessions.sort(key=_session_recency_key, reverse=True)
        return sessions[:SESSION_LIST_LIMIT]

    async def _resolve_new_workspace(self, argument: str) -> Path:
        if not argument:
            self._default_workspace.mkdir(parents=True, exist_ok=True)
            return self._default_workspace
        workspace = Path(argument).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace}")
        return workspace

    async def _create_and_bind(
        self, conversation_key: str, workspace: Path, title: str
    ) -> ConversationBinding:
        session_id = await self._client.create_session(
            str(workspace),
            title=title,
            model=self._model,
            permission_mode=DEFAULT_PERMISSION_MODE,
        )
        current = self._state.bindings.get(conversation_key)
        binding = ConversationBinding(
            session_id=session_id,
            workspace=str(workspace),
            permission_mode=DEFAULT_PERMISSION_MODE,
            render_thinking=(current.render_thinking if current is not None else False),
        )
        self._state.bindings[conversation_key] = binding
        self._state_store.save(self._state)
        return binding

    def _binding_from_session(
        self,
        session: dict[str, Any],
        *,
        render_thinking: bool,
    ) -> ConversationBinding:
        workspace = str(session["metadata"]["cwd"])
        agent_config = session.get("agent_config")
        mode = (
            agent_config.get("permission_mode")
            if isinstance(agent_config, dict)
            else None
        )
        if mode not in PERMISSION_MODES:
            mode = DEFAULT_PERMISSION_MODE
        binding = ConversationBinding(
            session_id=str(session["id"]),
            workspace=workspace,
            permission_mode=mode,
            render_thinking=render_thinking,
        )
        return binding

    async def _resolve_session(
        self, conversation_key: str, selector: str
    ) -> dict[str, Any] | None:
        if selector.isdecimal():
            index = int(selector) - 1
            choices = self._session_choices.get(conversation_key, [])
            if 0 <= index < len(choices):
                return choices[index]
            return None
        try:
            return await self._client.get_session(selector)
        except KimiServerAPIError as exc:
            if exc.code == 40401:
                return None
            raise

    async def _ensure_active_stream(
        self,
        conversation_key: str,
        session_id: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
    ) -> None:
        active = self._active
        if (
            active is not None
            and active.conversation_key == conversation_key
            and active.session_id == session_id
            and active.task is not None
            and not active.task.done()
        ):
            active.adapter = adapter
            active.conversation = conversation
            active.actor = actor
            return

        await self._stop_active_stream()
        active = _ActiveStream(
            conversation_key=conversation_key,
            session_id=session_id,
            adapter=adapter,
            conversation=conversation,
            actor=actor,
        )
        active.task = asyncio.create_task(
            self._consume_events(active),
            name=f"kimi-events-{session_id}",
        )
        stream_task = active.task
        self._active = active
        subscription_wait = asyncio.create_task(
            self._client.wait_until_subscribed(session_id),
            name=f"kimi-subscription-ready-{session_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {stream_task, subscription_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stream_task in done:
                active.task = None
                stream_task.result()
                raise KimiServerError("kimi event stream ended before subscribing")
            await subscription_wait
        except BaseException:
            if not subscription_wait.done():
                subscription_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await subscription_wait
            await self._stop_active_stream()
            raise
        stream_task.add_done_callback(self._stream_done)
        active.interaction_task = asyncio.create_task(
            self._poll_interactions(active),
            name=f"kimi-interactions-{session_id}",
        )
        active.interaction_task.add_done_callback(self._interaction_poll_done)

    async def _stop_active_stream(self) -> None:
        active = self._active
        self._active = None
        if active is None:
            return
        await self._cancel_delayed_flush(active.render)
        await self._cancel_delayed_flush(active.thinking)
        if active.interaction_task is not None:
            active.interaction_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active.interaction_task
        if active.task is not None:
            stream_task = active.task
            active.task = None
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)

    async def _consume_events(self, active: _ActiveStream) -> None:
        failure: KimiServerError | None = None
        try:
            async for event in self._client.subscribe_events(active.session_id):
                if self._active is not active:
                    return
                await self.dispatch_event(active.conversation_key, event)
        except asyncio.CancelledError:
            failure = KimiServerError("kimi event stream stopped")
            raise
        except Exception as exc:
            failure = KimiServerError(f"kimi event stream failed: {exc}")
            raise
        else:
            failure = KimiServerError("kimi event stream ended")
        finally:
            if failure is None:
                failure = KimiServerError("kimi event stream stopped")
            self._fail_compaction_waiter(active.session_id, failure)

    def _dispatch_compaction_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        waiter = self._compaction_waiters.get(session_id)
        if waiter is None or waiter.future.done():
            return
        if event_type == "compaction.started":
            trigger = payload.get("trigger")
            if trigger in {"manual", "auto"}:
                waiter.active_trigger = cast(Literal["manual", "auto"], trigger)
            return
        if event_type not in {
            "compaction.completed",
            "compaction.blocked",
            "compaction.cancelled",
        }:
            return
        if waiter.active_trigger == "auto":
            waiter.active_trigger = None
            return
        if waiter.active_trigger != "manual":
            return

        if event_type == "compaction.completed":
            result = payload.get("result")
            if not isinstance(result, dict):
                waiter.future.set_exception(
                    KimiServerProtocolError("compaction.completed event has no result")
                )
                return
            try:
                outcome = _CompactionOutcome(
                    state="completed",
                    compacted_count=int(result["compactedCount"]),
                    tokens_before=int(result["tokensBefore"]),
                    tokens_after=int(result["tokensAfter"]),
                )
            except (KeyError, TypeError, ValueError):
                waiter.future.set_exception(
                    KimiServerProtocolError(
                        "compaction.completed event has invalid metrics"
                    )
                )
                return
        elif event_type == "compaction.blocked":
            outcome = _CompactionOutcome(state="blocked")
        else:
            outcome = _CompactionOutcome(state="cancelled")
        waiter.future.set_result(outcome)

    def _fail_compaction_waiter(self, session_id: str, error: KimiServerError) -> None:
        waiter = self._compaction_waiters.get(session_id)
        if waiter is not None and not waiter.future.done():
            waiter.future.set_exception(error)

    def _fail_all_compaction_waiters(self, error: KimiServerError) -> None:
        for session_id in tuple(self._compaction_waiters):
            self._fail_compaction_waiter(session_id, error)

    def _stream_done(self, task: asyncio.Task[None]) -> None:
        active = self._active
        if active is not None and active.task is task:
            active.task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("kimi event stream stopped unexpectedly")
