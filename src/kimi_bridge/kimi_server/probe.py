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
        "session.materialize_before_subscribe",
        "websocket.bearer_auth",
        "websocket.subscribe_ack",
        "websocket.reconnect_materializes",
    }
)


async def probe_kimi_compatibility(
    supervisor: KimiServerSupervisor,
    workspace: Path,
) -> KimiCompatibilityProbe:
    """Check specs plus create/materialize/subscribe/reconnect without inference."""

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
        session_id = await client.create_session(str(workspace))
        runtime_checks.append(
            _pass(
                "runtime.lifecycle.session.create",
                "runtime",
                "an empty session can be created without inference",
                "KimiServerClient.create_session",
            )
        )
        await client.get_session_status(session_id)
        runtime_checks.append(
            _pass(
                "runtime.lifecycle.session.materialize_before_subscribe",
                "runtime",
                "the empty session can be materialized through public v1",
                "KimiServerClient.get_session_status",
            )
        )
        await client.probe_subscription(session_id)
        runtime_checks.append(
            _pass(
                "runtime.lifecycle.websocket.subscribe_ack",
                "runtime",
                "the materialized session accepts a WebSocket subscription",
                "KimiServerClient.probe_subscription",
            )
        )
        runtime_checks.append(
            _pass(
                "runtime.lifecycle.websocket.bearer_auth",
                "runtime",
                "the WebSocket accepts the managed bearer header",
                "KimiServerClient.probe_subscription",
            )
        )
        await client.probe_subscription(session_id)
        runtime_checks.append(
            _pass(
                "runtime.lifecycle.websocket.reconnect_materializes",
                "runtime",
                "a fresh subscription rematerializes before reconnecting",
                "KimiServerClient.probe_subscription",
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
