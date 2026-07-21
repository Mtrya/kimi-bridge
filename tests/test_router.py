from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from kimi_bridge.interactions import (
    ApprovalPrompt,
    ApprovalRequest,
    ApprovalResponse,
    InteractionOutcome,
    InteractionPrompt,
    MultipleChoiceAnswer,
    MultipleChoiceWithOtherAnswer,
    OtherAnswer,
    Question,
    QuestionAnswer,
    QuestionOption,
    QuestionPrompt,
    QuestionRequest,
    QuestionResponse,
    SingleChoiceAnswer,
    SkippedAnswer,
)
from kimi_bridge.kimi_server import (
    KimiServerAPIError,
    ModelInfo,
    SessionProfile,
    SessionStatus,
    SessionUsage,
    SkillInfo,
    TaskInfo,
    TaskStatus,
    ToolInfo,
)
from kimi_bridge.platforms.base import (
    ActorRef,
    ConversationRef,
    InboundFile,
    InboundImage,
    InboundInteraction,
    InboundMessage,
    MessageRef,
)
from kimi_bridge.router import ChatRouter
from kimi_bridge.state import BridgeState, ConversationBinding, StateStore


class FakeKimiClient:
    def __init__(self) -> None:
        self.server_version = "0.28.1"
        self.created: list[tuple[str, str | None, dict[str, Any]]] = []
        self.prompts: list[tuple[str, str | list[dict[str, Any]], dict[str, Any]]] = []
        self.prompt_statuses: list[str] = []
        self.steered: list[tuple[str, list[str]]] = []
        self.steer_error: KimiServerAPIError | None = None
        self.profile_updates: list[tuple[str, dict[str, Any]]] = []
        self.models = [
            ModelInfo(
                alias="kimi-code/k3",
                provider="kimi-code",
                display_name="K3",
                max_context_size=262_144,
                capabilities=("thinking", "always_thinking"),
                support_efforts=("low", "high", "max"),
                default_effort="high",
            )
        ]
        self.tasks: dict[str, list[TaskInfo]] = {}
        self.task_details: dict[tuple[str, str], TaskInfo] = {}
        self.task_list_calls: list[tuple[str, TaskStatus | None]] = []
        self.task_detail_calls: list[tuple[str, str, int]] = []
        self.cancelled_tasks: list[tuple[str, str]] = []
        self.skills: dict[str, list[SkillInfo]] = {}
        self.activated_skills: list[tuple[str, str, str]] = []
        self.activation_subscription_ready: list[bool] = []
        self.tools: dict[str, list[ToolInfo]] = {}
        self.aborted: list[str] = []
        self.abort_result = True
        self.sessions: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.subscriptions: list[str] = []
        self.stream_actions: list[tuple[str, str]] = []
        self.call_order: list[str] = []
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, list[ApprovalRequest]] = {}
        self.questions: dict[str, list[QuestionRequest]] = {}
        self.resolved_approvals: list[tuple[str, str, str]] = []
        self.resolved_questions: list[
            tuple[str, str, tuple[QuestionAnswer, ...]]
        ] = []
        self.dismissed_questions: list[tuple[str, str]] = []
        self.stream_errors: dict[str, BaseException] = {}
        self._events: dict[
            str, asyncio.Queue[dict[str, Any] | BaseException]
        ] = {}
        self._ready: dict[str, asyncio.Event] = {}

    async def create_session(
        self,
        workspace: str,
        *,
        title: str | None = None,
        **profile: Any,
    ) -> str:
        session_id = f"session-{len(self.created) + 1}"
        self.created.append((workspace, title, profile))
        self.sessions.insert(
            0,
            {
                "id": session_id,
                "title": title or "",
                "busy": False,
                "metadata": {"cwd": workspace},
                "agent_config": profile,
            },
        )
        return session_id

    async def submit_prompt(
        self,
        session_id: str,
        content: str | list[dict[str, Any]],
        **profile: Any,
    ) -> dict[str, Any]:
        self.call_order.append("submit")
        self.prompts.append((session_id, content, profile))
        status = self.prompt_statuses.pop(0) if self.prompt_statuses else "running"
        return {
            "prompt_id": f"prompt-{len(self.prompts)}",
            "status": status,
        }

    async def steer_prompts(self, session_id: str, prompt_ids: list[str]) -> bool:
        self.call_order.append("steer")
        self.steered.append((session_id, prompt_ids))
        if self.steer_error is not None:
            raise self.steer_error
        return True

    async def get_server_version(self) -> str:
        return self.server_version

    async def list_models(self) -> list[ModelInfo]:
        return list(self.models)

    async def get_session_profile(self, session_id: str) -> SessionProfile:
        session = await self.get_session(session_id)
        agent_config = session.setdefault("agent_config", {})
        return SessionProfile(
            session_id=session_id,
            title=str(session.get("title", "")),
            workspace=str(session["metadata"]["cwd"]),
            busy=bool(session.get("busy", False)),
            pending_interaction=session.get("pending_interaction", "none"),
            model=str(agent_config.get("model", "kimi-code/k3")),
            thinking_effort=agent_config.get("thinking", "high"),
            permission_mode=agent_config.get("permission_mode", "manual"),
            plan_mode=bool(agent_config.get("plan_mode", False)),
            usage=session.get(
                "usage",
                SessionUsage(0, 0, 0, 0, 0, 262_144),
            ),
        )

    async def get_session_status(self, session_id: str) -> SessionStatus:
        profile = await self.get_session_profile(session_id)
        context_tokens = profile.usage.context_tokens or 0
        context_limit = profile.usage.context_limit or 0
        return SessionStatus(
            busy=profile.busy,
            model=profile.model,
            thinking_effort=profile.thinking_effort or "off",
            permission_mode=profile.permission_mode or "manual",
            plan_mode=bool(profile.plan_mode),
            swarm_mode=False,
            context_tokens=context_tokens,
            context_limit=context_limit,
            context_usage=(context_tokens / context_limit if context_limit else 0),
        )

    async def get_session_usage(self, session_id: str) -> SessionUsage:
        return (await self.get_session_profile(session_id)).usage

    async def update_profile(
        self,
        session_id: str,
        *,
        title: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
        permission_mode: str | None = None,
        plan_mode: bool | None = None,
    ) -> SessionProfile:
        changes = {
            key: value
            for key, value in {
                "title": title,
                "model": model,
                "thinking": thinking,
                "permission_mode": permission_mode,
                "plan_mode": plan_mode,
            }.items()
            if value is not None
        }
        self.profile_updates.append((session_id, changes))
        session = await self.get_session(session_id)
        if title is not None:
            session["title"] = title
        agent_config = session.setdefault("agent_config", {})
        agent_config.update(
            {
                key: value
                for key, value in changes.items()
                if key != "title"
            }
        )
        return await self.get_session_profile(session_id)

    async def list_tasks(
        self, session_id: str, *, status: TaskStatus | None = None
    ) -> list[TaskInfo]:
        self.task_list_calls.append((session_id, status))
        tasks = self.tasks.get(session_id, [])
        return [task for task in tasks if status is None or task.status == status]

    async def get_task(
        self, session_id: str, task_id: str, *, output_bytes: int = 8192
    ) -> TaskInfo:
        self.task_detail_calls.append((session_id, task_id, output_bytes))
        return self.task_details[(session_id, task_id)]

    async def cancel_task(self, session_id: str, task_id: str) -> bool:
        self.cancelled_tasks.append((session_id, task_id))
        return True

    async def list_skills(self, session_id: str) -> list[SkillInfo]:
        return list(self.skills.get(session_id, []))

    async def activate_skill(
        self, session_id: str, skill_name: str, *, args: str = ""
    ) -> str:
        ready = self._ready.get(session_id)
        self.activation_subscription_ready.append(
            ready is not None and ready.is_set()
        )
        self.activated_skills.append((session_id, skill_name, args))
        return skill_name

    async def list_tools(self, session_id: str) -> list[ToolInfo]:
        return list(self.tools.get(session_id, []))

    async def list_sessions(self, **params: Any) -> list[dict[str, Any]]:
        self.list_calls.append(params)
        sessions = [
            session
            for session in self.sessions
            if bool(session.get("busy")) is params["busy"]
        ]
        return sessions[: params.get("page_size")]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return next(session for session in self.sessions if session["id"] == session_id)

    async def abort_prompt(self, session_id: str) -> bool:
        self.aborted.append(session_id)
        return self.abort_result

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return self.snapshots.get(
            session_id,
            {"in_flight_turn": None, "messages": {"items": []}},
        )

    async def list_approvals(self, session_id: str) -> list[ApprovalRequest]:
        return list(self.approvals.get(session_id, []))

    async def resolve_approval(
        self, session_id: str, approval_id: str, decision: str
    ) -> bool:
        self.resolved_approvals.append((session_id, approval_id, decision))
        self.approvals[session_id] = [
            item
            for item in self.approvals.get(session_id, [])
            if item.id != approval_id
        ]
        return True

    async def list_questions(self, session_id: str) -> list[QuestionRequest]:
        return list(self.questions.get(session_id, []))

    async def resolve_question(
        self,
        session_id: str,
        question_id: str,
        answers: tuple[QuestionAnswer, ...],
    ) -> bool:
        self.resolved_questions.append((session_id, question_id, answers))
        self.questions[session_id] = [
            item
            for item in self.questions.get(session_id, [])
            if item.id != question_id
        ]
        return True

    async def dismiss_question(self, session_id: str, question_id: str) -> bool:
        self.dismissed_questions.append((session_id, question_id))
        self.questions[session_id] = [
            item
            for item in self.questions.get(session_id, [])
            if item.id != question_id
        ]
        return True

    async def wait_until_subscribed(
        self, session_id: str, *, timeout: float = 1
    ) -> None:
        ready = self._ready.setdefault(session_id, asyncio.Event())
        await asyncio.wait_for(ready.wait(), timeout)

    async def subscribe_events(self, session_id: str):
        self.subscriptions.append(session_id)
        self.stream_actions.append(("subscribe", session_id))
        error = self.stream_errors.get(session_id)
        if error is not None:
            raise error
        queue = self._events.setdefault(session_id, asyncio.Queue())
        self._ready.setdefault(session_id, asyncio.Event()).set()
        while True:
            item = await queue.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    def emit(self, session_id: str, event: dict[str, Any]) -> None:
        self._events.setdefault(session_id, asyncio.Queue()).put_nowait(event)

    def fail_stream(self, session_id: str, error: BaseException) -> None:
        self._events.setdefault(session_id, asyncio.Queue()).put_nowait(error)


class FakeAdapter:
    name = "feishu"

    def __init__(self, *, message_limit: int = 1000) -> None:
        self.message_limit = message_limit
        self.sent: list[tuple[MessageRef, ConversationRef, str]] = []
        self.edits: list[tuple[MessageRef, str]] = []
        self.interactions: list[
            tuple[MessageRef, ConversationRef, InteractionPrompt]
        ] = []
        self.outcomes: list[tuple[MessageRef, InteractionOutcome]] = []

    async def start(
        self, _message_handler: Any, _interaction_handler: Any
    ) -> None:
        pass

    async def wait(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_text(
        self, conversation: ConversationRef, text: str
    ) -> MessageRef:
        message = MessageRef(
            conversation, f"message-{len(self.sent) + 1}"
        )
        self.sent.append((message, conversation, text))
        return message

    async def edit_text(self, message: MessageRef, text: str) -> None:
        self.edits.append((message, text))

    async def present_interaction(
        self, conversation: ConversationRef, prompt: InteractionPrompt
    ) -> MessageRef:
        message = MessageRef(
            conversation, f"interaction-{len(self.interactions) + 1}"
        )
        self.interactions.append((message, conversation, prompt))
        return message

    async def finish_interaction(
        self, message: MessageRef, outcome: InteractionOutcome
    ) -> None:
        self.outcomes.append((message, outcome))


def _message(
    text: str,
    *,
    user_id: str = "ou_user",
    conversation_id: str = "oc_direct",
    images: tuple[InboundImage, ...] = (),
    files: tuple[InboundFile, ...] = (),
) -> InboundMessage:
    conversation = ConversationRef("feishu", "cli_bot", conversation_id)
    return InboundMessage(
        conversation=conversation,
        actor=ActorRef(user_id),
        text=text,
        timestamp=1.0,
        message_id="om_inbound",
        images=images,
        files=files,
    )


def _interaction(
    source: MessageRef,
    *,
    user_id: str = "ou_user",
    interaction_id: str | None = None,
    response: ApprovalResponse | QuestionResponse | None = None,
) -> InboundInteraction:
    return InboundInteraction(
        source=source,
        actor=ActorRef(user_id),
        interaction_id=interaction_id,
        response=response,
    )


def _approval(approval_id: str = "approval-1") -> ApprovalRequest:
    return ApprovalRequest(
        id=approval_id,
        session_id="session-1",
        tool_name="Shell",
        action="Run command",
        input_display={"command": "touch approved.txt"},
    )


def _question_request(
    question_id: str = "question-1",
    *,
    allow_other: bool = True,
) -> QuestionRequest:
    return QuestionRequest(
        id=question_id,
        session_id="session-1",
        questions=(
            Question(
                id="q1",
                text="Pick one",
                header="Choice",
                options=(
                    QuestionOption(id="one", label="One"),
                    QuestionOption(id="two", label="Two"),
                ),
                allow_other=allow_other,
                other_label="Something else",
            ),
        ),
    )


def _control_session(
    *,
    busy: bool = False,
    model: str = "kimi-code/k3",
    thinking: str = "high",
    plan_mode: bool = False,
    pending_interaction: str = "none",
    usage: SessionUsage | None = None,
) -> dict[str, Any]:
    return {
        "id": "session-control",
        "title": "Control session",
        "busy": busy,
        "pending_interaction": pending_interaction,
        "metadata": {"cwd": "/tmp/control"},
        "agent_config": {
            "model": model,
            "thinking": thinking,
            "permission_mode": "manual",
            "plan_mode": plan_mode,
        },
        "usage": usage or SessionUsage(10, 20, 3, 4, 30, 120),
    }


def _bind_control_session(store: StateStore) -> None:
    store.save(
        BridgeState(
            bindings={
                "feishu:cli_bot:ou_user": ConversationBinding(
                    session_id="session-control",
                    workspace="/tmp/control",
                    permission_mode="manual",
                )
            }
        )
    )


async def test_first_message_creates_manual_session_and_persists_binding(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    store = StateStore(tmp_path / "state.json")
    workspace = tmp_path / "workspace"
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=workspace,
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("  hello from Feishu  "))
    finally:
        await router.close()

    assert client.created == [
        (
            str(workspace.resolve()),
            "hello from Feishu",
            {"model": "kimi-code/k3", "permission_mode": "manual"},
        )
    ]
    assert client.prompts == [
        (
            "session-1",
            [{"type": "text", "text": "hello from Feishu"}],
            {"permission_mode": "manual"},
        )
    ]
    binding = store.load().bindings["feishu:cli_bot:ou_user"]
    assert binding.session_id == "session-1"
    assert binding.workspace == str(workspace.resolve())
    assert binding.permission_mode == "manual"


async def test_persisted_auto_binding_keeps_mode_and_subscribes(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    store = StateStore(tmp_path / "state.json")
    store.save(
        BridgeState(
            bindings={
                "feishu:cli_bot:ou_user": ConversationBinding(
                    session_id="session-restored",
                    workspace=str(tmp_path),
                    permission_mode="auto",
                )
            }
        )
    )
    client.sessions = [
        {
            "id": "session-restored",
            "title": "Restored",
            "busy": False,
            "metadata": {"cwd": str(tmp_path)},
            "agent_config": {"permission_mode": "auto"},
        }
    ]
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("after restart"))
    finally:
        await router.close()

    assert client.stream_actions == [("subscribe", "session-restored")]
    assert client.prompts[0][2]["permission_mode"] == "auto"


async def test_close_after_runtime_stream_failure_is_clean(tmp_path: Path) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    await router.handle_inbound(adapter, _message("start the stream"))
    client.fail_stream(
        "session-1",
        KimiServerAPIError(42901, "provider failed after subscription"),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await router.close()


async def test_bridge_commands_switch_stop_and_mode(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [
        {
            "id": "session-a",
            "title": "Alpha",
            "busy": False,
            "metadata": {"cwd": "/tmp/alpha"},
            "agent_config": {"permission_mode": "auto"},
        },
        {
            "id": "session-b",
            "title": "Beta",
            "busy": True,
            "metadata": {"cwd": "/tmp/beta"},
            "agent_config": {"permission_mode": "manual"},
        },
    ]
    adapter = FakeAdapter()
    store = StateStore(tmp_path / "state.json")
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("/help"))
        await router.handle_inbound(adapter, _message("/sessions"))
        await router.handle_inbound(adapter, _message("/switch 2"))
        await router.handle_inbound(adapter, _message("/mode yolo"))
        await router.handle_inbound(adapter, _message("/stop"))
    finally:
        await router.close()

    texts = [text for _message, _conversation, text in adapter.sent]
    help_text = next(text for text in texts if text.startswith("Commands:"))
    for grammar in (
        "/mode <manual|auto|yolo>",
        "/model [alias]",
        "/effort [effort]",
        "/plan [on|off]",
        "/status",
        "/title [text]",
        "/usage",
        "/tasks [running|completed|failed|cancelled]",
        "/skills run <name> [args]",
        "/mcp",
    ):
        assert grammar in help_text
    assert any("Alpha [idle]" in text and "Beta [busy]" in text for text in texts)
    assert any("Switched to session-b" in text for text in texts)
    assert any("Permission mode: yolo" in text for text in texts)
    assert any(text == "Stopped." for text in texts)
    assert client.profile_updates == [("session-b", {"permission_mode": "yolo"})]
    assert client.aborted == ["session-b"]
    binding = store.load().bindings["feishu:cli_bot:ou_user"]
    assert binding.session_id == "session-b"
    assert binding.permission_mode == "yolo"


async def test_model_and_effort_commands_use_exact_catalog_and_profile_inheritance(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session()]
    client.models.extend(
        (
            ModelInfo(
                alias="kimi-code/other",
                provider="kimi-code",
                display_name="Other",
                max_context_size=131_072,
                capabilities=("thinking",),
                support_efforts=("low", "high"),
                default_effort="high",
            ),
            ModelInfo(
                alias="kimi-code/legacy",
                provider="kimi-code",
                display_name="Legacy",
                max_context_size=131_072,
                capabilities=("thinking", "always_thinking"),
                support_efforts=(),
                default_effort=None,
            ),
            ModelInfo(
                alias="kimi-code/fast",
                provider="kimi-code",
                display_name="Fast",
                max_context_size=131_072,
                capabilities=(),
                support_efforts=(),
                default_effort=None,
            ),
        )
    )
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="startup-default",
    )
    try:
        await router.handle_inbound(adapter, _message("/model"))
        await router.handle_inbound(adapter, _message("/model K3"))
        await router.handle_inbound(adapter, _message("/effort"))
        await router.handle_inbound(adapter, _message("/effort off"))
        await router.handle_inbound(adapter, _message("/effort max"))
        await router.handle_inbound(
            adapter, _message("/model kimi-code/other")
        )
        await router.handle_inbound(
            adapter, _message("/model kimi-code/legacy")
        )
        await router.handle_inbound(adapter, _message("/effort"))
        await router.handle_inbound(adapter, _message("/effort off"))
        await router.handle_inbound(
            adapter, _message("/model kimi-code/fast")
        )
        await router.handle_inbound(adapter, _message("/effort high"))
        await router.handle_inbound(adapter, _message("/effort off"))
        await router.handle_inbound(adapter, _message("profile survives"))
    finally:
        await router.close()

    texts = [text for _message_ref, _conversation, text in adapter.sent]
    catalog = next(text for text in texts if "Available models:" in text)
    assert "kimi-code/k3 — K3" in catalog
    assert "kimi-code/other — Other" in catalog
    assert "kimi-code/legacy — Legacy — thinking efforts: on" in catalog
    assert "kimi-code/fast — Fast" in catalog
    assert any("Unknown model alias: K3" in text for text in texts)
    assert any("Valid choices: low, high, max" in text for text in texts)
    assert any(
        "Unsupported effort for kimi-code/k3: off" in text for text in texts
    )
    assert any("max -> high" in text for text in texts)
    assert any("high -> on" in text for text in texts)
    assert any("Valid choices: on" in text for text in texts)
    assert any(
        "Unsupported effort for kimi-code/legacy: off" in text
        for text in texts
    )
    assert any("on -> off" in text for text in texts)
    assert any("Unsupported effort for kimi-code/fast" in text for text in texts)
    assert client.profile_updates == [
        ("session-control", {"thinking": "max"}),
        (
            "session-control",
            {"model": "kimi-code/other", "thinking": "high"},
        ),
        (
            "session-control",
            {"model": "kimi-code/legacy", "thinking": "on"},
        ),
        (
            "session-control",
            {"model": "kimi-code/fast", "thinking": "off"},
        ),
    ]
    assert client.prompts[-1][2] == {"permission_mode": "manual"}


async def test_plan_is_explicit_idle_only_and_idempotent(tmp_path: Path) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session(plan_mode=False)]
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("/plan"))
        await router.handle_inbound(adapter, _message("/plan on"))
        await router.handle_inbound(adapter, _message("/plan on"))
        await router.handle_inbound(adapter, _message("/plan off"))
        await router.handle_inbound(adapter, _message("/plan toggle"))
    finally:
        await router.close()

    assert client.profile_updates == [
        ("session-control", {"plan_mode": True}),
        ("session-control", {"plan_mode": False}),
    ]
    texts = [text for _message_ref, _conversation, text in adapter.sent]
    assert texts[0] == "Current plan mode: off"
    assert any("already: on" in text for text in texts)
    assert any("Usage: /plan" in text for text in texts)


async def test_status_title_and_usage_report_session_owned_values(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [
        _control_session(
            busy=True,
            thinking="max",
            plan_mode=True,
            pending_interaction="question",
            usage=SessionUsage(100, 40, 25, 5, 300, 1200),
        )
    ]
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("/title"))
        await router.handle_inbound(
            adapter, _message("/title   Exact  Title  ")
        )
        await router.handle_inbound(adapter, _message("/status"))
        await router.handle_inbound(adapter, _message("/usage"))
        client.sessions[0]["usage"] = SessionUsage(
            None, None, None, None, None, None
        )
        await router.handle_inbound(adapter, _message("/usage"))
    finally:
        await router.close()

    texts = [text for _message_ref, _conversation, text in adapter.sent]
    assert "Title: Control session" in texts
    assert "Title: Exact  Title" in texts
    status_text = next(text for text in texts if text.startswith("Session:"))
    assert "Session: Exact  Title" in status_text
    assert "State: busy" in status_text
    assert "Pending interaction: question" in status_text
    assert "Thinking effort: max" in status_text
    assert "Plan mode: on" in status_text
    assert "Permission mode: manual" in status_text
    assert "Kimi-code: 0.28.1" in status_text
    usage_text = next(text for text in texts if "Input tokens: 100" in text)
    assert "Cache-read tokens: 25" in usage_text
    assert "Context: 300/1200 (25.0%)" in usage_text
    assert "Turns:" not in usage_text
    assert "Cost:" not in usage_text
    assert texts[-1].count("unknown") >= 5
    assert client.profile_updates == [
        ("session-control", {"title": "Exact  Title"})
    ]


async def test_empty_session_uses_configured_default_as_effective_model(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session(model="", thinking="off")]
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("/model"))
        await router.handle_inbound(adapter, _message("/effort"))
        await router.handle_inbound(adapter, _message("/status"))
        await router.handle_inbound(adapter, _message("/effort high"))
    finally:
        await router.close()

    texts = [text for _message_ref, _conversation, text in adapter.sent]
    assert "Current model: kimi-code/k3" in texts[0]
    assert "Valid choices: low, high, max" in texts[1]
    assert "Model: kimi-code/k3" in texts[2]
    assert client.profile_updates == [
        ("session-control", {"thinking": "high"})
    ]


async def test_task_commands_filter_bound_output_and_cancel_while_busy(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    running = TaskInfo(
        id="task-running",
        session_id="session-control",
        kind="bash",
        description="Long command",
        status="running",
        command="sleep 60",
        created_at="created",
        started_at="started",
        output_preview="last output",
        output_bytes=10000,
    )
    completed = TaskInfo(
        id="task-complete",
        session_id="session-control",
        kind="subagent",
        description="Finished helper",
        status="completed",
        command=None,
        created_at="created",
        completed_at="completed",
    )
    client.tasks["session-control"] = [running, completed]
    client.task_details[("session-control", "task-running")] = running
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("/tasks"))
        await router.handle_inbound(adapter, _message("/tasks running"))
        await router.handle_inbound(
            adapter, _message("/tasks show task-running")
        )
        await router.handle_inbound(
            adapter, _message("/tasks cancel task-running")
        )
        await router.handle_inbound(adapter, _message("/tasks unknown"))
    finally:
        await router.close()

    assert client.task_list_calls == [
        ("session-control", None),
        ("session-control", "running"),
    ]
    assert client.task_detail_calls == [
        ("session-control", "task-running", 8192)
    ]
    assert client.cancelled_tasks == [("session-control", "task-running")]
    texts = [text for _message_ref, _conversation, text in adapter.sent]
    assert any("task-running [running] bash" in text for text in texts)
    assert any("task-complete [completed] subagent" in text for text in texts)
    assert any("Output tail:\nlast output" in text for text in texts)
    assert any(text == "Cancelled task task-running" for text in texts)
    assert any("Usage: /tasks" in text for text in texts)


async def test_skills_activate_after_subscription_and_mcp_is_session_scoped(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session()]
    client.skills["session-control"] = [
        SkillInfo(
            name="harmless",
            description="Reply without tools",
            source="user",
            path="/tmp/harmless/SKILL.md",
        )
    ]
    client.tools["session-control"] = [
        ToolInfo("builtin", "Built in", "builtin"),
        ToolInfo("search", "Search docs", "mcp", "docs"),
        ToolInfo("lookup", "Look up records", "mcp", "records"),
        ToolInfo("orphan", "Missing server", "mcp"),
    ]
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("/skills"))
        await router.handle_inbound(
            adapter, _message("/skills run missing")
        )
        sent_before_activation = len(adapter.sent)
        await router.handle_inbound(
            adapter, _message("/skills run harmless focus tests")
        )
        assert len(adapter.sent) == sent_before_activation
        client.emit("session-control", _event("turn.started"))
        client.emit(
            "session-control",
            _event("assistant.delta", delta="SKILL_OK", offset=0),
        )
        await _wait_for(lambda: len(adapter.sent) == sent_before_activation + 1)
        client.snapshots["session-control"] = {
            "in_flight_turn": None,
            "messages": {
                "items": [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "SKILL_OK"}],
                    }
                ]
            },
        }
        client.emit("session-control", _event("turn.ended"))
        await asyncio.sleep(0)
        await router.handle_inbound(adapter, _message("/mcp"))
    finally:
        await router.close()

    assert client.activated_skills == [
        ("session-control", "harmless", "focus tests")
    ]
    assert client.activation_subscription_ready == [True]
    texts = [text for _message_ref, _conversation, text in adapter.sent]
    assert any("harmless [user]" in text for text in texts)
    assert any("Unknown skill: missing" in text for text in texts)
    assert texts.count("SKILL_OK") == 1
    mcp_text = next(text for text in texts if text.startswith("MCP servers:"))
    assert "docs\n- search — Search docs" in mcp_text
    assert "records\n- lookup — Look up records" in mcp_text
    assert "builtin" not in mcp_text
    assert "orphan" not in mcp_text


async def test_busy_state_matrix_allows_reads_title_and_task_cancel_only(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    client.models.append(
        ModelInfo(
            alias="kimi-code/fast",
            provider="kimi-code",
            display_name="Fast",
            max_context_size=131_072,
            capabilities=(),
            support_efforts=(),
            default_effort=None,
        )
    )
    task = TaskInfo(
        id="task-1",
        session_id="session-control",
        kind="tool",
        description="Busy task",
        status="running",
        command=None,
        created_at="created",
    )
    client.tasks["session-control"] = [task]
    client.task_details[("session-control", "task-1")] = task
    client.skills["session-control"] = [
        SkillInfo("harmless", "Harmless", "user", "/tmp/skill")
    ]
    client.tools["session-control"] = [
        ToolInfo("search", "Search", "mcp", "server")
    ]
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="kimi-code/k3",
    )
    reads_and_allowed_mutations = (
        "/model",
        "/effort",
        "/plan",
        "/status",
        "/title",
        "/usage",
        "/tasks",
        "/tasks running",
        "/tasks show task-1",
        "/tasks cancel task-1",
        "/skills",
        "/mcp",
        "/title Busy Rename",
    )
    rejected_mutations = (
        "/model kimi-code/fast",
        "/effort off",
        "/plan on",
        "/skills run harmless",
    )
    try:
        for command in reads_and_allowed_mutations:
            await router.handle_inbound(adapter, _message(command))
        for command in rejected_mutations:
            await router.handle_inbound(adapter, _message(command))
    finally:
        await router.close()

    assert client.profile_updates == [
        ("session-control", {"title": "Busy Rename"})
    ]
    assert client.cancelled_tasks == [("session-control", "task-1")]
    assert client.activated_skills == []
    texts = [text for _message_ref, _conversation, text in adapter.sent]
    busy_rejections = [text for text in texts if text.startswith("Session is busy.")]
    assert len(busy_rejections) == len(rejected_mutations)


async def test_control_commands_require_binding_and_surface_upstream_errors(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    store = StateStore(tmp_path / "state.json")
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="kimi-code/k3",
    )
    try:
        for command in (
            "/model",
            "/effort",
            "/plan",
            "/status",
            "/title",
            "/usage",
            "/tasks",
            "/skills",
            "/mcp",
        ):
            await router.handle_inbound(adapter, _message(command))
    finally:
        await router.close()
    assert [text for _ref, _conversation, text in adapter.sent] == [
        "No bound session."
    ] * 9

    failing_client = FakeKimiClient()
    failing_client.sessions = [_control_session()]

    async def fail_models() -> list[ModelInfo]:
        raise KimiServerAPIError(50301, "catalog unavailable")

    failing_client.list_models = fail_models  # type: ignore[method-assign]
    failing_store = StateStore(tmp_path / "failing-state.json")
    _bind_control_session(failing_store)
    failing_adapter = FakeAdapter()
    failing_router = ChatRouter(
        failing_client,  # type: ignore[arg-type]
        state_store=failing_store,
        default_workspace=tmp_path,
        model="kimi-code/k3",
    )
    try:
        await failing_router.handle_inbound(failing_adapter, _message("/model"))
    finally:
        await failing_router.close()
    assert "catalog unavailable" in failing_adapter.sent[-1][2]


async def test_control_command_grammars_reject_extra_or_incomplete_arguments(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [_control_session()]
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="kimi-code/k3",
    )
    try:
        for command in (
            "/status now",
            "/usage now",
            "/mcp restart",
            "/skills activate harmless",
            "/skills run",
            "/tasks show",
            "/tasks cancel",
            "/plan maybe",
        ):
            await router.handle_inbound(adapter, _message(command))
    finally:
        await router.close()

    texts = [text for _ref, _conversation, text in adapter.sent]
    assert any(text == "Usage: /status" for text in texts)
    assert any(text == "Usage: /usage" for text in texts)
    assert any(text == "Usage: /mcp" for text in texts)
    assert sum("Usage: /skills run" in text for text in texts) == 2
    assert sum("Usage: /tasks" in text for text in texts) == 2
    assert any("Usage: /plan" in text for text in texts)
    assert client.profile_updates == []
    assert client.activated_skills == []
    assert client.cancelled_tasks == []
    assert client.tools == {}


async def test_switched_session_profile_is_not_overridden_by_next_prompt(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    first = _control_session(model="kimi-code/k3", thinking="high")
    second = {
        "id": "session-other",
        "title": "Other session",
        "busy": False,
        "pending_interaction": "none",
        "metadata": {"cwd": "/tmp/other"},
        "agent_config": {
            "model": "kimi-code/other",
            "thinking": "low",
            "permission_mode": "auto",
            "plan_mode": True,
        },
        "usage": SessionUsage(0, 0, 0, 0, 0, 131_072),
    }
    client.sessions = [first, second]
    store = StateStore(tmp_path / "state.json")
    _bind_control_session(store)
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path,
        model="startup-default",
    )
    try:
        await router.handle_inbound(
            adapter, _message("/switch session-other")
        )
        await router.handle_inbound(adapter, _message("after switch"))
    finally:
        await router.close()

    assert client.prompts == [
        (
            "session-other",
            [{"type": "text", "text": "after switch"}],
            {"permission_mode": "auto"},
        )
    ]
    assert second["agent_config"] == {
        "model": "kimi-code/other",
        "thinking": "low",
        "permission_mode": "auto",
        "plan_mode": True,
    }
    assert client.profile_updates == []


async def test_switch_stream_failure_is_visible_and_does_not_rebind(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [
        {
            "id": "session-broken",
            "title": "Broken",
            "busy": False,
            "metadata": {"cwd": "/tmp/missing"},
            "agent_config": {"permission_mode": "manual"},
        }
    ]
    client.stream_errors["session-broken"] = KimiServerAPIError(
        40409, "workspace root does not exist"
    )
    adapter = FakeAdapter()
    store = StateStore(tmp_path / "state.json")
    store.save(
        BridgeState(
            bindings={
                "feishu:cli_bot:ou_user": ConversationBinding(
                    session_id="session-working",
                    workspace=str(tmp_path),
                    permission_mode="manual",
                )
            }
        )
    )
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("/switch session-broken"))
    finally:
        await router.close()

    texts = [text for _message, _conversation, text in adapter.sent]
    assert any(
        "session-broken" in text and "workspace root does not exist" in text
        for text in texts
    )
    binding = store.load().bindings["feishu:cli_bot:ou_user"]
    assert binding.session_id == "session-working"


async def test_new_command_uses_requested_workspace_without_forwarding(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.json")
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path / "scratch",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message(f"/new {project}"))
    finally:
        await router.close()

    assert client.created[0][0] == str(project.resolve())
    assert client.created[0][2] == {
        "model": "kimi-code/k3",
        "permission_mode": "manual",
    }
    assert client.prompts == []


async def test_submit_then_steer_and_no_active_turn_fallback(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.prompt_statuses = ["running", "queued", "queued"]
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("first"))
        await router.handle_inbound(adapter, _message("change course"))
        client.steer_error = KimiServerAPIError(40001, "no active turn")
        await router.handle_inbound(adapter, _message("race fallback"))
    finally:
        await router.close()

    assert client.call_order == ["submit", "submit", "steer", "submit", "steer"]
    assert client.steered == [
        ("session-1", ["prompt-2"]),
        ("session-1", ["prompt-3"]),
    ]


async def test_approval_interaction_resolves_and_rejects_wrong_actor(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.approvals["session-1"] = [_approval()]
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("run a command"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        message, conversation, prompt = adapter.interactions[0]
        assert conversation == _message("").conversation
        assert isinstance(prompt, ApprovalPrompt)
        assert prompt.request == _approval()
        assert prompt.session_title == "run a command"
        assert prompt.workspace == str((tmp_path / "workspace").resolve())

        await router.handle_interaction(
            adapter,
            _interaction(
                message,
                user_id="ou_other",
                interaction_id=prompt.interaction_id,
                response=ApprovalResponse("approved"),
            ),
        )
        assert client.resolved_approvals == []
        await router.handle_interaction(
            adapter,
            _interaction(
                message,
                interaction_id=prompt.interaction_id,
                response=ApprovalResponse("approved"),
            ),
        )
    finally:
        await router.close()

    assert client.resolved_approvals == [("session-1", "approval-1", "approved")]
    assert len(adapter.outcomes) == 1
    assert adapter.outcomes[0][0] == message
    assert adapter.outcomes[0][1].state == "completed"
    assert len(adapter.sent) == 1
    assert adapter.sent[0][1] == message.conversation


async def test_question_option_and_free_text_paths(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.questions["session-1"] = [_question_request()]
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("ask me"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        first_message, _conversation, first_prompt = adapter.interactions[0]
        assert isinstance(first_prompt, QuestionPrompt)
        assert first_prompt.request.questions[0].header == "Choice"
        assert first_prompt.request.questions[0].text == "Pick one"
        await router.handle_interaction(
            adapter,
            _interaction(
                first_message,
                interaction_id=first_prompt.interaction_id,
                response=QuestionResponse(
                    (SingleChoiceAnswer("q1", "one"),)
                ),
            ),
        )

        client.questions["session-1"] = [_question_request("question-2")]
        assert router._active is not None
        await router._discover_interaction(router._active)
        second_message, _conversation, second_prompt = adapter.interactions[1]
        assert isinstance(second_prompt, QuestionPrompt)
        await router.handle_interaction(
            adapter,
            _interaction(
                second_message,
                interaction_id=second_prompt.interaction_id,
                response=QuestionResponse((OtherAnswer("q1", "custom"),)),
            ),
        )

        client.questions["session-1"] = [_question_request("question-3")]
        await router._discover_interaction(router._active)
        third_message, _conversation, third_prompt = adapter.interactions[2]
        assert isinstance(third_prompt, QuestionPrompt)
        await router.handle_interaction(
            adapter,
            _interaction(
                third_message,
                interaction_id=third_prompt.interaction_id,
                response=QuestionResponse((SkippedAnswer("q1"),)),
            ),
        )
    finally:
        await router.close()

    assert client.resolved_questions == [
        (
            "session-1",
            "question-1",
            (SingleChoiceAnswer("q1", "one"),),
        ),
        (
            "session-1",
            "question-2",
            (OtherAnswer("q1", "custom"),),
        ),
        (
            "session-1",
            "question-3",
            (SkippedAnswer("q1"),),
        ),
    ]


async def test_multi_question_form_maps_all_answer_shapes(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    request = QuestionRequest(
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
    )
    client.questions["session-1"] = [request]
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("ask many"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        message, _conversation, prompt = adapter.interactions[0]
        assert isinstance(prompt, QuestionPrompt)
        await router.handle_interaction(
            adapter,
            _interaction(
                message,
                interaction_id=prompt.interaction_id,
                response=QuestionResponse(
                    (
                        SkippedAnswer("single"),
                        MultipleChoiceWithOtherAnswer(
                            "multi", ("x",), "custom"
                        ),
                        MultipleChoiceAnswer(
                            "multi-only", ("left", "right")
                        ),
                    )
                ),
            ),
        )
    finally:
        await router.close()

    assert client.resolved_questions[-1][2] == (
        SkippedAnswer("single"),
        MultipleChoiceWithOtherAnswer("multi", ("x",), "custom"),
        MultipleChoiceAnswer("multi-only", ("left", "right")),
    )


async def test_stop_cancels_pending_approval_and_makes_callback_stale(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.approvals["session-1"] = [_approval()]
    adapter = FakeAdapter()
    never_timeout = asyncio.Event()

    async def timeout_sleep(_delay: float) -> None:
        await never_timeout.wait()

    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
        interaction_sleep=timeout_sleep,
    )
    try:
        await router.handle_inbound(adapter, _message("run"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        message, _conversation, prompt = adapter.interactions[0]

        await router.handle_inbound(adapter, _message("/stop"))
        await router.handle_interaction(
            adapter,
            _interaction(
                message,
                interaction_id=prompt.interaction_id,
                response=ApprovalResponse("approved"),
            ),
        )
    finally:
        await router.close()

    assert client.aborted == ["session-1"]
    assert client.resolved_approvals == []
    assert [outcome.state for _message, outcome in adapter.outcomes] == [
        "cancelled",
        "stale",
    ]
    assert any(text == "Stopped." for _ref, _conversation, text in adapter.sent)


async def test_stop_cancels_pending_question_without_dismissing_it(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.questions["session-1"] = [_question_request()]
    adapter = FakeAdapter()
    never_timeout = asyncio.Event()

    async def timeout_sleep(_delay: float) -> None:
        await never_timeout.wait()

    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
        interaction_sleep=timeout_sleep,
    )
    try:
        await router.handle_inbound(adapter, _message("ask"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        await router.handle_inbound(adapter, _message("/stop"))
    finally:
        await router.close()

    assert client.aborted == ["session-1"]
    assert client.resolved_questions == []
    assert client.dismissed_questions == []
    assert adapter.outcomes[-1][1].state == "cancelled"


async def test_stop_aborts_pending_interaction_after_binding_changes(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.approvals["session-1"] = [_approval()]
    adapter = FakeAdapter()
    never_timeout = asyncio.Event()

    async def timeout_sleep(_delay: float) -> None:
        await never_timeout.wait()

    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
        interaction_sleep=timeout_sleep,
    )
    new_workspace = tmp_path / "new-workspace"
    new_workspace.mkdir()
    try:
        await router.handle_inbound(adapter, _message("run"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        await router.handle_inbound(
            adapter, _message(f"/new {new_workspace}")
        )
        await router.handle_inbound(adapter, _message("/stop"))
    finally:
        await router.close()

    assert client.aborted == ["session-1", "session-2"]
    assert adapter.outcomes[-1][1].state == "cancelled"


async def test_approval_timeout_auto_rejects_and_finishes_interaction(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.approvals["session-1"] = [_approval()]
    adapter = FakeAdapter()
    release_timeout = asyncio.Event()
    delays: list[float] = []

    async def timeout_sleep(delay: float) -> None:
        delays.append(delay)
        await release_timeout.wait()

    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
        interaction_timeout_seconds=12,
        interaction_sleep=timeout_sleep,
    )
    try:
        await router.handle_inbound(adapter, _message("run"))
        await _wait_for(lambda: len(adapter.interactions) == 1 and bool(delays))
        release_timeout.set()
        await _wait_for(lambda: bool(client.resolved_approvals))
    finally:
        await router.close()

    assert delays == [12]
    assert client.resolved_approvals == [("session-1", "approval-1", "rejected")]
    assert adapter.outcomes[0][1].state == "timed_out"
    assert len(adapter.sent) == 1


async def test_question_timeout_dismisses_request(tmp_path: Path) -> None:
    client = FakeKimiClient()
    client.questions["session-1"] = [_question_request()]
    adapter = FakeAdapter()
    release_timeout = asyncio.Event()

    async def timeout_sleep(_delay: float) -> None:
        await release_timeout.wait()

    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
        interaction_sleep=timeout_sleep,
    )
    try:
        await router.handle_inbound(adapter, _message("ask"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        release_timeout.set()
        await _wait_for(lambda: bool(client.dismissed_questions))
    finally:
        await router.close()

    assert client.dismissed_questions == [("session-1", "question-1")]


async def test_stale_interaction_after_restart_is_explained_without_api_call(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )

    stale_message = MessageRef(_message("").conversation, "card-from-old-run")
    await router.handle_interaction(adapter, _interaction(stale_message))
    await router.close()

    assert client.resolved_approvals == []
    assert client.resolved_questions == []
    assert adapter.outcomes[0][0] == stale_message
    assert adapter.outcomes[0][1].state == "stale"
    assert len(adapter.sent) == 1


async def test_images_and_files_map_to_prompt_content_and_workspace_inbox(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    workspace = tmp_path / "workspace"
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=workspace,
        model="kimi-code/k3",
    )
    images = (
        InboundImage(b"one", "image/png"),
        InboundImage(b"two", "image/jpeg"),
    )
    files = (
        InboundFile(b"first", "../notes.txt", "text/plain"),
        InboundFile(b"second", "notes.txt", "text/plain"),
    )
    try:
        await router.handle_inbound(
            adapter,
            _message("inspect these", images=images, files=files),
        )
    finally:
        await router.close()

    content = client.prompts[0][1]
    assert isinstance(content, list)
    text = content[0]["text"]
    first_path = workspace / ".kimi-bridge-inbox" / "notes.txt"
    second_path = workspace / ".kimi-bridge-inbox" / "notes-1.txt"
    assert str(first_path.resolve()) in text
    assert str(second_path.resolve()) in text
    assert first_path.read_bytes() == b"first"
    assert second_path.read_bytes() == b"second"
    assert [item["source"] for item in content[1:]] == [
        {
            "kind": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(b"one").decode("ascii"),
        },
        {
            "kind": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode(b"two").decode("ascii"),
        },
    ]


async def test_delta_throttle_final_edit_and_router_chunking(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter(message_limit=4)
    conversation = _message("").conversation
    release_flush = asyncio.Event()
    delays: list[float] = []
    now = [100.0]

    async def controlled_sleep(delay: float) -> None:
        delays.append(delay)
        await release_flush.wait()

    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
        edit_throttle_seconds=1.5,
        sleep=controlled_sleep,
        clock=lambda: now[0],
    )
    try:
        await router.handle_inbound(adapter, _message("hello"))
        client.emit("session-1", _event("turn.started"))
        client.emit("session-1", _event("assistant.delta", delta="abc", offset=0))
        await _wait_for(lambda: len(adapter.sent) == 1)
        assert adapter.sent[0][2] == "abc"

        client.emit("session-1", _event("assistant.delta", delta="def", offset=3))
        await _wait_for(lambda: bool(delays))
        assert adapter.edits == []
        assert len(adapter.sent) == 1

        release_flush.set()
        await _wait_for(lambda: len(adapter.sent) == 2 and bool(adapter.edits))
        assert adapter.edits == [
            (MessageRef(conversation, "message-1"), "abcd")
        ]
        assert adapter.sent[1][2] == "ef"

        client.snapshots["session-1"] = {
            "in_flight_turn": None,
            "messages": {
                "items": [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "abcdefgh"}],
                    }
                ]
            },
        }
        client.emit("session-1", _event("turn.ended"))
        await _wait_for(
            lambda: (MessageRef(conversation, "message-2"), "efgh")
            in adapter.edits
        )
    finally:
        await router.close()

    assert delays == [1.5]
    assert adapter.sent == [
        (MessageRef(conversation, "message-1"), conversation, "abc"),
        (MessageRef(conversation, "message-2"), conversation, "ef"),
    ]
    assert adapter.edits[-1] == (
        MessageRef(conversation, "message-2"),
        "efgh",
    )


async def test_resync_snapshot_rebuilds_in_flight_stream(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter(message_limit=4)
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("hello"))
        client.emit(
            "session-1",
            {
                "type": "resync_required",
                "payload": {"type": "resync_required"},
                "snapshot": {
                    "in_flight_turn": {"assistant_text": "abcdefghi"},
                    "messages": {"items": []},
                },
            },
        )
        await _wait_for(lambda: len(adapter.sent) == 3)
    finally:
        await router.close()

    assert [text for _message, _conversation, text in adapter.sent] == [
        "abcd",
        "efgh",
        "i",
    ]


def _event(
    event_type: str, *, delta: str | None = None, offset: int | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": event_type}
    if delta is not None:
        payload["delta"] = delta
    event: dict[str, Any] = {"type": event_type, "payload": payload}
    if offset is not None:
        event["offset"] = offset
    return event


async def _wait_for(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")
