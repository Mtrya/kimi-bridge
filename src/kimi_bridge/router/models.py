"""Private runtime state shared by the router components."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from ..interactions import ApprovalRequest, QuestionRequest
from ..platforms.base import (
    ActorRef,
    ConversationRef,
    MessageRef,
    PlatformAdapter,
)


THINKING_LABEL = "Thinking\n\n"


@dataclass(slots=True)
class _RenderState:
    prefix: str = ""
    text: str = ""
    messages: list[MessageRef] = field(default_factory=list)
    rendered_chunks: list[str] = field(default_factory=list)
    turn_active: bool = False
    last_flush: float | None = None
    delayed_flush: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class _ActiveStream:
    conversation_key: str
    session_id: str
    adapter: PlatformAdapter
    conversation: ConversationRef
    actor: ActorRef
    render: _RenderState = field(default_factory=_RenderState)
    thinking: _RenderState = field(
        default_factory=lambda: _RenderState(prefix=THINKING_LABEL)
    )
    step: int | None = None
    task: asyncio.Task[None] | None = None
    interaction_task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _PendingInteraction:
    interaction_id: str
    kind: Literal["approval", "question"]
    request_id: str
    conversation_key: str
    session_id: str
    adapter: PlatformAdapter
    conversation: ConversationRef
    actor: ActorRef
    message: MessageRef
    request: ApprovalRequest | QuestionRequest
    timeout_task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class _CompactionOutcome:
    state: Literal["completed", "blocked", "cancelled"]
    compacted_count: int | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None


@dataclass(slots=True)
class _CompactionWaiter:
    future: asyncio.Future[_CompactionOutcome]
    active_trigger: Literal["manual", "auto"] | None = None
