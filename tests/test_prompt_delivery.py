from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from kimi_bridge.kimi_server import (
    InteractionResolution,
    KimiServerAPIError,
    KimiServerClient,
    KimiServerProtocolError,
    KimiServerTransportError,
    PromptDelivery,
    PromptOutcome,
    PromptSteeringError,
    PromptSubmission,
    PromptSubmissionUncertain,
    ServerConnection,
)


def envelope(data: Any = None, *, code: int = 0) -> dict[str, Any]:
    return {
        "code": code,
        "msg": "test response",
        "data": data,
        "request_id": "test-request",
    }


def exchange(
    responses: list[dict[str, Any] | BaseException],
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    pending: Iterator[dict[str, Any] | BaseException] = iter(responses)
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = next(pending)
        if isinstance(response, BaseException):
            raise response
        return httpx.Response(200, json=response)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), requests


@pytest.mark.parametrize(
    ("status", "delivery", "expected"),
    [
        ("running", PromptDelivery.STEER_IF_ACTIVE, PromptOutcome.SUBMITTED),
        ("blocked", PromptDelivery.STEER_IF_ACTIVE, PromptOutcome.BLOCKED),
        ("queued", PromptDelivery.ENQUEUE, PromptOutcome.SUBMITTED),
        ("blocked", PromptDelivery.ENQUEUE, PromptOutcome.BLOCKED),
    ],
)
async def test_submission_without_steering(status, delivery, expected) -> None:
    http, requests = exchange([envelope({"prompt_id": "p1", "status": status})])
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        result = await client.submit_prompt("s1", "hello", delivery=delivery)
    assert result == PromptSubmission("p1", expected)
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "content": [{"type": "text", "text": "hello"}]
    }


@pytest.mark.parametrize("steer_code", [0, 40402])
async def test_queued_prompt_is_steered_or_left_to_its_server_lifecycle(
    steer_code,
) -> None:
    http, requests = exchange(
        [
            envelope({"prompt_id": "p1", "status": "queued"}),
            envelope({"steered": True}, code=steer_code),
        ]
    )
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        result = await client.submit_prompt(
            "s1", "hello", delivery=PromptDelivery.STEER_IF_ACTIVE
        )
    assert result == PromptSubmission(
        "p1",
        PromptOutcome.STEERED
        if steer_code == 0
        else PromptOutcome.STEERING_UNAVAILABLE,
    )
    assert [request.url.path for request in requests] == [
        "/api/v1/sessions/s1/prompts",
        "/api/v1/sessions/s1/prompts:steer",
    ]
    assert json.loads(requests[1].content) == {"prompt_ids": ["p1"]}


@pytest.mark.parametrize("code", [40001, 40401, 50000])
async def test_steering_rejection_preserves_acknowledged_submission(code) -> None:
    http, requests = exchange(
        [
            envelope({"prompt_id": "p1", "status": "queued"}),
            envelope(code=code),
        ]
    )
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        with pytest.raises(PromptSteeringError) as caught:
            await client.submit_prompt(
                "s1", "hello", delivery=PromptDelivery.STEER_IF_ACTIVE
            )
    assert caught.value.submission == PromptSubmission("p1", PromptOutcome.SUBMITTED)
    assert caught.value.uncertain is False
    assert isinstance(caught.value.__cause__, KimiServerAPIError)
    assert caught.value.__cause__.code == code
    assert len(requests) == 2


@pytest.mark.parametrize("during_steering", [False, True])
async def test_lost_response_does_not_resubmit(during_steering) -> None:
    responses = (
        [envelope({"prompt_id": "p1", "status": "queued"})] if during_steering else []
    )
    http, requests = exchange([*responses, httpx.ReadTimeout("lost response")])
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        with pytest.raises(
            PromptSteeringError if during_steering else PromptSubmissionUncertain
        ) as caught:
            await client.submit_prompt(
                "s1", "hello", delivery=PromptDelivery.STEER_IF_ACTIVE
            )
    if during_steering:
        assert caught.value.submission.prompt_id == "p1"
        assert caught.value.uncertain is True
    assert isinstance(caught.value.__cause__, KimiServerTransportError)
    assert sum(request.url.path.endswith("/prompts") for request in requests) == 1


async def test_submission_rejection_is_not_misclassified_as_uncertain() -> None:
    http, requests = exchange([envelope(code=40001)])
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        with pytest.raises(KimiServerAPIError):
            await client.submit_prompt(
                "s1", "hello", delivery=PromptDelivery.STEER_IF_ACTIVE
            )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"prompt_id": "p1"},
        {"prompt_id": 1, "status": "queued"},
        {"prompt_id": "", "status": "queued"},
        {"prompt_id": "p1", "status": "unknown"},
        {"prompt_id": "p1", "status": []},
    ],
)
async def test_malformed_submission_is_a_protocol_failure(data) -> None:
    http, requests = exchange([envelope(data)])
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        with pytest.raises(KimiServerProtocolError):
            await client.submit_prompt(
                "s1", "hello", delivery=PromptDelivery.STEER_IF_ACTIVE
            )
    assert len(requests) == 1


@pytest.mark.parametrize("data", [None, {}, {"steered": "yes"}, {"steered": False}])
async def test_malformed_steering_response_keeps_prompt_identity_in_diagnostic(
    data,
) -> None:
    http, requests = exchange(
        [
            envelope({"prompt_id": "p1", "status": "queued"}),
            envelope(data),
        ]
    )
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        with pytest.raises(KimiServerProtocolError) as caught:
            await client.submit_prompt(
                "s1", "hello", delivery=PromptDelivery.STEER_IF_ACTIVE
            )
    assert "p1" in str(caught.value)
    assert len(requests) == 2


async def test_delivery_keeps_the_original_server_connection() -> None:
    class Supervisor:
        calls = 0

        async def wait_until_ready(self) -> ServerConnection:
            self.calls += 1
            return ServerConnection(
                f"http://localhost:{1233 + self.calls}",
                1233 + self.calls,
                self.calls,
                "token",
            )

    supervisor = Supervisor()
    http, requests = exchange(
        [
            envelope({"prompt_id": "p1", "status": "queued"}),
            envelope({"steered": True}),
        ]
    )
    async with http:
        client = KimiServerClient(supervisor=supervisor, http_client=http)  # type: ignore[arg-type]
        await client.submit_prompt(
            "s1", "hello", delivery=PromptDelivery.STEER_IF_ACTIVE
        )
    assert supervisor.calls == 1
    assert [request.url.port for request in requests] == [1234, 1234]


@pytest.mark.parametrize("during_steering", [False, True])
async def test_cancellation_is_not_converted_to_an_operation_outcome(
    during_steering,
) -> None:
    responses = (
        [envelope({"prompt_id": "p1", "status": "queued"})] if during_steering else []
    )
    http, requests = exchange([*responses, asyncio.CancelledError()])
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        with pytest.raises(asyncio.CancelledError):
            await client.submit_prompt(
                "s1", "hello", delivery=PromptDelivery.STEER_IF_ACTIVE
            )
    assert len(requests) == (2 if during_steering else 1)


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        ("resolve_approval", 40401),
        ("resolve_approval", 40404),
        ("resolve_approval", 40902),
        ("resolve_question", 40401),
        ("resolve_question", 40405),
        ("resolve_question", 40902),
        ("resolve_question", 40909),
        ("dismiss_question", 40401),
        ("dismiss_question", 40405),
        ("dismiss_question", 40902),
    ],
)
async def test_expired_interactions_have_semantic_outcomes(operation, code) -> None:
    http, _ = exchange([envelope(code=code)])
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        args = (
            ("approved",)
            if operation == "resolve_approval"
            else ((),)
            if operation == "resolve_question"
            else ()
        )
        result = await getattr(client, operation)("s1", "i1", *args)
    assert result is InteractionResolution.EXPIRED


@pytest.mark.parametrize(
    "operation", ["resolve_approval", "resolve_question", "dismiss_question"]
)
async def test_interaction_validation_errors_remain_failures(operation) -> None:
    http, _ = exchange([envelope(code=40001)])
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        args = (
            ("approved",)
            if operation == "resolve_approval"
            else ((),)
            if operation == "resolve_question"
            else ()
        )
        with pytest.raises(KimiServerAPIError):
            await getattr(client, operation)("s1", "i1", *args)


@pytest.mark.parametrize("resolved", [True, False, "yes", None])
async def test_interaction_acknowledgement_requires_a_boolean(resolved) -> None:
    http, _ = exchange([envelope({"resolved": resolved})])
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        if isinstance(resolved, bool):
            result = await client.resolve_approval("s1", "i1", "approved")
            assert result is (
                InteractionResolution.APPLIED
                if resolved
                else InteractionResolution.EXPIRED
            )
        else:
            with pytest.raises(KimiServerProtocolError):
                await client.resolve_approval("s1", "i1", "approved")


async def test_dismissal_success_code_is_scoped_to_dismissal() -> None:
    http, _ = exchange(
        [
            envelope({"dismissed": True}, code=40909),
            envelope(code=40909),
        ]
    )
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        assert (
            await client.dismiss_question("s1", "i1") is InteractionResolution.APPLIED
        )
        with pytest.raises(KimiServerAPIError):
            await client.resolve_approval("s1", "i1", "approved")


async def test_session_lookup_distinguishes_missing_from_failure() -> None:
    http, _ = exchange(
        [
            envelope({"id": "s1"}),
            envelope(code=40401),
            envelope(code=40001),
        ]
    )
    async with http:
        client = KimiServerClient("http://localhost:1234", "token", http_client=http)
        assert await client.find_session("s1") == {"id": "s1"}
        assert await client.find_session("missing") is None
        with pytest.raises(KimiServerAPIError):
            await client.find_session("invalid")
