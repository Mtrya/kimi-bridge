"""Reliable allowlisted private-text WeChat adapter over iLink long polling."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Protocol, TypeVar

from ...interactions import InteractionOutcome, InteractionPrompt
from ..base import (
    ActorRef,
    ConversationRef,
    InboundAudio,
    InboundFile,
    InboundHandler,
    InboundImage,
    InboundMessage,
    InboundVideo,
    InteractionHandler,
    MessageRef,
    OutboundFile,
)
from .formatting import sanitize_markdown
from .media import WeChatMediaClient, WeChatOutboundClassification
from .storage import WeChatRuntimeState, WeChatStorage
from .types import (
    DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    MAX_LONG_POLL_TIMEOUT_SECONDS,
    MEDIA_ITEM_TYPES,
    MESSAGE_ITEM_TYPE_FILE,
    MESSAGE_ITEM_TYPE_TEXT,
    MESSAGE_TYPE_BOT,
    MESSAGE_TYPE_USER,
    MIN_LONG_POLL_TIMEOUT_SECONDS,
    TYPING_REFRESH_SECONDS,
    TYPING_STATUS_ACTIVE,
    TYPING_STATUS_CANCEL,
    TYPING_TICKET_TTL_SECONDS,
    WeChatAPIResult,
    WeChatAuthenticationExpired,
    WeChatInboundEvent,
    WeChatMessageItem,
    WeChatPollResult,
    WeChatProtocolError,
    WeChatRetryableError,
    WeChatStorageError,
    WeChatTypingConfig,
    WeChatUploadRequest,
    WeChatUploadedMedia,
    WeChatUploadTarget,
    WeChatUnsupportedOperation,
)


LOGGER = logging.getLogger(__name__)
_UNSUPPORTED_INTERACTION_NOTICE = (
    "Interactive prompts are unavailable on WeChat; steer with a normal message."
)
DEFAULT_PROCESSED_MESSAGE_LIMIT = 4096
DEFAULT_POLL_RETRY_INITIAL_SECONDS = 1.0
DEFAULT_POLL_RETRY_MAX_SECONDS = 30.0
DEFAULT_SAFE_RETRY_ATTEMPTS = 3
DEFAULT_SAFE_RETRY_INITIAL_SECONDS = 0.5
DEFAULT_SAFE_RETRY_MAX_SECONDS = 2.0

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
ProcessedIdentity = tuple[str, str, int]
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _TypingTicket:
    value: str = field(repr=False)
    expires_at: float


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

    async def get_upload_url(
        self, request: WeChatUploadRequest
    ) -> WeChatUploadTarget: ...

    async def send_media(
        self,
        *,
        to_user_id: str,
        context_token: str,
        client_id: str,
        item_type: int,
        uploaded: WeChatUploadedMedia,
        file_name: str | None = None,
    ) -> None: ...

    async def get_config(
        self, *, ilink_user_id: str, context_token: str
    ) -> WeChatTypingConfig: ...

    async def send_typing(
        self,
        *,
        ilink_user_id: str,
        typing_ticket: str,
        status: int,
    ) -> None: ...

    async def notify_start(self) -> WeChatAPIResult: ...

    async def notify_stop(self) -> WeChatAPIResult: ...

    async def close(self) -> None: ...


class WeChatMediaRuntime(Protocol):
    async def download_item(
        self, item: WeChatMessageItem
    ) -> InboundImage | InboundVideo | InboundFile | InboundAudio: ...

    async def upload_file(
        self, file: OutboundFile, *, to_user_id: str
    ) -> tuple[WeChatOutboundClassification, WeChatUploadedMedia]: ...

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
        media: WeChatMediaRuntime | None = None,
        storage: WeChatStorage,
        runtime_state: WeChatRuntimeState | None = None,
        processed_message_limit: int = DEFAULT_PROCESSED_MESSAGE_LIMIT,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        poll_retry_initial_seconds: float = DEFAULT_POLL_RETRY_INITIAL_SECONDS,
        poll_retry_max_seconds: float = DEFAULT_POLL_RETRY_MAX_SECONDS,
        safe_retry_attempts: int = DEFAULT_SAFE_RETRY_ATTEMPTS,
        safe_retry_initial_seconds: float = DEFAULT_SAFE_RETRY_INITIAL_SECONDS,
        safe_retry_max_seconds: float = DEFAULT_SAFE_RETRY_MAX_SECONDS,
        typing_refresh_seconds: float = TYPING_REFRESH_SECONDS,
        typing_ticket_ttl_seconds: float = TYPING_TICKET_TTL_SECONDS,
    ) -> None:
        if not bot_id.strip():
            raise ValueError("bot_id must be non-empty")
        if not allowed_users:
            raise ValueError("allowed_users must be non-empty")
        if any(not isinstance(user, str) or not user.strip() for user in allowed_users):
            raise ValueError("allowed_users must contain non-empty strings")
        if processed_message_limit <= 0:
            raise ValueError("processed_message_limit must be positive")
        if poll_retry_initial_seconds <= 0 or poll_retry_max_seconds <= 0:
            raise ValueError("poll retry delays must be positive")
        if poll_retry_initial_seconds > poll_retry_max_seconds:
            raise ValueError("initial poll retry delay cannot exceed its cap")
        if safe_retry_attempts <= 0:
            raise ValueError("safe_retry_attempts must be positive")
        if safe_retry_initial_seconds <= 0 or safe_retry_max_seconds <= 0:
            raise ValueError("safe retry delays must be positive")
        if safe_retry_initial_seconds > safe_retry_max_seconds:
            raise ValueError("initial safe retry delay cannot exceed its cap")
        if typing_refresh_seconds <= 0 or typing_ticket_ttl_seconds <= 0:
            raise ValueError("WeChat typing intervals must be positive")

        self._bot_id = bot_id.strip()
        self._allowed_users = frozenset(user.strip() for user in allowed_users)
        self._api = api
        self._media = media or WeChatMediaClient(api)
        self._storage = storage
        self._runtime_state = runtime_state or storage.load_runtime_state()
        self._processed_message_limit = processed_message_limit
        self._processed_ids = set(self._runtime_state.processed_message_ids)
        self._sleep = sleep
        self._clock = clock
        self._poll_retry_initial_seconds = poll_retry_initial_seconds
        self._poll_retry_max_seconds = poll_retry_max_seconds
        self._safe_retry_attempts = safe_retry_attempts
        self._safe_retry_initial_seconds = safe_retry_initial_seconds
        self._safe_retry_max_seconds = safe_retry_max_seconds
        self._typing_refresh_seconds = typing_refresh_seconds
        self._typing_ticket_ttl_seconds = typing_ticket_ttl_seconds
        self._poll_timeout_seconds = DEFAULT_LONG_POLL_TIMEOUT_SECONDS
        self._on_message: InboundHandler | None = None
        self._on_interaction: InteractionHandler | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._typing_tasks: dict[ConversationRef, asyncio.Task[None]] = {}
        self._typing_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._typing_tickets: dict[ConversationRef, _TypingTicket] = {}
        self._terminal_error: BaseException | None = None
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
        try:
            await self._poll_task
        except asyncio.CancelledError:
            if self._terminal_error is not None:
                raise self._terminal_error
            raise
        if self._terminal_error is not None:
            raise self._terminal_error

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        poll_task = self._poll_task
        if poll_task is not None and not poll_task.done():
            poll_task.cancel()

        typing_tasks = tuple(self._typing_tasks.values())
        for conversation in tuple(self._typing_tasks):
            self._finish_typing(conversation)

        cleanup_tasks = tuple(self._typing_cleanup_tasks)
        for task in typing_tasks:
            task.cancel()
        if poll_task is not None:
            await asyncio.gather(poll_task, return_exceptions=True)
        if typing_tasks or cleanup_tasks:
            await asyncio.gather(
                *typing_tasks,
                *cleanup_tasks,
                return_exceptions=True,
            )
        try:
            if poll_task is not None:
                await self._notify_best_effort(starting=False)
        finally:
            try:
                await self._media.close()
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
        try:
            return await self.send_text(conversation, text)
        finally:
            self._finish_typing(conversation)

    async def edit_text(self, message: MessageRef, text: str) -> None:
        raise WeChatUnsupportedOperation("WeChat text messages cannot be edited")

    async def send_file(
        self, conversation: ConversationRef, file: OutboundFile
    ) -> MessageRef:
        self._validate_conversation(conversation)
        context_token = self._runtime_state.context_tokens.get(
            (conversation.bot_id, conversation.conversation_id)
        )
        if context_token is None:
            raise WeChatProtocolError(
                "WeChat reply context is unavailable for this conversation"
            )
        classification, uploaded = await self._media.upload_file(
            file, to_user_id=conversation.conversation_id
        )
        client_id = secrets.token_hex(16)
        await self._api.send_media(
            to_user_id=conversation.conversation_id,
            context_token=context_token,
            client_id=client_id,
            item_type=classification.message_item_type,
            uploaded=uploaded,
            file_name=(
                classification.name
                if classification.message_item_type == MESSAGE_ITEM_TYPE_FILE
                else None
            ),
        )
        return MessageRef(conversation, client_id)

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
        """Handle in order, record completions durably, then commit the cursor.

        This narrows ordinary replay but remains at-least-once: a crash after Kimi
        accepts a prompt and before the processed identity is stored can replay it.
        """

        batch_identities = {
            identity
            for event in result.messages
            if (identity := self._processed_identity(event)) is not None
        }
        protected = batch_identities & self._processed_ids
        for event in result.messages:
            identity = self._processed_identity(event)
            if identity is not None and identity in self._processed_ids:
                continue
            await self._handle_inbound_event(event)
            if identity is not None:
                protected.add(identity)
                self._record_processed(identity, protected=protected)

        cursor = result.get_updates_buf or self._runtime_state.get_updates_buf
        bounded_ids = self._bounded_processed(
            self._runtime_state.processed_message_ids,
            protected=frozenset(),
        )
        if (
            cursor != self._runtime_state.get_updates_buf
            or bounded_ids != self._runtime_state.processed_message_ids
        ):
            self._save_runtime_state(
                replace(
                    self._runtime_state,
                    get_updates_buf=cursor,
                    processed_message_ids=bounded_ids,
                )
            )

    async def _poll_updates(self) -> None:
        retry_delay = self._poll_retry_initial_seconds
        while True:
            try:
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
            except WeChatAuthenticationExpired:
                raise
            except WeChatProtocolError:
                raise
            except (WeChatRetryableError, WeChatStorageError) as exc:
                LOGGER.warning(
                    "WeChat polling will retry after %s (%s)",
                    type(exc).__name__,
                    retry_delay,
                )
                await self._sleep(retry_delay)
                retry_delay = min(
                    retry_delay * 2, self._poll_retry_max_seconds
                )
                continue
            except Exception as exc:
                LOGGER.warning(
                    "WeChat inbound handling will retry after %s (%s)",
                    type(exc).__name__,
                    retry_delay,
                )
                await self._sleep(retry_delay)
                retry_delay = min(
                    retry_delay * 2, self._poll_retry_max_seconds
                )
                continue
            retry_delay = self._poll_retry_initial_seconds

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
        if self._on_message is None:
            raise RuntimeError("WeChat adapter has not been started")
        images: list[InboundImage] = []
        videos: list[InboundVideo] = []
        files: list[InboundFile] = []
        audios: list[InboundAudio] = []
        if has_media:
            for item in event.items:
                if item.type not in MEDIA_ITEM_TYPES:
                    continue
                downloaded = await self._media.download_item(item)
                if isinstance(downloaded, InboundImage):
                    images.append(downloaded)
                elif isinstance(downloaded, InboundVideo):
                    videos.append(downloaded)
                elif isinstance(downloaded, InboundFile):
                    files.append(downloaded)
                else:
                    audios.append(downloaded)
        assert text_item is not None or has_media
        text = text_item.text if text_item is not None and text_item.text else ""
        self._start_typing(conversation, event.context_token)
        try:
            await self._on_message(
                self,
                InboundMessage(
                    conversation=conversation,
                    actor=ActorRef(id=sender),
                    message_id=str(event.message_id),
                    text=text,
                    timestamp=event.create_time_ms / 1000,
                    images=tuple(images),
                    videos=tuple(videos),
                    files=tuple(files),
                    audios=tuple(audios),
                ),
            )
        except BaseException:
            self._finish_typing(conversation)
            raise

    def _store_context_token(
        self, conversation: ConversationRef, context_token: str
    ) -> None:
        key = (conversation.bot_id, conversation.conversation_id)
        if self._runtime_state.context_tokens.get(key) == context_token:
            return
        contexts = dict(self._runtime_state.context_tokens)
        contexts[key] = context_token
        self._save_runtime_state(
            replace(self._runtime_state, context_tokens=contexts)
        )

    def _processed_identity(
        self, event: WeChatInboundEvent
    ) -> ProcessedIdentity | None:
        if (
            not event.from_user_id
            or event.message_id is None
            or event.message_id <= 0
        ):
            return None
        return (self._bot_id, event.from_user_id, event.message_id)

    def _record_processed(
        self,
        identity: ProcessedIdentity,
        *,
        protected: set[ProcessedIdentity],
    ) -> None:
        if identity in self._processed_ids:
            return
        ordered = (*self._runtime_state.processed_message_ids, identity)
        bounded = self._bounded_processed(ordered, protected=protected)
        self._save_runtime_state(
            replace(self._runtime_state, processed_message_ids=bounded)
        )

    def _bounded_processed(
        self,
        identities: tuple[ProcessedIdentity, ...],
        *,
        protected: set[ProcessedIdentity] | frozenset[ProcessedIdentity],
    ) -> tuple[ProcessedIdentity, ...]:
        kept = list(identities)
        target = max(self._processed_message_limit, len(protected))
        while len(kept) > target:
            remove_at = next(
                (index for index, identity in enumerate(kept) if identity not in protected),
                None,
            )
            if remove_at is None:
                break
            kept.pop(remove_at)
        return tuple(kept)

    def _save_runtime_state(self, state: WeChatRuntimeState) -> None:
        self._storage.save_runtime_state(state)
        self._runtime_state = state
        self._processed_ids = set(state.processed_message_ids)

    def _validate_conversation(self, conversation: ConversationRef) -> None:
        if conversation.platform != "wechat":
            raise ValueError(
                f"unexpected platform for WeChat adapter: {conversation.platform}"
            )
        if conversation.bot_id != self._bot_id:
            raise ValueError("unexpected bot identity for WeChat adapter")

    def _start_typing(
        self, conversation: ConversationRef, context_token: str
    ) -> None:
        self._finish_typing(conversation)
        task = asyncio.create_task(
            self._typing_loop(conversation, context_token),
            name="wechat-typing-indicator",
        )
        self._typing_tasks[conversation] = task
        task.add_done_callback(
            lambda completed, current=conversation: self._typing_done(
                current, completed
            )
        )

    def _finish_typing(self, conversation: ConversationRef) -> None:
        task = self._typing_tasks.pop(conversation, None)
        if task is not None and not task.done():
            task.cancel()
        ticket = self._typing_tickets.get(conversation)
        if ticket is None:
            return
        cleanup = asyncio.create_task(
            self._cancel_typing_best_effort(conversation, ticket.value),
            name="wechat-typing-cancel",
        )
        self._typing_cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._typing_cleanup_done)

    async def _typing_loop(
        self, conversation: ConversationRef, context_token: str
    ) -> None:
        try:
            while True:
                ticket = await self._typing_ticket(conversation, context_token)
                if ticket is None:
                    return
                await self._retry_safe_operation(
                    lambda: self._api.send_typing(
                        ilink_user_id=conversation.conversation_id,
                        typing_ticket=ticket,
                        status=TYPING_STATUS_ACTIVE,
                    ),
                    operation="sendTyping",
                )
                await self._sleep(self._typing_refresh_seconds)
        except asyncio.CancelledError:
            raise
        except WeChatAuthenticationExpired:
            raise
        except Exception as exc:
            LOGGER.warning(
                "WeChat typing stopped after a best-effort failure (%s)",
                type(exc).__name__,
            )

    async def _typing_ticket(
        self, conversation: ConversationRef, context_token: str
    ) -> str | None:
        cached = self._typing_tickets.get(conversation)
        now = self._clock()
        if cached is not None and now < cached.expires_at:
            return cached.value
        config = await self._retry_safe_operation(
            lambda: self._api.get_config(
                ilink_user_id=conversation.conversation_id,
                context_token=context_token,
            ),
            operation="getConfig",
        )
        ticket = config.typing_ticket
        if not ticket:
            return None
        self._typing_tickets[conversation] = _TypingTicket(
            value=ticket,
            expires_at=now + self._typing_ticket_ttl_seconds,
        )
        return ticket

    async def _cancel_typing_best_effort(
        self, conversation: ConversationRef, ticket: str
    ) -> None:
        try:
            await self._retry_safe_operation(
                lambda: self._api.send_typing(
                    ilink_user_id=conversation.conversation_id,
                    typing_ticket=ticket,
                    status=TYPING_STATUS_CANCEL,
                ),
                operation="sendTyping",
            )
        except WeChatAuthenticationExpired as exc:
            if not self._closed:
                self._set_terminal_error(exc)
        except Exception as exc:
            LOGGER.warning(
                "WeChat typing cancellation failed and was ignored (%s)",
                type(exc).__name__,
            )

    def _typing_done(
        self,
        conversation: ConversationRef,
        task: asyncio.Task[None],
    ) -> None:
        if self._typing_tasks.get(conversation) is task:
            self._typing_tasks.pop(conversation, None)
        if task.cancelled():
            return
        error = task.exception()
        if isinstance(error, WeChatAuthenticationExpired):
            self._set_terminal_error(error)

    def _typing_cleanup_done(self, task: asyncio.Task[None]) -> None:
        self._typing_cleanup_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _set_terminal_error(self, error: BaseException) -> None:
        if self._closed or self._terminal_error is not None:
            return
        self._terminal_error = error
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()

    async def _retry_safe_operation(
        self,
        call: Callable[[], Awaitable[T]],
        *,
        operation: str,
    ) -> T:
        delay = self._safe_retry_initial_seconds
        for attempt in range(self._safe_retry_attempts):
            try:
                return await call()
            except WeChatAuthenticationExpired:
                raise
            except WeChatRetryableError:
                if attempt + 1 >= self._safe_retry_attempts:
                    raise
                LOGGER.warning(
                    "WeChat %s transient failure; retrying", operation
                )
                await self._sleep(delay)
                delay = min(delay * 2, self._safe_retry_max_seconds)
        raise AssertionError("unreachable WeChat retry state")

    async def _notify_best_effort(self, *, starting: bool) -> None:
        name = "notifyStart" if starting else "notifyStop"
        try:
            result: WeChatAPIResult = await self._retry_safe_operation(
                self._api.notify_start if starting else self._api.notify_stop,
                operation=name,
            )
        except WeChatAuthenticationExpired:
            if starting:
                raise
            LOGGER.warning("WeChat %s reported stale authorization", name)
            return
        except Exception as exc:
            LOGGER.warning(
                "WeChat %s failed and was ignored (%s)",
                name,
                type(exc).__name__,
            )
            return
        if result.ret not in {None, 0}:
            LOGGER.warning("WeChat %s returned ret=%s", name, result.ret)
