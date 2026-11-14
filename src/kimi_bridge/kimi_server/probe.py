"""Credential-free behavioral probe for one managed Kimi runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from .client import KimiServerClient
from .contract import (
    KIMI_LIFECYCLE_INVARIANTS,
    KimiCompatibilityProbe,
    KimiContractCheck,
    evaluate_kimi_semantic_contract,
)
from .supervisor import KimiServerSupervisor
from .contract_validation import _schema_accepts_instance
from .conversation import ConversationProjection
from .types import KimiServerAPIError, PromptOutcome


PROBED_LIFECYCLE_INVARIANTS = frozenset(
    identifier
    for identifier, _source in KIMI_LIFECYCLE_INVARIANTS
    if identifier != "server.intentional_restart"
)


async def probe_kimi_compatibility(
    supervisor: KimiServerSupervisor,
    workspace: Path,
    *,
    on_document: Callable[[Literal["openapi", "asyncapi"], dict[str, Any]], None]
    | None = None,
) -> KimiCompatibilityProbe:
    """Check native contracts and lifecycle behavior without inference."""

    runtime_checks: list[KimiContractCheck] = []
    connection = supervisor.connection
    runtime_checks.extend(
        (
            _pass(
                "runtime.lifecycle.startup.foreground",
                "runtime",
                "the foreground managed server reached its loopback endpoint",
                "KimiServerSupervisor",
            ),
            _pass(
                "runtime.lifecycle.startup.bearer_token",
                "runtime",
                "the managed startup line yielded a non-empty bearer token",
                "KimiServerSupervisor",
            ),
        )
    )
    if not connection.token:
        raise RuntimeError("managed Kimi connection has an empty bearer token")
    async with KimiServerClient(supervisor=supervisor) as client:
        version = await client.check_server_version()
        runtime_checks.append(
            _pass(
                "runtime.version.match",
                "runtime",
                "CLI and live server versions match",
                "KimiServerClient.check_server_version",
            )
        )
        openapi = await client.get_openapi_document()
        if on_document is not None:
            on_document("openapi", openapi)
        asyncapi = await client.get_asyncapi_document()
        if on_document is not None:
            on_document("asyncapi", asyncapi)
        runtime_checks.append(
            _pass(
                "runtime.lifecycle.rest.bearer_auth",
                "runtime",
                "bearer-authenticated REST and specification fetches succeed",
                "KimiServerClient._request_document",
            )
        )
        checks = list(
            evaluate_kimi_semantic_contract(openapi, asyncapi, expected_version=version)
        )
        checks.append(probe_turns_replay(asyncapi))

        workspace.mkdir(parents=True, exist_ok=True)
        default_model = await client.get_default_model()
        configured_secondary_model = await client.get_secondary_model()
        secondary_alias = configured_secondary_model.default_model
        if secondary_alias is None:
            raise RuntimeError(
                "compatibility probe requires a preconfigured secondary model"
            )
        secondary_model = await client.set_secondary_model(secondary_alias)
        if secondary_model.default_model != secondary_alias:
            raise RuntimeError("secondary model configuration did not persist")
        runtime_checks.append(
            _pass(
                "runtime.behavior.secondary_model.persistence",
                "runtime",
                "the subagent model default persists through public v1",
                "KimiServerClient.set_secondary_model",
            )
        )
        session_id = await client.create_session(str(workspace), model=default_model)
        await client.update_profile(session_id, model=default_model)
        if (await client.get_session_model(session_id)).alias != default_model:
            raise RuntimeError("the probe session did not retain its model binding")
        runtime_checks.append(
            _pass(
                "runtime.behavior.session.profile_binding",
                "runtime",
                "the session model persists after an explicit profile update",
                "KimiServerClient.update_profile/get_session_model",
            )
        )
        runtime_checks.append(
            _pass(
                "runtime.lifecycle.session.create",
                "runtime",
                "an empty session can be created without inference",
                "KimiServerClient.create_session",
            )
        )
        await client.probe_subscription(session_id)
        runtime_checks.extend(
            (
                _pass(
                    "runtime.lifecycle.websocket.subscribe_resumes",
                    "runtime",
                    "subscribe resumes the session and its response precedes "
                    "the initial agent and conversation frames",
                    "KimiServerClient.probe_subscription",
                ),
                _pass(
                    "runtime.lifecycle.websocket.response_before_agent_burst",
                    "runtime",
                    "task.list, agent, usage, goal, todo, and an authoritative "
                    "turns page follow the subscription response",
                    "KimiServerClient.probe_subscription",
                ),
                _pass(
                    "runtime.lifecycle.websocket.bearer_auth",
                    "runtime",
                    "the WebSocket accepts the managed bearer header",
                    "KimiServerClient.probe_subscription",
                ),
            )
        )
        await client.probe_subscription(session_id)
        runtime_checks.append(
            _pass(
                "runtime.lifecycle.websocket.turns_page_resume",
                "runtime",
                "a fresh subscription supplies an authoritative turns page "
                "without a sequence cursor",
                "KimiServerClient.probe_subscription",
            )
        )
        snapshot = await client.get_snapshot(session_id)
        if snapshot["messages"]["items"] or snapshot["in_flight_turn"] is not None:
            raise RuntimeError("new probe session has unexpected conversation content")
        runtime_checks.append(
            _pass(
                "runtime.behavior.turns_snapshot",
                "runtime",
                "the native turns page forms an empty snapshot without a "
                "sequence watermark",
                "KimiServerClient.get_snapshot",
            )
        )
        fork_id = await client.fork_session(session_id)
        if fork_id == session_id:
            raise RuntimeError("session fork did not create a distinct session")
        await client.archive_session(fork_id)
        if not await client.is_session_archived(fork_id):
            raise RuntimeError("session fork was not archived")
        restored_id = await client.restore_session(fork_id)
        if restored_id != fork_id or await client.is_session_archived(fork_id):
            raise RuntimeError("archived fork did not restore its native identity")
        runtime_checks.append(
            _pass(
                "runtime.behavior.session.fork_restore", "runtime",
                "an empty fork can be archived and explicitly restored without inference",
                "KimiServerClient.fork_session/restore_session",
            )
        )
        for command in (client.get_task, client.cancel_task):
            try:
                await command(session_id, "missing-canary-task")
            except KimiServerAPIError as exc:
                if exc.code != 40406:
                    raise
            else:
                raise RuntimeError("the server accepted a nonexistent task")
        runtime_checks.append(
            _pass(
                "runtime.behavior.task.direct_commands",
                "runtime",
                "direct task watch and stop preserve native missing-task responses",
                "KimiServerClient.get_task/cancel_task",
            )
        )
        await _probe_native_undo(client, session_id)
        runtime_checks.append(
            _pass(
                "runtime.behavior.turns.undo",
                "runtime",
                "an unreachable loopback provider opened a turn without inference; "
                "prompt cancellation ended it and native undo confirmed its opaque "
                "ID and removed it",
                "KimiServerClient.submit_prompt/cancel_prompt/undo_session",
            )
        )

    return KimiCompatibilityProbe(
        product=supervisor.executable_identity.product.value,
        version=version,
        checks=tuple(sorted((*checks, *runtime_checks), key=lambda item: item.id)),
        openapi=openapi,
        asyncapi=asyncapi,
    )


async def _probe_native_undo(client: KimiServerClient, session_id: str) -> None:
    submission = await client.submit_prompt(session_id, "Local compatibility probe")
    if submission.outcome is not PromptOutcome.SUBMITTED:
        raise RuntimeError(
            f"native undo probe could not open a turn: {submission.outcome.value}"
        )
    async with asyncio.timeout(15):
        while True:
            snapshot = await client.get_snapshot(session_id)
            current = snapshot["in_flight_turn"]
            if (
                current is not None
                and current.get("current_prompt_id") == submission.prompt_id
            ):
                break
            await asyncio.sleep(0.05)
    if not await client.cancel_prompt(session_id, submission.prompt_id):
        raise RuntimeError("native undo probe could not cancel the local turn")
    async with asyncio.timeout(15):
        while True:
            terminal = await client._terminal_turn_for_prompt(
                session_id, "main", submission.prompt_id
            )
            if terminal is not None:
                break
            await asyncio.sleep(0.05)
    if terminal.get("outcome") != "interrupted":
        raise RuntimeError("the native cancellation did not interrupt the local turn")
    await client.undo_session(session_id)
    snapshot = await client.get_snapshot(session_id)
    if (
        snapshot["turn_ids"]
        or snapshot["messages"]["items"]
        or snapshot["in_flight_turn"] is not None
    ):
        raise RuntimeError("native undo left conversation content in the probe session")


def probe_turns_replay(asyncapi: dict[str, Any]) -> KimiContractCheck:
    components = asyncapi.get("components")
    messages = components.get("messages") if isinstance(components, dict) else None
    messages = messages if isinstance(messages, dict) else {}
    base = {
        "session_id": "probe-session",
        "agent_id": "main",
        "event_created_at": "2026-01-01T00:00:00Z",
    }
    running = {
        "turn_id": "opaque-turn",
        "ordinal": 0,
        "kind": "agent",
        "status": "running",
        "started_at": "2026-01-01T00:00:00Z",
        "steps": [],
    }
    ended = {
        **running,
        "status": "ended",
        "outcome": "completed",
        "ended_at": "2026-01-01T00:00:01Z",
    }
    user = {
        "type": "user_message",
        "item_id": "user-1",
        "status": "ended",
        "outcome": "completed",
        "created_at": "2026-01-01T00:00:00Z",
        "prompt_ids": ["prompt-1"],
        "placement": "opener",
        "origin": {"kind": "user"},
        "text": "hello",
        "attachments": [],
        "skills": [],
    }
    reasoning = {
        "type": "reasoning",
        "item_id": "thinking-1",
        "step_index": 1,
        "status": "running",
        "created_at": "2026-01-01T00:00:00Z",
        "text": "",
        "started_at": "2026-01-01T00:00:00Z",
    }
    assistant = {
        "type": "assistant_message",
        "item_id": "answer-1",
        "step_index": 1,
        "status": "running",
        "created_at": "2026-01-01T00:00:00Z",
        "text": "",
    }
    frames = [
        ("turns", {**base, "type": "turns", "turns": [], "has_more": False}),
        ("turn", {**base, "type": "turn.started", "turn": running}),
        ("item", {**base, "type": "item.ended", "turn_id": "opaque-turn", "item": user}),
        ("item", {**base, "type": "item.started", "turn_id": "opaque-turn", "item": reasoning}),
        (
            "item_delta",
            {
                **base,
                "type": "item.delta",
                "turn_id": "opaque-turn",
                "item_id": "thinking-1",
                "field": "text",
                "delta": "Think",
            },
        ),
        ("item", {**base, "type": "item.started", "turn_id": "opaque-turn", "item": assistant}),
        (
            "item_delta",
            {
                **base,
                "type": "item.delta",
                "turn_id": "opaque-turn",
                "item_id": "answer-1",
                "field": "text",
                "delta": "Answer",
            },
        ),
        (
            "item",
            {
                **base,
                "type": "item.ended",
                "turn_id": "opaque-turn",
                "item": {
                    **assistant,
                    "status": "ended",
                    "outcome": "completed",
                    "text": "Corrected",
                },
            },
        ),
        ("turn", {**base, "type": "turn.ended", "turn": ended}),
        (
            "turns_removed",
            {**base, "type": "turns.removed", "turn_ids": ["opaque-turn"]},
        ),
    ]
    projection = ConversationProjection("probe-session")
    payloads: list[dict[str, Any]] = []
    resyncs: list[dict[str, Any]] = []
    try:
        for message_name, frame in frames:
            message = messages.get(message_name)
            schema = message.get("payload") if isinstance(message, dict) else None
            if not isinstance(schema, dict) or not _schema_accepts_instance(schema, frame):
                raise ValueError(f"the native schema rejects {frame['type']}")
            events = projection.apply_frame(frame)
            payloads.extend(
                event["payload"]
                for event in events
                if event["type"] == "session_event"
            )
            resyncs.extend(
                event["snapshot"]
                for event in events
                if event["type"] == "resync_required"
            )
            if frame["type"] == "turn.ended" and projection.apply_frame(frame):
                raise ValueError("a repeated terminal turn emitted duplicate events")
        expected = [
            "turn.started",
            "turn.step.started",
            "thinking.delta",
            "assistant.delta",
            "turn.ended",
            "prompt.completed",
        ]
        actual = [payload["type"] for payload in payloads]
        if actual != expected:
            raise ValueError(f"unexpected semantic turn lifecycle: {actual!r}")
        if (
            len(resyncs) != 2
            or resyncs[0]["in_flight_turn"]["assistant_text"] != "Corrected"
            or resyncs[1]["turn_ids"]
        ):
            raise ValueError("native correction or opaque-ID removal did not reconcile")
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return KimiContractCheck(
            "runtime.behavior.turns.replay",
            "runtime",
            "fail",
            str(exc),
            "ConversationProjection",
        )
    return _pass(
        "runtime.behavior.turns.replay",
        "runtime",
        "native-schema turn/item replay preserves independent deltas, corrections, "
        "terminal prompts, and opaque-ID removal",
        "ConversationProjection",
    )


def _pass(
    identifier: str,
    category: str,
    detail: str,
    source: str,
) -> KimiContractCheck:
    return KimiContractCheck(identifier, category, "pass", detail, source)
