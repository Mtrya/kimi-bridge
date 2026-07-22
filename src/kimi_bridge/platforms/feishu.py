"""Feishu adapter using the official long-connection SDK."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol

from ..interactions import InteractionOutcome, InteractionPrompt
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
    OutboundFile,
)
from .feishu_cards import (
    decode_interaction_response,
    interaction_id_from_value,
    render_interaction,
    render_outcome,
)


LOGGER = logging.getLogger(__name__)
UNSUPPORTED_MESSAGE = "Unsupported message type; send text, an image, or a file."
FEISHU_TEXT_LIMIT = 7000
FEISHU_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/bmp",
        "image/gif",
        "image/heic",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/vnd.microsoft.icon",
        "image/webp",
        "image/x-icon",
    }
)
VIDEO_COVER_NAME = "video-cover.png"


class FeishuAPIError(RuntimeError):
    """A Feishu message operation failed."""


@dataclass(frozen=True, slots=True)
class _DownloadedResource:
    data: bytes
    name: str
    media_type: str


class _FeishuTransport(Protocol):
    async def send_text(
        self, receive_id: str, receive_id_type: str, text: str
    ) -> str: ...

    async def edit_text(self, message_id: str, text: str) -> None: ...

    async def upload_image(self, file: OutboundFile) -> str: ...

    async def upload_file(self, file: OutboundFile, file_type: str) -> str: ...

    async def send_media(
        self,
        receive_id: str,
        receive_id_type: str,
        message_type: str,
        content: dict[str, str],
    ) -> str: ...

    async def send_card(
        self, receive_id: str, receive_id_type: str, card: dict[str, Any]
    ) -> str: ...

    async def edit_card(self, message_id: str, card: dict[str, Any]) -> None: ...

    async def download_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
        *,
        name: str | None = None,
    ) -> _DownloadedResource: ...


class _WebSocketRunner(Protocol):
    def run(self) -> None: ...
    def stop(self) -> None: ...


class _LarkTransport:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError(
                "Feishu support is not installed; run 'uv sync --extra feishu'"
            ) from exc
        self._client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .build()
        )
        return self._client

    async def send_text(self, receive_id: str, receive_id_type: str, text: str) -> str:
        return await self._send_message(
            receive_id,
            receive_id_type,
            "post",
            _post_content(text),
        )

    async def _send_message(
        self,
        receive_id: str,
        receive_id_type: str,
        message_type: str,
        content: dict[str, Any],
    ) -> str:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(message_type)
            .content(json.dumps(content, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        client = self._get_client()
        response = await client.im.v1.message.acreate(request)
        _raise_for_response(response, "send message")
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not isinstance(message_id, str) or not message_id:
            raise FeishuAPIError("Feishu send response omitted message_id")
        return message_id

    async def edit_text(self, message_id: str, text: str) -> None:
        from lark_oapi.api.im.v1 import (
            UpdateMessageRequest,
            UpdateMessageRequestBody,
        )

        body = (
            UpdateMessageRequestBody.builder()
            .msg_type("post")
            .content(json.dumps(_post_content(text), ensure_ascii=False))
            .build()
        )
        request = (
            UpdateMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        client = self._get_client()
        response = await client.im.v1.message.aupdate(request)
        _raise_for_response(response, "edit message")

    async def upload_image(self, file: OutboundFile) -> str:
        from lark_oapi.api.im.v1 import (
            CreateImageRequest,
            CreateImageRequestBody,
        )

        body = (
            CreateImageRequestBody.builder()
            .image_type("message")
            .image(io.BytesIO(file.data))
            .build()
        )
        request = CreateImageRequest.builder().request_body(body).build()
        client = self._get_client()
        response = await client.im.v1.image.acreate(request)
        _raise_for_response(response, "upload image")
        image_key = getattr(getattr(response, "data", None), "image_key", None)
        if not isinstance(image_key, str) or not image_key:
            raise FeishuAPIError("Feishu image-upload response omitted image_key")
        return image_key

    async def upload_file(self, file: OutboundFile, file_type: str) -> str:
        from lark_oapi.api.im.v1 import (
            CreateFileRequest,
            CreateFileRequestBody,
        )

        body = (
            CreateFileRequestBody.builder()
            .file_type(file_type)
            .file_name(file.name)
            .file(io.BytesIO(file.data))
            .build()
        )
        request = CreateFileRequest.builder().request_body(body).build()
        client = self._get_client()
        response = await client.im.v1.file.acreate(request)
        _raise_for_response(response, "upload file")
        file_key = getattr(getattr(response, "data", None), "file_key", None)
        if not isinstance(file_key, str) or not file_key:
            raise FeishuAPIError("Feishu file-upload response omitted file_key")
        return file_key

    async def send_media(
        self,
        receive_id: str,
        receive_id_type: str,
        message_type: str,
        content: dict[str, str],
    ) -> str:
        return await self._send_message(
            receive_id,
            receive_id_type,
            message_type,
            content,
        )

    async def send_card(
        self, receive_id: str, receive_id_type: str, card: dict[str, Any]
    ) -> str:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        client = self._get_client()
        response = await client.im.v1.message.acreate(request)
        _raise_for_response(response, "send card")
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not isinstance(message_id, str) or not message_id:
            raise FeishuAPIError("Feishu send-card response omitted message_id")
        return message_id

    async def edit_card(self, message_id: str, card: dict[str, Any]) -> None:
        from lark_oapi.api.im.v1 import (
            PatchMessageRequest,
            PatchMessageRequestBody,
        )

        body = (
            PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        client = self._get_client()
        response = await client.im.v1.message.apatch(request)
        _raise_for_response(response, "edit card")

    async def download_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
        *,
        name: str | None = None,
    ) -> _DownloadedResource:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        client = self._get_client()
        response = await client.im.v1.message_resource.aget(request)
        _raise_for_response(response, "download message resource")
        resource_file = getattr(response, "file", None)
        if resource_file is None:
            raise FeishuAPIError("Feishu resource response omitted file data")
        filename = name or getattr(response, "file_name", None) or file_key
        headers = getattr(getattr(response, "raw", None), "headers", {}) or {}
        content_type = headers.get("content-type") or headers.get("Content-Type")
        if isinstance(content_type, str):
            media_type = content_type.partition(";")[0].strip()
        else:
            media_type = ""
        if not media_type:
            media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return _DownloadedResource(
            data=resource_file.read(),
            name=filename,
            media_type=media_type,
        )


class _LarkWebSocketRunner:
    """Run the SDK's blocking WebSocket client in a worker thread."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        message_callback: Callable[[Any], None],
        card_callback: Callable[[Any], Any],
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._message_callback = message_callback
        self._card_callback = card_callback
        self._client: Any | None = None
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._initialized = threading.Event()
        self._stopping = False

    def run(self) -> None:
        sdk_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(sdk_loop)
        try:
            import lark_oapi as lark
            from lark_oapi.ws import client as ws_module
        except ImportError as exc:
            self._initialized.set()
            sdk_loop.close()
            raise RuntimeError(
                "Feishu support is not installed; run 'uv sync --extra feishu'"
            ) from exc
        previous_sdk_loop = ws_module.loop
        ws_module.loop = sdk_loop
        try:
            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._message_callback)
                .register_p2_card_action_trigger(self._card_callback)
                .build()
            )
            self._client = lark.ws.Client(
                self._app_id,
                self._app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.WARNING,
            )
            self._sdk_loop = sdk_loop
            self._initialized.set()

            def poll_for_stop() -> None:
                if self._stopping:
                    sdk_loop.stop()
                elif not sdk_loop.is_closed():
                    sdk_loop.call_later(0.1, poll_for_stop)

            sdk_loop.call_later(0.1, poll_for_stop)
            if not self._stopping:
                try:
                    self._client.start()
                except BaseException:
                    if not self._stopping:
                        raise
        finally:
            self._initialized.set()
            if not sdk_loop.is_closed():
                pending = asyncio.all_tasks(sdk_loop)
                for task in pending:
                    task.cancel()
                if pending:
                    sdk_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                if self._client is not None:
                    try:
                        sdk_loop.run_until_complete(
                            asyncio.wait_for(self._client._disconnect(), timeout=2.0)
                        )
                    except Exception:
                        LOGGER.warning(
                            "Feishu WebSocket disconnect did not complete cleanly"
                        )
                sdk_loop.close()
            if ws_module.loop is sdk_loop:
                ws_module.loop = previous_sdk_loop

    def stop(self) -> None:
        self._stopping = True
        self._initialized.wait(timeout=2.0)
        client = self._client
        loop = self._sdk_loop
        if client is None or loop is None:
            return
        client._auto_reconnect = False
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            if not loop.is_closed():
                raise


def _raise_for_response(response: Any, operation: str) -> None:
    if response.success():
        return
    raise FeishuAPIError(f"Feishu {operation} failed: {response.code} {response.msg}")


class FeishuAdapter:
    """Receive allowlisted direct messages and send editable text replies."""

    name = "feishu"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        allowed_users: set[str] | frozenset[str],
        *,
        message_limit: int = FEISHU_TEXT_LIMIT,
        transport: _FeishuTransport | None = None,
        ws_factory: Callable[
            [Callable[[Any], None], Callable[[Any], Any]], _WebSocketRunner
        ]
        | None = None,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("Feishu app_id and app_secret are required")
        if message_limit <= 0:
            raise ValueError("message_limit must be positive")
        self._app_id = app_id
        self._app_secret = app_secret
        self._allowed_users = frozenset(allowed_users)
        self.message_limit = message_limit
        self._transport = transport
        self._ws_factory = ws_factory

        self._on_message: InboundHandler | None = None
        self._on_interaction: InteractionHandler | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._ws: _WebSocketRunner | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_error: BaseException | None = None
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        self._presented_interactions: dict[MessageRef, InteractionPrompt] = {}
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._started_at_ms = 0

    async def start(
        self,
        on_message: InboundHandler,
        on_interaction: InteractionHandler,
    ) -> None:
        if self._ws_thread is not None:
            return
        self._on_message = on_message
        self._on_interaction = on_interaction
        self._main_loop = asyncio.get_running_loop()
        self._started_at_ms = int(time.time() * 1000)
        if self._transport is None:
            self._transport = _LarkTransport(self._app_id, self._app_secret)
        factory = self._ws_factory or (
            lambda message_callback, card_callback: _LarkWebSocketRunner(
                self._app_id,
                self._app_secret,
                message_callback,
                card_callback,
            )
        )
        self._ws = factory(self._dispatch_sdk_event, self._dispatch_sdk_card_action)
        self._ws_error = None
        ws = self._ws

        def run_websocket() -> None:
            try:
                ws.run()
            except BaseException as exc:
                self._ws_error = exc

        self._ws_thread = threading.Thread(
            target=run_websocket, name="feishu-websocket"
        )
        self._ws_thread.start()
        await asyncio.sleep(0)
        if not self._ws_thread.is_alive():
            self._raise_ws_error()

    async def wait(self) -> None:
        if self._ws_thread is not None:
            await _join_worker(self._ws_thread)
            self._raise_ws_error()

    async def stop(self) -> None:
        ws = self._ws
        thread = self._ws_thread
        self._ws = None
        self._ws_thread = None
        if ws is not None:
            ws.stop()
        if thread is not None:
            await _join_worker(thread)
        if self._inbound_tasks:
            tasks = tuple(self._inbound_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._raise_ws_error()

    async def send_text(
        self, conversation: ConversationRef, text: str
    ) -> MessageRef:
        if self._transport is None:
            raise RuntimeError("Feishu adapter is not started")
        message_id = await self._transport.send_text(
            conversation.conversation_id,
            "chat_id",
            text,
        )
        return MessageRef(conversation, message_id)

    async def edit_text(self, message: MessageRef, text: str) -> None:
        if self._transport is None:
            raise RuntimeError("Feishu adapter is not started")
        await self._transport.edit_text(message.message_id, text)

    async def send_file(
        self, conversation: ConversationRef, file: OutboundFile
    ) -> MessageRef:
        if self._transport is None:
            raise RuntimeError("Feishu adapter is not started")
        receive_id = conversation.conversation_id
        if file.media_type in FEISHU_IMAGE_MEDIA_TYPES:
            image_key = await self._transport.upload_image(file)
            message_id = await self._transport.send_media(
                receive_id,
                "chat_id",
                "image",
                {"image_key": image_key},
            )
        elif file.media_type == "video/mp4":
            file_key = await self._transport.upload_file(file, "mp4")
            cover = OutboundFile(
                name=VIDEO_COVER_NAME,
                data=_load_video_cover(),
                media_type="image/png",
            )
            image_key = await self._transport.upload_image(cover)
            message_id = await self._transport.send_media(
                receive_id,
                "chat_id",
                "media",
                {"file_key": file_key, "image_key": image_key},
            )
        else:
            file_key = await self._transport.upload_file(
                file, _feishu_file_type(file.name)
            )
            message_id = await self._transport.send_media(
                receive_id,
                "chat_id",
                "file",
                {"file_key": file_key},
            )
        return MessageRef(conversation, message_id)

    async def present_interaction(
        self, conversation: ConversationRef, prompt: InteractionPrompt
    ) -> MessageRef:
        if self._transport is None:
            raise RuntimeError("Feishu adapter is not started")
        message_id = await self._transport.send_card(
            conversation.conversation_id,
            "chat_id",
            render_interaction(prompt),
        )
        message = MessageRef(conversation, message_id)
        self._presented_interactions[message] = prompt
        return message

    async def finish_interaction(
        self, message: MessageRef, outcome: InteractionOutcome
    ) -> None:
        if self._transport is None:
            raise RuntimeError("Feishu adapter is not started")
        try:
            await self._transport.edit_card(
                message.message_id,
                render_outcome(outcome),
            )
        finally:
            self._presented_interactions.pop(message, None)

    async def handle_event(self, data: Any) -> None:
        """Normalize one ``im.message.receive_v1`` SDK event."""

        event = data.event
        sender = event.sender
        message = event.message
        sender_id = sender.sender_id

        if sender.sender_type != "user" or message.chat_type != "p2p":
            return
        identities = {
            value
            for value in (sender_id.open_id, sender_id.user_id)
            if isinstance(value, str) and value
        }
        if not identities.intersection(self._allowed_users):
            LOGGER.info("ignored a message from a non-allowlisted Feishu user")
            return

        message_id = message.message_id
        if not isinstance(message_id, str) or not message_id:
            return
        if self._remember_message(message_id):
            return

        create_time = int(message.create_time or 0)
        if self._started_at_ms and create_time < self._started_at_ms - 30_000:
            LOGGER.info("ignored a stale Feishu message event")
            return

        open_id = sender_id.open_id
        user_id = sender_id.user_id
        recipient = open_id or user_id
        if not isinstance(recipient, str) or not recipient:
            return
        chat_id = message.chat_id
        if not isinstance(chat_id, str) or not chat_id:
            return
        bot_id = getattr(getattr(data, "header", None), "app_id", None)
        conversation = ConversationRef(
            platform=self.name,
            bot_id=bot_id or self._app_id,
            conversation_id=chat_id,
        )

        content = json.loads(message.content)
        if not isinstance(content, dict):
            raise ValueError("Feishu message content must be an object")
        text = ""
        image_keys: list[str] = []
        file_specs: list[tuple[str, str | None]] = []
        if message.message_type == "text":
            text_value = content.get("text")
            if not isinstance(text_value, str):
                raise ValueError("Feishu text message content omitted text")
            text = text_value
        elif message.message_type == "image":
            image_keys = [_required_string(content, "image_key")]
        elif message.message_type == "file":
            file_specs = [
                (
                    _required_string(content, "file_key"),
                    content.get("file_name")
                    if isinstance(content.get("file_name"), str)
                    else None,
                )
            ]
        elif message.message_type == "post":
            text, image_keys = _parse_post_content(content)
        else:
            await self.send_text(conversation, UNSUPPORTED_MESSAGE)
            return

        images = tuple(
            await asyncio.gather(
                *(
                    self._download_image(message_id, image_key)
                    for image_key in image_keys
                )
            )
        )
        files = tuple(
            await asyncio.gather(
                *(
                    self._download_file(message_id, file_key, filename)
                    for file_key, filename in file_specs
                )
            )
        )
        if self._on_message is None:
            raise RuntimeError("Feishu adapter is not started")
        await self._on_message(
            self,
            InboundMessage(
                conversation=conversation,
                actor=ActorRef(recipient),
                message_id=message_id,
                text=text,
                timestamp=create_time / 1000,
                images=images,
                files=files,
            ),
        )

    async def handle_card_action_event(self, data: Any) -> None:
        """Normalize one ``card.action.trigger`` SDK callback."""

        event = data.event
        operator = event.operator
        identities = {
            value
            for value in (operator.open_id, operator.user_id)
            if isinstance(value, str) and value
        }
        if not identities.intersection(self._allowed_users):
            LOGGER.info("ignored a card action from a non-allowlisted Feishu user")
            return

        header = getattr(data, "header", None)
        event_id = getattr(header, "event_id", None)
        if isinstance(event_id, str) and event_id:
            if self._remember_message(f"card:{event_id}"):
                return

        recipient = operator.open_id or operator.user_id
        if not isinstance(recipient, str) or not recipient:
            return
        context = event.context
        message_id = context.open_message_id
        chat_id = context.open_chat_id
        if not isinstance(message_id, str) or not message_id:
            return
        if not isinstance(chat_id, str) or not chat_id:
            return
        if self._on_interaction is None:
            raise RuntimeError("Feishu adapter is not started")
        value = event.action.value
        conversation = ConversationRef(
            platform=self.name,
            bot_id=getattr(header, "app_id", None) or self._app_id,
            conversation_id=chat_id,
        )
        source = MessageRef(conversation, message_id)
        prompt = self._presented_interactions.get(source)
        response = (
            decode_interaction_response(
                prompt,
                value=value,
                form_value=event.action.form_value,
                action_name=event.action.name,
            )
            if prompt is not None
            else None
        )
        await self._on_interaction(
            self,
            InboundInteraction(
                source=source,
                actor=ActorRef(recipient),
                interaction_id=(
                    interaction_id_from_value(value)
                    or (prompt.interaction_id if prompt is not None else None)
                ),
                response=response,
            ),
        )

    async def _download_image(self, message_id: str, image_key: str) -> InboundImage:
        assert self._transport is not None
        resource = await self._transport.download_resource(
            message_id, image_key, "image"
        )
        return InboundImage(resource.data, resource.media_type)

    async def _download_file(
        self, message_id: str, file_key: str, filename: str | None
    ) -> InboundFile:
        assert self._transport is not None
        resource = await self._transport.download_resource(
            message_id, file_key, "file", name=filename
        )
        return InboundFile(resource.data, resource.name, resource.media_type)

    def _dispatch_sdk_event(self, data: Any) -> None:
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._start_inbound_task, data)

    def _dispatch_sdk_card_action(self, data: Any) -> Any:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        loop = self._main_loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._start_card_action_task, data)
        return P2CardActionTriggerResponse()

    def _start_inbound_task(self, data: Any) -> None:
        task = asyncio.create_task(
            self.handle_event(data), name="feishu-inbound-message"
        )
        self._inbound_tasks.add(task)
        task.add_done_callback(self._inbound_done)

    def _start_card_action_task(self, data: Any) -> None:
        task = asyncio.create_task(
            self.handle_card_action_event(data),
            name="feishu-card-action",
        )
        self._inbound_tasks.add(task)
        task.add_done_callback(self._inbound_done)

    def _inbound_done(self, task: asyncio.Task[None]) -> None:
        self._inbound_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("failed to handle Feishu message event")

    def _remember_message(self, message_id: str) -> bool:
        if message_id in self._seen_ids:
            return True
        self._seen_ids.add(message_id)
        self._seen_order.append(message_id)
        if len(self._seen_order) > 2048:
            self._seen_ids.remove(self._seen_order.popleft())
        return False

    def _raise_ws_error(self) -> None:
        error = self._ws_error
        if error is not None:
            self._ws_error = None
            raise error


def _post_content(text: str) -> dict[str, Any]:
    return {
        "zh_cn": {
            "content": [[{"tag": "md", "text": text}]],
        }
    }


def _load_video_cover() -> bytes:
    return (
        files("kimi_bridge")
        .joinpath("assets")
        .joinpath(VIDEO_COVER_NAME)
        .read_bytes()
    )


def _feishu_file_type(filename: str) -> str:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    return {
        "doc": "doc",
        "docx": "doc",
        "mp4": "mp4",
        "opus": "opus",
        "pdf": "pdf",
        "ppt": "ppt",
        "pptx": "ppt",
        "xls": "xls",
        "xlsx": "xls",
    }.get(suffix, "stream")


async def _join_worker(thread: threading.Thread) -> None:
    while thread.is_alive():
        await asyncio.sleep(0.05)
    thread.join()


def _required_string(content: dict[str, Any], key: str) -> str:
    value = content.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Feishu message content omitted {key}")
    return value


def _parse_post_content(content: dict[str, Any]) -> tuple[str, list[str]]:
    blocks = content.get("content")
    if not isinstance(blocks, list):
        raise ValueError("Feishu post message content omitted content blocks")
    text_parts: list[str] = []
    title = content.get("title")
    if isinstance(title, str) and title:
        text_parts.append(title)
    image_keys: list[str] = []
    for block in blocks:
        if not isinstance(block, list):
            continue
        line: list[str] = []
        for element in block:
            if not isinstance(element, dict):
                continue
            if element.get("tag") == "img":
                image_keys.append(_required_string(element, "image_key"))
            elif element.get("tag") in {"text", "a"}:
                value = element.get("text")
                if isinstance(value, str):
                    line.append(value)
        if line:
            text_parts.append("".join(line))
    return "\n".join(text_parts), image_keys
