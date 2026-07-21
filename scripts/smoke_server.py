#!/usr/bin/env python3
"""Exercise the managed kimi server with one complete prompt turn."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from kimi_bridge.config import load_config
from kimi_bridge.kimi_server import KimiServerClient, KimiServerSupervisor


PROMPT = "Reply with exactly: PONG"
TURN_TIMEOUT_SECONDS = 180.0


async def _collect_turn(
    client: KimiServerClient, session_id: str
) -> tuple[str, dict[str, Any]]:
    deltas: list[str] = []
    events = client.subscribe_events(session_id)
    try:
        async for event in events:
            print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)
            payload = event.get("payload", {})
            if payload.get("type") == "assistant.delta":
                deltas.append(str(payload.get("delta", "")))
            if payload.get("type") == "turn.ended":
                return "".join(deltas), event
    finally:
        await events.aclose()
    raise AssertionError("event stream ended before turn.ended")


async def smoke() -> None:
    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    supervisor = KimiServerSupervisor(preferred_port=config.kimi_server.port)
    async with supervisor:
        print(f"managed kimi server ready at {supervisor.connection.base_url}")
        async with KimiServerClient(supervisor=supervisor) as client:
            await client.check_server_version()
            server_config = await client.get_config()
            default_model = server_config.get("default_model")
            if not isinstance(default_model, str) or not default_model:
                raise RuntimeError(
                    "kimi-code has no default_model; configure one before "
                    "running the smoke test"
                )
            smoke_workspace = config.default_workspace / ".smoke"
            smoke_workspace.mkdir(parents=True, exist_ok=True)
            session_id = await client.create_session(str(smoke_workspace))
            print(f"created session {session_id}")
            logging.getLogger(__name__).info(
                "smoke session uses permission_mode=auto so the "
                "non-interactive check is fully autonomous and never asks questions"
            )

            collector = asyncio.create_task(
                _collect_turn(client, session_id), name="smoke-event-collector"
            )
            try:
                await client.wait_until_subscribed(session_id)
                await client.submit_prompt(
                    session_id,
                    PROMPT,
                    model=default_model,
                    permission_mode="auto",
                )
                assistant_text, turn_end = await asyncio.wait_for(
                    collector, TURN_TIMEOUT_SECONDS
                )
            finally:
                if not collector.done():
                    collector.cancel()
                    try:
                        await collector
                    except asyncio.CancelledError:
                        pass

            reason = turn_end["payload"]["reason"]
            if reason != "completed":
                raise AssertionError(f"turn ended with reason {reason!r}")
            if "PONG" not in assistant_text:
                raise AssertionError(
                    f"assistant deltas did not contain PONG: {assistant_text!r}"
                )
            print("smoke passed: observed PONG and a completed turn")


if __name__ == "__main__":
    asyncio.run(smoke())
