from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import pytest

from kimi_bridge.compatibility import KimiExecutableIdentity, KimiProduct
from kimi_bridge.interactions import (
    ApprovalRequest,
    MultipleChoiceAnswer,
    MultipleChoiceWithOtherAnswer,
    OtherAnswer,
    Question,
    QuestionOption,
    QuestionRequest,
    SingleChoiceAnswer,
    SkippedAnswer,
)
from kimi_bridge.kimi_server import (
    GoalBudget,
    GoalInfo,
    KimiServerAPIError,
    KimiServerClient,
    KimiServerProtocolError,
    KimiServerStartupError,
    KimiServerSupervisor,
    ModelInfo,
    ServerConnection,
    SessionStatus,
    SessionUsage,
    SkillInfo,
    TaskInfo,
    ToolInfo,
    parse_server_startup_line,
)


KIMI_CODE_HELP = """Usage: kimi [options] [command]
The Starting Point for Next-Gen Agents
web [options]  Run the local Kimi server and open the web UI.
doctor  Validate Kimi Code configuration files.
migrate  Migrate data from a legacy kimi-cli installation into kimi-code.
"""
KIMI_WEB_HELP = """Usage: kimi web [options]
--no-open
--host <host>
--port <port>
"""

LEGACY_KIMI_CLI_HELP = """Usage: kimi [OPTIONS] COMMAND [ARGS]...
Kimi, your next CLI agent.
--mcp-config-file PATH
Documentation: https://moonshotai.github.io/kimi-cli/
"""


def _envelope(data: Any, *, code: int = 0, msg: str = "success") -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data, "request_id": "req-1"}


def _profile_payload(
    *,
    session_id: str = "session-1",
    title: str = "Test session",
    model: str = "kimi-code/k3",
    thinking: str = "high",
    permission_mode: str = "manual",
    plan_mode: bool = False,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "title": title,
        "busy": False,
        "pending_interaction": "none",
        "metadata": {"cwd": "/tmp/workspace"},
        "agent_config": {
            "model": model,
            "thinking": thinking,
            "permission_mode": permission_mode,
            "plan_mode": plan_mode,
        },
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_tokens": 3,
            "cache_creation_tokens": 4,
            "total_cost_usd": 0.0125,
            "context_tokens": 30,
            "context_limit": 100,
            "turn_count": 2,
        },
    }


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHttpClient:
    def __init__(
        self,
        responses: Iterable[dict[str, Any]],
        *,
        actions: list[str] | None = None,
    ) -> None:
        self._responses = iter(responses)
        self._actions = actions
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        if self._actions is not None:
            self._actions.append(f"http:{method}:{url}")
        self.requests.append((method, url, kwargs))
        return FakeResponse(next(self._responses))


class FakeWebSocket:
    def __init__(
        self,
        *,
        subscribe_payload: dict[str, Any],
        events: Iterable[dict[str, Any]] = (),
        events_before_ack: bool = False,
        disconnect_after_events: bool = False,
    ) -> None:
        self._subscribe_payload = subscribe_payload
        self._events = list(events)
        self._events_before_ack = events_before_ack
        self._disconnect_after_events = disconnect_after_events
        self._incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self._incoming.put_nowait(
            json.dumps(
                {
                    "type": "server_hello",
                    "timestamp": "now",
                    "payload": {
                        "ws_connection_id": "conn-1",
                        "protocol_version": 2,
                        "max_event_buffer_size": 1000,
                        "capabilities": {
                            "event_batching": False,
                            "compression": False,
                        },
                    },
                }
            )
        )
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def __aenter__(self) -> FakeWebSocket:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        pass

    async def send(self, raw_frame: str) -> None:
        frame = json.loads(raw_frame)
        self.sent.append(frame)
        if frame["type"] == "client_hello":
            self._incoming.put_nowait(
                json.dumps(
                    {
                        "type": "ack",
                        "id": frame["id"],
                        "code": 0,
                        "msg": "success",
                        "payload": {
                            "accepted_subscriptions": [],
                            "resync_required": [],
                            "cursors": {},
                        },
                    }
                )
            )
        elif frame["type"] == "subscribe":
            if self._events_before_ack:
                for event in self._events:
                    self._incoming.put_nowait(json.dumps(event))
            self._incoming.put_nowait(
                json.dumps(
                    {
                        "type": "ack",
                        "id": frame["id"],
                        "code": 0,
                        "msg": "success",
                        "payload": self._subscribe_payload,
                    }
                )
            )
            if not self._events_before_ack:
                for event in self._events:
                    self._incoming.put_nowait(json.dumps(event))
            if self._disconnect_after_events:
                self._incoming.put_nowait(OSError("connection lost"))

    async def recv(self) -> str:
        incoming = await self._incoming.get()
        if isinstance(incoming, BaseException):
            raise incoming
        return incoming

    async def close(self) -> None:
        self.closed = True


class FakeWebSocketConnect:
    def __init__(
        self,
        sockets: Iterable[FakeWebSocket],
        *,
        actions: list[str] | None = None,
    ) -> None:
        self._sockets = iter(sockets)
        self._actions = actions
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeWebSocket:
        if self._actions is not None:
            self._actions.append(f"ws:{url}")
        self.calls.append((url, kwargs))
        return next(self._sockets)


class FakeCompletedProcess:
    def __init__(self, output: str, *, returncode: int = 0) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output.encode())
        self.stdout.feed_eof()
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class FakeProcess:
    def __init__(self, startup_line: str) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data((startup_line + "\n").encode())
        self.returncode: int | None = None
        self._done = asyncio.Event()
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode

    def crash(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout.feed_eof()
        self._done.set()

    def terminate(self) -> None:
        self.terminated = True
        self.crash(0)

    def kill(self) -> None:
        self.killed = True
        self.crash(-9)


class FakeProcessFactory:
    def __init__(self, processes: Iterable[Any]) -> None:
        self._processes = iter(processes)
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return next(self._processes)


class FakeSupervisor:
    def __init__(self, connections: Iterable[ServerConnection]) -> None:
        self._connections = iter(connections)

    async def wait_until_ready(self) -> ServerConnection:
        return next(self._connections)


class FakeVersionedSupervisor(FakeSupervisor):
    def __init__(
        self, connections: Iterable[ServerConnection], *, version: str
    ) -> None:
        super().__init__(connections)
        self.executable_identity = KimiExecutableIdentity(
            product=KimiProduct.KIMI_CODE,
            version=version,
        )


def _session_event(
    seq: int, epoch: str, event_type: str, *, volatile: bool = False
) -> dict[str, Any]:
    event = {
        "type": event_type,
        "seq": seq,
        "epoch": epoch,
        "session_id": "session-1",
        "timestamp": "now",
        "payload": {
            "type": event_type,
            "delta": event_type,
            "agentId": "main",
            "sessionId": "session-1",
        },
    }
    if volatile:
        event.update({"volatile": True, "offset": 0})
    return event


def test_parses_contract_and_ansi_startup_lines_without_exposing_token() -> None:
    token = "secret_token-123"
    assert parse_server_startup_line(
        f"Kimi server: http://127.0.0.1:43123/#token={token}"
    ) == (43123, token)
    assert parse_server_startup_line(
        f"\x1b[1mLocal:\x1b[0m http://127.0.0.1:43123/#token={token}\x1b[0m"
    ) == (43123, token)
    assert token not in repr(
        ServerConnection(
            base_url="http://127.0.0.1:43123",
            port=43123,
            generation=1,
            token=token,
        )
    )


async def test_rest_methods_use_snapshotted_paths_and_shapes(tmp_path: Any) -> None:
    http = FakeHttpClient(
        [
            _envelope({"server_version": "0.28.1"}),
            _envelope({"default_model": "kimi-code/k3"}),
            _envelope({"default_model": "kimi-code/k3"}),
            _envelope({"id": "session-1"}),
            _envelope(
                {
                    "id": "session-1",
                    "metadata": {"cwd": str(tmp_path.resolve())},
                }
            ),
            _envelope({"prompt_id": "prompt-1", "status": "running"}),
            _envelope(
                {
                    "active": {"prompt_id": "prompt-1"},
                    "queued": [],
                }
            ),
            _envelope({"aborted": True}),
            _envelope({"as_of_seq": 4, "epoch": "epoch-1"}),
        ]
    )
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    assert await client.check_server_version() == "0.28.1"
    assert await client.get_config() == {"default_model": "kimi-code/k3"}
    assert await client.get_default_model() == "kimi-code/k3"
    assert (
        await client.create_session(
            str(tmp_path), title="Test session", permission_mode="auto"
        )
        == "session-1"
    )
    assert (await client.get_session("session-1"))["id"] == "session-1"
    await client.submit_prompt(
        "session-1",
        "hello",
        model="kimi-code/k3",
        permission_mode="auto",
    )
    assert await client.abort_prompt("session-1") is True
    assert await client.get_snapshot("session-1") == {
        "as_of_seq": 4,
        "epoch": "epoch-1",
    }

    assert [request[0:2] for request in http.requests] == [
        ("GET", "http://127.0.0.1:43123/api/v1/meta"),
        ("GET", "http://127.0.0.1:43123/api/v1/config"),
        ("GET", "http://127.0.0.1:43123/api/v1/config"),
        ("POST", "http://127.0.0.1:43123/api/v1/sessions"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1"),
        ("POST", "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts"),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts/prompt-1:abort",
        ),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/snapshot"),
    ]
    assert http.requests[3][2]["json"] == {
        "metadata": {"cwd": str(tmp_path.resolve())},
        "title": "Test session",
        "agent_config": {"permission_mode": "auto"},
    }
    assert http.requests[5][2]["json"] == {
        "content": [{"type": "text", "text": "hello"}],
        "model": "kimi-code/k3",
        "permission_mode": "auto",
    }
    assert all(
        request[2]["headers"] == {"Authorization": "Bearer token-1"}
        for request in http.requests
    )


async def test_rest_envelope_error_is_raised() -> None:
    http = FakeHttpClient([_envelope(None, code=40401, msg="not found")])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with pytest.raises(KimiServerAPIError) as caught:
        await client.get_snapshot("missing")

    assert caught.value.code == 40401


async def test_control_and_inspection_methods_use_public_v1_shapes() -> None:
    model = {
        "provider": "kimi-code",
        "model": "kimi-code/k3",
        "display_name": "K3",
        "max_context_size": 262144,
        "capabilities": ["thinking"],
        "support_efforts": ["low", "high", "max"],
        "default_effort": "high",
    }
    status = {
        "busy": True,
        "model": "kimi-code/k3",
        "thinking_level": "max",
        "permission": "manual",
        "plan_mode": True,
        "swarm_mode": False,
        "context_tokens": 30,
        "max_context_tokens": 100,
        "context_usage": 0.3,
    }
    task = {
        "id": "task/1",
        "session_id": "session-1",
        "kind": "bash",
        "description": "Run checks",
        "status": "running",
        "command": "pytest",
        "created_at": "created",
        "started_at": "started",
        "output_preview": "tail",
        "output_bytes": 4,
    }
    skill = {
        "name": "review/code",
        "description": "Review code",
        "path": "/skills/review/code",
        "source": "user",
        "type": "prompt",
        "disable_model_invocation": False,
    }
    tool = {
        "name": "mcp_search",
        "description": "Search",
        "input_schema": {},
        "source": "mcp",
        "mcp_server_id": "search-server",
    }
    updated = _profile_payload(
        title="Renamed",
        model="kimi-code/k3",
        thinking="max",
        plan_mode=True,
    )
    http = FakeHttpClient(
        [
            _envelope({"server_version": "0.28.1"}),
            _envelope({"items": [model]}),
            _envelope(_profile_payload()),
            _envelope(status),
            _envelope(status),
            _envelope(updated),
            _envelope({"items": [task]}),
            _envelope(task),
            _envelope({"cancelled": True}),
            _envelope({"skills": [skill]}),
            _envelope({"activated": True, "skill_name": "review/code"}),
            _envelope({"tools": [tool]}),
        ]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    )

    assert await client.get_server_version() == "0.28.1"
    assert await client.list_models() == [
        ModelInfo(
            alias="kimi-code/k3",
            provider="kimi-code",
            display_name="K3",
            max_context_size=262144,
            capabilities=("thinking",),
            support_efforts=("low", "high", "max"),
            default_effort="high",
        )
    ]
    assert (await client.get_session_profile("session-1")).title == "Test session"
    assert await client.get_session_status("session-1") == SessionStatus(
        busy=True,
        model="kimi-code/k3",
        thinking_effort="max",
        permission_mode="manual",
        plan_mode=True,
        swarm_mode=False,
        context_tokens=30,
        context_limit=100,
        context_usage=0.3,
    )
    assert await client.get_session_usage("session-1") == SessionUsage(
        None, None, None, None, 30, 100
    )
    assert (
        await client.update_profile(
            "session-1",
            title="Renamed",
            model="kimi-code/k3",
            thinking="max",
            plan_mode=True,
        )
    ).title == "Renamed"
    assert await client.list_tasks("session-1", status="running") == [
        TaskInfo(
            id="task/1",
            session_id="session-1",
            kind="bash",
            description="Run checks",
            status="running",
            command="pytest",
            created_at="created",
            started_at="started",
            output_preview="tail",
            output_bytes=4,
        )
    ]
    assert (
        await client.get_task("session-1", "task/1", output_bytes=8192)
    ).output_preview == "tail"
    assert await client.cancel_task("session-1", "task/1")
    assert await client.list_skills("session-1") == [
        SkillInfo(
            name="review/code",
            description="Review code",
            source="user",
            path="/skills/review/code",
            kind="prompt",
            disable_model_invocation=False,
        )
    ]
    assert (
        await client.activate_skill(
            "session-1", "review/code", args="focus tests"
        )
        == "review/code"
    )
    assert await client.list_tools("session-1") == [
        ToolInfo("mcp_search", "Search", "mcp", "search-server")
    ]

    assert [request[0:2] for request in http.requests] == [
        ("GET", "http://127.0.0.1:43123/api/v1/meta"),
        ("GET", "http://127.0.0.1:43123/api/v1/models"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/profile"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/status"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/status"),
        ("POST", "http://127.0.0.1:43123/api/v1/sessions/session-1/profile"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/tasks"),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/tasks/task%2F1",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/tasks/task%2F1:cancel",
        ),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/skills"),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/skills/review%2Fcode:activate",
        ),
        ("GET", "http://127.0.0.1:43123/api/v1/tools"),
    ]
    assert http.requests[5][2]["json"] == {
        "title": "Renamed",
        "agent_config": {
            "model": "kimi-code/k3",
            "thinking": "max",
            "plan_mode": True,
        },
    }
    assert http.requests[6][2]["params"] == {"status": "running"}
    assert http.requests[7][2]["params"] == {
        "with_output": True,
        "output_bytes": 8192,
    }
    assert http.requests[10][2]["json"] == {"args": "focus tests"}
    assert http.requests[11][2]["params"] == {"session_id": "session-1"}


async def test_interaction_profile_steer_and_media_methods_use_spec_shapes() -> None:
    approval = {
        "approval_id": "approval-1",
        "session_id": "session-1",
        "tool_name": "Shell",
        "action": "Run command",
        "tool_input_display": {"command": "pwd"},
    }
    question = {
        "question_id": "question-1",
        "session_id": "session-1",
        "questions": [
            {
                "id": "q1",
                "question": "Pick one",
                "options": [
                    {"id": "one", "label": "One", "description": "First"}
                ],
                "allow_other": True,
            }
        ],
    }
    http = FakeHttpClient(
        [
            _envelope({"prompt_id": "prompt-1", "status": "running"}),
            _envelope({"steered": True, "prompt_ids": ["prompt-1"]}),
            _envelope(_profile_payload(permission_mode="yolo")),
            _envelope({"items": [approval]}),
            _envelope({"resolved": True, "resolved_at": "now"}),
            _envelope({"items": [question]}),
            _envelope({"resolved": True, "resolved_at": "now"}),
            _envelope({"dismissed": True, "dismissed_at": "now"}),
        ]
    )
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)
    content = [
        {"type": "text", "text": "look"},
        {
            "type": "image",
            "source": {
                "kind": "base64",
                "media_type": "image/png",
                "data": "aW1hZ2U=",
            },
        },
    ]

    await client.submit_prompt(
        "session-1", content, model="kimi-code/k3", permission_mode="manual"
    )
    assert await client.steer_prompts("session-1", ["prompt-1"])
    assert (
        await client.update_profile("session-1", permission_mode="yolo")
    ).session_id == "session-1"
    assert await client.list_approvals("session-1") == [
        ApprovalRequest(
            id="approval-1",
            session_id="session-1",
            tool_name="Shell",
            action="Run command",
            input_display={"command": "pwd"},
        )
    ]
    assert await client.resolve_approval("session-1", "approval-1", "approved")
    assert await client.list_questions("session-1") == [
        QuestionRequest(
            id="question-1",
            session_id="session-1",
            questions=(
                Question(
                    id="q1",
                    text="Pick one",
                    options=(QuestionOption("one", "One", "First"),),
                    allow_other=True,
                ),
            ),
        )
    ]
    answers = (
        SingleChoiceAnswer("q1", "one"),
        MultipleChoiceAnswer("q2", ("x", "y")),
        OtherAnswer("q3", "custom"),
        MultipleChoiceWithOtherAnswer("q4", ("left",), "another"),
        SkippedAnswer("q5"),
    )
    assert await client.resolve_question("session-1", "question-1", answers)
    assert await client.dismiss_question("session-1", "question-1")

    assert [request[0:2] for request in http.requests] == [
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts:steer",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/approvals",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/approvals/approval-1",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/questions",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/questions/question-1",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/questions/question-1:dismiss",
        ),
    ]
    assert http.requests[0][2]["json"] == {
        "content": content,
        "model": "kimi-code/k3",
        "permission_mode": "manual",
    }
    assert http.requests[1][2]["json"] == {"prompt_ids": ["prompt-1"]}
    assert http.requests[2][2]["json"] == {"agent_config": {"permission_mode": "yolo"}}
    assert http.requests[3][2]["params"] == {"status": "pending"}
    assert http.requests[4][2]["json"] == {"decision": "approved"}
    assert http.requests[5][2]["params"] == {"status": "pending"}
    assert http.requests[6][2]["json"] == {
        "answers": {
            "q1": {"kind": "single", "option_id": "one"},
            "q2": {"kind": "multi", "option_ids": ["x", "y"]},
            "q3": {"kind": "other", "text": "custom"},
            "q4": {
                "kind": "multi_with_other",
                "option_ids": ["left"],
                "other_text": "another",
            },
            "q5": {"kind": "skipped"},
        },
        "method": "click",
    }
    assert "json" not in http.requests[7][2]


async def test_stateful_session_methods_use_public_v1_shapes() -> None:
    goal = {
        "goalId": "goal-1",
        "objective": "Ship the bridge",
        "completionCriterion": "All checks pass",
        "status": "paused",
        "turnsUsed": 3,
        "tokensUsed": 4200,
        "wallClockMs": 65_000,
        "budget": {
            "tokenBudget": 10_000,
            "turnBudget": 8,
            "wallClockBudgetMs": None,
            "remainingTokens": 5800,
            "remainingTurns": 5,
            "remainingWallClockMs": None,
            "tokenBudgetReached": False,
            "turnBudgetReached": False,
            "wallClockBudgetReached": False,
            "overBudget": False,
        },
        "terminalReason": "Waiting for review",
    }
    http = FakeHttpClient(
        [
            _envelope({}),
            _envelope({"messages": [], "status": {}}),
            _envelope(None),
            _envelope(goal),
            _envelope(_profile_payload()),
            _envelope(_profile_payload()),
            _envelope(_profile_payload()),
            _envelope(_profile_payload()),
        ]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    )

    await client.compact_session("session-1")
    await client.undo_session("session-1", count=2)
    assert await client.get_goal("session-1") is None
    assert await client.get_goal("session-1") == GoalInfo(
        id="goal-1",
        objective="Ship the bridge",
        completion_criterion="All checks pass",
        status="paused",
        turns_used=3,
        tokens_used=4200,
        wall_clock_ms=65_000,
        budget=GoalBudget(
            token_budget=10_000,
            turn_budget=8,
            wall_clock_budget_ms=None,
            remaining_tokens=5800,
            remaining_turns=5,
            remaining_wall_clock_ms=None,
            token_budget_reached=False,
            turn_budget_reached=False,
            wall_clock_budget_reached=False,
            over_budget=False,
        ),
        terminal_reason="Waiting for review",
    )
    await client.update_profile(
        "session-1", goal_objective="Ship the bridge"
    )
    await client.update_profile("session-1", goal_control="pause")
    await client.update_profile("session-1", goal_control="resume")
    await client.update_profile("session-1", goal_control="cancel")

    assert [(method, url) for method, url, _kwargs in http.requests] == [
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1:compact",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1:undo",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/goal",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/goal",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
    ]
    assert http.requests[0][2]["json"] == {}
    assert http.requests[1][2]["json"] == {"count": 2}
    assert http.requests[4][2]["json"] == {
        "agent_config": {"goal_objective": "Ship the bridge"}
    }
    assert http.requests[5][2]["json"] == {
        "agent_config": {"goal_control": "pause"}
    }
    assert http.requests[6][2]["json"] == {
        "agent_config": {"goal_control": "resume"}
    }
    assert http.requests[7][2]["json"] == {
        "agent_config": {"goal_control": "cancel"}
    }


async def test_undo_validates_count_and_surfaces_upstream_error() -> None:
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient(
            [_envelope(None, code=40911, msg="Nothing to undo")]
        ),
    )

    for count in (0, -1, True):
        with pytest.raises(ValueError, match="positive integer"):
            await client.undo_session("session-1", count=count)  # type: ignore[arg-type]
    with pytest.raises(KimiServerAPIError, match="Nothing to undo") as exc_info:
        await client.undo_session("session-1")
    assert exc_info.value.code == 40911


async def test_no_active_turn_means_no_pending_interactions() -> None:
    http = FakeHttpClient(
        [
            _envelope(None, code=40001, msg="no active turn"),
            _envelope(None, code=40001, msg="no active turn"),
        ]
    )
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    assert await client.list_approvals("session-1") == []
    assert await client.list_questions("session-1") == []


async def test_unknown_server_version_warns_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    http = FakeHttpClient([_envelope({"server_version": "0.28.0"})])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with caplog.at_level("WARNING"):
        assert await client.check_server_version() == "0.28.0"

    assert "UNTESTED KIMI CODE VERSION 0.28.0" in caplog.text


async def test_executable_server_version_mismatch_is_rejected() -> None:
    http = FakeHttpClient([_envelope({"server_version": "0.28.0"})])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with pytest.raises(
        KimiServerStartupError,
        match="executable 0.28.1, server 0.28.0",
    ):
        await client.check_server_version(executable_version="0.28.1")


async def test_supervised_client_uses_preflight_version_for_mismatch() -> None:
    connection = ServerConnection(
        base_url="http://127.0.0.1:43123",
        port=43123,
        generation=1,
        token="secret",
    )
    supervisor = FakeVersionedSupervisor([connection], version="0.28.1")
    client = KimiServerClient(
        supervisor=supervisor,  # type: ignore[arg-type]
        http_client=FakeHttpClient([_envelope({"server_version": "0.29.0"})]),
    )

    with pytest.raises(
        KimiServerStartupError,
        match="executable 0.28.1, server 0.29.0",
    ):
        await client.check_server_version()


async def test_malformed_server_version_is_rejected() -> None:
    http = FakeHttpClient([_envelope({"server_version": "version unknown"})])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with pytest.raises(KimiServerStartupError, match="malformed version"):
        await client.check_server_version()


async def test_materializes_session_before_initial_subscription() -> None:
    actions: list[str] = []
    socket = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        events=[_session_event(1, "epoch-1", "turn.ended")],
    )
    ws_connect = FakeWebSocketConnect([socket], actions=actions)
    http = FakeHttpClient([_envelope({"busy": False})], actions=actions)
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=ws_connect,
    )
    events = client.subscribe_events("session-1")

    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 1
    await events.aclose()

    assert actions == [
        "http:GET:http://127.0.0.1:43123/api/v1/sessions/session-1/status",
        "ws:ws://127.0.0.1:43123/api/v1/ws",
    ]
    subscribe = next(frame for frame in socket.sent if frame["type"] == "subscribe")
    assert subscribe["payload"]["agent_filter"] == {"session-1": ["main"]}


async def test_main_agent_filter_allows_global_sequence_gaps() -> None:
    socket = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        events=[
            _session_event(1, "epoch-1", "turn.started"),
            _session_event(4, "epoch-1", "turn.ended"),
        ],
    )
    http = FakeHttpClient([_envelope({"busy": False})])
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=FakeWebSocketConnect([socket]),
    )
    events = client.subscribe_events("session-1")

    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 1
    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 4
    await events.aclose()


async def test_live_usage_event_feeds_inspection_and_reconnect_invalidates_it() -> None:
    status = {
        "busy": False,
        "model": "kimi-code/k3",
        "thinking_level": "high",
        "permission": "manual",
        "plan_mode": False,
        "swarm_mode": False,
        "context_tokens": 321,
        "max_context_tokens": 1000,
        "context_usage": 0.321,
    }
    usage_event = _session_event(
        1, "epoch-1", "agent.status.updated", volatile=True
    )
    usage_event["payload"] = {
        "type": "agent.status.updated",
        "usage": {
            "total": {
                "inputOther": 10,
                "output": 4,
                "inputCacheRead": 20,
                "inputCacheCreation": 3,
            }
        },
    }
    first = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        events=[usage_event],
        disconnect_after_events=True,
    )
    second = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        events=[_session_event(2, "epoch-1", "turn.ended")],
    )
    http = FakeHttpClient([_envelope(status) for _ in range(4)])

    async def no_sleep(_delay: float) -> None:
        pass

    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=FakeWebSocketConnect([first, second]),
        sleep=no_sleep,
    )
    events = client.subscribe_events("session-1")

    assert (await asyncio.wait_for(anext(events), 1))["payload"] == usage_event[
        "payload"
    ]
    assert await client.get_session_usage("session-1") == SessionUsage(
        10, 4, 20, 3, 321, 1000
    )

    assert (await asyncio.wait_for(anext(events), 1))["payload"]["type"] == (
        "turn.ended"
    )
    assert await client.get_session_usage("session-1") == SessionUsage(
        None, None, None, None, 321, 1000
    )
    await events.aclose()


async def test_missing_session_fails_before_websocket_connection() -> None:
    http = FakeHttpClient([_envelope(None, code=40401, msg="not found")])
    ws_connect = FakeWebSocketConnect([])
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=ws_connect,
    )
    events = client.subscribe_events("missing")

    with pytest.raises(KimiServerAPIError) as caught:
        await asyncio.wait_for(anext(events), 1)
    await events.aclose()

    assert caught.value.code == 40401
    assert ws_connect.calls == []


async def test_epoch_change_resyncs_from_snapshot_and_reuses_cursor() -> None:
    first = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {"session-1": {"seq": 5, "epoch": "epoch-old"}},
        },
        events=[
            _session_event(5, "epoch-old", "assistant.delta", volatile=True),
            _session_event(6, "epoch-old", "assistant.delta"),
            _session_event(7, "epoch-new", "turn.started"),
        ],
    )
    second = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        events=[
            _session_event(8, "epoch-new", "assistant.delta"),
            _session_event(9, "epoch-new", "turn.ended"),
        ],
    )
    actions: list[str] = []
    ws_connect = FakeWebSocketConnect([first, second], actions=actions)
    http = FakeHttpClient(
        [
            _envelope({"busy": True}),
            _envelope({"as_of_seq": 8, "epoch": "epoch-new"}),
            _envelope({"busy": True}),
        ],
        actions=actions,
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=ws_connect,
    )
    events = client.subscribe_events("session-1")

    volatile = await asyncio.wait_for(anext(events), 1)
    assert volatile["seq"] == 5
    assert volatile["volatile"] is True
    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 6
    resync = await asyncio.wait_for(anext(events), 1)
    assert resync["type"] == "resync_required"
    assert resync["payload"]["reason"] == "epoch_changed"
    assert resync["snapshot"] == {
        "as_of_seq": 8,
        "epoch": "epoch-new",
    }
    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 9
    await events.aclose()

    assert [request[0:2] for request in http.requests] == [
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/status",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/snapshot",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/status",
        ),
    ]
    assert actions == [
        "http:GET:http://127.0.0.1:43123/api/v1/sessions/session-1/status",
        "ws:ws://127.0.0.1:43123/api/v1/ws",
        "http:GET:http://127.0.0.1:43123/api/v1/sessions/session-1/snapshot",
        "http:GET:http://127.0.0.1:43123/api/v1/sessions/session-1/status",
        "ws:ws://127.0.0.1:43123/api/v1/ws",
    ]
    subscribe = next(frame for frame in second.sent if frame["type"] == "subscribe")
    assert subscribe["payload"]["cursors"] == {
        "session-1": {"seq": 8, "epoch": "epoch-new"}
    }
    assert ws_connect.calls[0] == (
        "ws://127.0.0.1:43123/api/v1/ws",
        {
            "additional_headers": {"Authorization": "Bearer token-1"},
            "ping_interval": None,
        },
    )


async def test_reconnect_materializes_again_on_new_supervisor_generation() -> None:
    first = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        disconnect_after_events=True,
    )
    second = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        events=[_session_event(1, "epoch-new", "turn.ended")],
    )
    actions: list[str] = []
    ws_connect = FakeWebSocketConnect([first, second], actions=actions)
    http = FakeHttpClient(
        [_envelope({"busy": False}), _envelope({"busy": False})],
        actions=actions,
    )
    supervisor = FakeSupervisor(
        [
            ServerConnection(
                base_url="http://127.0.0.1:43123",
                port=43123,
                generation=1,
                token="token-1",
            ),
            ServerConnection(
                base_url="http://127.0.0.1:43124",
                port=43124,
                generation=2,
                token="token-2",
            ),
        ]
    )

    async def fake_sleep(delay: float) -> None:
        actions.append(f"sleep:{delay}")

    client = KimiServerClient(
        supervisor=supervisor,  # type: ignore[arg-type]
        http_client=http,
        ws_connect=ws_connect,
        sleep=fake_sleep,
    )
    events = client.subscribe_events("session-1")

    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 1
    await events.aclose()

    assert actions == [
        "http:GET:http://127.0.0.1:43123/api/v1/sessions/session-1/status",
        "ws:ws://127.0.0.1:43123/api/v1/ws",
        "sleep:0.25",
        "http:GET:http://127.0.0.1:43124/api/v1/sessions/session-1/status",
        "ws:ws://127.0.0.1:43124/api/v1/ws",
    ]


async def test_subscription_rejects_not_found_after_materialization() -> None:
    socket = FakeWebSocket(
        subscribe_payload={
            "accepted": [],
            "not_found": ["session-1"],
            "resync_required": [],
            "cursors": {},
        }
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([_envelope({"busy": False})]),
        ws_connect=FakeWebSocketConnect([socket]),
    )
    events = client.subscribe_events("session-1")

    with pytest.raises(KimiServerProtocolError, match="after public-v1 materialization"):
        await asyncio.wait_for(anext(events), 1)
    await events.aclose()


async def test_document_fetch_and_probe_subscription_stay_inside_client_boundary() -> None:
    openapi = {"openapi": "3.0.3", "info": {"title": "REST"}}
    asyncapi = {"asyncapi": "3.1.0", "info": {"title": "WebSocket"}}
    http = FakeHttpClient(
        [
            openapi,
            asyncapi,
            _envelope({"busy": False}),
            _envelope({"busy": False}),
        ]
    )
    sockets = [
        FakeWebSocket(
            subscribe_payload={
                "accepted": ["session-1"],
                "not_found": [],
                "resync_required": [],
                "cursors": {},
            }
        )
        for _ in range(2)
    ]
    ws_connect = FakeWebSocketConnect(sockets)
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=ws_connect,
    )

    assert await client.get_openapi_document() == openapi
    assert await client.get_asyncapi_document() == asyncapi
    await client.probe_subscription("session-1")
    await client.probe_subscription("session-1")

    assert [request[:2] for request in http.requests] == [
        ("GET", "http://127.0.0.1:43123/openapi.json"),
        ("GET", "http://127.0.0.1:43123/asyncapi.json"),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/status",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/status",
        ),
    ]
    assert all(
        request[2]["headers"] == {"Authorization": "Bearer token-1"}
        for request in http.requests
    )
    assert len(ws_connect.calls) == 2
    assert all(
        call[1]["additional_headers"]
        == {"Authorization": "Bearer token-1"}
        for call in ws_connect.calls
    )


async def test_supervisor_restarts_with_exponential_backoff() -> None:
    startup = "Kimi server: http://127.0.0.1:43123/#token=secret"
    version = FakeCompletedProcess("0.28.1\n")
    help_output = FakeCompletedProcess(KIMI_CODE_HELP)
    first = FakeProcess(startup)
    second = FakeProcess(startup)
    third = FakeProcess(startup)
    web_help = FakeCompletedProcess(KIMI_WEB_HELP)
    factory = FakeProcessFactory(
        [version, help_output, web_help, first, second, third]
    )
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        initial_backoff=0.1,
        max_backoff=0.2,
        process_factory=factory,
        sleep=fake_sleep,
    )
    try:
        assert (await supervisor.start()).generation == 1
        first.crash(7)
        assert (await supervisor.wait_until_ready(after_generation=1)).generation == 2
        second.crash(8)
        assert (await supervisor.wait_until_ready(after_generation=2)).generation == 3
    finally:
        await supervisor.stop()

    assert delays == [0.1, 0.2]
    assert third.terminated is True
    assert factory.calls[0][0] == ("kimi", "--version")
    assert factory.calls[1][0] == ("kimi", "--help")
    assert factory.calls[2][0] == ("kimi", "web", "--help")
    assert factory.calls[3][0] == (
        "kimi",
        "web",
        "--no-open",
        "--host",
        "127.0.0.1",
        "--port",
        "43123",
    )
    assert factory.calls[3][1]["start_new_session"] is True
    assert [call[0] for call in factory.calls].count(("kimi", "--version")) == 1
    assert [call[0] for call in factory.calls].count(("kimi", "--help")) == 1


async def test_supervisor_warns_for_unknown_official_version_and_starts(
    caplog: pytest.LogCaptureFixture,
    unlisted_kimi_code_version: str,
) -> None:
    startup = "Kimi server: http://127.0.0.1:43123/#token=secret"
    child = FakeProcess(startup)
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess(f"{unlisted_kimi_code_version}\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess(KIMI_WEB_HELP),
            child,
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    try:
        with caplog.at_level("WARNING"):
            await supervisor.start()
    finally:
        await supervisor.stop()

    assert supervisor.executable_identity.version == unlisted_kimi_code_version
    assert f"UNTESTED KIMI CODE VERSION {unlisted_kimi_code_version}" in caplog.text
    assert [call[0] for call in factory.calls[:3]] == [
        ("kimi", "--version"),
        ("kimi", "--help"),
        ("kimi", "web", "--help"),
    ]


async def test_supervisor_rejects_missing_managed_web_flag_before_start() -> None:
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("0.28.1\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess("--no-open --host"),
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    with pytest.raises(KimiServerStartupError, match="--port"):
        await supervisor.start()

    assert [call[0] for call in factory.calls] == [
        ("kimi", "--version"),
        ("kimi", "--help"),
        ("kimi", "web", "--help"),
    ]


async def test_supervisor_rejects_legacy_product_before_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("kimi, version 1.49.0\n"),
            FakeCompletedProcess(LEGACY_KIMI_CLI_HELP),
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    with caplog.at_level("WARNING"), pytest.raises(
        KimiServerStartupError, match="legacy Python kimi-cli 1.49.0"
    ):
        await supervisor.start()

    assert "INCOMPATIBLE KIMI PRODUCT" in caplog.text
    assert [call[0] for call in factory.calls] == [
        ("kimi", "--version"),
        ("kimi", "--help"),
    ]


async def test_supervisor_rejects_unrecognized_product_fingerprint() -> None:
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("0.28.1\n"),
            FakeCompletedProcess("Usage: kimi [options]\n"),
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    with pytest.raises(KimiServerStartupError, match="product fingerprint"):
        await supervisor.start()

    assert [call[0] for call in factory.calls] == [
        ("kimi", "--version"),
        ("kimi", "--help"),
    ]


async def test_supervisor_reports_missing_kimi_before_start() -> None:
    async def missing_process(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError

    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=missing_process,
    )

    with pytest.raises(KimiServerStartupError, match="not installed"):
        await supervisor.start()
