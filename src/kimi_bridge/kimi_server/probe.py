"""Credential-free behavioral probe for one managed Kimi runtime."""

from __future__ import annotations

from pathlib import Path

from .client import KimiServerClient
from .contract import (
    KimiCompatibilityProbe,
    KimiContractCheck,
    evaluate_kimi_semantic_contract,
)
from .supervisor import KimiServerSupervisor


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
            evaluate_kimi_semantic_contract(
                openapi, asyncapi, expected_version=version
            )
        )

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
        session_id = await client.create_session(
            str(workspace), model=default_model
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

    return KimiCompatibilityProbe(
        product=supervisor.executable_identity.product.value,
        version=version,
        checks=tuple(sorted((*checks, *runtime_checks), key=lambda item: item.id)),
        openapi=openapi,
        asyncapi=asyncapi,
    )


def _pass(
    identifier: str,
    category: str,
    detail: str,
    source: str,
) -> KimiContractCheck:
    return KimiContractCheck(identifier, category, "pass", detail, source)
