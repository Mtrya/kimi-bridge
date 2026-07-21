"""Platform adapter interface used by the chat router."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


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
class InboundMessage:
    platform: str
    bot_id: str
    user_id: str
    user_name: str | None
    text: str
    timestamp: float
    message_id: str = ""
    conversation_id: str = ""
    images: tuple[InboundImage, ...] = ()
    files: tuple[InboundFile, ...] = ()


@dataclass(frozen=True, slots=True)
class CardAction:
    platform: str
    bot_id: str
    user_id: str
    conversation_id: str
    message_id: str
    value: dict[str, Any] = field(default_factory=dict)
    form_value: dict[str, Any] = field(default_factory=dict)
    action_name: str | None = None


# What an adapter does with an inbound message is the router's business.
InboundHandler = Callable[["PlatformAdapter", InboundMessage], Awaitable[None]]
CardActionHandler = Callable[["PlatformAdapter", CardAction], Awaitable[None]]


class PlatformAdapter(Protocol):
    name: str
    message_limit: int

    async def start(
        self,
        on_message: InboundHandler,
        on_card_action: CardActionHandler,
    ) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, user_id: str, text: str) -> str: ...
    async def edit_text(self, message_id: str, text: str) -> None: ...
    async def send_card(self, user_id: str, card: dict[str, Any]) -> str: ...
    async def edit_card(self, message_id: str, card: dict[str, Any]) -> None: ...
