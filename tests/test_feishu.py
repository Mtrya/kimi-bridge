from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from kimi_bridge.interactions import (
    ApprovalDecision,
    ApprovalPrompt,
    ApprovalRequest,
    ApprovalResponse,
    InteractionOutcome,
    InteractionState,
    MultipleChoiceAnswer,
    MultipleChoiceWithOtherAnswer,
    OtherAnswer,
    Question,
    QuestionOption,
    QuestionPrompt,
    QuestionRequest,
    QuestionResponse,
    SingleChoiceAnswer,
    SkippedAnswer,
)
from kimi_bridge.platforms.base import (
    ActorRef,
    ConversationRef,
    InboundInteraction,
    InboundMessage,
    MessageRef,
    OutboundFile,
)
from kimi_bridge.platforms.feishu import (
    FeishuAPIError,
    FeishuAdapter,
    UNSUPPORTED_MESSAGE,
    _DownloadedResource,
    _LarkTransport,
    _LarkWebSocketRunner,
    _load_video_cover,
)
from kimi_bridge.platforms.feishu_cards import (
    decode_interaction_response,
    render_interaction,
    render_outcome,
)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.edited: list[tuple[str, str]] = []
        self.sent_cards: list[tuple[str, str, dict[str, Any]]] = []
        self.edited_cards: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[tuple[str, str, str, str | None]] = []
        self.resources: dict[str, _DownloadedResource] = {}
        self.uploaded_images: list[OutboundFile] = []
        self.uploaded_files: list[tuple[OutboundFile, str]] = []
        self.sent_media: list[tuple[str, str, str, dict[str, str]]] = []

    async def send_text(self, receive_id: str, receive_id_type: str, text: str) -> str:
        self.sent.append((receive_id, receive_id_type, text))
        return f"message-{len(self.sent)}"

    async def edit_text(self, message_id: str, text: str) -> None:
        self.edited.append((message_id, text))

    async def upload_image(self, file: OutboundFile) -> str:
        self.uploaded_images.append(file)
        return f"img-{len(self.uploaded_images)}"

    async def upload_file(self, file: OutboundFile, file_type: str) -> str:
        self.uploaded_files.append((file, file_type))
        return f"file-{len(self.uploaded_files)}"

    async def send_media(
        self,
        receive_id: str,
        receive_id_type: str,
        message_type: str,
        content: dict[str, str],
    ) -> str:
        self.sent_media.append(
            (receive_id, receive_id_type, message_type, content)
        )
        return f"media-{len(self.sent_media)}"

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


async def _discard_interaction(
    _adapter: Any, _action: InboundInteraction
) -> None:
    pass


def _approval_prompt() -> ApprovalPrompt:
    return ApprovalPrompt(
        interaction_id="interaction-1",
        request=ApprovalRequest(
            id="approval-1",
            session_id="session-1",
            tool_name="Shell",
            action="Run command",
            input_display={"command": "touch approved.txt"},
        ),
        session_title="Test session",
        workspace="/tmp/workspace",
    )


def _question_prompt() -> QuestionPrompt:
    return QuestionPrompt(
        interaction_id="interaction-1",
        request=QuestionRequest(
            id="question-1",
            session_id="session-1",
            questions=(
                Question(
                    id="q1",
                    text="Pick one",
                    header="Choice",
                    options=(
                        QuestionOption("one", "One"),
                        QuestionOption("two", "Two"),
                    ),
                    allow_other=True,
                    other_label="Something else",
                ),
            ),
        ),
        session_title="Test session",
        workspace="/tmp/workspace",
    )


@pytest.mark.parametrize(
    ("state", "template", "tag_color", "icon_token", "icon_color"),
    [
        ("completed", "green", "green", "check_outlined", "green"),
        ("timed_out", "red", "red", "time_outlined", "red"),
        ("stale", "grey", "neutral", "warning_outlined", "grey"),
        ("cancelled", "grey", "neutral", "close_outlined", "grey"),
    ],
)
def test_interaction_outcome_uses_semantic_header(
    state: InteractionState,
    template: str,
    tag_color: str,
    icon_token: str,
    icon_color: str,
) -> None:
    card = render_outcome(InteractionOutcome(state=state, detail="Finished"))

    assert card["header"]["template"] == template
    assert card["header"]["text_tag_list"][0]["color"] == tag_color
    assert card["header"]["icon"] == {
        "tag": "standard_icon",
        "token": icon_token,
        "color": icon_color,
    }


@pytest.mark.parametrize(
    ("decision", "template", "tag_color", "icon_token", "icon_color"),
    [
        ("approved", "green", "green", "check_outlined", "green"),
        ("rejected", "red", "red", "close_outlined", "red"),
        ("cancelled", "grey", "neutral", "close_outlined", "grey"),
    ],
)
def test_approval_outcome_uses_semantic_decision_color(
    decision: ApprovalDecision,
    template: str,
    tag_color: str,
    icon_token: str,
    icon_color: str,
) -> None:
    card = render_outcome(
        InteractionOutcome(
            state="completed",
            detail=decision.title(),
            approval_decision=decision,
        )
    )

    assert card["header"]["template"] == template
    assert card["header"]["text_tag_list"][0]["color"] == tag_color
    assert card["header"]["icon"] == {
        "tag": "standard_icon",
        "token": icon_token,
        "color": icon_color,
    }
    status_blocks = card["body"]["elements"]
    assert len(status_blocks) == 1
    status_lines = status_blocks[0]["columns"][0]["elements"]
    assert [line["text"]["content"] for line in status_lines] == [
        f"Interaction status: {decision.title()}"
    ]


@pytest.mark.parametrize(
    ("input_display", "section"),
    [
        ({"command": "printf hello"}, "Command"),
        ({"path": "src/app.py"}, "Path"),
        ({"path": "src/app.py", "content": "new text"}, "File write"),
        ({"path": "src/app.py", "diff": "- old\n+ new"}, "Diff"),
        (
            {
                "kind": "file_io",
                "operation": "edit",
                "path": "src/app.py",
                "before": "old text\n",
                "after": "new text\n",
            },
            "Diff",
        ),
        ({"unfamiliar": {"nested": True}}, "Input"),
    ],
)
def test_approval_card_has_semantic_bounded_preview_and_stable_callbacks(
    input_display: object, section: str
) -> None:
    prompt = _approval_prompt()
    prompt = ApprovalPrompt(
        interaction_id=prompt.interaction_id,
        request=ApprovalRequest(
            id=prompt.request.id,
            session_id=prompt.request.session_id,
            tool_name=prompt.request.tool_name,
            action=prompt.request.action,
            input_display=input_display,
        ),
        session_title=prompt.session_title,
        workspace="/tmp/workspace/project",
    )

    card = render_interaction(prompt)
    serialized = json.dumps(card, ensure_ascii=False)

    assert card["schema"] == "2.0"
    assert card["header"]["subtitle"]["content"] == "Test session"
    assert len(card["body"]["elements"]) == 3
    assert "/tmp/workspace/project" in serialized
    assert section in serialized
    callbacks = [
        column["elements"][0]["behaviors"][0]["value"]
        for column in card["body"]["elements"][-1]["columns"]
    ]
    assert {item["decision"] for item in callbacks} == {
        "approved",
        "rejected",
        "cancelled",
    }
    assert {item["interaction_id"] for item in callbacks} == {
        "interaction-1"
    }
    if (
        section == "Diff"
        and isinstance(input_display, dict)
        and "before" in input_display
    ):
        assert "-old text" in serialized
        assert "+new text" in serialized


def test_approval_preview_truncates_dynamic_content() -> None:
    prompt = _approval_prompt()
    prompt = ApprovalPrompt(
        interaction_id=prompt.interaction_id,
        request=ApprovalRequest(
            id=prompt.request.id,
            session_id=prompt.request.session_id,
            tool_name="WriteFile",
            action="Write file",
            input_display={"path": "notes.txt", "content": "x" * 5000},
        ),
        session_title=prompt.session_title,
        workspace=prompt.workspace,
    )

    card = render_interaction(prompt)
    preview = card["body"]["elements"][1]["columns"][0]["elements"][-1]

    assert preview["tag"] == "markdown"
    assert len(preview["content"]) < 1700
    assert "…" in preview["content"]


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
    await adapter.start(on_message, _discard_interaction)
    try:
        event = _event(message_id="om_1")
        await adapter.handle_event(event)
        await adapter.handle_event(event)

        conversation = ConversationRef("feishu", "cli_event", "oc_direct")
        assert received == [
            InboundMessage(
                conversation=conversation,
                actor=ActorRef("ou_allowed"),
                text="hello",
                timestamp=event.event.message.create_time / 1000,
                message_id="om_1",
            )
        ]
        text_message = MessageRef(conversation, "message-1")
        assert await adapter.send_text(conversation, "reply") == text_message
        await adapter.edit_text(text_message, "updated")
        card_message = MessageRef(conversation, "card-1")
        assert (
            await adapter.present_interaction(conversation, _approval_prompt())
            == card_message
        )
        await adapter.finish_interaction(
            card_message,
            InteractionOutcome(
                state="completed",
                detail="Approved",
                approval_decision="approved",
            ),
        )
        assert transport.sent == [("oc_direct", "chat_id", "reply")]
        assert transport.edited == [("message-1", "updated")]
        assert transport.sent_cards[0][:2] == ("oc_direct", "chat_id")
        assert transport.sent_cards[0][2]["schema"] == "2.0"
        assert transport.edited_cards[0][0] == "card-1"
        assert transport.edited_cards[0][1]["header"]["template"] == "green"
    finally:
        await adapter.stop()

    assert len(runners) == 1


async def test_outbound_files_use_native_image_media_and_file_messages() -> None:
    transport = FakeTransport()
    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"ou_allowed"},
        transport=transport,
    )
    conversation = ConversationRef("feishu", "cli_event", "oc_direct")

    image_message = await adapter.send_file(
        conversation, OutboundFile("photo.png", b"png", "image/png")
    )
    video_message = await adapter.send_file(
        conversation, OutboundFile("demo.mp4", b"mp4", "video/mp4")
    )
    file_message = await adapter.send_file(
        conversation, OutboundFile("notes.txt", b"text", "text/plain")
    )

    assert [message.message_id for message in (
        image_message,
        video_message,
        file_message,
    )] == ["media-1", "media-2", "media-3"]
    assert transport.uploaded_images[0] == OutboundFile(
        "photo.png", b"png", "image/png"
    )
    cover = transport.uploaded_images[1]
    assert cover.name == "video-cover.png"
    assert cover.media_type == "image/png"
    assert cover.data == _load_video_cover()
    assert cover.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert [(item.name, file_type) for item, file_type in transport.uploaded_files] == [
        ("demo.mp4", "mp4"),
        ("notes.txt", "stream"),
    ]
    assert transport.sent_media == [
        ("oc_direct", "chat_id", "image", {"image_key": "img-1"}),
        (
            "oc_direct",
            "chat_id",
            "media",
            {"file_key": "file-1", "image_key": "img-2"},
        ),
        ("oc_direct", "chat_id", "file", {"file_key": "file-2"}),
    ]


async def test_outbound_native_upload_error_is_not_silently_remapped() -> None:
    class FailingTransport(FakeTransport):
        async def upload_image(self, file: OutboundFile) -> str:
            raise FeishuAPIError(f"unsupported image: {file.name}")

    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"ou_allowed"},
        transport=FailingTransport(),
    )

    with pytest.raises(FeishuAPIError, match="unsupported image"):
        await adapter.send_file(
            ConversationRef("feishu", "cli_event", "oc_direct"),
            OutboundFile("photo.png", b"png", "image/png"),
        )


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

    class ImageAPI:
        async def acreate(self, request: Any) -> Any:
            requests.append(("image", request))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(image_key="img_uploaded"),
            )

    class FileAPI:
        async def acreate(self, request: Any) -> Any:
            requests.append(("file", request))
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(file_key="file_uploaded"),
            )

    transport = _LarkTransport("cli_test", "secret")
    transport._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=MessageAPI(),
                message_resource=ResourceAPI(),
                image=ImageAPI(),
                file=FileAPI(),
            )
        )
    )

    assert await transport.send_text("ou_user", "open_id", "hello") == "om_reply"
    await transport.edit_text("om_reply", "updated")
    card = {"schema": "2.0", "body": {"elements": []}}
    assert await transport.send_card("ou_user", "open_id", card) == "om_reply"
    await transport.edit_card("om_reply", card)
    resource = await transport.download_resource("om_source", "img_key", "image")
    image = OutboundFile("photo.png", b"png", "image/png")
    document = OutboundFile("notes.txt", b"notes", "text/plain")
    assert await transport.upload_image(image) == "img_uploaded"
    assert await transport.upload_file(document, "stream") == "file_uploaded"
    assert (
        await transport.send_media(
            "ou_user", "open_id", "file", {"file_key": "file_uploaded"}
        )
        == "om_reply"
    )

    create_text = requests[0][1]
    assert create_text.receive_id_type == "open_id"
    assert create_text.request_body.receive_id == "ou_user"
    assert create_text.request_body.msg_type == "post"
    assert json.loads(create_text.request_body.content) == {
        "zh_cn": {"content": [[{"tag": "md", "text": "hello"}]]}
    }
    update_text = requests[1][1]
    assert update_text.message_id == "om_reply"
    assert update_text.request_body.msg_type == "post"
    assert json.loads(update_text.request_body.content) == {
        "zh_cn": {"content": [[{"tag": "md", "text": "updated"}]]}
    }
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
    image_request = requests[5][1].request_body
    assert image_request.image_type == "message"
    assert image_request.image.read() == b"png"
    file_request = requests[6][1].request_body
    assert (file_request.file_type, file_request.file_name) == (
        "stream",
        "notes.txt",
    )
    assert file_request.file.read() == b"notes"
    native_message = requests[7][1]
    assert native_message.request_body.msg_type == "file"
    assert json.loads(native_message.request_body.content) == {
        "file_key": "file_uploaded"
    }


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
        _discard_interaction,
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
        _discard_interaction,
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


def test_approval_card_rendering_and_callback_decoding() -> None:
    prompt = _approval_prompt()
    card = render_interaction(prompt)

    assert card["schema"] == "2.0"
    assert card["header"]["icon"] == {
        "tag": "standard_icon",
        "token": "approval_colorful",
    }
    button_columns = card["body"]["elements"][-1]["columns"]
    values = [
        column["elements"][0]["behaviors"][0]["value"]
        for column in button_columns
    ]
    assert {value["decision"] for value in values} == {
        "approved",
        "rejected",
        "cancelled",
    }
    assert {value["interaction_id"] for value in values} == {
        prompt.interaction_id
    }
    assert decode_interaction_response(
        prompt,
        value=values[0],
        form_value=None,
        action_name=None,
    ) == ApprovalResponse("approved")


def test_single_question_card_rendering_and_answer_decoding() -> None:
    prompt = _question_prompt()
    card = render_interaction(prompt)

    assert card["header"]["icon"] == {
        "tag": "standard_icon",
        "token": "myai_colorful",
    }
    question_context = card["body"]["elements"][1]["columns"][0]["elements"][0][
        "text"
    ]["content"]
    assert question_context == "Choice\nPick one"
    option_columns = card["body"]["elements"][2]["columns"]
    assert all(
        column["elements"][0]["type"] == "default"
        for column in option_columns
    )
    other_input = card["body"]["elements"][3]["elements"][0]
    assert other_input.get("required", False) is False

    option_value = option_columns[0]["elements"][0]["behaviors"][0]["value"]
    assert decode_interaction_response(
        prompt,
        value=option_value,
        form_value=None,
        action_name=None,
    ) == QuestionResponse((SingleChoiceAnswer("q1", "one"),))

    skip_value = option_columns[-1]["elements"][0]["behaviors"][0]["value"]
    assert decode_interaction_response(
        prompt,
        value=skip_value,
        form_value=None,
        action_name=None,
    ) == QuestionResponse((SkippedAnswer("q1"),))

    assert decode_interaction_response(
        prompt,
        value=None,
        form_value={"other_0": " custom "},
        action_name="submit_other",
    ) == QuestionResponse((OtherAnswer("q1", "custom"),))


def test_multi_question_card_form_decodes_all_answer_shapes() -> None:
    prompt = QuestionPrompt(
        interaction_id="interaction-many",
        request=QuestionRequest(
            id="question-many",
            session_id="session-1",
            questions=(
                Question(
                    id="single",
                    text="One?",
                    options=(
                        QuestionOption("a", "A"),
                        QuestionOption("b", "B"),
                    ),
                ),
                Question(
                    id="multi",
                    text="Many?",
                    options=(
                        QuestionOption("x", "X"),
                        QuestionOption("y", "Y"),
                    ),
                    multi_select=True,
                    allow_other=True,
                ),
                Question(
                    id="multi-only",
                    text="More?",
                    options=(
                        QuestionOption("left", "Left"),
                        QuestionOption("right", "Right"),
                    ),
                    multi_select=True,
                ),
            ),
        ),
        session_title="Test session",
        workspace="/tmp/workspace",
    )

    card = render_interaction(prompt)
    assert card["body"]["elements"][1]["tag"] == "form"
    assert decode_interaction_response(
        prompt,
        value=None,
        form_value={
            "q_1": ["x"],
            "other_1": "custom",
            "q_2": ["left", "right"],
        },
        action_name="submit_answers",
    ) == QuestionResponse(
        (
            SkippedAnswer("single"),
            MultipleChoiceWithOtherAnswer("multi", ("x",), "custom"),
            MultipleChoiceAnswer("multi-only", ("left", "right")),
        )
    )


async def test_card_callback_is_normalized_and_non_allowlisted_is_silent() -> None:
    transport = FakeTransport()
    actions: list[InboundInteraction] = []
    runners: list[FakeWebSocketRunner] = []

    def factory(message_callback: Any, card_callback: Any) -> FakeWebSocketRunner:
        runner = FakeWebSocketRunner(message_callback, card_callback)
        runners.append(runner)
        return runner

    async def on_interaction(
        _adapter: Any, action: InboundInteraction
    ) -> None:
        actions.append(action)

    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"ou_allowed"},
        transport=transport,
        ws_factory=factory,
    )
    await adapter.start(lambda _adapter, _message: _noop(), on_interaction)
    try:
        conversation = ConversationRef("feishu", "cli_event", "oc_direct")
        message = await adapter.present_interaction(
            conversation, _approval_prompt()
        )
        response = runners[0].card_callback(
            _card_event(
                message_id=message.message_id,
                value={
                    "interaction_id": "interaction-1",
                    "decision": "approved",
                },
            )
        )
        assert response is not None
        await _wait_for(lambda: len(actions) == 1)
        await adapter.handle_card_action_event(
            _card_event(open_id="ou_denied", user_id="user_denied")
        )
    finally:
        await adapter.stop()

    assert actions == [
        InboundInteraction(
            source=MessageRef(conversation, "card-1"),
            actor=ActorRef("ou_allowed"),
            interaction_id="interaction-1",
            response=ApprovalResponse("approved"),
        )
    ]


async def test_card_callback_after_restart_preserves_stale_identity() -> None:
    transport = FakeTransport()
    actions: list[InboundInteraction] = []
    adapter = FeishuAdapter(
        "cli_config",
        "secret",
        {"ou_allowed"},
        transport=transport,
        ws_factory=FakeWebSocketRunner,
    )
    await adapter.start(
        lambda _adapter, _message: _noop(),
        lambda _adapter, action: _append(actions, action),
    )
    try:
        await adapter.handle_card_action_event(
            _card_event(
                value={
                    "interaction_id": "interaction-from-old-run",
                    "decision": "approved",
                }
            )
        )
    finally:
        await adapter.stop()

    assert actions == [
        InboundInteraction(
            source=MessageRef(
                ConversationRef("feishu", "cli_event", "oc_direct"),
                "om_card",
            ),
            actor=ActorRef("ou_allowed"),
            interaction_id="interaction-from-old-run",
            response=None,
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
        _discard_interaction,
    )
    try:
        await adapter.handle_event(_event(message_id="om_audio", message_type="audio"))
    finally:
        await adapter.stop()

    assert received == []
    assert transport.sent == [("oc_direct", "chat_id", UNSUPPORTED_MESSAGE)]


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
