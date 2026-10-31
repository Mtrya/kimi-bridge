"""Project native transcript turns into the bridge's message snapshot."""

from __future__ import annotations

from typing import Any

from .events import _EventCursor


def snapshot_from_transcript(
    transcript: dict[str, Any], cursor: _EventCursor
) -> dict[str, Any]:
    """Keep transcript sequence numbers separate from session-event cursors."""

    attachments = {item["attachmentId"]: item for item in transcript["attachments"]}
    messages: list[dict[str, Any]] = []
    in_flight: dict[str, Any] | None = None
    for turn in transcript["items"]:
        if turn["kind"] != "turn" or turn["state"] == "queued":
            continue
        prompt_id = turn.get("triggerPromptId")
        opener = _text_parts(turn.get("prompt", ""), turn, attachments)
        if opener:
            messages.append(_message("user", opener, prompt_id))
        last_assistant: list[dict[str, Any]] = []
        for step in turn["steps"]:
            assistant: list[dict[str, Any]] = []
            for frame in step["frames"]:
                if frame["kind"] == "thinking":
                    assistant.append({"type": "thinking", "thinking": frame["text"]})
                elif frame["kind"] == "tool":
                    # Preserve an empty-text assistant step so reconciliation
                    # cannot reuse an earlier answer for a tool-only ending.
                    assistant.append({"type": "tool_use"})
                elif frame["kind"] == "text":
                    parts = _text_parts(frame["text"], frame, attachments)
                    if frame["role"] == "user":
                        if assistant:
                            messages.append(_message("assistant", assistant, prompt_id))
                            assistant = []
                        messages.append(_message("user", parts, prompt_id))
                    else:
                        assistant.extend(parts)
            if assistant:
                messages.append(_message("assistant", assistant, prompt_id))
            last_assistant = assistant
        if turn["state"] == "running":
            in_flight = {
                "turn_id": turn["ordinal"],
                "assistant_text": "".join(
                    part["text"] for part in last_assistant if part["type"] == "text"
                ),
                "thinking_text": "".join(
                    part["thinking"]
                    for part in last_assistant
                    if part["type"] == "thinking"
                ),
            }
            if prompt_id is not None:
                in_flight["current_prompt_id"] = prompt_id
    return {
        "as_of_seq": cursor.seq,
        "epoch": cursor.epoch,
        "messages": {
            "items": messages[-100:],
            "has_more": transcript["has_more"] or len(messages) > 100,
        },
        "in_flight_turn": in_flight,
    }


def _message(
    role: str, parts: list[dict[str, Any]], prompt_id: str | None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "content": parts}
    if prompt_id is not None:
        message["prompt_id"] = prompt_id
    return message


def _text_parts(
    text: str, source: dict[str, Any], attachments: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for attachment_id in source.get("attachmentIds", []):
        attachment = attachments.get(attachment_id)
        if attachment is None:
            parts.append({"type": "attachment", "name": attachment_id})
            continue
        part = {"type": attachment["mediaType"]}
        if "name" in attachment:
            part["name"] = attachment["name"]
        parts.append(part)
    return parts
