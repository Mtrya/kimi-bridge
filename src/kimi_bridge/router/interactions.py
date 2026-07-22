"""Approval and question discovery, validation, and resolution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Literal

from ..interactions import (
    ApprovalDecision,
    ApprovalPrompt,
    ApprovalRequest,
    ApprovalResponse,
    InteractionOutcome,
    MultipleChoiceAnswer,
    MultipleChoiceWithOtherAnswer,
    OtherAnswer,
    QuestionAnswer,
    QuestionPrompt,
    QuestionRequest,
    QuestionResponse,
    SingleChoiceAnswer,
    SkippedAnswer,
)
from ..kimi_server import KimiServerAPIError
from ..platforms.base import InboundInteraction, PlatformAdapter
from .formatting import _conversation_key
from .models import _ActiveStream, _PendingInteraction


LOGGER = logging.getLogger(__name__)
INTERACTION_POLL_SECONDS = 1.0
TERMINAL_INTERACTION_ERROR_CODES = {40001, 40401, 40404, 40902}
STALE_INTERACTION_TEXT = (
    "This interaction is stale or was already resolved. Run the task again "
    "if you still need it."
)


class _InteractionMixin:
    async def handle_interaction(
        self, adapter: PlatformAdapter, action: InboundInteraction
    ) -> None:
        """Resolve one normalized platform interaction submission."""

        async with self._interaction_lock:
            pending = next(
                (
                    item
                    for item in self._pending.values()
                    if item.message == action.source
                ),
                None,
            )
            if pending is None:
                try:
                    await adapter.finish_interaction(
                        action.source,
                        InteractionOutcome(
                            state="stale",
                            detail=STALE_INTERACTION_TEXT,
                        ),
                    )
                finally:
                    await adapter.send_text(action.conversation, STALE_INTERACTION_TEXT)
                return
            if (
                _conversation_key(action) != pending.conversation_key
                or action.actor.id != pending.actor.id
                or action.conversation != pending.conversation
            ):
                await adapter.send_text(
                    action.conversation,
                    "This interaction belongs to another conversation.",
                )
                return
            action_interaction_id = action.interaction_id
            if (
                action_interaction_id is not None
                and action_interaction_id != pending.interaction_id
            ):
                await adapter.send_text(action.conversation, STALE_INTERACTION_TEXT)
                return

            approval_decision: ApprovalDecision | None = None
            try:
                if pending.kind == "approval":
                    approval_decision = await self._resolve_approval_action(
                        pending, action
                    )
                    outcome = approval_decision.capitalize()
                else:
                    outcome = await self._resolve_question_action(pending, action)
                    if outcome is None:
                        await adapter.send_text(
                            action.conversation,
                            "Choose an option or enter a free-text answer.",
                        )
                        return
            except KimiServerAPIError as exc:
                if exc.code not in TERMINAL_INTERACTION_ERROR_CODES:
                    raise
                outcome = "Already resolved or expired"

            await self._clear_pending(pending)
            await adapter.finish_interaction(
                pending.message,
                InteractionOutcome(
                    state="completed",
                    detail=outcome,
                    approval_decision=approval_decision,
                ),
            )

    async def _cancel_active_work(
        self,
        conversation_key: str,
        session_id: str,
        *,
        detail: str,
    ) -> tuple[bool, bool]:
        async with self._interaction_lock:
            pending = self._pending.get(conversation_key)
            session_ids: list[str] = []
            if pending is not None:
                session_ids.append(pending.session_id)
            if session_id not in session_ids:
                session_ids.append(session_id)
            aborted = False
            for target_session_id in session_ids:
                aborted = await self._client.abort_prompt(target_session_id) or aborted
            if pending is not None:
                await self._clear_pending(pending)
                await pending.adapter.finish_interaction(
                    pending.message,
                    InteractionOutcome(state="cancelled", detail=detail),
                )
            return aborted, pending is not None

    def _interaction_poll_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("kimi interaction polling stopped unexpectedly")

    async def _poll_interactions(self, active: _ActiveStream) -> None:
        while self._active is active:
            try:
                await self._discover_interaction(active)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("failed to poll kimi interactions; retrying")
            await self._poll_sleep(INTERACTION_POLL_SECONDS)

    async def _discover_interaction(self, active: _ActiveStream) -> None:
        async with self._interaction_lock:
            if self._active is not active:
                return
            if active.conversation_key in self._pending:
                return
            if any(
                pending.session_id == active.session_id
                for pending in self._pending.values()
            ):
                return
            approvals, questions = await asyncio.gather(
                self._client.list_approvals(active.session_id),
                self._client.list_questions(active.session_id),
            )
            if approvals:
                kind: Literal["approval", "question"] = "approval"
                request = approvals[0]
                request_id = request.id
            elif questions:
                kind = "question"
                request = questions[0]
                request_id = request.id
            else:
                return

            interaction_id = uuid.uuid4().hex
            session = await self._client.get_session(active.session_id)
            if kind == "approval":
                assert isinstance(request, ApprovalRequest)
                prompt = ApprovalPrompt(
                    interaction_id=interaction_id,
                    request=request,
                    session_title=str(session.get("title") or "Untitled"),
                    workspace=str(session["metadata"]["cwd"]),
                )
            else:
                assert isinstance(request, QuestionRequest)
                prompt = QuestionPrompt(
                    interaction_id=interaction_id,
                    request=request,
                    session_title=str(session.get("title") or "Untitled"),
                    workspace=str(session["metadata"]["cwd"]),
                )
            message = await active.adapter.present_interaction(
                active.conversation, prompt
            )
            pending = _PendingInteraction(
                interaction_id=interaction_id,
                kind=kind,
                request_id=request_id,
                conversation_key=active.conversation_key,
                session_id=active.session_id,
                adapter=active.adapter,
                conversation=active.conversation,
                actor=active.actor,
                message=message,
                request=request,
            )
            self._pending[active.conversation_key] = pending
            pending.timeout_task = asyncio.create_task(
                self._expire_interaction(pending),
                name=f"interaction-timeout-{request_id}",
            )

    async def _expire_interaction(self, pending: _PendingInteraction) -> None:
        await self._interaction_sleep(self._interaction_timeout_seconds)
        async with self._interaction_lock:
            if self._pending.get(pending.conversation_key) is not pending:
                return
            try:
                if pending.kind == "approval":
                    await self._client.resolve_approval(
                        pending.session_id,
                        pending.request_id,
                        "rejected",
                    )
                    detail = "Timed out and was automatically rejected."
                else:
                    await self._client.dismiss_question(
                        pending.session_id, pending.request_id
                    )
                    detail = "Timed out and was automatically dismissed."
            except KimiServerAPIError as exc:
                if exc.code not in TERMINAL_INTERACTION_ERROR_CODES:
                    raise
                detail = "Expired after it had already been resolved."
            await self._clear_pending(pending)
            await pending.adapter.finish_interaction(
                pending.message,
                InteractionOutcome(state="timed_out", detail=detail),
            )

    async def _clear_pending(self, pending: _PendingInteraction) -> None:
        if self._pending.get(pending.conversation_key) is pending:
            self._pending.pop(pending.conversation_key)
        task = pending.timeout_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _resolve_approval_action(
        self, pending: _PendingInteraction, action: InboundInteraction
    ) -> ApprovalDecision:
        if not isinstance(pending.request, ApprovalRequest):
            raise TypeError("approval interaction has a question request")
        if not isinstance(action.response, ApprovalResponse):
            raise ValueError("approval interaction has an invalid response")
        decision = action.response.decision
        await self._client.resolve_approval(
            pending.session_id,
            pending.request_id,
            decision,
        )
        return decision

    async def _resolve_question_action(
        self, pending: _PendingInteraction, action: InboundInteraction
    ) -> str | None:
        if not isinstance(pending.request, QuestionRequest):
            raise TypeError("question interaction has an approval request")
        if action.response is None:
            return None
        if not isinstance(action.response, QuestionResponse):
            raise ValueError("question interaction has an invalid response")
        answers = _validate_question_answers(pending.request, action.response.answers)
        await self._client.resolve_question(
            pending.session_id,
            pending.request_id,
            answers,
        )
        return "Answer submitted"


def _validate_question_answers(
    request: QuestionRequest,
    answers: tuple[QuestionAnswer, ...],
) -> tuple[QuestionAnswer, ...]:
    questions = {question.id: question for question in request.questions}
    if len({answer.question_id for answer in answers}) != len(answers):
        raise ValueError("question response contains duplicate answers")
    if {answer.question_id for answer in answers} != set(questions):
        raise ValueError("question response must answer or skip every question")

    for answer in answers:
        question = questions[answer.question_id]
        option_ids = {option.id for option in question.options}
        if isinstance(answer, SkippedAnswer):
            continue
        if isinstance(answer, SingleChoiceAnswer):
            if question.multi_select or answer.option_id not in option_ids:
                raise ValueError("question response contains an invalid single choice")
            continue
        if isinstance(answer, MultipleChoiceAnswer):
            if (
                not question.multi_select
                or not answer.option_ids
                or any(option_id not in option_ids for option_id in answer.option_ids)
            ):
                raise ValueError("question response contains invalid multiple choices")
            continue
        if isinstance(answer, OtherAnswer):
            if question.multi_select or not question.allow_other or not answer.text:
                raise ValueError("question response contains invalid free text")
            continue
        if isinstance(answer, MultipleChoiceWithOtherAnswer):
            if (
                not question.multi_select
                or not question.allow_other
                or not answer.text
                or any(option_id not in option_ids for option_id in answer.option_ids)
            ):
                raise ValueError(
                    "question response contains invalid choices with free text"
                )
            continue
        raise TypeError(f"unsupported question answer: {type(answer).__name__}")
    return answers
