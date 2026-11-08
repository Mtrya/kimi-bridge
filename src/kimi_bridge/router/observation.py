"""Session-labelled, one-attempt notices and independent side answers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..kimi_server import ActivityUpdate, KimiServerOperationError
from ..platforms.base import ActorRef, ConversationRef, PlatformAdapter
from .formatting import _persisted_assistant_text
from .models import _ActiveStream


LOGGER = logging.getLogger(__name__)


class _ObservationMixin:
    async def _session_has_work(self, active: _ActiveStream) -> bool:
        status = await self._client.get_session_status(active.session_id)
        if status.busy or active.interactions:
            return True
        activity = active.activity
        if activity is None:
            activity = await self._client.inspect_activity(active.session_id)
        return (
            any(agent.status == "running" for agent in activity.agents)
            or any(task.status == "running" for task in activity.tasks)
            or (activity.goal is not None and activity.goal.status == "active")
        )

    async def _notice_once(self, active: _ActiveStream, text: str) -> None:
        try:
            session = await self._client.get_session(active.session_id)
            title = str(session.get("title") or active.session_id)
            await active.adapter.send_notice_text(
                active.conversation, f"[{title} · {active.session_id}] {text}"
            )
        except Exception:
            LOGGER.exception(
                "background notice failed; dropping it for session %s",
                active.session_id,
            )

    async def _observe_activity(
        self, active: _ActiveStream, update: ActivityUpdate
    ) -> bool:
        current, previous = update.activity, active.activity
        active.activity = current
        if not update.baseline and previous is not None:
            if (
                current.goal is not None
                and previous.goal is not None
                and current.goal.status in {"complete", "blocked"}
                and current.goal.status != previous.goal.status
            ):
                await self._notice_once(
                    active, f"Goal {current.goal.status}: {current.goal.objective}"
                )
            prior = {(task.owner_agent_id, task.id): task for task in previous.tasks}
            for task in current.tasks:
                old = prior.get((task.owner_agent_id, task.id))
                if (
                    old is not None
                    and old.status == "running"
                    and task.status != "running"
                    and (old.detached or old.kind == "subagent")
                ):
                    await self._notice_once(
                        active,
                        f"{task.kind} {task.status}: {task.description or task.id}",
                    )
        # Empty snapshots never imply a missed task outcome.
        if (
            not active.foreground
            and not active.interactions
            and not any(agent.status == "running" for agent in current.agents)
            and not any(task.status == "running" for task in current.tasks)
            and (current.goal is None or current.goal.status != "active")
        ):
            self._state.observations.pop(active.session_id, None)
            self._observers.pop(active.session_id, None)
            self._state_store.save(self._state)
            return True
        return False

    async def _restore_observations(self, adapter: PlatformAdapter) -> None:
        for session_id, route in tuple(self._state.observations.items()):
            if session_id in self._observers:
                continue
            delivery = route.delivery
            conversation = ConversationRef(
                delivery.platform, delivery.bot_id, delivery.conversation_id
            )
            actor = ActorRef(delivery.actor_id, aliases=delivery.actor_aliases)
            if not adapter.restore_conversation(conversation, actor):
                LOGGER.warning(
                    "saved observation is no longer allowed for session %s", session_id
                )
                continue
            try:
                if await self._client.is_session_archived(session_id):
                    self._state.observations.pop(session_id, None)
                    self._state_store.save(self._state)
                    continue
                if not adapter.supports_interactions:
                    await self._client.update_profile(
                        session_id, permission_mode="auto"
                    )
                binding = self._state.bindings.get(route.conversation_key)
                foreground = binding is not None and binding.session_id == session_id
                active = _ActiveStream(
                    route.conversation_key,
                    session_id,
                    adapter,
                    conversation,
                    actor,
                    foreground=foreground,
                )
                self._observers[session_id] = active
                active.task = asyncio.create_task(
                    self._consume_events(active), name=f"kimi-events-{session_id}"
                )
                active.task.add_done_callback(self._stream_done)
            except KimiServerOperationError:
                LOGGER.warning(
                    "could not restore observation for session %s",
                    session_id,
                    exc_info=True,
                )

    async def _cancel_side_question(self, conversation_key: str) -> None:
        side = self._side_questions.pop(conversation_key, None)
        if side is not None:
            _, task = side
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _handle_btw(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
        question: str,
    ) -> None:
        if not question:
            await self._send_chunked(adapter, conversation, "Usage: /btw <question>")
            return
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        await self._ensure_active_stream(
            conversation_key, binding.session_id, adapter, conversation, actor
        )
        await self._cancel_side_question(conversation_key)
        task = asyncio.create_task(
            self._answer_side_question(
                binding.session_id, adapter, conversation, question
            ),
            name=f"kimi-side-question-{binding.session_id}",
        )
        self._side_questions[conversation_key] = (binding.session_id, task)

        def done(completed: asyncio.Task[None]) -> None:
            if self._side_questions.get(conversation_key) == (
                binding.session_id,
                completed,
            ):
                self._side_questions.pop(conversation_key)
            self._command_done(completed)

        task.add_done_callback(done)

    async def _answer_side_question(
        self,
        session_id: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        question: str,
    ) -> None:
        stream = self._client.side_question(session_id, question)
        try:
            async for event in stream:
                payload: dict[str, Any] = event.get("payload", {})
                if payload.get("type") != "turn.ended":
                    continue
                snapshot = event.get("snapshot", {})
                text = _persisted_assistant_text(snapshot) or "No answer text."
                if payload.get("reason") != "completed":
                    text += f"\nSide question {payload.get('reason', 'ended')}."
                await self._send_chunked(
                    adapter, conversation, f"Side question\n\n{text}"
                )
        except KimiServerOperationError as exc:
            await self._report_operation_error(
                adapter, conversation, exc, operation="Side question"
            )
        finally:
            await stream.aclose()
