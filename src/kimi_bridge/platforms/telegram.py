"""Telegram Bot API transport and semantic platform adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import httpx

from ..interactions import (
    ApprovalDecision,
    ApprovalPrompt,
    ApprovalResponse,
    InteractionOutcome,
    InteractionPrompt,
    MultipleChoiceAnswer,
    MultipleChoiceWithOtherAnswer,
    OtherAnswer,
    Question,
    QuestionAnswer,
    QuestionPrompt,
    QuestionResponse,
    SingleChoiceAnswer,
    SkippedAnswer,
)
from .base import (
    ActorRef,
    ConversationRef,
    InboundFile,
    InboundHandler,
    InboundImage,
    InboundInteraction,
    InboundMessage,
    InteractionHandler,
    MessageRef,
)


LOGGER = logging.getLogger(__name__)
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024
TELEGRAM_POLL_TIMEOUT = 30
_ALLOWED_UPDATES = ["message", "callback_query"]
_MAX_INTERACTION_TEXT = 3800
_ALBUM_MEMORY = 256
_CLOSED_REPLY_MEMORY = 256
_UNSUPPORTED_MESSAGE_FIELDS = frozenset(
    {
        "animation",
        "audio",
        "contact",
        "dice",
        "game",
        "invoice",
        "live_photo",
        "location",
        "paid_media",
        "poll",
        "sticker",
        "story",
        "successful_payment",
        "venue",
        "video",
        "video_note",
        "voice",
    }
)
_UNSUPPORTED_MESSAGE_TEXT = (
    "This Telegram message type is not supported. Send plain text, one photo, "
    "or one document."
)
_ALBUM_MESSAGE_TEXT = (
    "Telegram albums are not supported. Send one photo or document at a time."
)
_OVERSIZE_MESSAGE_TEXT = "Telegram files must be 20 MB or smaller."
_WIZARD_REMINDER = (
    "Finish the active question, choose Skip, or use /stop before sending "
    "another message."
)
_INACTIVE_REPLY_TEXT = "That custom-answer prompt is no longer active."


class TelegramError(RuntimeError):
    """Base exception for the Telegram boundary."""


class TelegramProtocolError(TelegramError):
    """Telegram returned a shape that violates the Bot API contract."""


class TelegramTransportError(TelegramError):
    """A Telegram HTTP request failed after transient retries."""


class TelegramAPIError(TelegramError):
    """Telegram returned a structured Bot API failure."""

    def __init__(
        self,
        method: str,
        error_code: int,
        description: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        self.method = method
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after
        super().__init__(f"Telegram API {method} failed ({error_code}): {description}")


class TelegramFileTooLarge(TelegramError):
    """An inbound Telegram file exceeds the supported Bot API limit."""


class TelegramAPI(Protocol):
    async def request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any: ...

    async def get_file(
        self, file_id: str, *, known_size: int | None = None
    ) -> bytes: ...

    async def close(self) -> None: ...


class TelegramBotAPI:
    """Small async client for the Bot API methods used by the bridge."""

    def __init__(
        self,
        bot_token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = "https://api.telegram.org",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 4,
        initial_backoff: float = 0.5,
        max_backoff: float = 8.0,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token must be non-empty")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if initial_backoff <= 0 or max_backoff <= 0:
            raise ValueError("retry backoff must be positive")
        if initial_backoff > max_backoff:
            raise ValueError("initial_backoff cannot exceed max_backoff")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        base = api_base_url.rstrip("/")
        self._method_base_url = f"{base}/bot{bot_token}"
        self._file_base_url = f"{base}/file/bot{bot_token}"
        self._http = http_client or httpx.AsyncClient()
        self._owns_http = http_client is None
        self._sleep = sleep
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._closed = False

    async def request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call one Bot API method and return its result."""

        if self._closed:
            raise RuntimeError("Telegram Bot API client is closed")
        request_timeout = timeout if timeout is not None else 30.0
        attempt = 0
        while True:
            try:
                response = await self._http.post(
                    f"{self._method_base_url}/{method}",
                    json=payload or {},
                    timeout=request_timeout,
                )
            except asyncio.CancelledError:
                raise
            except httpx.RequestError:
                if attempt >= self._max_retries:
                    raise TelegramTransportError(
                        f"Telegram API {method} request failed"
                    ) from None
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue

            envelope = _response_json(response)
            api_error = _api_error(method, response.status_code, envelope)
            retry_after = api_error.retry_after if api_error is not None else None
            retryable = response.status_code == 429 or response.status_code >= 500
            if api_error is not None:
                retryable = retryable or api_error.error_code == 429
                retryable = retryable or api_error.error_code >= 500
            if retryable and attempt < self._max_retries:
                delay = (
                    retry_after if retry_after is not None else self._backoff(attempt)
                )
                await self._sleep(delay)
                attempt += 1
                continue
            if api_error is not None:
                raise api_error
            if not 200 <= response.status_code < 300:
                raise TelegramAPIError(
                    method,
                    response.status_code,
                    f"HTTP {response.status_code}",
                )
            if not isinstance(envelope, dict) or envelope.get("ok") is not True:
                raise TelegramProtocolError(
                    f"Telegram API {method} returned an invalid response envelope"
                )
            if "result" not in envelope:
                raise TelegramProtocolError(
                    f"Telegram API {method} response omitted result"
                )
            return envelope["result"]

    async def get_file(self, file_id: str, *, known_size: int | None = None) -> bytes:
        """Resolve and download a Telegram file with a hard byte ceiling."""

        if known_size is not None and known_size > TELEGRAM_FILE_LIMIT:
            raise TelegramFileTooLarge("Telegram file exceeds 20 MB")
        result = await self.request("getFile", {"file_id": file_id})
        if not isinstance(result, dict):
            raise TelegramProtocolError("Telegram getFile result must be an object")
        file_path = result.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise TelegramProtocolError("Telegram getFile result omitted file_path")
        resolved_size = result.get("file_size")
        if resolved_size is not None and (
            isinstance(resolved_size, bool) or not isinstance(resolved_size, int)
        ):
            raise TelegramProtocolError("Telegram file_size must be an integer")
        if isinstance(resolved_size, int) and resolved_size > TELEGRAM_FILE_LIMIT:
            raise TelegramFileTooLarge("Telegram file exceeds 20 MB")
        return await self._download_file(file_path)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            await self._http.aclose()

    async def _download_file(self, file_path: str) -> bytes:
        safe_path = file_path.lstrip("/")
        attempt = 0
        while True:
            try:
                async with self._http.stream(
                    "GET",
                    f"{self._file_base_url}/{safe_path}",
                    timeout=60.0,
                ) as response:
                    retryable = (
                        response.status_code == 429 or response.status_code >= 500
                    )
                    if retryable and attempt < self._max_retries:
                        await response.aread()
                        retry_after = _retry_after(_response_json(response))
                    elif not 200 <= response.status_code < 300:
                        raise TelegramAPIError(
                            "file download",
                            response.status_code,
                            f"HTTP {response.status_code}",
                        )
                    else:
                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            try:
                                declared_size = int(content_length)
                            except ValueError as exc:
                                raise TelegramProtocolError(
                                    "Telegram file Content-Length is invalid"
                                ) from exc
                            if declared_size > TELEGRAM_FILE_LIMIT:
                                raise TelegramFileTooLarge(
                                    "Telegram file exceeds 20 MB"
                                )
                        chunks: list[bytes] = []
                        received = 0
                        async for chunk in response.aiter_bytes():
                            received += len(chunk)
                            if received > TELEGRAM_FILE_LIMIT:
                                raise TelegramFileTooLarge(
                                    "Telegram file exceeds 20 MB"
                                )
                            chunks.append(chunk)
                        return b"".join(chunks)
            except asyncio.CancelledError:
                raise
            except (TelegramFileTooLarge, TelegramProtocolError, TelegramAPIError):
                raise
            except httpx.RequestError:
                if attempt >= self._max_retries:
                    raise TelegramTransportError(
                        "Telegram file download failed"
                    ) from None
                retry_after = None

            delay = retry_after if retry_after is not None else self._backoff(attempt)
            await self._sleep(delay)
            attempt += 1

    def _backoff(self, attempt: int) -> float:
        return min(self._initial_backoff * (2**attempt), self._max_backoff)


@dataclass(slots=True)
class _ApprovalState:
    prompt: ApprovalPrompt
    message: MessageRef | None = None
    callback_tokens: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _QuestionWizard:
    prompt: QuestionPrompt
    conversation: ConversationRef
    actor_id: str
    message: MessageRef | None = None
    question_index: int = 0
    answers: list[QuestionAnswer] = field(default_factory=list)
    selected: set[str] = field(default_factory=set)
    callback_tokens: set[str] = field(default_factory=set)
    other_prompt_message_id: str | None = None
    submitting: bool = False


_InteractionState = _ApprovalState | _QuestionWizard


@dataclass(frozen=True, slots=True)
class _CallbackAction:
    state: _InteractionState
    kind: str
    question_index: int | None = None
    value: str | None = None


class TelegramAdapter:
    """Allowlisted private-chat Telegram adapter."""

    name = "telegram"
    message_limit = TELEGRAM_TEXT_LIMIT

    def __init__(
        self,
        bot_token: str,
        allowed_users: set[int] | frozenset[int],
        *,
        api: TelegramAPI | None = None,
        poll_timeout: int = TELEGRAM_POLL_TIMEOUT,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token must be non-empty")
        if not allowed_users:
            raise ValueError("allowed_users must be non-empty")
        if any(
            isinstance(user, bool) or not isinstance(user, int) or user <= 0
            for user in allowed_users
        ):
            raise ValueError("allowed_users must contain positive integers")
        if poll_timeout <= 0:
            raise ValueError("poll_timeout must be positive")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._allowed_users = frozenset(allowed_users)
        self._api = api or TelegramBotAPI(bot_token)
        self._poll_timeout = poll_timeout
        self._bot_id: str | None = None
        self._on_message: InboundHandler | None = None
        self._on_interaction: InteractionHandler | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._closed = False
        self._interactions: dict[MessageRef, _InteractionState] = {}
        self._wizards: dict[ConversationRef, _QuestionWizard] = {}
        self._conversation_actors: dict[ConversationRef, str] = {}
        self._callbacks: dict[str, _CallbackAction] = {}
        self._seen_albums: set[str] = set()
        self._album_order: deque[str] = deque()
        self._closed_replies: set[tuple[ConversationRef, str]] = set()
        self._closed_reply_order: deque[tuple[ConversationRef, str]] = deque()

    async def start(
        self,
        on_message: InboundHandler,
        on_interaction: InteractionHandler,
    ) -> None:
        if self._poll_task is not None:
            raise RuntimeError("Telegram adapter is already started")
        if self._closed:
            raise RuntimeError("Telegram adapter is closed")
        me = await self._api.request("getMe")
        if not isinstance(me, dict):
            raise TelegramProtocolError("Telegram getMe result must be an object")
        bot_id = me.get("id")
        if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id <= 0:
            raise TelegramProtocolError("Telegram getMe result has an invalid bot id")
        if me.get("is_bot") is not True:
            raise TelegramProtocolError("Telegram getMe identity is not a bot")
        await self._api.request("deleteWebhook", {"drop_pending_updates": True})
        self._bot_id = str(bot_id)
        self._on_message = on_message
        self._on_interaction = on_interaction
        self._poll_task = asyncio.create_task(
            self._poll_updates(), name="telegram-long-poll"
        )

    async def wait(self) -> None:
        if self._poll_task is None:
            raise RuntimeError("Telegram adapter has not been started")
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
        await self._api.close()

    async def send_text(self, conversation: ConversationRef, text: str) -> MessageRef:
        self._validate_conversation(conversation)
        result = await self._api.request(
            "sendMessage",
            {"chat_id": _chat_id(conversation), "text": _message_text(text)},
        )
        return _message_ref(conversation, result, "sendMessage")

    async def edit_text(self, message: MessageRef, text: str) -> None:
        self._validate_conversation(message.conversation)
        await self._edit_message(message, _message_text(text))

    async def present_interaction(
        self, conversation: ConversationRef, prompt: InteractionPrompt
    ) -> MessageRef:
        self._validate_conversation(conversation)
        if isinstance(prompt, ApprovalPrompt):
            state = _ApprovalState(prompt)
            text = _approval_text(prompt)
            markup = self._approval_markup(state)
        else:
            actor_id = self._actor_for_conversation(conversation)
            state = _QuestionWizard(prompt, conversation, actor_id)
            text = _question_text(state)
            markup = self._question_markup(state)
        try:
            result = await self._api.request(
                "sendMessage",
                {
                    "chat_id": _chat_id(conversation),
                    "text": text,
                    "reply_markup": markup,
                },
            )
            message = _message_ref(conversation, result, "sendMessage")
        except Exception:
            self._clear_callback_tokens(state)
            raise
        state.message = message
        self._interactions[message] = state
        if isinstance(state, _QuestionWizard):
            self._wizards[conversation] = state
        return message

    async def finish_interaction(
        self, message: MessageRef, outcome: InteractionOutcome
    ) -> None:
        self._validate_conversation(message.conversation)
        state = self._interactions.pop(message, None)
        if state is not None:
            self._clear_callback_tokens(state)
            if isinstance(state, _QuestionWizard):
                if self._wizards.get(state.conversation) is state:
                    self._wizards.pop(state.conversation)
                if state.other_prompt_message_id is not None:
                    self._remember_closed_reply(
                        state.conversation, state.other_prompt_message_id
                    )
        title = {
            "completed": "Interaction complete",
            "timed_out": "Interaction timed out",
            "stale": "Interaction expired",
            "cancelled": "Interaction cancelled",
        }[outcome.state]
        try:
            await self._edit_message(
                message,
                _truncate(f"{title}\n\n{outcome.detail}"),
                reply_markup={"inline_keyboard": []},
            )
        except TelegramAPIError as exc:
            if not _terminal_edit_unavailable(exc):
                raise

    async def handle_update(self, update: dict[str, Any]) -> None:
        """Handle one Bot API update; public for fake-based adapter tests."""

        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback(callback)
            return
        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message)

    async def _poll_updates(self) -> None:
        offset: int | None = None
        while True:
            payload: dict[str, Any] = {
                "timeout": self._poll_timeout,
                "allowed_updates": _ALLOWED_UPDATES,
            }
            if offset is not None:
                payload["offset"] = offset
            updates = await self._api.request(
                "getUpdates",
                payload,
                timeout=float(self._poll_timeout + 10),
            )
            if not isinstance(updates, list):
                raise TelegramProtocolError(
                    "Telegram getUpdates result must be an array"
                )
            for update in updates:
                if not isinstance(update, dict):
                    raise TelegramProtocolError("Telegram update must be an object")
                update_id = update.get("update_id")
                if isinstance(update_id, bool) or not isinstance(update_id, int):
                    raise TelegramProtocolError("Telegram update_id must be an integer")
                await self.handle_update(update)
                offset = update_id + 1

    async def _handle_message(self, message: dict[str, Any]) -> None:
        identity = self._message_identity(message)
        if identity is None:
            return
        conversation, actor = identity
        message_id = _required_int(message, "message_id", "Telegram message")
        text = message.get("text") if isinstance(message.get("text"), str) else ""
        caption = (
            message.get("caption") if isinstance(message.get("caption"), str) else ""
        )
        has_media = bool(message.get("photo") or message.get("document")) or any(
            field in message for field in _UNSUPPORTED_MESSAGE_FIELDS
        )
        if text.strip().startswith("/") and not has_media:
            await self._deliver_message(conversation, actor, message_id, text, message)
            return

        reply_to_id = _reply_to_message_id(message)
        wizard = self._wizards.get(conversation)
        if wizard is not None and wizard.actor_id == actor.id:
            if (
                wizard.other_prompt_message_id is not None
                and reply_to_id == wizard.other_prompt_message_id
                and text.strip()
                and not has_media
            ):
                await self._accept_other_answer(wizard, actor, text.strip())
            else:
                await self.send_text(conversation, _WIZARD_REMINDER)
            return

        if (
            reply_to_id is not None
            and (
                conversation,
                reply_to_id,
            )
            in self._closed_replies
        ):
            await self.send_text(conversation, _INACTIVE_REPLY_TEXT)
            return

        media_group_id = message.get("media_group_id")
        if isinstance(media_group_id, str) and media_group_id:
            if self._remember_album(media_group_id):
                await self.send_text(conversation, _ALBUM_MESSAGE_TEXT)
            return

        photo = message.get("photo")
        document = message.get("document")
        unsupported = any(field in message for field in _UNSUPPORTED_MESSAGE_FIELDS)
        if unsupported or (photo is not None and document is not None):
            await self.send_text(conversation, _UNSUPPORTED_MESSAGE_TEXT)
            return

        images: tuple[InboundImage, ...] = ()
        files: tuple[InboundFile, ...] = ()
        inbound_text = text
        try:
            if photo is not None:
                selected_photo = _largest_photo(photo)
                file_id = _required_str(selected_photo, "file_id", "Telegram photo")
                size = _optional_size(selected_photo, "Telegram photo")
                data = await self._api.get_file(file_id, known_size=size)
                images = (InboundImage(data=data, media_type="image/jpeg"),)
                inbound_text = caption
            elif document is not None:
                if not isinstance(document, dict):
                    raise TelegramProtocolError("Telegram document must be an object")
                file_id = _required_str(document, "file_id", "Telegram document")
                size = _optional_size(document, "Telegram document")
                data = await self._api.get_file(file_id, known_size=size)
                name = document.get("file_name")
                if not isinstance(name, str) or not name.strip():
                    unique_id = document.get("file_unique_id")
                    suffix = unique_id if isinstance(unique_id, str) else file_id
                    name = f"telegram-document-{suffix}"
                media_type = document.get("mime_type")
                if not isinstance(media_type, str) or not media_type:
                    media_type = "application/octet-stream"
                files = (InboundFile(data=data, name=name, media_type=media_type),)
                inbound_text = caption
            elif not text.strip():
                await self.send_text(conversation, _UNSUPPORTED_MESSAGE_TEXT)
                return
        except TelegramFileTooLarge:
            await self.send_text(conversation, _OVERSIZE_MESSAGE_TEXT)
            return

        await self._deliver_message(
            conversation,
            actor,
            message_id,
            inbound_text,
            message,
            images=images,
            files=files,
        )

    async def _deliver_message(
        self,
        conversation: ConversationRef,
        actor: ActorRef,
        message_id: int,
        text: str,
        raw: dict[str, Any],
        *,
        images: tuple[InboundImage, ...] = (),
        files: tuple[InboundFile, ...] = (),
    ) -> None:
        if self._on_message is None:
            raise RuntimeError("Telegram adapter has not been started")
        created = raw.get("date")
        timestamp = (
            float(created)
            if isinstance(created, int) and not isinstance(created, bool)
            else 0.0
        )
        await self._on_message(
            self,
            InboundMessage(
                conversation=conversation,
                actor=actor,
                message_id=str(message_id),
                text=text,
                timestamp=timestamp,
                images=images,
                files=files,
            ),
        )

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = callback.get("id")
        if not isinstance(query_id, str) or not query_id:
            return
        data = callback.get("data")
        action = self._callbacks.get(data) if isinstance(data, str) else None
        try:
            await self._api.request(
                "answerCallbackQuery",
                {
                    "callback_query_id": query_id,
                    **(
                        {"text": "This button is no longer active."}
                        if action is None
                        else {}
                    ),
                },
            )
        except TelegramAPIError as exc:
            if not _callback_query_expired(exc):
                raise

        message = callback.get("message")
        sender = callback.get("from")
        if not isinstance(message, dict) or not isinstance(sender, dict):
            return
        identity = self._message_identity(message, sender=sender)
        if identity is None:
            return
        conversation, actor = identity
        message_id = _required_int(message, "message_id", "Telegram callback")
        source = MessageRef(conversation, str(message_id))
        state = self._interactions.get(source)

        if action is None or state is None or action.state is not state:
            if state is not None:
                if (
                    isinstance(state, _QuestionWizard)
                    and not state.submitting
                    and state.other_prompt_message_id is None
                ):
                    await self._render_question(state)
                return
            await self._deliver_stale_callback(source, actor)
            return
        if isinstance(state, _ApprovalState):
            if action.kind != "approval" or action.value is None:
                return
            response = ApprovalResponse(cast(ApprovalDecision, action.value))
            await self._deliver_interaction(
                source, actor, state.prompt.interaction_id, response
            )
            return
        await self._handle_question_callback(state, action, actor)

    async def _handle_question_callback(
        self,
        wizard: _QuestionWizard,
        action: _CallbackAction,
        actor: ActorRef,
    ) -> None:
        if wizard.submitting or action.question_index != wizard.question_index:
            if not wizard.submitting:
                await self._render_question(wizard)
            return
        question = wizard.prompt.request.questions[wizard.question_index]
        if action.kind == "option" and action.value is not None:
            option_ids = {option.id for option in question.options}
            if action.value not in option_ids:
                raise TelegramProtocolError(
                    "Telegram question callback refers to an unknown option"
                )
            if question.multi_select:
                if action.value in wizard.selected:
                    wizard.selected.remove(action.value)
                else:
                    wizard.selected.add(action.value)
                await self._render_question(wizard)
            else:
                await self._advance_question(
                    wizard,
                    actor,
                    SingleChoiceAnswer(question.id, action.value),
                )
            return
        if action.kind == "done" and question.multi_select:
            if not wizard.selected:
                await self.send_text(
                    wizard.conversation,
                    "Choose at least one option or use Skip.",
                )
                return
            await self._advance_question(
                wizard,
                actor,
                MultipleChoiceAnswer(
                    question.id, _ordered_selected(question, wizard.selected)
                ),
            )
            return
        if action.kind == "skip":
            await self._advance_question(wizard, actor, SkippedAnswer(question.id))
            return
        if action.kind == "other" and question.allow_other:
            await self._begin_other_answer(wizard)

    async def _begin_other_answer(self, wizard: _QuestionWizard) -> None:
        assert wizard.message is not None
        self._clear_callback_tokens(wizard)
        await self._edit_message(
            wizard.message,
            _truncate(
                f"{_question_text(wizard)}\n\nReply to the next message with "
                "your custom answer."
            ),
            reply_markup={"inline_keyboard": []},
        )
        result = await self._api.request(
            "sendMessage",
            {
                "chat_id": _chat_id(wizard.conversation),
                "text": "Enter your custom answer:",
                "reply_markup": {
                    "force_reply": True,
                    "input_field_placeholder": "Custom answer",
                },
            },
        )
        prompt_message = _message_ref(wizard.conversation, result, "sendMessage")
        wizard.other_prompt_message_id = prompt_message.message_id

    async def _accept_other_answer(
        self, wizard: _QuestionWizard, actor: ActorRef, text: str
    ) -> None:
        question = wizard.prompt.request.questions[wizard.question_index]
        if wizard.other_prompt_message_id is not None:
            self._remember_closed_reply(
                wizard.conversation, wizard.other_prompt_message_id
            )
        wizard.other_prompt_message_id = None
        if question.multi_select:
            answer: QuestionAnswer = MultipleChoiceWithOtherAnswer(
                question.id,
                _ordered_selected(question, wizard.selected),
                text,
            )
        else:
            answer = OtherAnswer(question.id, text)
        await self._advance_question(wizard, actor, answer)

    async def _advance_question(
        self,
        wizard: _QuestionWizard,
        actor: ActorRef,
        answer: QuestionAnswer,
    ) -> None:
        wizard.answers.append(answer)
        wizard.question_index += 1
        wizard.selected.clear()
        wizard.other_prompt_message_id = None
        if wizard.question_index < len(wizard.prompt.request.questions):
            await self._render_question(wizard)
            return
        wizard.submitting = True
        self._clear_callback_tokens(wizard)
        assert wizard.message is not None
        await self._edit_message(
            wizard.message,
            "Submitting answers…",
            reply_markup={"inline_keyboard": []},
        )
        await self._deliver_interaction(
            wizard.message,
            actor,
            wizard.prompt.interaction_id,
            QuestionResponse(tuple(wizard.answers)),
        )

    async def _render_question(self, wizard: _QuestionWizard) -> None:
        assert wizard.message is not None
        self._clear_callback_tokens(wizard)
        await self._edit_message(
            wizard.message,
            _question_text(wizard),
            reply_markup=self._question_markup(wizard),
        )

    async def _deliver_stale_callback(
        self, source: MessageRef, actor: ActorRef
    ) -> None:
        await self._deliver_interaction(source, actor, None, None)

    async def _deliver_interaction(
        self,
        source: MessageRef,
        actor: ActorRef,
        interaction_id: str | None,
        response: ApprovalResponse | QuestionResponse | None,
    ) -> None:
        if self._on_interaction is None:
            raise RuntimeError("Telegram adapter has not been started")
        await self._on_interaction(
            self,
            InboundInteraction(
                source=source,
                actor=actor,
                interaction_id=interaction_id,
                response=response,
            ),
        )

    def _approval_markup(self, state: _ApprovalState) -> dict[str, Any]:
        buttons = []
        for label, decision in (
            ("Approve", "approved"),
            ("Reject", "rejected"),
            ("Cancel", "cancelled"),
        ):
            token = self._register_callback(state, "approval", value=decision)
            buttons.append({"text": label, "callback_data": token})
        return {"inline_keyboard": [buttons]}

    def _question_markup(self, wizard: _QuestionWizard) -> dict[str, Any]:
        question = wizard.prompt.request.questions[wizard.question_index]
        rows: list[list[dict[str, str]]] = []
        for option in question.options:
            selected = option.id in wizard.selected
            label = f"✓ {option.label}" if selected else option.label
            token = self._register_callback(
                wizard,
                "option",
                question_index=wizard.question_index,
                value=option.id,
            )
            rows.append([{"text": _button_text(label), "callback_data": token}])
        if question.multi_select:
            done = self._register_callback(
                wizard, "done", question_index=wizard.question_index
            )
            rows.append([{"text": "Done", "callback_data": done}])
        final_row: list[dict[str, str]] = []
        if question.allow_other:
            other = self._register_callback(
                wizard, "other", question_index=wizard.question_index
            )
            final_row.append(
                {
                    "text": _button_text(question.other_label or "Other"),
                    "callback_data": other,
                }
            )
        skip = self._register_callback(
            wizard, "skip", question_index=wizard.question_index
        )
        final_row.append({"text": "Skip", "callback_data": skip})
        rows.append(final_row)
        return {"inline_keyboard": rows}

    def _register_callback(
        self,
        state: _InteractionState,
        kind: str,
        *,
        question_index: int | None = None,
        value: str | None = None,
    ) -> str:
        while True:
            token = f"kb:{secrets.token_urlsafe(9)}"
            if token not in self._callbacks:
                break
        if len(token.encode("utf-8")) > 64:
            raise AssertionError("Telegram callback token exceeds 64 bytes")
        self._callbacks[token] = _CallbackAction(
            state,
            kind,
            question_index=question_index,
            value=value,
        )
        state.callback_tokens.add(token)
        return token

    def _clear_callback_tokens(self, state: _InteractionState) -> None:
        for token in state.callback_tokens:
            self._callbacks.pop(token, None)
        state.callback_tokens.clear()

    async def _edit_message(
        self,
        message: MessageRef,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": _chat_id(message.conversation),
            "message_id": _message_id(message),
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._api.request("editMessageText", payload)

    def _message_identity(
        self,
        message: dict[str, Any],
        *,
        sender: dict[str, Any] | None = None,
    ) -> tuple[ConversationRef, ActorRef] | None:
        if self._bot_id is None:
            raise RuntimeError("Telegram adapter has not been started")
        chat = message.get("chat")
        sender = sender if sender is not None else message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return None
        if (
            chat.get("type") != "private"
            or sender.get("is_bot") is True
            or "message_thread_id" in message
        ):
            return None
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or isinstance(user_id, bool)
            or not isinstance(user_id, int)
        ):
            return None
        if user_id not in self._allowed_users:
            return None
        name_parts = [
            value.strip()
            for value in (sender.get("first_name"), sender.get("last_name"))
            if isinstance(value, str) and value.strip()
        ]
        name = " ".join(name_parts) or None
        conversation = ConversationRef("telegram", self._bot_id, str(chat_id))
        actor_id = str(user_id)
        self._conversation_actors[conversation] = actor_id
        return conversation, ActorRef(actor_id, name)

    def _validate_conversation(self, conversation: ConversationRef) -> None:
        if conversation.platform != "telegram":
            raise ValueError("Telegram adapter received another platform")
        if self._bot_id is not None and conversation.bot_id != self._bot_id:
            raise ValueError("Telegram conversation belongs to another bot")

    def _actor_for_conversation(self, conversation: ConversationRef) -> str:
        actor_id = self._conversation_actors.get(conversation)
        if actor_id is None:
            raise ValueError("Telegram conversation has no allowlisted actor")
        return actor_id

    def _remember_album(self, media_group_id: str) -> bool:
        if media_group_id in self._seen_albums:
            return False
        if len(self._album_order) >= _ALBUM_MEMORY:
            expired = self._album_order.popleft()
            self._seen_albums.discard(expired)
        self._album_order.append(media_group_id)
        self._seen_albums.add(media_group_id)
        return True

    def _remember_closed_reply(
        self, conversation: ConversationRef, message_id: str
    ) -> None:
        key = (conversation, message_id)
        if key in self._closed_replies:
            return
        if len(self._closed_reply_order) >= _CLOSED_REPLY_MEMORY:
            expired = self._closed_reply_order.popleft()
            self._closed_replies.discard(expired)
        self._closed_reply_order.append(key)
        self._closed_replies.add(key)


def _response_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return None


def _api_error(
    method: str, status_code: int, envelope: object
) -> TelegramAPIError | None:
    if isinstance(envelope, dict) and envelope.get("ok") is False:
        error_code = envelope.get("error_code", status_code)
        if isinstance(error_code, bool) or not isinstance(error_code, int):
            error_code = status_code
        description = envelope.get("description")
        if not isinstance(description, str) or not description:
            description = f"HTTP {status_code}"
        return TelegramAPIError(
            method,
            error_code,
            description,
            retry_after=_retry_after(envelope),
        )
    return None


def _retry_after(envelope: object) -> float | None:
    if not isinstance(envelope, dict):
        return None
    parameters = envelope.get("parameters")
    if not isinstance(parameters, dict):
        return None
    value = parameters.get("retry_after")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _message_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        raise ValueError("Telegram message text must be non-empty")
    if len(text) > TELEGRAM_TEXT_LIMIT:
        raise ValueError("Telegram message text exceeds 4096 characters")
    return text


def _truncate(text: str, limit: int = _MAX_INTERACTION_TEXT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _button_text(text: str) -> str:
    clean = text.strip() or "Option"
    return _truncate(clean, 80)


def _chat_id(conversation: ConversationRef) -> int:
    try:
        return int(conversation.conversation_id)
    except ValueError as exc:
        raise ValueError("Telegram conversation id must be numeric") from exc


def _message_id(message: MessageRef) -> int:
    try:
        return int(message.message_id)
    except ValueError as exc:
        raise ValueError("Telegram message id must be numeric") from exc


def _message_ref(
    conversation: ConversationRef, result: object, method: str
) -> MessageRef:
    if not isinstance(result, dict):
        raise TelegramProtocolError(f"Telegram {method} result must be a message")
    message_id = result.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise TelegramProtocolError(
            f"Telegram {method} result has an invalid message_id"
        )
    return MessageRef(conversation, str(message_id))


def _required_int(value: dict[str, Any], key: str, context: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise TelegramProtocolError(f"{context} omitted integer {key}")
    return item


def _required_str(value: dict[str, Any], key: str, context: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise TelegramProtocolError(f"{context} omitted string {key}")
    return item


def _optional_size(value: dict[str, Any], context: str) -> int | None:
    size = value.get("file_size")
    if size is None:
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise TelegramProtocolError(f"{context} has an invalid file_size")
    if size > TELEGRAM_FILE_LIMIT:
        raise TelegramFileTooLarge(f"{context} exceeds 20 MB")
    return size


def _largest_photo(value: object) -> dict[str, Any]:
    if not isinstance(value, list) or not value:
        raise TelegramProtocolError("Telegram photo must contain sizes")
    photos = [item for item in value if isinstance(item, dict)]
    if not photos:
        raise TelegramProtocolError("Telegram photo sizes must be objects")

    def score(photo: dict[str, Any]) -> tuple[int, int]:
        size = photo.get("file_size")
        width = photo.get("width")
        height = photo.get("height")
        byte_size = size if isinstance(size, int) and not isinstance(size, bool) else 0
        area = (
            width * height
            if isinstance(width, int)
            and not isinstance(width, bool)
            and isinstance(height, int)
            and not isinstance(height, bool)
            else 0
        )
        return byte_size, area

    return max(photos, key=score)


def _reply_to_message_id(message: dict[str, Any]) -> str | None:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return None
    message_id = reply.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        return None
    return str(message_id)


def _approval_text(prompt: ApprovalPrompt) -> str:
    request = prompt.request
    summary = ""
    if request.input_display is not None:
        summary = json.dumps(
            request.input_display,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    parts = [
        "Approval requested",
        f"Session: {prompt.session_title}",
        f"Workspace: {prompt.workspace}",
        f"Tool: {request.tool_name}",
        f"Action: {request.action}",
    ]
    if summary:
        parts.extend(("", summary))
    return _truncate("\n".join(parts))


def _question_text(wizard: _QuestionWizard) -> str:
    questions = wizard.prompt.request.questions
    question = questions[wizard.question_index]
    parts = [
        "Question from Kimi",
        f"Session: {wizard.prompt.session_title}",
        f"Workspace: {wizard.prompt.workspace}",
        f"Question {wizard.question_index + 1} of {len(questions)}",
    ]
    if question.header:
        parts.extend(("", question.header))
    parts.extend(("", question.text))
    if question.body:
        parts.append(question.body)
    descriptions = [
        f"{option.label}: {option.description}"
        for option in question.options
        if option.description
    ]
    if descriptions:
        parts.extend(("", *descriptions))
    if question.multi_select:
        parts.extend(("", "Select one or more options, then choose Done."))
    return _truncate("\n".join(parts))


def _ordered_selected(question: Question, selected: set[str]) -> tuple[str, ...]:
    return tuple(option.id for option in question.options if option.id in selected)


def _terminal_edit_unavailable(exc: TelegramAPIError) -> bool:
    if exc.error_code != 400:
        return False
    description = exc.description.lower()
    return any(
        marker in description
        for marker in (
            "message is not modified",
            "message to edit not found",
            "message can't be edited",
        )
    )


def _callback_query_expired(exc: TelegramAPIError) -> bool:
    description = exc.description.lower()
    return exc.error_code == 400 and (
        "query is too old" in description or "query id is invalid" in description
    )
