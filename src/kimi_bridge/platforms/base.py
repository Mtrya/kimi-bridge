"""Platform adapter interface used by the chat router."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from ..interactions import (
    InteractionOutcome,
    InteractionPrompt,
    InteractionResponse,
)


@dataclass(frozen=True, slots=True)
class ConversationRef:
    platform: str
    bot_id: str
    conversation_id: str


@dataclass(frozen=True, slots=True)
class ActorRef:
    id: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class MessageRef:
    conversation: ConversationRef
    message_id: str


@dataclass(frozen=True, slots=True)
class InboundImage:
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class InboundFile:
    data: bytes
    name: str
    media_type: str


@dataclass(frozen=True, slots=True)
class OutboundFile:
    """One platform-neutral file selected for outbound delivery."""

    name: str
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class InboundMessage:
    conversation: ConversationRef
    actor: ActorRef
    message_id: str
    text: str
    timestamp: float
    images: tuple[InboundImage, ...] = ()
    files: tuple[InboundFile, ...] = ()

    @property
    def source(self) -> MessageRef:
        return MessageRef(self.conversation, self.message_id)


@dataclass(frozen=True, slots=True)
class InboundInteraction:
    source: MessageRef
    actor: ActorRef
    interaction_id: str | None
    response: InteractionResponse | None

    @property
    def conversation(self) -> ConversationRef:
        return self.source.conversation


# What an adapter does with an inbound message is the router's business.
InboundHandler = Callable[["PlatformAdapter", InboundMessage], Awaitable[None]]
InteractionHandler = Callable[
    ["PlatformAdapter", InboundInteraction], Awaitable[None]
]


class PlatformAdapter(Protocol):
    name: str
    message_limit: int

    async def start(
        self,
        on_message: InboundHandler,
        on_interaction: InteractionHandler,
    ) -> None: ...
    async def wait(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(
        self, conversation: ConversationRef, text: str
    ) -> MessageRef: ...
    async def edit_text(self, message: MessageRef, text: str) -> None: ...
    async def send_file(
        self, conversation: ConversationRef, file: OutboundFile
    ) -> MessageRef: ...
    async def present_interaction(
        self, conversation: ConversationRef, prompt: InteractionPrompt
    ) -> MessageRef: ...
    async def finish_interaction(
        self, message: MessageRef, outcome: InteractionOutcome
    ) -> None: ...
