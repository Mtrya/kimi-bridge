"""Feishu adapter using the official long-connection SDK."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from .base import InboundHandler, InboundMessage


LOGGER = logging.getLogger(__name__)
UNSUPPORTED_MESSAGE = "Unsupported in v1; please send text."
FEISHU_TEXT_LIMIT = 7000


class FeishuAPIError(RuntimeError):
    """A Feishu message operation failed."""


class _TextTransport(Protocol):
    async def send_text(
        self, receive_id: str, receive_id_type: str, text: str
    ) -> str: ...

    async def edit_text(self, message_id: str, text: str) -> None: ...


class _WebSocketRunner(Protocol):
    def run(self) -> None: ...
    def stop(self) -> None: ...


class _LarkTextTransport:
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

    async def send_text(
        self, receive_id: str, receive_id_type: str, text: str
    ) -> str:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
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
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
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


class _LarkWebSocketRunner:
    """Run the SDK's blocking WebSocket client in a worker thread."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        callback: Callable[[Any], None],
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._callback = callback
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
                .register_p2_im_message_receive_v1(self._callback)
                .build()
            )
            self._client = lark.ws.Client(
                self._app_id,
                self._app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
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
                if self._client is not None:
                    try:
                        sdk_loop.run_until_complete(
                            asyncio.wait_for(
                                self._client._disconnect(), timeout=2.0
                            )
                        )
                    except Exception:
                        LOGGER.warning(
                            "Feishu WebSocket disconnect did not complete cleanly"
                        )
                pending = asyncio.all_tasks(sdk_loop)
                for task in pending:
                    task.cancel()
                if pending:
                    sdk_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
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
    raise FeishuAPIError(
        f"Feishu {operation} failed: {response.code} {response.msg}"
    )


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
        transport: _TextTransport | None = None,
        ws_factory: Callable[[Callable[[Any], None]], _WebSocketRunner]
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
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._ws: _WebSocketRunner | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_error: BaseException | None = None
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        self._receive_id_types: dict[str, str] = {}
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._started_at_ms = 0

    async def start(self, on_message: InboundHandler) -> None:
        if self._ws_thread is not None:
            return
        self._on_message = on_message
        self._main_loop = asyncio.get_running_loop()
        self._started_at_ms = int(time.time() * 1000)
        if self._transport is None:
            self._transport = _LarkTextTransport(
                self._app_id, self._app_secret
            )
        factory = self._ws_factory or (
            lambda callback: _LarkWebSocketRunner(
                self._app_id, self._app_secret, callback
            )
        )
        self._ws = factory(self._dispatch_sdk_event)
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

    async def send_text(self, user_id: str, text: str) -> str:
        if self._transport is None:
            raise RuntimeError("Feishu adapter is not started")
        receive_id_type = self._receive_id_types.get(
            user_id, "open_id" if user_id.startswith("ou_") else "user_id"
        )
        return await self._transport.send_text(
            user_id, receive_id_type, text
        )

    async def edit_text(self, message_id: str, text: str) -> None:
        if self._transport is None:
            raise RuntimeError("Feishu adapter is not started")
        await self._transport.edit_text(message_id, text)

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
        self._receive_id_types[recipient] = "open_id" if open_id else "user_id"

        if message.message_type != "text":
            await self.send_text(recipient, UNSUPPORTED_MESSAGE)
            return
        content = json.loads(message.content)
        text = content.get("text")
        if not isinstance(text, str):
            raise ValueError("Feishu text message content omitted text")
        if self._on_message is None:
            raise RuntimeError("Feishu adapter is not started")
        bot_id = getattr(getattr(data, "header", None), "app_id", None)
        await self._on_message(
            self,
            InboundMessage(
                platform=self.name,
                bot_id=bot_id or self._app_id,
                user_id=recipient,
                user_name=None,
                text=text,
                timestamp=create_time / 1000,
            ),
        )

    def _dispatch_sdk_event(self, data: Any) -> None:
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._start_inbound_task, data)

    def _start_inbound_task(self, data: Any) -> None:
        task = asyncio.create_task(
            self.handle_event(data), name="feishu-inbound-message"
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


async def _join_worker(thread: threading.Thread) -> None:
    while thread.is_alive():
        await asyncio.sleep(0.05)
    thread.join()
