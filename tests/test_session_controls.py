from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kimi_bridge.kimi_server import (
    KimiServerAPIError,
    KimiServerClient,
    PromptDelivery,
    PromptQueue,
    QueuedPrompt,
)
from kimi_bridge.router import ChatRouter
from kimi_bridge.state import StateStore
from test_kimi_server import (
    FakeHttpClient,
    FakeWebSocket,
    FakeWebSocketConnect,
    _envelope,
    _profile_payload,
)
from test_router import (
    FakeAdapter,
    FakeKimiClient,
    _bind_control_session,
    _control_session,
    _goal,
    _message,
)


@pytest.mark.parametrize("status", ["active", "paused", "blocked"])
async def test_native_goal_edit_decodes_existing_identity_progress_and_budget(
    status: str,
) -> None:
    before = {
        "goalId": "native-goal",
        "objective": "Original",
        "status": status,
        "turnsUsed": 4,
        "tokensUsed": 1234,
        "wallClockMs": 9000,
        "budget": {
            "tokenBudget": 5000,
            "turnBudget": None,
            "wallClockBudgetMs": None,
            "remainingTokens": 3766,
            "remainingTurns": None,
            "remainingWallClockMs": None,
            "tokenBudgetReached": False,
            "turnBudgetReached": False,
            "wallClockBudgetReached": False,
            "overBudget": False,
        },
    }
    after = {**before, "objective": "Revised"}
    http = FakeHttpClient([_envelope(before), _envelope(after)])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        original = await client.get_goal("session-1")
        revised = await client.edit_goal("session-1", "Revised")
    assert revised == replace(original, objective="Revised")
    assert http.requests[1][0] == "PATCH"
    assert http.requests[1][1].endswith("/sessions/session-1/goal")
    assert http.requests[1][2]["json"] == {"objective": "Revised"}


@pytest.mark.parametrize("code", [40401, 40914, 40915, 40917, 40918])
async def test_native_goal_edit_preserves_server_failures(code: int) -> None:
    http = FakeHttpClient([_envelope(None, code=code, msg="native edit rejected")])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        with pytest.raises(KimiServerAPIError) as error:
            await client.edit_goal("session-1", "Revised")
    assert error.value.code == code
    assert len(http.requests) == 1


async def test_native_creation_applies_settings_through_profile_after_create(
    tmp_path: Path,
) -> None:
    http = FakeHttpClient(
        [_envelope({"id": "session-1"}), _envelope(_profile_payload())]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        assert (
            await client.create_session(
                str(tmp_path), model="kimi-code/k3", swarm_mode=False
            )
            == "session-1"
        )
    assert http.requests[0][2]["json"] == {"metadata": {"cwd": str(tmp_path)}}
    assert http.requests[1][1].endswith("/sessions/session-1/profile")
    assert http.requests[1][2]["json"] == {
        "agent_config": {"model": "kimi-code/k3", "swarm_mode": False}
    }


def _router(tmp_path: Path, client: FakeKimiClient) -> tuple[ChatRouter, StateStore]:
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    return ChatRouter(
        client, state_store=store, default_workspace=tmp_path, model="kimi-code/k3"
    ), store


@pytest.mark.parametrize("status", ["active", "paused", "blocked"])
async def test_goal_edit_preserves_native_identity_and_submits_no_prompt(
    tmp_path: Path, status: str
) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    before = _goal(status=status)
    client.goals["session-control"] = before
    router, store = _router(tmp_path, client)
    state = store.load()
    try:
        await router.handle_inbound(
            FakeAdapter(), _message("/goal edit Revised objective")
        )
    finally:
        await router.close()
    assert client.goal_edits == [("session-control", "Revised objective")]
    assert client.goals["session-control"] == replace(
        before, objective="Revised objective"
    )
    assert client.prompts == []
    assert client.profile_updates == []
    assert store.load() == state


async def test_goal_edit_empty_and_literal_reserved_objective(tmp_path: Path) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session()]
    router, _ = _router(tmp_path, client)
    try:
        await router.handle_inbound(FakeAdapter(), _message("/goal edit"))
        assert client.prompts == [] and client.goal_edits == []
        await router.handle_inbound(
            FakeAdapter(), _message("/goal -- edit the fixture")
        )
    finally:
        await router.close()
    assert client.goals["session-control"].objective == "edit the fixture"
    assert client.prompts[0][1] == "edit the fixture"


async def test_queue_controls_select_waiting_ids_and_keep_enqueue_distinct(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    client.prompt_queues["session-control"] = PromptQueue(
        QueuedPrompt("active-id", "running", "Active request"),
        (
            QueuedPrompt("waiting-a", "queued", "First request"),
            QueuedPrompt("waiting-b", "blocked", "Second request"),
        ),
    )
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/queue"))
        listing = adapter.sent[-1][2]
        assert "1. waiting-a" in listing and "2. waiting-b" in listing
        assert "1. active-id" not in listing
        await router.handle_inbound(adapter, _message("/queue cancel 1"))
        await router.handle_inbound(adapter, _message("/queue steer 2"))
        await router.handle_inbound(adapter, _message("/queue -- cancel the fixture"))
        await router.handle_inbound(adapter, _message("ordinary steering"))
    finally:
        await router.close()
    assert client.cancelled_prompts == [("session-control", "waiting-a")]
    assert client.steered_prompts == [("session-control", "waiting-b")]
    assert client.prompts[0][1] == "cancel the fixture"
    assert client.prompt_deliveries == [
        PromptDelivery.ENQUEUE,
        PromptDelivery.STEER_IF_ACTIVE,
    ]
    assert client.stopped == []


async def test_queue_selections_expire_on_switch_and_numeric_ids_are_explicit(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [
        _control_session(),
        {**_control_session(), "id": "other-session"},
    ]
    client.prompt_queues["session-control"] = PromptQueue(
        None, (QueuedPrompt("waiting-a", "queued", "old"),)
    )
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/queue"))
        await router.handle_inbound(adapter, _message("/switch other-session"))
        await router.handle_inbound(adapter, _message("/queue cancel 1"))
        await router.handle_inbound(adapter, _message("/queue cancel id 123"))
    finally:
        await router.close()
    assert client.cancelled_prompts == [("other-session", "123")]


async def test_swarm_changes_do_not_prompt_and_fork_preserves_native_settings(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True, model="kimi-code/selected")]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/swarm on"))
        await router.handle_inbound(adapter, _message("/fork"))
        await router.handle_inbound(adapter, _message("/swarm"))
    finally:
        await router.close()
    assert client.forks == ["session-control"]
    assert client.prompts == []
    assert client.aborted == [] and client.stopped == []
    assert client.profile_updates == [("session-control", {"swarm_mode": True})]
    assert (
        store.load().bindings["feishu:cli_bot:ou_user"].session_id == "forked-session"
    )
    fork = await client.get_session("forked-session")
    assert fork["agent_config"]["swarm_mode"] is True
    assert fork["agent_config"]["model"] == "kimi-code/selected"


async def test_archived_selection_requires_restore_and_failed_restore_keeps_binding(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [
        _control_session(),
        {**_control_session(), "id": "archived-session", "archived": True},
    ]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/sessions archived"))
        await router.handle_inbound(adapter, _message("/switch 1"))
        assert (
            store.load().bindings["feishu:cli_bot:ou_user"].session_id
            == "session-control"
        )
        client.restore_error = KimiServerAPIError(40401, "missing fixture")
        await router.handle_inbound(adapter, _message("/restore 1"))
        assert (
            store.load().bindings["feishu:cli_bot:ou_user"].session_id
            == "session-control"
        )
        client.restore_error = None
        await router.handle_inbound(adapter, _message("/restore 1"))
    finally:
        await router.close()
    assert (
        store.load().bindings["feishu:cli_bot:ou_user"].session_id == "archived-session"
    )
    assert client.restored == ["archived-session", "archived-session"]
    assert not (await client.get_session("archived-session"))["archived"]


async def test_native_queue_decoding_and_selected_abort_steer_requests() -> None:
    http = FakeHttpClient(
        [
            _envelope(
                {
                    "active": {
                        "prompt_id": "active",
                        "status": "running",
                        "content": [{"type": "text", "text": "working"}],
                    },
                    "queued": [
                        {
                            "prompt_id": "next",
                            "status": "queued",
                            "content": [
                                {"type": "text", "text": "question"},
                                {"type": "file", "name": "input.csv"},
                            ],
                        }
                    ],
                }
            ),
            _envelope({"aborted": True}),
            _envelope({"steered": True, "prompt_ids": ["next"]}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        queue = await client.get_prompt_queue("session-1")
        assert queue.active == QueuedPrompt("active", "running", "working")
        assert queue.waiting == (
            QueuedPrompt("next", "queued", "question\n[attachment: input.csv]"),
        )
        assert await client.cancel_prompt("session-1", "next")
        await client.steer_prompt("session-1", "next")
    assert http.requests[1][1].endswith("/prompts/next:abort")
    assert http.requests[2][2]["json"] == {"prompt_ids": ["next"]}


@pytest.mark.parametrize("code", [40402, 40903])
async def test_native_cancel_reports_expired_prompt_outcome(code: int) -> None:
    http = FakeHttpClient([_envelope(None, code=code, msg="ended")])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        if code == 40903:
            assert not await client.cancel_prompt("session-1", "selected")
        else:
            with pytest.raises(KimiServerAPIError) as error:
                await client.cancel_prompt("session-1", "selected")
            assert error.value.code == code


async def test_effective_inspection_reads_live_agent_without_caching_closed_subscription() -> (
    None
):
    class SelectedSocket(FakeWebSocket):
        def _agent_burst_frames(self, session_id, *, include_reset=True):
            frames = super()._agent_burst_frames(
                session_id, include_reset=include_reset
            )
            for frame in frames:
                if frame.get("type") == "agent":
                    frame.update(
                        model="kimi-code/selected",
                        permission_mode="auto",
                        swarm_mode=True,
                    )
            return frames

    stored = _profile_payload()
    stored["agent_config"] = {}
    http = FakeHttpClient([_envelope(stored), _envelope(stored)])
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=http,
        ws_connect=FakeWebSocketConnect(
            [SelectedSocket(subscribe_payload={"accepted": ["session-1"]})]
        ),
    ) as client:
        status = await client.inspect_session("session-1")
        assert (status.model, status.permission_mode, status.swarm_mode) == (
            "kimi-code/selected",
            "auto",
            True,
        )
        assert (await client.get_session_status("session-1")).model is None


async def test_effective_inspection_times_out_when_subscription_ack_stalls():
    import asyncio
    import json
    from kimi_bridge.kimi_server import KimiServerTransportError

    class StalledSocket(FakeWebSocket):
        async def send(self, raw_frame):
            if json.loads(raw_frame)["type"] == "subscribe_v2":
                self.sent.append(json.loads(raw_frame))
            else:
                await super().send(raw_frame)

    socket = StalledSocket(subscribe_payload={"accepted": ["session-1"]})
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=FakeHttpClient([_envelope(_profile_payload())]),
        ws_connect=FakeWebSocketConnect([socket]),
        timeout=0.02,
    ) as client:
        with pytest.raises(KimiServerTransportError):
            await asyncio.wait_for(client.inspect_session("session-1"), timeout=1)


async def test_creation_failure_reports_native_session_identity(tmp_path: Path):
    from kimi_bridge.kimi_server import KimiServerOperationError

    http = FakeHttpClient(
        [
            _envelope({"id": "created-session"}),
            _envelope(None, code=50000, msg="settings unavailable"),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        with pytest.raises(KimiServerOperationError) as error:
            await client.create_session(str(tmp_path), model="kimi-code/k3")
    assert "created-session" in str(error.value)
    assert len(http.requests) == 2
