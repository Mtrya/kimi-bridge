"""Credential-free behavioral probe for one managed Kimi runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .client import KimiServerClient
from .contract import (
    KimiCompatibilityProbe,
    KimiContractCheck,
    evaluate_kimi_semantic_contract,
)
from .supervisor import KimiServerSupervisor
from .contract_validation import _schema_accepts_instance, _transcript_ops_schema
from .events import TranscriptProjection
from .types import KimiServerAPIError


PROBED_LIFECYCLE_INVARIANTS = frozenset(
    {
        "startup.foreground",
        "startup.bearer_token",
        "rest.bearer_auth",
        "session.create",
        "websocket.bearer_auth",
        "websocket.subscribe_v2_resumes",
        "websocket.agent_burst_before_ack",
        "websocket.transcript_seq_resume",
    }
)


async def probe_kimi_compatibility(
    supervisor: KimiServerSupervisor,
    workspace: Path,
) -> KimiCompatibilityProbe:
    """Check specs plus create/subscribe/resume without inference."""

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
        asyncapi = await client.get_asyncapi_document()
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
        checks.append(probe_transcript_replay(openapi))

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
        transcript_seq = await client.probe_subscription(session_id)
        runtime_checks.extend(
            (
                _pass(
                    "runtime.lifecycle.websocket.subscribe_v2_resumes",
                    "runtime",
                    "subscribe_v2 resumes the session and pushes agent state "
                    "frames before its acknowledgement",
                    "KimiServerClient.probe_subscription",
                ),
                _pass(
                    "runtime.lifecycle.websocket.agent_burst_before_ack",
                    "runtime",
                    "task.list, agent, usage, goal, todo, and a transcript "
                    "reset arrive before the subscription acknowledgement",
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
        await client.probe_subscription(session_id, since=transcript_seq)
        runtime_checks.append(
            _pass(
                "runtime.lifecycle.websocket.transcript_seq_resume",
                "runtime",
                "a fresh subscription resumes from the transcript sequence "
                "without resetting the stream",
                "KimiServerClient.probe_subscription",
            )
        )
        snapshot = await client.get_snapshot(session_id)
        if snapshot["messages"]["items"] or snapshot["in_flight_turn"] is not None:
            raise RuntimeError("new probe session has unexpected transcript content")
        runtime_checks.append(
            _pass(
                "runtime.behavior.transcript_snapshot",
                "runtime",
                "the transcript page and its own sequence watermark form an "
                "empty snapshot",
                "KimiServerClient.get_snapshot",
            )
        )
        fork_id = await client.fork_session(session_id)
        if fork_id == session_id:
            raise RuntimeError("session fork did not create a distinct session")
        await client.archive_session(fork_id)
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

    return KimiCompatibilityProbe(
        product=supervisor.executable_identity.product.value,
        version=version,
        checks=tuple(sorted((*checks, *runtime_checks), key=lambda item: item.id)),
        openapi=openapi,
        asyncapi=asyncapi,
    )


def probe_transcript_replay(openapi: dict[str, Any]) -> KimiContractCheck:
    """Exercise nonempty decoding with operations accepted by the live schema."""

    schema = _transcript_ops_schema(openapi)
    turn = {
        "kind": "turn",
        "turnId": "t0",
        "ordinal": 0,
        "state": "running",
        "origin": {"kind": "user"},
    }
    step = {
        "kind": "step",
        "stepId": "t0.1",
        "turnId": "t0",
        "ordinal": 1,
        "state": "running",
    }
    frame = {"kind": "text", "frameId": "f1", "role": "assistant", "text": "prefix 🙂"}
    snapshot = {
        "items": [{**turn, "steps": [{**step, "frames": [frame]}]}],
        "attachments": [],
        "interactions": [],
        "prompts": [],
    }
    batches = [
        ([{"op": "reset", "agentId": "main", "snapshot": snapshot}], "prefix 🙂"),
        (
            [
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t0",
                        "stepId": "t0.1",
                        "frameId": "f1",
                    },
                    "offset": 9,
                    "text": "tail",
                }
            ],
            "prefix 🙂tail",
        ),
        (
            [
                {
                    "op": "frame.upsert",
                    "turnId": "t0",
                    "stepId": "t0.1",
                    "frame": {**frame, "text": "corrected"},
                }
            ],
            "corrected",
        ),
        (
            [
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t0",
                        "stepId": "t0.1",
                        "frameId": "f1",
                    },
                    "offset": 0,
                    "text": "replaced",
                }
            ],
            "replaced",
        ),
        ([{"op": "items.remove", "ids": ["t0"]}], None),
    ]
    projection = TranscriptProjection("probe-session")
    try:
        if schema is None:
            raise ValueError("the native transcript journal schema is unavailable")
        for seq, (ops, expected) in enumerate(batches, 1):
            for op in ops:
                if not _schema_accepts_instance(schema, op):
                    raise ValueError(
                        f"the native schema rejects the replay operation {op['op']}"
                    )
            batch = {"seq": seq, "ops": ops}
            events = projection.apply_ops(batch)
            state = projection.snapshot()
            current = state["in_flight_turn"]
            if (current["assistant_text"] if current else None) != expected:
                raise ValueError(
                    "the reduced transcript differs from the replayed text"
                )
            if projection.apply_ops(batch) or projection.snapshot() != state:
                raise ValueError("a repeated journal batch emitted duplicate events")
            if seq == 2:
                deltas = [
                    (event.get("offset"), event["payload"].get("delta"))
                    for event in events
                    if event["payload"]["type"] == "assistant.delta"
                ]
                if deltas != [(8, "tail")]:
                    raise ValueError(
                        "the appended text did not reach the semantic stream"
                    )
            if seq in (1, 3, 4, 5):
                if not any(
                    event["type"] == "resync_required" and event["snapshot"] == state
                    for event in events
                ):
                    raise ValueError(
                        "a reset or correction emitted no authoritative snapshot"
                    )
    except (ValueError, RuntimeError) as exc:
        return KimiContractCheck(
            "runtime.behavior.transcript.replay",
            "runtime",
            "fail",
            str(exc),
            "TranscriptProjection",
        )
    return _pass(
        "runtime.behavior.transcript.replay",
        "runtime",
        "native-schema replay preserves reset, append, correction, removal, and sequence deduplication",
        "TranscriptProjection",
    )


def _pass(
    identifier: str,
    category: str,
    detail: str,
    source: str,
) -> KimiContractCheck:
    return KimiContractCheck(identifier, category, "pass", detail, source)
