"""Allowlisted private-text WeChat adapter over iLink long polling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from dataclasses import replace
from typing import Protocol

from ...interactions import InteractionOutcome, InteractionPrompt
from ..base import (
    ActorRef,
    ConversationRef,
    InboundAudio,
    InboundHandler,
    InboundMessage,
    InteractionHandler,
    MessageRef,
    OutboundFile,
)
from .formatting import sanitize_markdown
from .storage import WeChatRuntimeState, WeChatStorage
from .types import (
    DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    MAX_LONG_POLL_TIMEOUT_SECONDS,
    MEDIA_ITEM_TYPES,
    MESSAGE_ITEM_TYPE_TEXT,
    MESSAGE_TYPE_BOT,
    MESSAGE_TYPE_USER,
    MIN_LONG_POLL_TIMEOUT_SECONDS,
    WeChatAPIResult,
    WeChatInboundEvent,
    WeChatPollResult,
    WeChatProtocolError,
    WeChatUnsupportedOperation,
)


LOGGER = logging.getLogger(__name__)
_UNSUPPORTED_MEDIA_NOTICE = (
    "This WeChat bridge does not support media messages yet; send text instead."
)
_UNSUPPORTED_INTERACTION_NOTICE = (
    "Interactive prompts are unavailable on WeChat; steer with a normal message."
)


class WeChatRuntimeAPI(Protocol):
    """Narrow runtime transport surface consumed by ``WeChatAdapter``."""

    async def get_updates(
        self, get_updates_buf: str, *, timeout_seconds: float
    ) -> WeChatPollResult: ...

    async def send_text(
        self,
        *,
        to_user_id: str,
        context_token: str,
        text: str,
        client_id: str,
    ) -> None: ...

    async def notify_start(self) -> WeChatAPIResult: ...

    async def notify_stop(self) -> WeChatAPIResult: ...

    async def close(self) -> None: ...


class WeChatAdapter:
    """One QR-authorized, direct-message-only WeChat adapter."""

    name = "wechat"
    message_limit = 4000
    supports_edits = False
    supports_interactions = False
    message_edit_limit = None

    def __init__(
        self,
        bot_id: str,
        allowed_users: set[str] | frozenset[str],
        *,
        api: WeChatRuntimeAPI,
        storage: WeChatStorage,
        runtime_state: WeChatRuntimeState | None = None,
    ) -> None:
        if not bot_id.strip():
            raise ValueError("bot_id must be non-empty")
        if not allowed_users:
            raise ValueError("allowed_users must be non-empty")
        if any(not isinstance(user, str) or not user.strip() for user in allowed_users):
            raise ValueError("allowed_users must contain non-empty strings")
        self._bot_id = bot_id.strip()
        self._allowed_users = frozenset(user.strip() for user in allowed_users)
        self._api = api
        self._storage = storage
        self._runtime_state = runtime_state or storage.load_runtime_state()
        self._poll_timeout_seconds = DEFAULT_LONG_POLL_TIMEOUT_SECONDS
        self._on_message: InboundHandler | None = None
        self._on_interaction: InteractionHandler | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(
        self,
        on_message: InboundHandler,
        on_interaction: InteractionHandler,
    ) -> None:
        if self._poll_task is not None:
            raise RuntimeError("WeChat adapter is already started")
        if self._closed:
            raise RuntimeError("WeChat adapter is closed")
        self._on_message = on_message
        self._on_interaction = on_interaction
        await self._notify_best_effort(starting=True)
        self._poll_task = asyncio.create_task(
            self._poll_updates(), name="wechat-long-poll"
        )

    async def wait(self) -> None:
        if self._poll_task is None:
            raise RuntimeError("WeChat adapter has not been started")
        await self._poll_task

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._poll_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            if task is not None:
                await self._notify_best_effort(starting=False)
        finally:
            await self._api.close()

    async def transcribe_audio(self, audio: InboundAudio) -> str:
        return audio.transcript.strip() if audio.transcript else ""

    async def send_text(
        self, conversation: ConversationRef, text: str
    ) -> MessageRef:
        self._validate_conversation(conversation)
        context_token = self._runtime_state.context_tokens.get(
            (conversation.bot_id, conversation.conversation_id)
        )
        if context_token is None:
            raise WeChatProtocolError(
                "WeChat reply context is unavailable for this conversation"
            )
        client_id = secrets.token_hex(16)
        await self._api.send_text(
            to_user_id=conversation.conversation_id,
            context_token=context_token,
            text=sanitize_markdown(text),
            client_id=client_id,
        )
        return MessageRef(conversation, client_id)

    async def send_final_text(
        self, conversation: ConversationRef, text: str
    ) -> MessageRef:
        return await self.send_text(conversation, text)

    async def edit_text(self, message: MessageRef, text: str) -> None:
        raise WeChatUnsupportedOperation("WeChat text messages cannot be edited")

    async def send_file(
        self, conversation: ConversationRef, file: OutboundFile
    ) -> MessageRef:
        raise WeChatUnsupportedOperation(
            "WeChat file delivery is not available yet"
        )

    async def present_interaction(
        self, conversation: ConversationRef, prompt: InteractionPrompt
    ) -> MessageRef:
        return await self.send_final_text(
            conversation, _UNSUPPORTED_INTERACTION_NOTICE
        )

    async def finish_interaction(
        self, message: MessageRef, outcome: InteractionOutcome
    ) -> None:
        return None

    async def handle_poll_result(self, result: WeChatPollResult) -> None:
        """Process one validated response in order, then commit its cursor."""

        for event in result.messages:
            await self._handle_inbound_event(event)
        cursor = result.get_updates_buf
        if cursor and cursor != self._runtime_state.get_updates_buf:
            next_state = replace(self._runtime_state, get_updates_buf=cursor)
            self._storage.save_runtime_state(next_state)
            self._runtime_state = next_state

    async def _poll_updates(self) -> None:
        while True:
            result = await self._api.get_updates(
                self._runtime_state.get_updates_buf,
                timeout_seconds=self._poll_timeout_seconds,
            )
            if result.long_poll_timeout_seconds is not None:
                self._poll_timeout_seconds = min(
                    MAX_LONG_POLL_TIMEOUT_SECONDS,
                    max(
                        MIN_LONG_POLL_TIMEOUT_SECONDS,
                        result.long_poll_timeout_seconds,
                    ),
                )
            await self.handle_poll_result(result)

    async def _handle_inbound_event(self, event: WeChatInboundEvent) -> None:
        if event.message_type == MESSAGE_TYPE_BOT:
            return
        if event.message_type is not None and event.message_type != MESSAGE_TYPE_USER:
            LOGGER.warning(
                "unsupported WeChat event type %s", event.message_type
            )
            return
        sender = event.from_user_id
        if not sender:
            raise WeChatProtocolError(
                "authorized WeChat message is missing from_user_id"
            )
        if sender not in self._allowed_users:
            LOGGER.warning(
                "unauthorized WeChat sender %s; add it to [wechat].allowed_users",
                sender,
            )
            return
        if event.message_type is None:
            raise WeChatProtocolError(
                "authorized WeChat message is missing message_type"
            )
        if event.group_id:
            LOGGER.warning("unsupported WeChat group message from %s", sender)
            return
        if event.message_id is None or event.message_id <= 0:
            raise WeChatProtocolError(
                "authorized WeChat message has an invalid message_id"
            )
        if event.create_time_ms is None or event.create_time_ms <= 0:
            raise WeChatProtocolError(
                "authorized WeChat message has an invalid create_time_ms"
            )
        if not event.context_token:
            raise WeChatProtocolError(
                "authorized WeChat message is missing context_token"
            )

        conversation = ConversationRef(
            platform="wechat",
            bot_id=self._bot_id,
            conversation_id=sender,
        )
        has_media = any(item.type in MEDIA_ITEM_TYPES for item in event.items)
        text_item = next(
            (item for item in event.items if item.type == MESSAGE_ITEM_TYPE_TEXT),
            None,
        )
        if not has_media and (text_item is None or text_item.text is None):
            raise WeChatProtocolError(
                "authorized WeChat message is missing a valid text item"
            )
        self._store_context_token(conversation, event.context_token)
        if has_media:
            await self.send_final_text(conversation, _UNSUPPORTED_MEDIA_NOTICE)
            return

        assert text_item is not None and text_item.text is not None
        if self._on_message is None:
            raise RuntimeError("WeChat adapter has not been started")
        await self._on_message(
            self,
            InboundMessage(
                conversation=conversation,
                actor=ActorRef(id=sender),
                message_id=str(event.message_id),
                text=text_item.text,
                timestamp=event.create_time_ms / 1000,
            ),
        )

    def _store_context_token(
        self, conversation: ConversationRef, context_token: str
    ) -> None:
        contexts = dict(self._runtime_state.context_tokens)
        contexts[(conversation.bot_id, conversation.conversation_id)] = context_token
        next_state = replace(self._runtime_state, context_tokens=contexts)
        self._storage.save_runtime_state(next_state)
        self._runtime_state = next_state

    def _validate_conversation(self, conversation: ConversationRef) -> None:
        if conversation.platform != "wechat":
            raise ValueError(
                f"unexpected platform for WeChat adapter: {conversation.platform}"
            )
        if conversation.bot_id != self._bot_id:
            raise ValueError("unexpected bot identity for WeChat adapter")

    async def _notify_best_effort(self, *, starting: bool) -> None:
        name = "notifyStart" if starting else "notifyStop"
        try:
            result: WeChatAPIResult = await (
                self._api.notify_start()
                if starting
                else self._api.notify_stop()
            )
        except Exception as exc:
            LOGGER.warning(
                "WeChat %s failed and was ignored (%s)",
                name,
                type(exc).__name__,
            )
            return
        if result.ret not in {None, 0}:
            LOGGER.warning("WeChat %s returned ret=%s", name, result.ret)
