"""Private runtime state shared by the router components."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from ..interactions import ApprovalRequest, QuestionRequest
from ..kimi_server import SessionNotice
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
    # Deferred rendering (edit-less adapters) sends the buffered output in
    # append-only batches; this preserves the exact prefix already delivered.
    emitted_text: str = ""
    turn_id: int | None = None
    prompt_id: str | None = None
    turn_active: bool = False
    last_flush: float | None = None
    # Backstop deadline for the opening flush, armed lazily on the first
    # delta that falls short of first_flush_min_chars so a stalled stream
    # cannot defer the opening chunk forever.
    first_flush_after: float | None = None
    delayed_flush: asyncio.Task[None] | None = None
    edit_counts: dict[MessageRef, int] = field(default_factory=dict)
    exhausted_messages: set[MessageRef] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class _PendingFinalization:
    answer: _RenderState
    thinking: _RenderState
    turn_end_seq: int | None
    notice: SessionNotice | None = None


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
    pending_finalization: _PendingFinalization | None = None
    pending_terminal_notice: SessionNotice | None = None
    reported_terminal_notices: set[tuple[str | None, str]] = field(
        default_factory=set
    )
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
