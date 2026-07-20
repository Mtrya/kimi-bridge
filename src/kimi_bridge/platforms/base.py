"""Platform adapter interface used by the chat router."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class InboundMessage:
    platform: str
    bot_id: str
    user_id: str
    user_name: str | None
    text: str
    timestamp: float


# What an adapter does with an inbound message is the router's business.
InboundHandler = Callable[["PlatformAdapter", InboundMessage], Awaitable[None]]


class PlatformAdapter(Protocol):
    name: str
    message_limit: int

    async def start(self, on_message: InboundHandler) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, user_id: str, text: str) -> str: ...
    async def edit_text(self, message_id: str, text: str) -> None: ...
