from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from kimi_bridge.interactions import ApprovalResponse
from kimi_bridge.kimi_server import (
    ActivityUpdate,
    AgentActivity,
    GoalActivity,
    InteractionUpdate,
    InteractionSnapshot,
    KimiServerClient,
    KimiServerOperationError,
    SessionActivity,
)
from kimi_bridge.router import ChatRouter
from kimi_bridge.state import BridgeState, DeliveryRoute, ObservationRoute, StateStore
from test_router import (
    FakeAdapter,
    FakeKimiClient,
    _approval,
    _control_session,
    _event,
    _interaction,
    _message,
    _task,
    _wait_for,
)
from test_session_controls import _router
from test_kimi_server import (
    FakeHttpClient,
    FakeWebSocket,
    FakeWebSocketConnect,
    _envelope,
    _turn_ended_ops,
)


def _activity(
    session_id="session-control",
    *,
    status="running",
    goal=None,
    tasks=(),
    baseline=False,
):
    return ActivityUpdate(
        SessionActivity(
            session_id, (AgentActivity("main", status),), tuple(tasks), goal
        ),
        baseline,
    )


@pytest.mark.parametrize("transition", ["complete", "blocked"])
async def test_goal_and_task_notices_survive_switch_without_changing_binding(
    tmp_path: Path, transition
):
    client = FakeKimiClient()
    client.sessions = [
        _control_session(busy=True),
        {**_control_session(), "id": "other", "title": "Other"},
    ]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    running = _task("fixture-task")
    try:
        await router.handle_inbound(adapter, _message("/status"))
        client.emit(
            "session-control",
            _activity(
                goal=GoalActivity("Fixture", "active"), tasks=(running,), baseline=True
            ),
        )
        await _wait_for(lambda: router._active.activity is not None)
        await router.handle_inbound(adapter, _message("/switch other"))
        assert "session-control" in store.load().observations
        client.emit(
            "session-control",
            _activity(
                goal=GoalActivity("Fixture", transition),
                tasks=(replace(running, status="completed"),),
            ),
        )
        await _wait_for(lambda: len(adapter.notice_texts) == 2)
        client.emit(
            "session-control",
            _activity(
                goal=GoalActivity("Fixture", transition),
                tasks=(replace(running, status="completed"),),
            ),
        )
        client.emit("other", _event("turn.started", turnId=1))
        client.emit("other", _event("assistant.delta", delta="B answer", offset=0))
        client.emit("other", _event("turn.ended", turnId=1, reason="completed"))
        await _wait_for(lambda: any("B answer" in text for _, _, text in adapter.sent))
        assert len(adapter.notice_texts) == 2
        for _, _, text in adapter.notice_texts:
            heading, body, action = text.split("\n\n")
            assert "Control session" in heading
            assert heading.startswith("**") and heading.endswith("**")
            assert "\n" in body
            assert "/switch session-control" in action
        assert store.load().bindings["feishu:cli_bot:ou_user"].session_id == "other"
    finally:
        await router.close()


async def test_notice_failure_is_logged_and_not_retried_or_reconstructed(
    tmp_path: Path, caplog
):
    class FailingNoticeAdapter(FakeAdapter):
        attempts = 0

        async def send_notice_text(self, conversation, text):
            self.attempts += 1
            raise OSError("fixture delivery failed")

    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    router, _ = _router(tmp_path, client)
    adapter = FailingNoticeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        baseline = _activity(goal=GoalActivity("Fixture", "active"), baseline=True)
        terminal = _activity(goal=GoalActivity("Fixture", "complete"))
        client.emit("session-control", baseline)
        client.emit("session-control", terminal)
        await _wait_for(lambda: adapter.attempts == 1)
        client.emit("session-control", terminal)
        client.emit("session-control", replace(terminal, baseline=True))
        client.emit("session-control", _activity(tasks=(), baseline=True))
        await router.handle_inbound(adapter, _message("/status"))
        await asyncio.sleep(0)
        assert adapter.attempts == 1
        assert "dropping" in caplog.text
        assert router._active.task is not None and not router._active.task.done()
    finally:
        await router.close()


async def test_off_session_controls_need_switch_back_and_stop_addresses_binding(
    tmp_path: Path,
):
    client = FakeKimiClient()
    client.sessions = [
        _control_session(busy=True),
        {**_control_session(), "id": "other"},
    ]
    request = replace(_approval(), session_id="session-control")
    client.approvals["session-control"] = [request]
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        card, _, prompt = adapter.interactions[0]
        await router.handle_inbound(adapter, _message("/switch other"))
        assert len(adapter.notice_texts) == 1
        client.emit(
            "session-control", InteractionUpdate("session-control", request.id, request)
        )
        await asyncio.sleep(0)
        assert len(adapter.notice_texts) == 1 and len(adapter.interactions) == 1
        await router.handle_interaction(
            adapter, _interaction(card, interaction_id=prompt.interaction_id)
        )
        assert client.resolved_approvals == []
        await router.handle_inbound(adapter, _message("/stop"))
        assert client.stopped == ["other"]
        assert client.approvals["session-control"] == [request]
        await router.handle_inbound(adapter, _message("/switch session-control"))
        await _wait_for(lambda: len(adapter.interactions) == 2)
        card2, _, prompt2 = adapter.interactions[-1]
        await router.handle_interaction(
            adapter,
            _interaction(
                card2,
                interaction_id=prompt2.interaction_id,
                response=ApprovalResponse("approved"),
            ),
        )
        assert client.resolved_approvals == [
            ("session-control", request.id, "approved")
        ]
    finally:
        await router.close()


async def test_background_plan_is_automatically_approved_for_noninteractive_adapter(
    tmp_path: Path,
):
    client = FakeKimiClient()
    client.sessions = [
        _control_session(busy=True),
        {**_control_session(), "id": "other"},
    ]
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter(supports_interactions=False)
    try:
        await router.handle_inbound(adapter, _message("/status"))
        await router.handle_inbound(adapter, _message("/switch other"))
        request = replace(
            _approval(), session_id="session-control", tool_name="ExitPlanMode"
        )
        client.emit(
            "session-control", InteractionUpdate("session-control", request.id, request)
        )
        await _wait_for(lambda: bool(client.resolved_approvals))
        assert client.resolved_approvals == [
            ("session-control", request.id, "approved")
        ]
        assert adapter.interactions == [] and adapter.notice_texts == []
    finally:
        await router.close()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_observation_state_migrates_existing_bindings(version, tmp_path: Path):
    path = tmp_path / "state.json"
    binding = {
        "session_id": "A",
        "workspace": "/fixture",
        "permission_mode": "auto",
        "render_thinking": True,
    }
    path.write_text(
        json.dumps(
            {
                "version": version,
                "active_conversation": "chat",
                "bindings": {"chat": binding},
            }
        )
    )
    store = StateStore(path)
    before = store.load()
    assert before.observations == {}
    route = ObservationRoute(
        "chat", DeliveryRoute("feishu", "cli_bot", "oc_direct", "ou_user")
    )
    before.observations["B"] = route
    store.save(before)
    assert store.load() == before
    assert store.load().bindings["chat"].session_id == "A"


@pytest.mark.parametrize("allowed", [True, False])
async def test_restart_restores_only_allowed_observation_routes(
    tmp_path: Path, allowed
):
    client = FakeKimiClient()
    client.sessions = [_control_session()]
    store = StateStore(tmp_path / "state.json")
    store.save(
        BridgeState(
            observations={
                "session-control": ObservationRoute(
                    "feishu:cli_bot:ou_user",
                    DeliveryRoute(
                        "feishu",
                        "cli_bot" if allowed else "other-bot",
                        "oc_direct",
                        "ou_user",
                    ),
                )
            }
        )
    )
    router = ChatRouter(
        client, state_store=store, default_workspace=tmp_path, model="kimi-code/k3"
    )
    adapter = FakeAdapter()
    try:
        await router.restore(adapter)
        if allowed:
            await _wait_for(lambda: "session-control" in client.subscriptions)
        assert ("session-control" in client.subscriptions) == allowed
        assert router._active is None
        assert not adapter.notice_texts
    finally:
        await router.close()


class SideClient(FakeKimiClient):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.cancelled = []
        self.answers = asyncio.Queue()

    async def side_question(self, session_id, question):
        self.calls.append((session_id, question))
        try:
            yield await self.answers.get()
        finally:
            self.cancelled.append(question)


async def test_side_answer_is_labelled_independent_and_replaced_on_new_question(
    tmp_path: Path,
):
    client = SideClient()
    client.sessions = [_control_session(busy=True)]
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/btw first"))
        await _wait_for(lambda: len(client.calls) == 1)
        main = router._active
        await router.handle_inbound(adapter, _message("/btw second"))
        await _wait_for(lambda: len(client.calls) == 2)
        assert client.cancelled == ["first"]
        await router.handle_inbound(adapter, _message("main steering"))
        client.answers.put_nowait(
            {
                **_event("turn.ended", reason="completed"),
                "snapshot": {
                    "messages": {
                        "items": [
                            {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": "Side fixture answer"}
                                ],
                            }
                        ]
                    }
                },
            }
        )
        await _wait_for(
            lambda: any("Side fixture answer" in text for _, _, text in adapter.sent)
        )
        assert any("Side question" in text for _, _, text in adapter.sent)
        assert router._active is main and main.render.text == ""
        assert [item[1].text for item in client.prompts] == ["main steering"]
        assert client.calls == [
            ("session-control", "first"),
            ("session-control", "second"),
        ]
    finally:
        await router.close()


async def test_native_side_question_subscribes_and_submits_only_to_created_agent():
    ended = _turn_ended_ops(2)
    ended["payload"]["agent_id"] = "side-agent"
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []}, events=[ended]
    )
    http = FakeHttpClient(
        [
            _envelope({"agent_id": "side-agent"}),
            _envelope({"prompt_id": "side-prompt", "status": "running"}),
            _envelope(None, code=40903, msg="already ended"),
        ],
        transcript_snapshots=[
            {"agents": [], "pending_interactions": []},
            {"attachments": [], "seq": 2, "has_more": False, "items": [
                {"kind": "turn", "ordinal": 1, "state": "completed", "steps": [
                    {"frames": [{"kind": "text", "role": "assistant", "text": "Native side answer"}]}
                ]}
            ]},
        ],
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=http,
        ws_connect=FakeWebSocketConnect([socket]),
    ) as client:
        events = [
            event
            async for event in client.side_question("session-1", "Inspect fixture")
        ]
    assert events[-1]["payload"]["type"] == "turn.ended"
    assert events[-1]["snapshot"]["messages"]["items"][-1]["content"] == [
        {"type": "text", "text": "Native side answer"}
    ]
    assert http.requests[0][1].endswith("/sessions/session-1:btw")
    prompt = next(
        request for request in http.requests if request[1].endswith("/prompts")
    )
    assert prompt[2]["json"] == {
        "content": [{"type": "text", "text": "Inspect fixture"}],
        "agent_id": "side-agent",
    }
    subscribe = next(frame for frame in socket.sent if frame["type"] == "subscribe_v2")
    assert subscribe["payload"]["transcript"] == {"side-agent": "delta"}
    transcripts = [
        request for request in http.requests if request[1].endswith("/transcript")
    ]
    assert all(
        request[2]["params"]["agent_id"] == "side-agent" for request in transcripts
    )


async def test_native_subscriptions_for_distinct_sessions_progress_independently():
    sockets = [
        FakeWebSocket(subscribe_payload={"accepted": [session_id], "not_found": []})
        for session_id in ("A", "B")
    ]
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect(sockets),
    ) as client:
        streams = [
            client.subscribe_events(session_id, include_activity=True)
            for session_id in ("A", "B")
        ]
        try:
            first = await asyncio.wait_for(
                asyncio.gather(*(anext(stream) for stream in streams)), 1
            )
            assert [
                item.session_id
                for item in first
                if isinstance(item, InteractionSnapshot)
            ] == ["A", "B"]
            baseline = await asyncio.gather(*(anext(stream) for stream in streams))
            assert all(
                isinstance(item, ActivityUpdate) and item.baseline for item in baseline
            )
            assert len(client._active_ws) == 2
        finally:
            await asyncio.gather(*(stream.aclose() for stream in streams))
        assert client._active_ws == {} and client._activities == {}


async def test_reconnect_replaces_activity_with_a_baseline():
    disconnected = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        disconnect_after_events=True,
        tasks=[
            {
                "task_id": "old-task",
                "kind": "shell",
                "status": "running",
                "detached": True,
            }
        ],
    )
    reconnected = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []}
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([disconnected, reconnected]),
        reconnect_initial_backoff=0,
    ) as client:
        stream = client.subscribe_events("session-1", include_activity=True)
        try:
            await anext(stream)
            first = await anext(stream)
            assert (
                isinstance(first, ActivityUpdate)
                and first.activity.tasks[0].id == "old-task"
            )
            second = None
            async with asyncio.timeout(1):
                while second is None:
                    event = await anext(stream)
                    if isinstance(event, ActivityUpdate):
                        second = event
            assert second.baseline and second.activity.tasks == ()
        finally:
            await stream.aclose()


async def test_fork_keeps_source_observation_and_its_notice_route(tmp_path: Path):
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        client.emit(
            "session-control",
            _activity(goal=GoalActivity("Source", "active"), baseline=True),
        )
        await _wait_for(lambda: router._active.activity is not None)
        await router.handle_inbound(adapter, _message("/fork"))
        assert (
            store.load().bindings["feishu:cli_bot:ou_user"].session_id
            == "forked-session"
        )
        client.emit(
            "session-control", _activity(goal=GoalActivity("Source", "complete"))
        )
        await _wait_for(lambda: bool(adapter.notice_texts))
        assert adapter.notice_texts[0][1] == _message("").conversation
        assert "Source" in adapter.notice_texts[0][2]
        assert client.stopped == [] and client.aborted == []
    finally:
        await router.close()


async def test_foreground_observer_survives_idle_baseline_in_another_chat(tmp_path: Path):
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        source = router._active
        await router.handle_inbound(
            adapter,
            _message("Other work", user_id="ou_other", conversation_id="oc_other"),
        )
        client.emit("session-control", _activity(status="idle", baseline=True))
        client.emit("session-control", _event("turn.started", turnId=1))
        client.emit(
            "session-control",
            _event("assistant.delta", delta="Original chat answer", offset=0),
        )
        client.emit(
            "session-control", _event("turn.ended", turnId=1, reason="completed")
        )
        await _wait_for(
            lambda: any("Original chat answer" in text for _, _, text in adapter.sent)
        )
        assert router._observers["session-control"] is source and source.foreground
        assert all(
            conversation == source.conversation
            for _, conversation, text in adapter.sent
            if "Original chat answer" in text
        )
    finally:
        await router.close()


async def test_failed_switch_work_lookup_keeps_source_controls_and_binding(
    tmp_path: Path,
):
    class Client(FakeKimiClient):
        fail_lookup = False

        async def get_session_status(self, session_id):
            if self.fail_lookup and session_id == "session-control":
                raise KimiServerOperationError("Work lookup unavailable")
            return await super().get_session_status(session_id)

    client = Client()
    client.sessions = [
        _control_session(busy=True),
        {**_control_session(), "id": "other"},
    ]
    client.approvals["session-control"] = [
        replace(_approval(), session_id="session-control")
    ]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        source = router._active
        pending = router._pending[source.conversation_key]
        client.fail_lookup = True
        await router.handle_inbound(adapter, _message("/switch other"))
        assert (
            store.load().bindings[source.conversation_key].session_id
            == "session-control"
        )
        assert source.foreground and router._pending[source.conversation_key] is pending
        assert adapter.notice_texts == [] and not pending.timeout_task.cancelled()
    finally:
        await router.close()


async def test_same_session_in_other_chat_keeps_original_render_and_controls(
    tmp_path: Path,
):
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    client.approvals["session-control"] = [
        replace(_approval(), session_id="session-control")
    ]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        source = router._active
        pending = router._pending[source.conversation_key]
        await router.handle_inbound(
            adapter,
            _message(
                "/switch session-control",
                user_id="ou_other",
                conversation_id="oc_other",
            ),
        )
        assert router._active is source
        assert source.conversation == _message("").conversation
        assert "feishu:cli_bot:ou_other" not in store.load().bindings
        assert router._pending[source.conversation_key] is pending
        assert len(adapter.interactions) == 1
    finally:
        await router.close()


async def test_switch_back_renders_thinking_with_destination_binding(tmp_path: Path):
    client = FakeKimiClient()
    client.sessions = [
        _control_session(busy=True),
        {**_control_session(busy=True), "id": "other"},
    ]
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/render-thinking on"))
        await router.handle_inbound(adapter, _message("/switch other"))
        client.snapshots["session-control"] = {
            "in_flight_turn": {
                "turn_id": 1,
                "step": 1,
                "assistant_text": "Answer fixture",
                "thinking_text": "Thinking fixture",
            },
            "messages": {"items": []},
        }
        await router.handle_inbound(adapter, _message("/switch session-control"))
        assert any("Thinking fixture" in text for _, _, text in adapter.sent)
    finally:
        await router.close()


async def test_active_goal_retained_before_activity_baseline(tmp_path: Path):
    from test_router import _goal

    client = FakeKimiClient()
    client.sessions = [_control_session(), {**_control_session(), "id": "other"}]
    client.goals["session-control"] = _goal(status="active")
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        source = router._active
        assert source.activity is None
        await router.handle_inbound(adapter, _message("/switch other"))
        assert router._observers["session-control"] is source
        assert "session-control" in store.load().observations
    finally:
        await router.close()


async def test_same_operator_chat_handoff_rebuilds_messages_and_controls(
    tmp_path: Path,
):
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    request = replace(_approval(), session_id="session-control")
    client.approvals["session-control"] = [request]
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        source = router._active
        client.emit("session-control", _event("turn.started", turnId=1))
        client.emit(
            "session-control",
            _event("assistant.delta", delta="Original answer prefix " * 3, offset=0),
        )
        await _wait_for(lambda: bool(source.render.messages))
        old_messages = tuple(source.render.messages)
        prefix = "Original answer prefix " * 3
        client.snapshots["session-control"] = {
            "in_flight_turn": {
                "turn_id": 1,
                "step": 1,
                "assistant_text": prefix,
                "thinking_text": "",
            },
            "messages": {"items": []},
        }
        moved = _message("/status", conversation_id="oc_destination")
        await router.handle_inbound(adapter, moved)
        await _wait_for(lambda: len(adapter.interactions) == 2)
        assert all(
            message not in old_messages for message in router._active.render.messages
        )
        assert any(
            text == prefix and conversation == moved.conversation
            for _, conversation, text in adapter.sent
        )
        assert adapter.interactions[-1][1] == moved.conversation
        client.snapshots["session-control"] = {
            "in_flight_turn": None,
            "messages": {
                "items": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": prefix + "Destination answer"}
                        ],
                    }
                ]
            },
        }
        client.emit(
            "session-control",
            _event("assistant.delta", delta="Destination answer", offset=len(prefix)),
        )
        client.emit(
            "session-control", _event("turn.ended", turnId=1, reason="completed")
        )
        await _wait_for(
            lambda: any("Destination answer" in text for _, text in adapter.edits)
        )
        assert all(
            message not in old_messages
            for message, text in adapter.edits
            if "Destination answer" in text
        )
        assert all(
            conversation == moved.conversation
            for _, conversation, text in adapter.sent
            if "Destination answer" in text
        )
    finally:
        await router.close()


@pytest.mark.parametrize("failure", ["subscription", "snapshot", "other_operator"])
async def test_failed_switch_preserves_source_delivery_controls_and_selections(
    tmp_path, failure
):
    from kimi_bridge.kimi_server import (
        KimiServerTransportError,
        PromptQueue,
        QueuedPrompt,
    )

    class Client(FakeKimiClient):
        fail_snapshot = False

        async def get_snapshot(self, session_id):
            if self.fail_snapshot and session_id == "other":
                raise KimiServerTransportError("Destination snapshot unavailable")
            return await super().get_snapshot(session_id)

    client = Client()
    client.sessions = [
        _control_session(busy=True),
        {**_control_session(busy=True), "id": "other"},
    ]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        if failure == "snapshot":
            await router.handle_inbound(adapter, _message("/switch other"))
            await router.handle_inbound(adapter, _message("/switch session-control"))
            client.fail_snapshot = True
        else:
            await router.handle_inbound(adapter, _message("/status"))
            if failure == "other_operator":
                await router.handle_inbound(
                    adapter,
                    _message(
                        "/switch other", user_id="ou_other", conversation_id="oc_other"
                    ),
                )
            else:
                client.stream_errors["other"] = KimiServerTransportError(
                    "Destination unavailable"
                )
        request = replace(_approval(), session_id="session-control")
        client.approvals["session-control"] = [request]
        client.emit(
            "session-control", InteractionUpdate("session-control", request.id, request)
        )
        await _wait_for(lambda: bool(router._pending))
        source = router._observers["session-control"]
        pending = router._pending[source.conversation_key]
        client.tasks["session-control"] = [_task("selected-task")]
        client.task_details[("session-control", "selected-task")] = client.tasks[
            "session-control"
        ][0]
        client.prompt_queues["session-control"] = PromptQueue(
            None, (QueuedPrompt("selected-prompt", "queued", "Fixture"),)
        )
        await router.handle_inbound(adapter, _message("/tasks"))
        await router.handle_inbound(adapter, _message("/queue"))
        await router.handle_inbound(adapter, _message("/switch other"))
        assert (
            store.load().bindings[source.conversation_key].session_id
            == "session-control"
        )
        assert source.foreground and router._pending[source.conversation_key] is pending
        assert source.task is not None and not source.task.done()
        await router.handle_inbound(adapter, _message("/tasks show 1"))
        await router.handle_inbound(adapter, _message("/queue cancel 1"))
        assert client.task_detail_calls[-1][:2] == ("session-control", "selected-task")
        assert client.cancelled_prompts[-1] == ("session-control", "selected-prompt")
        client.emit("session-control", _event("turn.started", turnId=1))
        client.emit(
            "session-control",
            _event(
                "assistant.delta",
                delta="Source continues after failed switch",
                offset=0,
            ),
        )
        client.emit(
            "session-control", _event("turn.ended", turnId=1, reason="completed")
        )
        await _wait_for(
            lambda: any(
                "Source continues after failed switch" in text
                for _, _, text in adapter.sent
            )
        )
        assert all(
            conversation == source.conversation
            for _, conversation, text in adapter.sent
            if "Source continues after failed switch" in text
        )
    finally:
        await router.close()


async def test_running_agent_retains_observation_without_main_busy_or_tasks(tmp_path):
    class Client(FakeKimiClient):
        async def inspect_activity(self, session_id):
            return SessionActivity(session_id, (AgentActivity("child", "running"),), ())

    client = Client()
    client.sessions = [_control_session(), {**_control_session(), "id": "other"}]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        source = router._active
        await router.handle_inbound(adapter, _message("/switch other"))
        assert router._observers["session-control"] is source and not source.foreground
        assert "session-control" in store.load().observations
    finally:
        await router.close()


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
async def test_native_side_question_recovers_terminal_reset_without_resubmitting(state):
    class SideSocket(FakeWebSocket):
        def _agent_burst_frames(self, session_id, *, include_reset=True):
            frames = super()._agent_burst_frames(
                session_id, include_reset=include_reset
            )
            for frame in frames:
                frame["agent_id"] = "side-agent"
                if frame["type"] == "transcript.reset":
                    frame["payload"]["agent_id"] = "side-agent"
            return frames

    turn = {
        "kind": "turn",
        "turnId": "side-turn",
        "ordinal": 1,
        "state": state,
        "triggerPromptId": "side-prompt",
        "steps": [
            {
                "stepId": "side-step",
                "ordinal": 1,
                "state": state,
                "frames": [
                    {
                        "kind": "text",
                        "role": "assistant",
                        "text": "Recovered side fixture",
                    }
                ],
            }
        ],
    }
    first = SideSocket(
        subscribe_payload={"accepted": ["session-1"]}, disconnect_after_events=True
    )
    second = SideSocket(
        subscribe_payload={"accepted": ["session-1"]},
        reset_on_resume=True,
        reset_seq=10,
    )
    second._reset_snapshot_items = [turn]
    empty = {
        "items": [],
        "agents": [],
        "pending_interactions": [],
        "attachments": [],
        "has_more": False,
        "seq": 1,
    }
    complete = {**empty, "items": [turn], "seq": 10}
    http = FakeHttpClient(
        [
            _envelope({"agent_id": "side-agent"}),
            _envelope({"prompt_id": "side-prompt", "status": "running"}),
            _envelope(None, code=40903, msg="already ended"),
        ],
        transcript_snapshots=[empty, complete, complete],
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=http,
        ws_connect=FakeWebSocketConnect([first, second]),
        sleep=lambda _: asyncio.sleep(0),
    ) as client:

        async def collect():
            return [
                event
                async for event in client.side_question("session-1", "Inspect fixture")
            ]

        events = await asyncio.wait_for(collect(), 1)
    ended = [event for event in events if event["payload"]["type"] == "turn.ended"]
    assert len(ended) == 1 and ended[0]["payload"]["reason"] == state
    assert ended[0]["snapshot"]["messages"]["items"][-1]["content"] == [
        {"type": "text", "text": "Recovered side fixture"}
    ]
    assert (
        len(
            [
                request
                for request in http.requests
                if request[0] == "POST" and request[1].endswith("/prompts")
            ]
        )
        == 1
    )
