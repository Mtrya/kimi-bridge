from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from kimi_bridge.platforms.base import CardAction, InboundMessage
from kimi_bridge.platforms.feishu import (
    FeishuAdapter,
    UNSUPPORTED_MESSAGE,
    _DownloadedResource,
    _LarkTextTransport,
    _LarkWebSocketRunner,
)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.edited: list[tuple[str, str]] = []
        self.sent_cards: list[tuple[str, str, dict[str, Any]]] = []
        self.edited_cards: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[tuple[str, str, str, str | None]] = []
        self.resources: dict[str, _DownloadedResource] = {}

    async def send_text(self, receive_id: str, receive_id_type: str, text: str) -> str:
        self.sent.append((receive_id, receive_id_type, text))
        return f"message-{len(self.sent)}"

    async def edit_text(self, message_id: str, text: str) -> None:
        self.edited.append((message_id, text))

    async def send_card(
        self,
        receive_id: str,
        receive_id_type: str,
        card: dict[str, Any],
    ) -> str:
        self.sent_cards.append((receive_id, receive_id_type, card))
        return f"card-{len(self.sent_cards)}"

    async def edit_card(self, message_id: str, card: dict[str, Any]) -> None:
        self.edited_cards.append((message_id, card))

    async def download_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
        *,
        name: str | None = None,
    ) -> _DownloadedResource:
        self.downloads.append((message_id, file_key, resource_type, name))
        resource = self.resources[file_key]
        if name is None:
            return resource
        return _DownloadedResource(resource.data, name, resource.media_type)


class FakeWebSocketRunner:
    def __init__(self, message_callback: Any, card_callback: Any) -> None:
        self.message_callback = message_callback
        self.card_callback = card_callback
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
    content: dict[str, Any] | None = None,
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
                chat_id="oc_direct",
                chat_type=chat_type,
                message_type=message_type,
                content=json.dumps(content or {"text": "hello"}),
                create_time=int(time.time() * 1000),
            ),
        ),
    )


def _card_event(
    *,
    open_id: str = "ou_allowed",
    user_id: str = "user_allowed",
    chat_id: str = "oc_direct",
    message_id: str = "om_card",
    value: dict[str, Any] | None = None,
    form_value: dict[str, Any] | None = None,
) -> Any:
    return SimpleNamespace(
        header=SimpleNamespace(
            app_id="cli_event", event_id=f"evt-{message_id}-{open_id}"
        ),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=open_id, user_id=user_id),
            context=SimpleNamespace(open_message_id=message_id, open_chat_id=chat_id),
            action=SimpleNamespace(
                value=value,
                form_value=form_value,
                name="submit",
            ),
        ),
    )


async def _discard_card(_adapter: Any, _action: CardAction) -> None:
    pass


async def test_allowlisted_p2p_text_is_normalized_once() -> None:
    transport = FakeTransport()
    runners: list[FakeWebSocketRunner] = []
    received: list[InboundMessage] = []

    def factory(message_callback: Any, card_callback: Any) -> FakeWebSocketRunner:
        runner = FakeWebSocketRunner(message_callback, card_callback)
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
    await adapter.start(on_message, _discard_card)
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
                message_id="om_1",
                conversation_id="oc_direct",
            )
        ]
        assert await adapter.send_text("ou_allowed", "reply") == "message-1"
        await adapter.edit_text("message-1", "updated")
        card = {"schema": "2.0", "body": {"elements": []}}
        assert await adapter.send_card("ou_allowed", card) == "card-1"
        await adapter.edit_card("card-1", card)
        assert transport.sent == [("ou_allowed", "open_id", "reply")]
        assert transport.edited == [("message-1", "updated")]
        assert transport.sent_cards == [("ou_allowed", "open_id", card)]
        assert transport.edited_cards == [("card-1", card)]
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
    runner = _LarkWebSocketRunner(
        "cli_test",
        "secret",
        lambda _event: None,
        lambda _event: None,
    )

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


async def test_sdk_transport_builds_message_card_and_resource_requests() -> None:
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

        async def apatch(self, request: Any) -> Any:
            requests.append(("patch", request))
            return SimpleNamespace(success=lambda: True)

    class ResourceAPI:
        async def aget(self, request: Any) -> Any:
            requests.append(("resource", request))
            return SimpleNamespace(
                success=lambda: True,
                file=io.BytesIO(b"image-data"),
                file_name="photo.png",
                raw=SimpleNamespace(headers={"content-type": "image/png"}),
            )

    transport = _LarkTextTransport("cli_test", "secret")
    transport._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(message=MessageAPI(), message_resource=ResourceAPI())
        )
    )

    assert await transport.send_text("ou_user", "open_id", "hello") == "om_reply"
    await transport.edit_text("om_reply", "updated")
    card = {"schema": "2.0", "body": {"elements": []}}
    assert await transport.send_card("ou_user", "open_id", card) == "om_reply"
    await transport.edit_card("om_reply", card)
    resource = await transport.download_resource("om_source", "img_key", "image")

    create_text = requests[0][1]
    assert create_text.receive_id_type == "open_id"
    assert create_text.request_body.receive_id == "ou_user"
    assert create_text.request_body.msg_type == "text"
    assert json.loads(create_text.request_body.content) == {"text": "hello"}
    update_text = requests[1][1]
    assert update_text.message_id == "om_reply"
    assert update_text.request_body.msg_type == "text"
    assert json.loads(update_text.request_body.content) == {"text": "updated"}
    assert requests[2][1].request_body.msg_type == "interactive"
    assert json.loads(requests[2][1].request_body.content) == card
    assert requests[3][0] == "patch"
    assert requests[3][1].message_id == "om_reply"
    assert not hasattr(requests[3][1].request_body, "msg_type")
    assert json.loads(requests[3][1].request_body.content) == card
    download = requests[4][1]
    assert (download.message_id, download.file_key, download.type) == (
        "om_source",
        "img_key",
        "image",
    )
    assert resource == _DownloadedResource(b"image-data", "photo.png", "image/png")


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
    await adapter.start(
        lambda _adapter, message: _append(received, message),
        _discard_card,
    )
    try:
        await adapter.handle_event(_event(message_id="om_group", chat_type="group"))
        await adapter.handle_event(_event(message_id="om_bot", sender_type="app"))
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


async def test_image_file_and_multi_image_post_are_downloaded() -> None:
    transport = FakeTransport()
    transport.resources = {
        "img_one": _DownloadedResource(b"one", "one.png", "image/png"),
        "img_two": _DownloadedResource(b"two", "two.jpg", "image/jpeg"),
        "file_one": _DownloadedResource(b"file", "server-name.txt", "text/plain"),
    }
    received: list[InboundMessage] = []
    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"ou_allowed"},
        transport=transport,
        ws_factory=FakeWebSocketRunner,
    )
    await adapter.start(
        lambda _adapter, message: _append(received, message),
        _discard_card,
    )
    try:
        await adapter.handle_event(
            _event(
                message_id="om_image",
                message_type="image",
                content={"image_key": "img_one"},
            )
        )
        await adapter.handle_event(
            _event(
                message_id="om_file",
                message_type="file",
                content={"file_key": "file_one", "file_name": "notes.txt"},
            )
        )
        await adapter.handle_event(
            _event(
                message_id="om_post",
                message_type="post",
                content={
                    "title": "Compare",
                    "content": [
                        [
                            {"tag": "img", "image_key": "img_one"},
                            {"tag": "text", "text": " and "},
                            {"tag": "img", "image_key": "img_two"},
                        ]
                    ],
                },
            )
        )
    finally:
        await adapter.stop()

    assert received[0].images[0].data == b"one"
    assert received[1].files[0].name == "notes.txt"
    assert received[1].files[0].data == b"file"
    assert received[2].text == "Compare\n and "
    assert [image.data for image in received[2].images] == [b"one", b"two"]
    assert transport.downloads == [
        ("om_image", "img_one", "image", None),
        ("om_file", "file_one", "file", "notes.txt"),
        ("om_post", "img_one", "image", None),
        ("om_post", "img_two", "image", None),
    ]


async def test_card_callback_is_normalized_and_non_allowlisted_is_silent() -> None:
    transport = FakeTransport()
    actions: list[CardAction] = []
    runners: list[FakeWebSocketRunner] = []

    def factory(message_callback: Any, card_callback: Any) -> FakeWebSocketRunner:
        runner = FakeWebSocketRunner(message_callback, card_callback)
        runners.append(runner)
        return runner

    async def on_card(_adapter: Any, action: CardAction) -> None:
        actions.append(action)

    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"ou_allowed"},
        transport=transport,
        ws_factory=factory,
    )
    await adapter.start(lambda _adapter, _message: _noop(), on_card)
    try:
        response = runners[0].card_callback(_card_event(value={"decision": "approved"}))
        assert response is not None
        await _wait_for(lambda: len(actions) == 1)
        await adapter.handle_card_action_event(
            _card_event(open_id="ou_denied", user_id="user_denied")
        )
    finally:
        await adapter.stop()

    assert actions == [
        CardAction(
            platform="feishu",
            bot_id="cli_event",
            user_id="ou_allowed",
            conversation_id="oc_direct",
            message_id="om_card",
            value={"decision": "approved"},
            action_name="submit",
        )
    ]


async def test_other_message_type_gets_supported_types_notice() -> None:
    transport = FakeTransport()
    received: list[InboundMessage] = []
    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"ou_allowed"},
        transport=transport,
        ws_factory=FakeWebSocketRunner,
    )
    await adapter.start(
        lambda _adapter, message: _append(received, message),
        _discard_card,
    )
    try:
        await adapter.handle_event(_event(message_id="om_audio", message_type="audio"))
    finally:
        await adapter.stop()

    assert received == []
    assert transport.sent == [("ou_allowed", "open_id", UNSUPPORTED_MESSAGE)]


async def _append(items: list[Any], item: Any) -> None:
    items.append(item)


async def _noop() -> None:
    pass


async def _wait_for(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")
