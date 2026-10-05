"""Submission and optional steering as one Kimi-owned delivery operation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .types import (
    KimiServerAPIError,
    KimiServerProtocolError,
    KimiServerTransportError,
    PromptDelivery,
    PromptOutcome,
    PromptSteeringError,
    PromptSubmission,
    PromptSubmissionUncertain,
)


async def deliver_prompt(
    request: Callable[[str, dict[str, Any]], Awaitable[Any]],
    payload: dict[str, Any],
    delivery: PromptDelivery,
) -> PromptSubmission:
    """Use one connection and submit once, regardless of the steering outcome."""

    try:
        data = await request("submit_prompt", payload)
    except KimiServerTransportError as exc:
        raise PromptSubmissionUncertain() from exc
    if not isinstance(data, dict):
        raise KimiServerProtocolError("prompt submission is not an object")
    prompt_id = data.get("prompt_id")
    status = data.get("status")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise KimiServerProtocolError("prompt submission has no prompt id")
    if not isinstance(status, str) or status not in {"running", "queued", "blocked"}:
        raise KimiServerProtocolError("prompt submission has an invalid status")
    if status == "blocked":
        return PromptSubmission(prompt_id, PromptOutcome.BLOCKED)
    submitted = PromptSubmission(prompt_id, PromptOutcome.SUBMITTED)
    if delivery is PromptDelivery.ENQUEUE or status != "queued":
        return submitted

    try:
        data = await request("steer_prompts", {"prompt_ids": [prompt_id]})
    except KimiServerAPIError as exc:
        # This fresh, acknowledged ID can cease to be pending or lose its
        # active target between requests. Leave its server lifecycle alone.
        if exc.code == 40402:
            return PromptSubmission(prompt_id, PromptOutcome.STEERING_UNAVAILABLE)
        raise PromptSteeringError(submitted, uncertain=False) from exc
    except KimiServerTransportError as exc:
        raise PromptSteeringError(submitted, uncertain=True) from exc
    if not isinstance(data, dict) or data.get("steered") is not True:
        raise KimiServerProtocolError(
            f"prompt {prompt_id} was submitted, but its steering response is invalid"
        )
    return PromptSubmission(prompt_id, PromptOutcome.STEERED)
