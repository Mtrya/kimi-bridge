from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from kimi_bridge.platforms.base import InboundMessage
from kimi_bridge.platforms.feishu import (
    FeishuAdapter,
    UNSUPPORTED_MESSAGE,
    _LarkTextTransport,
    _LarkWebSocketRunner,
)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.edited: list[tuple[str, str]] = []

    async def send_text(
        self, receive_id: str, receive_id_type: str, text: str
    ) -> str:
        self.sent.append((receive_id, receive_id_type, text))
        return f"message-{len(self.sent)}"

    async def edit_text(self, message_id: str, text: str) -> None:
        self.edited.append((message_id, text))


class FakeWebSocketRunner:
    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self._stopped = threading.Event()

    def run(self) -> None:
        self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()


def _event(
    *,
    message_id: str,
    open_id: str = "ou_allowed",
    user_id: str = "user_allowed",
    chat_type: str = "p2p",
    message_type: str = "text",
    sender_type: str = "user",
    text: str = "hello",
) -> Any:
    return SimpleNamespace(
        header=SimpleNamespace(app_id="cli_event"),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type=sender_type,
                sender_id=SimpleNamespace(open_id=open_id, user_id=user_id),
            ),
            message=SimpleNamespace(
                message_id=message_id,
                chat_type=chat_type,
                message_type=message_type,
                content=f'{{"text": "{text}"}}',
                create_time=int(time.time() * 1000),
            ),
        ),
    )


async def test_allowlisted_p2p_text_is_normalized_once() -> None:
    transport = FakeTransport()
    runners: list[FakeWebSocketRunner] = []
    received: list[InboundMessage] = []

    def factory(callback: Any) -> FakeWebSocketRunner:
        runner = FakeWebSocketRunner(callback)
        runners.append(runner)
        return runner

    async def on_message(_adapter: Any, message: InboundMessage) -> None:
        received.append(message)

    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"user_allowed"},
        transport=transport,
        ws_factory=factory,
    )
    await adapter.start(on_message)
    try:
        event = _event(message_id="om_1")
        await adapter.handle_event(event)
        await adapter.handle_event(event)

        assert received == [
            InboundMessage(
                platform="feishu",
                bot_id="cli_event",
                user_id="ou_allowed",
                user_name=None,
                text="hello",
                timestamp=event.event.message.create_time / 1000,
            )
        ]
        assert await adapter.send_text("ou_allowed", "reply") == "message-1"
        await adapter.edit_text("message-1", "updated")
        assert transport.sent == [("ou_allowed", "open_id", "reply")]
        assert transport.edited == [("message-1", "updated")]
    finally:
        await adapter.stop()

    assert len(runners) == 1


def test_sdk_websocket_owns_a_worker_event_loop(monkeypatch: Any) -> None:
    lark = pytest.importorskip("lark_oapi")
    from lark_oapi.ws import client as ws_module

    original_loop = ws_module.loop
    observed: list[asyncio.AbstractEventLoop] = []
    started = threading.Event()

    def fake_start(_client: Any) -> None:
        observed.append(ws_module.loop)
        assert asyncio.get_event_loop() is ws_module.loop
        started.set()
        ws_module.loop.run_forever()

    monkeypatch.setattr(lark.ws.Client, "start", fake_start)
    runner = _LarkWebSocketRunner("cli_test", "secret", lambda _event: None)

    thread = threading.Thread(target=runner.run)
    thread.start()
    try:
        assert started.wait(5)
        runner.stop()
        thread.join(3)
    finally:
        runner.stop()
        thread.join(5)

    assert not thread.is_alive()
    assert len(observed) == 1
    assert observed[0] is not original_loop
    assert observed[0].is_closed()
    assert ws_module.loop is original_loop


async def test_sdk_transport_builds_text_create_and_update_requests() -> None:
    pytest.importorskip("lark_oapi")
    requests: list[tuple[str, Any]] = []

    class MessageAPI:
        async def acreate(self, request: Any) -> Any:
            requests.append(("create", request))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om_reply"),
            )

        async def aupdate(self, request: Any) -> Any:
            requests.append(("update", request))
            return SimpleNamespace(success=lambda: True)

    transport = _LarkTextTransport("cli_test", "secret")
    transport._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(message=MessageAPI())
        )
    )

    assert await transport.send_text("ou_user", "open_id", "hello") == "om_reply"
    await transport.edit_text("om_reply", "updated")

    create = requests[0][1]
    assert create.receive_id_type == "open_id"
    assert create.request_body.receive_id == "ou_user"
    assert create.request_body.msg_type == "text"
    assert json.loads(create.request_body.content) == {"text": "hello"}
    update = requests[1][1]
    assert update.message_id == "om_reply"
    assert update.request_body.msg_type == "text"
    assert json.loads(update.request_body.content) == {"text": "updated"}


async def test_groups_bots_and_non_allowlisted_users_are_silent() -> None:
    transport = FakeTransport()
    received: list[InboundMessage] = []
    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"ou_allowed"},
        transport=transport,
        ws_factory=FakeWebSocketRunner,
    )
    await adapter.start(lambda _adapter, message: _append(received, message))
    try:
        await adapter.handle_event(
            _event(message_id="om_group", chat_type="group")
        )
        await adapter.handle_event(
            _event(message_id="om_bot", sender_type="app")
        )
        await adapter.handle_event(
            _event(
                message_id="om_denied",
                open_id="ou_denied",
                user_id="user_denied",
            )
        )
    finally:
        await adapter.stop()

    assert received == []
    assert transport.sent == []


async def test_allowlisted_non_text_message_gets_v1_notice() -> None:
    transport = FakeTransport()
    received: list[InboundMessage] = []
    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"ou_allowed"},
        transport=transport,
        ws_factory=FakeWebSocketRunner,
    )
    await adapter.start(lambda _adapter, message: _append(received, message))
    try:
        await adapter.handle_event(
            _event(message_id="om_image", message_type="image")
        )
    finally:
        await adapter.stop()

    assert received == []
    assert transport.sent == [
        ("ou_allowed", "open_id", UNSUPPORTED_MESSAGE)
    ]


async def _append(items: list[InboundMessage], item: InboundMessage) -> None:
    items.append(item)
