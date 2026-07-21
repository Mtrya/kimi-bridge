from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

import kimi_bridge.platforms.telegram as telegram_module
from kimi_bridge.interactions import (
    ApprovalDecision,
    ApprovalPrompt,
    ApprovalRequest,
    ApprovalResponse,
    InteractionOutcome,
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
    ConversationRef,
    InboundInteraction,
    InboundMessage,
)
from kimi_bridge.platforms.telegram import (
    TELEGRAM_FILE_LIMIT,
    TELEGRAM_TEXT_LIMIT,
    TelegramAdapter,
    TelegramAPIError,
    TelegramBotAPI,
    TelegramFileTooLarge,
)


class FakeTelegramAPI:
    def __init__(
        self, update_batches: list[list[dict[str, Any]]] | None = None
    ) -> None:
        self.requests: list[tuple[str, dict[str, Any], float | None]] = []
        self.events: list[str] = []
        self.files: dict[str, bytes] = {}
        self.file_requests: list[tuple[str, int | None]] = []
        self.sent_message_ids: list[int] = []
        self.closed = False
        self.edit_error: TelegramAPIError | None = None
        self.poll_waiting = asyncio.Event()
        self._poll_block = asyncio.Event()
        self._update_batches = list(update_batches or [])
        self._next_message_id = 100

    async def request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        request_payload = dict(payload or {})
        self.requests.append((method, request_payload, timeout))
        self.events.append(f"api:{method}")
        if method == "getMe":
            return {"id": 999, "is_bot": True, "first_name": "Bridge"}
        if method == "deleteWebhook":
            return True
        if method == "getUpdates":
            if self._update_batches:
                return self._update_batches.pop(0)
            self.poll_waiting.set()
            await self._poll_block.wait()
            return []
        if method == "sendMessage":
            message_id = self._next_message_id
            self._next_message_id += 1
            self.sent_message_ids.append(message_id)
            return {
                "message_id": message_id,
                "chat": {"id": request_payload["chat_id"], "type": "private"},
            }
        if method == "editMessageText":
            if self.edit_error is not None:
                raise self.edit_error
            return {
                "message_id": request_payload["message_id"],
                "chat": {"id": request_payload["chat_id"], "type": "private"},
            }
        if method == "answerCallbackQuery":
            return True
        raise AssertionError(f"unexpected Telegram method: {method}")

    async def get_file(self, file_id: str, *, known_size: int | None = None) -> bytes:
        self.file_requests.append((file_id, known_size))
        if known_size is not None and known_size > TELEGRAM_FILE_LIMIT:
            raise TelegramFileTooLarge("too large")
        return self.files[file_id]

    async def close(self) -> None:
        self.closed = True
        self._poll_block.set()


async def _wait_for(predicate: Any, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout)


def _message_update(
    update_id: int,
    *,
    message_id: int | None = None,
    user_id: int = 111,
    chat_id: int | None = None,
    chat_type: str = "private",
    is_bot: bool = False,
    text: str | None = "hello",
    reply_to_message_id: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id if message_id is not None else update_id,
        "date": 1_700_000_000 + update_id,
        "chat": {
            "id": chat_id if chat_id is not None else user_id,
            "type": chat_type,
        },
        "from": {
            "id": user_id,
            "is_bot": is_bot,
            "first_name": "Allowed",
        },
        **fields,
    }
    if text is not None:
        message["text"] = text
    if reply_to_message_id is not None:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {"update_id": update_id, "message": message}


def _callback_update(
    update_id: int,
    *,
    data: str,
    message_id: int,
    user_id: int = 111,
    chat_id: int | None = None,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"query-{update_id}",
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Allowed",
            },
            "message": {
                "message_id": message_id,
                "date": 1_700_000_000,
                "chat": {
                    "id": chat_id if chat_id is not None else user_id,
                    "type": "private",
                },
            },
            "data": data,
        },
    }


async def _start_adapter(
    api: FakeTelegramAPI,
    *,
    on_message: Any | None = None,
    on_interaction: Any | None = None,
) -> TelegramAdapter:
    async def discard_message(_adapter: Any, _message: InboundMessage) -> None:
        pass

    async def discard_interaction(
        _adapter: Any, _interaction: InboundInteraction
    ) -> None:
        pass

    adapter = TelegramAdapter(
        "123456:secret-token",
        {111},
        api=api,
        poll_timeout=1,
    )
    await adapter.start(
        on_message or discard_message,
        on_interaction or discard_interaction,
    )
    await asyncio.wait_for(api.poll_waiting.wait(), 1)
    return adapter


def _requests(api: FakeTelegramAPI, method: str) -> list[dict[str, Any]]:
    return [payload for name, payload, _timeout in api.requests if name == method]


def _latest_markup(api: FakeTelegramAPI) -> dict[str, Any]:
    for method, payload, _timeout in reversed(api.requests):
        if method in {"sendMessage", "editMessageText"}:
            markup = payload.get("reply_markup")
            if (
                isinstance(markup, dict)
                and isinstance(markup.get("inline_keyboard"), list)
                and markup["inline_keyboard"]
            ):
                return markup
    raise AssertionError("no inline keyboard request")


def _button_data(markup: dict[str, Any], label: str) -> str:
    for row in markup["inline_keyboard"]:
        for button in row:
            if button["text"].removeprefix("✓ ") == label:
                return button["callback_data"]
    raise AssertionError(f"button not found: {label}")


def _approval_prompt() -> ApprovalPrompt:
    return ApprovalPrompt(
        interaction_id="interaction-approval",
        request=ApprovalRequest(
            id="approval-1",
            session_id="session-1",
            tool_name="Shell",
            action="Run command",
            input_display={"command": "touch approved.txt"},
        ),
        session_title="Approval test",
        workspace="/tmp/project",
    )


def _question_prompt() -> QuestionPrompt:
    return QuestionPrompt(
        interaction_id="interaction-question",
        request=QuestionRequest(
            id="question-1",
            session_id="session-1",
            questions=(
                Question(
                    id="single",
                    text="Pick one",
                    options=(
                        QuestionOption("a", "A"),
                        QuestionOption("b", "B"),
                    ),
                ),
                Question(
                    id="multi",
                    text="Pick many",
                    options=(
                        QuestionOption("x", "X"),
                        QuestionOption("y", "Y"),
                    ),
                    multi_select=True,
                ),
                Question(
                    id="other",
                    text="Custom",
                    options=(QuestionOption("fixed", "Fixed"),),
                    allow_other=True,
                    other_label="Custom answer",
                ),
                Question(
                    id="multi-other",
                    text="Many plus custom",
                    options=(
                        QuestionOption("left", "Left"),
                        QuestionOption("right", "Right"),
                    ),
                    multi_select=True,
                    allow_other=True,
                ),
                Question(
                    id="skip",
                    text="Skip this",
                    options=(QuestionOption("only", "Only"),),
                ),
            ),
        ),
        session_title="Question test",
        workspace="/tmp/project",
    )


async def test_bot_api_retries_429_and_never_logs_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "123456:very-secret-token"
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 2},
                },
            )
        assert json.loads(request.content) == {"drop_pending_updates": True}
        return httpx.Response(200, json={"ok": True, "result": True})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = TelegramBotAPI(
        secret,
        http_client=http,
        sleep=fake_sleep,
        max_retries=1,
    )
    caplog.set_level(logging.INFO)
    try:
        assert (
            await api.request("deleteWebhook", {"drop_pending_updates": True}) is True
        )
    finally:
        await api.close()
        await http.aclose()

    assert attempts == 2
    assert delays == [2.0]
    assert secret not in caplog.text


async def test_bot_api_invalid_token_bubbles_without_secret() -> None:
    secret = "123456:very-secret-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "ok": False,
                "error_code": 401,
                "description": "Unauthorized",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = TelegramBotAPI(secret, http_client=http, max_retries=0)
    try:
        with pytest.raises(TelegramAPIError) as caught:
            await api.request("getMe")
    finally:
        await api.close()
        await http.aclose()

    assert caught.value.error_code == 401
    assert secret not in str(caught.value)


async def test_bot_api_uses_bounded_backoff_for_transport_and_server_failures() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                503,
                json={
                    "ok": False,
                    "error_code": 503,
                    "description": "Unavailable",
                },
            )
        if attempts == 2:
            raise httpx.ConnectError("unavailable", request=request)
        if attempts == 3:
            return httpx.Response(502, content=b"bad gateway")
        return httpx.Response(200, json={"ok": True, "result": True})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = TelegramBotAPI(
        "token",
        http_client=http,
        sleep=fake_sleep,
        max_retries=3,
        initial_backoff=0.25,
        max_backoff=0.5,
    )
    try:
        assert await api.request("deleteWebhook") is True
    finally:
        await api.close()
        await http.aclose()

    assert attempts == 4
    assert delays == [0.25, 0.5, 0.5]


async def test_bot_api_stream_enforces_limit_without_size_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_module, "TELEGRAM_FILE_LIMIT", 4)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"file_path": "documents/file.bin"},
                },
            )
        return httpx.Response(200, stream=httpx.ByteStream(b"12345"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = TelegramBotAPI("token", http_client=http)
    try:
        with pytest.raises(TelegramFileTooLarge):
            await api.get_file("file-id")
    finally:
        await api.close()
        await http.aclose()


async def test_bot_api_file_download_honors_retry_after() -> None:
    download_attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal download_attempts
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"file_path": "documents/file.bin"},
                },
            )
        download_attempts += 1
        if download_attempts == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 3},
                },
            )
        return httpx.Response(200, content=b"contents")

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = TelegramBotAPI(
        "token",
        http_client=http,
        sleep=fake_sleep,
        max_retries=1,
    )
    try:
        assert await api.get_file("file-id") == b"contents"
    finally:
        await api.close()
        await http.aclose()

    assert download_attempts == 2
    assert delays == [3.0]


async def test_start_drops_backlog_then_advances_poll_offset() -> None:
    received: list[InboundMessage] = []
    api = FakeTelegramAPI([[_message_update(10, text="new message")]])

    async def on_message(_adapter: Any, message: InboundMessage) -> None:
        received.append(message)

    adapter = await _start_adapter(api, on_message=on_message)
    try:
        await _wait_for(lambda: len(received) == 1)
        await _wait_for(lambda: len(_requests(api, "getUpdates")) >= 2)
    finally:
        await adapter.stop()

    assert [method for method, _payload, _timeout in api.requests[:3]] == [
        "getMe",
        "deleteWebhook",
        "getUpdates",
    ]
    assert _requests(api, "deleteWebhook") == [{"drop_pending_updates": True}]
    polls = _requests(api, "getUpdates")
    assert polls[0]["allowed_updates"] == ["message", "callback_query"]
    assert "offset" not in polls[0]
    assert polls[1]["offset"] == 11
    assert received[0].conversation == ConversationRef("telegram", "999", "111")
    assert received[0].actor.id == "111"
    assert api.closed


async def test_private_allowlist_and_topic_filters() -> None:
    received: list[InboundMessage] = []
    api = FakeTelegramAPI()

    async def on_message(_adapter: Any, message: InboundMessage) -> None:
        received.append(message)

    adapter = await _start_adapter(api, on_message=on_message)
    try:
        await adapter.handle_update(_message_update(1, chat_type="group", text="group"))
        await adapter.handle_update(_message_update(2, user_id=222, text="not allowed"))
        await adapter.handle_update(_message_update(3, is_bot=True, text="bot"))
        await adapter.handle_update(
            _message_update(4, message_thread_id=7, text="topic")
        )
        await adapter.handle_update(_message_update(5, text="allowed"))
    finally:
        await adapter.stop()

    assert [message.text for message in received] == ["allowed"]


async def test_send_and_edit_are_plain_persistent_messages() -> None:
    api = FakeTelegramAPI()
    adapter = await _start_adapter(api)
    conversation = ConversationRef("telegram", "999", "111")
    try:
        message = await adapter.send_text(conversation, "hello")
        await adapter.edit_text(message, "updated")
    finally:
        await adapter.stop()

    assert adapter.message_limit == TELEGRAM_TEXT_LIMIT
    assert _requests(api, "sendMessage")[-1] == {
        "chat_id": 111,
        "text": "hello",
    }
    assert _requests(api, "editMessageText")[-1] == {
        "chat_id": 111,
        "message_id": 100,
        "text": "updated",
    }


async def test_photo_document_and_unsupported_media_normalization() -> None:
    received: list[InboundMessage] = []
    api = FakeTelegramAPI()
    api.files = {"photo-large": b"jpeg", "document": b"contents"}

    async def on_message(_adapter: Any, message: InboundMessage) -> None:
        received.append(message)

    adapter = await _start_adapter(api, on_message=on_message)
    try:
        await adapter.handle_update(
            _message_update(
                1,
                text=None,
                caption="look",
                photo=[
                    {
                        "file_id": "photo-small",
                        "file_unique_id": "small",
                        "width": 100,
                        "height": 100,
                        "file_size": 10,
                    },
                    {
                        "file_id": "photo-large",
                        "file_unique_id": "large",
                        "width": 1000,
                        "height": 1000,
                        "file_size": 100,
                    },
                ],
            )
        )
        await adapter.handle_update(
            _message_update(
                2,
                text=None,
                caption="read",
                document={
                    "file_id": "document",
                    "file_unique_id": "document-unique",
                    "file_name": "notes.txt",
                    "mime_type": "text/plain",
                    "file_size": 8,
                },
            )
        )
        await adapter.handle_update(
            _message_update(
                3,
                text=None,
                document={
                    "file_id": "too-large",
                    "file_unique_id": "large",
                    "file_size": TELEGRAM_FILE_LIMIT + 1,
                },
            )
        )
        await adapter.handle_update(
            _message_update(4, text=None, media_group_id="album-1", photo=[])
        )
        await adapter.handle_update(
            _message_update(5, text=None, media_group_id="album-1", photo=[])
        )
        await adapter.handle_update(
            _message_update(6, text=None, video={"file_id": "video"})
        )
    finally:
        await adapter.stop()

    assert api.file_requests == [("photo-large", 100), ("document", 8)]
    assert received[0].text == "look"
    assert received[0].images[0].data == b"jpeg"
    assert received[0].images[0].media_type == "image/jpeg"
    assert received[1].text == "read"
    assert received[1].files[0].data == b"contents"
    assert received[1].files[0].name == "notes.txt"
    assert received[1].files[0].media_type == "text/plain"
    notices = _requests(api, "sendMessage")
    assert len(notices) == 3


@pytest.mark.parametrize(
    ("label", "decision"),
    [
        ("Approve", "approved"),
        ("Reject", "rejected"),
        ("Cancel", "cancelled"),
    ],
)
async def test_approval_callback_is_acknowledged_before_delivery_and_goes_stale(
    label: str, decision: ApprovalDecision
) -> None:
    api = FakeTelegramAPI()
    delivered: list[InboundInteraction] = []

    async def on_interaction(_adapter: Any, interaction: InboundInteraction) -> None:
        api.events.append("handler:interaction")
        delivered.append(interaction)

    adapter = await _start_adapter(api, on_interaction=on_interaction)
    conversation = ConversationRef("telegram", "999", "111")
    try:
        message = await adapter.present_interaction(conversation, _approval_prompt())
        markup = _latest_markup(api)
        callback_data = _button_data(markup, label)
        assert len(callback_data.encode("utf-8")) <= 64
        api.events.clear()

        await adapter.handle_update(
            _callback_update(1, data=callback_data, message_id=int(message.message_id))
        )
        await adapter.finish_interaction(
            message,
            InteractionOutcome(state="completed", detail="Approved"),
        )
        await adapter.handle_update(
            _callback_update(2, data=callback_data, message_id=int(message.message_id))
        )
        await adapter.handle_update(
            _callback_update(
                3,
                data=callback_data,
                message_id=int(message.message_id),
                user_id=222,
                chat_id=222,
            )
        )
    finally:
        await adapter.stop()

    assert api.events.index("api:answerCallbackQuery") < api.events.index(
        "handler:interaction"
    )
    assert delivered[0].interaction_id == "interaction-approval"
    assert delivered[0].response == ApprovalResponse(decision)
    assert delivered[1].interaction_id is None
    assert delivered[1].response is None
    assert len(delivered) == 2


@pytest.mark.parametrize(
    "outcome",
    [
        InteractionOutcome(state="timed_out", detail="Expired"),
        InteractionOutcome(state="cancelled", detail="Stopped"),
    ],
)
async def test_question_terminal_outcomes_clear_callbacks_and_custom_reply(
    outcome: InteractionOutcome,
) -> None:
    api = FakeTelegramAPI()
    stale: list[InboundInteraction] = []

    async def on_interaction(_adapter: Any, interaction: InboundInteraction) -> None:
        stale.append(interaction)

    adapter = await _start_adapter(api, on_interaction=on_interaction)
    conversation = ConversationRef("telegram", "999", "111")
    prompt = QuestionPrompt(
        interaction_id="interaction-terminal",
        request=QuestionRequest(
            id="question-terminal",
            session_id="session-1",
            questions=(
                Question(
                    id="answer",
                    text="Answer this",
                    options=(QuestionOption("fixed", "Fixed"),),
                    allow_other=True,
                ),
            ),
        ),
        session_title="Terminal test",
        workspace="/tmp/project",
    )
    try:
        await adapter.handle_update(_message_update(1, text="initial"))
        message = await adapter.present_interaction(conversation, prompt)
        markup = _latest_markup(api)
        stale_data = _button_data(markup, "Skip")
        other_data = _button_data(markup, "Other")
        await adapter.handle_update(
            _callback_update(
                2,
                data=other_data,
                message_id=int(message.message_id),
            )
        )
        force_reply_id = api.sent_message_ids[-1]

        await adapter.finish_interaction(message, outcome)
        await adapter.handle_update(
            _callback_update(
                3,
                data=stale_data,
                message_id=int(message.message_id),
            )
        )
        await adapter.handle_update(
            _message_update(
                4,
                text="too late",
                reply_to_message_id=force_reply_id,
            )
        )
    finally:
        await adapter.stop()

    terminal_edits = _requests(api, "editMessageText")
    assert terminal_edits[-1]["reply_markup"] == {"inline_keyboard": []}
    assert len(stale) == 1
    assert stale[0].interaction_id is None
    assert stale[0].response is None


@pytest.mark.parametrize(
    "description",
    [
        "Bad Request: message is not modified",
        "Bad Request: message to edit not found",
        "Bad Request: message can't be edited",
    ],
)
async def test_terminal_interaction_tolerates_known_stale_message_errors(
    description: str,
) -> None:
    api = FakeTelegramAPI()
    adapter = await _start_adapter(api)
    try:
        message = await adapter.present_interaction(
            ConversationRef("telegram", "999", "111"),
            _approval_prompt(),
        )
        api.edit_error = TelegramAPIError("editMessageText", 400, description)
        await adapter.finish_interaction(
            message,
            InteractionOutcome(state="stale", detail="Expired"),
        )
    finally:
        await adapter.stop()


async def test_terminal_interaction_does_not_swallow_other_edit_errors() -> None:
    api = FakeTelegramAPI()
    adapter = await _start_adapter(api)
    try:
        message = await adapter.present_interaction(
            ConversationRef("telegram", "999", "111"),
            _approval_prompt(),
        )
        api.edit_error = TelegramAPIError(
            "editMessageText", 400, "Bad Request: chat not found"
        )
        with pytest.raises(TelegramAPIError, match="chat not found"):
            await adapter.finish_interaction(
                message,
                InteractionOutcome(state="stale", detail="Expired"),
            )
    finally:
        await adapter.stop()


async def test_question_wizard_collects_every_answer_shape_and_blocks_text() -> None:
    api = FakeTelegramAPI()
    inbound: list[InboundMessage] = []
    delivered: list[InboundInteraction] = []

    async def on_message(_adapter: Any, message: InboundMessage) -> None:
        inbound.append(message)

    async def on_interaction(_adapter: Any, interaction: InboundInteraction) -> None:
        delivered.append(interaction)

    adapter = await _start_adapter(
        api,
        on_message=on_message,
        on_interaction=on_interaction,
    )
    try:
        await adapter.handle_update(_message_update(1, text="initial"))
        inbound.clear()
        message = await adapter.present_interaction(
            ConversationRef("telegram", "999", "111"),
            _question_prompt(),
        )
        message_id = int(message.message_id)
        callback_id = 10

        async def click(label: str) -> None:
            nonlocal callback_id
            data = _button_data(_latest_markup(api), label)
            await adapter.handle_update(
                _callback_update(callback_id, data=data, message_id=message_id)
            )
            callback_id += 1

        await click("A")
        await click("X")
        await click("Y")
        await click("Done")
        await click("Custom answer")
        first_force_reply = api.sent_message_ids[-1]

        await adapter.handle_update(_message_update(20, text="not a reply"))
        await adapter.handle_update(_message_update(21, text="/help"))
        await adapter.handle_update(
            _message_update(
                22,
                text="custom one",
                reply_to_message_id=first_force_reply,
            )
        )

        await click("Left")
        await click("Other")
        second_force_reply = api.sent_message_ids[-1]
        await adapter.handle_update(
            _message_update(
                23,
                text="custom many",
                reply_to_message_id=second_force_reply,
            )
        )
        await click("Skip")
    finally:
        await adapter.stop()

    assert [message.text for message in inbound] == ["/help"]
    assert len(delivered) == 1
    assert delivered[0].interaction_id == "interaction-question"
    assert delivered[0].response == QuestionResponse(
        (
            SingleChoiceAnswer("single", "a"),
            MultipleChoiceAnswer("multi", ("x", "y")),
            OtherAnswer("other", "custom one"),
            MultipleChoiceWithOtherAnswer("multi-other", ("left",), "custom many"),
            SkippedAnswer("skip"),
        )
    )
    assert any(
        payload.get("reply_markup", {}).get("force_reply") is True
        for payload in _requests(api, "sendMessage")
    )


async def test_old_callback_after_adapter_restart_uses_stale_path() -> None:
    first_api = FakeTelegramAPI()
    first = await _start_adapter(first_api)
    conversation = ConversationRef("telegram", "999", "111")
    try:
        message = await first.present_interaction(conversation, _approval_prompt())
        old_data = _button_data(_latest_markup(first_api), "Reject")
    finally:
        await first.stop()

    second_api = FakeTelegramAPI()
    stale: list[InboundInteraction] = []

    async def on_interaction(
        adapter: TelegramAdapter, interaction: InboundInteraction
    ) -> None:
        stale.append(interaction)
        await adapter.finish_interaction(
            interaction.source,
            InteractionOutcome(state="stale", detail="Expired"),
        )

    second = await _start_adapter(second_api, on_interaction=on_interaction)
    try:
        await second.handle_update(
            _callback_update(50, data=old_data, message_id=int(message.message_id))
        )
    finally:
        await second.stop()

    assert len(stale) == 1
    assert stale[0].interaction_id is None
    assert stale[0].response is None
    assert _requests(second_api, "editMessageText")[-1]["reply_markup"] == {
        "inline_keyboard": []
    }
