"""Project native conversation turns and items into bridge snapshots."""

from __future__ import annotations

from typing import Any

from .types import KimiServerProtocolError


def snapshot_from_turns(page: dict[str, Any]) -> dict[str, Any]:
    turns = page.get("turns")
    if not isinstance(turns, list):
        raise KimiServerProtocolError("turns page must carry a turn list")
    messages: list[dict[str, Any]] = []
    in_flight: dict[str, Any] | None = None
    for turn in turns:
        _validate_turn(turn)
        messages.extend(_messages_from_turn(turn))
    current = _current_turn(turns)
    if current is not None:
        answer, thinking, step = stream_text_from_turn(current)
        in_flight = {
            "turn_id": current["ordinal"],
            "step": step,
            "assistant_text": answer,
            "thinking_text": thinking,
        }
        prompt_id = turn_prompt_id(current)
        if prompt_id is not None:
            in_flight["current_prompt_id"] = prompt_id
    return {
        "turn_ids": [turn["ordinal"] for turn in turns],
        "messages": {
            "items": messages[-100:],
            "has_more": bool(page.get("has_more", False)) or len(messages) > 100,
        },
        "in_flight_turn": in_flight,
    }


def stream_text_from_turn(turn: dict[str, Any]) -> tuple[str, str, int | None]:
    step = _current_step(turn)
    answer: list[str] = []
    thinking: list[str] = []
    for item in turn.get("items") or ():
        if item.get("step_index") != step:
            continue
        item_type = item.get("type")
        if item_type == "user_message" and item.get("placement") == "steer":
            answer.clear()
            thinking.clear()
        elif item_type == "assistant_message":
            answer.append(_item_text(item))
        elif item_type == "reasoning":
            thinking.append(_item_text(item))
    return "".join(answer), "".join(thinking), step


def turn_prompt_ids(turn: dict[str, Any]) -> list[str]:
    identifiers: list[str] = []
    for item in turn.get("items") or ():
        if item.get("type") != "user_message":
            continue
        for prompt_id in item.get("prompt_ids") or ():
            if prompt_id not in identifiers:
                identifiers.append(prompt_id)
    return identifiers


def turn_prompt_id(turn: dict[str, Any]) -> str | None:
    identifiers = turn_prompt_ids(turn)
    return identifiers[0] if identifiers else None


def turn_opens_with_user_input(turn: dict[str, Any]) -> bool:
    if turn.get("kind") != "agent":
        return False
    opener = next(
        (
            item
            for item in turn.get("items") or ()
            if item.get("type") == "user_message"
            and item.get("placement") == "opener"
        ),
        None,
    )
    if opener is None:
        return False
    origin = opener.get("origin")
    return isinstance(origin, dict) and origin.get("kind") in {
        "user",
        "skill",
        "plugin_command",
    }


def _messages_from_turn(turn: dict[str, Any]) -> list[dict[str, Any]]:
    if turn.get("kind") != "agent":
        return []
    prompt_id = turn_prompt_id(turn)
    messages: list[dict[str, Any]] = []
    items = turn.get("items") or []
    for item in items:
        if (
            item.get("type") == "user_message"
            and item.get("placement") == "opener"
        ):
            parts = _user_parts(item)
            if parts:
                messages.append(_message("user", parts, _message_prompt_id(item)))

    step_indexes = {step["index"] for step in turn.get("steps") or ()}
    step_indexes.update(
        item["step_index"]
        for item in items
        if isinstance(item.get("step_index"), int)
        and not isinstance(item.get("step_index"), bool)
        and item.get("placement") != "opener"
    )
    for step in sorted(step_indexes):
        assistant: list[dict[str, Any]] = []
        for item in items:
            if item.get("step_index") != step or item.get("placement") == "opener":
                continue
            item_type = item.get("type")
            if item_type == "reasoning":
                assistant.append({"type": "thinking", "thinking": _item_text(item)})
            elif item_type == "tool_call":
                assistant.append({"type": "tool_use"})
            elif item_type == "assistant_message":
                text = _item_text(item)
                if text:
                    assistant.append({"type": "text", "text": text})
            elif item_type == "user_message":
                if assistant:
                    messages.append(_message("assistant", assistant, prompt_id))
                    assistant = []
                parts = _user_parts(item)
                if parts:
                    messages.append(
                        _message("user", parts, _message_prompt_id(item) or prompt_id)
                    )
        messages.append(_message("assistant", assistant, prompt_id))
    return messages


def _current_turn(turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            turn
            for turn in reversed(turns)
            if turn.get("kind") == "agent" and turn.get("status") == "running"
        ),
        None,
    )


def _current_step(turn: dict[str, Any]) -> int | None:
    steps = [
        item["step_index"]
        for item in turn.get("items") or ()
        if isinstance(item.get("step_index"), int)
        and not isinstance(item.get("step_index"), bool)
        and item.get("placement") != "opener"
    ]
    return max(steps) if steps else None


def _user_parts(item: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [{"type": "text", "text": item["text"]}] if item.get("text") else []
    for attachment in item.get("attachments") or ():
        part = {"type": attachment.get("media_type") or attachment["kind"]}
        name = attachment.get("name")
        if name is not None:
            part["name"] = name
        parts.append(part)
    return parts


def _message(
    role: str, parts: list[dict[str, Any]], prompt_id: str | None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "content": parts}
    if prompt_id is not None:
        message["prompt_id"] = prompt_id
    return message


def _message_prompt_id(item: dict[str, Any]) -> str | None:
    prompt_ids = item.get("prompt_ids") or ()
    return str(prompt_ids[0]) if prompt_ids else None


def _item_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if not isinstance(text, str):
        raise KimiServerProtocolError("conversation text item must carry text")
    return text


def _validate_turn(turn: dict[str, Any]) -> None:
    if not isinstance(turn, dict):
        raise KimiServerProtocolError("conversation turn must be an object")
    if not isinstance(turn.get("turn_id"), str) or not turn["turn_id"]:
        raise KimiServerProtocolError("conversation turn has no opaque turn_id")
    ordinal = turn.get("ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise KimiServerProtocolError("conversation turn has no numeric ordinal")
    if turn.get("kind") not in {"agent", "system"}:
        raise KimiServerProtocolError("conversation turn has an unknown kind")
    if turn.get("status") not in {"running", "ended"}:
        raise KimiServerProtocolError("conversation turn has an unknown status")
    items = turn.get("items")
    if items is not None and not isinstance(items, list):
        raise KimiServerProtocolError("conversation turn items must be a list")
